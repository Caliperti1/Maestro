from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.main import create_app
from app.db.models import CalendarEvent, Domain, RecurringTodoSeries, RoutedItem, Todo
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.maestro.todo_agent_tasks import TodoAgentTaskService
from app.memory.recurring_todos import RecurringTodoService
from app.memory.routed_hygiene import RoutedHygieneService
from app.memory.routed_retrieval import RoutedEditService
from app.memory.routed_service import RoutedMemoryService


def _domain(session, key: str = "perti-laboratories") -> Domain:
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == key))
    assert domain is not None
    return domain


def _client(session) -> TestClient:
    app = create_app()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_monthly_series_materializes_current_and_future_occurrences(session) -> None:
    domain = _domain(session)
    service = RecurringTodoService(session)

    creation = service.create_series(
        domain_id=domain.id,
        title="Build and send Perti Labs invoices",
        description="Prepare and send the monthly invoices.",
        recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=5",
        due_anchor_at=datetime(2026, 8, 5, 21, tzinfo=UTC),
        scheduled_anchor_at=None,
        estimated_minutes=90,
        priority="high",
    )

    # Re-materialize at a fixed point so the test is independent of wall-clock date.
    for todo in session.scalars(select(Todo)).all():
        session.delete(todo)
    session.commit()
    created = service.materialize_series(
        creation.series,
        now=datetime(2026, 8, 31, 14, tzinfo=UTC),
    )

    assert [todo.due_at for todo in created] == [
        datetime(2026, 8, 5, 21, tzinfo=UTC),
        datetime(2026, 9, 5, 21, tzinfo=UTC),
        datetime(2026, 10, 5, 21, tzinfo=UTC),
    ]
    assert all(todo.recurring_series_id == creation.series.id for todo in created)
    assert service.materialize_series(
        creation.series,
        now=datetime(2026, 8, 31, 14, tzinfo=UTC),
    ) == []


def test_completing_monthly_occurrence_keeps_series_and_next_month_open(session) -> None:
    domain = _domain(session)
    service = RecurringTodoService(session)
    creation = service.create_series(
        domain_id=domain.id,
        title="Send invoices",
        description="Send invoices.",
        recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=5",
        due_anchor_at=datetime(2026, 8, 5, 13, tzinfo=UTC),
        scheduled_anchor_at=None,
        estimated_minutes=60,
    )
    for todo in session.scalars(select(Todo)).all():
        session.delete(todo)
    session.commit()
    service.materialize_series(
        creation.series,
        now=datetime(2026, 8, 4, 12, tzinfo=UTC),
    )
    occurrences = session.scalars(
        select(Todo).order_by(Todo.recurrence_original_at)
    ).all()

    RoutedEditService(session).update_todo(occurrences[0].id, {"status": "done"})

    session.refresh(creation.series)
    session.refresh(occurrences[1])
    assert creation.series.status == "active"
    assert occurrences[0].status == "done"
    assert occurrences[1].status == "open"


def test_pausing_series_archives_future_occurrences_but_keeps_overdue_work(session) -> None:
    domain = _domain(session)
    service = RecurringTodoService(session)
    creation = service.create_series(
        domain_id=domain.id,
        title="Monthly close",
        description="Close the monthly books.",
        recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=5",
        due_anchor_at=datetime(2026, 8, 5, 13, tzinfo=UTC),
        scheduled_anchor_at=None,
        estimated_minutes=120,
    )
    for todo in session.scalars(select(Todo)).all():
        session.delete(todo)
    session.commit()
    service.materialize_series(
        creation.series,
        now=datetime(2026, 8, 31, 12, tzinfo=UTC),
    )

    # Freeze the comparison by making the generated future dates future relative to wall time.
    service.update_series(creation.series.id, {"status": "paused"})

    assert session.get(RecurringTodoSeries, creation.series.id).status == "paused"
    occurrences = session.scalars(select(Todo).order_by(Todo.due_at)).all()
    assert occurrences[0].status == "open"
    assert all(todo.status == "archived" for todo in occurrences[1:])

    service.update_series(creation.series.id, {"status": "active"})
    occurrences = session.scalars(select(Todo).order_by(Todo.due_at)).all()
    assert all(todo.status == "open" for todo in occurrences)


def test_todo_api_creates_and_exposes_monthly_series(session) -> None:
    _domain(session)
    response = _client(session).post(
        "/memory/routed-objects/todos",
        json={
            "domain_key": "perti-laboratories",
            "title": "Build and send monthly invoices",
            "description": "Prepare and send Perti Labs invoices.",
            "due_at": "2026-09-05T17:00:00-04:00",
            "estimated_minutes": 90,
            "priority": "high",
            "agent_task": False,
            "recurrence_rule": "FREQ=MONTHLY;BYMONTHDAY=5",
            "recurrence_timezone": "America/New_York",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["series"]["status"] == "active"
    assert payload["todo"]["recurring_series_id"] == payload["series"]["id"]
    listed = _client(session).get(
        "/memory/routed-objects/todos?domain_key=perti-laboratories&limit=20"
    )
    assert listed.status_code == 200
    recurring = [item for item in listed.json()["todos"] if item["recurring_series"]]
    assert len(recurring) >= 2
    assert all(item["recurring_series"]["recurrence_rule"].startswith("FREQ=MONTHLY") for item in recurring)


def test_routed_extraction_promotes_explicit_recurrence_to_series(session) -> None:
    domain = _domain(session)
    item = RoutedItem(
        domain_id=domain.id,
        route_type="task",
        title="Send monthly invoices",
        content="Prepare and send the Perti Labs invoices every month.",
        priority="high",
        status="open",
        source_refs=[{"source_system": "maestro_chat", "source_id": "message-1"}],
        metadata_={
            "due_at": "2026-09-05T17:00:00-04:00",
            "estimated_minutes": 90,
            "recurrence_rule": "FREQ=MONTHLY;BYMONTHDAY=5",
            "recurrence_timezone": "America/New_York",
        },
    )
    session.add(item)
    session.commit()

    RoutedMemoryService(session, enable_llm_resolver=False).process_pending()

    series = session.scalar(select(RecurringTodoSeries))
    assert series is not None
    assert series.title == "Send monthly invoices"
    assert series.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=5"
    assert session.query(Todo).filter(Todo.recurring_series_id == series.id).count() >= 2


def test_hygiene_does_not_merge_separate_occurrences_from_one_series(session) -> None:
    domain = _domain(session)
    creation = RecurringTodoService(session).create_series(
        domain_id=domain.id,
        title="Send monthly invoices",
        description="Send the monthly invoices.",
        recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=5",
        due_anchor_at=datetime(2026, 9, 5, 17, tzinfo=UTC),
        scheduled_anchor_at=None,
        estimated_minutes=90,
    )
    before = session.query(Todo).filter(Todo.recurring_series_id == creation.series.id).count()

    report = RoutedHygieneService(session).run_once()

    after = session.query(Todo).filter(Todo.recurring_series_id == creation.series.id).count()
    assert before >= 2
    assert after == before
    assert report.duplicates_merged == 0


def test_future_recurring_agent_occurrence_waits_until_eligible(session) -> None:
    domain = _domain(session)
    creation = RecurringTodoService(session).create_series(
        domain_id=domain.id,
        title="Prepare monthly finance report",
        description="Prepare the monthly finance report.",
        recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=5",
        due_anchor_at=datetime(2026, 9, 5, 17, tzinfo=UTC),
        scheduled_anchor_at=None,
        estimated_minutes=90,
        agent_task=True,
    )

    result = TodoAgentTaskService(session).run_once()

    assert result["started"] == 0
    occurrences = session.query(Todo).filter(Todo.recurring_series_id == creation.series.id).all()
    assert all(todo.agent_task_status == "pending" for todo in occurrences)


def test_pausing_scheduled_series_removes_future_calendar_projections(session) -> None:
    domain = _domain(session)
    service = RecurringTodoService(session)
    creation = service.create_series(
        domain_id=domain.id,
        title="Monthly finance block",
        description="Reserved time for monthly finance work.",
        recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=5",
        due_anchor_at=datetime(2026, 9, 5, 21, tzinfo=UTC),
        scheduled_anchor_at=datetime(2026, 9, 5, 17, tzinfo=UTC),
        estimated_minutes=90,
    )
    assert session.query(CalendarEvent).count() >= 2

    service.update_series(creation.series.id, {"status": "paused"})

    assert session.query(CalendarEvent).count() == 0
