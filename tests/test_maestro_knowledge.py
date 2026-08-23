from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import maestro as maestro_api
from app.api.main import create_app
from app.db.models import (
    CalendarEvent,
    Contact,
    Conversation,
    Domain,
    Message,
    Task,
    Todo,
    ToolCall,
    WorkflowDefinition,
    WorkflowRun,
)
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


class SequenceKnowledgePlanner:
    def __init__(self, turns: list[KnowledgeTurn]):
        self.turns = turns
        self.contexts: list[str] = []

    def plan(self, **kwargs) -> KnowledgeTurn:
        self.contexts.append(str(kwargs["context_text"]))
        return self.turns.pop(0)


class FakeWebClient:
    def web_search_response(self, **kwargs):
        assert kwargs["input_text"] == "current Praxis SBIR deadline"
        return {
            "output_text": "The deadline is September 1, 2026.",
            "annotations": [
                {
                    "url_citation": {
                        "url": "https://example.test/sbir",
                        "title": "SBIR notice",
                        "content": "Deadline details",
                    }
                }
            ],
            "usage": {"total_tokens": 42},
        }


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
                iterations=2,
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
    assert payload["knowledge_iterations"] == 2
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_knowledge_reply_resumes_waiting_agent_task_instead_of_answering_directly(
    session: Session,
) -> None:
    seed_default_domains(session)
    conversation = maestro_api._get_or_create_maestro_conversation(session, None)
    domain = session.scalar(select(Domain).where(Domain.key == "personal"))
    todo = Todo(
        domain_id=domain.id,
        title="Determine shed debris disposal rules",
        description="Research the applicable disposal requirements.",
        agent_task=True,
        agent_task_status="needs_input",
        agent_task_error="Provide the shed locality.",
        source_refs=[],
        provenance={},
        metadata_={"planning_notified_at": "2026-08-22T12:00:00+00:00"},
    )
    session.add(todo)
    session.commit()
    maestro_api._record_session_message(
        session,
        conversation,
        "maestro",
        "Where is the shed located?",
        metadata={
            "message_type": "todo_agent_task_rfi",
            "todo_id": str(todo.id),
            "channel_visibility": "global",
        },
    )

    response = _client(session).post(
        "/maestro/respond",
        json={
            "message": "The shed is in Stony Brook, New York.",
            "interaction_mode": "knowledge",
            "conversation_id": str(conversation.id),
        },
    )

    assert response.status_code == 200
    assert response.json()["classification"] == "todo_agent_task_rfi_answer"
    session.refresh(todo)
    assert todo.agent_task_status == "retry"
    assert todo.agent_task_error is None
    assert "User clarification: The shed is in Stony Brook, New York." in todo.description
    assert "planning_notified_at" not in todo.metadata_


def test_knowledge_approval_resumes_global_background_agent_task(
    session: Session,
    monkeypatch,
) -> None:
    conversation = maestro_api._get_or_create_maestro_conversation(session, None)
    parent = Task(
        objective="Create the approved GitHub issue.",
        status="blocked",
        source_type="todo_agent_task",
        input_payload={},
    )
    session.add(parent)
    session.flush()
    child = Task(
        parent_task_id=parent.id,
        objective="Create the GitHub issue.",
        status="blocked",
        source_type="agent",
        workflow_key="scheduler.workflow_item",
        input_payload={},
    )
    session.add(child)
    session.flush()
    run = WorkflowRun(
        parent_task_id=parent.id,
        source_type="todo_agent_task",
        status="blocked",
        input_payload={},
    )
    tool_call = ToolCall(
        task_id=child.id,
        tool_name="github.issue.create",
        status="approval_required",
        input_payload={"title": "Add quality checks"},
    )
    session.add_all([run, tool_call])
    session.commit()

    approved: list[str] = []

    def fake_approve(_db, *, tool_call, **_kwargs):
        approved.append(str(tool_call.id))
        return {
            "kind": "tool_approved",
            "classification": "tool_approved",
            "message": "Approved and resumed the background workflow.",
        }

    monkeypatch.setattr(maestro_api, "_approve_pending_tool_from_chat", fake_approve)
    response = _client(session).post(
        "/maestro/respond",
        json={
            "message": "Approved",
            "interaction_mode": "knowledge",
            "conversation_id": str(conversation.id),
        },
    )

    assert response.status_code == 200
    assert response.json()["classification"] == "tool_approved"
    assert approved == [str(tool_call.id)]


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


def test_knowledge_mode_creates_nonblocking_recurring_context_window(session: Session) -> None:
    seed_default_domains(session)
    planner = StaticKnowledgePlanner(
        KnowledgeTurn(
            message="I added the household context without blocking your availability.",
            actions=[
                {
                    "type": "calendar.create",
                    "reason": "Chris explicitly supplied recurring household context.",
                    "arguments": {
                        "title": "Wife working",
                        "domain_key": "personal",
                        "summary": "Household capacity is reduced during this window.",
                        "start_at": "2026-08-25T07:00:00-04:00",
                        "end_at": "2026-08-25T19:00:00-04:00",
                        "timezone": "America/New_York",
                        "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
                        "item_kind": "context_window",
                        "context_type": "household",
                        "scheduling_effect": "strongly_avoid",
                        "blocks_time": True,
                    },
                }
            ],
        )
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "My wife works every Tuesday from 7am to 7pm; keep that as household context."
    )

    event = session.scalar(select(CalendarEvent).where(CalendarEvent.title == "Wife working"))
    assert event is not None
    assert event.item_kind == "context_window"
    assert event.context_type == "household"
    assert event.scheduling_effect == "strongly_avoid"
    assert event.blocks_time is False
    assert response.action_results[0].status == "completed"


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


def test_knowledge_mode_can_search_update_and_verify_in_one_turn(session: Session) -> None:
    seed_default_domains(session)
    l3 = next(domain for domain in session.query(Domain).all() if domain.key == "l3")
    event = CalendarEvent(
        domain_id=l3.id,
        title="Collaborative Autonomy Standup",
        summary="L3 team standup.",
        status="scheduled",
        attendees=[],
        supporting_refs=[],
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(event)
    session.commit()
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I am finding the exact event.",
                actions=[
                    {
                        "type": "context.search",
                        "arguments": {
                            "query_text": "Collaborative Autonomy Standup",
                            "domain_key": "l3",
                            "stores": ["events"],
                        },
                    }
                ],
            ),
            KnowledgeTurn(
                message="I found it and am updating it.",
                actions=[
                    {
                        "type": "calendar.update",
                        "arguments": {
                            "target": str(event.id),
                            "domain_key": "l3",
                            "updates": {"location": "Room 204"},
                        },
                    }
                ],
            ),
            KnowledgeTurn(
                message="I am verifying the saved event.",
                actions=[
                    {
                        "type": "context.search",
                        "arguments": {
                            "query_text": "Collaborative Autonomy Standup",
                            "domain_key": "l3",
                            "stores": ["events"],
                        },
                    }
                ],
            ),
            KnowledgeTurn(
                message="I updated the standup location to Room 204 and verified the change.",
                actions=[],
            ),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Move the Collaborative Autonomy Standup to Room 204."
    )

    session.refresh(event)
    assert event.location == "Room 204"
    assert response.iterations == 4
    assert [result.action_type for result in response.action_results] == [
        "context.search",
        "calendar.update",
        "context.search",
    ]
    assert response.action_results[0].data["routed"]["events"][0]["id"] == str(event.id)
    assert "Immediate action results: round 1" in planner.contexts[1]
    assert "Room 204" in planner.contexts[3]
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_knowledge_mode_can_search_web_then_answer_without_workflow(session: Session) -> None:
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I am checking the current deadline.",
                actions=[
                    {
                        "type": "web.search",
                        "arguments": {"query": "current Praxis SBIR deadline"},
                    }
                ],
            ),
            KnowledgeTurn(
                message="The current published deadline is September 1, 2026.",
                actions=[],
            ),
        ]
    )

    response = MaestroKnowledgeService(
        session,
        planner=planner,
        web_client=FakeWebClient(),
    ).respond("When is the current Praxis SBIR deadline?")

    assert response.iterations == 2
    assert response.action_results[0].data["citations"][0]["title"] == "SBIR notice"
    assert session.scalar(select(func.count()).select_from(Task)) == 0
