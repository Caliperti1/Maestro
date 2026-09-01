"""Recurring todo series and idempotent occurrence materialization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import home_timezone
from app.db.models import RecurringTodoSeries, Todo
from app.memory.calendar_recurrence import (
    event_occurs_on,
    normalize_recurrence_rule,
    recurrence_options,
)

MATERIALIZED_FUTURE_OCCURRENCES = 2
MAX_RECURRENCE_SCAN_DAYS = 366 * 50


@dataclass(frozen=True)
class RecurringTodoCreation:
    series: RecurringTodoSeries
    occurrences: list[Todo]


class RecurringTodoService:
    def __init__(self, session: Session):
        self.session = session

    def create_series(
        self,
        *,
        domain_id: uuid.UUID | None,
        title: str,
        description: str,
        recurrence_rule: str,
        due_anchor_at: datetime | None,
        scheduled_anchor_at: datetime | None,
        timezone: str = "America/New_York",
        estimated_minutes: int | None = None,
        owner_type: str = "user",
        owner_ref: str | None = None,
        agent_task: bool = False,
        priority: str = "normal",
        source_refs: list[dict[str, Any]] | None = None,
        provenance: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> RecurringTodoCreation:
        anchor = due_anchor_at or scheduled_anchor_at
        if anchor is None:
            raise ValueError("A recurring todo needs its first due or scheduled date and time.")
        anchor = _aware(anchor)
        normalized_rule = normalize_recurrence_rule(
            recurrence_rule,
            start_at=anchor,
        )
        series = RecurringTodoSeries(
            domain_id=domain_id,
            title=title.strip(),
            description=description.strip() or title.strip(),
            recurrence_rule=normalized_rule,
            timezone=timezone,
            due_anchor_at=_aware(due_anchor_at) if due_anchor_at else None,
            scheduled_anchor_at=(
                _aware(scheduled_anchor_at) if scheduled_anchor_at else None
            ),
            estimated_minutes=_bounded_minutes(estimated_minutes),
            owner_type=owner_type,
            owner_ref=owner_ref,
            agent_task=agent_task,
            priority=priority,
            status="active",
            source_refs=source_refs or [],
            provenance=provenance or {},
            metadata_=metadata or {},
        )
        self.session.add(series)
        self.session.flush()
        occurrences = self.materialize_series(series, commit=False)
        if not occurrences:
            self.session.delete(series)
            self.session.flush()
            raise ValueError(
                "The recurring todo rule does not produce a current or future occurrence."
            )
        if series.estimated_minutes is None and occurrences:
            from app.memory.todo_scheduling import TodoSchedulingService

            series.estimated_minutes = TodoSchedulingService(self.session).estimate_minutes(
                occurrences[0]
            )
            for todo in occurrences:
                todo.estimated_minutes = series.estimated_minutes
                self._sync_projection(todo)
        if commit:
            self.session.commit()
            self.session.refresh(series)
        return RecurringTodoCreation(series=series, occurrences=occurrences)

    def materialize_series(
        self,
        series: RecurringTodoSeries,
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> list[Todo]:
        if series.status != "active":
            return []
        current = _aware(now or datetime.now(UTC))
        created: list[Todo] = []
        for occurrence_at in self.occurrence_candidates(series, now=current):
            existing = self.session.scalar(
                select(Todo).where(
                    Todo.recurring_series_id == series.id,
                    Todo.recurrence_original_at == occurrence_at,
                )
            )
            if existing is not None:
                if (
                    existing.status == "archived"
                    and (existing.metadata_ or {}).get("series_suppressed_reason")
                    == "series_paused"
                ):
                    existing.status = "open"
                    existing.metadata_ = {
                        key: value
                        for key, value in (existing.metadata_ or {}).items()
                        if key != "series_suppressed_reason"
                    }
                    self._sync_projection(existing)
                continue
            due_at, scheduled_start_at = _occurrence_times(series, occurrence_at)
            todo = Todo(
                domain_id=series.domain_id,
                title=series.title,
                description=series.description,
                todo_type="task",
                owner_type=series.owner_type,
                owner_ref=series.owner_ref,
                due_at=due_at,
                estimated_minutes=series.estimated_minutes,
                scheduled_start_at=scheduled_start_at,
                recurring_series_id=series.id,
                recurrence_original_at=occurrence_at,
                agent_task=series.agent_task,
                agent_task_status="pending" if series.agent_task else "not_agent",
                priority=series.priority,
                status="open",
                source_refs=series.source_refs,
                provenance={
                    **(series.provenance or {}),
                    "recurring_todo_series_id": str(series.id),
                    "recurrence_original_at": occurrence_at.isoformat(),
                },
                metadata_={
                    **(series.metadata_ or {}),
                    "recurring_todo": True,
                    "recurring_series_id": str(series.id),
                },
            )
            self.session.add(todo)
            self.session.flush()
            self._sync_projection(todo)
            created.append(todo)
        if (
            created
            or series.last_materialized_at is None
            or _aware(series.last_materialized_at).date() != current.date()
        ):
            series.last_materialized_at = current
        if commit:
            self.session.commit()
        return created

    def materialize_all(self, *, now: datetime | None = None) -> int:
        series_rows = self.session.scalars(
            select(RecurringTodoSeries).where(RecurringTodoSeries.status == "active")
        ).all()
        created = 0
        for series in series_rows:
            created += len(self.materialize_series(series, now=now, commit=False))
        self.session.commit()
        return created

    def occurrence_candidates(
        self,
        series: RecurringTodoSeries,
        *,
        now: datetime | None = None,
    ) -> list[datetime]:
        anchor = _series_anchor(series)
        timezone = _timezone(series.timezone)
        local_anchor = anchor.astimezone(timezone)
        current = _aware(now or datetime.now(UTC))
        options = recurrence_options(series.recurrence_rule)
        count_limit = _positive_int(options.get("COUNT"))
        latest_past: datetime | None = None
        future: list[datetime] = []
        occurrence_count = 0
        cursor = local_anchor.date()
        for _ in range(MAX_RECURRENCE_SCAN_DAYS):
            if event_occurs_on(
                start_at=anchor,
                recurrence_rule=series.recurrence_rule,
                target_date=cursor,
                timezone_name=series.timezone,
            ):
                occurrence_count += 1
                if count_limit is not None and occurrence_count > count_limit:
                    break
                occurrence = datetime.combine(
                    cursor,
                    local_anchor.timetz().replace(tzinfo=None),
                    timezone,
                ).astimezone(UTC)
                if occurrence <= current:
                    latest_past = occurrence
                else:
                    future.append(occurrence)
                    if len(future) >= MATERIALIZED_FUTURE_OCCURRENCES:
                        break
            cursor += timedelta(days=1)
        return [item for item in (latest_past, *future) if item is not None]

    def update_series(
        self,
        series_id: uuid.UUID,
        updates: dict[str, Any],
    ) -> RecurringTodoSeries:
        series = self.session.get(RecurringTodoSeries, series_id)
        if series is None:
            raise ValueError("Recurring todo series not found.")
        schedule_changed = False
        for key in (
            "title",
            "description",
            "timezone",
            "owner_type",
            "owner_ref",
            "priority",
        ):
            if key in updates:
                setattr(series, key, updates[key])
        if "estimated_minutes" in updates:
            series.estimated_minutes = _bounded_minutes(updates["estimated_minutes"])
        if "agent_task" in updates:
            series.agent_task = bool(updates["agent_task"])
        if "due_anchor_at" in updates:
            series.due_anchor_at = _optional_datetime(updates["due_anchor_at"])
            schedule_changed = True
        if "scheduled_anchor_at" in updates:
            series.scheduled_anchor_at = _optional_datetime(updates["scheduled_anchor_at"])
            schedule_changed = True
        if "recurrence_rule" in updates:
            anchor = _series_anchor(series)
            series.recurrence_rule = normalize_recurrence_rule(
                str(updates["recurrence_rule"]),
                start_at=anchor,
            )
            schedule_changed = True
        if "status" in updates:
            status = str(updates["status"]).lower()
            if status not in {"active", "paused", "ended"}:
                raise ValueError("Recurring todo status must be active, paused, or ended.")
            series.status = status
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            series.metadata_ = {**(series.metadata_ or {}), **updates["metadata"]}

        current = datetime.now(UTC)
        open_occurrences = self.session.scalars(
            select(Todo).where(
                Todo.recurring_series_id == series.id,
                Todo.status.notin_(["done", "archived"]),
            )
        ).all()
        if series.status != "active" or schedule_changed:
            for todo in open_occurrences:
                reference = todo.recurrence_original_at or todo.due_at or todo.scheduled_start_at
                if reference is not None and _aware(reference) > current:
                    todo.status = "archived"
                    todo.metadata_ = {
                        **(todo.metadata_ or {}),
                        "series_suppressed_reason": (
                            "series_schedule_changed"
                            if schedule_changed
                            else f"series_{series.status}"
                        ),
                    }
                    self._sync_projection(todo)
        for todo in open_occurrences:
            if todo.status == "archived":
                continue
            todo.title = series.title
            todo.description = series.description
            todo.priority = series.priority
            todo.owner_type = series.owner_type
            todo.owner_ref = series.owner_ref
            todo.estimated_minutes = series.estimated_minutes
            todo.agent_task = series.agent_task
            if series.agent_task and todo.agent_task_status == "not_agent":
                todo.agent_task_status = "pending"
            elif not series.agent_task:
                todo.agent_task_status = "not_agent"
                todo.agent_task_error = None
            self._sync_projection(todo)
        if series.status == "active":
            self.materialize_series(series, now=current, commit=False)
        self.session.commit()
        self.session.refresh(series)
        return series

    def series_payload(self, series: RecurringTodoSeries) -> dict[str, Any]:
        occurrences = self.session.scalars(
            select(Todo)
            .where(Todo.recurring_series_id == series.id)
            .order_by(Todo.recurrence_original_at)
        ).all()
        open_occurrences = [
            todo for todo in occurrences if todo.status not in {"done", "archived"}
        ]
        next_todo = min(
            open_occurrences,
            key=lambda item: item.due_at
            or item.scheduled_start_at
            or datetime.max.replace(tzinfo=UTC),
            default=None,
        )
        return {
            "id": str(series.id),
            "domain_id": str(series.domain_id) if series.domain_id else None,
            "title": series.title,
            "description": series.description,
            "recurrence_rule": series.recurrence_rule,
            "timezone": series.timezone,
            "due_anchor_at": _iso(series.due_anchor_at),
            "scheduled_anchor_at": _iso(series.scheduled_anchor_at),
            "estimated_minutes": series.estimated_minutes,
            "owner_type": series.owner_type,
            "owner_ref": series.owner_ref,
            "agent_task": series.agent_task,
            "priority": series.priority,
            "status": series.status,
            "next_todo_id": str(next_todo.id) if next_todo else None,
            "next_due_at": _iso(next_todo.due_at) if next_todo else None,
            "open_occurrence_count": len(open_occurrences),
            "completed_occurrence_count": sum(todo.status == "done" for todo in occurrences),
        }

    def _sync_projection(self, todo: Todo) -> None:
        from app.memory.todo_scheduling import TodoSchedulingService

        TodoSchedulingService(self.session).sync_projection(todo, commit=False)


def _series_anchor(series: RecurringTodoSeries) -> datetime:
    anchor = series.due_anchor_at or series.scheduled_anchor_at
    if anchor is None:
        raise ValueError("A recurring todo needs its first due or scheduled date and time.")
    return _aware(anchor)


def _occurrence_times(
    series: RecurringTodoSeries,
    occurrence_at: datetime,
) -> tuple[datetime | None, datetime | None]:
    anchor = _series_anchor(series)
    due_offset = _aware(series.due_anchor_at) - anchor if series.due_anchor_at else None
    scheduled_offset = (
        _aware(series.scheduled_anchor_at) - anchor if series.scheduled_anchor_at else None
    )
    return (
        occurrence_at + due_offset if due_offset is not None else None,
        occurrence_at + scheduled_offset if scheduled_offset is not None else None,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _aware(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _aware(parsed)


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/New_York")
    except ZoneInfoNotFoundError:
        return home_timezone()


def _bounded_minutes(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return min(480, max(5, int(value)))


def _positive_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value else None
