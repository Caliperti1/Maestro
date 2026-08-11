from sqlalchemy import select

from app.agents.runtime import AgentRegistryService, _tool_request_is_auto_executable
from app.db.models import Agent, Domain, ToolConnection
from app.tools.runtime import default_tool_adapters


def test_personal_and_perti_agents_receive_isolated_google_and_github_connections(session):
    registry = AgentRegistryService(session)
    agents = registry.ensure_seed_agents()
    registry.ensure_domain_provider_connections()
    domains = {domain.id: domain.key for domain in session.scalars(select(Domain)).all()}
    connections = session.scalars(select(ToolConnection)).all()

    selected_agents = {agent.key: agent for agent in agents if agent.key in {"personal-operations-agent", "perti-operations-agent"}}
    assert set(selected_agents) == {"personal-operations-agent", "perti-operations-agent"}
    assert "google.calendar.events.list" in selected_agents["personal-operations-agent"].tool_permissions
    assert "github.repo.list" in selected_agents["perti-operations-agent"].tool_permissions

    configs = {(domains[connection.domain_id], connection.tool_key): connection.config for connection in connections}
    assert configs[("personal", "google")]["refresh_token_env"] == "PERSONAL_GOOGLE_CLIENT_REFRESH_TOKEN"
    assert configs[("perti-laboratories", "google")]["refresh_token_env"] == "PERTI_GOOGLE_CLIENT_REFRESH_TOKEN"
    assert configs[("personal", "github")]["env_token_name"] == "PERSONAL_GITHUB_TOKEN"
    assert configs[("perti-laboratories", "github")]["env_token_name"] == "PERTI_GITHUB_TOKEN"


def test_google_calendar_reads_are_automatic_and_writes_require_approval():
    adapters = default_tool_adapters()
    assert "google.calendar.events.list" in adapters
    assert "google.calendar.event.create" in adapters
    assert _tool_request_is_auto_executable("google.calendar.events.list", {}) is True
    assert _tool_request_is_auto_executable("google.calendar.event.create", {}) is False
