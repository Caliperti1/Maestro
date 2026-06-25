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


class MaestroRunBody(BaseModel):
    execute_llm: bool = True


@router.post("/plan")
def create_maestro_plan(body: MaestroPlanBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        plan = MaestroOrchestratorService(db).create_plan(body.message)
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
        "selected_agents": plan.selected_agents,
        "registry_snapshot": plan.registry_snapshot,
        "approval_required": plan.approval_required,
        "scheduler": plan.scheduler,
        "created_at": plan.created_at,
        "direct_response": plan.direct_response,
        "planner_notes": plan.planner_notes,
    }


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
