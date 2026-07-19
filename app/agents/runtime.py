"""Agent registry, prompt aggregation, run-once execution, and artifact packaging.

Agents are configured per domain, receive prompts through the aggregation service, and may request
tools through the shared tool runtime. This module also packages agent interactions into artifacts
that can enter the memory pipeline, which keeps execution traces separate from durable memory
writes.
"""

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
    Skill,
    Task,
    ToolCall,
    ToolConnection,
)
from app.db.repositories import AgentRepository, DomainRepository, SkillRepository
from app.db.seed import seed_default_domains
from app.llm.client import LLMClient, OllamaLLMClient, OpenAILLMClient
from app.memory.retrieval import (
    MemoryContextBundle,
    MemoryContextBundleRequest,
    MemoryRetrievalService,
)
from app.prompts import load_prompt
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
class SkillManifestItem:
    key: str
    name: str
    description: str
    category: str
    instruction: str
    domain_key: str | None = None


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
    allowed_skills: list[SkillManifestItem]
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
class SkillRegistryItem:
    id: uuid.UUID
    key: str
    name: str
    description: str | None
    category: str
    instruction: str
    domain_key: str | None
    is_active: bool
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
    required_skills: list[str] | None = None
    model_profile: str | None = None


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
    skill_manifest: list[SkillManifestItem]
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
        self._ensure_seed_skills()
        created_or_existing: list[Agent] = []
        repo = AgentRepository(self.session)
        domain_repo = DomainRepository(self.session)
        for seed in _SEED_AGENTS:
            existing = repo.get_by_key(seed["key"])
            if existing is not None:
                capabilities = dict(existing.capabilities or {})
                changed_existing = False
                if not capabilities.get("model_profile") and seed.get("model_profile"):
                    capabilities["model_profile"] = seed["model_profile"]
                    existing.capabilities = capabilities
                    changed_existing = True
                if not capabilities.get("manual_tool_permissions"):
                    merged_permissions = dict(existing.tool_permissions or {})
                    for tool_key, permission in seed["tool_permissions"].items():
                        if tool_key not in merged_permissions:
                            merged_permissions[tool_key] = permission
                            changed_existing = True
                    if changed_existing:
                        existing.tool_permissions = merged_permissions
                if not capabilities.get("manual_skill_permissions"):
                    merged_skill_permissions = dict(existing.skill_permissions or {})
                    for skill_key, permission in (seed.get("skill_permissions") or {}).items():
                        if skill_key not in merged_skill_permissions:
                            merged_skill_permissions[skill_key] = permission
                            changed_existing = True
                    if changed_existing:
                        existing.skill_permissions = merged_skill_permissions
                if changed_existing:
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
                    skill_permissions=seed.get("skill_permissions") or {},
                )
            )
        self._ensure_internal_default_tools()
        self._ensure_research_agents_can_search_web()
        return created_or_existing

    def _ensure_internal_default_tools(self) -> None:
        changed = False
        for agent in self.session.scalars(select(Agent).where(Agent.is_active.is_(True))).all():
            permissions = _with_internal_default_tool_permissions(agent.tool_permissions or {})
            if permissions == (agent.tool_permissions or {}):
                continue
            agent.tool_permissions = permissions
            changed = True
        if changed:
            self.session.commit()

    def _ensure_seed_skills(self) -> None:
        domain_repo = DomainRepository(self.session)
        skill_repo = SkillRepository(self.session)
        changed = False
        now = datetime.now(UTC)
        for seed in _SEED_SKILLS:
            domain = domain_repo.get_by_key(seed["domain_key"]) if seed.get("domain_key") else None
            existing = skill_repo.get_by_key(seed["key"])
            if existing is None:
                self.session.add(
                    Skill(
                        key=seed["key"],
                        name=seed["name"],
                        description=seed["description"],
                        category=seed["category"],
                        instruction=seed["instruction"],
                        domain_id=domain.id if domain else None,
                        metadata_=seed.get("metadata") or {},
                        is_active=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                changed = True
                continue
            if (existing.metadata_ or {}).get("manual_edit"):
                continue
            updates = {
                "name": seed["name"],
                "description": seed["description"],
                "category": seed["category"],
                "instruction": seed["instruction"],
                "domain_id": domain.id if domain else None,
                "metadata_": seed.get("metadata") or {},
                "is_active": True,
            }
            for key, value in updates.items():
                if getattr(existing, key) != value:
                    setattr(existing, key, value)
                    changed = True
            if changed:
                existing.updated_at = now
        if changed:
            self.session.commit()

    def _ensure_research_agents_can_search_web(self) -> None:
        changed = False
        for agent in self.session.scalars(select(Agent).where(Agent.is_active.is_(True))).all():
            permissions = dict(agent.tool_permissions or {})
            if "web.search" in permissions:
                continue
            capabilities = agent.capabilities or {}
            role_text = " ".join(
                str(value or "")
                for value in (
                    agent.key,
                    agent.name,
                    agent.description,
                    capabilities.get("role_summary") if isinstance(capabilities, dict) else "",
                    capabilities.get("role_prompt") if isinstance(capabilities, dict) else "",
                )
            ).lower()
            if "sota" not in role_text and "research" not in role_text:
                continue
            permissions["web.search"] = {
                "permission": "read",
                "description": "Search the web for current SOTA/research context with citations.",
            }
            agent.tool_permissions = permissions
            changed = True
        if changed:
            self.session.commit()

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
        skill_permissions: dict[str, Any] | None = None,
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
            tool_permissions=_with_internal_default_tool_permissions(tool_permissions or {}),
            skill_permissions=skill_permissions or {},
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
        skill_permissions: dict[str, Any] | None = None,
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
            agent.tool_permissions = _with_internal_default_tool_permissions(tool_permissions)
            capabilities["manual_tool_permissions"] = True
        if skill_permissions is not None:
            agent.skill_permissions = skill_permissions
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

    def list_skills(self) -> list[SkillRegistryItem]:
        self.ensure_seed_agents()
        domains_by_id = {
            domain.id: domain for domain in DomainRepository(self.session).list_active()
        }
        authorized_agents: dict[str, list[dict[str, str]]] = {}
        for agent in self.session.scalars(select(Agent).where(Agent.is_active.is_(True))).all():
            domain = domains_by_id.get(agent.domain_id)
            if domain is None:
                continue
            for skill_key, value in (agent.skill_permissions or {}).items():
                permission = value if isinstance(value, str) else value.get("permission", "use")
                authorized_agents.setdefault(skill_key, []).append(
                    {
                        "agent_key": agent.key,
                        "agent_name": agent.name,
                        "domain_key": domain.key,
                        "permission": str(permission),
                    }
                )
        return [
            SkillRegistryItem(
                id=skill.id,
                key=skill.key,
                name=skill.name,
                description=skill.description,
                category=skill.category,
                instruction=skill.instruction,
                domain_key=domains_by_id.get(skill.domain_id).key
                if skill.domain_id and domains_by_id.get(skill.domain_id)
                else None,
                is_active=skill.is_active,
                authorized_agents=sorted(
                    authorized_agents.get(skill.key, []),
                    key=lambda item: (item["domain_key"], item["agent_key"]),
                ),
            )
            for skill in SkillRepository(self.session).list_active()
        ]

    def upsert_skill(
        self,
        *,
        key: str,
        name: str,
        instruction: str,
        description: str | None = None,
        category: str = "general",
        domain_key: str | None = None,
        is_active: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> SkillRegistryItem:
        seed_default_domains(self.session)
        cleaned_key = _slug(key)
        if not cleaned_key:
            raise AgentRuntimeError("Skill key cannot be blank.")
        cleaned_name = name.strip()
        cleaned_instruction = instruction.strip()
        if not cleaned_name:
            raise AgentRuntimeError("Skill name cannot be blank.")
        if not cleaned_instruction:
            raise AgentRuntimeError("Skill instruction cannot be blank.")
        domain = DomainRepository(self.session).get_by_key(domain_key) if domain_key else None
        if domain_key and domain is None:
            raise AgentRuntimeError(f"Unknown domain: {domain_key}")
        skill = SkillRepository(self.session).get_by_key(cleaned_key)
        if skill is None:
            skill = Skill(
                key=cleaned_key,
                name=cleaned_name,
                instruction=cleaned_instruction,
                description=description,
                category=category.strip() or "general",
                domain_id=domain.id if domain else None,
                metadata_=metadata or {},
                is_active=is_active,
            )
            self.session.add(skill)
        else:
            skill.name = cleaned_name
            skill.instruction = cleaned_instruction
            skill.description = description
            skill.category = category.strip() or "general"
            skill.domain_id = domain.id if domain else None
            skill.metadata_ = metadata or {}
            skill.is_active = is_active
        self.session.commit()
        self.session.refresh(skill)
        return next(item for item in self.list_skills() if item.key == skill.key)

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
            allowed_skills=self._skill_manifest(agent, domain=domain),
            is_active=agent.is_active,
            current_action=capabilities.get("current_action"),
            scheduled_actions=list(capabilities.get("scheduled_actions") or []),
        )

    def _skill_manifest(self, agent: Agent, *, domain: Domain) -> list[SkillManifestItem]:
        permissions = agent.skill_permissions or {}
        if not permissions:
            return []
        skills = {
            skill.key: skill
            for skill in self.session.scalars(
                select(Skill).where(Skill.is_active.is_(True))
            ).all()
        }
        domains_by_id = {
            found.id: found for found in DomainRepository(self.session).list_active()
        }
        manifest: list[SkillManifestItem] = []
        for key in sorted(permissions):
            skill = skills.get(key)
            if skill is None:
                continue
            skill_domain = domains_by_id.get(skill.domain_id) if skill.domain_id else None
            if skill_domain is not None and skill_domain.id != domain.id:
                continue
            manifest.append(
                SkillManifestItem(
                    key=skill.key,
                    name=skill.name,
                    description=skill.description or "",
                    category=skill.category,
                    instruction=skill.instruction,
                    domain_key=skill_domain.key if skill_domain else None,
                )
            )
        return manifest

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

        query_text = _memory_query_for_prompt_package(
            request=request,
            agent=spec,
            domain_name=domain.name,
            domain_key=domain.key,
            domain_context=domain.description or _DOMAIN_CONTEXTS.get(spec.domain_key, ""),
        )
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
        prompt_task_instruction = _compact_task_instruction_for_prompt(request.task_instruction)
        skill_manifest = _scoped_skill_manifest(spec.allowed_skills, request.required_skills)
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
            skill_manifest=skill_manifest,
            output_contract=output_contract,
            assembled_prompt=self._render_prompt(
                global_context=global_context,
                domain_context=domain.description or _DOMAIN_CONTEXTS.get(spec.domain_key, ""),
                role_prompt=spec.role_prompt,
                task_instruction=prompt_task_instruction,
                user_context=request.user_context,
                memory_text=memory_context.rendered_text,
                tools=spec.allowed_tools,
                skills=skill_manifest,
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
        skills: list[SkillManifestItem],
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
        if skills:
            skills_text = "\n\n".join(
                f"### {skill.name} (`{skill.key}`)\n{skill.instruction}"
                for skill in skills
            )
            sections.append(("Assigned Skills", skills_text))
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
                "memory_query_text": package.memory_context.request.query_text,
                "execute_llm": execute_llm,
                "stage_interaction": stage_interaction,
                "auto_tool_loop": auto_tool_loop,
                "max_tool_iterations": max_tool_iterations,
                "prompt_context": {
                    "task_instruction": request.task_instruction,
                    "user_context": request.user_context,
                    "assembled_prompt_chars": len(package.assembled_prompt),
                    "raw_task_instruction_chars": len(request.task_instruction),
                    "prompt_task_instruction_chars": len(
                        _compact_task_instruction_for_prompt(request.task_instruction)
                    ),
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
                llm_client = _llm_client_for_model_profile(
                    request.model_profile or package.agent.model_profile
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
                user_display_name = get_settings().user_display_name
                task.status = "blocked"
                task.output_payload = {
                    "run_id": run_id,
                    "tool_call_count": len(tool_call_payloads),
                    "approval_required": True,
                }
                task.error_message = f"Waiting for {user_display_name} to approve tool use."
                self._set_agent_current_action(
                    package.agent.key,
                    f"Waiting for approval: {request.task_instruction[:160]}",
                    commit=False,
                )
                self.session.commit()
                self.session.refresh(task)
                execution_note = f"Agent run paused while waiting for {user_display_name} to approve tool use."
                status = "blocked"
            else:
                assembled_prompt = package.assembled_prompt
                if tool_call_payloads:
                    compact_tool_results = _compact_tool_results_for_prompt(tool_call_payloads)
                    assembled_prompt = (
                        f"{assembled_prompt}\n\n## Tool Results\n"
                        "The following compact tool-result evidence has already been executed by Maestro. "
                        "Full raw tool outputs remain stored in Maestro by tool_call_id; use these "
                        "summaries as evidence and call out if a full artifact/file read is needed. "
                        "Use these results as evidence in your report. Do not emit tool-call XML, "
                        "JSON function-call requests, or instructions to call more tools; instead, "
                        "state any additional tool access needed as an open question or next step.\n\n"
                        f"{json.dumps(compact_tool_results, indent=2)}"
                    )
                tool_call = ToolCall(
                    task_id=task.id,
                    agent_id=package.agent.id,
                    tool_name="llm.gateway",
                    input_payload={
                        "provider": getattr(llm_client, "provider", "configured"),
                        "model": request.model_profile or package.agent.model_profile,
                        "prompt_chars": len(assembled_prompt),
                        "base_prompt_chars": len(package.assembled_prompt),
                        "tool_result_raw_chars": len(json.dumps(tool_call_payloads, default=str)),
                        "tool_result_prompt_chars": len(
                            json.dumps(_compact_tool_results_for_prompt(tool_call_payloads), default=str)
                        )
                        if tool_call_payloads
                        else 0,
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
                "Manual run prepared without an LLM call. Prompt, "
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
                "status": "manual_run",
                "reason": (
                    "This direct run-once path bypasses the durable scheduler queue."
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
                tool_planner_input = self._render_tool_planner_input(
                    package=package,
                    prior_results=prior_results,
                    iteration=index + 1,
                )
                planner_call.input_payload = {
                    **planner_call.input_payload,
                    "prompt_chars": len(tool_planner_input),
                    "base_prompt_chars": len(package.assembled_prompt),
                    "prior_result_raw_chars": len(json.dumps(prior_results, default=str)),
                    "prior_result_prompt_chars": len(
                        json.dumps(_compact_tool_results_for_prompt(prior_results), default=str)
                    ),
                }
                self.session.commit()
                planner_source = "llm_planner"
                plan = llm_client.structured_response(
                    instructions=_TOOL_PLANNER_INSTRUCTIONS,
                    input_text=tool_planner_input,
                    schema_name="agent_tool_plan",
                    schema=_TOOL_PLAN_SCHEMA,
                )
                requested = _normalize_tool_plan(plan)
                fallback_reason = ""
                if not requested and _should_try_deterministic_tool_fallback(package, prior_results):
                    requested = _deterministic_tool_plan(
                        package=package,
                        prior_results=prior_results,
                        iteration=index + 1,
                    )
                    if requested:
                        planner_source = "deterministic_fallback"
                        fallback_reason = "LLM planner returned no executable tool calls for an obvious supported input."
                        plan = {
                            "plan_summary": "Used deterministic fallback after the LLM planner returned no executable tool calls.",
                            "requires_final_answer": True,
                            "tool_calls": [
                                {
                                    "tool_key": item["tool_key"],
                                    "payload_json": json.dumps(item.get("payload") or {}),
                                    "rationale": item.get("rationale") or "",
                                }
                                for item in requested
                            ],
                        }
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
                    "planner_source": planner_source,
                    "fallback_reason": fallback_reason or None,
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
                    "planner_source": planner_source,
                    "fallback_reason": fallback_reason or None,
                    "requested_tools": requested,
                    "executed": [],
                    "blocked": [],
                }
                if not requested:
                    trace["iterations"].append(iteration_trace)
                    break
                self._execute_auto_tool_requests(
                    tool_service=tool_service,
                    package=package,
                    task=task,
                    requested=requested,
                    iteration_trace=iteration_trace,
                    executed_calls=executed_calls,
                    prior_results=prior_results,
                )
                trace["iterations"].append(iteration_trace)
                if iteration_trace["blocked"] or not iteration_trace["executed"]:
                    break
            except Exception as exc:
                fallback_requested = _deterministic_tool_plan(
                    package=package,
                    prior_results=prior_results,
                    iteration=index + 1,
                )
                if fallback_requested:
                    planner_call.status = "complete"
                    planner_call.error_message = None
                    planner_call.output_payload = {
                        "plan_summary": "Used deterministic fallback after the LLM tool planner failed.",
                        "tool_call_count": len(fallback_requested),
                        "requires_final_answer": True,
                        "planner_source": "deterministic_fallback",
                        "fallback_reason": str(exc),
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
                        "plan_summary": planner_call.output_payload["plan_summary"],
                        "planner_source": "deterministic_fallback",
                        "fallback_reason": str(exc),
                        "requested_tools": fallback_requested,
                        "executed": [],
                        "blocked": [],
                    }
                    self._execute_auto_tool_requests(
                        tool_service=tool_service,
                        package=package,
                        task=task,
                        requested=fallback_requested,
                        iteration_trace=iteration_trace,
                        executed_calls=executed_calls,
                        prior_results=prior_results,
                    )
                    trace["iterations"].append(iteration_trace)
                    if iteration_trace["blocked"] or not iteration_trace["executed"]:
                        break
                    continue
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

    def _execute_auto_tool_requests(
        self,
        *,
        tool_service: ToolExecutionService,
        package: PromptPackage,
        task: Task,
        requested: list[dict[str, Any]],
        iteration_trace: dict[str, Any],
        executed_calls: list[dict[str, Any]],
        prior_results: list[dict[str, Any]],
    ) -> None:
        for requested_tool in requested:
            tool_key = requested_tool["tool_key"]
            payload = requested_tool.get("payload") or {}
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
                    "payload": payload,
                    "safety_level": policy["level"],
                    "reason": policy["reason"],
                    "rationale": requested_tool.get("rationale"),
                }
                iteration_trace["blocked"].append(blocked)
                proposed = tool_service.propose_for_task(
                    ToolExecutionRequest(
                        agent_key=package.agent.key,
                        tool_key=tool_key,
                        payload=payload,
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
                    payload=payload,
                    dry_run=False,
                ),
                task=task,
            )
            result_payload = tool_result_payload(result)
            executed_calls.append(result_payload)
            prior_results.append(result_payload)
            iteration_trace["executed"].append(result_payload)

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
                "permission": tool.permission,
                "safety": _compact_tool_safety(tool.key),
            }
            for tool in package.tool_manifest
        ]
        prompt_brief = _tool_planning_prompt_brief(package)
        return "\n\n".join(
            [
                prompt_brief,
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
                "## Prior Tool Results\n" + json.dumps(
                    _compact_tool_results_for_prompt(prior_results),
                    indent=2,
                ),
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


def _deterministic_tool_plan(
    *,
    package: PromptPackage,
    prior_results: list[dict[str, Any]],
    iteration: int,
) -> list[dict[str, Any]]:
    allowed = {tool.key for tool in package.tool_manifest}
    task_text = package.task_instruction.lower()
    prompt = f"{package.task_instruction}\n\n{package.assembled_prompt}".lower()
    google_plan = _deterministic_google_workspace_file_plan(
        allowed=allowed,
        prompt=prompt,
        prior_results=prior_results,
    )
    if google_plan:
        return google_plan
    if (
        "gmail.message.list_recent" in allowed
        and "gmail.message.get" in allowed
        and "email" in task_text
        and any(token in task_text for token in ("latest", "recent", "inbox"))
    ):
        requested_count = _requested_email_count(task_text)
        if not _has_tool_result(prior_results, "gmail.message.list_recent"):
            return [
                {
                    "tool_key": "gmail.message.list_recent",
                    "payload": {
                        "limit": requested_count,
                        "newer_than_days": 365,
                        "unread_only": False,
                    },
                    "rationale": "Read recent Praxis Gmail message metadata before summarizing it.",
                }
            ]
        missing_message_ids = _gmail_message_ids_missing_body(prior_results)[:requested_count]
        if missing_message_ids:
            return [
                {
                    "tool_key": "gmail.message.get",
                    "payload": {
                        "message_id": message_id,
                        "max_body_chars": 6000,
                    },
                    "rationale": "Read the selected Gmail message body for triage.",
                }
                for message_id in missing_message_ids
            ]
    return []


def _should_try_deterministic_tool_fallback(
    package: PromptPackage,
    prior_results: list[dict[str, Any]],
) -> bool:
    allowed = {tool.key for tool in package.tool_manifest}
    task_text = package.task_instruction.lower()
    prompt = f"{package.task_instruction}\n\n{package.assembled_prompt}".lower()
    if _deterministic_google_workspace_file_plan(
        allowed=allowed,
        prompt=prompt,
        prior_results=prior_results,
    ):
        return True
    return (
        "gmail.message.list_recent" in allowed
        and "gmail.message.get" in allowed
        and "email" in task_text
        and any(token in task_text for token in ("latest", "recent", "inbox"))
    )


def _deterministic_google_workspace_file_plan(
    *,
    allowed: set[str],
    prompt: str,
    prior_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    url = _google_workspace_url_from_text(prompt)
    if not url:
        return []
    file_id = _google_file_id_from_url_text(url)
    payload = {"url": url}
    if file_id:
        payload["file_id"] = file_id
    if "/presentation/" in url and "google.slides.get" in allowed and not _has_tool_result(prior_results, "google.slides.get"):
        return [
            {
                "tool_key": "google.slides.get",
                "payload": payload,
                "rationale": "Read the linked Google Slides deck enough to verify readability.",
            }
        ]
    if "/document/" in url and "google.docs.get" in allowed and not _has_tool_result(prior_results, "google.docs.get"):
        return [
            {
                "tool_key": "google.docs.get",
                "payload": payload,
                "rationale": "Read the linked Google Doc enough to verify readability.",
            }
        ]
    if "/spreadsheets/" in url and "google.sheets.get" in allowed and not _has_tool_result(prior_results, "google.sheets.get"):
        return [
            {
                "tool_key": "google.sheets.get",
                "payload": payload,
                "rationale": "Read Google Sheets metadata for the linked spreadsheet.",
            }
        ]
    if any(marker in url for marker in ("/presentation/", "/document/", "/spreadsheets/")):
        return []
    if "google.drive.file.get" in allowed and not _has_tool_result(prior_results, "google.drive.file.get"):
        return [
            {
                "tool_key": "google.drive.file.get",
                "payload": payload,
                "rationale": "Read Google Drive metadata for the linked Google Workspace file.",
            }
        ]
    return []


def _google_workspace_url_from_text(text: str) -> str | None:
    match = re.search(r"https?://(?:(?:docs|drive)\.google\.com|meet\.google\.com)/[^\s<>)\"']+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,;]")


def _google_file_id_from_url_text(url: str) -> str | None:
    for pattern in (
        r"/document/d/([^/?#]+)",
        r"/spreadsheets/d/([^/?#]+)",
        r"/presentation/d/([^/?#]+)",
        r"/file/d/([^/?#]+)",
        r"[?&]id=([^&#]+)",
    ):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _has_tool_result(prior_results: list[dict[str, Any]], tool_name: str) -> bool:
    return any(
        isinstance(result, dict)
        and result.get("tool_name") == tool_name
        and result.get("status") in {"complete", "approval_required"}
        for result in prior_results
    )


def _requested_email_count(prompt: str) -> int:
    match = re.search(r"\b(?:latest|recent|last)\s+(\d{1,2})\s+(?:emails?|messages?)\b", prompt)
    if not match:
        return 1
    return max(1, min(int(match.group(1)), 5))


def _gmail_message_ids_missing_body(prior_results: list[dict[str, Any]]) -> list[str]:
    fetched_ids = {
        str(output.get("message_id") or output.get("id"))
        for result in prior_results
        if isinstance(result, dict) and result.get("tool_name") == "gmail.message.get"
        for output in [result.get("output_payload")]
        if isinstance(output, dict) and (output.get("message_id") or output.get("id"))
    }
    for result in reversed(prior_results):
        if not isinstance(result, dict) or result.get("tool_name") != "gmail.message.list_recent":
            continue
        output = result.get("output_payload")
        if not isinstance(output, dict):
            continue
        messages = output.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        message_ids = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = message.get("message_id") or message.get("id")
            if message_id and str(message_id) not in fetched_ids:
                message_ids.append(str(message_id))
        return message_ids
    return []


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
    "memory.context_bundle": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Internal read-only retrieval from Maestro memory for RAG context.",
    },
    "routed.item.create": {
        "level": "internal_write",
        "auto_executable": True,
        "reason": "Creates internal routed-memory candidates with provenance inside Maestro.",
    },
    "workflow.notification.create": {
        "level": "internal_notification",
        "auto_executable": True,
        "reason": "Surfaces an internal, provenance-linked notification to Chris in Maestro.",
    },
    "reports.search": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Internal read-only retrieval from completed workflow reports.",
    },
    "reports.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Internal read-only retrieval of a specific workflow report.",
    },
    "artifact.stage_interaction": {
        "level": "internal_artifact_staging",
        "auto_executable": True,
        "reason": "Internal deferred artifact staging; Maestro stages final reports after completion.",
    },
    "llm.gateway": {
        "level": "internal_reasoning",
        "auto_executable": True,
        "reason": "Internal LLM reasoning call for an authorized agent task.",
    },
    "web.search": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only web search and synthesis through the authorized LLM provider.",
    },
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
    "github.read": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only aggregate GitHub repository inspection.",
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
    "gmail.message.search": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Gmail message search.",
    },
    "gmail.message.list_recent": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Gmail recent message listing.",
    },
    "gmail.message.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Gmail message inspection.",
    },
    "gmail.thread.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Gmail thread inspection.",
    },
    "gmail.draft.create": {
        "level": "external_write",
        "auto_executable": False,
        "reason": "Creates an external Gmail draft and requires Chris approval.",
    },
    "gmail.message.modify": {
        "level": "external_write",
        "auto_executable": False,
        "reason": "Modifies Gmail labels/read state and requires Chris approval.",
    },
    "google.drive.file.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Google Drive file metadata retrieval.",
    },
    "google.drive.file.export": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Google Drive file content export.",
    },
    "google.docs.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Google Docs document retrieval.",
    },
    "google.slides.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Google Slides presentation retrieval.",
    },
    "google.sheets.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Google Sheets spreadsheet metadata retrieval.",
    },
    "google.sheets.values.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Google Sheets cell values retrieval.",
    },
    "google.meet.conference_records.list": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Google Meet conference record listing.",
    },
    "google.meet.conference_records.get": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only Google Meet conference record retrieval.",
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
    "local.app.inspect": {
        "level": "safe_read",
        "auto_executable": True,
        "reason": "Read-only inspection of the dedicated local runtime checkout.",
    },
    "local.app.recover": {
        "level": "local_app_recovery",
        "auto_executable": False,
        "reason": "Stashes unexpected runtime changes after Chris approves preserving them.",
    },
    "local.app.deploy_pr": {
        "level": "production_code_delivery",
        "auto_executable": False,
        "reason": "Merges an approved PR and updates the dedicated runtime checkout.",
    },
}

_AUTO_TOOL_SAFE_TOOL_KEYS = {
    key for key, policy in _TOOL_SAFETY_POLICIES.items() if policy["auto_executable"]
}

_TOOL_PLANNER_INSTRUCTIONS = load_prompt("agent_tool_planner.md")

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
    "routed.item.create": {
        "name": "Create Routed Candidate",
        "description": (
            "Create internal routed candidates for contacts, todos, events, organizations, "
            "ideas, decisions, or RFIs and promote them into routed stores."
        ),
    },
    "workflow.notification.create": {
        "name": "Create Workflow Notification",
        "description": (
            "Notify Chris when an email or workflow requires his response, decision, deadline "
            "awareness, or attention to a material risk. Include source provenance."
        ),
    },
    "reports.search": {
        "name": "Report Search",
        "description": "Search completed workflow reports and return compact report summaries.",
    },
    "reports.get": {
        "name": "Report Get",
        "description": "Read the full markdown body for a specific completed workflow report.",
    },
    "artifact.stage_interaction": {
        "name": "Stage Interaction Artifact",
        "description": "Package interaction outputs for curator processing.",
    },
    "llm.gateway": {
        "name": "LLM Gateway",
        "description": "Call the configured LLM provider through Maestro's shared gateway.",
    },
    "web.search": {
        "name": "Web Search",
        "description": (
            "Use OpenRouter's server-side web search to gather current web context and cited "
            "findings for research tasks."
        ),
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
    "gmail": {
        "name": "Gmail",
        "description": "Shared Gmail OAuth/config inherited by Gmail tools.",
    },
    "gmail.message.search": {
        "name": "Gmail Message Search",
        "description": "Search Gmail messages with Gmail query syntax through the authorized domain account.",
    },
    "gmail.message.list_recent": {
        "name": "Gmail Recent Messages",
        "description": "List recent Gmail messages, optionally unread-only.",
    },
    "gmail.message.get": {
        "name": "Gmail Message Details",
        "description": "Read a full Gmail message, including headers and decoded text body.",
    },
    "gmail.thread.get": {
        "name": "Gmail Thread Details",
        "description": "Read all messages in a Gmail conversation thread.",
    },
    "gmail.draft.create": {
        "name": "Gmail Draft Create",
        "description": "Create a Gmail draft after Chris approval.",
    },
    "gmail.message.modify": {
        "name": "Gmail Message Modify",
        "description": "Apply or remove Gmail labels after Chris approval.",
    },
    "google": {
        "name": "Google Workspace",
        "description": "Shared Google Workspace OAuth/config inherited by Drive, Docs, Slides, and related tools.",
    },
    "google.drive.file.get": {
        "name": "Google Drive File Metadata",
        "description": "Read Google Drive file metadata and links through the authorized domain account.",
    },
    "google.drive.file.export": {
        "name": "Google Drive File Export",
        "description": "Export readable Google Workspace file content, such as Docs to text/plain.",
    },
    "google.docs.get": {
        "name": "Google Docs Read",
        "description": "Read a Google Doc and extract its text through the authorized domain account.",
    },
    "google.slides.get": {
        "name": "Google Slides Read",
        "description": "Read a Google Slides presentation and extract slide text through the authorized domain account.",
    },
    "google.sheets.get": {
        "name": "Google Sheets Read",
        "description": "Read Google Sheets spreadsheet metadata and sheet tabs through the authorized domain account.",
    },
    "google.sheets.values.get": {
        "name": "Google Sheets Values Read",
        "description": "Read values from a specific range in a Google Sheet through the authorized domain account.",
    },
    "google.meet.conference_records.list": {
        "name": "Google Meet Conference Records List",
        "description": "List recent Google Meet conference records visible to the authorized domain account.",
    },
    "google.meet.conference_records.get": {
        "name": "Google Meet Conference Record Read",
        "description": "Read a Google Meet conference record visible to the authorized domain account.",
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
    "local.app.inspect": {
        "name": "Local Runtime Inspect",
        "description": "Inspect the dedicated runtime checkout without changing files.",
    },
    "local.app.recover": {
        "name": "Local Runtime Recovery",
        "description": "Preserve unexpected runtime changes in an approved Git stash.",
    },
    "local.app.deploy_pr": {
        "name": "Deploy Approved PR",
        "description": "Merge a reviewed pull request and update the dedicated local runtime.",
    },
}


def _provider_connection_key(tool_key: str) -> str:
    if tool_key.startswith("github."):
        return "github"
    if tool_key.startswith("gmail."):
        return "google"
    if tool_key.startswith("google."):
        return "google"
    if tool_key.startswith("codex."):
        return "codex"
    if tool_key.startswith("local.app."):
        return "local.app.reload"
    return tool_key


def _inherited_connection_tool_keys(tool_key: str) -> list[str]:
    if tool_key == "github":
        return [key for key in _TOOL_DESCRIPTIONS if key.startswith("github.")]
    if tool_key == "google":
        return [
            key
            for key in _TOOL_DESCRIPTIONS
            if key.startswith("google.") or key.startswith("gmail.")
        ]
    if tool_key == "codex":
        return [key for key in _TOOL_DESCRIPTIONS if key.startswith("codex.")]
    return []


def _with_internal_default_tool_permissions(raw_permissions: dict[str, Any]) -> dict[str, Any]:
    permissions = dict(raw_permissions or {})
    permissions.setdefault(
        "memory.context_bundle",
        {
            "permission": "read",
            "description": "Retrieve domain-scoped memory bundles for RAG.",
        },
    )
    permissions.setdefault(
        "reports.search",
        {
            "permission": "read",
            "description": "Search completed workflow reports visible to this domain.",
        },
    )
    permissions.setdefault(
        "reports.get",
        {
            "permission": "read",
            "description": "Read a specific completed workflow report.",
        },
    )
    return permissions


def _compact_task_instruction_for_prompt(task_instruction: str) -> str:
    text = task_instruction.strip()
    marker = "Assigned decomposed work items:"
    if marker not in text:
        return _truncate_text(text, 4000)
    assigned = text.split(marker, 1)[1].strip()
    preamble_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("You are "):
            preamble_lines.append(stripped)
        if stripped.startswith("Your specialty:"):
            preamble_lines.append(stripped)
        if len(preamble_lines) >= 2:
            break
    compact = "\n".join(
        [
            "Agent task brief:",
            *preamble_lines,
            "",
            marker,
            assigned,
        ]
    )
    return _truncate_text(compact, 5000)


def _memory_query_for_prompt_package(
    *,
    request: PromptPackageRequest,
    agent: AgentSpec,
    domain_name: str,
    domain_key: str,
    domain_context: str,
) -> str:
    """Build a retrieval query that includes stable domain anchors plus the task."""
    query_parts = [
        request.query_text,
        request.task_instruction,
        request.user_context,
        f"domain background for {domain_name or domain_key} {domain_key}",
        f"agent role {agent.name} {agent.role_summary}",
        "stable operating context Chris preferences domain identity prior decisions",
        domain_context,
    ]
    query = "\n".join(part.strip() for part in query_parts if part and part.strip())
    return _truncate_text(query, 2500)


def _tool_planning_prompt_brief(package: PromptPackage) -> str:
    tools = ", ".join(tool.key for tool in package.tool_manifest) or "none"
    sections = [
        ("Agent", f"{package.agent.name} ({package.agent.key}) in {package.agent.domain_key}"),
        ("Role", _truncate_text(package.role_prompt or package.agent.role_summary, 700)),
        ("Task", _compact_task_instruction_for_prompt(package.task_instruction)),
        ("Memory Summary", _truncate_text(package.memory_context.rendered_text, 1200)),
        ("Allowed Tool Keys", _truncate_text(tools, 1200)),
    ]
    return "\n\n".join(f"## {title}\n{body}".strip() for title, body in sections if body)


def _compact_tool_safety(tool_key: str) -> dict[str, Any]:
    policy = _TOOL_SAFETY_POLICIES.get(
        tool_key,
        {
            "level": "approval_required",
            "auto_executable": False,
            "reason": "Tool is not approved for autonomous execution.",
        },
    )
    return {
        "level": policy.get("level"),
        "auto_executable": bool(policy.get("auto_executable")),
    }


def _compact_tool_results_for_prompt(
    tool_results: list[dict[str, Any]],
    *,
    max_total_chars: int = 7000,
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    remaining = max_total_chars
    for result in tool_results:
        if remaining <= 0:
            compact.append(
                {
                    "id": result.get("id"),
                    "tool_name": result.get("tool_name"),
                    "status": result.get("status"),
                    "omitted": "Prompt evidence budget exhausted; full output remains stored by tool_call_id.",
                }
            )
            continue
        item = _compact_single_tool_result(result, max_chars=min(remaining, 2200))
        item_chars = len(json.dumps(item, default=str))
        if item_chars > remaining:
            item["evidence"] = _truncate_text(str(item.get("evidence") or ""), max(120, remaining - 400))
            item["truncated"] = True
            item_chars = len(json.dumps(item, default=str))
        compact.append(item)
        remaining -= item_chars
    return compact


def _compact_single_tool_result(result: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    output = result.get("output_payload")
    compact: dict[str, Any] = {
        "id": result.get("id"),
        "tool_name": result.get("tool_name"),
        "status": result.get("status"),
    }
    if result.get("error_message"):
        compact["error_message"] = _truncate_text(str(result.get("error_message")), 500)
    if result.get("connection_id"):
        compact["connection_id"] = result.get("connection_id")
    if output is None:
        compact["summary"] = "No output payload."
        return compact
    compact["summary"] = _compact_output_summary(output)
    evidence = _evidence_text(output)
    if evidence:
        compact["evidence"] = _truncate_text(evidence, max(200, max_chars - 700))
    compact["raw_output_chars"] = len(json.dumps(output, default=str))
    if compact["raw_output_chars"] > len(json.dumps(compact, default=str)):
        compact["full_output"] = "stored_in_tool_call_output_payload"
    return compact


def _compact_output_summary(output: Any) -> Any:
    if not isinstance(output, dict):
        return _truncate_text(str(output), 600)
    summary = output.get("summary")
    if summary is not None:
        return _truncate_nested(summary, max_text_chars=500)
    keys = list(output.keys())
    compact = {
        "type": output.get("type") or output.get("object") or "tool_output",
        "keys": keys[:12],
    }
    for count_key in ("included_count", "dropped_count", "total_count", "count", "issue_count", "result_count"):
        if count_key in output:
            compact[count_key] = output[count_key]
    for id_key in ("url", "html_url", "number", "title", "path", "repo", "branch", "pr_number"):
        if id_key in output:
            compact[id_key] = _truncate_nested(output[id_key], max_text_chars=220)
    return compact


def _evidence_text(output: Any) -> str:
    if not isinstance(output, dict):
        return str(output)
    parts: list[str] = []
    for key in (
        "rendered_text",
        "output_text",
        "body",
        "content",
        "text",
        "markdown",
        "diff",
        "output_preview",
    ):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}:\n{value.strip()}")
    for key in ("annotations", "citations", "results", "issues", "pull_requests", "files", "items"):
        value = output.get(key)
        if value:
            parts.append(f"{key}:\n{json.dumps(_truncate_nested(value), default=str)}")
    if not parts:
        parts.append(json.dumps(_truncate_nested(output), default=str))
    return "\n\n".join(parts)


def _truncate_nested(value: Any, *, max_text_chars: int = 350, max_items: int = 5) -> Any:
    if isinstance(value, str):
        return _truncate_text(value, max_text_chars)
    if isinstance(value, list):
        return [_truncate_nested(item, max_text_chars=max_text_chars, max_items=max_items) for item in value[:max_items]]
    if isinstance(value, dict):
        return {
            str(key): _truncate_nested(item, max_text_chars=max_text_chars, max_items=max_items)
            for key, item in list(value.items())[:12]
        }
    return value


def _truncate_text(value: str, max_chars: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - 3)].rstrip() + "..."


def _llm_client_for_model_profile(model_profile: str | None) -> LLMClient:
    profile = (model_profile or "default").strip()
    settings = get_settings()
    if not profile or profile == "default":
        return OpenAILLMClient()
    if profile.startswith("ollama:"):
        model = profile.removeprefix("ollama:").strip()
        if not model:
            raise AgentRuntimeError("Ollama model profile must include a model name.")
        return OllamaLLMClient(
            model=model,
            base_url=settings.embedding_base_url,
            timeout_seconds=settings.ollama_llm_timeout_seconds,
        )
    if profile.startswith("openrouter:"):
        model = profile.removeprefix("openrouter:").strip()
        return OpenAILLMClient(provider="openrouter", model=model or None)
    if profile.startswith("openai:"):
        model = profile.removeprefix("openai:").strip()
        return OpenAILLMClient(provider="openai", model=model or None)
    return OpenAILLMClient(model=profile)


def _scoped_skill_manifest(
    skills: list[SkillManifestItem],
    required_skills: list[str] | None,
) -> list[SkillManifestItem]:
    requested = [key.strip() for key in (required_skills or []) if key and key.strip()]
    if not requested:
        return skills
    by_key = {skill.key: skill for skill in skills}
    return [by_key[key] for key in requested if key in by_key]


_SEED_SKILLS = [
    {
        "key": "email_triage",
        "name": "Email Triage",
        "category": "workflow",
        "description": "Classify incoming domain email and decide what to notify, route, or ignore.",
        "domain_key": None,
        "instruction": """## Purpose
Classify incoming domain email and decide what should be ignored, surfaced to Chris, routed into Maestro stores, or processed by follow-on work.

## Use When
- A work item asks you to review Gmail, an inbox, a message, or a thread.
- An email may contain contacts, organizations, events, due-outs, or useful context.

## Do Not Use When
- The source is not an email/message/thread.
- The task is only drafting a reply from already-known context.

## Procedure
1. Read message metadata first: sender, recipients, subject, date, message_id, thread_id, labels.
2. Fetch full message or thread only when needed.
3. Classify as `spam_noise`, `response_needed`, `useful_info`, or `action_required`.
4. If Chris needs to respond or decide, a material deadline is approaching, or the message exposes
   meaningful risk, call `workflow.notification.create`. Useful information alone does not warrant
   a notification.
5. Extract routed candidates only for durable objects: contacts, events, organizations, and Chris-owned todos.
6. Never create todos for your own agent steps such as "record contact" or "triage email".
7. Preserve Gmail provenance in every candidate: message_id, thread_id, subject, sender, and date.

## Output Contract
- Email classification and confidence.
- Whether Chris was notified and the concrete reason, or why no notification was warranted.
- Routed candidates created, grouped by type.
- Any approval requests such as marking read or creating a draft.
- Brief evidence for each decision.

For a warranted notification, call `workflow.notification.create` with `title`, `message`,
`severity`, `reason`, and Gmail provenance fields `message_id`, `thread_id`, `subject`, and `from`.

## Validation
- If it is spam/noise, explain why before requesting any Gmail modification.
- If a candidate lacks enough identity/date/title information, create an RFI instead of guessing.""",
        "metadata": {"seeded_by": "maestro"},
    },
    {
        "key": "contact_manager",
        "name": "Contact Manager",
        "category": "routed_memory",
        "description": "Create or update contact candidates from interactions.",
        "domain_key": None,
        "instruction": """## Purpose
Create high-quality contact candidates for real people so the routed resolver can create or update canonical contacts.

## Use When
- A person is mentioned with useful identity, role, relationship, or contact information.
- A message/report describes a relationship, affiliation, preference, or interaction with a person.

## Do Not Use When
- The item is an organization, team, project, or abstract role with no person.
- The only action is for Maestro/agent to record the contact; that is not a Chris todo.

## Procedure
1. Use the person's canonical display name as the candidate title.
2. Put extracted fields in metadata: `name`, `email`, `phone`, `linkedin`, `organization`, `summary`, `relationship_context`, `last_contact_at`, `aliases`.
3. Keep content as a short human-readable summary of why this contact matters.
4. Include source_refs with the source message/report/artifact.
5. Do not dedupe manually. If it might match an existing person, include aliases and provenance; the routed resolver adjudicates merge/update.

## Output Contract
Call `routed.item.create` with route_type `contact`, title as the person name, content summary, metadata fields, and source_refs.

## Validation
- Title must not be generic like "record contact" or "partner lead".
- If only a first name is known, include contextual aliases/source refs and note uncertainty in metadata.""",
        "metadata": {"seeded_by": "maestro"},
    },
    {
        "key": "to_do_manager",
        "name": "To Do Manager",
        "category": "routed_memory",
        "description": "Create Chris-owned task/reminder candidates from interactions.",
        "domain_key": None,
        "instruction": """## Purpose
Create todo/reminder candidates only for obligations that Chris personally needs to track.

## Use When
- Chris needs to do, decide, send, review, approve, bring, pay, call, or follow up on something.
- A due-out/reminder belongs on Chris's task list even if an agent discovered it.

## Do Not Use When
- The action is agent-internal work such as "extract contact", "record event", "summarize email", or "route this item".
- The task belongs to another agent or an external person unless Chris needs to monitor it.

## Procedure
1. Title must be a concrete action phrase.
2. Description explains context, source, and why it matters.
3. Metadata may include `due_at`, `owner_type=user`, `owner_ref=Chris`, `related_contact`, `related_event`, `blocking`.
4. Set priority based on deadline/impact.
5. Include source_refs.

## Output Contract
Call `routed.item.create` with route_type `task`, title, description/content, priority, metadata, and source_refs.

## Validation
- If the task is for Maestro or an agent to execute immediately, do not create a todo; it belongs in workflow work.
- If due date is ambiguous, create the task with uncertainty in metadata instead of inventing a date.""",
        "metadata": {"seeded_by": "maestro"},
    },
    {
        "key": "calendar_manager",
        "name": "Calendar Manager",
        "category": "routed_memory",
        "description": "Create or update event candidates from interactions.",
        "domain_key": None,
        "instruction": """## Purpose
Create event candidates for meetings, calls, deadlines with time windows, travel, ceremonies, and other calendar-worthy items.

## Use When
- A source contains a date/time, meeting/call/sync, appointment, travel window, or event summary.
- A prior event is being updated with new time/location/attendee details.

## Do Not Use When
- The item is only an undated task or general reminder.
- The time/date is too ambiguous to be useful; create an RFI instead.

## Procedure
1. Title should be what would appear on a calendar, e.g. "Partner sync with Jane Smith".
2. Metadata should include `event_title`, `start_at`, `end_at`, `duration_minutes`, `location`, `attendees`, and `summary` when known.
3. If attendees are mentioned and contacts do not exist, still include attendee names; the routed resolver can create/link contacts.
4. Infer a reasonable duration only when the source implies a typical meeting and uncertainty is low.
5. Include source_refs.

## Output Contract
Call `routed.item.create` with route_type `event`, title, content summary, metadata, and source_refs.

## Validation
- Do not title events "recorded meeting metadata" or similar system language.
- Ask for clarification if missing date/time would cause a bad calendar entry.""",
        "metadata": {"seeded_by": "maestro"},
    },
    {
        "key": "organization_manager",
        "name": "Organization Manager",
        "category": "routed_memory",
        "description": "Create or update organization candidates from interactions.",
        "domain_key": None,
        "instruction": """## Purpose
Create organization candidates for companies, agencies, military units, vendors, partners, schools, labs, and institutions.

## Use When
- A source names an organization with useful relationship, context, website, contact, or opportunity information.
- A person/contact is affiliated with an organization and the organization itself matters.

## Do Not Use When
- The name is a person, product feature, event, or vague group with no durable identity.

## Procedure
1. Use the organization name as the title.
2. Metadata may include `entity_name`, `website`, `summary`, `relationship_context`, `domain_context`, `known_contacts`, and `aliases`.
3. Content should summarize the domain-specific relevance.
4. Include source_refs.
5. Do not dedupe manually; provide aliases/provenance and let the routed resolver merge/update.

## Output Contract
Call `routed.item.create` with route_type `entity`, title as organization name, content summary, metadata, and source_refs.

## Validation
- Do not create organization candidates for generic nouns like "partner" or "customer" unless a named organization is known.""",
        "metadata": {"seeded_by": "maestro"},
    },
]


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
        "role_prompt": load_prompt("agents/praxis_planning_agent.md"),
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
            "gmail.message.search": {
                "permission": "read",
                "description": "Search Praxis Gmail for partner and workflow context.",
            },
            "gmail.message.list_recent": {
                "permission": "read",
                "description": "List recent Praxis Gmail messages for triage.",
            },
            "gmail.message.get": {
                "permission": "read",
                "description": "Read selected Praxis Gmail messages.",
            },
            "gmail.thread.get": {
                "permission": "read",
                "description": "Read Praxis Gmail conversation threads.",
            },
        },
    },
    {
        "domain_key": "praxis",
        "key": "praxis-email-agent",
        "name": "Praxis Email Agent",
        "agent_type": "domain_agent",
        "role_summary": (
            "Triages Praxis Gmail, identifies email requiring Chris' attention, and routes "
            "contacts, organizations, events, and Chris-owned todos into Maestro stores."
        ),
        "role_prompt": load_prompt("agents/praxis_email_agent.md"),
        "memory_profile": "agent_prompt",
        "model_profile": "ollama:qwen3:8b",
        "tool_permissions": {
            "memory.context_bundle": {
                "permission": "read",
                "description": "Retrieve Praxis-scoped memory bundles.",
            },
            "reports.search": {
                "permission": "read",
                "description": "Search prior Praxis reports for email context.",
            },
            "reports.get": {
                "permission": "read",
                "description": "Read relevant prior Praxis reports.",
            },
            "artifact.stage_interaction": {
                "permission": "write",
                "description": "Stage Praxis email triage interaction packages.",
            },
            "llm.gateway": {
                "permission": "use",
                "description": "Use Maestro's shared LLM gateway.",
            },
            "gmail.message.search": {
                "permission": "read",
                "description": "Search Praxis Gmail for triage context.",
            },
            "gmail.message.list_recent": {
                "permission": "read",
                "description": "List recent Praxis Gmail messages for triage.",
            },
            "gmail.message.get": {
                "permission": "read",
                "description": "Read selected Praxis Gmail messages.",
            },
            "gmail.thread.get": {
                "permission": "read",
                "description": "Read Praxis Gmail conversation threads.",
            },
            "gmail.message.modify": {
                "permission": "use",
                "description": "Mark spam/noise messages read when approved.",
            },
            "routed.item.create": {
                "permission": "write",
                "description": "Create routed candidates for contacts, todos, events, organizations, and ideas.",
            },
            "workflow.notification.create": {
                "permission": "write",
                "description": "Surface Praxis email that needs Chris' attention in Maestro chat.",
            },
            "google.drive.file.get": {
                "permission": "read",
                "description": "Read metadata for linked Google Workspace files in Praxis emails.",
            },
            "google.drive.file.export": {
                "permission": "read",
                "description": "Export readable linked Google Workspace files when authorized.",
            },
            "google.docs.get": {
                "permission": "read",
                "description": "Read linked Google Docs such as meeting notes when authorized.",
            },
            "google.slides.get": {
                "permission": "read",
                "description": "Read linked Google Slides decks when authorized.",
            },
            "google.sheets.get": {
                "permission": "read",
                "description": "Read linked Google Sheets metadata when authorized.",
            },
            "google.sheets.values.get": {
                "permission": "read",
                "description": "Read linked Google Sheets values when authorized.",
            },
            "google.meet.conference_records.list": {
                "permission": "read",
                "description": "List Google Meet conference records when authorized.",
            },
            "google.meet.conference_records.get": {
                "permission": "read",
                "description": "Read Google Meet conference records when authorized.",
            },
        },
        "skill_permissions": {
            "email_triage": {"permission": "use"},
            "contact_manager": {"permission": "use"},
            "to_do_manager": {"permission": "use"},
            "calendar_manager": {"permission": "use"},
            "organization_manager": {"permission": "use"},
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
        "role_prompt": load_prompt("agents/maestro_introspection_agent.md"),
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
            "web.search": {
                "permission": "read",
                "description": "Search the web for current SOTA/tooling context with citations.",
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
            "local.app.inspect": {
                "permission": "read",
                "description": "Inspect runtime Git state before deployment.",
            },
            "local.app.recover": {
                "permission": "use",
                "description": "Preserve unexpected runtime changes when Chris approves recovery.",
            },
            "local.app.deploy_pr": {
                "permission": "use",
                "description": "Merge a reviewed PR and update the dedicated Maestro runtime.",
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
        "role_prompt": load_prompt("agents/maestro_coding_agent.md"),
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
            "local.app.inspect": {
                "permission": "read",
                "description": "Inspect runtime Git state before deployment.",
            },
            "local.app.recover": {
                "permission": "use",
                "description": "Preserve unexpected runtime changes when Chris approves recovery.",
            },
            "local.app.deploy_pr": {
                "permission": "use",
                "description": "Merge a reviewed PR and update the dedicated Maestro runtime.",
            },
        },
    },
]
