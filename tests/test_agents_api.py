from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import MemoryItem
from app.db.repositories import DomainRepository
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


def test_list_agents_returns_seeded_runtime_specs(session: Session, tmp_path: Path) -> None:
    client = _client(session, tmp_path)

    response = client.get("/agents")

    assert response.status_code == 200
    agents = response.json()["agents"]
    praxis = next(agent for agent in agents if agent["key"] == "praxis-planning-agent")
    assert praxis["domain_key"] == "praxis"
    assert praxis["memory_profile"] == "agent_prompt"
    assert {tool["key"] for tool in praxis["allowed_tools"]} == {
        "artifact.stage_interaction",
        "llm.gateway",
        "memory.context_bundle",
    }


def test_prompt_package_endpoint_returns_scoped_prompt(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    ophi = DomainRepository(session).get_by_key("ophi")
    assert praxis is not None
    assert ophi is not None
    session.add_all(
        [
            MemoryItem(
                scope="domain",
                domain_id=praxis.id,
                memory_type="fact",
                title="Praxis partner call",
                content="Praxis partner calls require transition and training context.",
                impact_level="medium",
                importance=0.9,
                metadata_={},
            ),
            MemoryItem(
                scope="domain",
                domain_id=ophi.id,
                memory_type="fact",
                title="Ophi unrelated context",
                content="This must not leak into Praxis prompt packages.",
                impact_level="medium",
                importance=1.0,
                metadata_={},
            ),
        ]
    )
    session.commit()
    client = _client(session, tmp_path)

    response = client.post(
        "/agents/praxis-planning-agent/prompt-package",
        json={
            "task_instruction": "Prepare context for a Praxis partner follow-up call.",
            "query_text": "Praxis partner call",
            "use_semantic": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()["prompt_package"]
    assert payload["agent"]["domain_key"] == "praxis"
    assert "Praxis partner call" in payload["assembled_prompt"]
    assert "Ophi unrelated context" not in payload["assembled_prompt"]
    assert payload["memory_context"]["included_count"] >= 1


def test_domain_context_update_changes_prompt_package(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    update = client.patch(
        "/agents/domains/praxis",
        json={"context": "Praxis UI-edited context for agent prompts."},
    )
    prompt = client.post(
        "/agents/praxis-planning-agent/prompt-package",
        json={
            "task_instruction": "Prepare a Praxis brief.",
            "use_semantic": False,
        },
    )

    assert update.status_code == 200
    assert update.json()["domain"]["context"] == "Praxis UI-edited context for agent prompts."
    assert prompt.status_code == 200
    assert "Praxis UI-edited context" in prompt.json()["prompt_package"]["assembled_prompt"]


def test_global_context_update_changes_prompt_package(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    update = client.patch(
        "/agents/global-context",
        json={"context": "Global context edited through UI."},
    )
    prompt = client.post(
        "/agents/praxis-planning-agent/prompt-package",
        json={
            "task_instruction": "Prepare a Praxis brief.",
            "use_semantic": False,
        },
    )

    assert update.status_code == 200
    assert update.json()["global_context"]["context"] == "Global context edited through UI."
    assert prompt.status_code == 200
    assembled_prompt = prompt.json()["prompt_package"]["assembled_prompt"]
    assert "Global context edited through UI." in assembled_prompt


def test_create_agent_endpoint_adds_domain_agent(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    response = client.post(
        "/agents",
        json={
            "domain_key": "praxis",
            "key": "Praxis Email Agent",
            "name": "Praxis Email Agent",
            "role_summary": "Triages Praxis inbox.",
            "tool_permissions": {"gmail.read": {"permission": "read"}},
        },
    )

    assert response.status_code == 200
    agent = response.json()["agent"]
    assert agent["key"] == "praxis-email-agent"
    assert agent["domain_key"] == "praxis"
    assert agent["allowed_tools"][0]["key"] == "gmail.read"


def test_agent_update_and_tool_registry_endpoint(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    update = client.patch(
        "/agents/praxis-planning-agent",
        json={
            "role_summary": "Updated Praxis planning role.",
            "current_action": "Drafting partner-call prep.",
            "tool_permissions": {
                "memory.context_bundle": {
                    "permission": "read",
                    "description": "Read Praxis memory.",
                }
            },
        },
    )
    tools = client.get("/agents/tools")

    assert update.status_code == 200
    agent = update.json()["agent"]
    assert agent["role_summary"] == "Updated Praxis planning role."
    assert agent["current_action"] == "Drafting partner-call prep."
    assert [tool["key"] for tool in agent["allowed_tools"]] == ["memory.context_bundle"]
    assert tools.status_code == 200
    memory_tool = next(
        tool for tool in tools.json()["tools"] if tool["key"] == "memory.context_bundle"
    )
    assert any(
        authorized["agent_key"] == "praxis-planning-agent"
        for authorized in memory_tool["authorized_agents"]
    )


def test_tool_connection_endpoint_redacts_config(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    update = client.put(
        "/agents/tools/connections",
        json={
            "domain_key": "praxis",
            "tool_key": "gmail.read",
            "display_name": "Praxis Gmail",
            "auth_type": "api_key",
            "config": {"api_key": "secret-value", "label": "praxis"},
            "is_active": True,
        },
    )
    connections = client.get("/agents/tools/connections")

    assert update.status_code == 200
    assert update.json()["connection"]["config"]["api_key"] == "********"
    assert connections.status_code == 200
    assert connections.json()["connections"][0]["display_name"] == "Praxis Gmail"


def test_run_once_endpoint_prepares_stubbed_run(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    response = client.post(
        "/agents/praxis-planning-agent/run-once",
        json={
            "task_instruction": "Prepare a Praxis brief.",
            "query_text": "Praxis brief",
            "use_semantic": False,
            "stage_interaction": True,
            "execute_llm": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()["run"]
    assert payload["status"] == "prepared"
    assert payload["scheduler"]["status"] == "stubbed"
    assert payload["prompt_package"]["agent"]["key"] == "praxis-planning-agent"
    assert payload["task_id"] is not None
    assert payload["output_text"] is None
    assert payload["staged_artifact_path"] is not None


def test_interaction_artifact_endpoint_can_stage_package(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    response = client.post(
        "/agents/interaction-artifacts",
        json={
            "domain_key": "praxis",
            "agent_key": "praxis-planning-agent",
            "user_input": "Prep the call.",
            "maestro_tasking": "Build a concise brief.",
            "agent_output": "Use partner context and transition risks.",
            "tool_calls": [{"tool_name": "memory.context_bundle", "status": "complete"}],
            "generated_artifacts": [{"name": "brief.md", "uri": "reports/brief.md"}],
            "open_questions": ["Who owns follow-up?"],
            "next_steps": ["Draft agenda."],
            "stage": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["staged_path"] is not None
    assert Path(payload["staged_path"]).is_file()
    assert payload["artifact_package"]["schema_version"] == "maestro.interaction_artifact.v1"
    assert payload["artifact_package"]["domain_key"] == "praxis"
