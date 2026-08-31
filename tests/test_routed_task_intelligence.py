import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.db.models import (
    CalendarEvent,
    Contact,
    Domain,
    Entity,
    Message,
    Report,
    RoutedItem,
    Task,
    Todo,
    WorkflowQueueItem,
    WorkflowRun,
)
from app.db.seed import seed_default_domains
from app.maestro.todo_agent_tasks import TodoAgentTaskService
from app.memory.routed_hygiene import RoutedHygieneService
from app.memory.routed_resolver import RoutedObjectResolver
from app.memory.routed_retrieval import RoutedEditService
from app.memory.routed_service import RoutedMemoryService
from app.memory.todo_scheduling import TodoSchedulingService


class FakeEstimator:
    provider = "test"
    model = "test"

    def structured_response(self, **kwargs):
        return {"estimated_minutes": 45}


class FakeOrganizationResolver:
    def __init__(self, object_id):
        self.object_id = object_id

    def choose_match(self, *, item, object_type, candidates):
        assert object_type == "organization"
        assert candidates
        return {
            "action": "update_existing",
            "object_id": str(self.object_id),
            "confidence": 0.93,
            "reason": "The name and organization context identify the same entity.",
        }


def _domain(session) -> Domain:
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None
    return domain


def test_scheduled_todo_projects_to_calendar_and_stays_when_done(session) -> None:
    domain = _domain(session)
    todo = Todo(
        domain_id=domain.id,
        title="Review partner deck",
        description="Review the updated partner pitch deck.",
        scheduled_start_at=datetime(2026, 8, 24, 14, tzinfo=UTC),
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(todo)
    session.flush()
    service = TodoSchedulingService(session, estimator=FakeEstimator())

    event = service.sync_projection(todo)
    todo.status = "done"
    service.sync_projection(todo)

    assert event is not None
    assert event.item_kind == "scheduled_todo"
    assert (event.end_at.hour, event.end_at.minute) == (14, 45)
    assert event.metadata_["todo_status"] == "done"
    assert session.scalar(select(CalendarEvent).where(CalendarEvent.todo_id == todo.id)) is not None


def test_contact_hygiene_extracts_email_name_and_merges_full_name(session) -> None:
    session.add_all(
        [
            Contact(
                name="chris.flournoy.mil@army.mil",
                normalized_name="chris flournoy mil army mil",
                email="chris.flournoy.mil@army.mil",
                source_refs=[],
                provenance={},
                metadata_={},
            ),
            Contact(
                name="Chris Flournoy",
                normalized_name="chris flournoy",
                email=None,
                source_refs=[],
                provenance={},
                metadata_={},
            ),
        ]
    )
    session.commit()

    report = RoutedHygieneService(session).run_once()
    active = session.scalars(select(Contact).where(Contact.status != "archived")).all()

    assert report.duplicates_merged >= 1
    assert len(active) == 1
    assert active[0].name == "Chris Flournoy"
    assert active[0].email == "chris.flournoy.mil@army.mil"


def test_contact_hygiene_does_not_reuse_email_held_by_archived_contact(session) -> None:
    session.add_all(
        [
            Contact(
                name="douglas.w.thompson50.mil@army.mil",
                normalized_name="douglas w thompson50 mil army mil",
                email=None,
                source_refs=[],
                provenance={},
                metadata_={},
            ),
            Contact(
                name="Archived Douglas Thompson",
                normalized_name="archived douglas thompson",
                email="douglas.w.thompson50.mil@army.mil",
                status="archived",
                source_refs=[],
                provenance={},
                metadata_={},
            ),
        ]
    )
    session.commit()

    report = RoutedHygieneService(session).run_once()
    active = session.scalar(select(Contact).where(Contact.status != "archived"))

    assert report.display_fields_canonicalized >= 1
    assert active is not None
    assert active.name == "Douglas W Thompson"
    assert active.email is None
    assert active.metadata_["observed_email_identity"] == "douglas.w.thompson50.mil@army.mil"


def test_contact_hygiene_does_not_assign_same_derived_email_twice_in_one_batch(session) -> None:
    session.add_all(
        [
            Contact(
                name="russell.w.hupp.ctr@army.mil",
                normalized_name="russell w hupp ctr army mil",
                email=None,
                source_refs=[],
                provenance={},
                metadata_={},
            ),
            Contact(
                name="Russell W. Hupp <russell.w.hupp.ctr@army.mil>",
                normalized_name="russell w hupp russell w hupp ctr army mil",
                email=None,
                source_refs=[],
                provenance={},
                metadata_={},
            ),
        ]
    )
    session.commit()

    report = RoutedHygieneService(session).run_once()
    active = session.scalars(select(Contact).where(Contact.status != "archived")).all()

    assert report.duplicates_merged == 1
    assert len(active) == 1
    assert active[0].email == "russell.w.hupp.ctr@army.mil"


def test_organization_hygiene_merges_legal_suffix_variants(session) -> None:
    session.add_all(
        [
            Entity(
                name="Example Corp",
                normalized_name="example corp",
                source_refs=[],
                provenance={},
                metadata_={},
            ),
            Entity(
                name="Example Corporation",
                normalized_name="example corporation",
                source_refs=[],
                provenance={},
                metadata_={},
            ),
        ]
    )
    session.commit()

    report = RoutedHygieneService(session).run_once()
    active = session.scalars(select(Entity).where(Entity.status != "archived")).all()

    assert report.duplicates_merged >= 1
    assert len(active) == 1


def test_organization_resolver_uses_llm_for_semantic_ambiguity(session) -> None:
    domain = _domain(session)
    organization = Entity(
        name="United States Military Academy",
        normalized_name="united states military academy",
        summary="West Point faculty, teaching, and cadet development context.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(organization)
    session.flush()
    item = RoutedItem(
        domain_id=domain.id,
        route_type="organization",
        title="West Point",
        content="A West Point faculty contact and teaching obligation.",
        source_refs=[],
        metadata_={},
    )
    session.add(item)
    session.flush()

    decision = RoutedObjectResolver(
        session,
        llm=FakeOrganizationResolver(organization.id),
    ).resolve_organization(item, name="West Point", identifiers=[])

    assert decision.action == "update_existing"
    assert decision.object_id == organization.id
    assert decision.strategy == "llm_resolver"


def test_standalone_human_input_promotes_to_open_todo(session) -> None:
    domain = _domain(session)
    item = RoutedItem(
        domain_id=domain.id,
        route_type="human_input",
        title="Choose the trip villa",
        content="Compare the remaining options and choose one.",
        status="needs_input",
        source_refs=[],
        metadata_={"enriched_at": datetime.now(UTC).isoformat()},
    )
    session.add(item)
    session.flush()

    result = RoutedMemoryService(session, enable_llm_resolver=False).promote_item(item)
    todo = session.get(Todo, result.object_id)

    assert todo is not None
    assert todo.todo_type == "human_input"
    assert todo.status == "open"


def test_explicitly_blocking_human_input_retains_needs_input_status(session) -> None:
    domain = _domain(session)
    item = RoutedItem(
        domain_id=domain.id,
        route_type="human_input",
        title="Confirm the deployment target",
        content="The workflow cannot continue until Chris confirms the target.",
        status="needs_input",
        source_refs=[],
        metadata_={
            "blocks_execution": True,
            "enriched_at": datetime.now(UTC).isoformat(),
        },
    )
    session.add(item)
    session.flush()

    result = RoutedMemoryService(session, enable_llm_resolver=False).promote_item(item)
    todo = session.get(Todo, result.object_id)

    assert todo is not None
    assert todo.status == "needs_input"


class FakeOrchestrator:
    def __init__(self, session):
        self.session = session

    def create_plan(self, prompt):
        parent_id = uuid.uuid4()
        from app.db.models import Task

        self.session.add(
            Task(
                id=parent_id,
                objective=prompt,
                status="proposed",
                source_type="todo_agent_task",
                input_payload={},
            )
        )
        self.session.commit()
        return SimpleNamespace(
            plan_id=str(parent_id),
            parent_task_id=str(parent_id),
            is_chat_only=False,
            subtasks=[SimpleNamespace(agent_key="praxis-email-agent")],
            work_items=[],
        )

    def enqueue_plan(self, plan_id):
        run = WorkflowRun(
            parent_task_id=uuid.UUID(plan_id),
            source_type="todo_agent_task",
            status="queued",
            input_payload={},
        )
        self.session.add(run)
        self.session.commit()


def test_agent_todo_is_planned_once_and_linked_to_workflow(session) -> None:
    domain = _domain(session)
    todo = Todo(
        domain_id=domain.id,
        title="Research partner",
        description="Prepare a short partner background report.",
        agent_task=True,
        agent_task_status="pending",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(todo)
    session.commit()

    result = TodoAgentTaskService(session, orchestrator=FakeOrchestrator(session)).run_once()

    assert result["started"] == 1
    assert todo.agent_task_status == "queued"
    assert todo.workflow_task_id is not None
    assert todo.workflow_run_id is not None
    messages = session.scalars(select(Message).where(Message.sender_type == "maestro")).all()
    assert any("picked up the background task" in message.content for message in messages)

    run = session.get(WorkflowRun, todo.workflow_run_id)
    assert run is not None
    parent = session.get(Task, todo.workflow_task_id)
    assert parent is not None
    assert parent.source_type == "todo_agent_task"
    assert parent.input_payload["originating_todo_id"] == str(todo.id)
    linked_clarification = Todo(
        domain_id=domain.id,
        title="Confirm partner scope",
        description="Clarify the requested partner research scope.",
        todo_type="human_input",
        status="needs_input",
        source_refs=[],
        provenance={"task_id": str(parent.id)},
        metadata_={},
    )
    session.add(linked_clarification)
    run.status = "completed"
    run.output_payload = {"chat_summary": "I prepared the partner background report."}
    session.commit()

    TodoAgentTaskService(session, orchestrator=FakeOrchestrator(session)).run_once()

    assert todo.status == "done"
    assert todo.agent_task_status == "completed"
    assert linked_clarification.status == "done"
    assert linked_clarification.metadata_["resolved_by_agent_task_id"] == str(todo.id)
    messages = session.scalars(select(Message).where(Message.sender_type == "maestro")).all()
    assert any("prepared the partner background report" in message.content for message in messages)


def test_agent_todo_quality_gate_keeps_incomplete_work_open(session) -> None:
    domain = _domain(session)
    todo = Todo(
        domain_id=domain.id,
        title="Research local disposal rules",
        description="Return current, source-cited disposal guidance.",
        agent_task=True,
        agent_task_status="pending",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(todo)
    session.commit()

    service = TodoAgentTaskService(session, orchestrator=FakeOrchestrator(session))
    service.run_once()
    run = session.get(WorkflowRun, todo.workflow_run_id)
    parent = session.get(Task, todo.workflow_task_id)
    report = Report(
        task_id=parent.id,
        domain_id=domain.id,
        title="Disposal research",
        report_type="agent_run_once",
        summary="Research did not execute.",
        body_markdown='{"summary":{"status":"incomplete—official research unavailable"}}',
        structured_data={},
    )
    session.add(report)
    session.flush()
    session.add(
        WorkflowQueueItem(
            workflow_run_id=run.id,
            parent_task_id=parent.id,
            external_key="q1-wi1",
            status="completed",
            objective="Research current rules.",
            input_payload={},
            output_payload={"report_id": str(report.id)},
        )
    )
    run.status = "completed"
    run.output_payload = {"chat_summary": "The official research did not execute."}
    session.commit()

    service.run_once()

    assert todo.status == "open"
    assert todo.agent_task_status == "needs_input"
    assert "incomplete" in (todo.agent_task_error or "")
    messages = session.scalars(select(Message).where(Message.sender_type == "maestro")).all()
    assert any("completion check failed" in message.content for message in messages)


def test_agent_task_clarification_closes_only_explicitly_linked_human_input(session) -> None:
    domain = _domain(session)
    todo = Todo(
        domain_id=domain.id,
        title="Research shed disposal",
        description="Find local rules.",
        agent_task=True,
        agent_task_status="needs_input",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    linked = Todo(
        domain_id=domain.id,
        title="Provide shed locality",
        description="Which town is the shed in?",
        todo_type="human_input",
        status="needs_input",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    unrelated = Todo(
        domain_id=domain.id,
        title="Confirm meeting owner",
        description="Who owns the partner meeting?",
        todo_type="human_input",
        status="needs_input",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add_all([todo, linked, unrelated])
    session.flush()
    linked.metadata_ = {"blocking_for_todo_id": str(todo.id)}
    session.commit()

    TodoAgentTaskService(session, orchestrator=FakeOrchestrator(session)).apply_clarification(
        todo,
        clarification="The shed is in Stony Brook.",
    )

    assert todo.agent_task_status == "retry"
    assert linked.status == "done"
    assert linked.metadata_["resolved_by_agent_task_id"] == str(todo.id)
    assert unrelated.status == "needs_input"


def test_existing_todo_toggle_becomes_pending_even_with_stale_ui_status(session) -> None:
    domain = _domain(session)
    todo = Todo(
        domain_id=domain.id,
        title="Research disposal rules",
        description="Determine the local disposal requirements.",
        agent_task=False,
        agent_task_status="not_agent",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(todo)
    session.commit()

    RoutedEditService(session).update_todo(
        todo.id,
        {"agent_task": True, "agent_task_status": "not_agent"},
    )

    assert todo.agent_task is True
    assert todo.agent_task_status == "pending"
