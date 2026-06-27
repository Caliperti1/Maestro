from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
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


class MaestroRunBody(BaseModel):
    execute_llm: bool = True


class MaestroSessionMessage(BaseModel):
    sender: str
    content: str


class MaestroSessionCloseBody(BaseModel):
    messages: list[MaestroSessionMessage]
    plan_id: uuid.UUID | None = None


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
    try:
        service = MaestroOrchestratorService(db)
        if body.active_plan_id is not None:
            active_plan = service.get_plan(body.active_plan_id)
            classification = _classify_active_session_message(body.message, active_plan)
            if classification == "side_chat":
                return {
                    "kind": "chat_only",
                    "classification": classification,
                    "message": _side_chat_response(body.message, active_plan),
                    "plan": None,
                    "chat_plan": None,
                    "active_plan": _plan_payload(active_plan),
                }
            if classification == "new_workflow":
                plan = service.create_plan(body.message)
                kind = "chat_only" if plan.is_chat_only else "planned"
            else:
                plan = service.refine_plan(
                    body.active_plan_id,
                    _classified_refinement_message(body.message, classification, active_plan),
                )
                kind = "chat_only" if plan.is_chat_only else classification
        else:
            plan = service.create_plan(body.message)
            kind = "chat_only" if plan.is_chat_only else "planned"
            classification = kind
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "kind": kind,
        "classification": classification,
        "message": _maestro_response_text(
            plan,
            refined=kind in {"refined", "rfi_answered", "routed"},
        ),
        "plan": None if plan.is_chat_only else _plan_payload(plan),
        "chat_plan": _plan_payload(plan) if plan.is_chat_only else None,
        "active_plan": None,
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
    try:
        run = MaestroOrchestratorService(db).run_plan(plan_id, execute_llm=body.execute_llm)
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run": _run_payload(run)}


@router.post("/sessions/close")
def close_maestro_session(
    body: MaestroSessionCloseBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        staged_artifact_path = MaestroOrchestratorService(db).close_session(
            messages=[message.model_dump() for message in body.messages],
            plan_id=body.plan_id,
        )
    except MaestroOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"staged_artifact_path": staged_artifact_path}


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
        for token in ("change the plan", "update the plan", "refine", "instead", "also include", "add ")
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


def _run_payload(run: MaestroRun) -> dict[str, Any]:
    return {
        "plan": _plan_payload(run.plan),
        "status": run.status,
        "parent_task_id": run.parent_task_id,
        "synthesis_report_id": run.synthesis_report_id,
        "synthesis": run.synthesis,
        "staged_artifact_path": run.staged_artifact_path,
        "artifact_id": run.artifact_id,
        "scheduler": run.scheduler,
        "execution_stages": run.execution_stages,
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
            }
            for child in run.child_runs
        ],
    }
