from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CalendarEvent, Domain, RoutedItem
from app.db.seed import seed_default_domains
from app.maestro.calendar_sync import stage_google_calendar_event
from app.memory.routed_service import RoutedMemoryService


def _provider_item(domain: Domain, *, version: str, title: str, start_at: str, status: str) -> RoutedItem:
    return RoutedItem(
        domain_id=domain.id,
        route_type="event",
        title=title,
        content=title,
        priority="normal",
        status=status,
        source_refs=[{
            "type": "google_calendar_event",
            "calendar_id": "primary",
            "event_id": "google-event-1",
            "event_version": version,
        }],
        metadata_={
            "start_at": start_at,
            "end_at": "2026-08-24T15:00:00-04:00",
            "timezone": "America/New_York",
            "external_provider": "google_calendar",
            "external_calendar_id": "primary",
            "external_event_id": "google-event-1",
            "external_etag": version,
            "sync_status": "synced",
        },
    )


def test_provider_calendar_changes_update_one_canonical_event(session: Session) -> None:
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None
    first = _provider_item(
        domain,
        version="v1",
        title="Partner review",
        start_at="2026-08-24T14:00:00-04:00",
        status="open",
    )
    session.add(first)
    session.flush()
    first_result = RoutedMemoryService(session, enable_llm_resolver=False).promote_item(first)
    session.commit()

    changed = _provider_item(
        domain,
        version="v2",
        title="Partner review moved",
        start_at="2026-08-24T14:30:00-04:00",
        status="cancelled",
    )
    session.add(changed)
    session.flush()
    changed_result = RoutedMemoryService(session, enable_llm_resolver=False).promote_item(changed)
    session.commit()

    events = session.scalars(select(CalendarEvent)).all()
    assert len(events) == 1
    assert first_result is not None and changed_result is not None
    assert first_result.object_id == changed_result.object_id == events[0].id
    assert events[0].title == "Partner review moved"
    assert events[0].start_at.strftime("%Y-%m-%dT%H:%M") == "2026-08-24T14:30"
    assert events[0].status == "cancelled"
    assert events[0].external_etag == "v2"


def test_cancelled_provider_tombstone_does_not_create_visible_event(session: Session) -> None:
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None

    result = stage_google_calendar_event(
        session,
        domain=domain,
        event_payload={
            "calendar_id": "primary",
            "event_id": "deleted-series_20260826T160000Z",
            "event_version": "v1",
            "google_event": {
                "id": "deleted-series_20260826T160000Z",
                "status": "cancelled",
                "etag": "v1",
                "summary": "CANCELLED",
                "start": {"dateTime": "2026-08-26T12:00:00-04:00"},
                "end": {"dateTime": "2026-08-26T13:00:00-04:00"},
            },
        },
    )

    assert result == {"status": "ignored_tombstone", "event_id": None}
    assert session.query(CalendarEvent).count() == 0
    assert session.query(RoutedItem).count() == 0


def test_cancelled_provider_tombstone_updates_existing_event_without_dates(
    session: Session,
) -> None:
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None
    start = datetime.now(UTC) + timedelta(days=2)
    created = stage_google_calendar_event(
        session,
        domain=domain,
        event_payload={
            "calendar_id": "primary",
            "event_id": "google-event-1",
            "event_version": "v1",
            "google_event": {
                "id": "google-event-1",
                "status": "confirmed",
                "etag": "v1",
                "summary": "Partner review",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
            },
        },
    )

    deleted = stage_google_calendar_event(
        session,
        domain=domain,
        event_payload={
            "calendar_id": "primary",
            "event_id": "google-event-1",
            "event_version": "v2",
            "google_event": {
                "id": "google-event-1",
                "status": "cancelled",
                "etag": "v2",
            },
        },
    )

    event = session.scalar(
        select(CalendarEvent).where(CalendarEvent.external_event_id == "google-event-1")
    )
    assert deleted == {"status": "updated", "event_id": created["event_id"]}
    assert event is not None
    assert event.status == "cancelled"
    assert event.external_etag == "v2"
    assert event.metadata_["provider_tombstone_event_id"] == "google-event-1"


def test_series_master_tombstone_cancels_future_occurrences(session: Session) -> None:
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None
    start = datetime.now(UTC) + timedelta(days=2)
    for index in range(2):
        occurrence_start = start + timedelta(days=7 * index)
        result = stage_google_calendar_event(
            session,
            domain=domain,
            event_payload={
                "calendar_id": "primary",
                "event_id": f"series-1-occurrence-{index}",
                "event_version": "v1",
                "google_event": {
                    "id": f"series-1-occurrence-{index}",
                    "recurringEventId": "series-1",
                    "status": "confirmed",
                    "etag": "v1",
                    "summary": "Weekly sync",
                    "start": {"dateTime": occurrence_start.isoformat()},
                    "end": {
                        "dateTime": (occurrence_start + timedelta(minutes=30)).isoformat()
                    },
                },
            },
        )
        assert result["event_id"]

    deleted = stage_google_calendar_event(
        session,
        domain=domain,
        event_payload={
            "calendar_id": "primary",
            "event_id": "series-1",
            "event_version": "v2",
            "google_event": {
                "id": "series-1",
                "status": "cancelled",
                "etag": "v2",
            },
        },
    )

    events = session.scalars(select(CalendarEvent)).all()
    assert deleted["status"] == "cancelled_series"
    assert deleted["updated_count"] == 2
    assert all(event.status == "cancelled" for event in events)
    assert all(event.external_etag == "v2" for event in events)
