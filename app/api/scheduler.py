from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import WorkflowDefinition, WorkflowQueueItem, WorkflowRun
from app.db.repositories import DomainRepository
from app.db.session import get_db
from app.maestro.scheduler import SchedulerService
from app.maestro.scheduler_worker import (
    SchedulerWorkerService,
    scheduler_worker_settings,
    update_scheduler_worker_settings,
)

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


class SchedulerRunUpdateBody(BaseModel):
    status: str | None = None


class SchedulerDefinitionBody(BaseModel):
    key: str
    name: str
    domain_key: str | None = None
    description: str | None = None
    trigger_type: str = "manual"
    trigger_config: dict[str, Any] = Field(default_factory=dict)
    workflow_spec: dict[str, Any] = Field(default_factory=dict)
    priority: str = "normal"
    fairness_group: str | None = None
    is_active: bool = True


class SchedulerEventBody(BaseModel):
    event_type: str
    event_payload: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = None


class SchedulerTickBody(BaseModel):
    owner: str = "maestro-worker"
    claim_limit: int = Field(default=4, ge=1, le=20)
    lease_seconds: int = Field(default=900, ge=30, le=86400)


class SchedulerWorkerRunBody(BaseModel):
    owner: str = "maestro-worker"
    claim_limit: int = Field(default=4, ge=1, le=20)
    lease_seconds: int = Field(default=900, ge=30, le=86400)
    execute_llm: bool = True
    auto_tool_loop: bool = True
    max_tool_iterations: int = Field(default=2, ge=1, le=4)


class SchedulerWorkerSettingsBody(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    claim_limit: int | None = Field(default=None, ge=1, le=20)
    execute_llm: bool | None = None
    auto_tool_loop: bool | None = None


@router.get("/definitions")
def list_workflow_definitions(
    active_only: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = SchedulerService(db)
    definitions = service.list_definitions(active_only=active_only)
    return {"definitions": [service.workflow_definition_payload(definition) for definition in definitions]}


@router.post("/definitions")
def upsert_workflow_definition(
    body: SchedulerDefinitionBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain = DomainRepository(db).get_by_key(body.domain_key) if body.domain_key else None
    if body.domain_key and domain is None:
        raise HTTPException(status_code=400, detail=f"Unknown domain: {body.domain_key}")
    service = SchedulerService(db)
    definition = service.upsert_definition(
        key=body.key,
        name=body.name,
        domain_id=domain.id if domain else None,
        description=body.description,
        trigger_type=body.trigger_type,
        trigger_config=body.trigger_config,
        workflow_spec=body.workflow_spec,
        priority=body.priority,
        fairness_group=body.fairness_group or body.domain_key,
        is_active=body.is_active,
    )
    return {"definition": service.workflow_definition_payload(definition)}


@router.get("/definitions/{definition_id}")
def get_workflow_definition(
    definition_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    definition = db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Unknown workflow definition.")
    service = SchedulerService(db)
    runs = service.runs_for_definition(definition.id, limit=20)
    return {
        "definition": service.workflow_definition_payload(definition),
        "runs": [service.workflow_run_payload(run, include_events=True) for run in runs],
    }


@router.patch("/definitions/{definition_id}")
def update_workflow_definition(
    definition_id: uuid.UUID,
    body: SchedulerDefinitionBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    existing = db.get(WorkflowDefinition, definition_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown workflow definition.")
    domain = DomainRepository(db).get_by_key(body.domain_key) if body.domain_key else None
    if body.domain_key and domain is None:
        raise HTTPException(status_code=400, detail=f"Unknown domain: {body.domain_key}")
    service = SchedulerService(db)
    definition = service.upsert_definition(
        key=existing.key,
        name=body.name,
        domain_id=domain.id if domain else None,
        description=body.description,
        trigger_type=body.trigger_type,
        trigger_config=body.trigger_config,
        workflow_spec=body.workflow_spec,
        priority=body.priority,
        fairness_group=body.fairness_group or body.domain_key,
        is_active=body.is_active,
    )
    return {"definition": service.workflow_definition_payload(definition)}


@router.get("/dashboard")
def get_scheduler_dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    return SchedulerService(db).dashboard()


@router.get("/runs/{run_id}")
def get_workflow_run(run_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown workflow run.")
    return {"run": SchedulerService(db).workflow_run_payload(run, include_events=True)}


@router.patch("/runs/{run_id}")
def update_workflow_run(
    run_id: uuid.UUID,
    body: SchedulerRunUpdateBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown workflow run.")
    if body.status:
        if body.status == "archived":
            archived = SchedulerService(db).archive_run(
                run.id,
                reason="Workflow archived from UI.",
            )
            return {"run": SchedulerService(db).workflow_run_payload(archived, include_events=True)}
        else:
            run.status = body.status
    db.commit()
    db.refresh(run)
    return {"run": SchedulerService(db).workflow_run_payload(run, include_events=True)}


@router.get("/runnable")
def get_runnable_batches(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"batches": SchedulerService(db).runnable_batches()}


@router.post("/triggers/enqueue-due")
def enqueue_due_triggers(db: Session = Depends(get_db)) -> dict[str, Any]:
    service = SchedulerService(db)
    runs = service.enqueue_due_workflows()
    return {"runs": [service.workflow_run_payload(run) for run in runs]}


@router.post("/triggers/event")
def enqueue_event_triggers(
    body: SchedulerEventBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = SchedulerService(db)
    runs = service.enqueue_event_workflows(
        event_type=body.event_type,
        event_payload=body.event_payload,
        event_id=body.event_id,
    )
    return {"runs": [service.workflow_run_payload(run) for run in runs]}


@router.post("/tick")
def run_scheduler_tick(
    body: SchedulerTickBody | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    options = body or SchedulerTickBody()
    return SchedulerService(db).tick(
        owner=options.owner,
        claim_limit=options.claim_limit,
        lease_seconds=options.lease_seconds,
    )


@router.post("/worker/run")
def run_scheduler_worker_once(
    body: SchedulerWorkerRunBody | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    options = body or SchedulerWorkerRunBody()
    return SchedulerWorkerService(db).run_once(
        owner=options.owner,
        claim_limit=options.claim_limit,
        lease_seconds=options.lease_seconds,
        execute_llm=options.execute_llm,
        auto_tool_loop=options.auto_tool_loop,
        max_tool_iterations=options.max_tool_iterations,
    )


@router.get("/worker/status")
def get_scheduler_worker_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"worker": scheduler_worker_settings(db)}


@router.patch("/worker/status")
def update_scheduler_worker_status(
    body: SchedulerWorkerSettingsBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return {
        "worker": update_scheduler_worker_settings(
            db,
            enabled=body.enabled,
            interval_seconds=body.interval_seconds,
            claim_limit=body.claim_limit,
            execute_llm=body.execute_llm,
            auto_tool_loop=body.auto_tool_loop,
        )
    }


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
