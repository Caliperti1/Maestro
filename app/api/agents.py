from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentRuntimeError,
    AgentRegistryService,
    InteractionArtifactPackager,
    InteractionArtifactPackage,
    PromptAggregationService,
    PromptPackageRequest,
    ToolManifestItem,
)
from app.db.session import get_db

router = APIRouter(prefix="/agents", tags=["agents"])


class PromptPackageBody(BaseModel):
    task_instruction: str
    caller: str = "maestro"
    user_context: str | None = None
    query_text: str | None = None
    max_memory_items: int = 10
    max_memory_chars: int = 3500
    use_semantic: bool = True


class DomainContextBody(BaseModel):
    context: str


class GlobalContextBody(BaseModel):
    context: str


class AgentCreateBody(BaseModel):
    domain_key: str
    key: str
    name: str
    agent_type: str = "domain_agent"
    role_summary: str = ""
    role_prompt: str = ""
    memory_profile: str = "agent_prompt"
    model_profile: str = "default"
    tool_permissions: dict[str, Any] = Field(default_factory=dict)
    current_action: str | None = None


class AgentUpdateBody(BaseModel):
    role_summary: str | None = None
    role_prompt: str | None = None
    memory_profile: str | None = None
    model_profile: str | None = None
    tool_permissions: dict[str, Any] | None = None
    current_action: str | None = None
    scheduled_actions: list[dict[str, Any]] | None = None
    is_active: bool | None = None


class ToolConnectionBody(BaseModel):
    domain_key: str
    tool_key: str
    display_name: str
    auth_type: str = "manual"
    config: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class AgentRunOnceBody(PromptPackageBody):
    stage_interaction: bool = False


class InteractionArtifactBody(BaseModel):
    domain_key: str
    agent_key: str | None = None
    user_input: str | None = None
    maestro_tasking: str | None = None
    agent_output: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    generated_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    task_id: str | None = None
    conversation_id: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    stage: bool = False


@router.get("")
def list_agents(db: Session = Depends(get_db)) -> dict[str, Any]:
    specs = AgentRegistryService(db).list_specs()
    return {"agents": [_agent_payload(spec) for spec in specs]}


@router.post("")
def create_agent(body: AgentCreateBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        spec = AgentRegistryService(db).create_agent_spec(
            domain_key=body.domain_key,
            key=body.key,
            name=body.name,
            agent_type=body.agent_type,
            role_summary=body.role_summary,
            role_prompt=body.role_prompt,
            memory_profile=body.memory_profile,
            model_profile=body.model_profile,
            tool_permissions=body.tool_permissions,
            current_action=body.current_action,
        )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"agent": _agent_payload(spec)}


@router.get("/global-context")
def get_global_context(db: Session = Depends(get_db)) -> dict[str, Any]:
    context = AgentRegistryService(db).get_global_context()
    return {"global_context": {"context": context.context}}


@router.patch("/global-context")
def update_global_context(
    body: GlobalContextBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        context = AgentRegistryService(db).update_global_context(body.context)
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"global_context": {"context": context.context}}


@router.get("/domains")
def list_domain_contexts(db: Session = Depends(get_db)) -> dict[str, Any]:
    domains = AgentRegistryService(db).list_domain_contexts()
    return {
        "domains": [
            {
                "id": str(domain.id),
                "key": domain.key,
                "name": domain.name,
                "context": domain.context,
                "is_active": domain.is_active,
            }
            for domain in domains
        ]
    }


@router.patch("/domains/{domain_key}")
def update_domain_context(
    domain_key: str,
    body: DomainContextBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        domain = AgentRegistryService(db).update_domain_context(domain_key, body.context)
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "domain": {
            "id": str(domain.id),
            "key": domain.key,
            "name": domain.name,
            "context": domain.context,
            "is_active": domain.is_active,
        }
    }


@router.get("/tools")
def list_tools(db: Session = Depends(get_db)) -> dict[str, Any]:
    tools = AgentRegistryService(db).list_tools()
    return {
        "tools": [
            {
                "key": tool.key,
                "name": tool.name,
                "description": tool.description,
                "exclusive": tool.exclusive,
                "connected_domains": tool.connected_domains,
                "authorized_agents": tool.authorized_agents,
            }
            for tool in tools
        ]
    }


@router.get("/tools/connections")
def list_tool_connections(db: Session = Depends(get_db)) -> dict[str, Any]:
    connections = AgentRegistryService(db).list_tool_connections()
    return {"connections": [_tool_connection_payload(connection) for connection in connections]}


@router.put("/tools/connections")
def upsert_tool_connection(
    body: ToolConnectionBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        connection = AgentRegistryService(db).upsert_tool_connection(
            domain_key=body.domain_key,
            tool_key=body.tool_key,
            display_name=body.display_name,
            auth_type=body.auth_type,
            config=body.config,
            is_active=body.is_active,
        )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"connection": _tool_connection_payload(connection)}


@router.get("/{agent_key}")
def get_agent(agent_key: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        spec = AgentRegistryService(db).get_spec(agent_key)
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"agent": _agent_payload(spec)}


@router.patch("/{agent_key}")
def update_agent(
    agent_key: str,
    body: AgentUpdateBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        spec = AgentRegistryService(db).update_agent_spec(
            agent_key,
            role_summary=body.role_summary,
            role_prompt=body.role_prompt,
            memory_profile=body.memory_profile,
            model_profile=body.model_profile,
            tool_permissions=body.tool_permissions,
            current_action=body.current_action,
            scheduled_actions=body.scheduled_actions,
            is_active=body.is_active,
        )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"agent": _agent_payload(spec)}


@router.post("/{agent_key}/prompt-package")
def build_prompt_package(
    agent_key: str,
    body: PromptPackageBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        package = PromptAggregationService(db).build_prompt_package(
            PromptPackageRequest(
                agent_key=agent_key,
                task_instruction=body.task_instruction,
                caller=body.caller,  # type: ignore[arg-type]
                user_context=body.user_context,
                query_text=body.query_text,
                max_memory_items=body.max_memory_items,
                max_memory_chars=body.max_memory_chars,
                use_semantic=body.use_semantic,
            )
        )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prompt_package": _prompt_package_payload(package)}


@router.post("/{agent_key}/run-once")
def run_agent_once(
    agent_key: str,
    body: AgentRunOnceBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = PromptAggregationService(db).run_agent_once(
            PromptPackageRequest(
                agent_key=agent_key,
                task_instruction=body.task_instruction,
                caller=body.caller,  # type: ignore[arg-type]
                user_context=body.user_context,
                query_text=body.query_text,
                max_memory_items=body.max_memory_items,
                max_memory_chars=body.max_memory_chars,
                use_semantic=body.use_semantic,
            ),
            stage_interaction=body.stage_interaction,
        )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "run": {
            "run_id": result.run_id,
            "status": result.status,
            "agent": _agent_payload(result.agent),
            "prompt_package": _prompt_package_payload(result.prompt_package),
            "scheduler": result.scheduler,
            "execution_note": result.execution_note,
            "staged_artifact_path": result.staged_artifact_path,
            "artifact_id": result.artifact_id,
        }
    }


@router.post("/interaction-artifacts")
def build_interaction_artifact(
    body: InteractionArtifactBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    packager = InteractionArtifactPackager(db)
    try:
        package = packager.build_package(
            domain_key=body.domain_key,
            agent_key=body.agent_key,
            user_input=body.user_input,
            maestro_tasking=body.maestro_tasking,
            agent_output=body.agent_output,
            tool_calls=body.tool_calls,
            generated_artifacts=body.generated_artifacts,
            open_questions=body.open_questions,
            next_steps=body.next_steps,
            task_id=body.task_id,
            conversation_id=body.conversation_id,
            provenance=body.provenance,
        )
        staged = packager.stage_package(package) if body.stage else None
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "artifact_package": _interaction_package_payload(package),
        "staged_path": staged.path if staged is not None else None,
        "artifact_id": staged.artifact_id if staged is not None else None,
    }


def _agent_payload(spec) -> dict[str, Any]:
    return {
        "id": str(spec.id),
        "key": spec.key,
        "name": spec.name,
        "domain_key": spec.domain_key,
        "agent_type": spec.agent_type,
        "role_summary": spec.role_summary,
        "role_prompt": spec.role_prompt,
        "memory_profile": spec.memory_profile,
        "model_profile": spec.model_profile,
        "allowed_tools": [_tool_payload(tool) for tool in spec.allowed_tools],
        "is_active": spec.is_active,
        "current_action": spec.current_action,
        "scheduled_actions": spec.scheduled_actions,
    }


def _tool_payload(tool: ToolManifestItem) -> dict[str, Any]:
    return {
        "key": tool.key,
        "name": tool.name,
        "permission": tool.permission,
        "description": tool.description,
        "connection_id": tool.connection_id,
        "auth_type": tool.auth_type,
    }


def _tool_connection_payload(connection) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "domain_key": connection.domain_key,
        "tool_key": connection.tool_key,
        "display_name": connection.display_name,
        "auth_type": connection.auth_type,
        "config": connection.config,
        "is_active": connection.is_active,
    }


def _prompt_package_payload(package) -> dict[str, Any]:
    return {
        "created_at": package.created_at,
        "caller": package.caller,
        "agent": _agent_payload(package.agent),
        "task_instruction": package.task_instruction,
        "global_context": package.global_context,
        "domain_context": package.domain_context,
        "role_prompt": package.role_prompt,
        "user_context": package.user_context,
        "tool_manifest": [_tool_payload(tool) for tool in package.tool_manifest],
        "output_contract": package.output_contract,
        "memory_context": {
            "profile": package.memory_context.request.profile,
            "semantic_status": package.memory_context.semantic_status,
            "included_count": package.memory_context.included_count,
            "dropped_count": package.memory_context.dropped_count,
            "used_chars": package.memory_context.used_chars,
            "sections": [
                {
                    "key": section.key,
                    "label": section.label,
                    "memories": [
                        {
                            "id": str(snippet.memory.id),
                            "title": snippet.memory.title,
                            "scope": snippet.memory.scope,
                            "domain_id": str(snippet.memory.domain_id)
                            if snippet.memory.domain_id
                            else None,
                            "agent_id": str(snippet.memory.agent_id)
                            if snippet.memory.agent_id
                            else None,
                            "memory_type": snippet.memory.memory_type,
                            "excerpt": snippet.excerpt,
                            "score": snippet.score,
                            "provenance": {
                                "source_refs": snippet.provenance.source_refs,
                                "seed_package": snippet.provenance.seed_package,
                                "artifact": snippet.provenance.artifact,
                                "processed_path": snippet.provenance.processed_path,
                            },
                        }
                        for snippet in section.snippets
                    ],
                }
                for section in package.memory_context.sections
            ],
            "rendered_text": package.memory_context.rendered_text,
        },
        "assembled_prompt": package.assembled_prompt,
    }


def _interaction_package_payload(package: InteractionArtifactPackage) -> dict[str, Any]:
    return {
        "schema_version": package.schema_version,
        "package_id": package.package_id,
        "created_at": package.created_at,
        "domain_key": package.domain_key,
        "agent_key": package.agent_key,
        "task_id": package.task_id,
        "conversation_id": package.conversation_id,
        "user_input": package.user_input,
        "maestro_tasking": package.maestro_tasking,
        "agent_output": package.agent_output,
        "tool_calls": package.tool_calls,
        "generated_artifacts": package.generated_artifacts,
        "open_questions": package.open_questions,
        "next_steps": package.next_steps,
        "provenance": package.provenance,
    }
