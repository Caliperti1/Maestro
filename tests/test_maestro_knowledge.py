import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import maestro as maestro_api
from app.api.main import create_app
from app.db.models import (
    CalendarEvent,
    CalendarEventWorkLink,
    Contact,
    Conversation,
    Domain,
    LLMCallLog,
    Message,
    ProductIssue,
    ProductProject,
    Report,
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
    LLMKnowledgePlanner,
    MaestroKnowledgeService,
)


class FlakyKnowledgeClient:
    provider = "openrouter"
    model = "openai/gpt-5.6-terra"
    last_usage = None
    last_response_id = None

    def __init__(self):
        self.calls = 0

    def structured_response(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary connection failure")
        self.last_usage = {"prompt_tokens": 12, "completion_tokens": 6}
        self.last_response_id = "response-2"
        return {
            "message": "I found the requested issues.",
            "actions": [],
            "workflow_suggestion": None,
            "pending_clarification": None,
        }


class StaticKnowledgePlanner:
    def __init__(self, turn: KnowledgeTurn):
        self.turn = turn

    def plan(self, **_kwargs) -> KnowledgeTurn:
        return self.turn


def test_live_knowledge_planner_retries_once_and_records_each_attempt(session: Session) -> None:
    client = FlakyKnowledgeClient()

    turn = LLMKnowledgePlanner(client=client, session=session).plan(
        message="Show me the relevant product issues.",
        context_text="Canonical product issue context.",
        now=datetime.now(UTC),
    )

    assert turn.message == "I found the requested issues."
    assert client.calls == 2
    calls = session.scalars(
        select(LLMCallLog).where(LLMCallLog.component == "maestro.knowledge")
        .order_by(LLMCallLog.created_at)
    ).all()
    assert [call.status for call in calls] == ["failed", "complete"]
    assert [call.metadata_["attempt"] for call in calls] == [1, 2]


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


def test_voice_mode_returns_compact_spoken_contract_and_replays_client_turn_once(
    session: Session,
    monkeypatch,
) -> None:
    calls: list[str] = []

    class FakeKnowledgeService:
        def __init__(self, _session):
            pass

        def respond(self, message, **kwargs):
            calls.append(message)
            assert kwargs["response_mode"] == "voice"
            return KnowledgeResponse(
                message="Focus on the phone reliability test next. Want the acceptance steps?",
                action_results=[],
            )

    monkeypatch.setattr(maestro_api, "MaestroKnowledgeService", FakeKnowledgeService)
    client_turn_id = uuid.uuid4()
    request = {
        "message": "What should I focus on next?",
        "interaction_mode": "knowledge",
        "interface": "voice",
        "response_mode": "voice",
        "client_turn_id": str(client_turn_id),
    }
    first = _client(session).post("/maestro/respond", json=request)
    second = _client(session).post("/maestro/respond", json=request)

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == ["What should I focus on next?"]
    assert first.json()["classification"] == "knowledge_chat"
    assert second.json()["classification"] == "idempotent_replay"
    assert second.json()["message"] == first.json()["message"]
    assert first.json()["continue_listening"] is True
    assert first.json()["client_turn_id"] == str(client_turn_id)
    assert set(first.json()["conversation"]) == {"id", "title", "message_count"}

    user_messages = session.scalars(
        select(Message).where(Message.client_turn_id == client_turn_id)
    ).all()
    assert len(user_messages) == 1
    responses = [
        message
        for message in session.scalars(
            select(Message).where(
                Message.conversation_id == user_messages[0].conversation_id,
                Message.sender_type == "maestro",
            )
        ).all()
        if (message.metadata_ or {}).get("in_reply_to_client_turn_id") == str(client_turn_id)
    ]
    assert len(responses) == 1


def test_voice_mode_adds_spoken_response_guidance_to_knowledge_context(session: Session) -> None:
    planner = CapturingKnowledgePlanner(
        KnowledgeTurn(
            message="Your first priority is the phone reliability test.",
            actions=[],
        )
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "What should I focus on?",
        response_mode="voice",
    )

    assert response.message == "Your first priority is the phone reliability test."
    assert 'purpose="response_mode"' in planner.context_text
    assert "one to three short sentences" in planner.context_text
    assert "avoid Markdown and raw URLs" in planner.context_text


def test_knowledge_follow_up_receives_active_daily_standup_report(session: Session) -> None:
    seed_default_domains(session)
    maestro_domain = session.scalar(select(Domain).where(Domain.key == "maestro-development"))
    conversation = Conversation(
        domain_id=maestro_domain.id if maestro_domain else None,
        title="Maestro channel",
        metadata_={"channel": "primary"},
    )
    report = Report(
        domain_id=maestro_domain.id if maestro_domain else None,
        title="Daily Standup",
        report_type="daily_standup",
        summary="One cross-domain priority needs Chris's decision.",
        body_markdown=(
            "# Daily Standup\n\n## Praxis\nSchedule partner follow-through at 14:00.\n\n"
            "## Input Needed from Chris\nConfirm whether the partner call outranks product work."
        ),
        structured_data={},
    )
    session.add_all([conversation, report])
    session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            sender_type="maestro",
            content="Chris, your standup is ready.",
            metadata_={
                "workflow_key": "daily-standup",
                "standup_report_id": str(report.id),
                "event_type": "workflow_completed",
            },
        )
    )
    session.commit()
    planner = CapturingKnowledgePlanner(
        KnowledgeTurn(message="I can move the partner work to 14:00.", actions=[])
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Move the partner follow-through to 2 PM and let the Praxis agent take the product review.",
        conversation_id=conversation.id,
    )

    assert response.message == "I can move the partner work to 14:00."
    assert "Active Daily Standup Context" in planner.context_text
    assert "Schedule partner follow-through at 14:00" in planner.context_text
    assert "create or update accepted calendar events, todos, or Product Issues" in planner.context_text
    assert "agent_task=true" in planner.context_text


def test_knowledge_mode_invokes_existing_on_demand_workflow_once(session: Session) -> None:
    seed_default_domains(session)
    definition = WorkflowDefinition(
        key="daily-standup",
        name="Daily Standup",
        trigger_type="manual",
        trigger_config={
            "invocation_aliases": ["prepare my daily standup"],
            "parameter_schema": {
                "type": "object",
                "properties": {"focus": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        },
        workflow_spec={"queue_items": []},
        is_active=True,
    )
    session.add(definition)
    session.commit()
    message_id = uuid.uuid4()
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I found the standup playbook.",
                actions=[
                    {
                        "type": "workflow.get",
                        "arguments": {"query": "daily standup", "trigger_type": "manual"},
                    }
                ],
            ),
            KnowledgeTurn(
                message="I am starting your daily standup in the background.",
                actions=[
                    {
                        "type": "workflow.run",
                        "arguments": {
                            "target": "daily-standup",
                            "parameters": {"focus": "Praxis partner follow-through"},
                        },
                    }
                ],
            ),
            KnowledgeTurn(
                message=(
                    "I started your Daily Standup in the background with extra attention on "
                    "Praxis partner follow-through. I will bring any blockers or results back here."
                ),
                actions=[],
            ),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Prepare my daily standup, focused on Praxis partner follow-through.",
        message_id=message_id,
    )

    runs = session.scalars(select(WorkflowRun)).all()
    assert len(runs) == 1
    assert runs[0].source_type == "knowledge_on_demand"
    assert runs[0].input_payload["invocation"]["parameters"] == {
        "focus": "Praxis partner follow-through"
    }
    assert response.action_results[-1].action_type == "workflow.run"
    assert "background" in response.message


def test_workflow_context_is_authoritative_and_includes_current_execution_lanes(
    session: Session,
) -> None:
    seed_default_domains(session)
    session.add(
        WorkflowDefinition(
            key="daily-standup",
            name="Daily Standup",
            description="Prepare Chris's current cross-domain operating picture.",
            trigger_type="manual",
            trigger_config={
                "invocation_aliases": ["prepare my daily standup"],
                "workflow_version": "2",
            },
            workflow_spec={
                "queue_items": [
                    {
                        "id": "l3-input",
                        "domain_key": "l3",
                        "agent_key": "l3-operations-agent",
                        "stage_index": 1,
                        "objective": "Prepare the L3 input for Chris's daily standup.",
                    },
                    {
                        "id": "standup-synthesis",
                        "domain_key": "maestro-development",
                        "agent_key": "maestro-briefing-agent",
                        "stage_index": 2,
                        "depends_on": ["l3-input"],
                        "objective": "Synthesize the domain reports for Chris.",
                    },
                ]
            },
            is_active=True,
        )
    )
    session.commit()
    planner = CapturingKnowledgePlanner(
        KnowledgeTurn(message="It includes L3 and then synthesizes the domain reports.", actions=[])
    )

    MaestroKnowledgeService(session, planner=planner).respond(
        "What does my Daily Standup do?"
    )

    assert "Authoritative Current Workflow Definitions" in planner.context_text
    assert "override memories, reports, run logs" in planner.context_text
    assert "key=daily-standup" in planner.context_text
    assert "domain=l3" in planner.context_text
    assert "agent=l3-operations-agent" in planner.context_text
    assert 'depends_on=["l3-input"]' in planner.context_text


def test_semantic_paraphrase_can_invoke_on_demand_workflow_without_alias_match(
    session: Session,
) -> None:
    seed_default_domains(session)
    session.add(
        WorkflowDefinition(
            key="daily-standup",
            name="Daily Standup",
            trigger_type="manual",
            trigger_config={"invocation_aliases": ["prepare my daily standup"]},
            workflow_spec={"queue_items": []},
            is_active=True,
        )
    )
    session.commit()
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I am starting your morning operating picture.",
                actions=[
                    {
                        "type": "workflow.run",
                        "arguments": {"target": "daily-standup", "parameters": {}},
                    }
                ],
            ),
            KnowledgeTurn(
                message="I started your Daily Standup in the background.",
                actions=[],
            ),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Give me my morning operating picture."
    )

    runs = session.scalars(select(WorkflowRun)).all()
    assert len(runs) == 1
    assert runs[0].workflow_definition_id is not None
    assert response.action_results[-1].action_type == "workflow.run"


def test_missing_workflow_target_is_repaired_without_asking_chris(
    session: Session,
) -> None:
    seed_default_domains(session)
    session.add(
        WorkflowDefinition(
            key="daily-standup",
            name="Daily Standup",
            trigger_type="manual",
            trigger_config={"invocation_aliases": ["prepare my daily standup"]},
            workflow_spec={"queue_items": []},
            is_active=True,
        )
    )
    session.commit()
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I am starting your standup.",
                actions=[{"type": "workflow.run", "arguments": {}}],
            ),
            KnowledgeTurn(
                message="I found the intended approved playbook.",
                actions=[
                    {
                        "type": "workflow.run",
                        "arguments": {"target": "daily-standup", "parameters": {}},
                    }
                ],
            ),
            KnowledgeTurn(
                message="I started your Daily Standup in the background.",
                actions=[],
            ),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Please run my morning standup."
    )

    runs = session.scalars(select(WorkflowRun)).all()
    assert len(runs) == 1
    assert response.message == "I started your Daily Standup in the background."
    assert [result.status for result in response.action_results] == [
        "invalid_action",
        "completed",
    ]
    assert "required target" in planner.contexts[1]
    assert "I need one more detail" not in response.message


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


def test_knowledge_mode_moves_one_recurring_occurrence_from_ranked_calendar_match(
    session: Session,
) -> None:
    seed_default_domains(session)
    l3 = next(domain for domain in session.query(Domain).all() if domain.key == "l3")
    parent = CalendarEvent(
        domain_id=l3.id,
        title="Collaborative Autonomy Standup",
        summary="Daily autonomy coordination.",
        start_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 24, 15, 30, tzinfo=UTC),
        timezone="America/New_York",
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20261231T235959Z",
        status="scheduled",
        attendees=[],
        supporting_refs=[],
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(parent)
    session.commit()
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I am checking the L3 calendar and recurring occurrence.",
                actions=[
                    {
                        "type": "context.search",
                        "arguments": {
                            "query_text": (
                                "Collaborative Autonomy Standup scheduled August 31 2026 "
                                "at 11:00 AM Eastern"
                            ),
                            "domain_key": "l3",
                            "stores": ["events"],
                        },
                    }
                ],
            ),
            KnowledgeTurn(
                message="I found the recurring occurrence and am moving only that one.",
                actions=[
                    {
                        "type": "calendar.update",
                        "arguments": {
                            "target": str(parent.id),
                            "domain_key": "l3",
                            "updates": {
                                "edit_scope": "occurrence",
                                "occurrence_start_at": "2026-08-31T15:00:00+00:00",
                                "start_at": "2026-08-31T17:30:00+00:00",
                            },
                        },
                    }
                ],
            ),
            KnowledgeTurn(
                message=(
                    "I moved only today's Collaborative Autonomy Standup to 1:30 PM Eastern."
                ),
                actions=[],
            ),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Shift my L3 collaborative autonomy standup for only today from 11 to 1330 EST."
    )

    assert response.action_results[-1].status == "completed", response.action_results[-1].message
    session.refresh(parent)
    exceptions = session.scalars(
        select(CalendarEvent).where(CalendarEvent.id != parent.id)
    ).all()
    assert parent.start_at.replace(tzinfo=UTC) == datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
    assert len(exceptions) == 1
    assert exceptions[0].start_at.replace(tzinfo=UTC) == datetime(
        2026, 8, 31, 17, 30, tzinfo=UTC
    )
    assert exceptions[0].end_at.replace(tzinfo=UTC) == datetime(
        2026, 8, 31, 18, 0, tzinfo=UTC
    )
    assert exceptions[0].metadata_["recurrence_parent_id"] == str(parent.id)
    assert response.message.startswith("I moved only today's")
    assert "matched_occurrence_start_at" in planner.contexts[1]


def test_calendar_update_resolves_a_rough_recurring_event_reference(session: Session) -> None:
    seed_default_domains(session)
    l3 = next(domain for domain in session.query(Domain).all() if domain.key == "l3")
    event = CalendarEvent(
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
        provenance={},
        metadata_={},
    )
    session.add(event)
    session.commit()
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I found the L3 standup from its calendar context.",
                actions=[
                    {
                        "type": "calendar.update",
                        "arguments": {
                            "target": (
                                "my L3 autonomy standup at 11 AM on August 31 2026"
                            ),
                            "domain_key": "l3",
                            "updates": {"location": "Teams"},
                        },
                    }
                ],
            ),
            KnowledgeTurn(message="I updated the standup location to Teams.", actions=[]),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Make the L3 autonomy standup at 11 on August 31 a Teams meeting."
    )

    session.refresh(event)
    assert event.location == "Teams"
    assert response.action_results[-1].status == "completed"


def test_knowledge_mode_links_todo_to_event_as_prerequisite(session: Session) -> None:
    seed_default_domains(session)
    praxis = next(domain for domain in session.query(Domain).all() if domain.key == "praxis")
    event = CalendarEvent(
        domain_id=praxis.id,
        title="Partner review",
        summary="Review the partner plan.",
        status="scheduled",
        attendees=[],
        supporting_refs=[],
        source_refs=[],
        provenance={},
        metadata_={},
    )
    todo = Todo(
        domain_id=praxis.id,
        title="Prepare partner plan",
        description="Prepare the partner plan.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add_all([event, todo])
    session.commit()
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I am linking that preparation to the review.",
                actions=[
                    {
                        "type": "calendar.link_work",
                        "arguments": {
                            "event_target": str(event.id),
                            "work_target": str(todo.id),
                            "target_type": "todo",
                            "relationship_type": "prerequisite",
                            "domain_key": "praxis",
                        },
                    }
                ],
            ),
            KnowledgeTurn(
                message="I linked the preparation task as a prerequisite to the partner review.",
                actions=[],
            ),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Make preparing the partner plan a prerequisite for the partner review."
    )

    link = session.scalar(select(CalendarEventWorkLink))
    assert link is not None
    assert link.event_id == event.id
    assert link.todo_id == todo.id
    assert link.relationship_type == "prerequisite"
    assert response.action_results[0].status == "completed"


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


def test_knowledge_portfolio_search_returns_compact_grounded_results(session: Session) -> None:
    seed_default_domains(session)
    maestro = session.scalar(select(Domain).where(Domain.key == "maestro-development"))
    praxis = session.scalar(select(Domain).where(Domain.key == "praxis"))
    maestro_project = ProductProject(
        domain_id=maestro.id,
        key="maestro",
        name="Maestro",
        summary="System orchestration.",
        vision="",
        source_refs=[],
        provenance={},
    )
    groundtruth_project = ProductProject(
        domain_id=praxis.id,
        key="groundtruth",
        name="GroundTruth",
        summary="Praxis product.",
        vision="",
        source_refs=[],
        provenance={},
    )
    session.add_all([maestro_project, groundtruth_project])
    session.flush()
    session.add_all(
        [
            ProductIssue(
                domain_id=maestro.id,
                project_id=maestro_project.id,
                issue_type="feature",
                title="Improve memory retrieval",
                normalized_title="improve memory retrieval",
                problem="A" * 3000,
                desired_outcome="",
                acceptance_criteria=[],
                notes="",
                status="ready",
                source_refs=[],
                provenance={},
            ),
            ProductIssue(
                domain_id=praxis.id,
                project_id=groundtruth_project.id,
                issue_type="feature",
                title="Add integration reporting",
                normalized_title="add integration reporting",
                problem="Connect GroundTruth reporting to its integrations.",
                desired_outcome="",
                acceptance_criteria=[],
                notes="",
                status="active",
                source_refs=[],
                provenance={},
            ),
            ProductIssue(
                domain_id=maestro.id,
                project_id=maestro_project.id,
                issue_type="feature",
                title="Add memory reporting dashboard",
                normalized_title="add memory reporting dashboard",
                problem="Expose memory integration metrics.",
                desired_outcome="",
                acceptance_criteria=[],
                notes="",
                status="ready",
                source_refs=[],
                provenance={},
            ),
        ]
    )
    session.commit()
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="I am searching the requested portfolio.",
                actions=[
                    {
                        "type": "issue.search",
                        "arguments": {
                            "query": "memory reporting integration",
                            "project_keys": ["maestro", "groundtruth"],
                            "status": "open",
                            "max_items": 2,
                        },
                    }
                ],
            ),
            KnowledgeTurn(message="I found the relevant issues.", actions=[]),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Show me open product issues across Maestro and GroundTruth."
    )

    issues = response.action_results[0].data["issues"]
    assert response.iterations == 2
    assert {item["project_key"] for item in issues} == {"maestro", "groundtruth"}
    assert {item["domain_key"] for item in issues} == {"maestro-development", "praxis"}
    assert all("problem" not in item for item in issues)
    assert max(len(item["summary"]) for item in issues) <= 280
    assert "Canonical Product Portfolio" in planner.contexts[0]
    assert "Existing durable workflows" not in planner.contexts[0]


def test_knowledge_reuses_equivalent_issue_search_instead_of_persisting_duplicates(
    session: Session,
) -> None:
    seed_default_domains(session)
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="Searching.",
                actions=[
                    {
                        "type": "issue.search",
                        "arguments": {
                            "query": "memory OR reporting",
                            "project_keys": ["groundtruth", "maestro"],
                            "status": "open",
                        },
                    }
                ],
            ),
            KnowledgeTurn(
                message="Searching again.",
                actions=[
                    {
                        "type": "issue.search",
                        "arguments": {
                            "query_text": "reporting memory",
                            "project_keys": ["maestro", "groundtruth"],
                            "status": "open",
                        },
                    }
                ],
            ),
            KnowledgeTurn(message="No matching issues were found.", actions=[]),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Find memory and reporting product issues."
    )

    assert response.iterations == 3
    assert len(response.action_results) == 1
    assert "cached_action_signature" in planner.contexts[2]


def test_knowledge_coalesces_per_project_issue_reads_into_one_portfolio_search(
    session: Session,
) -> None:
    seed_default_domains(session)
    planner = SequenceKnowledgePlanner(
        [
            KnowledgeTurn(
                message="Searching the portfolio.",
                actions=[
                    {
                        "type": "issue.search",
                        "arguments": {
                            "query": "memory reporting",
                            "project_key": "maestro",
                            "status": "open",
                        },
                    },
                    {
                        "type": "issue.search",
                        "arguments": {
                            "query": "reporting integrations",
                            "project_key": "groundtruth",
                            "status": "open",
                        },
                    },
                ],
            ),
            KnowledgeTurn(message="I reviewed the portfolio results.", actions=[]),
        ]
    )

    response = MaestroKnowledgeService(session, planner=planner).respond(
        "Compare Maestro and GroundTruth product issues."
    )

    assert response.iterations == 2
    assert len(response.action_results) == 1
    result = response.action_results[0]
    assert result.action_type == "issue.search"
    assert result.data["filters"]["project_keys"] == ["maestro", "groundtruth"]
    assert result.data["query"] == "integrations memory reporting"
