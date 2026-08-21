import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import (
    Artifact,
    CalendarEvent,
    CalendarEventAttendee,
    CalendarEventOrganization,
    Contact,
    ContactAlias,
    ContactDomainNote,
    ContactInteraction,
    ContactRelationship,
    Entity,
    MemoryItem,
    MemoryProposal,
    OrganizationIdentifier,
    OrganizationRelationship,
    RoutedItem,
    SeedPackage,
    Todo,
)
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.memory.dropbox import MemoryDropboxProcessor
from app.memory.routed_hygiene import RoutedHygieneService
from app.memory.routed_resolver import RoutedObjectResolver
from app.memory.routed_service import RoutedMemoryService


def test_todo_resolver_matches_retry_from_same_source_with_rephrased_title(
    session: Session,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    existing = Todo(
        domain_id=praxis.id,
        title="Update Praxis business card design with textured option",
        description="Ask Chris's brother about adding texture and share reference pictures.",
        todo_type="task",
        owner_type="user",
        priority="normal",
        status="open",
        source_refs=[{"system": "gmail", "message_id": "msg-daily-sync-1"}],
        provenance={},
        metadata_={"source_message_id": "msg-daily-sync-1"},
    )
    candidate = RoutedItem(
        domain_id=praxis.id,
        route_type="task",
        title="Update business card design with brother",
        content="Ask Chris's brother to add texture to the business card and send reference pictures.",
        priority="normal",
        status="open",
        source_refs=[{"id": "msg-daily-sync-1", "type": "gmail_message"}],
        metadata_={"source_message_id": "msg-daily-sync-1"},
    )
    session.add_all([existing, candidate])
    session.commit()

    decision = RoutedObjectResolver(session, enable_llm=False).resolve_todo(
        candidate,
        due_at=None,
    )

    assert decision.action == "update_existing"
    assert decision.object_id == existing.id
    assert decision.strategy == "source_context"


def _client(session: Session, tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)

    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_memory_status_and_upload(session: Session, tmp_path: Path) -> None:
    client = _client(session, tmp_path)

    status = client.get("/memory/dropbox/status")
    assert status.status_code == 200
    assert status.json()["root"] == str(tmp_path)
    assert any(domain["key"] == "perti-laboratories" for domain in status.json()["domains"])
    assert (
        next(
            domain for domain in status.json()["domains"] if domain["key"] == "perti-laboratories"
        )["processing"]
        == 0
    )

    upload = client.post(
        "/memory/dropbox/perti-laboratories/upload",
        files={"file": ("note.md", b"# Perti Laboratories note\nMemory test.", "text/markdown")},
    )

    assert upload.status_code == 200
    assert upload.json()["status"] == "uploaded"
    assert (tmp_path / "perti-laboratories" / "inbox" / "note.md").is_file()


def test_ingestion_status_lists_registered_sources(session: Session, tmp_path: Path) -> None:
    MemoryDropboxProcessor(session, root=tmp_path).ensure_directories()
    client = _client(session, tmp_path)

    response = client.get("/memory/ingestion/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_registrations"] == 7
    assert payload["records"] == {}
    assert payload["duplicates_skipped"] == 0
    assert payload["recent"] == []


def test_memory_dropbox_consolidates_legacy_perti_directories(
    session: Session,
    tmp_path: Path,
) -> None:
    (tmp_path / "ophi" / "inbox").mkdir(parents=True)
    (tmp_path / "personal-irad-projects" / "inbox").mkdir(parents=True)
    (tmp_path / "ophi" / "inbox" / "product.md").write_text(
        "Ophi product context", encoding="utf-8"
    )
    (tmp_path / "personal-irad-projects" / "inbox" / "research.md").write_text(
        "Personal research context",
        encoding="utf-8",
    )

    MemoryDropboxProcessor(session, root=tmp_path).ensure_directories()

    assert (tmp_path / "perti-laboratories" / "inbox" / "product.md").read_text() == (
        "Ophi product context"
    )
    assert (tmp_path / "perti-laboratories" / "inbox" / "research.md").read_text() == (
        "Personal research context"
    )
    assert not (tmp_path / "ophi").exists()
    assert not (tmp_path / "personal-irad-projects").exists()


def test_calendar_create_links_contacts_organizations_and_reports_conflicts(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    perti = DomainRepository(session).get_by_key("perti-laboratories")
    assert praxis is not None and perti is not None
    contact = Contact(
        name="Jane Smith",
        normalized_name="jane smith",
        email="jane@example.com",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    organization = Entity(
        name="Example Corp",
        normalized_name="example corp",
        website="https://example.com",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    conflict = CalendarEvent(
        domain_id=perti.id,
        title="Perti product review",
        start_at=datetime(2030, 8, 12, 14, 30, tzinfo=UTC),
        end_at=datetime(2030, 8, 12, 15, 30, tzinfo=UTC),
        attendees=[],
        supporting_refs=[],
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add_all([contact, organization, conflict])
    session.commit()
    client = _client(session, tmp_path)

    response = client.post(
        "/memory/routed-objects/events",
        json={
            "domain_key": "praxis",
            "title": "Partner review with Jane Smith",
            "summary": "Review the updated Praxis partnership plan.",
            "start_at": "2030-08-12T14:00:00Z",
            "end_at": "2030-08-12T15:00:00Z",
            "timezone": "America/New_York",
            "conferencing_url": "https://meet.google.com/example",
            "attendees": [{"contact_id": str(contact.id), "name": "Jane Smith"}],
            "organizations": [{"id": str(organization.id), "role": "partner"}],
        },
    )

    assert response.status_code == 200
    event = response.json()["event"]
    assert event["attendees"][0]["contact_id"] == str(contact.id)
    assert event["organizations"] == [
        {"id": str(organization.id), "name": "Example Corp", "role": "partner"}
    ]
    assert event["conflicts"][0]["title"] == "Perti product review"
    assert session.query(CalendarEventAttendee).count() == 1
    assert session.query(CalendarEventOrganization).count() == 1

    invalid_window = client.patch(
        f"/memory/routed-objects/events/{event['id']}",
        json={"updates": {"end_at": "2030-08-12T13:00:00Z"}},
    )
    assert invalid_window.status_code == 422

    contact_response = client.get(
        "/memory/routed-objects/contacts",
        params={"query_text": "Jane Smith", "use_semantic": "false"},
    )
    assert contact_response.status_code == 200
    assert contact_response.json()["contacts"][0]["upcoming_events"][0]["title"] == (
        "Partner review with Jane Smith"
    )


def test_calendar_context_window_is_nonblocking_and_keeps_soft_scheduling_semantics(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)

    context_response = client.post(
        "/memory/routed-objects/events",
        json={
            "domain_key": "personal",
            "title": "Household quiet time",
            "summary": "Kids usually nap during this window.",
            "start_at": "2030-08-12T13:00:00-04:00",
            "end_at": "2030-08-12T15:00:00-04:00",
            "timezone": "America/New_York",
            "item_kind": "context_window",
            "context_type": "childcare",
            "scheduling_effect": "prefer_avoid",
            "blocks_time": True,
        },
    )

    assert context_response.status_code == 200
    context = context_response.json()["event"]
    assert context["item_kind"] == "context_window"
    assert context["context_type"] == "childcare"
    assert context["scheduling_effect"] == "prefer_avoid"
    assert context["blocks_time"] is False
    assert context["conflicts"] == []

    event_response = client.post(
        "/memory/routed-objects/events",
        json={
            "domain_key": "personal",
            "title": "Quick phone call",
            "start_at": "2030-08-12T13:30:00-04:00",
            "end_at": "2030-08-12T14:00:00-04:00",
            "timezone": "America/New_York",
        },
    )

    assert event_response.status_code == 200
    event = event_response.json()["event"]
    assert event["blocks_time"] is True
    assert event["conflicts"] == []


def test_calendar_context_window_rejects_unknown_semantics(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    response = _client(session, tmp_path).post(
        "/memory/routed-objects/events",
        json={
            "domain_key": "personal",
            "title": "Unclear context",
            "item_kind": "context_window",
            "context_type": "invented",
        },
    )

    assert response.status_code == 422


def test_calendar_past_event_materializes_one_contact_interaction(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    contact = Contact(
        name="Jane Smith",
        normalized_name="jane smith",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(contact)
    session.commit()
    client = _client(session, tmp_path)
    created = client.post(
        "/memory/routed-objects/events",
        json={
            "domain_key": "praxis",
            "title": "Completed partner review",
            "summary": "Reviewed partnership milestones.",
            "start_at": "2020-08-12T14:00:00Z",
            "end_at": "2020-08-12T15:00:00Z",
            "attendees": [{"contact_id": str(contact.id), "name": contact.name}],
        },
    )
    assert created.status_code == 200
    event_id = created.json()["event"]["id"]

    interactions = session.scalars(
        select(ContactInteraction).where(
            ContactInteraction.calendar_event_id == uuid.UUID(event_id)
        )
    ).all()
    assert len(interactions) == 1
    assert interactions[0].contact_id == contact.id
    assert interactions[0].summary == "Reviewed partnership milestones."

    rejected = client.patch(
        f"/memory/routed-objects/events/{event_id}",
        json={"updates": {"status": "done"}},
    )
    assert rejected.status_code == 422

    cancelled = client.patch(
        f"/memory/routed-objects/events/{event_id}",
        json={"updates": {"status": "cancelled"}},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["event"]["status"] == "cancelled"


def test_organization_intelligence_resolves_identifiers_and_relationships(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    parent = Entity(
        name="Example Holdings",
        normalized_name="example holdings",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(parent)
    session.commit()
    routed = RoutedItem(
        domain_id=praxis.id,
        route_type="entity",
        title="Example Corp",
        content="Example Corp is a Praxis partner and subsidiary of Example Holdings.",
        priority="normal",
        status="open",
        source_refs=[{"type": "gmail_message", "id": "organization-1"}],
        metadata_={
            "entity_name": "Example Corp",
            "website": "https://example.com",
            "email_domain": "example.com",
            "aliases": ["Example"],
            "relationships": [
                {
                    "organization_id": str(parent.id),
                    "relationship_type": "subsidiary_of",
                    "description": "Example Corp is a subsidiary of Example Holdings.",
                }
            ],
        },
    )
    session.add(routed)
    session.commit()
    RoutedMemoryService(session).promote_items([routed])
    client = _client(session, tmp_path)

    response = client.get(
        "/memory/routed-objects/entities",
        params={"query_text": "example.com", "domain_key": "praxis", "use_semantic": "false"},
    )

    assert response.status_code == 200
    organization = response.json()["entities"][0]
    assert organization["name"] == "Example Corp"
    assert {item["type"] for item in organization["identifiers"]} == {
        "website",
        "web_domain",
        "email_domain",
    }
    assert organization["relationships"][0]["organization"] == "Example Holdings"
    assert session.query(OrganizationIdentifier).count() == 3
    assert session.query(OrganizationRelationship).count() == 1


def test_memory_preview_listing(session: Session, tmp_path: Path) -> None:
    client = _client(session, tmp_path)
    preview_dir = tmp_path / "perti-laboratories" / "previews"
    preview_dir.mkdir(parents=True)
    (preview_dir / "note.preview.json").write_text(
        """
        {
          "source_file": "note.md",
          "status": "written",
          "candidates": [{}],
          "routed_items": [{"route_type": "human_input"}],
          "results": [{"outcome": "written", "memory_item_id": "memory-1"}]
        }
        """,
        encoding="utf-8",
    )

    response = client.get("/memory/dropbox/previews?domain_key=perti-laboratories")

    assert response.status_code == 200
    previews = response.json()["previews"]
    assert len(previews) == 1
    assert previews[0]["source_file"] == "note.md"
    assert previews[0]["candidate_count"] == 1
    assert previews[0]["routed_count"] == 1
    assert previews[0]["result_count"] == 1
    assert previews[0]["progress_count"] == 1
    assert previews[0]["progress_total"] == 1
    assert previews[0]["is_processing"] is False
    assert previews[0]["written_count"] == 1


def test_routed_items_endpoint_filters_by_domain_and_type(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    ophi = DomainRepository(session).get_by_key("perti-laboratories")
    assert praxis is not None
    assert ophi is not None
    session.add_all(
        [
            RoutedItem(
                domain_id=praxis.id,
                route_type="human_input",
                title="Confirm Praxis RFI",
                content="Chris needs to answer the Praxis RFI.",
                priority="high",
                status="open",
                source_refs=[],
                metadata_={},
            ),
            RoutedItem(
                domain_id=ophi.id,
                route_type="task",
                title="Perti Laboratories task",
                content="This should not appear in Praxis RFI filter.",
                priority="normal",
                status="open",
                source_refs=[],
                metadata_={},
            ),
        ]
    )
    session.commit()
    client = _client(session, tmp_path)

    response = client.get("/memory/routed-items?domain_key=praxis&route_type=human_input")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Confirm Praxis RFI"
    assert items[0]["domain_key"] == "praxis"


def test_routed_item_status_update_endpoint(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_item = RoutedItem(
        domain_id=praxis.id,
        route_type="task",
        title="Draft follow-up",
        content="Draft a partner follow-up email.",
        priority="normal",
        status="open",
        source_refs=[],
        metadata_={},
    )
    session.add(routed_item)
    session.commit()
    session.refresh(routed_item)
    client = _client(session, tmp_path)

    response = client.patch(
        f"/memory/routed-items/{routed_item.id}",
        json={"status": "done", "reason": "Completed in routed-item board."},
    )
    open_items = client.get("/memory/routed-items?domain_key=praxis")

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert response.json()["item"]["status"] == "done"
    assert response.json()["item"]["metadata"]["last_status_reason"] == (
        "Completed in routed-item board."
    )
    assert open_items.json()["items"] == []


def test_routed_items_endpoint_can_return_all_statuses(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    session.add_all(
        [
            RoutedItem(
                domain_id=praxis.id,
                route_type="human_input",
                title="Confirm owner",
                content="Chris needs to confirm the owner.",
                priority="normal",
                status="needs_input",
                source_refs=[],
                metadata_={},
            ),
            RoutedItem(
                domain_id=praxis.id,
                route_type="event",
                title="Partner sync",
                content="Partner sync is scheduled.",
                priority="normal",
                status="scheduled",
                source_refs=[],
                metadata_={},
            ),
        ]
    )
    session.commit()
    client = _client(session, tmp_path)

    open_only = client.get("/memory/routed-items?domain_key=praxis")
    all_statuses = client.get("/memory/routed-items?domain_key=praxis&status=all")

    assert open_only.status_code == 200
    assert open_only.json()["items"] == []
    assert all_statuses.status_code == 200
    assert {item["status"] for item in all_statuses.json()["items"]} == {
        "needs_input",
        "scheduled",
    }


def test_routed_objects_api_promotes_pending_items_before_returning_stores(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    session.add(
        RoutedItem(
            domain_id=praxis.id,
            route_type="contact",
            title="Capture Alice Park as Praxis contact",
            content="Alice Park is the Praxis technical lead.",
            priority="normal",
            status="open",
            source_refs=[],
            metadata_={},
        )
    )
    session.commit()
    client = _client(session, tmp_path)

    response = client.get("/memory/routed-objects/contacts")

    assert response.status_code == 200
    assert response.json()["contacts"][0]["name"] == "Alice Park"
    assert session.query(Contact).filter_by(name="Alice Park").count() == 1


def test_routed_objects_api_returns_canonical_stores(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    session.add_all(
        [
            Todo(
                domain_id=praxis.id,
                title="Draft partner follow-up",
                description="Draft a partner follow-up email.",
                todo_type="task",
                owner_type="maestro",
                priority="normal",
                status="open",
                source_refs=[],
                provenance={"created_from": "test"},
                metadata_={},
            ),
            CalendarEvent(
                domain_id=praxis.id,
                title="Partner sync",
                summary="Partner sync with Example Corp.",
                status="scheduled",
                attendees=[],
                supporting_refs=[],
                source_refs=[],
                provenance={"created_from": "test"},
                metadata_={},
            ),
            Contact(
                name="Jane Smith",
                normalized_name="jane smith",
                email="jane@example.com",
                summary="Partner lead at Example Corp.",
                scheduled_event_ids=[],
                source_refs=[],
                provenance={"created_from": "test"},
                metadata_={},
            ),
        ]
    )
    session.commit()
    client = _client(session, tmp_path)

    bundle = client.get("/memory/routed-objects?domain_key=praxis&query_text=partner")
    contacts = client.get("/memory/routed-objects/contacts")
    todos = client.get("/memory/routed-objects/todos?domain_key=praxis")

    assert bundle.status_code == 200
    assert bundle.json()["events"][0]["title"] == "Partner sync"
    assert bundle.json()["todos"][0]["title"] == "Draft partner follow-up"
    assert contacts.json()["contacts"][0]["name"] == "Jane Smith"
    assert todos.json()["todos"][0]["domain_key"] == "praxis"


def test_routed_context_returns_recurring_event_for_requested_date(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    l3 = DomainRepository(session).get_by_key("l3")
    assert l3 is not None
    session.add(
        CalendarEvent(
            domain_id=l3.id,
            title="Collaborative Autonomy Standup",
            start_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
            end_at=datetime(2026, 8, 24, 15, 30, tzinfo=UTC),
            timezone="America/New_York",
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20261231T235959Z",
            status="scheduled",
            attendees=[],
            supporting_refs=[],
            source_refs=[],
            provenance={"created_from": "test"},
            metadata_={},
        )
    )
    session.commit()

    response = _client(session, tmp_path).get(
        "/memory/routed-objects?domain_key=l3&query_text=2026-08-31"
    )

    assert response.status_code == 200
    assert response.json()["events"][0]["title"] == "Collaborative Autonomy Standup"
    assert response.json()["events"][0]["recurrence_rule"].endswith("20261231T235959Z")


def test_routed_memory_service_dedupes_contacts_and_links_entities(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_items = [
        RoutedItem(
            domain_id=praxis.id,
            route_type="contact",
            title="Jane Smith",
            content="Jane Smith is the partner lead at Example Corp. jane@example.com",
            priority="normal",
            status="open",
            source_refs=[{"type": "test", "id": "one"}],
            metadata_={},
        ),
        RoutedItem(
            domain_id=praxis.id,
            route_type="contact",
            title="Jane Smith",
            content="Jane Smith prefers short agendas before calls. jane@example.com",
            priority="normal",
            status="open",
            source_refs=[{"type": "test", "id": "two"}],
            metadata_={},
        ),
    ]
    session.add_all(routed_items)
    session.commit()

    results = RoutedMemoryService(session).promote_items(routed_items)

    assert len(results) == 2
    assert session.query(Contact).count() == 1
    contact = session.query(Contact).one()
    assert contact.email == "jane@example.com"
    assert "short agendas" in contact.summary
    assert session.query(Entity).one().name == "Example Corp"
    assert session.query(ContactDomainNote).one().domain_id == praxis.id


def test_routed_memory_service_normalizes_multiple_contact_emails(session: Session) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_item = RoutedItem(
        domain_id=praxis.id,
        route_type="contact",
        title="Brenden Shutt",
        content="Brenden Shutt is a Praxis contact.",
        priority="normal",
        status="open",
        source_refs=[{"type": "gmail_message", "id": "multiple-email-contact"}],
        metadata_={
            "name": "Brenden Shutt",
            "email": ["brenden.m.shutt.mil@army.mil", "brendenshutt@gmail.com"],
        },
    )
    session.add(routed_item)
    session.commit()

    RoutedMemoryService(session).promote_items([routed_item])

    contact = session.query(Contact).one()
    assert contact.email == "brenden.m.shutt.mil@army.mil"
    assert contact.metadata_["alternate_emails"] == ["brendenshutt@gmail.com"]


def test_routed_memory_service_resolves_contact_aliases(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    first = RoutedItem(
        domain_id=praxis.id,
        route_type="contact",
        title="Chris Flournoy",
        content="Chris Flournoy is the Praxis standup contact.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "one"}],
        metadata_={"organization": "Praxis"},
    )
    second = RoutedItem(
        domain_id=praxis.id,
        route_type="contact",
        title="Chris F",
        content="Chris F prefers short updates before the Praxis standup.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "two"}],
        metadata_={"organization": "Praxis"},
    )
    session.add_all([first, second])
    session.commit()

    results = RoutedMemoryService(session).promote_items([first, second])

    assert [result.action for result in results] == ["created", "updated"]
    assert session.query(Contact).count() == 1
    contact = session.query(Contact).one()
    assert contact.name == "Chris Flournoy"
    assert "short updates" in contact.summary
    assert "chris f" in contact.metadata_["aliases"]
    alias = session.query(ContactAlias).filter_by(normalized_alias="chris f").one()
    assert alias.contact_id == contact.id
    assert second.metadata_["resolution"]["strategy"] in {"initial_alias", "alias"}


def test_routed_memory_service_suppresses_maestro_user_contact(session: Session) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_item = RoutedItem(
        domain_id=praxis.id,
        route_type="contact",
        title="Chris Aliperti",
        content="Chris Aliperti can be reached at chris.aliperti@praxis-defense.com.",
        priority="normal",
        status="open",
        source_refs=[{"type": "gmail_message", "id": "self-contact"}],
        metadata_={"email": "chris.aliperti@praxis-defense.com"},
    )
    session.add(routed_item)
    session.commit()

    results = RoutedMemoryService(session).promote_items([routed_item])

    assert results == []
    assert session.query(Contact).count() == 0
    assert routed_item.status == "ignored"
    assert routed_item.metadata_["identity_resolution"]["identity"] == "maestro_user"


def test_event_attendees_keep_user_identity_out_of_contacts(session: Session) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_item = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Praxis planning call",
        content="Chris Aliperti and Chris Flournoy will attend the Praxis planning call.",
        priority="normal",
        status="open",
        source_refs=[{"type": "gmail_message", "id": "attendees"}],
        metadata_={
            "attendees": [
                {"name": "Chris Aliperti", "email": "chris.aliperti@praxis-defense.com"},
                {"name": "Chris Flournoy"},
            ]
        },
    )
    session.add(routed_item)
    session.commit()

    RoutedMemoryService(session).promote_items([routed_item])

    event = session.query(CalendarEvent).one()
    contacts = session.query(Contact).all()
    assert [contact.name for contact in contacts] == ["Chris Flournoy"]
    assert event.attendees[0] == {
        "name": "Chris Aliperti",
        "email": "chris.aliperti@praxis-defense.com",
        "is_user": True,
        "identity": "maestro_user",
    }
    assert event.attendees[1] == {
        "name": "Chris Flournoy",
        "contact_id": str(contacts[0].id),
    }


def test_contact_alias_edit_merges_empty_placeholder_and_relinks_events(
    session: Session,
    tmp_path: Path,
) -> None:
    target = Contact(
        name="Chris Flournoy",
        normalized_name="chris flournoy",
        email="flournoy@example.com",
        summary="Praxis contact.",
        scheduled_event_ids=[],
        source_refs=[],
        provenance={},
        metadata_={},
    )
    placeholder = Contact(
        name="Chris F",
        normalized_name="chris f",
        summary="Created from an event attendee.",
        scheduled_event_ids=[],
        source_refs=[],
        provenance={},
        metadata_={"created_from_attendee": True},
    )
    session.add_all([target, placeholder])
    session.flush()
    session.add(
        ContactAlias(
            contact_id=placeholder.id,
            alias="Chris F",
            normalized_alias="chris f",
            source="attendee",
            source_refs=[],
            metadata_={},
        )
    )
    event = CalendarEvent(
        title="Praxis sync",
        attendees=[{"name": "Chris F", "contact_id": str(placeholder.id)}],
        supporting_refs=[],
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(event)
    session.commit()
    client = _client(session, tmp_path)

    response = client.patch(
        f"/memory/routed-objects/contacts/{target.id}",
        json={"updates": {"aliases": ["Chris F"]}},
    )

    assert response.status_code == 200
    assert response.json()["contact"]["aliases"] == ["Chris F"]
    assert placeholder.status == "archived"
    alias = session.query(ContactAlias).filter_by(normalized_alias="chris f").one()
    assert alias.contact_id == target.id
    assert event.attendees == [{"name": "Chris Flournoy", "contact_id": str(target.id)}]


def test_contact_alias_edit_rejects_substantive_contact_collision(
    session: Session,
    tmp_path: Path,
) -> None:
    target = Contact(
        name="Chris Flournoy",
        normalized_name="chris flournoy",
        email="flournoy@example.com",
        scheduled_event_ids=[],
        source_refs=[],
        provenance={},
        metadata_={},
    )
    other = Contact(
        name="Chris F",
        normalized_name="chris f",
        email="another.chris@example.com",
        scheduled_event_ids=[],
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add_all([target, other])
    session.flush()
    session.add(
        ContactAlias(
            contact_id=other.id,
            alias="Chris F",
            normalized_alias="chris f",
            source="manual",
            source_refs=[],
            metadata_={},
        )
    )
    session.commit()
    client = _client(session, tmp_path)

    response = client.patch(
        f"/memory/routed-objects/contacts/{target.id}",
        json={"updates": {"aliases": ["Chris F"]}},
    )

    assert response.status_code == 409
    assert "already belongs to Chris F" in response.json()["detail"]
    assert other.status == "active"


def test_routed_memory_service_canonicalizes_capture_contact_title(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_item = RoutedItem(
        domain_id=praxis.id,
        route_type="contact",
        title="Capture Ben Daniels from XVIII Airborne Corps as Praxis engagement contact",
        content="Capture Ben Daniels from XVIII Airborne Corps as Praxis engagement contact.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "capture"}],
        metadata_={},
    )
    session.add(routed_item)
    session.commit()

    RoutedMemoryService(session).promote_items([routed_item])

    contact = session.query(Contact).one()
    assert contact.name == "Ben Daniels"
    assert "Capture Ben Daniels" in contact.summary
    assert session.query(Entity).one().name == "XVIII Airborne Corps"


def test_routed_memory_service_extracts_contact_relationship(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    jane = RoutedItem(
        domain_id=praxis.id,
        route_type="contact",
        title="Jane Smith",
        content="Jane Smith is a Praxis partner.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "jane"}],
        metadata_={},
    )
    ben = RoutedItem(
        domain_id=praxis.id,
        route_type="contact",
        title="Ben Daniels",
        content="Ben Daniels works with Jane Smith on Praxis follow-ups.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "ben"}],
        metadata_={},
    )
    session.add_all([jane, ben])
    session.commit()

    RoutedMemoryService(session).promote_items([jane, ben])

    relationship = session.query(ContactRelationship).one()
    assert relationship.description == "works with"
    assert relationship.contact_id == session.query(Contact).filter_by(name="Ben Daniels").one().id
    assert (
        relationship.related_contact_id
        == session.query(Contact).filter_by(name="Jane Smith").one().id
    )


def test_routed_memory_service_dedupes_events(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_items = [
        RoutedItem(
            domain_id=praxis.id,
            route_type="event",
            title="Praxis daily standup",
            content="Praxis daily standup with Chris F today at 1200.",
            priority="normal",
            status="open",
            source_refs=[{"type": "test", "id": "one"}],
            metadata_={},
        ),
        RoutedItem(
            domain_id=praxis.id,
            route_type="event",
            title="Praxis daily standup",
            content="Praxis daily standup with Chris F today at 1200.",
            priority="normal",
            status="open",
            source_refs=[{"type": "test", "id": "two"}],
            metadata_={},
        ),
    ]
    session.add_all(routed_items)
    session.commit()

    results = RoutedMemoryService(session).promote_items(routed_items)

    assert len(results) == 2
    assert session.query(CalendarEvent).count() == 1
    event = session.query(CalendarEvent).one()
    assert len(event.source_refs) == 2
    assert [result.action for result in results] == ["created", "updated"]


def test_routed_memory_service_canonicalizes_event_metadata_title(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_item = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Recorded meeting metadata",
        content="Meeting with Ben Daniels about the Praxis partner follow-up.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "meeting-metadata"}],
        metadata_={
            "event_title": "Partner follow-up with Ben Daniels",
            "summary": "Discuss Praxis partner follow-up.",
            "attendees": [{"name": "Ben Daniels"}],
            "location": "Zoom",
        },
    )
    session.add(routed_item)
    session.commit()

    RoutedMemoryService(session).promote_items([routed_item])

    event = session.query(CalendarEvent).one()
    assert event.title == "Partner follow-up with Ben Daniels"
    assert event.summary == "Discuss Praxis partner follow-up."
    assert event.location == "Zoom"
    contact = session.query(Contact).one()
    assert contact.name == "Ben Daniels"
    assert event.attendees == [{"name": "Ben Daniels", "contact_id": str(contact.id)}]


def test_routed_memory_service_enriches_event_fields_from_messy_text(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_item = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Capture event/calendar context",
        content=(
            "Meeting with Chris F at 12 over Google Meet about the Praxis finance plan. "
            "Join at https://meet.google.com/abc-defg-hij?hs=224."
        ),
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "messy-event"}],
        metadata_={},
    )
    session.add(routed_item)
    session.commit()

    RoutedMemoryService(session).promote_items([routed_item])

    event = session.query(CalendarEvent).one()
    contact = session.query(Contact).one()
    assert event.title == "Meeting with Chris F"
    assert event.start_at is not None
    assert event.end_at is not None
    assert (event.end_at - event.start_at).total_seconds() == 3600
    assert event.location == "Google Meet"
    assert event.conferencing_url == "https://meet.google.com/abc-defg-hij?hs=224"
    assert event.attendees == [{"name": "Chris F", "contact_id": str(contact.id)}]
    assert routed_item.metadata_["enrichment_source"] == "routed_item_enricher"


def test_routed_memory_service_maps_join_url_to_conferencing_url(
    session: Session,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    routed_item = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Project Plan Review",
        content="Meet with Ben over Microsoft Teams.",
        priority="normal",
        status="open",
        source_refs=[{"type": "gmail_message", "message_id": "meeting-link"}],
        metadata_={
            "join_url": "https://dod.teams.microsoft.us/meet/993833865306?p=example",
            "location": "Microsoft Teams",
        },
    )
    session.add(routed_item)
    session.commit()

    RoutedMemoryService(session).promote_items([routed_item])

    event = session.query(CalendarEvent).one()
    assert event.conferencing_url == ("https://dod.teams.microsoft.us/meet/993833865306?p=example")


def test_calendar_listing_backfills_conferencing_url_from_summary(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    event = CalendarEvent(
        title="Partner sync",
        summary="Join via Zoom: https://example.zoom.us/j/123456789?pwd=abc.",
        source_refs=[],
        supporting_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(event)
    session.commit()

    response = _client(session, tmp_path).get("/memory/routed-objects/events")

    assert response.status_code == 200
    assert response.json()["events"][0]["conferencing_url"] == (
        "https://example.zoom.us/j/123456789?pwd=abc"
    )


def test_routed_memory_service_prefers_explicit_event_and_todo_dates(
    session: Session,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    event_item = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Partner meeting with Jane Smith",
        content="Confirm the partner meeting and send the Praxis overview.",
        priority="normal",
        status="open",
        source_refs=[{"type": "gmail_message", "id": "msg-explicit-date"}],
        metadata_={
            "start_date": "2026-08-04",
            "start_time": "14:00:00",
            "timezone": "America/New_York",
            "attendees": [{"name": "Jane Smith"}],
        },
    )
    todo_item = RoutedItem(
        domain_id=praxis.id,
        route_type="task",
        title="Send Jane the Praxis overview",
        content="Send the overview before the partner meeting.",
        priority="normal",
        status="open",
        source_refs=[{"type": "gmail_message", "id": "msg-explicit-date"}],
        metadata_={
            "due_date": "2026-08-04",
            "due_time": "13:00:00",
            "timezone": "America/New_York",
        },
    )
    session.add_all([event_item, todo_item])
    session.commit()

    RoutedMemoryService(session).promote_items([event_item, todo_item])

    event = session.query(CalendarEvent).one()
    todo = session.query(Todo).one()
    assert event.start_at.replace(tzinfo=UTC) == datetime(2026, 8, 4, 18, 0, tzinfo=UTC)
    assert todo.due_at.replace(tzinfo=UTC) == datetime(2026, 8, 4, 17, 0, tzinfo=UTC)


def test_routed_memory_service_updates_incomplete_event_from_followup_reference(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    initial = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Recorded meeting metadata",
        content="Meeting with Ben Daniels about the Praxis partner follow-up.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "initial"}],
        metadata_={"attendees": [{"name": "Ben Daniels"}]},
    )
    followup = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Meeting with Ben",
        content="That meeting with Ben was at 2.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "followup"}],
        metadata_={"start_at": "2026-07-09T14:00:00-04:00", "attendees": [{"name": "Ben Daniels"}]},
    )
    session.add_all([initial, followup])
    session.commit()

    results = RoutedMemoryService(session).promote_items([initial, followup])

    assert [result.action for result in results] == ["created", "updated"]
    assert session.query(CalendarEvent).count() == 1
    event = session.query(CalendarEvent).one()
    assert event.title == "Meeting with Ben Daniels"
    assert event.start_at is not None
    assert "That meeting with Ben was at 2" in event.summary
    contact = session.query(Contact).one()
    assert event.attendees == [{"name": "Ben Daniels", "contact_id": str(contact.id)}]


def test_routed_memory_service_resolves_events_by_time_and_title(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    start_at = "2026-07-10T16:00:00Z"
    first = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Praxis standup",
        content="Praxis standup with Chris Flournoy.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "one"}],
        metadata_={"start_at": start_at},
    )
    second = RoutedItem(
        domain_id=praxis.id,
        route_type="event",
        title="Praxis standup with Chris F",
        content="Same Praxis standup now includes finance-plan discussion.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "two"}],
        metadata_={"start_at": start_at},
    )
    session.add_all([first, second])
    session.commit()

    results = RoutedMemoryService(session).promote_items([first, second])

    assert [result.action for result in results] == ["created", "updated"]
    assert session.query(CalendarEvent).count() == 1
    event = session.query(CalendarEvent).one()
    assert "finance-plan" in event.summary
    assert second.metadata_["resolution"]["strategy"] in {"time_title", "llm_resolver"}


def test_routed_memory_service_resolves_todo_updates(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    first = RoutedItem(
        domain_id=praxis.id,
        route_type="task",
        title="Draft partner follow-up email",
        content="Draft the partner follow-up email.",
        priority="normal",
        status="open",
        source_refs=[{"type": "test", "id": "one"}],
        metadata_={"due_at": "2026-07-10T17:00:00Z"},
    )
    second = RoutedItem(
        domain_id=praxis.id,
        route_type="task",
        title="Partner follow-up email",
        content="Update the partner follow-up email with the finance-plan context.",
        priority="high",
        status="open",
        source_refs=[{"type": "test", "id": "two"}],
        metadata_={"due_at": "2026-07-10T17:00:00Z"},
    )
    session.add_all([first, second])
    session.commit()

    results = RoutedMemoryService(session).promote_items([first, second])

    assert [result.action for result in results] == ["created", "updated"]
    assert session.query(Todo).count() == 1
    todo = session.query(Todo).one()
    assert todo.priority == "high"
    assert "finance-plan context" in todo.description
    assert todo.due_at is not None
    assert todo.due_at.replace(tzinfo=UTC) == datetime(2026, 7, 10, 17, 0, tzinfo=UTC)


def test_routed_retrieval_and_edit_services(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    contact = Contact(
        name="Ben Daniels",
        normalized_name="ben daniels",
        summary="Ben Daniels supports Praxis engagement.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    todo = Todo(
        domain_id=praxis.id,
        title="Draft partner follow-up",
        description="Draft follow-up for Ben Daniels.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    entity = Entity(
        name="Example Corp",
        normalized_name="example corp",
        summary="Praxis partner organization.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add_all([contact, todo, entity])
    session.commit()

    client = _client(session, tmp_path)
    context = client.get("/memory/routed-context?domain_key=praxis&query_text=Ben&max_chars=1000")

    assert context.status_code == 200
    assert "Ben Daniels" in context.json()["rendered_text"]

    update = client.patch(
        f"/memory/routed-objects/contacts/{contact.id}",
        json={"updates": {"summary": "Updated Praxis engagement contact."}},
    )

    assert update.status_code == 200
    assert update.json()["contact"]["summary"] == "Updated Praxis engagement contact."

    todo_update = client.patch(
        f"/memory/routed-objects/todos/{todo.id}",
        json={"updates": {"due_at": "2026-07-10T17:00:00+00:00", "status": "in_progress"}},
    )
    entity_update = client.patch(
        f"/memory/routed-objects/entities/{entity.id}",
        json={"updates": {"name": "Example Corporation", "website": "https://example.com"}},
    )

    assert todo_update.status_code == 200
    assert todo_update.json()["todo"]["status"] == "in_progress"
    assert todo_update.json()["todo"]["due_at"].startswith("2026-07-10T17:00:00")
    assert entity_update.status_code == 200
    assert entity_update.json()["entity"]["name"] == "Example Corporation"


def test_routed_hygiene_backfills_aliases_and_suggests_duplicates(
    session: Session,
    tmp_path: Path,
) -> None:
    contacts = [
        Contact(
            name="Ben Daniels",
            normalized_name="ben daniels",
            email="ben@example.com",
            summary="One",
            source_refs=[],
            provenance={},
            metadata_={},
        ),
        Contact(
            name="Ben Daniels",
            normalized_name="ben daniels",
            email="ben.alt@example.com",
            summary="Two",
            source_refs=[],
            provenance={},
            metadata_={},
        ),
    ]
    session.add_all(contacts)
    session.commit()

    report = RoutedHygieneService(session).run_once()

    assert report.aliases_backfilled == 1
    assert report.duplicates_merged == 0
    assert session.query(Contact).filter(Contact.status != "archived").count() == 2
    assert any(item["reason"] == "same_name" for item in report.suggestions)


def test_routed_hygiene_canonicalizes_display_fields(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    contact = Contact(
        name="Capture Ben Daniels from XVIII Airborne Corps as Praxis engagement contact",
        normalized_name="capture ben daniels from xviii airborne corps as praxis engagement contact",
        summary="Ben Daniels is associated with XVIII Airborne Corps.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    event = CalendarEvent(
        domain_id=praxis.id,
        title="Record meeting metadata: Ben Daniels meeting",
        summary="Meeting with Ben Daniels occurred yesterday at 2 PM over Google Meet.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add_all([contact, event])
    session.commit()

    report = RoutedHygieneService(session).run_once()

    assert report.display_fields_canonicalized == 2
    assert session.query(Contact).one().name == "Ben Daniels"
    assert session.query(CalendarEvent).one().title == "Meeting with Ben Daniels"


def test_routed_hygiene_cleans_identifier_names_and_synthetic_aliases(
    session: Session,
) -> None:
    contact = Contact(
        name="william.r.rollins2.mil@army.mil",
        normalized_name="william r rollins2 mil army mil",
        email="william.r.rollins2.mil@army.mil",
        source_refs=[],
        provenance={},
        metadata_={"aliases": ["w r", "Will Rollins"]},
    )
    entity = Entity(
        name="praxis-defense.com",
        normalized_name="praxis defense com",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add_all([contact, entity])
    session.flush()
    session.add_all(
        [
            ContactAlias(
                contact_id=contact.id,
                alias="w r",
                normalized_alias="w r",
                source="routed_promote",
                source_refs=[],
                metadata_={},
            ),
            ContactAlias(
                contact_id=contact.id,
                alias="Will Rollins",
                normalized_alias="will rollins",
                source="routed_promote",
                source_refs=[],
                metadata_={},
            ),
        ]
    )
    session.commit()

    report = RoutedHygieneService(session).run_once()

    session.refresh(contact)
    session.refresh(entity)
    assert contact.name == "William R Rollins"
    assert contact.email == "william.r.rollins2.mil@army.mil"
    assert entity.name == "Praxis Defense"
    assert report.aliases_pruned >= 2
    assert (
        session.scalar(select(ContactAlias).where(ContactAlias.normalized_alias == "w r")) is None
    )
    assert (
        session.scalar(select(ContactAlias).where(ContactAlias.normalized_alias == "will rollins"))
        is not None
    )


def test_routed_hygiene_merges_contact_with_email_embedded_in_display_name(
    session: Session,
) -> None:
    canonical = Contact(
        name="Todd Poindexter",
        normalized_name="todd poindexter",
        email="todd.l.poindexter2.mil@army.mil",
        source_refs=[{"id": "canonical"}],
        provenance={},
        metadata_={},
    )
    duplicate = Contact(
        name="Todd L. Poindexter <todd.l.poindexter2.mil@army.mil>",
        normalized_name="todd l poindexter todd l poindexter2 mil army mil",
        source_refs=[{"id": "observed-header"}],
        provenance={},
        metadata_={},
    )
    session.add_all([canonical, duplicate])
    session.commit()

    report = RoutedHygieneService(session).run_once()

    assert report.duplicates_merged == 1
    assert duplicate.status == "archived"
    alias = session.scalar(
        select(ContactAlias).where(ContactAlias.normalized_alias == "todd l poindexter")
    )
    assert alias is not None
    assert alias.contact_id == canonical.id
    assert alias.source == "duplicate_merge"
    assert session.scalar(select(ContactAlias).where(ContactAlias.alias.contains("@"))) is None


def test_routed_hygiene_repairs_serialized_contact_email_list(session: Session) -> None:
    contact = Contact(
        name="Brenden M. Shutt",
        normalized_name="brenden m shutt",
        email="['brenden.m.shutt.mil@army.mil', 'brendenshutt@gmail.com']",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(contact)
    session.commit()

    RoutedHygieneService(session).run_once()

    session.refresh(contact)
    assert contact.email == "brenden.m.shutt.mil@army.mil"
    assert contact.metadata_["alternate_emails"] == ["brendenshutt@gmail.com"]


def test_routed_hygiene_merges_high_confidence_duplicates(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    first_contact = Contact(
        name="Ben Daniels",
        normalized_name="ben daniels",
        email="ben@example.com",
        summary="First note.",
        source_refs=[{"id": "contact-one"}],
        provenance={},
        metadata_={},
    )
    second_contact = Contact(
        name="Ben Daniels",
        normalized_name="ben daniels",
        email=None,
        summary="Second note.",
        source_refs=[{"id": "contact-two"}],
        provenance={},
        metadata_={},
    )
    first_event = CalendarEvent(
        domain_id=praxis.id,
        title="Partner sync",
        summary="First event note.",
        start_at=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
        source_refs=[{"id": "event-one"}],
        provenance={},
        metadata_={},
    )
    second_event = CalendarEvent(
        domain_id=praxis.id,
        title="Partner sync",
        summary="Second event note.",
        start_at=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
        source_refs=[{"id": "event-two"}],
        provenance={},
        metadata_={},
    )
    first_todo = Todo(
        domain_id=praxis.id,
        title="Draft follow-up",
        description="First todo note.",
        source_refs=[{"id": "todo-one"}],
        provenance={},
        metadata_={},
    )
    second_todo = Todo(
        domain_id=praxis.id,
        title="Draft follow-up",
        description="Second todo note.",
        source_refs=[{"id": "todo-two"}],
        provenance={},
        metadata_={},
    )
    session.add_all(
        [first_contact, second_contact, first_event, second_event, first_todo, second_todo]
    )
    session.commit()

    report = RoutedHygieneService(session).run_once()

    assert report.duplicates_merged == 3
    contacts = session.query(Contact).all()
    events = session.query(CalendarEvent).all()
    todos = session.query(Todo).all()
    assert len([contact for contact in contacts if contact.status != "archived"]) == 1
    assert "Second note" in next(
        contact for contact in contacts if contact.status != "archived"
    ).summary
    assert len([event for event in events if event.status != "archived"]) == 1
    assert len([todo for todo in todos if todo.status != "archived"]) == 1
    assert (
        "Second event note" in next(event for event in events if event.status != "archived").summary
    )
    assert (
        "Second todo note" in next(todo for todo in todos if todo.status != "archived").description
    )


def test_archive_memory_item_endpoint_hides_from_default_list(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    memory = MemoryItem(
        scope="domain",
        domain_id=praxis.id,
        memory_type="fact",
        title="Temporary API memory",
        content="This should be archived by the API.",
        impact_level="low",
        importance=0.5,
        metadata_={},
    )
    session.add(memory)
    session.commit()
    session.refresh(memory)
    client = _client(session, tmp_path)

    archive = client.request(
        "DELETE",
        f"/memory/items/{memory.id}",
        json={"reason": "Test cleanup."},
    )
    active = client.get("/memory/items")
    archived = client.get("/memory/items?include_archived=true")

    assert archive.status_code == 200
    assert archive.json()["status"] == "archived"
    assert active.json()["items"] == []
    assert archived.json()["items"][0]["title"] == "Temporary API memory"


def test_memory_artifacts_endpoint_lists_canonical_workflow_sources(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    artifact_id = uuid.uuid4()
    artifact = Artifact(
        id=artifact_id,
        artifact_type="interaction_package",
        name="Workflow run package",
        uri=str(tmp_path / "workflow.md"),
        mime_type="text/markdown",
        metadata_={
            "canonical_workflow_artifact": True,
            "domain_key": "maestro-development",
        },
    )
    ignored = Artifact(
        artifact_type="raw_file",
        name="Manual upload",
        uri=str(tmp_path / "manual.md"),
        mime_type="text/markdown",
        metadata_={},
    )
    memory = MemoryItem(
        scope="domain",
        memory_type="summary",
        title="Workflow artifact memory",
        content="The workflow artifact generated durable context.",
        impact_level="low",
        importance=0.6,
        metadata_={"artifact_id": str(artifact_id)},
    )
    proposal = MemoryProposal(
        scope="domain",
        memory_type="decision",
        title="Workflow artifact proposal",
        content="Review workflow artifact memory.",
        impact_level="high",
        status="proposed",
        source_refs=[{"type": "artifact", "id": str(artifact_id)}],
        metadata_={},
    )
    session.add_all([artifact, ignored, memory, proposal])
    session.commit()
    client = _client(session, tmp_path)

    response = client.get("/memory/artifacts")

    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    assert [item["name"] for item in artifacts] == ["Workflow run package"]
    assert artifacts[0]["canonical"] is True
    assert artifacts[0]["memory_count"] == 1
    assert artifacts[0]["proposal_count"] == 1


def test_memory_preview_listing_marks_in_progress_writes(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    preview_dir = tmp_path / "perti-laboratories" / "previews"
    preview_dir.mkdir(parents=True)
    (preview_dir / "note.preview.json").write_text(
        """
        {
          "source_file": "note.md",
          "status": "writing",
          "candidates": [{}, {}, {}],
          "results": [{"outcome": "written", "memory_item_id": "memory-1"}]
        }
        """,
        encoding="utf-8",
    )

    response = client.get("/memory/dropbox/previews?domain_key=perti-laboratories")

    assert response.status_code == 200
    preview = response.json()["previews"][0]
    assert preview["is_processing"] is True
    assert preview["candidate_count"] == 3
    assert preview["result_count"] == 1
    assert preview["progress_count"] == 1
    assert preview["progress_total"] == 3


def test_pending_approval_and_approve(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    proposal = MemoryProposal(
        scope="global",
        memory_type="standing_instruction",
        title="External approval",
        content="Do not send external messages without approval.",
        rationale="Authority-changing memory.",
        impact_level="very_high",
        status="pending_user_approval",
        source_refs=[],
        metadata_={},
    )
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    client = _client(session, tmp_path)

    pending = client.get("/memory/proposals/pending")
    assert pending.status_code == 200
    assert pending.json()["proposals"][0]["title"] == "External approval"

    approved = client.post(f"/memory/proposals/{proposal.id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["memory_item"]["title"] == "External approval"


def test_reject_pending_memory(session: Session, tmp_path: Path) -> None:
    proposal = MemoryProposal(
        scope="global",
        memory_type="standing_instruction",
        title="Reject me",
        content="This should be rejected.",
        rationale="Test rejection.",
        impact_level="very_high",
        status="pending_user_approval",
        source_refs=[],
        metadata_={},
    )
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    client = _client(session, tmp_path)

    rejected = client.post(
        f"/memory/proposals/{proposal.id}/reject",
        json={"reason": "Not appropriate."},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["proposal"]["metadata"]["rejection_reason"] == "Not appropriate."


def test_source_listing_and_reclassification(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    personal = DomainRepository(session).get_by_key("personal")
    assert personal is not None
    seed_package = SeedPackage(
        name="resume.pdf",
        source_type="dropbox_file",
        status="processed",
        metadata_={"seed": True},
    )
    session.add(seed_package)
    session.flush()
    memory_item = MemoryItem(
        scope="global",
        memory_type="fact",
        title="Resume fact",
        content="Chris has a resume.",
        impact_level="medium",
        importance=0.7,
        metadata_={"seed_package_id": str(seed_package.id), "dropbox_domain": "global"},
    )
    proposal = MemoryProposal(
        scope="global",
        memory_type="preference",
        title="Resume preference",
        content="Chris prefers durable context.",
        impact_level="medium",
        status="approved",
        source_refs=[],
        metadata_={"seed_package_id": str(seed_package.id), "dropbox_domain": "global"},
    )
    session.add_all([memory_item, proposal])
    session.commit()
    client = _client(session, tmp_path)

    sources = client.get("/memory/sources")

    assert sources.status_code == 200
    assert sources.json()["sources"][0]["name"] == "resume.pdf"
    assert sources.json()["sources"][0]["memory_count"] == 1
    assert sources.json()["sources"][0]["proposal_count"] == 1

    details = client.get(f"/memory/sources/{seed_package.id}")

    assert details.status_code == 200
    assert details.json()["source"]["memories"][0]["title"] == "Resume fact"

    reclassified = client.post(
        f"/memory/sources/{seed_package.id}/reclassify",
        json={"target_domain_key": "personal", "reason": "Resume belongs in Personal."},
    )

    assert reclassified.status_code == 200
    payload = reclassified.json()["source"]
    assert payload["domain_key"] == "personal"
    assert payload["memories"][0]["scope"] == "domain"
    assert payload["memories"][0]["metadata"]["dropbox_domain"] == "personal"
    assert payload["memories"][0]["metadata"]["reclassification_history"][0]["reason"] == (
        "Resume belongs in Personal."
    )
    session.refresh(memory_item)
    session.refresh(proposal)
    assert memory_item.domain_id == personal.id
    assert proposal.domain_id == personal.id


def test_memory_retrieval_endpoint_returns_scored_context(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    ophi = DomainRepository(session).get_by_key("perti-laboratories")
    assert praxis is not None
    assert ophi is not None
    praxis_memory = MemoryItem(
        scope="domain",
        domain_id=praxis.id,
        memory_type="fact",
        title="Praxis training model",
        content="Praxis trains Tactical Innovation Officers.",
        impact_level="medium",
        importance=0.8,
        metadata_={"source_refs": [{"type": "artifact", "id": "artifact-1"}]},
    )
    ophi_memory = MemoryItem(
        scope="domain",
        domain_id=ophi.id,
        memory_type="fact",
        title="Perti Laboratories research model",
        content="Perti Laboratories memory should not appear in Praxis-scoped retrieval.",
        impact_level="low",
        importance=1.0,
        metadata_={},
    )
    session.add_all([praxis_memory, ophi_memory])
    session.commit()
    client = _client(session, tmp_path)

    response = client.get(
        "/memory/retrieve",
        params={
            "audience": "maestro",
            "domain_key": "praxis",
            "query_text": "tactical innovation training",
            "use_semantic": "false",
            "limit": 5,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"]["domain_key"] == "praxis"
    assert payload["query"]["mode"] == "balanced"
    assert payload["query"]["use_semantic"] is False
    assert payload["semantic_status"] == "disabled"
    assert payload["filtered_count"] == 0
    assert payload["results"][0]["title"] == "Praxis training model"
    assert payload["results"][0]["domain_key"] == "praxis"
    assert payload["results"][0]["score"] > 0
    assert payload["results"][0]["query_relevance"] > 0
    assert payload["results"][0]["semantic_similarity"] is None
    assert payload["results"][0]["provenance"]["source_refs"][0]["id"] == "artifact-1"
    assert all(result["domain_key"] != "perti-laboratories" for result in payload["results"])


def test_memory_context_bundle_endpoint_returns_grouped_prompt_context(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    global_memory = MemoryItem(
        scope="global",
        memory_type="preference",
        title="Briefing preference",
        content="Chris prefers brief, decision-oriented context.",
        impact_level="medium",
        importance=0.9,
        metadata_={},
    )
    praxis_memory = MemoryItem(
        scope="domain",
        domain_id=praxis.id,
        memory_type="fact",
        title="Praxis training model",
        content="Praxis trains Tactical Innovation Officers.",
        impact_level="medium",
        importance=0.8,
        metadata_={"source_refs": [{"type": "artifact", "id": "artifact-2"}]},
    )
    session.add_all([global_memory, praxis_memory])
    session.commit()
    client = _client(session, tmp_path)

    response = client.get(
        "/memory/context-bundle",
        params={
            "profile": "agent_prompt",
            "audience": "agent",
            "domain_key": "praxis",
            "query_text": "tactical innovation training",
            "use_semantic": "false",
            "max_items": 6,
            "max_chars": 2000,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["profile"] == "agent_prompt"
    assert payload["audience"] == "agent"
    assert payload["semantic_status"] == "disabled"
    assert payload["retrieval_query"]["mode"] == "broad"
    assert [section["key"] for section in payload["sections"]] == ["global", "domain"]
    assert payload["sections"][1]["memories"][0]["title"] == "Praxis training model"
    assert payload["sections"][1]["memories"][0]["excerpt"] == (
        "Praxis trains Tactical Innovation Officers."
    )
    assert payload["sections"][1]["memories"][0]["provenance"]["source_refs"][0]["id"] == (
        "artifact-2"
    )
    assert "[Global Memory]" in payload["rendered_text"]
    assert str(praxis_memory.id) in payload["rendered_text"]
