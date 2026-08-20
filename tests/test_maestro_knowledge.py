import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import maestro as maestro_api
from app.api.main import create_app
from app.db.models import CalendarEvent, Contact, Task, WorkflowDefinition
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.maestro.knowledge import (
    KnowledgeResponse,
    KnowledgeTurn,
    MaestroKnowledgeService,
)


class StaticKnowledgePlanner:
    def __init__(self, turn: KnowledgeTurn):
        self.turn = turn

    def plan(self, **_kwargs) -> KnowledgeTurn:
        return self.turn


def _client(session: Session) -> TestClient:
    app = create_app()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_knowledge_mode_never_creates_a_workflow_task(session: Session, monkeypatch) -> None:
    class FakeKnowledgeService:
        def __init__(self, _session):
            pass

        def respond(self, *_args, **_kwargs):
            return KnowledgeResponse(
                message="That requires delegated work. Switch to Build workflow when you are ready.",
                action_results=[],
                workflow_suggestion="Use Build workflow to delegate this request.",
            )

    monkeypatch.setattr(maestro_api, "MaestroKnowledgeService", FakeKnowledgeService)
    response = _client(session).post(
        "/maestro/respond",
        json={
            "message": "Have three agents research this and prepare a report.",
            "interaction_mode": "knowledge",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["classification"] == "knowledge_chat"
    assert payload["plan"] is None
    assert payload["workflow_suggestion"]
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_workflow_builder_explicitly_creates_a_proposed_plan(session: Session) -> None:
    response = _client(session).post(
        "/maestro/respond",
        json={
            "message": "Prepare a Praxis partner call workflow.",
            "interaction_mode": "workflow_builder",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "planned"
    assert payload["interaction_mode"] == "workflow_builder"
    assert payload["plan"]["status"] == "proposed"


def test_knowledge_mode_creates_a_recurring_calendar_event(session: Session) -> None:
    seed_default_domains(session)
    planner = StaticKnowledgePlanner(
        KnowledgeTurn(
            message="I added the recurring Praxis planning block.",
            actions=[
                {
                    "type": "calendar.create",
                    "reason": "Chris explicitly requested it.",
                    "arguments": {
                        "title": "Praxis weekly planning",
                        "domain_key": "praxis",
                        "summary": "Review priorities for the week.",
                        "start_at": "2026-08-24T09:00:00-04:00",
                        "end_at": "2026-08-24T09:30:00-04:00",
                        "timezone": "America/New_York",
                        "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
                    },
                }
            ],
        )
    )

    response = MaestroKnowledgeService(session, planner=planner).respond("Create the event.")

    event = session.scalar(select(CalendarEvent).where(CalendarEvent.title == "Praxis weekly planning"))
    assert event is not None
    assert event.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO"
    assert event.timezone == "America/New_York"
    assert response.action_results[0].status == "completed"
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_knowledge_mode_updates_contact_manual_context(session: Session) -> None:
    seed_default_domains(session)
    contact = Contact(
        name="Jane Smith",
        normalized_name="jane smith",
        email="jane@example.com",
        summary="Partner lead.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(contact)
    session.commit()
    planner = StaticKnowledgePlanner(
        KnowledgeTurn(
            message="I updated Jane's contact record.",
            actions=[
                {
                    "type": "contact.update",
                    "reason": "Chris supplied a manual correction.",
                    "arguments": {
                        "target": "jane@example.com",
                        "domain_key": "praxis",
                        "updates": {
                            "phone": "555-0100",
                            "domain_note": "Jane owns the partner follow-up.",
                        },
                    },
                }
            ],
        )
    )

    response = MaestroKnowledgeService(session, planner=planner).respond("Update Jane.")

    session.refresh(contact)
    assert contact.phone == "555-0100"
    assert response.action_results[0].status == "completed"
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_knowledge_mode_edits_existing_workflow_without_creating_one(session: Session) -> None:
    definition = WorkflowDefinition(
        key="praxis.email-triage",
        name="Praxis email triage",
        trigger_type="event",
        trigger_config={"event_type": "gmail.message.received"},
        workflow_spec={"queue_items": []},
    )
    session.add(definition)
    session.commit()
    planner = StaticKnowledgePlanner(
        KnowledgeTurn(
            message="I paused Praxis email triage.",
            actions=[
                {
                    "type": "workflow.update",
                    "reason": "Chris asked to pause it.",
                    "arguments": {
                        "target": "praxis.email-triage",
                        "updates": {"is_active": False},
                    },
                }
            ],
        )
    )

    response = MaestroKnowledgeService(session, planner=planner).respond("Pause email triage.")

    session.refresh(definition)
    assert definition.is_active is False
    assert response.action_results[0].object_id == str(definition.id)
    assert session.scalar(select(func.count()).select_from(WorkflowDefinition)) == 1
    assert session.scalar(select(func.count()).select_from(Task)) == 0
