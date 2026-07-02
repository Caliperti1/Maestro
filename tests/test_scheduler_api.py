from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.core.config import get_settings
from app.db.seed import seed_default_domains
from app.db.session import get_db


def _client(session: Session, tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_scheduler_api_creates_definition_and_enqueues_event_trigger(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)

    created = client.post(
        "/scheduler/definitions",
        json={
            "key": "praxis-email-triage",
            "name": "Praxis Email Triage",
            "domain_key": "praxis",
            "trigger_type": "event",
            "trigger_config": {
                "event_type": "gmail.message.received",
                "filters": {"domain_key": "praxis"},
            },
            "workflow_spec": {
                "queue_items": [
                    {
                        "id": "triage",
                        "objective": "Triage the new Praxis email.",
                        "domain_key": "praxis",
                        "required_tools": ["gmail.message.get"],
                    }
                ]
            },
        },
    )

    assert created.status_code == 200
    assert created.json()["definition"]["trigger_type"] == "event"

    enqueued = client.post(
        "/scheduler/triggers/event",
        json={
            "event_type": "gmail.message.received",
            "event_id": "msg-123",
            "event_payload": {"domain_key": "praxis", "subject": "Partner update"},
        },
    )

    assert enqueued.status_code == 200
    runs = enqueued.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["source_type"] == "event"
    assert runs[0]["queue_items"][0]["external_key"] == "triage"


def test_scheduler_api_tick_claims_due_recurring_work(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    response = client.post(
        "/scheduler/definitions",
        json={
            "key": "daily-before-eight",
            "name": "Daily Before 8",
            "domain_key": "personal",
            "trigger_type": "recurring",
            "trigger_config": {
                "next_run_at": "2020-01-01T07:55:00+00:00",
                "interval_minutes": 1440,
            },
            "workflow_spec": {
                "queue_items": [
                    {
                        "id": "brief",
                        "objective": "Prepare the daily brief.",
                        "domain_key": "personal",
                    }
                ]
            },
        },
    )
    assert response.status_code == 200

    tick = client.post(
        "/scheduler/tick",
        json={"owner": "api-test", "claim_limit": 2, "lease_seconds": 120},
    )

    assert tick.status_code == 200
    payload = tick.json()
    assert len(payload["enqueued"]) == 1
    assert len(payload["claimed"]) == 1
    assert payload["claimed"][0]["lease_owner"] == "api-test"


def test_scheduler_api_exposes_run_detail_and_archives_noise(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    client.post(
        "/scheduler/definitions",
        json={
            "key": "daily-introspection",
            "name": "Daily Introspection",
            "domain_key": "maestro-development",
            "trigger_type": "recurring",
            "trigger_config": {
                "next_run_at": "2020-01-01T07:55:00+00:00",
                "interval_minutes": 1440,
            },
            "workflow_spec": {
                "queue_items": [
                    {
                        "id": "introspect",
                        "objective": "Analyze yesterday's Maestro logs.",
                        "domain_key": "maestro-development",
                    }
                ]
            },
        },
    )
    tick = client.post("/scheduler/tick", json={"owner": "api-test", "claim_limit": 1})
    run_id = tick.json()["enqueued"][0]["id"]

    detail = client.get(f"/scheduler/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run"]["events"][0]["event_type"] in {
        "queue_item_claimed",
        "locks_acquired",
        "workflow_enqueued",
    }

    archived = client.patch(f"/scheduler/runs/{run_id}", json={"status": "archived"})
    assert archived.status_code == 200
    assert archived.json()["run"]["status"] == "archived"

    dashboard = client.get("/scheduler/dashboard")
    assert all(run["id"] != run_id for run in dashboard.json()["runs"])


def test_scheduler_api_updates_definition_schedule(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    created = client.post(
        "/scheduler/definitions",
        json={
            "key": "agent-daily-brief",
            "name": "Agent Daily Brief",
            "domain_key": "personal",
            "trigger_type": "recurring",
            "trigger_config": {"time_of_day": "08:00", "interval_minutes": 1440},
            "workflow_spec": {"queue_items": [{"id": "brief", "objective": "Brief Chris."}]},
        },
    )
    definition_id = created.json()["definition"]["id"]

    updated = client.patch(
        f"/scheduler/definitions/{definition_id}",
        json={
            "key": "ignored-on-patch",
            "name": "Agent Daily Brief",
            "domain_key": "personal",
            "trigger_type": "recurring",
            "trigger_config": {"time_of_day": "07:30", "interval_minutes": 1440},
            "workflow_spec": {"queue_items": [{"id": "brief", "objective": "Brief Chris early."}]},
        },
    )

    assert updated.status_code == 200
    definition = updated.json()["definition"]
    assert definition["key"] == "agent-daily-brief"
    assert definition["trigger_config"]["time_of_day"] == "07:30"
    assert definition["workflow_spec"]["queue_items"][0]["objective"] == "Brief Chris early."


def test_scheduler_worker_run_executes_assigned_agent_item(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    created = client.post(
        "/scheduler/definitions",
        json={
            "key": "praxis-agent-worker-test",
            "name": "Praxis Agent Worker Test",
            "domain_key": "praxis",
            "trigger_type": "recurring",
            "trigger_config": {
                "next_run_at": "2020-01-01T07:55:00+00:00",
                "interval_minutes": 1440,
            },
            "workflow_spec": {
                "queue_items": [
                    {
                        "id": "brief",
                        "objective": "Prepare a brief scheduler worker report.",
                        "domain_key": "praxis",
                        "agent_key": "praxis-planning-agent",
                    }
                ]
            },
        },
    )
    assert created.status_code == 200

    worker = client.post(
        "/scheduler/worker/run",
        json={
            "owner": "api-worker-test",
            "claim_limit": 2,
            "execute_llm": False,
            "auto_tool_loop": False,
        },
    )

    assert worker.status_code == 200
    payload = worker.json()
    assert len(payload["enqueued"]) == 1
    assert len(payload["claimed"]) == 1
    assert len(payload["executed"]) == 1
    assert payload["executed"][0]["status"] == "completed"
    assert payload["executed"][0]["queue_item"]["status"] == "completed"
    assert payload["executed"][0]["agent_run"]["status"] == "prepared"


def test_scheduler_worker_blocks_unassigned_item(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    client.post(
        "/scheduler/definitions",
        json={
            "key": "unassigned-worker-test",
            "name": "Unassigned Worker Test",
            "domain_key": "praxis",
            "trigger_type": "recurring",
            "trigger_config": {
                "next_run_at": "2020-01-01T07:55:00+00:00",
                "interval_minutes": 1440,
            },
            "workflow_spec": {
                "queue_items": [
                    {
                        "id": "triage",
                        "objective": "Triage without an agent.",
                        "domain_key": "praxis",
                    }
                ]
            },
        },
    )

    worker = client.post(
        "/scheduler/worker/run",
        json={"owner": "api-worker-test", "claim_limit": 2, "execute_llm": False},
    )

    assert worker.status_code == 200
    executed = worker.json()["executed"][0]
    assert executed["status"] == "blocked"
    assert executed["queue_item"]["status"] == "blocked"
    assert "No agent" in executed["queue_item"]["error_message"]
