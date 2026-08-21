from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CalendarEvent, Domain, RoutedItem
from app.db.seed import seed_default_domains
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
