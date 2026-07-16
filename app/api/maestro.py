from typing import Any
import asyncio
import json
import logging
import uuid
import re
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Conversation, Domain, Message, RoutedItem, RuntimeSetting, Task, Todo
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.db.session import SessionLocal, get_db
from app.llm.client import LLMClientError, OpenAILLMClient
from app.maestro.channel import MAESTRO_CHANNEL_KEY, get_or_create_maestro_channel
from app.maestro.context_assembler import MaestroContextAssembler, maestro_context_payload
from app.maestro.intent_classifier import (
    classify_active_message_with_local_llm,
    resolve_topic_with_local_llm,
    understand_message_with_local_llm,
)
from app.maestro.scheduler import SchedulerService
from app.tools.runtime import ToolExecutionError, ToolExecutionService, tool_result_payload
from app.maestro.orchestrator import (
    MaestroOrchestratorError,
    MaestroOrchestratorService,
    MaestroPlan,
    MaestroRun,
)

router = APIRouter(prefix="/maestro", tags=["maestro"])
logger = logging.getLogger(__name__)


class MaestroPlanBody(BaseModel):
    message: str


class MaestroRespondBody(BaseModel):
    message: str
    active_plan_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


class MaestroRunBody(BaseModel):
    execute_llm: bool = True
    auto_tool_loop: bool = False
    max_tool_iterations: int = Field(default=2, ge=1, le=4)
    conversation_id: uuid.UUID | None = None


class MaestroPlanArchiveBody(BaseModel):
    reason: str | None = None
    conversation_id: uuid.UUID | None = None


class MaestroSessionMessage(BaseModel):
    sender: str
    content: str


class MaestroSessionCloseBody(BaseModel):
    messages: list[MaestroSessionMessage]
    plan_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


class MaestroSessionArchiveBody(BaseModel):
    archived: bool = True


@router.get("/context-bundle")
def build_maestro_context_bundle(
    query_text: str | None = None,
    domain_key: str | None = None,
    max_chars: int = 6500,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    bundle = MaestroContextAssembler(db).build_bundle(
        query_text=query_text,
        domain_key=domain_key,
        max_chars=max_chars,
    )
    return maestro_context_payload(bundle)


class MaestroToolRejectBody(BaseModel):
    reason: str | None = None
    conversation_id: uuid.UUID | None = None


class MaestroToolApproveBody(BaseModel):
    execute_llm: bool = True
    auto_tool_loop: bool = True
    max_tool_iterations: int = Field(default=2, ge=1, le=4)
    conversation_id: uuid.UUID | None = None


@router.post("/plan")
def create_maestro_plan(body: MaestroPlanBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        plan = MaestroOrchestratorService(db).create_plan(body.message)
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"plan": _plan_payload(plan)}


@router.post("/respond")
def respond_to_maestro(
    body: MaestroRespondBody,
    background_tasks: BackgroundTasks,
    x_maestro_async: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if x_maestro_async == "true":
        background_tasks.add_task(_respond_to_maestro_in_background, body)
        return {
            "kind": "pending",
            "classification": "pending",
            "message": "I received that and am working through it now.",
            "plan": None,
            "chat_plan": None,
            "active_plan": None,
            "channel_context": None,
            "conversation": None,
        }
    return _respond_to_maestro_sync(body, db)


def _respond_to_maestro_in_background(body: MaestroRespondBody) -> None:
    """Keep the shared channel responsive while local/model reasoning completes."""
    with SessionLocal() as session:
        try:
            _respond_to_maestro_sync(body, session)
        except Exception:
            logger.exception("Maestro background response failed.")


def _respond_to_maestro_sync(
    body: MaestroRespondBody,
    db: Session,
) -> dict[str, Any]:
    conversation = _get_or_create_maestro_conversation(db, body.conversation_id)
    normalized_message = _normalized_message_for_routing(body.message)
    topic_context = _resolve_topic_context(
        db,
        conversation,
        body.message,
        explicit_active_plan=body.active_plan_id is not None,
    )
    message_metadata = {"topic_id": topic_context.get("topic_id")} if topic_context.get("topic_id") else {}
    user_message = _record_session_message(db, conversation, "user", body.message, metadata=message_metadata)
    planner_message = _message_with_topic_context(
        db,
        conversation,
        body.message,
        topic_context=topic_context,
        current_message_id=user_message.id,
    )
    message_understanding = _understand_message_without_active_plan(body.message)
    planner_message = _message_with_intent_context(planner_message, message_understanding)
    if _is_scheduled_workflow_status_question(body.message):
        response_message = _scheduled_workflow_status_response(db)
        _record_session_message(db, conversation, "maestro", response_message, metadata=message_metadata)
        return {
            "kind": "chat_only",
            "classification": "system_status",
            "message": response_message,
            "plan": None,
            "chat_plan": None,
            "active_plan": None,
            "channel_context": topic_context,
            "conversation": _conversation_payload(db, conversation),
        }
    try:
        service = MaestroOrchestratorService(db)
        active_plan_id = None if topic_context.get("scope") in {"new_topic", "global_system"} else body.active_plan_id
        response_active_plan: MaestroPlan | None = None
        if _is_session_restart_message(normalized_message):
            response_message = _restart_maestro_session_response(
                db,
                service=service,
                conversation=conversation,
                message=body.message,
            )
            restart_context = _current_topic_context(conversation)
            restart_metadata = {"topic_id": restart_context.get("topic_id")} if restart_context.get("topic_id") else {}
            _record_session_message(db, conversation, "maestro", response_message, metadata=restart_metadata)
            return {
                "kind": "chat_only",
                "classification": "restart_session",
                "message": response_message,
                "plan": None,
                "chat_plan": None,
                "active_plan": None,
                "channel_context": restart_context,
                "conversation": _conversation_payload(db, conversation),
            }
        if active_plan_id is None and _is_pure_chat_understanding(message_understanding):
            response_message = _direct_chat_response(db, body.message)
            _record_session_message(db, conversation, "maestro", response_message, metadata=message_metadata)
            return {
                "kind": "chat_only",
                "classification": "direct_chat",
                "message": response_message,
                "plan": None,
                "chat_plan": None,
                "active_plan": None,
                "channel_context": topic_context,
                "conversation": _conversation_payload(db, conversation),
            }
        if active_plan_id is None:
            active_plan_context = None if topic_context.get("scope") == "new_topic" else _resolve_channel_context(db, conversation)
            if active_plan_context is not None and _should_use_plan_context(body.message, active_plan_context):
                active_plan_id = active_plan_context.parent_task_id
        if active_plan_id is not None:
            active_plan = service.get_plan(active_plan_id)
            classification = _classify_active_session_message(body.message, active_plan)
            if classification == "new_workflow":
                plan = service.create_plan(
                    planner_message,
                    conversation_id=conversation.id,
                    topic_id=str(topic_context.get("topic_id")) if topic_context.get("topic_id") else None,
                )
                kind = "routed" if plan.is_routing_only else ("chat_only" if plan.is_chat_only else "planned")
            elif classification == "delete_workflow":
                response_message = _delete_open_workflows_response(
                    db,
                    service=service,
                    conversation=conversation,
                    message=body.message,
                    fallback_plan_id=active_plan_id,
                )
                _record_session_message(db, conversation, "maestro", response_message, metadata=message_metadata)
                return {
                    "kind": "chat_only",
                    "classification": classification,
                    "message": response_message,
                    "plan": None,
                    "chat_plan": None,
                    "active_plan": None,
                    "channel_context": topic_context,
                    "conversation": _conversation_payload(db, conversation),
                }
            elif classification == "side_chat":
                response_message = _side_chat_response(body.message, active_plan)
                _record_session_message(db, conversation, "maestro", response_message, metadata=message_metadata)
                return {
                    "kind": "chat_only",
                    "classification": classification,
                    "message": response_message,
                    "plan": None,
                    "chat_plan": None,
                    "active_plan": _plan_payload(active_plan),
                    "channel_context": topic_context,
                    "conversation": _conversation_payload(db, conversation),
                }
            elif classification == "routed":
                plan = service.create_plan(
                    body.message,
                    conversation_id=conversation.id,
                    topic_id=str(topic_context.get("topic_id")) if topic_context.get("topic_id") else None,
                )
                kind = "routed" if plan.is_routing_only else ("chat_only" if plan.is_chat_only else "planned")
                response_active_plan = active_plan
            else:
                plan = service.refine_plan(
                    active_plan_id,
                    _classified_refinement_message(planner_message, classification, active_plan),
                )
                kind = "routed" if plan.is_routing_only else ("chat_only" if plan.is_chat_only else classification)
        else:
            if _is_workflow_delete_message(body.message.lower().strip()):
                response_message = _delete_open_workflows_response(
                    db,
                    service=service,
                    conversation=conversation,
                    message=body.message,
                    fallback_plan_id=None,
                )
                _record_session_message(db, conversation, "maestro", response_message, metadata=message_metadata)
                return {
                    "kind": "chat_only",
                    "classification": "delete_workflow",
                    "message": response_message,
                    "plan": None,
                    "chat_plan": None,
                    "active_plan": None,
                    "channel_context": topic_context,
                    "conversation": _conversation_payload(db, conversation),
                }
            plan = service.create_plan(
                planner_message,
                conversation_id=conversation.id,
                topic_id=str(topic_context.get("topic_id")) if topic_context.get("topic_id") else None,
            )
            kind = "routed" if plan.is_routing_only else ("chat_only" if plan.is_chat_only else "planned")
            classification = kind
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response_message = _maestro_response_text(
        plan,
        refined=kind in {"refined", "rfi_answered", "routed"},
        topic_context=topic_context,
    )
    _record_session_message(db, conversation, "maestro", response_message, metadata=message_metadata)
    return {
        "kind": kind,
        "classification": classification,
        "message": response_message,
        "plan": None if plan.is_chat_only else _plan_payload(plan),
        "chat_plan": _plan_payload(plan) if plan.is_chat_only else None,
        "active_plan": _plan_payload(response_active_plan) if response_active_plan is not None else None,
        "channel_context": topic_context,
        "conversation": _conversation_payload(db, conversation),
    }


@router.websocket("/channel/ws")
async def maestro_channel_ws(
    websocket: WebSocket,
    db: Session = Depends(get_db),
) -> None:
    await websocket.accept()
    last_signature: tuple[str, str | None, int] | None = None
    try:
        while True:
            db.expire_all()
            conversation = get_or_create_maestro_channel(db)
            payload = _conversation_payload(db, conversation)
            signature = (
                payload["id"],
                payload["updated_at"],
                int(payload["message_count"]),
            )
            if signature != last_signature:
                await websocket.send_json({"type": "conversation", "conversation": payload})
                last_signature = signature
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return


@router.post("/plans/{plan_id}/refine")
def refine_maestro_plan(
    plan_id: uuid.UUID,
    body: MaestroPlanBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        plan = MaestroOrchestratorService(db).refine_plan(plan_id, body.message)
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"plan": _plan_payload(plan)}


@router.get("/plans/{plan_id}")
def get_maestro_plan(plan_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        plan = MaestroOrchestratorService(db).get_plan(plan_id)
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"plan": _plan_payload(plan)}


@router.post("/plans/{plan_id}/run")
def run_maestro_plan(
    plan_id: uuid.UUID,
    body: MaestroRunBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conversation = _get_or_create_maestro_conversation(db, body.conversation_id)
    try:
        run = MaestroOrchestratorService(db).enqueue_plan(plan_id)
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_session_message(
        db,
        conversation,
        "maestro",
        run.chat_summary,
    )
    return {"run": _run_payload(run)}


@router.post("/plans/{plan_id}/archive")
def archive_maestro_plan(
    plan_id: uuid.UUID,
    body: MaestroPlanArchiveBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    reason = body.reason or "Candidate workflow cleared from Maestro chat."
    try:
        plan = MaestroOrchestratorService(db).archive_plan(plan_id, reason=reason)
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if body.conversation_id is not None:
        conversation = _get_or_create_maestro_conversation(db, body.conversation_id)
        _record_session_message(db, conversation, "maestro", "I cleared that candidate workflow.")
    return {"plan": _plan_payload(plan)}


@router.post("/tool-calls/{tool_call_id}/approve")
def approve_maestro_tool_call(
    tool_call_id: uuid.UUID,
    body: MaestroToolApproveBody | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        options = body or MaestroToolApproveBody()
        result, run = MaestroOrchestratorService(db).approve_tool_call_and_resume(
            tool_call_id,
            execute_llm=options.execute_llm,
            auto_tool_loop=options.auto_tool_loop,
            max_tool_iterations=options.max_tool_iterations,
        )
    except (ToolExecutionError, MaestroOrchestratorError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result_payload = tool_result_payload(result)
    message = _tool_approval_message(result_payload, approved=True)
    if run is not None:
        message = f"{message}\n\n{run.chat_summary}"
    conversation = _get_or_create_maestro_conversation(db, options.conversation_id)
    _record_session_message(db, conversation, "maestro", message)
    return {
        "tool_call": result_payload,
        "message": message,
        "run": _run_payload(run) if run is not None else None,
    }


@router.post("/tool-calls/{tool_call_id}/reject")
def reject_maestro_tool_call(
    tool_call_id: uuid.UUID,
    body: MaestroToolRejectBody | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = ToolExecutionService(db).reject_tool_call(
            tool_call_id,
            reason=body.reason if body else None,
        )
    except ToolExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    message = _tool_approval_message(tool_result_payload(result), approved=False)
    conversation = _get_or_create_maestro_conversation(
        db,
        body.conversation_id if body else None,
    )
    _record_session_message(db, conversation, "maestro", message)
    return {
        "tool_call": tool_result_payload(result),
        "message": message,
    }


@router.post("/sessions/close")
def close_maestro_session(
    body: MaestroSessionCloseBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        messages = [message.model_dump() for message in body.messages]
        if not messages and body.conversation_id:
            conversation = db.get(Conversation, body.conversation_id)
            if conversation is not None:
                messages = [
                    {"sender": message.sender_type, "content": message.content}
                    for message in _conversation_messages(db, conversation.id)
                ]
        staged_artifact_path = MaestroOrchestratorService(db).close_session(
            messages=messages,
            plan_id=body.plan_id,
        )
        _clear_active_maestro_conversation(db, body.conversation_id)
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"staged_artifact_path": staged_artifact_path}


@router.post("/sessions/start")
def start_maestro_session(db: Session = Depends(get_db)) -> dict[str, Any]:
    conversation = get_or_create_maestro_channel(db)
    _start_new_topic(db, conversation, title="New Maestro topic")
    _set_active_maestro_conversation(db, conversation.id)
    return {"conversation": _conversation_payload(db, conversation)}


@router.get("/sessions/active")
def get_active_maestro_session(db: Session = Depends(get_db)) -> dict[str, Any]:
    conversation = get_or_create_maestro_channel(db)
    _set_active_maestro_conversation(db, conversation.id)
    return {"conversation": _conversation_payload(db, conversation)}


@router.get("/sessions")
def list_maestro_sessions(
    include_archived: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    maestro_domain = DomainRepository(db).get_by_key("maestro-development")
    query = select(Conversation).order_by(Conversation.updated_at.desc()).limit(25)
    if maestro_domain is not None:
        query = query.where(Conversation.domain_id == maestro_domain.id)
    conversations = db.scalars(query).all()
    if not include_archived:
        conversations = [
            conversation
            for conversation in conversations
            if not bool((conversation.metadata_ or {}).get("archived"))
        ]
    return {"sessions": [_conversation_payload(db, conversation, include_messages=False) for conversation in conversations]}


@router.get("/sessions/{conversation_id}")
def get_maestro_session(conversation_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown Maestro session.")
    return {"conversation": _conversation_payload(db, conversation)}


@router.patch("/sessions/{conversation_id}/archive")
def archive_maestro_session(
    conversation_id: uuid.UUID,
    body: MaestroSessionArchiveBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown Maestro session.")
    metadata = dict(conversation.metadata_ or {})
    metadata["archived"] = body.archived
    metadata["archived_at"] = datetime.now(UTC).isoformat() if body.archived else None
    conversation.metadata_ = metadata
    conversation.updated_at = datetime.now(UTC)
    active = _active_maestro_conversation(db)
    if active is not None and active.id == conversation.id and body.archived:
        _clear_active_maestro_conversation(db, conversation.id)
    db.commit()
    db.refresh(conversation)
    return {"conversation": _conversation_payload(db, conversation)}


_ACTIVE_MAESTRO_SESSION_KEY = "active_maestro_conversation"


def _get_or_create_maestro_conversation(
    db: Session,
    conversation_id: uuid.UUID | None,
) -> Conversation:
    if conversation_id is not None:
        conversation = db.get(Conversation, conversation_id)
        if conversation is not None and (conversation.metadata_ or {}).get("channel") == "primary":
            _set_active_maestro_conversation(db, conversation.id)
            return conversation
    conversation = get_or_create_maestro_channel(db)
    _set_active_maestro_conversation(db, conversation.id)
    return conversation


def _create_maestro_conversation(db: Session) -> Conversation:
    seed_default_domains(db)
    maestro_domain = DomainRepository(db).get_by_key("maestro-development")
    conversation = Conversation(
        domain_id=maestro_domain.id if maestro_domain else None,
        title="Maestro session",
        metadata_={"archived": False},
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _active_maestro_conversation(db: Session) -> Conversation | None:
    setting = db.get(RuntimeSetting, MAESTRO_CHANNEL_KEY) or db.get(
        RuntimeSetting,
        _ACTIVE_MAESTRO_SESSION_KEY,
    )
    value = setting.value if setting is not None else {}
    conversation_id = value.get("conversation_id") if isinstance(value, dict) else None
    if not conversation_id:
        return None
    try:
        return db.get(Conversation, uuid.UUID(str(conversation_id)))
    except (TypeError, ValueError):
        return None


def _set_active_maestro_conversation(db: Session, conversation_id: uuid.UUID) -> None:
    setting = db.get(RuntimeSetting, _ACTIVE_MAESTRO_SESSION_KEY)
    if setting is None:
        setting = RuntimeSetting(key=_ACTIVE_MAESTRO_SESSION_KEY, value={})
        db.add(setting)
    setting.value = {"conversation_id": str(conversation_id)}
    db.commit()


def _clear_active_maestro_conversation(
    db: Session,
    conversation_id: uuid.UUID | None,
) -> None:
    setting = db.get(RuntimeSetting, _ACTIVE_MAESTRO_SESSION_KEY)
    if setting is None:
        return
    active_id = (setting.value or {}).get("conversation_id")
    if conversation_id is None or str(conversation_id) == str(active_id):
        setting.value = {}
        db.commit()


def _resolve_topic_context(
    db: Session,
    conversation: Conversation,
    message: str,
    *,
    explicit_active_plan: bool = False,
) -> dict[str, Any]:
    metadata = dict(conversation.metadata_ or {})
    active_topic = metadata.get("active_topic")
    if not isinstance(active_topic, dict):
        active_topic = None
    rule_scope, reason = _classify_topic_scope(message, active_topic, explicit_active_plan)
    message_scope = rule_scope
    recent_topics = _recent_topic_summaries(metadata)
    llm_resolution = None
    if rule_scope in {"needs_resolution", "fallback_active_topic", "fallback_new_topic"}:
        llm_resolution = resolve_topic_with_local_llm(
            message=message,
            active_topic=_topic_summary(active_topic) if active_topic else None,
            recent_topics=recent_topics,
        )
        if llm_resolution is not None:
            message_scope = llm_resolution.scope
            reason = llm_resolution.reason
        else:
            if rule_scope == "fallback_active_topic":
                message_scope = "active_topic"
            elif rule_scope == "fallback_new_topic":
                message_scope = "new_topic"
            else:
                message_scope = "active_topic" if active_topic is not None else "new_topic"
            reason = "Topic resolver unavailable or low confidence; used deterministic fallback."
    if message_scope == "existing_topic":
        selected_topic = _find_topic_by_id(
            [topic for topic in [active_topic, *recent_topics] if isinstance(topic, dict)],
            llm_resolution.topic_id if llm_resolution else None,
        )
        if selected_topic is not None:
            active_topic = _activate_existing_topic(db, conversation, selected_topic)
            return {
                "scope": "existing_topic",
                "topic_id": active_topic["id"],
                "topic_title": active_topic.get("title"),
                "started_new_topic": False,
                "reason": reason,
            }
        message_scope = "active_topic" if active_topic is not None else "new_topic"
        reason = "Topic resolver selected an unavailable topic; used deterministic fallback."
    started_new_topic = message_scope == "new_topic"
    if active_topic is None or started_new_topic:
        title = (
            (llm_resolution.suggested_title or "").strip()[:72]
            if llm_resolution is not None and llm_resolution.suggested_title
            else _topic_title(message)
        )
        active_topic = _start_new_topic(db, conversation, title=title)
        return {
            "scope": "new_topic",
            "topic_id": active_topic["id"],
            "topic_title": active_topic["title"],
            "started_new_topic": True,
            "reason": reason,
        }

    active_topic = dict(active_topic)
    active_topic["updated_at"] = datetime.now(UTC).isoformat()
    metadata["active_topic"] = active_topic
    conversation.metadata_ = metadata
    conversation.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(conversation)
    return {
        "scope": message_scope,
        "topic_id": active_topic.get("id"),
        "topic_title": active_topic.get("title"),
        "started_new_topic": False,
        "reason": reason,
    }


def _start_new_topic(db: Session, conversation: Conversation, *, title: str) -> dict[str, Any]:
    metadata = dict(conversation.metadata_ or {})
    previous_topic = _summarize_topic_for_storage(db, conversation, metadata.get("active_topic"))
    if isinstance(previous_topic, dict):
        previous_topic = {
            **previous_topic,
            **_stage_topic_artifact_if_needed(db, conversation, previous_topic),
        }
    topics = metadata.get("topics") if isinstance(metadata.get("topics"), list) else []
    if isinstance(previous_topic, dict):
        topics = [
            topic
            for topic in topics
            if isinstance(topic, dict) and topic.get("id") != previous_topic.get("id")
        ]
        topics.insert(0, previous_topic)
    active_topic = {
        "id": str(uuid.uuid4()),
        "title": title,
        "started_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    metadata["topics"] = topics[:24]
    metadata["active_topic"] = active_topic
    conversation.metadata_ = metadata
    conversation.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(conversation)
    return active_topic


def _stage_topic_artifact_if_needed(
    db: Session,
    conversation: Conversation,
    topic: dict[str, Any],
) -> dict[str, Any]:
    if topic.get("staged_artifact_path"):
        return {}
    topic_id = str(topic.get("id") or "")
    if not topic_id:
        return {}
    messages = [
        {"sender": message.sender_type, "content": message.content}
        for message in _conversation_messages(db, conversation.id)
        if (message.metadata_ or {}).get("topic_id") == topic_id
    ]
    if not messages:
        return {}
    staged_path = MaestroOrchestratorService(db).close_session(messages=messages)
    return {
        "staged_artifact_path": staged_path,
        "staged_for_curation_at": datetime.now(UTC).isoformat(),
    } if staged_path else {}


def _activate_existing_topic(
    db: Session,
    conversation: Conversation,
    selected_topic: dict[str, Any],
) -> dict[str, Any]:
    metadata = dict(conversation.metadata_ or {})
    current_topic = _summarize_topic_for_storage(db, conversation, metadata.get("active_topic"))
    topics = metadata.get("topics") if isinstance(metadata.get("topics"), list) else []
    selected_id = str(selected_topic.get("id"))
    remaining_topics = [
        topic
        for topic in topics
        if isinstance(topic, dict) and str(topic.get("id")) != selected_id
    ]
    if isinstance(current_topic, dict) and str(current_topic.get("id")) != selected_id:
        remaining_topics.insert(0, current_topic)
    active_topic = {
        **selected_topic,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    metadata["active_topic"] = active_topic
    metadata["topics"] = remaining_topics[:24]
    conversation.metadata_ = metadata
    conversation.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(conversation)
    return active_topic


def _recent_topic_summaries(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    topics = metadata.get("topics") if isinstance(metadata.get("topics"), list) else []
    summaries: list[dict[str, Any]] = []
    for topic in topics:
        if isinstance(topic, dict) and topic.get("id"):
            summaries.append(_topic_summary(topic))
    return summaries[:8]


def _topic_summary(topic: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(topic, dict):
        return {}
    return {
        "id": topic.get("id"),
        "title": topic.get("title"),
        "summary": topic.get("summary") or topic.get("title"),
        "keywords": topic.get("keywords") or [],
        "updated_at": topic.get("updated_at"),
    }


def _find_topic_by_id(topics: list[dict[str, Any]], topic_id: str | None) -> dict[str, Any] | None:
    if not topic_id:
        return None
    for topic in topics:
        if str(topic.get("id")) == str(topic_id):
            return topic
    return None


def _summarize_topic_for_storage(
    db: Session,
    conversation: Conversation,
    topic: Any,
) -> dict[str, Any] | None:
    if not isinstance(topic, dict) or not topic.get("id"):
        return None
    topic_id = str(topic["id"])
    messages = [
        message.content
        for message in _conversation_messages(db, conversation.id)
        if (message.metadata_ or {}).get("topic_id") == topic_id
    ]
    summary = topic.get("summary") or _compact_topic_summary(messages, str(topic.get("title") or ""))
    return {
        **topic,
        "summary": summary,
        "keywords": _topic_keywords(f"{topic.get('title', '')} {summary}"),
    }


def _compact_topic_summary(messages: list[str], fallback_title: str) -> str:
    if not messages:
        return fallback_title[:180]
    text = " ".join(" ".join(message.split()) for message in messages[:6])
    return text[:220]


def _topic_keywords(text: str) -> list[str]:
    stopwords = {
        "about",
        "again",
        "and",
        "for",
        "from",
        "have",
        "maestro",
        "that",
        "the",
        "this",
        "with",
        "would",
    }
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    keywords: list[str] = []
    for word in words:
        if word in stopwords or word in keywords:
            continue
        keywords.append(word)
        if len(keywords) >= 12:
            break
    return keywords


def _active_topic(conversation: Conversation) -> dict[str, Any] | None:
    active_topic = (conversation.metadata_ or {}).get("active_topic")
    return active_topic if isinstance(active_topic, dict) else None


def _classify_topic_scope(
    message: str,
    active_topic: dict[str, Any] | None,
    explicit_active_plan: bool,
) -> tuple[str, str]:
    lowered = _normalized_message_for_routing(message)
    if _is_session_restart_message(lowered):
        return "global_system", "System-level session reset command."
    if _is_scheduled_workflow_status_question(message) or _is_workflow_delete_message(lowered):
        return "global_system", "System-level queue/workflow command."
    if active_topic is None:
        return "new_topic", "No active working topic exists."
    followup_markers = (
        "this new feature",
        "this feature",
        "that feature",
        "the feature",
        "this concept",
        "that concept",
        "the concept",
        "this design",
        "that design",
        "the design",
        "this pattern",
        "that pattern",
        "how will this",
        "how would this",
        "what's the best pattern",
        "what is the best pattern",
        "add it as an issue",
        "this looks good",
    )
    if any(marker in lowered for marker in followup_markers):
        return "fallback_active_topic", "Message appears to refer back to the active working topic."
    active_modification_markers = (
        "existing feature",
        "current feature",
        "active feature",
        "this feature",
        "that feature",
        "the feature",
        "existing plan",
        "current plan",
        "active plan",
    )
    new_topic_markers = (
        "new topic",
        "switching gears",
        "separate thought",
        "different topic",
        "fresh topic",
        "start a new conversation",
        "brainstorm a new feature",
        "plan a new feature",
        "lets plan a new feature",
        "let's plan a new feature",
        "begin discussing a new feature",
        "discussing a new feature",
        "discuss a new feature",
        "new feature",
        "new feature for",
        "new feature in",
        "brainstorm a new agent",
        "new agent design",
        "design a new agent",
        "plan a new agent",
        "build a new agent",
        "create a new agent",
        "new agent for",
        "new agent in",
        "new praxis agent",
    )
    if any(marker in lowered for marker in new_topic_markers):
        if any(marker in lowered for marker in active_modification_markers):
            return "needs_resolution", "Message mentions new work but may refine the active topic."
        if any(marker in lowered for marker in ("new feature", "new agent", "new conversation", "new topic")):
            return "new_topic", "Message explicitly introduces a new working topic."
        return "fallback_new_topic", "Message appears to introduce a new working topic."
    if explicit_active_plan:
        return "needs_resolution", "The UI supplied an active plan, but the message still needs topic resolution."
    question_prefixes = ("what ", "why ", "how ", "who ", "when ", "where ", "can you explain")
    if lowered.endswith("?") or lowered.startswith(question_prefixes):
        return "needs_resolution", "Question needs topic resolver to choose active topic or global system."
    return "needs_resolution", "Ambiguous message needs topic resolver."


def _normalized_message_for_routing(message: str) -> str:
    lowered = " ".join(message.lower().strip().split())
    if lowered.startswith("iwant "):
        lowered = "i want " + lowered[len("iwant "):]
    if lowered.startswith("ilets "):
        lowered = "i lets " + lowered[len("ilets "):]
    return lowered


def _is_session_restart_message(lowered_message: str) -> bool:
    return any(
        token in lowered_message
        for token in (
            "restart session",
            "reset session",
            "start fresh session",
            "fresh session",
            "new clean session",
            "clean slate",
            "clear current work",
            "clear hung work",
            "clear stuck work",
            "clear all current work",
            "reset maestro",
        )
    )


def _current_topic_context(conversation: Conversation) -> dict[str, Any]:
    active_topic = _active_topic(conversation)
    return {
        "scope": "active_topic" if active_topic else "new_topic",
        "topic_id": active_topic.get("id") if active_topic else None,
        "topic_title": active_topic.get("title") if active_topic else None,
        "started_new_topic": False,
        "reason": "Current Maestro channel topic.",
    }


def _topic_title(message: str) -> str:
    cleaned = " ".join(message.strip().split())
    if cleaned.lower().startswith("iwant "):
        cleaned = "I want " + cleaned[len("Iwant "):]
    if not cleaned:
        return "Maestro topic"
    prefixes = (
        "hey maestro ",
        "maestro ",
        "i want to ",
        "let's ",
        "lets ",
    )
    lowered = cleaned.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    return cleaned[:72]


def _record_session_message(
    db: Session,
    conversation: Conversation,
    sender: str,
    content: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        sender_type="user" if sender == "user" else "maestro",
        content=content,
        metadata_=metadata or {},
    )
    db.add(message)
    if sender == "user" and (not conversation.title or conversation.title == "Maestro session"):
        conversation.title = content.strip()[:72] or "Maestro session"
    conversation.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(message)
    db.refresh(conversation)
    return message


def _conversation_messages(db: Session, conversation_id: uuid.UUID) -> list[Message]:
    return list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        ).all()
    )


def _message_with_topic_context(
    db: Session,
    conversation: Conversation,
    message: str,
    *,
    topic_context: dict[str, Any],
    current_message_id: uuid.UUID,
) -> str:
    if topic_context.get("scope") not in {"active_topic", "existing_topic"}:
        return message
    topic_id = topic_context.get("topic_id")
    if not topic_id:
        return message
    previous_messages = [
        prior
        for prior in _conversation_messages(db, conversation.id)
        if prior.id != current_message_id and (prior.metadata_ or {}).get("topic_id") == topic_id
    ]
    if not previous_messages:
        return message
    turns = previous_messages[-8:]
    rendered_turns = "\n".join(
        f"{'Chris' if turn.sender_type == 'user' else 'Maestro'}: {turn.content}"
        for turn in turns
    )
    return (
        "<latest_chris_message>\n"
        f"{message}\n"
        "</latest_chris_message>\n\n"
        "<maestro_hidden_context purpose=\"topic_continuity\" do_not_copy=\"true\">\n"
        "Use this active Maestro topic context only to interpret references like this concept, "
        "this feature, it, that, or we. Do not copy this context into user-facing responses, "
        "work item titles, descriptions, RFIs, or routed objects.\n\n"
        f"Active topic: {topic_context.get('topic_title') or 'Maestro topic'}\n"
        f"Previous topic turns:\n{rendered_turns}\n"
        "</maestro_hidden_context>"
    )


def _message_with_intent_context(message: str, understanding: Any | None) -> str:
    if understanding is None:
        return message
    return (
        f"{message}\n\n"
        "<maestro_hidden_context purpose=\"message_intent\" do_not_copy=\"true\">\n"
        "Maestro message classifier output. Use this as routing guidance only; do not expose it "
        "to Chris verbatim and do not copy it into work item titles, descriptions, RFIs, or routed "
        "objects. The classifier identifies message spans and high-level next steps, but the "
        "planner still owns task decomposition, agent/tool selection, dependencies, and execution "
        "structure.\n"
        f"{json.dumps(understanding.model_dump(), indent=2, default=str)}\n\n"
        "</maestro_hidden_context>"
    )


def _conversation_payload(
    db: Session,
    conversation: Conversation,
    *,
    include_messages: bool = True,
) -> dict[str, Any]:
    all_messages = _conversation_messages(db, conversation.id)
    active_topic = _active_topic(conversation)
    active_topic_id = active_topic.get("id") if isinstance(active_topic, dict) else None
    messages = all_messages if include_messages else []
    if include_messages and active_topic_id:
        messages = [
            message
            for message in all_messages
            if (message.metadata_ or {}).get("topic_id") == active_topic_id
        ]
    message_count = len(messages) if include_messages else len(all_messages)
    active_topic = _active_topic(conversation)
    plan = _latest_conversation_plan(
        db,
        conversation.id,
        topic_id=str(active_topic.get("id")) if active_topic and active_topic.get("id") else None,
    )
    return {
        "id": str(conversation.id),
        "title": conversation.title or "Maestro session",
        "active_topic": active_topic,
        "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
        "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else None,
        "archived": bool((conversation.metadata_ or {}).get("archived")),
        "archived_at": (conversation.metadata_ or {}).get("archived_at"),
        "message_count": message_count,
        "messages": [
            {
                "id": str(message.id),
                "sender": "user" if message.sender_type == "user" else "maestro",
                "content": message.content,
                "created_at": message.created_at.isoformat() if message.created_at else None,
                "metadata": message.metadata_ or {},
            }
            for message in messages
        ],
        "active_plan": _plan_payload(plan) if plan is not None else None,
    }


def _latest_conversation_plan(
    db: Session,
    conversation_id: uuid.UUID,
    *,
    topic_id: str | None = None,
) -> MaestroPlan | None:
    active_statuses = {"proposed", "queued", "ready", "running", "blocked", "failed"}
    filters = [
        Task.conversation_id == conversation_id,
        Task.workflow_key == "maestro.generic",
        Task.status.in_(active_statuses),
    ]
    if topic_id:
        filters.append(Task.input_payload["topic_id"].as_string() == topic_id)
    tasks = db.scalars(
        select(Task)
        .where(*filters)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(20)
    ).all()
    for task in tasks:
        try:
            plan = MaestroOrchestratorService(db).get_plan(task.id)
        except MaestroOrchestratorError:
            continue
        if not plan.is_chat_only:
            return plan
    return None


def _resolve_channel_context(db: Session, conversation: Conversation) -> MaestroPlan | None:
    active_topic = _active_topic(conversation)
    topic_id = str(active_topic.get("id")) if active_topic and active_topic.get("id") else None
    plan = _latest_conversation_plan(db, conversation.id, topic_id=topic_id)
    if plan is not None:
        return plan
    active = _active_maestro_conversation(db)
    if active is not None and active.id != conversation.id:
        active_topic = _active_topic(active)
        active_topic_id = (
            str(active_topic.get("id")) if active_topic and active_topic.get("id") else None
        )
        return _latest_conversation_plan(db, active.id, topic_id=active_topic_id)
    return None


def _open_plan_ids_for_cleanup(db: Session, conversation_id: uuid.UUID) -> list[str]:
    open_statuses = {"proposed", "queued", "ready", "running", "blocked", "failed", "scheduled"}
    return [
        str(task.id)
        for task in db.scalars(
            select(Task)
            .where(
                Task.conversation_id == conversation_id,
                Task.workflow_key == "maestro.generic",
                Task.status.in_(open_statuses),
            )
            .order_by(Task.created_at.desc(), Task.id.desc())
        ).all()
    ]


def _delete_open_workflows_response(
    db: Session,
    *,
    service: MaestroOrchestratorService,
    conversation: Conversation,
    message: str,
    fallback_plan_id: str | uuid.UUID | None,
) -> str:
    reason = f"Workflow archived from Maestro chat. Request: {message}"
    target_plan_ids = _open_plan_ids_for_cleanup(db, conversation.id)
    archived_count = service.archive_open_plans_for_conversation(
        conversation.id,
        reason=reason,
    )
    if archived_count == 0 and fallback_plan_id is not None:
        service.archive_plan(fallback_plan_id, reason=reason)
        archived_count = 1
        target_plan_ids.append(str(fallback_plan_id))
    target_plan_ids = _expanded_task_ids_for_cleanup(db, target_plan_ids)
    routed_archived_count = _archive_routed_items_for_cleanup(
        db,
        task_ids=target_plan_ids,
        command_message=message,
        reason=reason,
    )
    if archived_count == 0:
        response_message = "There was no open workflow for me to archive."
    else:
        response_message = (
            f"Done. I archived {archived_count} open workflow"
            f"{'' if archived_count == 1 else 's'} and removed them from the active queue."
        )
    if routed_archived_count:
        response_message += (
            f" I also archived {routed_archived_count} routed item"
            f"{'' if routed_archived_count == 1 else 's'} tied to that cleanup."
        )
    return response_message


def _restart_maestro_session_response(
    db: Session,
    *,
    service: MaestroOrchestratorService,
    conversation: Conversation,
    message: str,
) -> str:
    workflow_message = _delete_open_workflows_response(
        db,
        service=service,
        conversation=conversation,
        message=message,
        fallback_plan_id=None,
    )
    active_topic = _start_new_topic(db, conversation, title="Fresh Maestro session")
    return (
        f"{workflow_message} I also started a fresh session topic "
        f"(`{active_topic['title']}`), so the chat window is clean and future messages will not "
        "inherit the previous topic or stuck workflow context."
    )


def _expanded_task_ids_for_cleanup(db: Session, task_ids: list[str]) -> list[str]:
    expanded = {str(task_id) for task_id in task_ids if task_id}
    changed = True
    while changed:
        changed = False
        tasks = [
            db.get(Task, uuid.UUID(task_id))
            for task_id in list(expanded)
            if _looks_like_uuid(task_id)
        ]
        for task in tasks:
            if task is None or not isinstance(task.input_payload, dict):
                continue
            refined_from_plan_id = task.input_payload.get("refined_from_plan_id")
            if refined_from_plan_id:
                previous = db.scalar(
                    select(Task).where(
                        Task.workflow_key == "maestro.generic",
                        Task.input_payload["plan_id"].as_string() == str(refined_from_plan_id),
                    )
                )
                if previous is not None and str(previous.id) not in expanded:
                    expanded.add(str(previous.id))
                    changed = True
    return list(expanded)


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _archive_routed_items_for_cleanup(
    db: Session,
    *,
    task_ids: list[str],
    command_message: str,
    reason: str,
) -> int:
    task_id_set = {str(task_id) for task_id in task_ids if task_id}
    cleanup_tokens = ("clear", "clean slate", "blocked workflow", "archive the routed")
    routed_items = db.scalars(
        select(RoutedItem).where(RoutedItem.status.notin_(["archived", "done"]))
    ).all()
    archived_count = 0
    for item in routed_items:
        source_task_ids = {
            str(ref.get("task_id"))
            for ref in (item.source_refs or [])
            if isinstance(ref, dict) and ref.get("task_id")
        }
        item_text = f"{item.title} {item.content}".lower()
        is_cleanup_residue = all(token in command_message.lower() for token in ("clear", "workflow")) and any(
            token in item_text for token in cleanup_tokens
        )
        if not (source_task_ids & task_id_set or is_cleanup_residue):
            continue
        item.status = "archived"
        item.metadata_ = {
            **(item.metadata_ or {}),
            "archived_by": "maestro_cleanup_command",
            "archive_reason": reason,
            "archived_at": datetime.now(UTC).isoformat(),
        }
        _archive_promoted_todo_for_routed_item(db, item, reason=reason)
        archived_count += 1
    if archived_count:
        db.commit()
    return archived_count


def _archive_promoted_todo_for_routed_item(db: Session, item: RoutedItem, *, reason: str) -> None:
    metadata = item.metadata_ or {}
    if metadata.get("canonical_object_type") != "todo" or not metadata.get("canonical_object_id"):
        return
    try:
        todo_id = uuid.UUID(str(metadata["canonical_object_id"]))
    except (TypeError, ValueError):
        return
    todo = db.get(Todo, todo_id)
    if todo is None or todo.status == "archived":
        return
    todo.status = "archived"
    todo.metadata_ = {
        **(todo.metadata_ or {}),
        "archived_by": "maestro_cleanup_command",
        "archive_reason": reason,
        "archived_at": datetime.now(UTC).isoformat(),
    }


def _plan_payload(plan: MaestroPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "parent_task_id": plan.parent_task_id,
        "status": plan.status,
        "user_input": plan.user_input,
        "summary": plan.summary,
        "execution_mode": plan.execution_mode,
        "planner_mode": plan.planner_mode,
        "work_items": [work_item.__dict__ for work_item in plan.work_items],
        "intents": [intent.__dict__ for intent in plan.intents],
        "subtasks": [subtask.__dict__ for subtask in plan.subtasks],
        "execution_stages": plan.execution_stages,
        "workflow_graph": plan.workflow_graph,
        "is_chat_only": plan.is_chat_only,
        "is_routing_only": plan.is_routing_only,
        "selected_agents": plan.selected_agents,
        "registry_snapshot": plan.registry_snapshot,
        "approval_required": plan.approval_required,
        "scheduler": plan.scheduler,
        "created_at": plan.created_at,
        "direct_response": plan.direct_response,
        "planner_notes": plan.planner_notes,
    }


def _maestro_response_text(
    plan: MaestroPlan,
    *,
    refined: bool,
    topic_context: dict[str, Any] | None = None,
) -> str:
    topic_prefix = ""
    if (
        topic_context
        and topic_context.get("started_new_topic")
        and topic_context.get("reason") != "No active working topic exists."
    ):
        topic_prefix = "I started a fresh topic for this so we can keep the discussion clean. "
    if plan.is_chat_only:
        response = plan.direct_response or plan.summary or "I can handle that directly here."
        return f"{topic_prefix}{response}".strip()
    schedule_candidate = plan.scheduler.get("schedule_candidate")
    if isinstance(schedule_candidate, dict):
        if plan.direct_response:
            return (
                f"{topic_prefix}{plan.direct_response}\n\n"
                f"I also prepared a {schedule_candidate.get('trigger_type', 'scheduled')} workflow "
                "for this. Review "
                "it here; when you run the plan I will save the schedule in Queue instead of "
                "executing it immediately."
            ).strip()
        trigger_type = schedule_candidate.get("trigger_type", "scheduled")
        return (
            f"{topic_prefix}I prepared a {trigger_type} workflow for this. Review it here; "
            "when you run the plan I will save "
            "the schedule in Queue instead of executing it immediately."
        ).strip()
    blocking_items = [
        item for item in plan.work_items if item.needs_user_input and item.blocks_execution
    ]
    if blocking_items:
        question_text = _rfi_question_text(blocking_items)
        return (
            topic_prefix +
            f"{question_text} Answer here in chat and I will use that to refine this active plan."
        ).strip()
    non_blocking_questions = [
        item for item in plan.work_items if item.needs_user_input and not item.blocks_execution
    ]
    question_text = ""
    if non_blocking_questions:
        suffix = "" if len(non_blocking_questions) == 1 else "s"
        question_text = (
            f" I also found {len(non_blocking_questions)} non-blocking question{suffix} "
            "that can be answered later."
        )
    if plan.direct_response:
        workflow_note = _planned_work_message(plan, refined=refined, question_text=question_text)
        return f"{topic_prefix}{plan.direct_response}\n\n{workflow_note}".strip()
    verb = "refined" if refined else "drafted"
    if plan.summary:
        return f"{topic_prefix}{_planned_work_message(plan, refined=refined, question_text=question_text)}".strip()
    return f"{topic_prefix}I {verb} a plan for this. {_review_instruction(question_text)}".strip()


def _planned_work_message(plan: MaestroPlan, *, refined: bool, question_text: str) -> str:
    verb = "updated" if refined else "prepared"
    focus = _planned_work_focus(plan)
    if focus:
        return (
            f"I {verb} a plan to help with this. The work is focused on {focus}. "
            f"{_review_instruction(question_text)}"
        )
    return f"I {verb} a plan to help with this. {_review_instruction(question_text)}"


def _planned_work_focus(plan: MaestroPlan) -> str:
    executable_items = [
        item.title.strip().rstrip(".")
        for item in plan.work_items
        if item.type == "workflow_task" and item.title.strip()
    ]
    if not executable_items:
        executable_items = [
            item.title.strip().rstrip(".")
            for item in plan.work_items
            if item.title.strip()
        ]
    if not executable_items:
        return ""
    if len(executable_items) == 1:
        return executable_items[0]
    if len(executable_items) == 2:
        return f"{executable_items[0]} and {executable_items[1]}"
    shown = executable_items[:3]
    return f"{', '.join(shown[:-1])}, and {shown[-1]}"


def _review_instruction(question_text: str) -> str:
    if question_text:
        return f"Review it here when you are ready;{question_text}"
    return "Review it here when you are ready, and I will run it after you approve."


def _rfi_question_text(blocking_items: list[Any]) -> str:
    if len(blocking_items) == 1:
        item = blocking_items[0]
        detail = _strip_hidden_context(item.description.strip() if item.description else item.title)
        return f"I need one answer before this can run: {detail}"
    questions = [
        f"{index}. {_strip_hidden_context(item.description.strip() if item.description else item.title)}"
        for index, item in enumerate(blocking_items, start=1)
    ]
    return "I need these answers before this can run: " + " ".join(questions)


def _strip_hidden_context(value: str) -> str:
    stripped = re.sub(
        r"<maestro_hidden_context\b[^>]*>.*?</maestro_hidden_context>",
        "",
        value,
        flags=re.DOTALL | re.IGNORECASE,
    )
    stripped = re.sub(
        r"<latest_chris_message>\s*(.*?)\s*</latest_chris_message>",
        r"\1",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return " ".join(stripped.split())


def _classify_active_session_message(message: str, active_plan: MaestroPlan) -> str:
    lowered = message.lower().strip()
    has_blocking_rfi = any(
        item.needs_user_input and item.blocks_execution for item in active_plan.work_items
    )
    has_any_rfi = any(item.needs_user_input for item in active_plan.work_items)
    if _is_workflow_delete_message(lowered):
        return "delete_workflow"
    if any(token in lowered for token in ("new workflow", "new plan", "separate workflow", "start over")):
        return "new_workflow"
    understanding = _understand_active_message_with_llm(message, active_plan)
    if understanding is not None:
        return understanding.legacy_intent()
    if not _should_use_plan_context(message, active_plan):
        return "new_workflow"
    if has_any_rfi:
        llm_classification = _classify_active_message_with_llm(message, active_plan)
        if llm_classification is not None:
            return llm_classification
    if any(
        token in lowered
        for token in (
            "merge the pr",
            "merge pr",
            "merge this pr",
            "merge it",
            "merge that",
            "merge and reload",
            "hot reload",
            "reload the app",
            "make it live",
            "ship it",
            "change the plan",
            "update the plan",
            "refine",
            "instead",
            "also include",
            "remove ",
            "drop ",
            "only ",
            "belongs in",
            "move this",
            "do this first",
            "do that first",
        )
    ):
        return "refined"
    if (has_blocking_rfi or (has_any_rfi and _looks_like_rfi_answer(lowered))) and not lowered.endswith("?"):
        return "rfi_answered"
    if any(token in lowered for token in ("remember", "log ", "capture ", "add task", "contact:", "event:")):
        return "routed"
    if lowered.endswith("?") or any(
        lowered.startswith(prefix)
        for prefix in ("what ", "why ", "how ", "who ", "when ", "where ", "can you explain")
    ):
        return "side_chat"
    llm_classification = _classify_active_message_with_llm(message, active_plan)
    if llm_classification is not None:
        return llm_classification
    return "refined"


def _is_scheduled_workflow_status_question(message: str) -> bool:
    lowered = message.lower().strip()
    if not any(
        token in lowered
        for token in (
            "scheduled workflow",
            "scheduled workflows",
            "actively scheduled",
            "recurring workflow",
            "recurring workflows",
            "trigger workflow",
            "trigger workflows",
            "active queue",
        )
    ):
        return False
    return lowered.endswith("?") or any(
        lowered.startswith(prefix)
        for prefix in (
            "what ",
            "which ",
            "tell me",
            "show me",
            "list ",
            "summarize",
            "give me",
        )
    )


def _scheduled_workflow_status_response(db: Session) -> str:
    definitions = SchedulerService(db).list_definitions(active_only=True)
    domains = {domain.id: domain.key for domain in db.scalars(select(Domain)).all()}
    scheduled = [
        definition
        for definition in definitions
        if definition.trigger_type in {"scheduled", "recurring"}
    ]
    triggered = [definition for definition in definitions if definition.trigger_type == "event"]
    if not scheduled and not triggered:
        return "There are no active scheduled or trigger-based workflows configured."

    lines = ["Here is what is actively scheduled right now:"]
    if scheduled:
        lines.append("Scheduled and recurring workflows:")
        lines.extend(f"- {_definition_summary(definition, domains)}" for definition in scheduled)
    if triggered:
        lines.append("Trigger-based workflows:")
        lines.extend(f"- {_definition_summary(definition, domains)}" for definition in triggered)
    return "\n".join(lines)


def _definition_summary(definition: Any, domains: dict[uuid.UUID, str]) -> str:
    trigger_config = definition.trigger_config or {}
    if definition.trigger_type == "event":
        trigger = trigger_config.get("event_type") or "event trigger"
    else:
        trigger = (
            trigger_config.get("time_of_day")
            or trigger_config.get("next_run_at")
            or "configured schedule"
        )
    domain = domains.get(definition.domain_id, "global")
    return f"{definition.name} ({domain}, {definition.trigger_type}, {trigger})"


def _should_use_plan_context(message: str, active_plan: MaestroPlan) -> bool:
    lowered = message.lower().strip()
    has_blocking_rfi = any(
        item.needs_user_input and item.blocks_execution for item in active_plan.work_items
    )
    has_any_rfi = any(item.needs_user_input for item in active_plan.work_items)
    if any(token in lowered for token in ("new workflow", "new plan", "separate workflow", "start over")):
        return False
    understanding = _understand_active_message_with_llm(message, active_plan)
    if understanding is not None:
        intent_types = {intent.type for intent in understanding.intents if intent.confidence >= 0.55}
        if intent_types & {"rfi_answer", "plan_refinement", "plan_question", "system_command"}:
            return True
        if understanding.relationship_to_active_plan in {"answers_rfi", "refines_plan", "asks_about_plan"}:
            return True
        if understanding.relationship_to_active_plan == "unrelated":
            return False
        if "workflow_request" in intent_types:
            return understanding.topic_scope == "active_topic"
        if "routed_item" in intent_types:
            return understanding.topic_scope == "active_topic"
        if "chat_response" in intent_types:
            return understanding.topic_scope == "active_topic" and understanding.relationship_to_active_plan != "none"
        return understanding.topic_scope == "active_topic"
    if has_any_rfi:
        llm_classification = _classify_active_message_with_llm(message, active_plan)
        if llm_classification in {"rfi_answered", "refined"}:
            return True
        if llm_classification in {"new_workflow", "side_chat"}:
            return False
    if (has_blocking_rfi or (has_any_rfi and _looks_like_rfi_answer(lowered))) and not lowered.endswith("?"):
        return True
    contextual_tokens = (
        "this plan",
        "that plan",
        "the plan",
        "current plan",
        "active plan",
        "previous plan",
        "this workflow",
        "that workflow",
        "the workflow",
        "current workflow",
        "active workflow",
        "previous workflow",
        "the pr",
        "this pr",
        "that pr",
        "merge the pr",
        "merge pr",
        "merge it",
        "merge that",
        "merge and reload",
        "hot reload",
        "reload the app",
        "make it live",
        "ship it",
        "approve",
        "approved",
        "reject",
        "run it",
        "save it",
        "save schedule",
        "change the plan",
        "update the plan",
        "refine",
        "instead",
        "also include",
        "remove ",
        "drop ",
        "only ",
        "belongs in",
        "move this",
        "do this first",
        "do that first",
    )
    if any(token in lowered for token in contextual_tokens):
        return True
    if _is_workflow_delete_message(lowered):
        return True
    if any(token in lowered for token in ("remember", "log ", "capture ", "add task", "contact:", "event:")):
        return True
    if lowered.endswith("?") or any(
        lowered.startswith(prefix)
        for prefix in ("what ", "why ", "how ", "who ", "when ", "where ", "can you explain")
    ):
        return False
    return False


def _classify_active_message_with_llm(message: str, active_plan: MaestroPlan) -> str | None:
    understanding = _understand_active_message_with_llm(message, active_plan)
    if understanding is not None:
        return understanding.legacy_intent()
    return classify_active_message_with_local_llm(
        message=message,
        active_plan=_active_plan_classifier_payload(active_plan),
        has_blocking_rfi=any(
            item.needs_user_input and item.blocks_execution for item in active_plan.work_items
        ),
    )


def _understand_active_message_with_llm(message: str, active_plan: MaestroPlan) -> Any | None:
    return understand_message_with_local_llm(
        message=message,
        active_plan=_active_plan_classifier_payload(active_plan),
        has_blocking_rfi=any(
            item.needs_user_input and item.blocks_execution for item in active_plan.work_items
        ),
    )


def _understand_message_without_active_plan(message: str) -> Any | None:
    return understand_message_with_local_llm(
        message=message,
        active_plan={
            "summary": None,
            "status": "none",
            "open_rfis": [],
            "work_items": [],
        },
        has_blocking_rfi=False,
    )


def _is_pure_chat_understanding(understanding: Any | None) -> bool:
    if understanding is None or getattr(understanding, "confidence", 0.0) < 0.62:
        return False
    intents = [
        intent
        for intent in getattr(understanding, "intents", [])
        if getattr(intent, "confidence", 0.0) >= 0.55
    ]
    if not intents:
        return False
    intent_types = {getattr(intent, "type", "") for intent in intents}
    if intent_types != {"chat_response"}:
        return False
    if getattr(understanding, "recommended_next_step", None) not in {"respond", "no_action"}:
        return False
    if getattr(understanding, "relationship_to_active_plan", "none") not in {"none", "unrelated"}:
        return False
    return True


def _direct_chat_response(db: Session, message: str) -> str:
    settings = get_settings()
    if settings.llm_provider == "openrouter" and not settings.openrouter_api_key:
        return _fallback_direct_chat_response(message)
    if settings.llm_provider == "openai" and not settings.openai_api_key:
        return _fallback_direct_chat_response(message)
    try:
        context_bundle = MaestroContextAssembler(db).build_bundle(
            query_text=message,
            max_chars=4200,
            memory_chars=1800,
            routed_chars=1000,
            report_limit=4,
            run_log_limit=4,
            artifact_limit=3,
        )
        context_text = context_bundle.rendered_text
    except Exception:
        context_text = ""
    instructions = (
        "You are Maestro speaking directly with Chris. Answer conversationally and helpfully. "
        "Do not create a workflow, do not claim agents are working, and do not route memory unless "
        "Chris explicitly asked for that. If useful, mention that you can turn the conversation into "
        "agent work later. Use the provided Maestro context as background; do not dump raw context "
        "or provenance unless Chris asks. Format responses as clean GitHub-flavored Markdown with "
        "blank lines between paragraphs, numbered lists, and bullet lists. Do not put an entire "
        "numbered list on one line."
    )
    input_text = (
        f"Chris's message:\n{message}\n\n"
        f"Relevant Maestro context, if any:\n{context_text or '(none retrieved)'}"
    )
    try:
        response = OpenAILLMClient().text_response(
            instructions=instructions,
            input_text=input_text,
        )
    except (LLMClientError, OSError, ValueError):
        return _fallback_direct_chat_response(message)
    return _strip_hidden_context(response.strip()) or _fallback_direct_chat_response(message)


def _fallback_direct_chat_response(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("brainstorm", "think through", "design", "feature")):
        return (
            "Yes, let's think this through here first. I'll keep this as a conversation until "
            "you ask me to turn it into agent work, a routed item, or a GitHub issue."
        )
    if lowered.endswith("?"):
        return (
            "I can answer this directly here. If we decide the answer needs codebase inspection, "
            "web research, or another agent's help, I'll propose that as a workflow first."
        )
    return "I'm with you. I'll keep this in the conversation for now rather than creating a workflow."


def _active_plan_classifier_payload(active_plan: MaestroPlan) -> dict[str, Any]:
    return {
        "summary": active_plan.summary,
        "status": active_plan.status,
        "open_rfis": [
            {
                "id": item.id,
                "title": item.title,
                "description": item.description,
                "blocks_execution": item.blocks_execution,
            }
            for item in active_plan.work_items
            if item.needs_user_input
        ],
        "work_items": [
            {
                "id": item.id,
                "type": item.type,
                "title": item.title,
                "description": item.description,
                "needs_agent": item.needs_agent,
                "needs_user_input": item.needs_user_input,
                "blocks_execution": item.blocks_execution,
            }
            for item in active_plan.work_items[:8]
        ],
    }


def _looks_like_rfi_answer(lowered_message: str) -> bool:
    answer_markers = (
        "answer to your rfi",
        "answering your rfi",
        "to answer your question",
        "for your question",
        "currently ",
        "right now ",
        "we currently",
        "we use",
        "we bank",
        "we were looking",
        "the first use case",
        "my answer",
    )
    return any(marker in lowered_message for marker in answer_markers)


def _is_workflow_delete_message(lowered_message: str) -> bool:
    if (
        "remove" in lowered_message
        and any(token in lowered_message for token in (" task", "work item", "subtask"))
        and "workflow" not in lowered_message
    ):
        return False
    destructive = any(
        token in lowered_message
        for token in ("delete", "remove", "cancel", "archive", "discard", "clear out", "clear")
    )
    target = any(
        token in lowered_message
        for token in (
            "workflow",
            "plan",
            "queue item",
            "queued work",
            "current work",
            "under development",
            "this",
            "that",
            "it",
        )
    )
    return destructive and target


def _classified_refinement_message(message: str, classification: str, active_plan: MaestroPlan) -> str:
    if classification == "rfi_answered":
        rfi_titles = [
            item.title
            for item in active_plan.work_items
            if item.needs_user_input
        ]
        return (
            "Chris answered open RFI(s) for the active plan: "
            f"{'; '.join(rfi_titles)}\n\nAnswer:\n{message}\n\n"
            "Use this answer to satisfy or reduce the RFI, preserve still-valid workflow work, "
            "and respond conversationally to Chris about how the plan changed."
        )
    if classification == "routed":
        return (
            "User added routed context inside the active Maestro session. "
            "Classify it as a task, contact, event, decision, RFI, memory candidate, or think tank "
            f"item as appropriate without disrupting still-valid workflow work.\n\nMessage:\n{message}"
        )
    return message


def _side_chat_response(message: str, active_plan: MaestroPlan) -> str:
    return (
        "Quick answer while keeping the current plan open: "
        "I can answer that here without changing the proposed workflow. "
        f"The active plan is still `{active_plan.summary}`."
    )


def _tool_approval_message(tool_call: dict[str, Any], *, approved: bool) -> str:
    tool_name = tool_call.get("tool_name")
    status = tool_call.get("status")
    if approved and status == "complete":
        return f"Approved and ran `{tool_name}` successfully."
    if approved:
        return f"I tried to run `{tool_name}` after approval, but it finished with status `{status}`: {tool_call.get('error_message') or 'no detail'}"
    return f"Rejected `{tool_name}`. I did not run it."


def _run_payload(run: MaestroRun) -> dict[str, Any]:
    return {
        "plan": _plan_payload(run.plan),
        "status": run.status,
        "parent_task_id": run.parent_task_id,
        "synthesis_report_id": run.synthesis_report_id,
        "synthesis": run.synthesis,
        "chat_summary": run.chat_summary,
        "staged_artifact_path": run.staged_artifact_path,
        "artifact_id": run.artifact_id,
        "scheduler": run.scheduler,
        "execution_stages": run.execution_stages,
        "tool_activity": run.tool_activity,
        "error_message": run.error_message,
        "child_runs": [
            {
                "run_id": child.run_id,
                "status": child.status,
                "agent": {
                    "key": child.agent.key,
                    "name": child.agent.name,
                    "domain_key": child.agent.domain_key,
                },
                "task_id": child.task_id,
                "report_id": child.report_id,
                "execution_note": child.execution_note,
                "output_text": child.output_text,
                "error_message": child.error_message,
                "tool_calls": child.tool_calls,
                "tool_loop": child.tool_loop,
            }
            for child in run.child_runs
        ],
    }
