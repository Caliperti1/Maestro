from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import Domain, ToolConnection, WorkflowDefinition
from app.db.session import get_db
from app.maestro.workflow_templates import (
    DAILY_STANDUP_KEY,
    L3_OPERATIONS_AGENT_KEY,
    MAESTRO_BRIEFING_AGENT_KEY,
    MAESTRO_OPERATIONS_AGENT_KEY,
    PERTI_CALENDAR_AGENT_KEY,
    PERTI_EMAIL_AGENT_KEY,
    PRAXIS_EMAIL_AGENT_KEY,
    PRAXIS_EMAIL_SKILLS,
    USMA_OPERATIONS_AGENT_KEY,
    WorkflowTemplateService,
)


def _client(session: Session, tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    get_settings().memory_dropbox_root = str(tmp_path)
    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _add_google_connection(session: Session) -> None:
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None
    session.add(
        ToolConnection(
            domain_id=domain.id,
            tool_key="google",
            display_name="Praxis Google Workspace",
            auth_type="oauth",
            config={
                "user_id": "me",
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "refresh",
            },
            is_active=True,
        )
    )
    session.commit()


def test_praxis_email_template_installs_paused_with_canonical_contract(
    session: Session,
) -> None:
    service = WorkflowTemplateService(session)

    definition = service.install("praxis-email-triage")

    assert definition.is_active is False
    assert definition.trigger_type == "event"
    assert definition.trigger_config == {
        "event_type": "gmail.message.received",
        "filters": {"domain_key": "praxis"},
        "gmail_watch_enabled": False,
    }
    assert definition.workflow_spec["shadow_mode"] is True
    item = definition.workflow_spec["queue_items"][0]
    assert item["agent_key"] == PRAXIS_EMAIL_AGENT_KEY
    assert item["required_skills"] == PRAXIS_EMAIL_SKILLS
    assert item["model_profile"] == "openrouter:openai/gpt-5.6-luna"
    assert item["max_attempts"] == 3
    assert "payload.message_id" in item["objective"]
    assert "latest email" in item["objective"]


def test_daily_standup_template_installs_active_with_parallel_domain_inputs(
    session: Session,
) -> None:
    service = WorkflowTemplateService(session)

    definition = service.install(DAILY_STANDUP_KEY, is_active=True)

    assert definition.trigger_type == "manual"
    assert definition.is_active is True
    assert "prepare my daily standup" in definition.trigger_config["invocation_aliases"]
    items = definition.workflow_spec["queue_items"]
    assert [item["stage_index"] for item in items] == [1, 1, 1, 1, 1, 1, 2]
    assert {item["domain_key"] for item in items[:-1]} == {
        "personal",
        "maestro-development",
        "usma",
        "l3",
        "perti-laboratories",
        "praxis",
    }
    assert {item["agent_key"] for item in items[:-1]} >= {
        MAESTRO_OPERATIONS_AGENT_KEY,
        USMA_OPERATIONS_AGENT_KEY,
        L3_OPERATIONS_AGENT_KEY,
    }
    assert items[-1]["agent_key"] == MAESTRO_BRIEFING_AGENT_KEY
    assert set(items[-1]["depends_on"]) == {
        "personal-input",
        "maestro-development-input",
        "usma-input",
        "l3-input",
        "perti-input",
        "praxis-input",
    }
    assert definition.trigger_config["workflow_version"] == "2"
    assert service.readiness(DAILY_STANDUP_KEY)["ready"] is True


def test_praxis_email_template_requires_google_connection_before_activation(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    installed = client.post(
        "/scheduler/templates/praxis-email-triage/install",
        json={"is_active": False},
    )
    assert installed.status_code == 200
    definition_id = installed.json()["definition"]["id"]
    assert installed.json()["template"]["readiness"]["connection_ready"] is False
    dashboard = client.get("/scheduler/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["definitions"][0]["id"] == definition_id
    assert dashboard.json()["definitions"][0]["is_active"] is False

    rejected = client.patch(
        f"/scheduler/definitions/{definition_id}/activation",
        json={"is_active": True},
    )
    assert rejected.status_code == 409
    assert "Google connection" in rejected.json()["detail"]

    _add_google_connection(session)
    activated = client.patch(
        f"/scheduler/definitions/{definition_id}/activation",
        json={"is_active": True},
    )
    assert activated.status_code == 200
    assert activated.json()["definition"]["is_active"] is True
    assert activated.json()["template"]["readiness"]["ready"] is True
    assert activated.json()["definition"]["trigger_config"]["gmail_watch_enabled"] is False


def test_installed_template_enqueues_exact_trigger_message_once(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    installed = client.post(
        "/scheduler/templates/praxis-email-triage/install",
        json={"is_active": False},
    )
    definition_id = installed.json()["definition"]["id"]
    _add_google_connection(session)
    client.patch(
        f"/scheduler/definitions/{definition_id}/activation",
        json={"is_active": True},
    )
    watch = client.patch(
        f"/scheduler/definitions/{definition_id}/gmail-watch",
        json={"enabled": True},
    )
    assert watch.status_code == 200
    assert watch.json()["definition"]["trigger_config"]["gmail_watch_enabled"] is True
    assert watch.json()["worker"]["enabled"] is True
    event = {
        "event_type": "gmail.message.received",
        "event_id": "praxis:msg-004",
        "event_payload": {
            "domain_key": "praxis",
            "message_id": "msg-004",
            "thread_id": "thread-004",
        },
    }

    first = client.post("/scheduler/triggers/event", json=event)
    second = client.post("/scheduler/triggers/event", json=event)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["runs"][0]["id"] == second.json()["runs"][0]["id"]
    run = first.json()["runs"][0]
    assert run["input_payload"]["event"]["payload"]["message_id"] == "msg-004"
    assert run["queue_items"][0]["max_attempts"] == 3
    assert run["queue_items"][0]["model_profile"] == "openrouter:openai/gpt-5.6-luna"
    assert session.query(WorkflowDefinition).count() == 1


def test_praxis_email_watch_is_scoped_to_active_definition(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    installed = client.post(
        "/scheduler/templates/praxis-email-triage/install",
        json={"is_active": False},
    )
    definition_id = installed.json()["definition"]["id"]

    rejected = client.patch(
        f"/scheduler/definitions/{definition_id}/gmail-watch",
        json={"enabled": True},
    )

    assert rejected.status_code == 409
    assert "Activate the workflow" in rejected.json()["detail"]

    _add_google_connection(session)
    client.patch(
        f"/scheduler/definitions/{definition_id}/activation",
        json={"is_active": True},
    )
    ignored = client.post(
        "/scheduler/triggers/event",
        json={
            "event_type": "gmail.message.received",
            "event_id": "praxis:watch-off",
            "event_payload": {"domain_key": "praxis", "message_id": "watch-off"},
        },
    )
    assert ignored.status_code == 200
    assert ignored.json()["runs"] == []

    enabled = client.patch(
        f"/scheduler/definitions/{definition_id}/gmail-watch",
        json={"enabled": True},
    )
    disabled = client.patch(
        f"/scheduler/definitions/{definition_id}/gmail-watch",
        json={"enabled": False},
    )
    assert enabled.json()["worker"]["enabled"] is True
    assert disabled.json()["worker"]["enabled"] is False


def test_praxis_email_shadow_mode_is_per_workflow_definition(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    installed = client.post(
        "/scheduler/templates/praxis-email-triage/install",
        json={"is_active": False},
    )
    definition_id = installed.json()["definition"]["id"]

    assert installed.json()["definition"]["workflow_spec"]["shadow_mode"] is True
    updated = client.patch(
        f"/scheduler/definitions/{definition_id}/shadow-mode",
        json={"enabled": False},
    )

    assert updated.status_code == 200
    assert updated.json()["definition"]["workflow_spec"]["shadow_mode"] is False


def test_perti_email_and_calendar_templates_seed_dedicated_agents(
    session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERTI_GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("PERTI_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PERTI_GOOGLE_CLIENT_REFRESH_TOKEN", "refresh")
    service = WorkflowTemplateService(session)

    email = service.install("perti-email-triage")
    calendar = service.install("perti-calendar-monitor")

    assert email.is_active is False
    assert email.trigger_config["gmail_watch_enabled"] is False
    assert email.workflow_spec["queue_items"][0]["agent_key"] == PERTI_EMAIL_AGENT_KEY
    assert calendar.trigger_config["calendar_watch_enabled"] is False
    assert calendar.workflow_spec["queue_items"][0]["agent_key"] == PERTI_CALENDAR_AGENT_KEY
    assert service.readiness("perti-email-triage")["ready"] is True
    assert service.readiness("perti-calendar-monitor")["ready"] is True


def test_calendar_watch_is_scoped_to_active_template(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERTI_GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("PERTI_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PERTI_GOOGLE_CLIENT_REFRESH_TOKEN", "refresh")
    client = _client(session, tmp_path)
    installed = client.post(
        "/scheduler/templates/perti-calendar-monitor/install",
        json={"is_active": False},
    )
    definition_id = installed.json()["definition"]["id"]

    rejected = client.patch(
        f"/scheduler/definitions/{definition_id}/calendar-watch",
        json={"enabled": True},
    )
    assert rejected.status_code == 409

    activated = client.patch(
        f"/scheduler/definitions/{definition_id}/activation",
        json={"is_active": True},
    )
    enabled = client.patch(
        f"/scheduler/definitions/{definition_id}/calendar-watch",
        json={"enabled": True},
    )

    assert activated.status_code == 200
    assert enabled.status_code == 200
    assert enabled.json()["definition"]["trigger_config"]["calendar_watch_enabled"] is True
    assert enabled.json()["worker"]["enabled"] is True
