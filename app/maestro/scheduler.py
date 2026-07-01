import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Agent,
    Domain,
    SchedulerEvent,
    SchedulerResourceLock,
    Task,
    WorkflowDefinition,
    WorkflowQueueItem,
    WorkflowRun,
)


class SchedulerService:
    def __init__(self, session: Session):
        self.session = session

    def upsert_definition(
        self,
        *,
        key: str,
        name: str,
        domain_id: uuid.UUID | None = None,
        description: str | None = None,
        trigger_type: str = "manual",
        trigger_config: dict[str, Any] | None = None,
        workflow_spec: dict[str, Any] | None = None,
        priority: str = "normal",
        fairness_group: str | None = None,
        is_active: bool = True,
    ) -> WorkflowDefinition:
        definition = self.session.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.key == key)
        )
        if definition is None:
            definition = WorkflowDefinition(key=key, name=name)
            self.session.add(definition)
        definition.domain_id = domain_id
        definition.name = name
        definition.description = description
        definition.trigger_type = trigger_type
        definition.trigger_config = trigger_config or {}
        definition.workflow_spec = workflow_spec or {}
        definition.priority = priority
        definition.fairness_group = fairness_group
        definition.is_active = is_active
        self.session.commit()
        self.session.refresh(definition)
        return definition

    def enqueue_maestro_plan(self, parent_task: Task) -> WorkflowRun:
        existing = self.session.scalar(
            select(WorkflowRun).where(WorkflowRun.parent_task_id == parent_task.id)
        )
        payload = parent_task.input_payload or {}
        scheduler = payload.get("scheduler") if isinstance(payload.get("scheduler"), dict) else {}
        queue_items = scheduler.get("queue_items") if isinstance(scheduler.get("queue_items"), list) else []
        if existing is not None:
            self._sync_queue_items(existing, parent_task, queue_items)
            return existing

        fairness_group = self._fairness_group(parent_task)
        run = WorkflowRun(
            parent_task_id=parent_task.id,
            conversation_id=parent_task.conversation_id,
            domain_id=parent_task.domain_id,
            source_type=parent_task.source_type,
            status="queued" if queue_items else parent_task.status,
            priority=parent_task.priority,
            fairness_group=fairness_group,
            idempotency_key=f"maestro-plan:{parent_task.id}",
            input_payload={
                "plan_id": payload.get("plan_id"),
                "summary": parent_task.objective,
                "workflow_key": parent_task.workflow_key,
                "scheduler_policy": scheduler.get("policy"),
            },
            scheduled_for=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.flush()
        self._sync_queue_items(run, parent_task, queue_items, commit=False)
        self.record_event(
            run,
            event_type="workflow_enqueued",
            message="Workflow run was enqueued from a Maestro plan.",
            payload={"queue_item_count": len(queue_items)},
            commit=False,
        )
        self.session.commit()
        self.session.refresh(run)
        return run

    def sync_run_status_from_task(self, parent_task: Task) -> WorkflowRun | None:
        run = self.session.scalar(select(WorkflowRun).where(WorkflowRun.parent_task_id == parent_task.id))
        if run is None:
            return None
        payload = parent_task.input_payload or {}
        scheduler = payload.get("scheduler") if isinstance(payload.get("scheduler"), dict) else {}
        queue_items = scheduler.get("queue_items") if isinstance(scheduler.get("queue_items"), list) else []
        self._sync_queue_items(run, parent_task, queue_items, commit=False)
        run.status = parent_task.status
        run.output_payload = parent_task.output_payload
        run.error_message = parent_task.error_message
        run.started_at = parent_task.started_at
        run.completed_at = parent_task.completed_at
        self.record_event(
            run,
            event_type=f"workflow_{parent_task.status}",
            message=f"Workflow run status synchronized to {parent_task.status}.",
            commit=False,
        )
        self.session.commit()
        self.session.refresh(run)
        return run

    def runnable_batches(self, *, limit: int = 25) -> list[dict[str, Any]]:
        runs = self.session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.status.in_(["queued", "ready", "running", "blocked"]))
            .order_by(WorkflowRun.created_at)
            .limit(limit)
        ).all()
        active_locks = self._active_lock_keys()
        batches: list[dict[str, Any]] = []
        used_fairness_groups: set[str] = set()
        used_locks: set[tuple[str, str]] = set(active_locks)
        for run in runs:
            items = self._queue_items_for_run(run.id)
            runnable = self._runnable_items(items)
            selected: list[WorkflowQueueItem] = []
            for item in runnable:
                fairness_group = item.fairness_group or run.fairness_group or "global"
                if fairness_group in used_fairness_groups and len(runnable) > 1:
                    continue
                lock_keys = self._lock_keys(item)
                if lock_keys & used_locks:
                    continue
                selected.append(item)
                used_fairness_groups.add(fairness_group)
                used_locks |= lock_keys
            if selected:
                batches.append(
                    {
                        "workflow_run_id": str(run.id),
                        "status": run.status,
                        "fairness_group": run.fairness_group,
                        "parallel_ready": [self.queue_item_payload(item) for item in selected],
                    }
                )
        return batches

    def dashboard(self) -> dict[str, Any]:
        runs = self.session.scalars(
            select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(20)
        ).all()
        return {
            "runs": [self.workflow_run_payload(run) for run in runs],
            "runnable_batches": self.runnable_batches(),
            "active_locks": [
                self.resource_lock_payload(lock)
                for lock in self.session.scalars(
                    select(SchedulerResourceLock)
                    .where(SchedulerResourceLock.status == "held")
                    .order_by(SchedulerResourceLock.created_at.desc())
                ).all()
            ],
        }

    def acquire_locks(
        self,
        queue_item: WorkflowQueueItem,
        *,
        owner: str,
        lease_seconds: int = 900,
    ) -> list[SchedulerResourceLock]:
        requested = self._lock_keys(queue_item)
        if requested & self._active_lock_keys():
            raise ValueError("Requested scheduler resource lock is already held.")
        expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        locks: list[SchedulerResourceLock] = []
        for resource_key, lock_scope in requested:
            lock = SchedulerResourceLock(
                resource_key=resource_key,
                lock_scope=lock_scope,
                status="held",
                workflow_run_id=queue_item.workflow_run_id,
                queue_item_id=queue_item.id,
                owner=owner,
                lease_expires_at=expires_at,
            )
            self.session.add(lock)
            locks.append(lock)
        queue_item.lease_owner = owner
        queue_item.lease_expires_at = expires_at
        self.record_event(
            self.session.get(WorkflowRun, queue_item.workflow_run_id),
            queue_item=queue_item,
            event_type="locks_acquired",
            message=f"Acquired {len(locks)} resource lock(s).",
            payload={"locks": [list(item) for item in requested]},
            commit=False,
        )
        self.session.commit()
        return locks

    def release_locks(self, queue_item: WorkflowQueueItem) -> None:
        locks = self.session.scalars(
            select(SchedulerResourceLock).where(
                SchedulerResourceLock.queue_item_id == queue_item.id,
                SchedulerResourceLock.status == "held",
            )
        ).all()
        for lock in locks:
            lock.status = "released"
        queue_item.lease_owner = None
        queue_item.lease_expires_at = None
        self.record_event(
            self.session.get(WorkflowRun, queue_item.workflow_run_id),
            queue_item=queue_item,
            event_type="locks_released",
            message=f"Released {len(locks)} resource lock(s).",
            commit=False,
        )
        self.session.commit()

    def workflow_run_payload(self, run: WorkflowRun) -> dict[str, Any]:
        return {
            "id": str(run.id),
            "parent_task_id": str(run.parent_task_id) if run.parent_task_id else None,
            "conversation_id": str(run.conversation_id) if run.conversation_id else None,
            "source_type": run.source_type,
            "status": run.status,
            "priority": run.priority,
            "fairness_group": run.fairness_group,
            "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "summary": (run.input_payload or {}).get("summary"),
            "queue_items": [
                self.queue_item_payload(item) for item in self._queue_items_for_run(run.id)
            ],
        }

    def queue_item_payload(self, item: WorkflowQueueItem) -> dict[str, Any]:
        domain = self.session.get(Domain, item.domain_id) if item.domain_id else None
        agent = self.session.get(Agent, item.agent_id) if item.agent_id else None
        return {
            "id": str(item.id),
            "workflow_run_id": str(item.workflow_run_id),
            "external_key": item.external_key,
            "status": item.status,
            "priority": item.priority,
            "stage_index": item.stage_index,
            "position": item.position,
            "objective": item.objective,
            "dependency_keys": item.dependency_keys,
            "resource_locks": item.resource_locks,
            "fairness_group": item.fairness_group,
            "attempt_count": item.attempt_count,
            "max_attempts": item.max_attempts,
            "domain_key": domain.key if domain else None,
            "agent_key": agent.key if agent else None,
            "agent_name": agent.name if agent else None,
            "lease_owner": item.lease_owner,
            "lease_expires_at": item.lease_expires_at.isoformat() if item.lease_expires_at else None,
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            "error_message": item.error_message,
        }

    def resource_lock_payload(self, lock: SchedulerResourceLock) -> dict[str, Any]:
        return {
            "id": str(lock.id),
            "resource_key": lock.resource_key,
            "lock_scope": lock.lock_scope,
            "status": lock.status,
            "workflow_run_id": str(lock.workflow_run_id) if lock.workflow_run_id else None,
            "queue_item_id": str(lock.queue_item_id) if lock.queue_item_id else None,
            "owner": lock.owner,
            "lease_expires_at": lock.lease_expires_at.isoformat() if lock.lease_expires_at else None,
        }

    def record_event(
        self,
        run: WorkflowRun | None,
        *,
        event_type: str,
        message: str,
        queue_item: WorkflowQueueItem | None = None,
        payload: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> SchedulerEvent:
        event = SchedulerEvent(
            workflow_run_id=run.id if run is not None else None,
            queue_item_id=queue_item.id if queue_item is not None else None,
            event_type=event_type,
            message=message,
            payload=payload or {},
        )
        self.session.add(event)
        if commit:
            self.session.commit()
            self.session.refresh(event)
        return event

    def _sync_queue_items(
        self,
        run: WorkflowRun,
        parent_task: Task,
        queue_items: list[dict[str, Any]],
        *,
        commit: bool = True,
    ) -> None:
        existing = {
            item.external_key: item
            for item in self.session.scalars(
                select(WorkflowQueueItem).where(WorkflowQueueItem.workflow_run_id == run.id)
            ).all()
        }
        for raw in queue_items:
            external_key = str(raw.get("id") or raw.get("external_key") or uuid.uuid4())
            item = existing.get(external_key)
            if item is None:
                item = WorkflowQueueItem(
                    workflow_run_id=run.id,
                    parent_task_id=parent_task.id,
                    external_key=external_key,
                    objective=str(raw.get("objective") or "Queued Maestro work"),
                )
                self.session.add(item)
            agent = self.session.scalar(select(Agent).where(Agent.key == raw.get("agent_key")))
            domain = self.session.scalar(select(Domain).where(Domain.key == raw.get("domain_key")))
            item.child_task_id = self._uuid_or_none(raw.get("child_task_id"))
            item.agent_id = agent.id if agent else None
            item.domain_id = domain.id if domain else parent_task.domain_id
            item.status = str(raw.get("status") or item.status or "queued")
            item.priority = str(raw.get("priority") or parent_task.priority or "normal")
            item.stage_index = int(raw.get("stage_index") or 1)
            item.position = int(raw.get("position") or 1)
            item.objective = str(raw.get("objective") or item.objective)
            item.dependency_keys = [str(value) for value in raw.get("depends_on_work_item_ids") or []]
            item.resource_locks = self._resource_locks_for_queue_item(raw)
            item.fairness_group = str(raw.get("domain_key") or run.fairness_group or "global")
            item.attempt_count = int(raw.get("retry_count") or 0)
            item.max_attempts = int(raw.get("max_attempts") or 2)
            item.input_payload = dict(raw)
            item.output_payload = {
                "child_report_id": raw.get("child_report_id"),
                "work_item_ids": raw.get("work_item_ids") or [],
            }
            item.error_message = raw.get("error_message")
            item.started_at = self._datetime_or_none(raw.get("started_at"))
            item.completed_at = self._datetime_or_none(raw.get("completed_at"))
        if commit:
            self.session.commit()

    def _queue_items_for_run(self, run_id: uuid.UUID) -> list[WorkflowQueueItem]:
        return list(
            self.session.scalars(
                select(WorkflowQueueItem)
                .where(WorkflowQueueItem.workflow_run_id == run_id)
                .order_by(WorkflowQueueItem.stage_index, WorkflowQueueItem.position)
            ).all()
        )

    def _runnable_items(self, items: list[WorkflowQueueItem]) -> list[WorkflowQueueItem]:
        complete_keys = {
            key
            for item in items
            if item.status == "completed"
            for key in [item.external_key, *self._work_item_ids(item)]
        }
        runnable: list[WorkflowQueueItem] = []
        for item in items:
            if item.status not in {"queued", "pending", "ready", "retrying"}:
                continue
            if all(key in complete_keys for key in item.dependency_keys):
                runnable.append(item)
        return runnable

    def _active_lock_keys(self) -> set[tuple[str, str]]:
        now = datetime.now(UTC)
        locks = self.session.scalars(
            select(SchedulerResourceLock).where(SchedulerResourceLock.status == "held")
        ).all()
        return {
            (lock.resource_key, lock.lock_scope)
            for lock in locks
            if lock.lease_expires_at is None or lock.lease_expires_at > now
        }

    def _lock_keys(self, item: WorkflowQueueItem) -> set[tuple[str, str]]:
        locks: set[tuple[str, str]] = set()
        for raw in item.resource_locks or []:
            if not isinstance(raw, dict):
                continue
            resource_key = str(raw.get("resource_key") or "").strip()
            lock_scope = str(raw.get("lock_scope") or "exclusive").strip()
            if resource_key:
                locks.add((resource_key, lock_scope))
        return locks

    def _resource_locks_for_queue_item(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        locks = []
        for tool_key in raw.get("required_tools") or []:
            tool_key = str(tool_key)
            scope = "shared" if any(token in tool_key for token in (".get", ".search", ".diff", ".checks", "memory.context")) else "exclusive"
            locks.append({"resource_key": f"tool:{tool_key}", "lock_scope": scope})
        agent_key = raw.get("agent_key")
        if agent_key:
            locks.append({"resource_key": f"agent:{agent_key}", "lock_scope": "exclusive"})
        return locks

    def _fairness_group(self, task: Task) -> str:
        if task.domain_id:
            domain = self.session.get(Domain, task.domain_id)
            if domain is not None:
                return domain.key
        payload = task.input_payload or {}
        subtasks = payload.get("subtasks") if isinstance(payload.get("subtasks"), list) else []
        domain_keys = [str(item.get("domain_key")) for item in subtasks if isinstance(item, dict) and item.get("domain_key")]
        return domain_keys[0] if domain_keys else "maestro"

    def _work_item_ids(self, item: WorkflowQueueItem) -> list[str]:
        output = item.output_payload if isinstance(item.output_payload, dict) else {}
        return [str(value) for value in output.get("work_item_ids") or []]

    def _uuid_or_none(self, value: Any) -> uuid.UUID | None:
        if not value:
            return None
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            return None

    def _datetime_or_none(self, value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None
