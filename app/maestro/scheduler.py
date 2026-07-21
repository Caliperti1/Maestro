import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_aware_utc, home_isoformat, to_home_timezone
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

    def list_definitions(self, *, active_only: bool = False) -> list[WorkflowDefinition]:
        query = select(WorkflowDefinition).order_by(WorkflowDefinition.created_at.desc())
        if active_only:
            query = query.where(WorkflowDefinition.is_active.is_(True))
        return list(self.session.scalars(query).all())

    def archive_definition(
        self,
        definition_id: uuid.UUID,
        *,
        reason: str = "Workflow definition was archived.",
        commit: bool = True,
    ) -> WorkflowDefinition | None:
        definition = self.session.get(WorkflowDefinition, definition_id)
        if definition is None:
            return None
        definition.is_active = False
        trigger_config = dict(definition.trigger_config or {})
        trigger_config["archived_at"] = datetime.now(UTC).isoformat()
        trigger_config["archive_reason"] = reason
        definition.trigger_config = trigger_config
        for run in self.session.scalars(
            select(WorkflowRun).where(
                WorkflowRun.workflow_definition_id == definition.id,
                WorkflowRun.status.in_(["scheduled", "queued", "ready", "running", "blocked", "failed"]),
            )
        ).all():
            run.status = "archived"
            run.error_message = reason
            run.completed_at = run.completed_at or datetime.now(UTC)
            for item in self._queue_items_for_run(run.id):
                if item.status not in {"completed", "failed", "archived"}:
                    item.status = "archived"
                    item.error_message = reason
                    item.completed_at = item.completed_at or datetime.now(UTC)
                self.release_locks(item, commit=False)
            self.record_event(
                run,
                event_type="workflow_archived",
                message=reason,
                commit=False,
            )
        if commit:
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

    def enqueue_due_workflows(self, *, now: datetime | None = None) -> list[WorkflowRun]:
        now = now or datetime.now(UTC)
        self._normalize_definition_schedules(now=now)
        definitions = self.session.scalars(
            select(WorkflowDefinition).where(
                WorkflowDefinition.is_active.is_(True),
                WorkflowDefinition.trigger_type.in_(["scheduled", "recurring"]),
            )
        ).all()
        runs: list[WorkflowRun] = []
        for definition in definitions:
            trigger_config = definition.trigger_config or {}
            next_run_at = self._datetime_or_none(trigger_config.get("next_run_at"))
            if next_run_at is None or next_run_at > now:
                continue
            run = self.enqueue_definition_run(definition, scheduled_for=next_run_at)
            interval_minutes = int(trigger_config.get("interval_minutes") or 1440)
            definition.trigger_config = {
                **trigger_config,
                "last_enqueued_at": now.isoformat(),
                "next_run_at": (next_run_at + timedelta(minutes=interval_minutes)).isoformat(),
            }
            runs.append(run)
        if runs:
            self.session.commit()
        return runs

    def enqueue_event_workflows(
        self,
        *,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        event_id: str | None = None,
        now: datetime | None = None,
    ) -> list[WorkflowRun]:
        now = now or datetime.now(UTC)
        event_payload = event_payload or {}
        definitions = self.session.scalars(
            select(WorkflowDefinition).where(
                WorkflowDefinition.is_active.is_(True),
                WorkflowDefinition.trigger_type == "event",
            )
        ).all()
        runs: list[WorkflowRun] = []
        for definition in definitions:
            trigger_config = definition.trigger_config or {}
            if trigger_config.get("event_type") != event_type:
                continue
            if not self._event_matches_filters(event_payload, trigger_config.get("filters") or {}):
                continue
            suffix = event_id or str(event_payload.get("id") or uuid.uuid4())
            run = self.enqueue_definition_run(
                definition,
                scheduled_for=now,
                source_type="event",
                idempotency_suffix=f"event:{event_type}:{suffix}",
                event_payload={"event_type": event_type, "event_id": suffix, "payload": event_payload},
            )
            definition.trigger_config = {
                **trigger_config,
                "last_enqueued_at": now.isoformat(),
                "last_event_type": event_type,
                "last_event_id": suffix,
            }
            runs.append(run)
        if runs:
            self.session.commit()
        return runs

    def enqueue_definition_run(
        self,
        definition: WorkflowDefinition,
        *,
        scheduled_for: datetime | None = None,
        source_type: str = "scheduled",
        idempotency_suffix: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        scheduled_for = scheduled_for or datetime.now(UTC)
        idempotency_key = (
            f"workflow-definition:{definition.id}:{idempotency_suffix}"
            if idempotency_suffix
            else f"workflow-definition:{definition.id}:{scheduled_for.isoformat()}"
        )
        existing = self.session.scalar(
            select(WorkflowRun).where(WorkflowRun.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
        spec = definition.workflow_spec or {}
        queue_items = spec.get("queue_items") if isinstance(spec.get("queue_items"), list) else []
        run = WorkflowRun(
            workflow_definition_id=definition.id,
            domain_id=definition.domain_id,
            source_type=source_type,
            status="queued",
            priority=definition.priority,
            fairness_group=definition.fairness_group,
            idempotency_key=idempotency_key,
            input_payload={
                "definition_key": definition.key,
                "summary": definition.name,
                "workflow_spec": spec,
                "event": event_payload,
            },
            scheduled_for=scheduled_for,
        )
        self.session.add(run)
        self.session.flush()
        self._sync_definition_queue_items(run, definition, queue_items, commit=False)
        self.record_event(
            run,
            event_type="workflow_enqueued",
            message=f"Scheduled workflow `{definition.key}` was enqueued.",
            payload={"queue_item_count": len(queue_items)},
            commit=False,
        )
        self.session.commit()
        self.session.refresh(run)
        return run

    def replay_run(self, run_id: uuid.UUID) -> WorkflowRun:
        original = self.session.get(WorkflowRun, run_id)
        if original is None:
            raise ValueError("Unknown workflow run.")
        if original.workflow_definition_id is None:
            raise ValueError("Only workflow-definition runs can be replayed.")
        definition = self.session.get(WorkflowDefinition, original.workflow_definition_id)
        if definition is None:
            raise ValueError("The workflow definition for this run no longer exists.")
        event_payload = (original.input_payload or {}).get("event")
        replay = self.enqueue_definition_run(
            definition,
            scheduled_for=datetime.now(UTC),
            source_type="replay",
            idempotency_suffix=f"replay:{original.id}:{uuid.uuid4()}",
            event_payload=event_payload if isinstance(event_payload, dict) else None,
        )
        self.record_event(
            original,
            event_type="workflow_replayed",
            message=f"Workflow run was replayed as {replay.id}.",
            payload={"replay_run_id": str(replay.id)},
        )
        return replay

    def tick(
        self,
        *,
        owner: str = "maestro-worker",
        claim_limit: int = 4,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        enqueued = self.enqueue_due_workflows(now=now)
        claimed = self.claim_ready_items(
            owner=owner,
            limit=claim_limit,
            lease_seconds=lease_seconds,
        )
        return {
            "enqueued": [self.workflow_run_payload(run) for run in enqueued],
            "claimed": [self.queue_item_payload(item) for item in claimed],
            "runnable_batches": self.runnable_batches(),
        }

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

    def archive_run_for_parent_task(
        self,
        parent_task_id: uuid.UUID,
        *,
        reason: str = "Workflow was archived.",
        commit: bool = True,
    ) -> WorkflowRun | None:
        run = self.session.scalar(select(WorkflowRun).where(WorkflowRun.parent_task_id == parent_task_id))
        if run is None:
            return None
        return self.archive_run(run.id, reason=reason, commit=commit)

    def archive_run(
        self,
        run_id: uuid.UUID,
        *,
        reason: str = "Workflow was archived.",
        commit: bool = True,
    ) -> WorkflowRun | None:
        run = self.session.get(WorkflowRun, run_id)
        if run is None:
            return None
        run.status = "archived"
        run.error_message = reason
        run.completed_at = run.completed_at or datetime.now(UTC)
        for item in self._queue_items_for_run(run.id):
            if item.status not in {"completed", "failed", "archived"}:
                item.status = "archived"
                item.error_message = reason
                item.completed_at = item.completed_at or datetime.now(UTC)
            self.release_locks(item, commit=False)
        self.record_event(
            run,
            event_type="workflow_archived",
            message=reason,
            commit=False,
        )
        if commit:
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

    def claim_ready_items(
        self,
        *,
        owner: str,
        limit: int = 4,
        lease_seconds: int = 900,
    ) -> list[WorkflowQueueItem]:
        claimed: list[WorkflowQueueItem] = []
        for batch in self.runnable_batches():
            for item_payload in batch["parallel_ready"]:
                if len(claimed) >= limit:
                    self.session.commit()
                    return claimed
                item = self.session.get(WorkflowQueueItem, uuid.UUID(item_payload["id"]))
                if item is None or item.status not in {"queued", "pending", "ready", "retrying"}:
                    continue
                try:
                    self.acquire_locks(item, owner=owner, lease_seconds=lease_seconds, commit=False)
                except ValueError:
                    continue
                item.status = "running"
                item.attempt_count += 1
                item.lease_owner = owner
                item.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
                item.started_at = datetime.now(UTC)
                run = self.session.get(WorkflowRun, item.workflow_run_id)
                if run is not None:
                    run.status = "running"
                    run.started_at = run.started_at or datetime.now(UTC)
                self.record_event(
                    run,
                    queue_item=item,
                    event_type="queue_item_claimed",
                    message=f"Queue item `{item.external_key}` claimed by {owner}.",
                    payload={"owner": owner},
                    commit=False,
                )
                claimed.append(item)
        self.session.commit()
        return claimed

    def complete_queue_item(
        self,
        queue_item_id: uuid.UUID,
        *,
        output_payload: dict[str, Any] | None = None,
    ) -> WorkflowQueueItem:
        item = self._require_queue_item(queue_item_id)
        item.status = "completed"
        item.output_payload = {**(item.output_payload or {}), **(output_payload or {})}
        item.error_message = None
        item.completed_at = datetime.now(UTC)
        self.release_locks(item, commit=False)
        run = self.session.get(WorkflowRun, item.workflow_run_id)
        self._refresh_run_status(run)
        self.record_event(
            run,
            queue_item=item,
            event_type="queue_item_completed",
            message=f"Queue item `{item.external_key}` completed.",
            commit=False,
        )
        self.session.commit()
        self.session.refresh(item)
        return item

    def fail_queue_item(
        self,
        queue_item_id: uuid.UUID,
        *,
        error_message: str,
    ) -> WorkflowQueueItem:
        item = self._require_queue_item(queue_item_id)
        item.status = "retrying" if item.attempt_count < item.max_attempts else "failed"
        item.error_message = error_message
        item.completed_at = datetime.now(UTC) if item.status == "failed" else None
        self.release_locks(item, commit=False)
        run = self.session.get(WorkflowRun, item.workflow_run_id)
        self._refresh_run_status(run)
        self.record_event(
            run,
            queue_item=item,
            event_type="queue_item_failed",
            message=f"Queue item `{item.external_key}` failed with status {item.status}.",
            payload={"error_message": error_message},
            commit=False,
        )
        self.session.commit()
        self.session.refresh(item)
        return item

    def block_queue_item(
        self,
        queue_item_id: uuid.UUID,
        *,
        error_message: str,
        output_payload: dict[str, Any] | None = None,
    ) -> WorkflowQueueItem:
        item = self._require_queue_item(queue_item_id)
        item.status = "blocked"
        item.error_message = error_message
        item.output_payload = {**(item.output_payload or {}), **(output_payload or {})}
        self.release_locks(item, commit=False)
        run = self.session.get(WorkflowRun, item.workflow_run_id)
        self._refresh_run_status(run)
        self.record_event(
            run,
            queue_item=item,
            event_type="queue_item_blocked",
            message=f"Queue item `{item.external_key}` blocked: {error_message}",
            payload={"error_message": error_message},
            commit=False,
        )
        self.session.commit()
        self.session.refresh(item)
        return item

    def update_queue_item(
        self,
        queue_item_id: uuid.UUID,
        *,
        status: str | None = None,
        priority: str | None = None,
        fairness_group: str | None = None,
    ) -> WorkflowQueueItem:
        item = self._require_queue_item(queue_item_id)
        if status is not None:
            item.status = status
        if priority is not None:
            item.priority = priority
        if fairness_group is not None:
            item.fairness_group = fairness_group
        run = self.session.get(WorkflowRun, item.workflow_run_id)
        self._refresh_run_status(run)
        self.record_event(
            run,
            queue_item=item,
            event_type="queue_item_updated",
            message=f"Queue item `{item.external_key}` was edited.",
            payload={"status": status, "priority": priority, "fairness_group": fairness_group},
            commit=False,
        )
        self.session.commit()
        self.session.refresh(item)
        return item

    def dashboard(self) -> dict[str, Any]:
        runs = self.session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.status.in_(["queued", "ready", "running", "blocked", "failed"]))
            .order_by(WorkflowRun.created_at.desc())
            .limit(20)
        ).all()
        return {
            "definitions": [
                self.workflow_definition_payload(definition)
                for definition in self.list_definitions(active_only=True)
            ],
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

    def runs_for_definition(
        self,
        definition_id: uuid.UUID,
        *,
        limit: int = 20,
    ) -> list[WorkflowRun]:
        return list(
            self.session.scalars(
                select(WorkflowRun)
                .where(
                    WorkflowRun.workflow_definition_id == definition_id,
                    WorkflowRun.status != "archived",
                )
                .order_by(WorkflowRun.created_at.desc())
                .limit(limit)
            ).all()
        )

    def acquire_locks(
        self,
        queue_item: WorkflowQueueItem,
        *,
        owner: str,
        lease_seconds: int = 900,
        commit: bool = True,
    ) -> list[SchedulerResourceLock]:
        requested = self._lock_keys(queue_item)
        expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        locks: list[SchedulerResourceLock] = []
        for resource_key, lock_scope in requested:
            lock = self.session.scalar(
                select(SchedulerResourceLock).where(
                    SchedulerResourceLock.resource_key == resource_key,
                    SchedulerResourceLock.lock_scope == lock_scope,
                )
            )
            if lock is not None and self._is_active_lock(lock):
                if lock.queue_item_id == queue_item.id:
                    locks.append(lock)
                    continue
                raise ValueError("Requested scheduler resource lock is already held.")
            if lock is None:
                lock = SchedulerResourceLock(
                    resource_key=resource_key,
                    lock_scope=lock_scope,
                )
                self.session.add(lock)
            lock.status = "held"
            lock.workflow_run_id = queue_item.workflow_run_id
            lock.queue_item_id = queue_item.id
            lock.owner = owner
            lock.lease_expires_at = expires_at
            lock.metadata_ = {}
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
        if commit:
            self.session.commit()
        return locks

    def release_locks(self, queue_item: WorkflowQueueItem, *, commit: bool = True) -> None:
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
        if commit:
            self.session.commit()

    def workflow_run_payload(self, run: WorkflowRun, *, include_events: bool = False) -> dict[str, Any]:
        payload = {
            "id": str(run.id),
            "workflow_definition_id": str(run.workflow_definition_id) if run.workflow_definition_id else None,
            "parent_task_id": str(run.parent_task_id) if run.parent_task_id else None,
            "conversation_id": str(run.conversation_id) if run.conversation_id else None,
            "source_type": run.source_type,
            "status": run.status,
            "priority": run.priority,
            "fairness_group": run.fairness_group,
            "scheduled_for": home_isoformat(run.scheduled_for),
            "started_at": home_isoformat(run.started_at),
            "completed_at": home_isoformat(run.completed_at),
            "created_at": home_isoformat(run.created_at),
            "summary": (run.input_payload or {}).get("summary"),
            "input_payload": run.input_payload or {},
            "output_payload": run.output_payload or {},
            "error_message": run.error_message,
            "queue_items": [
                self.queue_item_payload(item) for item in self._queue_items_for_run(run.id)
            ],
        }
        if include_events:
            payload["events"] = [
                self.scheduler_event_payload(event)
                for event in self.session.scalars(
                    select(SchedulerEvent)
                    .where(SchedulerEvent.workflow_run_id == run.id)
                    .order_by(SchedulerEvent.created_at.desc())
                ).all()
            ]
        return payload

    def workflow_definition_payload(self, definition: WorkflowDefinition) -> dict[str, Any]:
        domain = self.session.get(Domain, definition.domain_id) if definition.domain_id else None
        return {
            "id": str(definition.id),
            "domain_key": domain.key if domain else None,
            "key": definition.key,
            "name": definition.name,
            "description": definition.description,
            "trigger_type": definition.trigger_type,
            "trigger_config": definition.trigger_config,
            "workflow_spec": definition.workflow_spec,
            "priority": definition.priority,
            "fairness_group": definition.fairness_group,
            "is_active": definition.is_active,
            "created_at": home_isoformat(definition.created_at),
            "updated_at": home_isoformat(definition.updated_at),
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
            "required_skills": [
                str(value)
                for value in (item.input_payload or {}).get("required_skills", [])
                if str(value).strip()
            ],
            "model_profile": (item.input_payload or {}).get("model_profile"),
            "model_tier": (item.input_payload or {}).get("model_tier") or "auto",
            "model_rationale": (item.input_payload or {}).get("model_rationale"),
            "lease_owner": item.lease_owner,
            "lease_expires_at": home_isoformat(item.lease_expires_at),
            "started_at": home_isoformat(item.started_at),
            "completed_at": home_isoformat(item.completed_at),
            "error_message": item.error_message,
            "output_payload": item.output_payload or {},
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
            "lease_expires_at": home_isoformat(lock.lease_expires_at),
        }

    def scheduler_event_payload(self, event: SchedulerEvent) -> dict[str, Any]:
        return {
            "id": str(event.id),
            "workflow_run_id": str(event.workflow_run_id) if event.workflow_run_id else None,
            "queue_item_id": str(event.queue_item_id) if event.queue_item_id else None,
            "event_type": event.event_type,
            "message": event.message,
            "payload": event.payload,
            "created_at": event.created_at.isoformat() if event.created_at else None,
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
        self._ensure_agents_available()
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

    def _sync_definition_queue_items(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        queue_items: list[dict[str, Any]],
        *,
        commit: bool = True,
    ) -> None:
        self._ensure_agents_available()
        existing = {
            item.external_key: item
            for item in self.session.scalars(
                select(WorkflowQueueItem).where(WorkflowQueueItem.workflow_run_id == run.id)
            ).all()
        }
        for position, raw in enumerate(queue_items, start=1):
            external_key = str(raw.get("id") or raw.get("external_key") or f"item-{position}")
            item = existing.get(external_key)
            if item is None:
                item = WorkflowQueueItem(
                    workflow_run_id=run.id,
                    external_key=external_key,
                    objective=str(raw.get("objective") or definition.name),
                )
                self.session.add(item)
            agent = self.session.scalar(select(Agent).where(Agent.key == raw.get("agent_key")))
            domain_key = raw.get("domain_key")
            domain = self.session.scalar(select(Domain).where(Domain.key == domain_key)) if domain_key else None
            item.agent_id = agent.id if agent else None
            item.domain_id = domain.id if domain else definition.domain_id
            item.status = str(raw.get("status") or "queued")
            item.priority = str(raw.get("priority") or definition.priority)
            item.stage_index = int(raw.get("stage_index") or 1)
            item.position = int(raw.get("position") or position)
            item.objective = str(raw.get("objective") or definition.name)
            item.dependency_keys = [str(value) for value in raw.get("dependency_keys") or raw.get("depends_on") or []]
            item.resource_locks = list(raw.get("resource_locks") or self._resource_locks_for_queue_item(raw))
            item.fairness_group = str(raw.get("fairness_group") or definition.fairness_group or domain_key or "global")
            item.max_attempts = int(raw.get("max_attempts") or 2)
            item.input_payload = dict(raw)
        if commit:
            self.session.commit()

    def _normalize_definition_schedules(self, *, now: datetime) -> None:
        changed = False
        definitions = self.session.scalars(
            select(WorkflowDefinition).where(
                WorkflowDefinition.is_active.is_(True),
                WorkflowDefinition.trigger_type.in_(["scheduled", "recurring"]),
            )
        ).all()
        for definition in definitions:
            trigger_config = definition.trigger_config or {}
            if trigger_config.get("next_run_at"):
                continue
            time_of_day = trigger_config.get("time_of_day")
            if not time_of_day:
                continue
            next_run = self._next_daily_time(now, str(time_of_day))
            definition.trigger_config = {
                **trigger_config,
                "interval_minutes": int(trigger_config.get("interval_minutes") or 1440),
                "next_run_at": next_run.isoformat(),
            }
            changed = True
        if changed:
            self.session.commit()

    def _next_daily_time(self, now: datetime, time_of_day: str) -> datetime:
        hour_text, minute_text = (time_of_day.split(":", 1) + ["0"])[:2]
        local_now = to_home_timezone(now)
        candidate = local_now.replace(
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0,
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def _event_matches_filters(
        self,
        event_payload: dict[str, Any],
        filters: dict[str, Any],
    ) -> bool:
        for key, expected in filters.items():
            actual: Any = event_payload
            for part in str(key).split("."):
                if not isinstance(actual, dict) or part not in actual:
                    return False
                actual = actual[part]
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

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

    def _require_queue_item(self, queue_item_id: uuid.UUID) -> WorkflowQueueItem:
        item = self.session.get(WorkflowQueueItem, queue_item_id)
        if item is None:
            raise ValueError("Unknown queue item.")
        return item

    def _refresh_run_status(self, run: WorkflowRun | None) -> None:
        if run is None:
            return
        items = self._queue_items_for_run(run.id)
        if not items:
            return
        active_items = [item for item in items if item.status != "archived"]
        if not active_items:
            run.status = "archived"
            return
        statuses = {item.status for item in active_items}
        if statuses <= {"completed"}:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
        elif "failed" in statuses:
            run.status = "failed"
        elif "running" in statuses:
            run.status = "running"
        elif statuses & {"blocked", "approval_required"}:
            run.status = "blocked"
        else:
            run.status = "queued"

    def _active_lock_keys(self) -> set[tuple[str, str]]:
        locks = self.session.scalars(
            select(SchedulerResourceLock).where(SchedulerResourceLock.status == "held")
        ).all()
        return {
            (lock.resource_key, lock.lock_scope)
            for lock in locks
            if self._is_active_lock(lock)
        }

    def _is_active_lock(self, lock: SchedulerResourceLock) -> bool:
        if lock.status != "held":
            return False
        return lock.lease_expires_at is None or ensure_aware_utc(lock.lease_expires_at) > datetime.now(UTC)

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
            scope = "shared" if any(
                token in tool_key
                for token in (
                    ".get",
                    ".search",
                    ".diff",
                    ".checks",
                    ".list_recent",
                    "memory.context",
                )
            ) else "exclusive"
            locks.append({"resource_key": f"tool:{tool_key}", "lock_scope": scope})
        agent_key = raw.get("agent_key")
        if agent_key:
            locks.append({"resource_key": f"agent:{agent_key}", "lock_scope": "exclusive"})
        return locks

    def _ensure_agents_available(self) -> None:
        from app.agents.runtime import AgentRegistryService

        AgentRegistryService(self.session).ensure_seed_agents()

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
            return ensure_aware_utc(value)
        try:
            return ensure_aware_utc(datetime.fromisoformat(str(value)))
        except ValueError:
            return None
