import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Agent,
    Artifact,
    Domain,
    Report,
    RuntimeSetting,
    Task,
    ToolCall,
    ToolConnection,
)
from app.db.repositories import AgentRepository, DomainRepository
from app.db.seed import seed_default_domains
from app.llm.client import LLMClient, OpenAILLMClient
from app.memory.retrieval import (
    MemoryContextBundle,
    MemoryContextBundleRequest,
    MemoryRetrievalService,
)
from app.tools.runtime import (
    ToolExecutionRequest,
    ToolExecutionService,
    tool_result_payload,
)

PromptCaller = Literal["maestro", "user", "system"]


@dataclass(frozen=True)
class ToolManifestItem:
    key: str
    name: str
    permission: str
    description: str
    connection_id: str | None = None
    auth_type: str | None = None


@dataclass(frozen=True)
class AgentSpec:
    id: uuid.UUID
    key: str
    name: str
    domain_key: str
    agent_type: str
    role_summary: str
    role_prompt: str
    memory_profile: str
    model_profile: str
    allowed_tools: list[ToolManifestItem]
    is_active: bool
    current_action: str | None = None
    scheduled_actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DomainContextSpec:
    id: uuid.UUID
    key: str
    name: str
    context: str
    is_active: bool


@dataclass(frozen=True)
class ToolRegistryItem:
    key: str
    name: str
    description: str
    exclusive: bool
    connected_domains: list[str]
    authorized_agents: list[dict[str, str]]


@dataclass(frozen=True)
class GlobalContextSpec:
    context: str


@dataclass(frozen=True)
class ToolConnectionSpec:
    id: uuid.UUID
    domain_key: str
    tool_key: str
    display_name: str
    auth_type: str
    config: dict[str, Any]
    is_active: bool


@dataclass(frozen=True)
class AgentTaskSpec:
    id: uuid.UUID
    status: str
    priority: str
    source_type: str
    workflow_key: str | None
    objective: str
    started_at: str | None
    completed_at: str | None
    error_message: str | None


@dataclass(frozen=True)
class PromptPackageRequest:
    agent_key: str
    task_instruction: str
    caller: PromptCaller = "maestro"
    user_context: str | None = None
    query_text: str | None = None
    max_memory_items: int = 10
    max_memory_chars: int = 3500
    use_semantic: bool = True


@dataclass(frozen=True)
class AgentToolRequest:
    tool_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


@dataclass(frozen=True)
class PromptPackage:
    agent: AgentSpec
    task_instruction: str
    caller: PromptCaller
    global_context: str
    domain_context: str
    role_prompt: str
    user_context: str | None
    memory_context: MemoryContextBundle
    tool_manifest: list[ToolManifestItem]
    output_contract: dict[str, Any]
    assembled_prompt: str
    created_at: str


@dataclass(frozen=True)
class InteractionArtifactPackage:
    schema_version: str
    package_id: str
    created_at: str
    domain_key: str
    agent_key: str | None
    task_id: str | None
    conversation_id: str | None
    user_input: str | None
    maestro_tasking: str | None
    agent_output: str | None
    tool_calls: list[dict[str, Any]]
    generated_artifacts: list[dict[str, Any]]
    open_questions: list[str]
    next_steps: list[str]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class StagedInteractionArtifact:
    package: InteractionArtifactPackage
    path: str | None = None
    artifact_id: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    run_id: str
    status: str
    agent: AgentSpec
    prompt_package: PromptPackage
    scheduler: dict[str, Any]
    execution_note: str
    output_text: str | None = None
    task_id: str | None = None
    report_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_loop: dict[str, Any] = field(default_factory=dict)
    staged_artifact_path: str | None = None
    artifact_id: str | None = None
    error_message: str | None = None


class AgentRuntimeError(ValueError):
    pass


class AgentRegistryService:
    def __init__(self, session: Session):
        self.session = session

    def ensure_seed_agents(self) -> list[Agent]:
        seed_default_domains(self.session)
        created_or_existing: list[Agent] = []
        repo = AgentRepository(self.session)
        domain_repo = DomainRepository(self.session)
        for seed in _SEED_AGENTS:
            existing = repo.get_by_key(seed["key"])
            if existing is not None:
                capabilities = dict(existing.capabilities or {})
                if not capabilities.get("manual_tool_permissions"):
                    merged_permissions = dict(existing.tool_permissions or {})
                    changed = False
                    for tool_key, permission in seed["tool_permissions"].items():
                        if tool_key not in merged_permissions:
                            merged_permissions[tool_key] = permission
                            changed = True
                    if changed:
                        existing.tool_permissions = merged_permissions
                        self.session.commit()
                        self.session.refresh(existing)
                created_or_existing.append(existing)
                continue
            domain = domain_repo.get_by_key(seed["domain_key"])
            if domain is None:
                continue
            created_or_existing.append(
                repo.create(
                    domain_id=domain.id,
                    key=seed["key"],
                    name=seed["name"],
                    agent_type=seed["agent_type"],
                    description=seed["role_summary"],
                    capabilities={
                        "role_summary": seed["role_summary"],
                        "role_prompt": seed["role_prompt"],
                        "memory_profile": seed["memory_profile"],
                        "model_profile": seed["model_profile"],
                        "current_action": None,
                        "scheduled_actions": [],
                        "output_contract": _DEFAULT_OUTPUT_CONTRACT,
                    },
                    tool_permissions=seed["tool_permissions"],
                )
            )
        return created_or_existing

    def list_specs(self) -> list[AgentSpec]:
        self.ensure_seed_agents()
        domains_by_id = {
            domain.id: domain for domain in DomainRepository(self.session).list_active()
        }
        agents = self.session.scalars(
            select(Agent).where(Agent.is_active.is_(True)).order_by(Agent.key)
        ).all()
        return [
            self._spec_for_agent(agent, domain=domains_by_id.get(agent.domain_id))
            for agent in agents
            if domains_by_id.get(agent.domain_id) is not None
        ]

    def list_domain_contexts(self) -> list[DomainContextSpec]:
        seed_default_domains(self.session)
        domains = DomainRepository(self.session).list_active()
        return [
            DomainContextSpec(
                id=domain.id,
                key=domain.key,
                name=domain.name,
                context=domain.description or _DOMAIN_CONTEXTS.get(domain.key, ""),
                is_active=domain.is_active,
            )
            for domain in domains
        ]

    def get_global_context(self) -> GlobalContextSpec:
        setting = self.session.get(RuntimeSetting, _GLOBAL_CONTEXT_SETTING_KEY)
        if setting is None:
            return GlobalContextSpec(context=_GLOBAL_MAESTRO_CONTEXT)
        return GlobalContextSpec(
            context=str(setting.value.get("context") or _GLOBAL_MAESTRO_CONTEXT)
        )

    def update_global_context(self, context: str) -> GlobalContextSpec:
        cleaned = context.strip()
        if not cleaned:
            raise AgentRuntimeError("Global Maestro context cannot be blank.")
        setting = self.session.get(RuntimeSetting, _GLOBAL_CONTEXT_SETTING_KEY)
        if setting is None:
            setting = RuntimeSetting(
                key=_GLOBAL_CONTEXT_SETTING_KEY,
                value={"context": cleaned},
            )
            self.session.add(setting)
        else:
            setting.value = {"context": cleaned}
        self.session.commit()
        return GlobalContextSpec(context=cleaned)

    def update_domain_context(self, domain_key: str, context: str) -> DomainContextSpec:
        seed_default_domains(self.session)
        domain = DomainRepository(self.session).get_by_key(domain_key)
        if domain is None:
            raise AgentRuntimeError(f"Unknown domain: {domain_key}")
        domain.description = context
        self.session.commit()
        self.session.refresh(domain)
        return DomainContextSpec(
            id=domain.id,
            key=domain.key,
            name=domain.name,
            context=domain.description or "",
            is_active=domain.is_active,
        )

    def get_spec(self, agent_key: str) -> AgentSpec:
        self.ensure_seed_agents()
        agent = AgentRepository(self.session).get_by_key(agent_key)
        if agent is None:
            raise AgentRuntimeError(f"Unknown agent: {agent_key}")
        domain = DomainRepository(self.session).get(agent.domain_id)
        if domain is None:
            raise AgentRuntimeError(f"Agent {agent_key} has no active domain.")
        return self._spec_for_agent(agent, domain=domain)

    def create_agent_spec(
        self,
        *,
        domain_key: str,
        key: str,
        name: str,
        agent_type: str = "domain_agent",
        role_summary: str = "",
        role_prompt: str = "",
        memory_profile: str = "agent_prompt",
        model_profile: str = "default",
        tool_permissions: dict[str, Any] | None = None,
        current_action: str | None = None,
    ) -> AgentSpec:
        self.ensure_seed_agents()
        domain = DomainRepository(self.session).get_by_key(domain_key)
        if domain is None:
            raise AgentRuntimeError(f"Unknown domain: {domain_key}")
        cleaned_key = _slug(key)
        if not cleaned_key:
            raise AgentRuntimeError("Agent key cannot be blank.")
        if AgentRepository(self.session).get_by_key(cleaned_key) is not None:
            raise AgentRuntimeError(f"Agent key already exists: {cleaned_key}")
        cleaned_name = name.strip()
        if not cleaned_name:
            raise AgentRuntimeError("Agent name cannot be blank.")
        capabilities = {
            "role_summary": role_summary.strip(),
            "role_prompt": role_prompt.strip(),
            "memory_profile": memory_profile.strip() or "agent_prompt",
            "model_profile": model_profile.strip() or "default",
            "current_action": current_action.strip() if current_action else None,
            "scheduled_actions": [],
            "output_contract": _DEFAULT_OUTPUT_CONTRACT,
        }
        agent = AgentRepository(self.session).create(
            domain_id=domain.id,
            key=cleaned_key,
            name=cleaned_name,
            agent_type=agent_type.strip() or "domain_agent",
            description=capabilities["role_summary"],
            capabilities=capabilities,
            tool_permissions=tool_permissions or {},
        )
        return self._spec_for_agent(agent, domain=domain)

    def update_agent_spec(
        self,
        agent_key: str,
        *,
        role_summary: str | None = None,
        role_prompt: str | None = None,
        memory_profile: str | None = None,
        model_profile: str | None = None,
        tool_permissions: dict[str, Any] | None = None,
        current_action: str | None = None,
        scheduled_actions: list[dict[str, Any]] | None = None,
        is_active: bool | None = None,
    ) -> AgentSpec:
        self.ensure_seed_agents()
        agent = AgentRepository(self.session).get_by_key(agent_key)
        if agent is None:
            raise AgentRuntimeError(f"Unknown agent: {agent_key}")
        capabilities = dict(agent.capabilities or {})
        if role_summary is not None:
            agent.description = role_summary
            capabilities["role_summary"] = role_summary
        if role_prompt is not None:
            capabilities["role_prompt"] = role_prompt
        if memory_profile is not None:
            capabilities["memory_profile"] = memory_profile
        if model_profile is not None:
            capabilities["model_profile"] = model_profile
        if current_action is not None:
            capabilities["current_action"] = current_action or None
        if scheduled_actions is not None:
            capabilities["scheduled_actions"] = scheduled_actions
        if tool_permissions is not None:
            agent.tool_permissions = tool_permissions
            capabilities["manual_tool_permissions"] = True
        if is_active is not None:
            agent.is_active = is_active
        agent.capabilities = capabilities
        self.session.commit()
        self.session.refresh(agent)
        return self.get_spec(agent.key)

    def archive_agent(self, agent_key: str) -> AgentSpec:
        self.ensure_seed_agents()
        agent = AgentRepository(self.session).get_by_key(agent_key)
        if agent is None:
            raise AgentRuntimeError(f"Unknown agent: {agent_key}")
        capabilities = dict(agent.capabilities or {})
        capabilities["current_action"] = None
        agent.capabilities = capabilities
        agent.is_active = False
        self.session.commit()
        self.session.refresh(agent)
        domain = DomainRepository(self.session).get(agent.domain_id)
        return self._spec_for_agent(agent, domain=domain)

    def list_agent_tasks(self, agent_key: str, *, limit: int = 20) -> list[AgentTaskSpec]:
        spec = self.get_spec(agent_key)
        tasks = self.session.scalars(
            select(Task)
            .where(Task.assigned_agent_id == spec.id)
            .order_by(Task.created_at.desc())
            .limit(limit)
        ).all()
        return [
            AgentTaskSpec(
                id=task.id,
                status=task.status,
                priority=task.priority,
                source_type=task.source_type,
                workflow_key=task.workflow_key,
                objective=task.objective,
                started_at=task.started_at.isoformat() if task.started_at else None,
                completed_at=task.completed_at.isoformat() if task.completed_at else None,
                error_message=task.error_message,
            )
            for task in tasks
        ]

    def list_tools(self) -> list[ToolRegistryItem]:
        self.ensure_seed_agents()
        domains_by_id = {
            domain.id: domain for domain in DomainRepository(self.session).list_active()
        }
        connections = self.session.scalars(
            select(ToolConnection).where(ToolConnection.is_active.is_(True))
        ).all()
        connected_domains: dict[str, set[str]] = {}
        for connection in connections:
            domain = domains_by_id.get(connection.domain_id)
            if domain is None:
                continue
            connected_domains.setdefault(connection.tool_key, set()).add(domain.key)
            for inherited_tool_key in _inherited_connection_tool_keys(connection.tool_key):
                connected_domains.setdefault(inherited_tool_key, set()).add(domain.key)

        authorized_agents: dict[str, list[dict[str, str]]] = {}
        for agent in self.session.scalars(select(Agent).where(Agent.is_active.is_(True))).all():
            domain = domains_by_id.get(agent.domain_id)
            if domain is None:
                continue
            for tool_key, value in (agent.tool_permissions or {}).items():
                permission = value if isinstance(value, str) else value.get("permission", "use")
                authorized_agents.setdefault(tool_key, []).append(
                    {
                        "agent_key": agent.key,
                        "agent_name": agent.name,
                        "domain_key": domain.key,
                        "permission": str(permission),
                    }
                )

        known_tool_keys = set(_TOOL_DESCRIPTIONS) | set(connected_domains) | set(authorized_agents)
        return [
            ToolRegistryItem(
                key=tool_key,
                name=_TOOL_DESCRIPTIONS.get(tool_key, {}).get("name", tool_key),
                description=_TOOL_DESCRIPTIONS.get(tool_key, {}).get("description", ""),
                exclusive=bool(_TOOL_DESCRIPTIONS.get(tool_key, {}).get("exclusive", False)),
                connected_domains=sorted(connected_domains.get(tool_key, set())),
                authorized_agents=sorted(
                    authorized_agents.get(tool_key, []),
                    key=lambda item: (item["domain_key"], item["agent_key"]),
                ),
            )
            for tool_key in sorted(known_tool_keys)
        ]

    def list_tool_connections(self) -> list[ToolConnectionSpec]:
        seed_default_domains(self.session)
        domains_by_id = {
            domain.id: domain for domain in DomainRepository(self.session).list_active()
        }
        connections = self.session.scalars(
            select(ToolConnection).order_by(ToolConnection.tool_key, ToolConnection.display_name)
        ).all()
        specs: list[ToolConnectionSpec] = []
        for connection in connections:
            domain = domains_by_id.get(connection.domain_id)
            if domain is None:
                continue
            specs.append(
                ToolConnectionSpec(
                    id=connection.id,
                    domain_key=domain.key,
                    tool_key=connection.tool_key,
                    display_name=connection.display_name,
                    auth_type=connection.auth_type,
                    config=_redact_config(connection.config or {}),
                    is_active=connection.is_active,
                )
            )
        return specs

    def upsert_tool_connection(
        self,
        *,
        domain_key: str,
        tool_key: str,
        display_name: str,
        auth_type: str,
        config: dict[str, Any] | None = None,
        is_active: bool = True,
    ) -> ToolConnectionSpec:
        seed_default_domains(self.session)
        domain = DomainRepository(self.session).get_by_key(domain_key)
        if domain is None:
            raise AgentRuntimeError(f"Unknown domain: {domain_key}")
        cleaned_tool_key = tool_key.strip()
        if not cleaned_tool_key:
            raise AgentRuntimeError("Tool key cannot be blank.")
        existing = self.session.scalar(
            select(ToolConnection).where(
                ToolConnection.domain_id == domain.id,
                ToolConnection.tool_key == cleaned_tool_key,
            )
        )
        merged_config = _merge_secret_config(existing.config if existing else {}, config or {})
        if existing is None:
            existing = ToolConnection(
                domain_id=domain.id,
                tool_key=cleaned_tool_key,
                display_name=display_name.strip() or cleaned_tool_key,
                auth_type=auth_type.strip() or "manual",
                config=merged_config,
                is_active=is_active,
            )
            self.session.add(existing)
        else:
            existing.display_name = display_name.strip() or existing.display_name
            existing.auth_type = auth_type.strip() or existing.auth_type
            existing.config = merged_config
            existing.is_active = is_active
        self.session.commit()
        self.session.refresh(existing)
        return ToolConnectionSpec(
            id=existing.id,
            domain_key=domain.key,
            tool_key=existing.tool_key,
            display_name=existing.display_name,
            auth_type=existing.auth_type,
            config=_redact_config(existing.config or {}),
            is_active=existing.is_active,
        )

    def _spec_for_agent(self, agent: Agent, *, domain: Domain | None) -> AgentSpec:
        if domain is None:
            raise AgentRuntimeError(f"Agent {agent.key} has no domain.")
        capabilities = agent.capabilities or {}
        return AgentSpec(
            id=agent.id,
            key=agent.key,
            name=agent.name,
            domain_key=domain.key,
            agent_type=agent.agent_type,
            role_summary=capabilities.get("role_summary") or agent.description or "",
            role_prompt=capabilities.get("role_prompt") or "",
            memory_profile=capabilities.get("memory_profile") or "agent_prompt",
            model_profile=capabilities.get("model_profile") or "default",
            allowed_tools=self._tool_manifest(agent, domain=domain),
            is_active=agent.is_active,
            current_action=capabilities.get("current_action"),
            scheduled_actions=list(capabilities.get("scheduled_actions") or []),
        )

    def _tool_manifest(self, agent: Agent, *, domain: Domain) -> list[ToolManifestItem]:
        permissions = agent.tool_permissions or {}
        connections = {
            connection.tool_key: connection
            for connection in self.session.scalars(
                select(ToolConnection).where(
                    ToolConnection.domain_id == domain.id,
                    ToolConnection.is_active.is_(True),
                )
            ).all()
        }
        manifest: list[ToolManifestItem] = []
        for key, value in sorted(permissions.items()):
            permission = "use"
            description = ""
            if isinstance(value, str):
                permission = value
            elif isinstance(value, dict):
                permission = str(value.get("permission") or "use")
                description = str(value.get("description") or "")
            connection = connections.get(key)
            if connection is None:
                connection = connections.get(_provider_connection_key(key))
            manifest.append(
                ToolManifestItem(
                    key=key,
                    name=_TOOL_DESCRIPTIONS.get(key, {}).get("name", key),
                    permission=permission,
                    description=description
                    or _TOOL_DESCRIPTIONS.get(key, {}).get("description", ""),
                    connection_id=str(connection.id) if connection is not None else None,
                    auth_type=connection.auth_type if connection is not None else None,
                )
            )
        return manifest


class PromptAggregationService:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: LLMClient | None = None,
        tool_adapters: dict[str, Any] | None = None,
    ):
        self.session = session
        self.registry = AgentRegistryService(session)
        self.llm_client = llm_client
        self.tool_adapters = tool_adapters

    def build_prompt_package(self, request: PromptPackageRequest) -> PromptPackage:
        spec = self.registry.get_spec(request.agent_key)
        domain = DomainRepository(self.session).get_by_key(spec.domain_key)
        if domain is None:
            raise AgentRuntimeError(f"Unknown domain for agent: {spec.domain_key}")

        query_text = request.query_text or request.task_instruction
        memory_context = MemoryRetrievalService(self.session).build_context_bundle(
            MemoryContextBundleRequest(
                profile=spec.memory_profile,  # type: ignore[arg-type]
                audience="agent",
                domain_id=domain.id,
                agent_id=spec.id,
                query_text=query_text,
                use_semantic=request.use_semantic,
                max_items=request.max_memory_items,
                max_chars=request.max_memory_chars,
            )
        )
        global_context = self.registry.get_global_context().context
        output_contract = _DEFAULT_OUTPUT_CONTRACT
        return PromptPackage(
            agent=spec,
            task_instruction=request.task_instruction,
            caller=request.caller,
            global_context=global_context,
            domain_context=domain.description or _DOMAIN_CONTEXTS.get(spec.domain_key, ""),
            role_prompt=spec.role_prompt,
            user_context=request.user_context,
            memory_context=memory_context,
            tool_manifest=spec.allowed_tools,
            output_contract=output_contract,
            assembled_prompt=self._render_prompt(
                global_context=global_context,
                domain_context=domain.description or _DOMAIN_CONTEXTS.get(spec.domain_key, ""),
                role_prompt=spec.role_prompt,
                task_instruction=request.task_instruction,
                user_context=request.user_context,
                memory_text=memory_context.rendered_text,
                tools=spec.allowed_tools,
                output_contract=output_contract,
            ),
            created_at=datetime.now(UTC).isoformat(),
        )

    def _render_prompt(
        self,
        *,
        global_context: str,
        domain_context: str,
        role_prompt: str,
        task_instruction: str,
        user_context: str | None,
        memory_text: str,
        tools: list[ToolManifestItem],
        output_contract: dict[str, Any],
    ) -> str:
        sections = [
            ("Global Maestro Context", global_context),
            ("Domain Context", domain_context),
            ("Agent Role", role_prompt),
            ("Task", task_instruction),
        ]
        if user_context:
            sections.append(("User Context", user_context))
        if memory_text:
            sections.append(("Retrieved Memory", memory_text))
        tools_text = "\n".join(
            f"- {tool.key} ({tool.permission}): {tool.description or tool.name}"
            for tool in tools
        ) or "- No external tools are currently authorized for this agent."
        sections.append(("Authorized Tools", tools_text))
        sections.append(("Output Contract", json.dumps(output_contract, indent=2)))
        return "\n\n".join(f"## {title}\n{body}".strip() for title, body in sections)

    def run_agent_once(
        self,
        request: PromptPackageRequest,
        *,
        stage_interaction: bool = False,
        execute_llm: bool = True,
        tool_requests: list[AgentToolRequest] | None = None,
        initial_tool_results: list[dict[str, Any]] | None = None,
        auto_tool_loop: bool = False,
        max_tool_iterations: int = 2,
        parent_task_id: uuid.UUID | None = None,
        source_type: str = "manual_run_once",
        workflow_key: str = "agent.run_once",
        priority: str = "normal",
    ) -> AgentRunResult:
        package = self.build_prompt_package(request)
        run_id = str(uuid.uuid4())
        domain = DomainRepository(self.session).get_by_key(package.agent.domain_key)
        if domain is None:
            raise AgentRuntimeError(f"Unknown domain: {package.agent.domain_key}")
        task = Task(
            parent_task_id=parent_task_id,
            domain_id=domain.id,
            assigned_agent_id=package.agent.id,
            status="running" if execute_llm else "prepared",
            priority=priority,
            source_type=source_type,
            workflow_key=workflow_key,
            objective=request.task_instruction,
            input_payload={
                "run_id": run_id,
                "caller": request.caller,
                "query_text": request.query_text,
                "execute_llm": execute_llm,
                "stage_interaction": stage_interaction,
                "auto_tool_loop": auto_tool_loop,
                "max_tool_iterations": max_tool_iterations,
                "prompt_context": {
                    "task_instruction": request.task_instruction,
                    "user_context": request.user_context,
                    "assembled_prompt_chars": len(package.assembled_prompt),
                    "memory_included_count": package.memory_context.included_count,
                    "semantic_status": package.memory_context.semantic_status,
                },
                "requested_tools": [
                    {
                        "tool_key": tool_request.tool_key,
                        "dry_run": tool_request.dry_run,
                    }
                    for tool_request in (tool_requests or [])
                ],
                "initial_tool_result_ids": [
                    result.get("id")
                    for result in (initial_tool_results or [])
                    if result.get("id")
                ],
            },
            started_at=datetime.now(UTC) if execute_llm else None,
        )
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        self._set_agent_current_action(package.agent.key, request.task_instruction)

        execution_note = "Manual run prepared."
        output_text: str | None = None
        error_message: str | None = None
        tool_call_payloads: list[dict[str, Any]] = list(initial_tool_results or [])
        tool_loop_trace: dict[str, Any] = {"enabled": auto_tool_loop, "iterations": []}
        report_id: str | None = None
        status = "prepared"

        if tool_requests:
            tool_service = ToolExecutionService(self.session, adapters=self.tool_adapters)
            for tool_request in tool_requests:
                result = tool_service.execute_for_task(
                    ToolExecutionRequest(
                        agent_key=package.agent.key,
                        tool_key=tool_request.tool_key,
                        payload=tool_request.payload,
                        dry_run=tool_request.dry_run,
                    ),
                    task=task,
                )
                tool_call_payloads.append(tool_result_payload(result))

        if execute_llm:
            llm_client = self.llm_client
            if llm_client is None:
                llm_client = OpenAILLMClient(
                    model=(
                        package.agent.model_profile if package.agent.model_profile != "default" else None
                    )
                )
            if auto_tool_loop:
                loop_results = self._run_auto_tool_loop(
                    package=package,
                    task=task,
                    llm_client=llm_client,
                    initial_tool_results=tool_call_payloads,
                    max_iterations=max_tool_iterations,
                )
                tool_call_payloads.extend(loop_results["tool_calls"])
                tool_loop_trace = loop_results["trace"]
            if any(call.get("status") == "approval_required" for call in tool_call_payloads):
                task.status = "blocked"
                task.output_payload = {
                    "run_id": run_id,
                    "tool_call_count": len(tool_call_payloads),
                    "approval_required": True,
                }
                task.error_message = "Waiting for Chris to approve tool use."
                self._set_agent_current_action(
                    package.agent.key,
                    f"Waiting for approval: {request.task_instruction[:160]}",
                    commit=False,
                )
                self.session.commit()
                self.session.refresh(task)
                execution_note = "Agent run paused while waiting for Chris to approve tool use."
                status = "blocked"
            else:
                assembled_prompt = package.assembled_prompt
                if tool_call_payloads:
                    assembled_prompt = (
                        f"{assembled_prompt}\n\n## Tool Results\n"
                        "The following tool calls have already been executed by Maestro. "
                        "Use these results as evidence in your report. Do not emit tool-call XML, "
                        "JSON function-call requests, or instructions to call more tools; instead, "
                        "state any additional tool access needed as an open question or next step.\n\n"
                        f"{json.dumps(tool_call_payloads, indent=2)}"
                    )
                tool_call = ToolCall(
                    task_id=task.id,
                    agent_id=package.agent.id,
                    tool_name="llm.gateway",
                    input_payload={
                        "provider": getattr(llm_client, "provider", "configured"),
                        "model": package.agent.model_profile,
                        "prompt_chars": len(assembled_prompt),
                    },
                    status="running",
                    started_at=datetime.now(UTC),
                )
                self.session.add(tool_call)
                self.session.commit()
                self.session.refresh(tool_call)
                try:
                    tool_call.input_payload = {
                        **tool_call.input_payload,
                        "provider": getattr(llm_client, "provider", "unknown"),
                        "model": getattr(llm_client, "model", package.agent.model_profile),
                    }
                    output_text = llm_client.text_response(
                        instructions=(
                            "You are executing as a Maestro domain agent. Follow the provided "
                            "assembled prompt exactly, stay within your domain, respect the tool "
                            "manifest, and produce the requested structured output. Include a "
                            "top-level `conversation` field written as a concise plain-English "
                            "message Maestro can say directly to Chris. Tool results, when present, "
                            "have already been executed by Maestro; do not emit synthetic "
                            "tool-call markup or function-call requests in your answer."
                        ),
                        input_text=assembled_prompt,
                    )
                    tool_call.status = "complete"
                    tool_call.output_payload = {
                        "output_chars": len(output_text),
                        "output_preview": output_text[:500],
                    }
                    tool_call.completed_at = datetime.now(UTC)
                    report = Report(
                        task_id=task.id,
                        domain_id=domain.id,
                        agent_id=package.agent.id,
                        title=f"{package.agent.name} run",
                        report_type="agent_run_once",
                        summary=output_text[:500],
                        body_markdown=output_text,
                        structured_data={
                            "run_id": run_id,
                            "prompt_created_at": package.created_at,
                            "memory_included_count": package.memory_context.included_count,
                            "semantic_status": package.memory_context.semantic_status,
                            "tool_loop": tool_loop_trace,
                        },
                    )
                    self.session.add(report)
                    self.session.flush()
                    task.status = "completed"
                    task.output_payload = {
                        "run_id": run_id,
                        "report_id": str(report.id),
                        "output_preview": output_text[:500],
                    }
                    task.completed_at = datetime.now(UTC)
                    self._set_agent_current_action(
                        package.agent.key,
                        f"Completed: {request.task_instruction[:160]}",
                        commit=False,
                    )
                    execution_note = "Manual run completed through the LLM gateway."
                    status = "completed"
                    self.session.commit()
                    self.session.refresh(report)
                    self.session.refresh(tool_call)
                    self.session.refresh(task)
                    report_id = str(report.id)
                except Exception as exc:
                    error_message = str(exc)
                    tool_call.status = "failed"
                    tool_call.error_message = error_message
                    tool_call.completed_at = datetime.now(UTC)
                    task.status = "failed"
                    task.error_message = error_message
                    task.completed_at = datetime.now(UTC)
                    self._set_agent_current_action(
                        package.agent.key,
                        f"Failed: {request.task_instruction[:160]}",
                        commit=False,
                    )
                    self.session.commit()
                    self.session.refresh(tool_call)
                    self.session.refresh(task)
                    execution_note = "Manual run failed while calling the LLM gateway."
                    status = "failed"

                tool_call_payloads.append(
                    {
                        "id": str(tool_call.id),
                        "tool_name": tool_call.tool_name,
                        "status": tool_call.status,
                        "error_message": tool_call.error_message,
                        "input_payload": tool_call.input_payload,
                        "output_payload": tool_call.output_payload,
                    }
                )
        else:
            task.output_payload = {
                "run_id": run_id,
                "prepared_prompt_chars": len(package.assembled_prompt),
                "tool_call_count": len(tool_call_payloads),
            }
            self._set_agent_current_action(
                package.agent.key,
                f"Prepared: {request.task_instruction[:160]}",
                commit=False,
            )
            self.session.commit()
            self.session.refresh(task)
            execution_note = (
                "Manual run prepared without an LLM call. The scheduler is stubbed, but prompt, "
                "scoped memory, tool manifest, and artifact packaging are verified."
            )

        staged_path: str | None = None
        artifact_id: str | None = None
        if stage_interaction:
            staged = InteractionArtifactPackager(self.session).stage_package(
                InteractionArtifactPackager(self.session).build_package(
                    domain_key=package.agent.domain_key,
                    agent_key=package.agent.key,
                    maestro_tasking=request.task_instruction,
                    agent_output=output_text or execution_note,
                    tool_calls=tool_call_payloads
                    or [
                        {
                            "tool_name": "prompt_aggregation.run_once",
                            "status": status,
                        }
                    ],
                    generated_artifacts=[
                        {
                            "name": f"{package.agent.key}-{run_id}.md",
                            "type": "agent_run_report",
                            "report_id": report_id,
                        }
                    ]
                    if report_id
                    else [],
                    open_questions=[]
                    if status == "completed"
                    else ["Review failed or prepared run before retrying."],
                    next_steps=[
                        "Review the agent output.",
                        "Let the memory curator process this interaction artifact.",
                    ],
                    task_id=str(task.id),
                    provenance={
                        "run_id": run_id,
                        "execution_mode": "manual_run_once",
                        "status": status,
                    },
                )
            )
            staged_path = staged.path
            artifact_id = staged.artifact_id
        return AgentRunResult(
            run_id=run_id,
            status=status,
            agent=package.agent,
            prompt_package=package,
            scheduler={
                "status": "stubbed",
                "reason": (
                    "Master scheduler/resource-conflict policy is planned "
                    "but not implemented."
                ),
            },
            execution_note=execution_note,
            output_text=output_text,
            task_id=str(task.id),
            report_id=report_id,
            tool_calls=tool_call_payloads,
            tool_loop=tool_loop_trace,
            staged_artifact_path=staged_path,
            artifact_id=artifact_id,
            error_message=error_message,
        )

    def _run_auto_tool_loop(
        self,
        *,
        package: PromptPackage,
        task: Task,
        llm_client: LLMClient,
        initial_tool_results: list[dict[str, Any]],
        max_iterations: int,
    ) -> dict[str, Any]:
        tool_service = ToolExecutionService(self.session, adapters=self.tool_adapters)
        executed_calls: list[dict[str, Any]] = []
        prior_results = list(initial_tool_results)
        trace: dict[str, Any] = {"enabled": True, "iterations": []}
        bounded_iterations = max(1, min(max_iterations, 4))
        for index in range(bounded_iterations):
            planner_call = ToolCall(
                task_id=task.id,
                agent_id=package.agent.id,
                tool_name="llm.tool_planner",
                input_payload={
                    "iteration": index + 1,
                    "model": getattr(llm_client, "model", package.agent.model_profile),
                    "provider": getattr(llm_client, "provider", "configured"),
                },
                status="running",
                started_at=datetime.now(UTC),
            )
            self.session.add(planner_call)
            self.session.commit()
            self.session.refresh(planner_call)
            try:
                plan = llm_client.structured_response(
                    instructions=_TOOL_PLANNER_INSTRUCTIONS,
                    input_text=self._render_tool_planner_input(
                        package=package,
                        prior_results=prior_results,
                        iteration=index + 1,
                    ),
                    schema_name="agent_tool_plan",
                    schema=_TOOL_PLAN_SCHEMA,
                )
                requested = _normalize_tool_plan(plan)
                requested = _hydrate_pr_tool_payloads(
                    requested,
                    prior_results,
                    context_text=package.assembled_prompt,
                )
                planner_call.status = "complete"
                planner_call.output_payload = {
                    "plan_summary": plan.get("plan_summary"),
                    "tool_call_count": len(requested),
                    "requires_final_answer": bool(plan.get("requires_final_answer", True)),
                }
                planner_call.completed_at = datetime.now(UTC)
                self.session.commit()
                self.session.refresh(planner_call)
                planner_payload = {
                    "id": str(planner_call.id),
                    "tool_name": planner_call.tool_name,
                    "status": planner_call.status,
                    "error_message": planner_call.error_message,
                    "input_payload": planner_call.input_payload,
                    "output_payload": planner_call.output_payload,
                }
                executed_calls.append(planner_payload)
                iteration_trace = {
                    "iteration": index + 1,
                    "plan_summary": plan.get("plan_summary"),
                    "requested_tools": requested,
                    "executed": [],
                    "blocked": [],
                }
                if not requested:
                    trace["iterations"].append(iteration_trace)
                    break
                for requested_tool in requested:
                    tool_key = requested_tool["tool_key"]
                    if tool_key not in _AUTO_TOOL_SAFE_TOOL_KEYS:
                        policy = _TOOL_SAFETY_POLICIES.get(
                            tool_key,
                            {
                                "level": "approval_required",
                                "reason": "Tool is not approved for autonomous execution.",
                            },
                        )
                        blocked = {
                            "tool_key": tool_key,
                            "payload": requested_tool.get("payload") or {},
                            "safety_level": policy["level"],
                            "reason": policy["reason"],
                            "rationale": requested_tool.get("rationale"),
                        }
                        iteration_trace["blocked"].append(blocked)
                        proposed = tool_service.propose_for_task(
                            ToolExecutionRequest(
                                agent_key=package.agent.key,
                                tool_key=tool_key,
                                payload=requested_tool.get("payload") or {},
                                dry_run=False,
                            ),
                            task=task,
                            rationale=requested_tool.get("rationale"),
                            safety_level=str(policy["level"]),
                            reason=str(policy["reason"]),
                        )
                        blocked_payload = tool_result_payload(proposed)
                        executed_calls.append(blocked_payload)
                        prior_results.append(blocked_payload)
                        continue
                    result = tool_service.execute_for_task(
                        ToolExecutionRequest(
                            agent_key=package.agent.key,
                            tool_key=tool_key,
                            payload=requested_tool.get("payload") or {},
                            dry_run=False,
                        ),
                        task=task,
                    )
                    payload = tool_result_payload(result)
                    executed_calls.append(payload)
                    prior_results.append(payload)
                    iteration_trace["executed"].append(payload)
                trace["iterations"].append(iteration_trace)
                if iteration_trace["blocked"] or not iteration_trace["executed"]:
                    break
            except Exception as exc:
                planner_call.status = "failed"
                planner_call.error_message = str(exc)
                planner_call.completed_at = datetime.now(UTC)
                self.session.commit()
                self.session.refresh(planner_call)
                failed_payload = {
                    "id": str(planner_call.id),
                    "tool_name": planner_call.tool_name,
                    "status": planner_call.status,
                    "error_message": planner_call.error_message,
                    "input_payload": planner_call.input_payload,
                    "output_payload": planner_call.output_payload,
                }
                executed_calls.append(failed_payload)
                trace["iterations"].append(
                    {
                        "iteration": index + 1,
                        "plan_summary": "Tool planning failed.",
                        "requested_tools": [],
                        "executed": [],
                        "blocked": [],
                        "error_message": str(exc),
                    }
                )
                break
        trace["max_iterations"] = bounded_iterations
        return {"tool_calls": executed_calls, "trace": trace}

    def _render_tool_planner_input(
        self,
        *,
        package: PromptPackage,
        prior_results: list[dict[str, Any]],
        iteration: int,
    ) -> str:
        allowed_tools = [
            {
                "key": tool.key,
                "name": tool.name,
                "permission": tool.permission,
                "description": tool.description,
                "safety": _TOOL_SAFETY_POLICIES.get(
                    tool.key,
                    {
                        "level": "approval_required",
                        "auto_executable": False,
                        "reason": "Tool is not approved for autonomous execution.",
                    },
                ),
            }
            for tool in package.tool_manifest
        ]
        return "\n\n".join(
            [
                package.assembled_prompt,
                "## Tool Planning Context",
                (
                    f"Iteration: {iteration}\n"
                    "Choose zero or more allowed tools that are necessary before producing the "
                    "final report. Prefer the smallest useful read-only tool set. If the task can "
                    "be answered with existing context, return no tool calls. If prior tool "
                    "results already include a completed approved write/action tool, do not "
                    "request follow-on write/action tools in this same turn; produce the final "
                    "report and let Maestro propose additional external actions separately."
                ),
                "## Allowed Tool Manifest\n" + json.dumps(allowed_tools, indent=2),
                "## Prior Tool Results\n" + json.dumps(prior_results, indent=2),
            ]
        )

    def _set_agent_current_action(
        self,
        agent_key: str,
        current_action: str | None,
        *,
        commit: bool = True,
    ) -> None:
        agent = AgentRepository(self.session).get_by_key(agent_key)
        if agent is None:
            return
        capabilities = dict(agent.capabilities or {})
        capabilities["current_action"] = current_action
        agent.capabilities = capabilities
        if commit:
            self.session.commit()


class InteractionArtifactPackager:
    def __init__(self, session: Session):
        self.session = session

    def build_package(
        self,
        *,
        domain_key: str,
        agent_key: str | None = None,
        user_input: str | None = None,
        maestro_tasking: str | None = None,
        agent_output: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        generated_artifacts: list[dict[str, Any]] | None = None,
        open_questions: list[str] | None = None,
        next_steps: list[str] | None = None,
        task_id: str | None = None,
        conversation_id: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> InteractionArtifactPackage:
        seed_default_domains(self.session)
        DomainRepository(self.session).get_by_key(domain_key) or self._raise_unknown_domain(
            domain_key
        )
        if agent_key is not None:
            AgentRegistryService(self.session).get_spec(agent_key)
        return InteractionArtifactPackage(
            schema_version="maestro.interaction_artifact.v1",
            package_id=str(uuid.uuid4()),
            created_at=datetime.now(UTC).isoformat(),
            domain_key=domain_key,
            agent_key=agent_key,
            task_id=task_id,
            conversation_id=conversation_id,
            user_input=user_input,
            maestro_tasking=maestro_tasking,
            agent_output=agent_output,
            tool_calls=tool_calls or [],
            generated_artifacts=generated_artifacts or [],
            open_questions=open_questions or [],
            next_steps=next_steps or [],
            provenance={
                "packaged_by": "interaction_artifact_packager",
                **(provenance or {}),
            },
        )

    def stage_package(self, package: InteractionArtifactPackage) -> StagedInteractionArtifact:
        root = Path(get_settings().memory_dropbox_root)
        inbox = root / package.domain_key / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        filename = f"{_slug(package.agent_key or 'maestro-session')}-{package.package_id}.json"
        path = inbox / filename
        path.write_text(json.dumps(asdict(package), indent=2, sort_keys=True), encoding="utf-8")

        artifact = Artifact(
            artifact_type="interaction_package",
            name=filename,
            uri=str(path),
            mime_type="application/json",
            metadata_={
                "schema_version": package.schema_version,
                "package_id": package.package_id,
                "domain_key": package.domain_key,
                "agent_key": package.agent_key,
                "staged_for_curation": True,
            },
        )
        self.session.add(artifact)
        self.session.commit()
        self.session.refresh(artifact)
        return StagedInteractionArtifact(
            package=package,
            path=str(path),
            artifact_id=str(artifact.id),
        )

    def _raise_unknown_domain(self, domain_key: str):
        raise AgentRuntimeError(f"Unknown domain: {domain_key}")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "interaction"


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in config.items():
        if _is_secret_key(key):
            redacted[key] = "********" if value else ""
        else:
            redacted[key] = value
    return redacted


def _merge_secret_config(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if _is_secret_key(key) and isinstance(value, str) and set(value) == {"*"}:
            continue
        merged[key] = value
    return merged


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("secret", "token", "api_key", "apikey", "password"))


def _normalize_tool_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in plan.get("tool_calls") or []:
        if not isinstance(item, dict):
            continue
        tool_key = str(item.get("tool_key") or "").strip()
        if not tool_key:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        payload_json = item.get("payload_json")
        if not payload and isinstance(payload_json, str) and payload_json.strip():
            try:
                parsed_payload = json.loads(payload_json)
                payload = parsed_payload if isinstance(parsed_payload, dict) else {}
            except json.JSONDecodeError:
                payload = {}
        normalized.append(
            {
                "tool_key": tool_key,
                "payload": payload,
                "rationale": str(item.get("rationale") or "").strip(),
            }
        )
    return normalized[:5]


def _hydrate_pr_tool_payloads(
    requested_tools: list[dict[str, Any]],
    prior_results: list[dict[str, Any]],
    *,
    context_text: str = "",
) -> list[dict[str, Any]]:
    pr_number = _latest_pr_number_from_tool_results(prior_results)
    if pr_number is None:
        pr_number = _latest_pr_number_from_text(context_text)
    if pr_number is None:
        return requested_tools
    hydrated: list[dict[str, Any]] = []
    for requested in requested_tools:
        payload = requested.get("payload") if isinstance(requested.get("payload"), dict) else {}
        if (
            str(requested.get("tool_key") or "").startswith("github.pr.")
            and str(requested.get("tool_key") or "") != "github.pr.search"
            and "pr_number" not in payload
            and "number" not in payload
        ):
            payload = {**payload, "pr_number": pr_number}
            requested = {**requested, "payload": payload}
        hydrated.append(requested)
    return hydrated


def _latest_pr_number_from_tool_results(prior_results: list[dict[str, Any]]) -> int | None:
    for result in reversed(prior_results):
        if not isinstance(result, dict):
            continue
        output = result.get("output_payload")
        if not isinstance(output, dict):
            output = result.get("output")
        if not isinstance(output, dict):
            continue
        number = output.get("pr_number") or output.get("number")
        if number is not None:
            try:
                return int(number)
            except (TypeError, ValueError):
                pass
        pr = output.get("pr")
        if isinstance(pr, dict):
            nested_number = pr.get("number") or pr.get("pr_number")
            if nested_number is not None:
                try:
                    return int(nested_number)
                except (TypeError, ValueError):
                    pass
        prs = output.get("prs")
        if isinstance(prs, list) and prs:
            latest = prs[0]
            if isinstance(latest, dict):
                latest_number = latest.get("number") or latest.get("pr_number")
                if latest_number is not None:
                    try:
                        return int(latest_number)
                    except (TypeError, ValueError):
                        pass
    return None


def _latest_pr_number_from_text(text: str) -> int | None:
    matches = re.findall(r"\bPR(?:\s+number|[#\s])\s*:?\s*#?(\d+)\b", text, flags=re.IGNORECASE)
    if not matches:
        matches = re.findall(r"/pull/(\d+)\b", text)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except (TypeError, ValueError):
        return None


_GLOBAL_CONTEXT_SETTING_KEY = "global_maestro_context"


_GLOBAL_MAESTRO_CONTEXT = (
    "Maestro is Chris Aliperti's cross-domain chief-of-staff system. Chris is the user "
    "Maestro is directly speaking with; interpret first-person references such as I, me, "
    "my, and we as Chris unless the local context clearly says otherwise. Maestro coordinates "
    "domain-scoped agents, preserves provenance, retrieves relevant memory through the "
    "Memory service, stages raw artifacts for curation, and keeps Chris in control of "
    "high-impact actions."
)

_DOMAIN_CONTEXTS = {
    "personal": (
        "Personal domain for Chris's life operations, preferences, calendar, tasks, "
        "and household context."
    ),
    "maestro-development": (
        "Maestro Development domain for designing, building, testing, and improving "
        "Maestro itself."
    ),
    "praxis": (
        "Praxis domain for Tactical Innovation, partner engagement, training, transition "
        "planning, and program development."
    ),
    "ophi": (
        "Ophi domain for product strategy, research loops, market learning, and "
        "operational experiments."
    ),
    "usma": (
        "USMA domain for teaching, cadet support, academic prep, and institutional obligations."
    ),
    "personal-irad-projects": (
        "Personal IRAD domain for independent research and development projects."
    ),
    "l3": "L3 domain for professional obligations and L3-related work context.",
}

_DEFAULT_OUTPUT_CONTRACT = {
    "format": "structured_report",
    "required_sections": [
        "conversation",
        "summary",
        "findings",
        "open_questions",
        "next_steps",
        "artifact_refs",
    ],
    "section_notes": {
        "conversation": (
            "Concise plain-English message Maestro can show directly in chat to Chris. "
            "Do not use JSON, markdown tables, or provenance lists in this field."
        )
    },
    "provenance_required": True,
}

_TOOL_SAFETY_POLICIES = {
    "github.repo.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only repository metadata.",
    },
    "github.repo.list": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only repository listing.",
    },
    "github.file.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only repository file retrieval.",
    },
    "github.file.search": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only repository file search.",
    },
    "github.issue.search": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only issue search.",
    },
    "github.issue.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only issue inspection.",
    },
    "github.pr.search": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only pull request search.",
    },
    "github.pr.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only pull request inspection.",
    },
    "github.pr.diff": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only pull request diff inspection.",
    },
    "github.pr.checks": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only pull request check inspection.",
    },
    "github.pr.merge": {
        "level": "external_write",
        "auto_executable": False,
        "reason": "Merges a GitHub pull request and requires Chris approval.",
    },
    "github.issue.create": {
        "level": "external_write",
        "auto_executable": False,
        "reason": "Creates an external GitHub issue and requires Chris approval.",
    },
    "github.repo.create": {
        "level": "external_write",
        "auto_executable": False,
        "reason": "Creates an external GitHub repository and requires Chris approval.",
    },
    "github.issue.comment": {
        "level": "external_write",
        "auto_executable": False,
        "reason": "Posts an external GitHub comment and requires Chris approval.",
    },
    "github.issue.update": {
        "level": "external_write",
        "auto_executable": False,
        "reason": "Updates external GitHub issue metadata and requires Chris approval.",
    },
    "codex.task.run": {
        "level": "branch_sandbox_code_execution",
        "auto_executable": True,
        "reason": (
            "Runs a local Codex coding task on an isolated feature branch and opens a PR. "
            "Merge/deploy still require Chris approval."
        ),
    },
    "local.app.reload": {
        "level": "local_app_update",
        "auto_executable": False,
        "reason": "Updates/reloads a local application checkout and requires Chris approval.",
    },
}

_AUTO_TOOL_SAFE_TOOL_KEYS = {
    key for key, policy in _TOOL_SAFETY_POLICIES.items() if policy["auto_executable"]
}

_TOOL_PLANNER_INSTRUCTIONS = (
    "You are an execution planner for a Maestro domain agent. Return only JSON matching the "
    "schema. Choose only tools from the allowed manifest. Prefer read-only tools and the smallest "
    "number of calls needed. Read-only tools marked safe can run automatically. `codex.task.run` "
    "can run automatically because it works on an isolated feature branch and returns a PR for "
    "Chris review; do not ask for approval before requesting it. Other write/action tools must "
    "only be requested when explicitly needed; they will be proposed for Chris approval instead "
    "of executed automatically. Return tool payloads as JSON strings in "
    "`payload_json`. Do not include repo placeholders such as repo:CURRENT or "
    "repo:AUTHORIZED_REPOSITORY in search queries; the tool connection already supplies the repo. "
    "For a request like 'check out the latest PR', use GitHub PR search/list tools first, then "
    "details/checks/diff if useful. If prior tool results include a PR number and the current "
    "request refers to 'the PR', 'that PR', or 'it', pass that number as `pr_number`."
)

_TOOL_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "plan_summary": {
            "type": "string",
            "description": "Short explanation of the tool plan or why no tools are needed.",
        },
        "requires_final_answer": {
            "type": "boolean",
            "description": "Whether the agent should still produce a final report after tools.",
        },
        "tool_calls": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tool_key": {"type": "string"},
                    "payload_json": {
                        "type": "string",
                        "description": "JSON object string to pass as the tool payload.",
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["tool_key", "payload_json", "rationale"],
            },
        },
    },
    "required": ["plan_summary", "requires_final_answer", "tool_calls"],
}

_TOOL_DESCRIPTIONS = {
    "memory.context_bundle": {
        "name": "Memory Context Bundle",
        "description": "Retrieve scoped, prompt-ready memory through the Memory Retrieval service.",
    },
    "artifact.stage_interaction": {
        "name": "Stage Interaction Artifact",
        "description": "Package interaction outputs for curator processing.",
    },
    "llm.gateway": {
        "name": "LLM Gateway",
        "description": "Call the configured LLM provider through Maestro's shared gateway.",
    },
    "github": {
        "name": "GitHub",
        "description": "Shared GitHub repository credentials/config inherited by GitHub tools.",
    },
    "github.read": {
        "name": "GitHub Read",
        "description": (
            "Read repository issues, pull requests, files, and CI context when authorized."
        ),
    },
    "github.repo.get": {
        "name": "GitHub Repo Metadata",
        "description": "Read repository metadata through the authorized domain GitHub connection.",
    },
    "github.repo.list": {
        "name": "GitHub Repo List",
        "description": "List repositories for an authorized owner or organization.",
    },
    "github.repo.create": {
        "name": "GitHub Repo Create",
        "description": "Create a new GitHub repository after approval.",
    },
    "github.file.get": {
        "name": "GitHub File Read",
        "description": "Read a specific repository file or directory from an authorized repo/ref.",
    },
    "github.file.search": {
        "name": "GitHub File Search",
        "description": "Search for files within the authorized repository.",
    },
    "github.issue.search": {
        "name": "GitHub Issue Search",
        "description": "Search GitHub issues in the authorized repository.",
    },
    "github.issue.get": {
        "name": "GitHub Issue Details",
        "description": "Read a specific GitHub issue from the authorized repository.",
    },
    "github.issue.create": {
        "name": "GitHub Issue Create",
        "description": (
            "Create a GitHub issue in the authorized repository after approval. Configured "
            "preferred labels are applied when present, missing optional labels are skipped, "
            "and configured required labels block creation if absent."
        ),
    },
    "github.issue.comment": {
        "name": "GitHub Issue Comment",
        "description": "Comment on a GitHub issue in the authorized repository.",
    },
    "github.issue.update": {
        "name": "GitHub Issue Update",
        "description": "Update title, body, labels, assignees, or milestone for a GitHub issue.",
    },
    "github.pr.search": {
        "name": "GitHub PR Search",
        "description": "Search pull requests in the authorized repository.",
    },
    "github.pr.get": {
        "name": "GitHub PR Details",
        "description": "Read pull request metadata, reviews, files, comments, and check rollups.",
    },
    "github.pr.diff": {
        "name": "GitHub PR Diff",
        "description": "Read pull request diff or changed filenames.",
    },
    "github.pr.checks": {
        "name": "GitHub PR Checks",
        "description": "Read CI/check status for a pull request with normalized status fields.",
    },
    "github.pr.merge": {
        "name": "GitHub PR Merge",
        "description": "Merge an approved GitHub pull request after Chris approval.",
    },
    "codex": {
        "name": "Codex",
        "description": "Shared local Codex CLI configuration inherited by Codex tools.",
    },
    "codex.task.run": {
        "name": "Codex Task Run",
        "description": (
            "Run a local Codex coding task on an isolated feature branch, then return "
            "session output, changed files, final summary, and PR review metadata."
        ),
    },
    "local.app.reload": {
        "name": "Local App Reload",
        "description": (
            "Update a configured local application checkout and run approved reload commands."
        ),
    },
}


def _provider_connection_key(tool_key: str) -> str:
    if tool_key.startswith("github."):
        return "github"
    if tool_key.startswith("codex."):
        return "codex"
    return tool_key


def _inherited_connection_tool_keys(tool_key: str) -> list[str]:
    if tool_key == "github":
        return [key for key in _TOOL_DESCRIPTIONS if key.startswith("github.")]
    if tool_key == "codex":
        return [key for key in _TOOL_DESCRIPTIONS if key.startswith("codex.")]
    return []

_SEED_AGENTS = [
    {
        "domain_key": "praxis",
        "key": "praxis-planning-agent",
        "name": "Praxis Planning Agent",
        "agent_type": "domain_agent",
        "role_summary": (
            "Prepares Praxis planning context, partner follow-ups, and tactical innovation "
            "recommendations."
        ),
        "role_prompt": (
            "You are the Praxis Planning Agent. Work only inside the Praxis domain. "
            "Use retrieved memory to ground recommendations in Praxis strategy, partner context, "
            "training design, and transition priorities. Produce practical next steps and cite "
            "memory or artifact references when available."
        ),
        "memory_profile": "agent_prompt",
        "model_profile": "default",
        "tool_permissions": {
            "memory.context_bundle": {
                "permission": "read",
                "description": "Retrieve Praxis-scoped memory bundles.",
            },
            "artifact.stage_interaction": {
                "permission": "write",
                "description": "Stage Praxis interaction packages.",
            },
            "llm.gateway": {
                "permission": "use",
                "description": "Use Maestro's shared LLM gateway.",
            },
        },
    },
    {
        "domain_key": "maestro-development",
        "key": "maestro-introspection-agent",
        "name": "Maestro Introspection Agent",
        "agent_type": "domain_agent",
        "role_summary": (
            "Reviews Maestro behavior, identifies system gaps, and proposes improvements."
        ),
        "role_prompt": (
            "You are the Maestro Introspection Agent. Work only inside the Maestro Development "
            "domain. Evaluate what is working, what is brittle, and what should be improved next. "
            "Prefer concrete implementation proposals with clear risks and validation steps."
        ),
        "memory_profile": "agent_prompt",
        "model_profile": "default",
        "tool_permissions": {
            "memory.context_bundle": {
                "permission": "read",
                "description": "Retrieve Maestro-development memory bundles.",
            },
            "artifact.stage_interaction": {
                "permission": "write",
                "description": "Stage introspection reports for curation.",
            },
            "llm.gateway": {
                "permission": "use",
                "description": "Use Maestro's shared LLM gateway.",
            },
            "github.read": {
                "permission": "read",
                "description": "Inspect Maestro GitHub context when authorized.",
            },
            "github.repo.get": {
                "permission": "read",
                "description": "Inspect Maestro repository metadata.",
            },
            "github.repo.list": {
                "permission": "read",
                "description": "List authorized GitHub repositories.",
            },
            "github.repo.create": {
                "permission": "use",
                "description": "Create GitHub repositories when approved.",
            },
            "github.file.get": {
                "permission": "read",
                "description": "Read files from authorized GitHub repositories.",
            },
            "github.file.search": {
                "permission": "read",
                "description": "Search files in authorized GitHub repositories.",
            },
            "github.issue.search": {
                "permission": "read",
                "description": "Search Maestro GitHub issues.",
            },
            "github.issue.get": {
                "permission": "read",
                "description": "Read Maestro GitHub issue details.",
            },
            "github.issue.create": {
                "permission": "use",
                "description": "Create Maestro GitHub issues when approved.",
            },
            "github.issue.comment": {
                "permission": "use",
                "description": "Comment on Maestro GitHub issues when approved.",
            },
            "github.issue.update": {
                "permission": "use",
                "description": "Update Maestro GitHub issue metadata when approved.",
            },
            "github.pr.search": {
                "permission": "read",
                "description": "Search Maestro GitHub pull requests.",
            },
            "github.pr.get": {
                "permission": "read",
                "description": "Read Maestro GitHub pull request details.",
            },
            "github.pr.diff": {
                "permission": "read",
                "description": "Read Maestro GitHub pull request diffs.",
            },
            "github.pr.checks": {
                "permission": "read",
                "description": "Read Maestro GitHub pull request check status.",
            },
            "github.pr.merge": {
                "permission": "use",
                "description": "Merge Maestro GitHub pull requests when approved.",
            },
            "codex.task.run": {
                "permission": "use",
                "description": "Run local Codex coding tasks in isolated branch/PR worktrees.",
            },
            "local.app.reload": {
                "permission": "use",
                "description": "Reload configured local Maestro app surfaces when approved.",
            },
        },
    },
    {
        "domain_key": "maestro-development",
        "key": "maestro-coding-agent",
        "name": "Maestro Coding Agent",
        "agent_type": "domain_agent",
        "role_summary": (
            "Executes scoped Maestro coding tasks through the local Codex tool and reports "
            "implementation results back to Maestro."
        ),
        "role_prompt": (
            "You are the Maestro Coding Agent. Work only inside the Maestro Development domain. "
            "Use the local Codex task tool for implementation work after Chris approves the tool "
            "plan. Keep coding tasks scoped, preserve unrelated work, run the requested validation "
            "when practical, and return a concise report with changed files, tests, and follow-up "
            "risks."
        ),
        "memory_profile": "agent_prompt",
        "model_profile": "default",
        "tool_permissions": {
            "memory.context_bundle": {
                "permission": "read",
                "description": "Retrieve Maestro-development memory bundles.",
            },
            "artifact.stage_interaction": {
                "permission": "write",
                "description": "Stage coding task outputs for curation.",
            },
            "llm.gateway": {
                "permission": "use",
                "description": "Use Maestro's shared LLM gateway.",
            },
            "github.issue.get": {
                "permission": "read",
                "description": "Read Maestro GitHub issue details before implementation.",
            },
            "github.file.get": {
                "permission": "read",
                "description": "Read repository files when preparing implementation context.",
            },
            "codex.task.run": {
                "permission": "use",
                "description": "Run local Codex coding tasks in isolated branch/PR worktrees.",
            },
            "github.pr.merge": {
                "permission": "use",
                "description": "Merge Maestro GitHub pull requests when approved.",
            },
            "local.app.reload": {
                "permission": "use",
                "description": "Reload configured local Maestro app surfaces when approved.",
            },
        },
    },
]
