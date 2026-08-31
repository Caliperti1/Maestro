from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.agents.runtime import AgentRegistryService
from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import (
    Artifact,
    Agent,
    Conversation,
    Domain,
    LLMCallLog,
    Message,
    Report,
    Task,
    WorkflowDefinition,
    WorkflowNotification,
    WorkflowQueueItem,
    WorkflowRun,
    WorkflowRunLogEntry,
)
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.maestro.scheduler import SchedulerService
from app.maestro.scheduler_worker import (
    SchedulerWorkerService,
    _approved_tool_results,
    _agent_run_blocker_message,
)
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


def test_scheduler_dashboard_excludes_terminal_failed_runs(session: Session) -> None:
    seed_default_domains(session)
    domain = session.query(Domain).filter(Domain.key == "maestro-development").one()
    failed = WorkflowRun(
        domain_id=domain.id,
        source_type="repository_intelligence",
        status="failed",
        priority="low",
        input_payload={},
        error_message="Historical failure.",
    )
    blocked = WorkflowRun(
        domain_id=domain.id,
        source_type="maestro",
        status="blocked",
        priority="normal",
        input_payload={},
    )
    session.add_all([failed, blocked])
    session.commit()

    dashboard = SchedulerService(session).dashboard()

    assert [run["id"] for run in dashboard["runs"]] == [str(blocked.id)]


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
        tool_calls=[{
            "tool_name": "llm.email_triage_finalizer",
            "status": "complete",
            "output_payload": {"shadow_mode": True},
        }],
        staged_artifact_path=None,
        artifact_id=None,
        error_message=None,
        email_triage_decision={"classification": "useful_information"},
    )
    payload = service._agent_run_payload(agent_run)
    queue_item = SimpleNamespace(output_payload=payload, external_key="email-triage")
    run = SimpleNamespace(input_payload={"summary": "Triage the latest Praxis email."})

    message = service._delivery_completion_message(run, [queue_item])

    assert payload["conversation"].startswith("Chris, I triaged")
    assert payload["email_triage_decision"]["classification"] == "useful_information"
    assert payload["email_triage_shadow_mode"] is True
    assert message == payload["conversation"]
    assert "structured_report" not in message


def test_daily_standup_completion_uses_synthesis_conversation(session: Session) -> None:
    definition = WorkflowDefinition(
        key="daily-standup",
        name="Daily Standup",
        trigger_type="manual",
        trigger_config={},
        workflow_spec={},
    )
    session.add(definition)
    session.commit()
    service = SchedulerWorkerService(session)
    run = SimpleNamespace(
        workflow_definition_id=definition.id,
        input_payload={"summary": "Daily Standup"},
    )
    items = [
        SimpleNamespace(
            external_key="personal-input",
            output_payload={
                "agent_name": "Personal Operations Agent",
                "conversation": "Personal input is ready.",
            },
        ),
        SimpleNamespace(
            external_key="standup-synthesis",
            output_payload={
                "agent_name": "Maestro Briefing Agent",
                "conversation": (
                    "Chris, your standup is ready. You have two fixed commitments and three "
                    "recommended focus blocks to review."
                ),
            },
        ),
    ]

    message = service._delivery_completion_message(run, items)

    assert message.startswith("Chris, your standup is ready")
    assert "Personal input is ready" not in message

    items[-1].output_payload["report_id"] = "e1f4f324-442e-4b90-8f50-e85e9885b63d"
    context = service._completion_context(run, items)
    assert context == {
        "workflow_key": "daily-standup",
        "standup_report_id": "e1f4f324-442e-4b90-8f50-e85e9885b63d",
        "synthesis_report_id": "e1f4f324-442e-4b90-8f50-e85e9885b63d",
        "standup_context_active": True,
    }


def test_email_completion_posts_one_conversational_message_and_delivers_existing_notification(
    session: Session,
) -> None:
    seed_default_domains(session)
    praxis = session.query(Domain).filter(Domain.key == "praxis").one()
    run = WorkflowRun(
        domain_id=praxis.id,
        source_type="event",
        status="completed",
        priority="normal",
        input_payload={"summary": "Triage one Praxis email."},
        output_payload={},
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    decision = {
        "classification": "action_required",
        "conversation": (
            "Chris, I triaged Jane's email. You need to confirm the August 4 meeting, and I "
            "saved the event, follow-up, contact, and organization."
        ),
        "memory_worthy": False,
        "notification": {"should_notify": True},
    }
    session.add(
        WorkflowQueueItem(
            workflow_run_id=run.id,
            domain_id=praxis.id,
            external_key="email-triage",
            status="completed",
            objective="Triage the trigger email.",
            dependency_keys=[],
            resource_locks=[],
            input_payload={},
            output_payload={
                "agent_run": {
                    "agent_name": "Praxis Email Agent",
                    "conversation": decision["conversation"],
                    "email_triage_decision": decision,
                    "tool_calls": [],
                }
            },
        )
    )
    notification = WorkflowNotification(
        workflow_run_id=run.id,
        domain_id=praxis.id,
        severity="warning",
        status="pending",
        title="Partner meeting needs confirmation",
        message="Confirm the August 4 partner meeting.",
        notification_type="email_attention",
        target="maestro_chat",
        metadata_={"delivery_policy": "workflow_completion"},
    )
    session.add(notification)
    session.commit()

    message = SchedulerWorkerService(session)._post_workflow_completion_update(run)

    assert message == decision["conversation"]
    channel_messages = session.query(Message).all()
    assert len(channel_messages) == 1
    assert channel_messages[0].content == decision["conversation"]
    assert channel_messages[0].metadata_["event_type"] == "email_attention"
    session.refresh(notification)
    assert notification.status == "delivered"
    assert (
        session.query(WorkflowNotification)
        .filter_by(workflow_run_id=run.id, notification_type="workflow_completed")
        .count()
        == 0
    )


def test_quiet_email_completion_does_not_post_to_primary_channel(session: Session) -> None:
    seed_default_domains(session)
    praxis = session.query(Domain).filter(Domain.key == "praxis").one()
    run = WorkflowRun(
        domain_id=praxis.id,
        source_type="event",
        status="completed",
        priority="normal",
        input_payload={"summary": "Triage one quiet Praxis email."},
        output_payload={},
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    session.add(
        WorkflowQueueItem(
            workflow_run_id=run.id,
            domain_id=praxis.id,
            external_key="email-triage",
            status="completed",
            objective="Triage the trigger email.",
            dependency_keys=[],
            resource_locks=[],
            input_payload={},
            output_payload={
                "agent_run": {
                    "conversation": "I recorded the useful update; nothing needs your attention.",
                    "email_triage_decision": {
                        "classification": "useful_information",
                        "memory_worthy": False,
                        "notification": {"should_notify": False},
                    },
                    "tool_calls": [],
                }
            },
        )
    )
    session.commit()

    message = SchedulerWorkerService(session)._post_workflow_completion_update(run)

    assert message == ""
    assert session.query(Message).count() == 0
    session.refresh(run)
    assert run.output_payload["completion_channel_message_suppressed"] is True


def test_email_memory_artifact_is_staged_only_for_separate_durable_context(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    get_settings.cache_clear()
    get_settings().memory_dropbox_root = str(tmp_path)
    praxis = session.query(Domain).filter(Domain.key == "praxis").one()

    def add_run(*, memory_worthy: bool, memory_summary: str) -> WorkflowRun:
        run = WorkflowRun(
            domain_id=praxis.id,
            source_type="event",
            status="completed",
            priority="normal",
            input_payload={"summary": "Triage one Praxis email."},
            output_payload={},
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        session.add(
            WorkflowQueueItem(
                workflow_run_id=run.id,
                domain_id=praxis.id,
                external_key="email-triage",
                status="completed",
                objective="Triage the trigger email.",
                dependency_keys=[],
                resource_locks=[],
                input_payload={},
                output_payload={
                    "agent_run": {
                        "email_triage_decision": {
                            "classification": "useful_information",
                            "summary": "The email was triaged.",
                            "memory_worthy": memory_worthy,
                            "memory_summary": memory_summary,
                            "notification": {"should_notify": False},
                        },
                        "tool_calls": [],
                    }
                },
            )
        )
        session.commit()
        return run

    skipped_run = add_run(memory_worthy=False, memory_summary="")
    staged_run = add_run(
        memory_worthy=True,
        memory_summary="Jane Smith is the durable Praxis relationship owner at Example Corp.",
    )
    worker = SchedulerWorkerService(session)

    worker._stage_completed_workflow_run(skipped_run)
    worker._stage_completed_workflow_run(staged_run)

    session.refresh(skipped_run)
    session.refresh(staged_run)
    assert skipped_run.output_payload["memory_curation_status"] == "skipped_not_durable"
    assert skipped_run.output_payload.get("artifact_id") is None
    assert staged_run.output_payload["memory_curation_status"] == "staged"
    assert staged_run.output_payload["artifact_id"]
    assert Path(staged_run.output_payload["staged_artifact_path"]).is_file()


def test_scheduler_blocker_message_explains_the_pending_tool_action() -> None:
    agent_run = SimpleNamespace(
        error_message="Agent run is blocked.",
        tool_calls=[
            {
                "status": "approval_required",
                "output_payload": {
                    "approval_preview": {
                        "summary": "Archive Gmail message `msg-1`.",
                        "rationale": "The email was classified as noise.",
                    }
                },
            }
        ],
    )

    assert _agent_run_blocker_message(agent_run) == (
        "Archive Gmail message `msg-1`. Reason: The email was classified as noise."
    )


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


def test_scheduler_api_runs_active_manual_definition_with_parameters(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    created = client.post(
        "/scheduler/definitions",
        json={
            "key": "project-scrum",
            "name": "Project Scrum",
            "trigger_type": "manual",
            "trigger_config": {
                "parameter_schema": {
                    "type": "object",
                    "properties": {"project": {"type": "string"}},
                    "required": ["project"],
                    "additionalProperties": False,
                }
            },
            "workflow_spec": {"queue_items": []},
            "is_active": True,
        },
    )
    definition_id = created.json()["definition"]["id"]

    missing = client.post(f"/scheduler/definitions/{definition_id}/run", json={})
    started = client.post(
        f"/scheduler/definitions/{definition_id}/run",
        json={"parameters": {"project": "GroundTruth"}},
    )

    assert missing.status_code == 409
    assert "project" in missing.json()["detail"]
    assert started.status_code == 200
    assert started.json()["run"]["source_type"] == "knowledge_on_demand"
    assert started.json()["run"]["input_payload"]["invocation"]["parameters"] == {
        "project": "GroundTruth"
    }


def test_scheduler_api_controls_gmail_trigger_worker(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)

    initial = client.get("/scheduler/triggers/gmail/status")
    updated = client.patch(
        "/scheduler/triggers/gmail/status",
        json={"enabled": True, "interval_seconds": 45, "page_size": 125},
    )

    assert initial.status_code == 200
    assert initial.json()["worker"]["enabled"] is False
    assert updated.status_code == 200
    assert updated.json()["worker"] == {
        "enabled": True,
        "interval_seconds": 45,
        "page_size": 125,
        "source": "runtime",
    }


def test_scheduler_api_replays_event_run_with_original_message_payload(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    created = client.post(
        "/scheduler/definitions",
        json={
            "key": "praxis-email-replay-test",
            "name": "Praxis Email Replay Test",
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
                        "objective": "Triage the exact trigger message.",
                        "domain_key": "praxis",
                    }
                ]
            },
        },
    )
    assert created.status_code == 200
    event = client.post(
        "/scheduler/triggers/event",
        json={
            "event_type": "gmail.message.received",
            "event_id": "praxis:msg-replay",
            "event_payload": {"domain_key": "praxis", "message_id": "msg-replay"},
        },
    )
    original = event.json()["runs"][0]

    replay = client.post(f"/scheduler/runs/{original['id']}/replay")

    assert replay.status_code == 200
    payload = replay.json()["run"]
    assert payload["source_type"] == "replay"
    assert payload["id"] != original["id"]
    assert payload["input_payload"]["event"]["payload"]["message_id"] == "msg-replay"


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


def test_workflow_outputs_api_attributes_llm_usage_to_workflow_run(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    client = _client(session, tmp_path)
    praxis = session.query(Domain).filter(Domain.key == "praxis").one()
    run = WorkflowRun(
        domain_id=praxis.id,
        source_type="event",
        status="completed",
        priority="normal",
        input_payload={"summary": "Triage one email."},
        output_payload={},
    )
    session.add(run)
    session.flush()
    session.add(
        LLMCallLog(
            workflow_run_id=run.id,
            component="agent.email_triage_finalizer",
            provider="openrouter",
            model="openai/gpt-5.6-luna",
            status="complete",
            prompt_chars=4200,
            prompt_tokens=1100,
            completion_tokens=240,
            cached_tokens=100,
            cost=0.0123,
            prompt_sections={"evidence": 3100},
            metadata_={"evidence_result_count": 2},
        )
    )
    session.commit()

    response = client.get(f"/workflow-outputs/llm-calls?workflow_run_id={run.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "call_count": 1,
        "prompt_tokens": 1100,
        "completion_tokens": 240,
        "cached_tokens": 100,
        "cost": 0.0123,
    }
    assert payload["calls"][0]["component"] == "agent.email_triage_finalizer"
    assert payload["calls"][0]["prompt_sections"] == {"evidence": 3100}


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


@pytest.mark.parametrize("nested_payload", [True, False])
def test_approved_tool_action_requeues_its_blocked_durable_workflow(
    session: Session,
    tmp_path: Path,
    nested_payload: bool,
) -> None:
    seed_default_domains(session)
    get_settings.cache_clear()
    get_settings().memory_dropbox_root = str(tmp_path)
    domain = session.query(Domain).filter(Domain.key == "praxis").one()
    conversation = Conversation(
        domain_id=domain.id,
        title="Durable Gmail approval",
        metadata_={"channel": "maestro_primary"},
    )
    session.add(conversation)
    session.flush()
    parent = Task(
        conversation_id=conversation.id,
        domain_id=domain.id,
        status="blocked",
        priority="normal",
        source_type="scheduler",
        workflow_key="scheduler.durable",
        objective="Triage new Praxis email.",
        input_payload={},
    )
    session.add(parent)
    session.flush()
    child = Task(
        parent_task_id=parent.id,
        conversation_id=conversation.id,
        domain_id=domain.id,
        status="blocked",
        priority="normal",
        source_type="scheduler_worker",
        workflow_key="scheduler.workflow_item",
        objective="Triage Gmail message msg-1.",
        input_payload={},
        error_message="Waiting for Gmail approval.",
    )
    session.add(child)
    session.flush()
    run = WorkflowRun(
        parent_task_id=parent.id,
        conversation_id=conversation.id,
        domain_id=domain.id,
        source_type="trigger",
        status="blocked",
        priority="normal",
        input_payload={"summary": "Triage Gmail message msg-1."},
        started_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    tool_call_id = str(uuid.uuid4())
    agent_run_payload = {
        "task_id": str(child.id),
        "agent_key": "praxis-email-agent",
        "agent_name": "Praxis Email Agent",
        "status": "blocked",
        "output_preview": "The message was noise.",
        "tool_calls": [
            {
                "id": str(uuid.uuid4()),
                "tool_name": "llm.tool_planner",
                "status": "complete",
                "output_payload": {"plan_summary": "Search before changing the message."},
            },
            {
                "id": str(uuid.uuid4()),
                "tool_name": "github.issue.search",
                "status": "complete",
                "output_payload": {"issues": [{"number": 10, "title": "Related work"}]},
            },
            {
                "id": tool_call_id,
                "tool_name": "gmail.message.modify",
                "status": "approval_required",
                "output_payload": {},
            }
        ],
    }
    item = WorkflowQueueItem(
        workflow_run_id=run.id,
        parent_task_id=parent.id,
        domain_id=domain.id,
        external_key="triage_email",
        status="blocked",
        priority="normal",
        stage_index=1,
        position=1,
        objective=child.objective,
        dependency_keys=[],
        resource_locks=[],
        input_payload={},
        output_payload={"agent_run": agent_run_payload} if nested_payload else agent_run_payload,
        error_message="Waiting for Gmail approval.",
    )
    session.add(item)
    session.commit()

    resumed_run = SchedulerWorkerService(session).resume_approved_tool_action(
        task_id=child.id,
        tool_result={
            "id": tool_call_id,
            "tool_name": "gmail.message.modify",
            "status": "complete",
            "output_payload": {"message_id": "msg-1", "label_ids": []},
        },
    )

    assert resumed_run is not None
    session.refresh(resumed_run)
    session.refresh(child)
    session.refresh(item)
    assert resumed_run.id == run.id
    assert resumed_run.status == "queued"
    assert child.status == "completed"
    assert item.status == "queued"
    approved = _approved_tool_results(item)
    assert approved[0]["tool_name"] == "gmail.message.modify"
    assert approved[0]["status"] == "complete"
    stored_agent_run = item.output_payload.get("agent_run", item.output_payload)
    approved_call = next(
        call for call in stored_agent_run["tool_calls"] if call["id"] == tool_call_id
    )
    assert approved_call["status"] == "complete"
    assert session.query(WorkflowRunLogEntry).filter_by(workflow_run_id=run.id).count() == 0

    AgentRegistryService(session).ensure_seed_agents()
    agent = session.query(Agent).filter(Agent.key == "praxis-email-agent").one()
    item.agent_id = agent.id
    session.commit()

    class ResumedRuntime:
        def run_agent_once(self, request, **kwargs):
            initial = kwargs["initial_tool_results"]
            assert [item["tool_name"] for item in initial] == [
                "github.issue.search",
                "gmail.message.modify",
            ]
            assert initial[0]["output_payload"]["issues"][0]["number"] == 10
            return SimpleNamespace(
                run_id="resumed-run",
                status="completed",
                agent=SimpleNamespace(key=agent.key, name=agent.name),
                task_id="resumed-task",
                report_id=None,
                execution_note="Finished after the approved Gmail action.",
                output_text="I finished processing the approved Gmail action.",
                tool_calls=initial,
                staged_artifact_path=None,
                artifact_id=None,
                error_message=None,
            )

    executed = SchedulerWorkerService(session, runtime=ResumedRuntime()).execute_queue_item(
        item.id,
        execute_llm=True,
        auto_tool_loop=True,
    )

    assert executed["status"] == "completed"
    session.refresh(resumed_run)
    session.refresh(item)
    assert resumed_run.status == "completed"
    assert item.status == "completed"
    assert session.query(WorkflowRunLogEntry).filter_by(workflow_run_id=run.id).count() == 1


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
