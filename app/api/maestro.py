from typing import Any
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Conversation, Message, RuntimeSetting, Task
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.tools.runtime import ToolExecutionError, ToolExecutionService, tool_result_payload
from app.maestro.orchestrator import (
    MaestroOrchestratorError,
    MaestroOrchestratorService,
    MaestroPlan,
    MaestroRun,
)

router = APIRouter(prefix="/maestro", tags=["maestro"])


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


class MaestroSessionMessage(BaseModel):
    sender: str
    content: str


class MaestroSessionCloseBody(BaseModel):
    messages: list[MaestroSessionMessage]
    plan_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


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
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conversation = _get_or_create_maestro_conversation(db, body.conversation_id)
    _record_session_message(db, conversation, "user", body.message)
    try:
        service = MaestroOrchestratorService(db)
        if body.active_plan_id is not None:
            active_plan = service.get_plan(body.active_plan_id)
            classification = _classify_active_session_message(body.message, active_plan)
            if classification == "side_chat":
                response_message = _side_chat_response(body.message, active_plan)
                _record_session_message(db, conversation, "maestro", response_message)
                return {
                    "kind": "chat_only",
                    "classification": classification,
                    "message": response_message,
                    "plan": None,
                    "chat_plan": None,
                    "active_plan": _plan_payload(active_plan),
                    "conversation": _conversation_payload(db, conversation),
                }
            if classification == "new_workflow":
                plan = service.create_plan(body.message, conversation_id=conversation.id)
                kind = "chat_only" if plan.is_chat_only else "planned"
            else:
                plan = service.refine_plan(
                    body.active_plan_id,
                    _classified_refinement_message(body.message, classification, active_plan),
                )
                kind = "chat_only" if plan.is_chat_only else classification
        else:
            plan = service.create_plan(body.message, conversation_id=conversation.id)
            kind = "chat_only" if plan.is_chat_only else "planned"
            classification = kind
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response_message = _maestro_response_text(
        plan,
        refined=kind in {"refined", "rfi_answered", "routed"},
    )
    _record_session_message(db, conversation, "maestro", response_message)
    return {
        "kind": kind,
        "classification": classification,
        "message": response_message,
        "plan": None if plan.is_chat_only else _plan_payload(plan),
        "chat_plan": _plan_payload(plan) if plan.is_chat_only else None,
        "active_plan": None,
        "conversation": _conversation_payload(db, conversation),
    }


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
        run = MaestroOrchestratorService(db).run_plan(
            plan_id,
            execute_llm=body.execute_llm,
            auto_tool_loop=body.auto_tool_loop,
            max_tool_iterations=body.max_tool_iterations,
        )
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_session_message(
        db,
        conversation,
        "maestro",
        run.chat_summary
        if run.status == "completed"
        else f"The workflow finished with status {run.status}.\n\n{run.chat_summary}",
    )
    return {"run": _run_payload(run)}


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
    conversation = _create_maestro_conversation(db)
    _set_active_maestro_conversation(db, conversation.id)
    return {"conversation": _conversation_payload(db, conversation)}


@router.get("/sessions/active")
def get_active_maestro_session(db: Session = Depends(get_db)) -> dict[str, Any]:
    conversation = _active_maestro_conversation(db)
    if conversation is None:
        conversation = _create_maestro_conversation(db)
        _set_active_maestro_conversation(db, conversation.id)
    return {"conversation": _conversation_payload(db, conversation)}


@router.get("/sessions")
def list_maestro_sessions(db: Session = Depends(get_db)) -> dict[str, Any]:
    maestro_domain = DomainRepository(db).get_by_key("maestro-development")
    query = select(Conversation).order_by(Conversation.updated_at.desc()).limit(25)
    if maestro_domain is not None:
        query = query.where(Conversation.domain_id == maestro_domain.id)
    conversations = db.scalars(query).all()
    return {"sessions": [_conversation_payload(db, conversation, include_messages=False) for conversation in conversations]}


@router.get("/sessions/{conversation_id}")
def get_maestro_session(conversation_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown Maestro session.")
    _set_active_maestro_conversation(db, conversation.id)
    return {"conversation": _conversation_payload(db, conversation)}


_ACTIVE_MAESTRO_SESSION_KEY = "active_maestro_conversation"


def _get_or_create_maestro_conversation(
    db: Session,
    conversation_id: uuid.UUID | None,
) -> Conversation:
    if conversation_id is not None:
        conversation = db.get(Conversation, conversation_id)
        if conversation is not None:
            _set_active_maestro_conversation(db, conversation.id)
            return conversation
    conversation = _active_maestro_conversation(db)
    if conversation is not None:
        return conversation
    conversation = _create_maestro_conversation(db)
    _set_active_maestro_conversation(db, conversation.id)
    return conversation


def _create_maestro_conversation(db: Session) -> Conversation:
    seed_default_domains(db)
    maestro_domain = DomainRepository(db).get_by_key("maestro-development")
    conversation = Conversation(
        domain_id=maestro_domain.id if maestro_domain else None,
        title="Maestro session",
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _active_maestro_conversation(db: Session) -> Conversation | None:
    setting = db.get(RuntimeSetting, _ACTIVE_MAESTRO_SESSION_KEY)
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


def _record_session_message(
    db: Session,
    conversation: Conversation,
    sender: str,
    content: str,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        sender_type="user" if sender == "user" else "maestro",
        content=content,
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


def _conversation_payload(
    db: Session,
    conversation: Conversation,
    *,
    include_messages: bool = True,
) -> dict[str, Any]:
    messages = _conversation_messages(db, conversation.id) if include_messages else []
    message_count = len(messages) if include_messages else len(_conversation_messages(db, conversation.id))
    plan = _latest_conversation_plan(db, conversation.id)
    return {
        "id": str(conversation.id),
        "title": conversation.title or "Maestro session",
        "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
        "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else None,
        "message_count": message_count,
        "messages": [
            {
                "id": str(message.id),
                "sender": "user" if message.sender_type == "user" else "maestro",
                "content": message.content,
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
            for message in messages
        ],
        "active_plan": _plan_payload(plan) if plan is not None else None,
    }


def _latest_conversation_plan(db: Session, conversation_id: uuid.UUID) -> MaestroPlan | None:
    task = db.scalar(
        select(Task)
        .where(
            Task.conversation_id == conversation_id,
            Task.workflow_key == "maestro.generic",
        )
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(1)
    )
    if task is None:
        return None
    try:
        return MaestroOrchestratorService(db).get_plan(task.id)
    except MaestroOrchestratorError:
        return None


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
        "selected_agents": plan.selected_agents,
        "registry_snapshot": plan.registry_snapshot,
        "approval_required": plan.approval_required,
        "scheduler": plan.scheduler,
        "created_at": plan.created_at,
        "direct_response": plan.direct_response,
        "planner_notes": plan.planner_notes,
    }


def _maestro_response_text(plan: MaestroPlan, *, refined: bool) -> str:
    if plan.is_chat_only:
        return plan.direct_response or plan.summary or "I can handle that directly here."
    blocking_items = [
        item for item in plan.work_items if item.needs_user_input and item.blocks_execution
    ]
    if blocking_items:
        question_text = _rfi_question_text(blocking_items)
        return (
            f"{question_text} Answer here in chat and I will use that to refine this active plan."
        )
    non_blocking_questions = [
        item for item in plan.work_items if item.needs_user_input and not item.blocks_execution
    ]
    stage_count = len(plan.execution_stages) or 1
    question_text = ""
    if non_blocking_questions:
        suffix = "" if len(non_blocking_questions) == 1 else "s"
        question_text = (
            f" I also found {len(non_blocking_questions)} non-blocking question{suffix} "
            "that can be answered later."
        )
    verb = "refined" if refined else "drafted"
    return (
        f"I {verb} a plan with {len(plan.work_items)} work items, "
        f"{len(plan.subtasks)} subtasks, and {stage_count} "
        f"{'stage' if stage_count == 1 else 'stages'}.{question_text} "
        "It is ready for review."
    )


def _rfi_question_text(blocking_items: list[Any]) -> str:
    if len(blocking_items) == 1:
        item = blocking_items[0]
        detail = item.description.strip() if item.description else item.title
        return f"I need one answer before this can run: {detail}"
    questions = [
        f"{index}. {item.description.strip() if item.description else item.title}"
        for index, item in enumerate(blocking_items, start=1)
    ]
    return "I need these answers before this can run: " + " ".join(questions)


def _classify_active_session_message(message: str, active_plan: MaestroPlan) -> str:
    lowered = message.lower().strip()
    has_blocking_rfi = any(
        item.needs_user_input and item.blocks_execution for item in active_plan.work_items
    )
    if any(token in lowered for token in ("new workflow", "new plan", "separate workflow", "start over")):
        return "new_workflow"
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
            "add ",
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
    if has_blocking_rfi and not lowered.endswith("?"):
        return "rfi_answered"
    if any(token in lowered for token in ("remember", "log ", "capture ", "add task", "contact:", "event:")):
        return "routed"
    if lowered.endswith("?") or any(
        lowered.startswith(prefix)
        for prefix in ("what ", "why ", "how ", "who ", "when ", "where ", "can you explain")
    ):
        return "side_chat"
    return "refined"


def _classified_refinement_message(message: str, classification: str, active_plan: MaestroPlan) -> str:
    if classification == "rfi_answered":
        blocking_titles = [
            item.title
            for item in active_plan.work_items
            if item.needs_user_input and item.blocks_execution
        ]
        return (
            "User answered blocking RFI(s): "
            f"{'; '.join(blocking_titles)}\n\nAnswer:\n{message}"
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
