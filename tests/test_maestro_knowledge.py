from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import maestro as maestro_api
from app.api.main import create_app
from app.db.models import CalendarEvent, Contact, Conversation, Message, Task, WorkflowDefinition
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


class CapturingKnowledgePlanner(StaticKnowledgePlanner):
    def __init__(self, turn: KnowledgeTurn):
        super().__init__(turn)
        self.context_text = ""

    def plan(self, **kwargs) -> KnowledgeTurn:
        self.context_text = str(kwargs["context_text"])
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

    event = session.scalar(
        select(CalendarEvent).where(CalendarEvent.title == "Praxis weekly planning")
    )
    assert event is not None
    assert event.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO"
    assert event.timezone == "America/New_York"
    assert response.action_results[0].status == "completed"
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_knowledge_mode_continues_clarification_and_repairs_event_schedule(
    session: Session,
) -> None:
    seed_default_domains(session)
    conversation = Conversation(title="Calendar scheduling", metadata_={})
    session.add(conversation)
    session.flush()
    session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                sender_type="user",
                content=(
                    "Create an L3 calendar event every Monday through Thursday from now until "
                    "the end of the year called Collaborative Autonomy Standup."
                ),
                metadata_={},
            ),
            Message(
                conversation_id=conversation.id,
                sender_type="maestro",
                content="What time and duration should I use?",
                metadata_={
                    "interaction_mode": "knowledge",
                    "pending_clarification": (
                        "Create the L3 Collaborative Autonomy Standup Monday through Thursday "
                        "through the end of 2026; waiting for time and duration."
                    ),
                },
            ),
        ]
    )
    current = Message(
        conversation_id=conversation.id,
        sender_type="user",
        content="11-1130",
        metadata_={},
    )
    session.add(current)
    session.commit()
    planner = CapturingKnowledgePlanner(
        KnowledgeTurn(
            message="I scheduled it for 11:00 to 11:30 AM.",
            actions=[
                {
                    "type": "calendar.create",
                    "reason": "Chris answered the pending time question.",
                    "arguments": {
                        "title": "Collaborative Autonomy Standup",
                        "domain_key": "l3",
                        "start_at": "2026-08-24T11:00:00-04:00",
                        "end_at": "2026-08-24T12:00:00-04:00",
                        "timezone": "America/New_York",
                        "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20251231T235959Z",
                    },
                }
            ],
        )
    )

    MaestroKnowledgeService(session, planner=planner).respond(
        "11-1130",
        conversation_id=conversation.id,
        message_id=current.id,
    )

    event = session.scalar(
        select(CalendarEvent).where(CalendarEvent.title == "Collaborative Autonomy Standup")
    )
    assert event is not None
    assert event.end_at is not None
    assert (event.end_at.hour, event.end_at.minute) == (11, 30)
    assert event.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20261231T235959Z"
    assert "What time and duration should I use?" in planner.context_text
    assert "waiting for time and duration" in planner.context_text


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
