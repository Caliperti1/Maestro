from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import WorkflowQueueItem
from app.db.session import get_db
from app.maestro.scheduler import SchedulerService

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


class SchedulerClaimBody(BaseModel):
    owner: str = "maestro-worker"
    limit: int = Field(default=4, ge=1, le=20)
    lease_seconds: int = Field(default=900, ge=30, le=86400)


class SchedulerQueueItemCompleteBody(BaseModel):
    output_payload: dict[str, Any] = Field(default_factory=dict)


class SchedulerQueueItemFailBody(BaseModel):
    error_message: str


class SchedulerQueueItemUpdateBody(BaseModel):
    status: str | None = None
    priority: str | None = None
    fairness_group: str | None = None


@router.get("/dashboard")
def get_scheduler_dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    return SchedulerService(db).dashboard()


@router.get("/runnable")
def get_runnable_batches(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"batches": SchedulerService(db).runnable_batches()}


@router.post("/triggers/enqueue-due")
def enqueue_due_triggers(db: Session = Depends(get_db)) -> dict[str, Any]:
    service = SchedulerService(db)
    runs = service.enqueue_due_workflows()
    return {"runs": [service.workflow_run_payload(run) for run in runs]}


@router.post("/worker/claim")
def claim_scheduler_work(
    body: SchedulerClaimBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = SchedulerService(db)
    items = service.claim_ready_items(
        owner=body.owner,
        limit=body.limit,
        lease_seconds=body.lease_seconds,
    )
    return {"claimed": [service.queue_item_payload(item) for item in items]}


@router.patch("/queue-items/{queue_item_id}")
def update_queue_item(
    queue_item_id: uuid.UUID,
    body: SchedulerQueueItemUpdateBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = SchedulerService(db).update_queue_item(
            queue_item_id,
            status=body.status,
            priority=body.priority,
            fairness_group=body.fairness_group,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"queue_item": SchedulerService(db).queue_item_payload(item)}


@router.post("/queue-items/{queue_item_id}/complete")
def complete_queue_item(
    queue_item_id: uuid.UUID,
    body: SchedulerQueueItemCompleteBody | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = SchedulerService(db).complete_queue_item(
            queue_item_id,
            output_payload=body.output_payload if body else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"queue_item": SchedulerService(db).queue_item_payload(item)}


@router.post("/queue-items/{queue_item_id}/fail")
def fail_queue_item(
    queue_item_id: uuid.UUID,
    body: SchedulerQueueItemFailBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = SchedulerService(db).fail_queue_item(
            queue_item_id,
            error_message=body.error_message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"queue_item": SchedulerService(db).queue_item_payload(item)}


@router.post("/queue-items/{queue_item_id}/locks/acquire")
def acquire_queue_item_locks(
    queue_item_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.get(WorkflowQueueItem, queue_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown queue item.")
    try:
        locks = SchedulerService(db).acquire_locks(item, owner="maestro-api")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"locks": [SchedulerService(db).resource_lock_payload(lock) for lock in locks]}


@router.post("/queue-items/{queue_item_id}/locks/release")
def release_queue_item_locks(
    queue_item_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.get(WorkflowQueueItem, queue_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown queue item.")
    SchedulerService(db).release_locks(item)
    return {"released": True}
