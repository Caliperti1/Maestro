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
from app.db.models import Agent, Artifact, Domain, RuntimeSetting, ToolConnection
from app.db.repositories import AgentRepository, DomainRepository
from app.db.seed import seed_default_domains
from app.memory.retrieval import (
    MemoryContextBundle,
    MemoryContextBundleRequest,
    MemoryRetrievalService,
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
    staged_artifact_path: str | None = None
    artifact_id: str | None = None


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
        agents = self.session.scalars(select(Agent).order_by(Agent.key)).all()
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
        if is_active is not None:
            agent.is_active = is_active
        agent.capabilities = capabilities
        self.session.commit()
        self.session.refresh(agent)
        return self.get_spec(agent.key)

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
    def __init__(self, session: Session):
        self.session = session
        self.registry = AgentRegistryService(session)

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
    ) -> AgentRunResult:
        package = self.build_prompt_package(request)
        run_id = str(uuid.uuid4())
        execution_note = (
            "Manual run prepared. The scheduler and autonomous LLM execution are intentionally "
            "stubbed; this verifies prompt, scoped memory, tool manifest, and artifact packaging."
        )
        staged_path: str | None = None
        artifact_id: str | None = None
        if stage_interaction:
            staged = InteractionArtifactPackager(self.session).stage_package(
                InteractionArtifactPackager(self.session).build_package(
                    domain_key=package.agent.domain_key,
                    agent_key=package.agent.key,
                    maestro_tasking=request.task_instruction,
                    agent_output=execution_note,
                    tool_calls=[
                        {
                            "tool_name": "prompt_aggregation.run_once_stub",
                            "status": "prepared",
                        }
                    ],
                    generated_artifacts=[],
                    open_questions=[
                        "Connect this run envelope to the reusable LLM gateway.",
                        "Route future scheduled runs through the master scheduler service.",
                    ],
                    next_steps=[
                        "Review assembled prompt package.",
                        "Use this contract for the first real agent execution loop.",
                    ],
                    provenance={
                        "run_id": run_id,
                        "execution_mode": "manual_run_once_stub",
                    },
                )
            )
            staged_path = staged.path
            artifact_id = staged.artifact_id
        return AgentRunResult(
            run_id=run_id,
            status="prepared",
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
            staged_artifact_path=staged_path,
            artifact_id=artifact_id,
        )


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


_GLOBAL_CONTEXT_SETTING_KEY = "global_maestro_context"


_GLOBAL_MAESTRO_CONTEXT = (
    "Maestro is Chris Aliperti's cross-domain chief-of-staff system. It coordinates "
    "domain-scoped agents, preserves provenance, retrieves relevant memory through the "
    "Memory service, stages raw artifacts for curation, and keeps the human in control "
    "of high-impact actions."
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
    "required_sections": ["summary", "findings", "open_questions", "next_steps", "artifact_refs"],
    "provenance_required": True,
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
    "github.read": {
        "name": "GitHub Read",
        "description": (
            "Read repository issues, pull requests, files, and CI context when authorized."
        ),
    },
}

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
        },
    },
]
