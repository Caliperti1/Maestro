from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Domain, RuntimeSetting, ToolConnection, WorkflowRun
from app.db.seed import seed_default_domains
from app.maestro.calendar_trigger import (
    CALENDAR_TRIGGER_CURSOR_PREFIX,
    CalendarSyncTokenExpired,
    CalendarTriggerService,
)
from app.maestro.scheduler import SchedulerService


class FakeCalendarChangeSource:
    def __init__(self) -> None:
        self.version = "v1"
        self.calls: list[dict[str, Any]] = []

    def changes_page(
        self,
        connection: ToolConnection,
        *,
        sync_token: str | None,
        page_token: str | None,
        page_size: int,
        bootstrap_at=None,
    ) -> dict[str, Any]:
        self.calls.append({
            "sync_token": sync_token,
            "page_token": page_token,
            "page_size": page_size,
            "bootstrap": bootstrap_at is not None,
        })
        if sync_token is None:
            return {"items": [], "nextSyncToken": "sync-1"}
        return {
            "items": [{
                "id": "event-1",
                "etag": self.version,
                "status": "confirmed",
                "summary": "Partner review",
                "start": {"dateTime": "2026-08-24T14:00:00-04:00"},
                "end": {"dateTime": "2026-08-24T14:30:00-04:00"},
            }],
            "nextSyncToken": f"sync-{self.version}",
        }


class ExpiredCalendarChangeSource(FakeCalendarChangeSource):
    def changes_page(self, connection, *, sync_token, page_token, page_size, bootstrap_at=None):
        if sync_token:
            raise CalendarSyncTokenExpired("Calendar sync token expired.")
        return super().changes_page(
            connection,
            sync_token=sync_token,
            page_token=page_token,
            page_size=page_size,
            bootstrap_at=bootstrap_at,
        )


def _seed_calendar_trigger(session: Session) -> Domain:
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None
    session.add(ToolConnection(
        domain_id=domain.id,
        tool_key="google",
        display_name="Praxis Google Workspace",
        auth_type="oauth",
        config={"calendar_id": "primary", "access_token": "fake"},
        is_active=True,
    ))
    session.commit()
    SchedulerService(session).upsert_definition(
        key="praxis-calendar-monitor",
        name="Praxis Calendar Monitor",
        domain_id=domain.id,
        trigger_type="event",
        trigger_config={
            "event_type": "google.calendar.event.changed",
            "filters": {"domain_key": "praxis"},
            "calendar_watch_enabled": True,
        },
        workflow_spec={"queue_items": [{
            "id": "calendar-sync",
            "objective": "Synchronize the exact calendar event.",
            "domain_key": "praxis",
            "agent_key": "praxis-calendar-agent",
        }]},
        fairness_group="praxis",
    )
    return domain


def test_calendar_trigger_bootstraps_then_versions_exact_event_runs(session: Session) -> None:
    _seed_calendar_trigger(session)
    source = FakeCalendarChangeSource()
    service = CalendarTriggerService(session, source=source)

    initialized = service.poll_once(page_size=25)
    first = service.poll_once(page_size=25)
    duplicate = service.poll_once(page_size=25)
    source.version = "v2"
    changed = service.poll_once(page_size=25)

    assert initialized["domains"][0]["status"] == "initialized"
    assert first["emitted_count"] == duplicate["emitted_count"] == changed["emitted_count"] == 1
    runs = session.scalars(select(WorkflowRun).order_by(WorkflowRun.created_at)).all()
    assert len(runs) == 2
    assert runs[0].input_payload["event"]["payload"]["event_id"] == "event-1"
    assert runs[0].input_payload["event"]["payload"]["event_version"] == "v1"
    assert runs[1].input_payload["event"]["payload"]["event_version"] == "v2"


def test_calendar_trigger_resets_expired_token_without_emitting(session: Session) -> None:
    domain = _seed_calendar_trigger(session)
    session.add(RuntimeSetting(
        key=f"{CALENDAR_TRIGGER_CURSOR_PREFIX}{domain.key}",
        value={"domain_key": domain.key, "sync_token": "expired", "status": "healthy"},
    ))
    session.commit()

    result = CalendarTriggerService(session, source=ExpiredCalendarChangeSource()).poll_once()

    assert result["emitted_count"] == 0
    assert result["domains"][0]["status"] == "token_reset"
    assert session.scalars(select(WorkflowRun)).all() == []


def test_calendar_trigger_skips_historical_changes(session: Session) -> None:
    _seed_calendar_trigger(session)
    source = FakeCalendarChangeSource()
    service = CalendarTriggerService(session, source=source)
    service.poll_once()
    past = datetime.now(UTC) - timedelta(days=2)
    original = source.changes_page

    def past_page(connection, *, sync_token, page_token, page_size, bootstrap_at=None):
        if sync_token is None:
            return original(
                connection,
                sync_token=sync_token,
                page_token=page_token,
                page_size=page_size,
                bootstrap_at=bootstrap_at,
            )
        return {
            "items": [{
                "id": "past-event",
                "etag": "past-v1",
                "status": "confirmed",
                "summary": "Past meeting",
                "start": {"dateTime": past.isoformat()},
                "end": {"dateTime": (past + timedelta(minutes=30)).isoformat()},
            }],
            "nextSyncToken": "sync-past",
        }

    source.changes_page = past_page
    result = service.poll_once()

    assert result["emitted_count"] == 0
    assert result["domains"][0]["skipped_past_count"] == 1
    assert session.scalars(select(WorkflowRun)).all() == []
