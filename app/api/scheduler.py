from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import WorkflowQueueItem
from app.db.session import get_db
from app.maestro.scheduler import SchedulerService

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.get("/dashboard")
def get_scheduler_dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    return SchedulerService(db).dashboard()


@router.get("/runnable")
def get_runnable_batches(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"batches": SchedulerService(db).runnable_batches()}


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
