from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import Domain, ToolConnection, WorkflowDefinition
from app.db.session import get_db
from app.maestro.workflow_templates import (
    PRAXIS_EMAIL_AGENT_KEY,
    PRAXIS_EMAIL_SKILLS,
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
            config={"user_id": "me"},
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
    }
    item = definition.workflow_spec["queue_items"][0]
    assert item["agent_key"] == PRAXIS_EMAIL_AGENT_KEY
    assert item["required_skills"] == PRAXIS_EMAIL_SKILLS
    assert item["model_profile"] == "openrouter:openai/gpt-5.6-luna"
    assert item["max_attempts"] == 3
    assert "payload.message_id" in item["objective"]
    assert "latest email" in item["objective"]


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
