from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import (
    Artifact,
    Conversation,
    Domain,
    Message,
    Report,
    Task,
    WorkflowNotification,
    WorkflowQueueItem,
    WorkflowRun,
    WorkflowRunLogEntry,
)
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.maestro.scheduler_worker import SchedulerWorkerService
from app.maestro.workflow_outputs import WorkflowOutputService


def _client(session: Session, tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_scheduler_completion_uses_agent_conversation_field(session: Session) -> None:
    service = SchedulerWorkerService(session)
    output_text = json.dumps(
        {
            "format": "structured_report",
            "conversation": (
                "Chris, I triaged the latest Praxis email. It was an informational receipt, "
                "so I filed the organization and did not create a task or notification."
            ),
            "summary": {"classification": "useful_info"},
        }
    )
    agent_run = SimpleNamespace(
        run_id="run-1",
        status="completed",
        agent=SimpleNamespace(key="praxis-email-agent", name="Praxis Email Agent"),
        task_id="task-1",
        report_id="report-1",
        execution_note="Completed.",
        output_text=output_text,
        tool_calls=[],
        staged_artifact_path=None,
        artifact_id=None,
        error_message=None,
    )
    payload = service._agent_run_payload(agent_run)
    queue_item = SimpleNamespace(output_payload=payload, external_key="email-triage")
    run = SimpleNamespace(input_payload={"summary": "Triage the latest Praxis email."})

    message = service._delivery_completion_message(run, [queue_item])

    assert payload["conversation"].startswith("Chris, I triaged")
    assert message == payload["conversation"]
    assert "structured_report" not in message


def test_scheduler_completion_recovers_conversation_from_legacy_preview(
    session: Session,
) -> None:
    service = SchedulerWorkerService(session)
    queue_item = SimpleNamespace(
        external_key="email-triage",
        output_payload={
            "agent_name": "Praxis Email Agent",
            "output_preview": (
                '{"format":"structured_report","conversation":"I reviewed the email and '
                'nothing needs your attention.","summary":{"classification":"useful_info"'
            ),
        },
    )
    run = SimpleNamespace(input_payload={"summary": "Triage the latest Praxis email."})

    message = service._delivery_completion_message(run, [queue_item])

    assert message == "I reviewed the email and nothing needs your attention."


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


def test_workflow_outputs_api_archives_reports(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    domain = session.query(Domain).filter(Domain.key == "praxis").one()
    task = Task(
        domain_id=domain.id,
        status="completed",
        priority="normal",
        source_type="test",
        workflow_key="test.report",
        objective="Write a report.",
        input_payload={},
    )
    session.add(task)
    session.flush()
    report = Report(
        task_id=task.id,
        domain_id=domain.id,
        title="Messy test report",
        report_type="workflow_report",
        summary="Old report shape to hide.",
        body_markdown="## Old Report\nNeeds cleanup later.",
        structured_data={},
    )
    session.add(report)
    session.commit()

    visible = client.get("/workflow-outputs/reports")
    assert visible.status_code == 200
    assert visible.json()["reports"][0]["id"] == str(report.id)

    archived = client.patch(f"/workflow-outputs/reports/{report.id}/archive")
    assert archived.status_code == 200
    assert archived.json()["report"]["archived"] is True

    hidden = client.get("/workflow-outputs/reports")
    assert hidden.status_code == 200
    assert hidden.json()["reports"] == []

    included = client.get("/workflow-outputs/reports?include_archived=true")
    assert included.status_code == 200
    assert included.json()["reports"][0]["archived"] is True


def test_run_log_extracts_routed_ids_from_agent_tool_results(session: Session) -> None:
    routed_ids = WorkflowOutputService(session)._routed_item_ids(
        {
            "tool_calls": [
                {
                    "tool_name": "routed.item.create",
                    "status": "complete",
                    "output_payload": {
                        "items": [
                            {"id": "routed-contact-1", "route_type": "contact"},
                            {"id": "routed-event-1", "route_type": "event"},
                        ]
                    },
                }
            ]
        }
    )

    assert routed_ids == ["routed-contact-1", "routed-event-1"]


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


def test_scheduler_tick_deconflicts_duplicate_agent_locks(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    for index in range(2):
        response = client.post(
            "/scheduler/definitions",
            json={
                "key": f"praxis-same-agent-{index}",
                "name": f"Praxis Same Agent {index}",
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
                            "objective": "Prepare a Praxis brief.",
                            "domain_key": "praxis",
                            "agent_key": "praxis-planning-agent",
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
    assert len(payload["enqueued"]) == 2
    assert len(payload["claimed"]) == 1
    assert payload["claimed"][0]["lease_owner"] == "api-test"


def test_scheduler_lock_row_is_reused_after_release(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    client.post(
        "/scheduler/definitions",
        json={
            "key": "praxis-lock-reuse",
            "name": "Praxis Lock Reuse",
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
                        "objective": "Prepare a Praxis brief.",
                        "domain_key": "praxis",
                        "agent_key": "praxis-planning-agent",
                    }
                ]
            },
        },
    )
    tick = client.post("/scheduler/tick", json={"owner": "api-test", "claim_limit": 1})
    queue_item_id = tick.json()["claimed"][0]["id"]

    released = client.post(f"/scheduler/queue-items/{queue_item_id}/locks/release")
    assert released.status_code == 200
    reacquired = client.post(f"/scheduler/queue-items/{queue_item_id}/locks/acquire")

    assert reacquired.status_code == 200
    assert len(reacquired.json()["locks"]) == 1


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
    assert archived.json()["run"]["queue_items"][0]["status"] == "archived"
    assert session.query(WorkflowQueueItem).filter_by(workflow_run_id=uuid.UUID(run_id)).one().status == "archived"

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
    message = session.query(Message).order_by(Message.created_at.desc()).first()
    assert message is not None
    assert message.sender_type == "maestro"
    assert "I finished the scheduled workflow" in message.content
    assert "What came back:" in message.content
    assert message.metadata_["source"] == "scheduler_worker"
    assert message.metadata_["event_type"] == "workflow_completed"
    assert message.metadata_["channel_visibility"] == "global"
    run = session.query(WorkflowRun).one()
    assert run.status == "completed"
    if run.parent_task_id is not None:
        parent = session.get(Task, run.parent_task_id)
        assert parent is not None
        assert parent.status == "completed"
        assert parent.output_payload["chat_summary"].startswith("I finished the scheduled workflow")
    assert run.output_payload["staged_artifact_path"]
    assert run.output_payload["completion_channel_message_posted"] is True
    run_log = session.query(WorkflowRunLogEntry).one()
    assert run_log.workflow_run_id == run.id
    assert run_log.title == "Praxis Agent Worker Test"
    assert run_log.status == "completed"
    assert run_log.agent_work[0]["external_key"] == "brief"
    assert run_log.agent_work[0]["agent_key"] == "praxis-planning-agent"
    assert run.output_payload["artifact_id"] in run_log.artifact_ids
    notification = session.query(WorkflowNotification).one()
    assert notification.workflow_run_id == run.id
    assert notification.status == "delivered"
    assert notification.notification_type == "workflow_completed"
    staged_path = Path(run.output_payload["staged_artifact_path"])
    assert staged_path.is_file()
    assert staged_path.parent == tmp_path / "praxis" / "inbox"
    canonical_artifact = next(
        artifact
        for artifact in session.query(Artifact).all()
        if (artifact.metadata_ or {}).get("canonical_scheduled_workflow_artifact") is True
    )
    assert canonical_artifact.uri == str(staged_path)

    dashboard = client.get("/scheduler/dashboard")
    assert dashboard.status_code == 200
    assert all(run["status"] != "completed" for run in dashboard.json()["runs"])

    run_log_response = client.get("/workflow-outputs/run-log")
    assert run_log_response.status_code == 200
    assert run_log_response.json()["entries"][0]["workflow_run_id"] == str(run.id)
    notifications = client.get("/workflow-outputs/notifications?status=delivered")
    assert notifications.status_code == 200
    assert notifications.json()["notifications"][0]["workflow_run_id"] == str(run.id)


def test_approved_delivery_finalizes_run_with_archived_superseded_items(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    get_settings.cache_clear()
    get_settings().memory_dropbox_root = str(tmp_path)
    domain = session.query(Domain).filter(Domain.key == "maestro-development").one()
    conversation = Conversation(
        domain_id=domain.id,
        title="Coding workflow regression",
        metadata_={"channel": "maestro_primary"},
    )
    session.add(conversation)
    session.flush()
    parent = Task(
        conversation_id=conversation.id,
        domain_id=domain.id,
        status="running",
        priority="normal",
        source_type="maestro",
        workflow_key="maestro.generic",
        objective="Implement a UI change and deploy it after approval.",
        input_payload={"plan_summary": "Implement, review, merge, and reload."},
    )
    session.add(parent)
    session.flush()
    child = Task(
        parent_task_id=parent.id,
        conversation_id=conversation.id,
        domain_id=domain.id,
        status="blocked",
        priority="normal",
        source_type="scheduler",
        workflow_key="agent.execute",
        objective="Implement the UI change and open a pull request.",
        input_payload={},
        error_message="Waiting for Chris to review PR #95 and approve delivery.",
    )
    session.add(child)
    session.flush()
    run = WorkflowRun(
        parent_task_id=parent.id,
        conversation_id=conversation.id,
        domain_id=domain.id,
        source_type="manual",
        status="blocked",
        priority="normal",
        input_payload={"summary": "Implement a UI change and deploy it after approval."},
        started_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    active_item = WorkflowQueueItem(
        workflow_run_id=run.id,
        parent_task_id=parent.id,
        child_task_id=child.id,
        domain_id=domain.id,
        external_key="implement_change",
        status="blocked",
        priority="normal",
        stage_index=1,
        position=1,
        objective=child.objective,
        dependency_keys=[],
        resource_locks=[],
        input_payload={},
        output_payload={
            "agent_run": {
                "task_id": str(child.id),
                "agent_key": "maestro-chief-engineer",
                "agent_name": "Maestro Chief Engineer",
                "status": "blocked",
                "output_preview": "PR #95 is ready for review.",
                "tool_calls": [],
            }
        },
        error_message="Waiting for delivery approval.",
    )
    superseded_item = WorkflowQueueItem(
        workflow_run_id=run.id,
        parent_task_id=parent.id,
        domain_id=domain.id,
        external_key="superseded_plan_item",
        status="archived",
        priority="normal",
        stage_index=1,
        position=2,
        objective="A superseded planning item.",
        dependency_keys=[],
        resource_locks=[],
        input_payload={},
        output_payload={"status": "archived"},
    )
    session.add_all([active_item, superseded_item])
    session.commit()

    completed_run = SchedulerWorkerService(session).complete_approved_delivery(
        task_id=child.id,
        delivery_result={
            "tool_name": "local.app.deploy_pr",
            "status": "complete",
            "output_payload": {
                "summary": {"pr_number": 95, "merged": True, "reloaded": True},
                "write_status": "merged_and_reloaded",
            },
        },
    )

    assert completed_run is not None
    session.refresh(completed_run)
    session.refresh(parent)
    session.refresh(child)
    session.refresh(active_item)
    assert completed_run.status == "completed"
    assert parent.status == "completed"
    assert child.status == "completed"
    assert child.error_message is None
    assert active_item.status == "completed"
    assert active_item.error_message is None
    assert superseded_item.status == "archived"
    assert completed_run.output_payload["staged_artifact_path"]
    assert session.query(WorkflowRunLogEntry).filter_by(workflow_run_id=run.id).count() == 1
    notification = session.query(WorkflowNotification).filter_by(workflow_run_id=run.id).one()
    assert notification.status == "delivered"
    assert "merged PR #95" in notification.message
    assert "reloaded successfully" in notification.message
    completion = (
        session.query(Message)
        .filter(Message.metadata_["event_type"].as_string() == "workflow_completed")
        .order_by(Message.created_at.desc())
        .first()
    )
    assert completion is not None
    assert completion.metadata_["event_type"] == "workflow_completed"
    assert "merged PR #95" in completion.content


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


def test_scheduler_worker_status_can_be_toggled_at_runtime(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)

    status = client.get("/scheduler/worker/status")

    assert status.status_code == 200
    assert status.json()["worker"]["enabled"] is False
    assert status.json()["worker"]["source"] == "env"

    updated = client.patch(
        "/scheduler/worker/status",
        json={
            "enabled": True,
            "interval_seconds": 15,
            "claim_limit": 3,
            "execute_llm": False,
            "auto_tool_loop": False,
        },
    )

    assert updated.status_code == 200
    worker = updated.json()["worker"]
    assert worker["enabled"] is True
    assert worker["interval_seconds"] == 15
    assert worker["claim_limit"] == 3
    assert worker["execute_llm"] is False
    assert worker["auto_tool_loop"] is False
    assert worker["source"] == "runtime"

    reloaded = client.get("/scheduler/worker/status")
    assert reloaded.json()["worker"]["enabled"] is True
