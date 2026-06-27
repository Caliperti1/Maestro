from pathlib import Path
from subprocess import CompletedProcess

from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentToolRequest,
    AgentRegistryService,
    InteractionArtifactPackager,
    PromptAggregationService,
    PromptPackageRequest,
)
from app.core.config import get_settings
from app.db.models import Artifact, MemoryItem, Task, ToolConnection
from app.db.repositories import AgentRepository, DomainRepository
from app.db.seed import seed_default_domains
from app.tools.runtime import GitHubCliToolAdapter, ToolExecutionContext


def _seed_memory(session: Session) -> None:
    seed_default_domains(session)
    repo = DomainRepository(session)
    praxis = repo.get_by_key("praxis")
    ophi = repo.get_by_key("ophi")
    assert praxis is not None
    assert ophi is not None
    session.add_all(
        [
            MemoryItem(
                scope="global",
                memory_type="preference",
                title="Chris likes concise context",
                content="Chris prefers concise, decision-oriented context with next steps.",
                impact_level="medium",
                importance=0.8,
                metadata_={},
            ),
            MemoryItem(
                scope="domain",
                domain_id=praxis.id,
                memory_type="fact",
                title="Praxis partner follow-up",
                content="Praxis partner follow-ups should connect training and transition needs.",
                impact_level="medium",
                importance=0.9,
                metadata_={},
            ),
            MemoryItem(
                scope="domain",
                domain_id=ophi.id,
                memory_type="fact",
                title="Ophi private roadmap",
                content="This Ophi context must not appear in a Praxis prompt package.",
                impact_level="medium",
                importance=1.0,
                metadata_={},
            ),
        ]
    )
    session.commit()


class FakeAgentLLMClient:
    provider = "test"
    model = "test-agent-model"

    def structured_response(self, **kwargs):
        raise AssertionError("Agent run should use text_response.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "Maestro domain agent" in instructions
        assert "Praxis partner run" in input_text
        return "## Summary\nPraxis partner run completed.\n\n## Next Steps\nSend the brief."


class FakeToolAwareAgentLLMClient:
    provider = "test"
    model = "test-agent-model"

    def structured_response(self, **kwargs):
        raise AssertionError("Agent run should use text_response.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "Tool Results" in input_text
        assert "already been executed by Maestro" in input_text
        assert "do not emit synthetic tool-call markup" in instructions
        assert "Implement GitHub tools" in input_text
        return "## Summary\nReviewed matching GitHub issues.\n\n## Next Steps\nPick the next issue."


class FailingAgentLLMClient:
    provider = "test"
    model = "test-agent-model"

    def structured_response(self, **kwargs):
        raise AssertionError("Agent run should use text_response.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise RuntimeError("provider rejected the request")


class FakeGitHubIssueSearchAdapter:
    key = "github.issue.search"

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, object],
    ) -> dict[str, object]:
        assert context.domain.key == "maestro-development"
        assert context.connection is not None
        assert payload["query"] == "tool integration"
        return {
            "repo": context.connection.config["repo"],
            "issues": [
                {
                    "number": 42,
                    "title": "Implement GitHub tools",
                    "state": "OPEN",
                }
            ],
        }


def test_seed_agent_registry_returns_domain_scoped_specs(session: Session) -> None:
    specs = AgentRegistryService(session).list_specs()

    praxis = next(spec for spec in specs if spec.key == "praxis-planning-agent")
    assert praxis.domain_key == "praxis"
    assert praxis.memory_profile == "agent_prompt"
    assert [tool.key for tool in praxis.allowed_tools] == [
        "artifact.stage_interaction",
        "llm.gateway",
        "memory.context_bundle",
    ]


def test_prompt_aggregation_includes_scoped_memory_and_tools(session: Session) -> None:
    _seed_memory(session)
    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare context for a Praxis partner follow-up call.",
            query_text="partner follow-up",
            use_semantic=False,
        )
    )

    assert package.agent.domain_key == "praxis"
    assert "Praxis Defense" in package.domain_context
    assert "Praxis partner follow-up" in package.assembled_prompt
    assert "Ophi private roadmap" not in package.assembled_prompt
    assert "memory.context_bundle" in package.assembled_prompt
    assert package.memory_context.included_count >= 1


def test_updated_domain_context_flows_into_prompt_package(session: Session) -> None:
    _seed_memory(session)
    registry = AgentRegistryService(session)
    registry.update_domain_context("praxis", "Praxis edited UI context for partner work.")

    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare a partner call brief.",
            use_semantic=False,
        )
    )

    assert package.domain_context == "Praxis edited UI context for partner work."
    assert "Praxis edited UI context for partner work." in package.assembled_prompt


def test_updated_global_context_flows_into_prompt_package(session: Session) -> None:
    _seed_memory(session)
    registry = AgentRegistryService(session)
    registry.update_global_context("Maestro edited global context.")

    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare a partner call brief.",
            use_semantic=False,
        )
    )

    assert package.global_context == "Maestro edited global context."
    assert "Maestro edited global context." in package.assembled_prompt


def test_create_agent_and_tool_connection_redacts_secret_config(session: Session) -> None:
    registry = AgentRegistryService(session)
    agent = registry.create_agent_spec(
        domain_key="praxis",
        key="Praxis Email Agent",
        name="Praxis Email Agent",
        role_summary="Triages Praxis email.",
        tool_permissions={"gmail.read": {"permission": "read"}},
    )
    connection = registry.upsert_tool_connection(
        domain_key="praxis",
        tool_key="gmail.read",
        display_name="Praxis Gmail",
        auth_type="api_key",
        config={"api_key": "secret-value", "label": "praxis"},
    )

    assert agent.key == "praxis-email-agent"
    assert connection.config["api_key"] == "********"
    assert connection.config["label"] == "praxis"
    assert registry.get_spec("praxis-email-agent").allowed_tools[0].connection_id is not None


def test_tool_manifest_can_attach_domain_connections(session: Session) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    session.add(
        ToolConnection(
            domain_id=praxis.id,
            tool_key="memory.context_bundle",
            display_name="Praxis memory retrieval",
            auth_type="service",
            config={},
            is_active=True,
        )
    )
    session.commit()

    spec = AgentRegistryService(session).get_spec("praxis-planning-agent")

    memory_tool = next(tool for tool in spec.allowed_tools if tool.key == "memory.context_bundle")
    assert memory_tool.connection_id is not None
    assert memory_tool.auth_type == "service"


def test_tool_manifest_can_inherit_provider_level_github_connection(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro", "env_token_name": "MAESTRO_GITHUB_TOKEN"},
    )

    spec = registry.get_spec("maestro-introspection-agent")
    tools = registry.list_tools()

    issue_search = next(tool for tool in spec.allowed_tools if tool.key == "github.issue.search")
    assert issue_search.connection_id is not None
    assert issue_search.auth_type == "gh_cli"
    registry_issue_search = next(tool for tool in tools if tool.key == "github.issue.search")
    assert "maestro-development" in registry_issue_search.connected_domains


def test_seed_agent_merge_adds_github_tool_permissions_to_existing_seed_agent(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    assert agent is not None
    agent.tool_permissions = {
        "memory.context_bundle": {
            "permission": "read",
            "description": "Legacy seed permissions.",
        }
    }
    session.commit()

    refreshed = registry.get_spec("maestro-introspection-agent")

    assert "memory.context_bundle" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.issue.search" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.issue.get" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.repo.get" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.issue.create" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.pr.checks" in [tool.key for tool in refreshed.allowed_tools]


def test_github_adapter_uses_domain_token_env_for_write_tools(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github.issue.create",
        display_name="Maestro GitHub issue writer",
        auth_type="gh_cli",
        config={
            "repo": "Caliperti1/Maestro",
            "env_token_name": "MAESTRO_GITHUB_TOKEN_TEST",
        },
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.github",
        objective="Create a test issue.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    connection = session.query(ToolConnection).filter_by(tool_key="github.issue.create").one()
    monkeypatch.setenv("MAESTRO_GITHUB_TOKEN_TEST", "test-token")
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return CompletedProcess(args=args, returncode=0, stdout="https://github.com/x/y/issues/99\n")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = GitHubCliToolAdapter("github.issue.create").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"title": "Tool-generated issue", "body": "Created by a test."},
    )

    assert output["url"] == "https://github.com/x/y/issues/99"
    assert captured["args"] == [
        "gh",
        "issue",
        "create",
        "--repo",
        "Caliperti1/Maestro",
        "--title",
        "Tool-generated issue",
        "--body",
        "Created by a test.",
    ]
    assert captured["env"]["GH_TOKEN"] == "test-token"


def test_run_agent_once_prepares_prompt_and_optional_staged_artifact(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    _seed_memory(session)

    result = PromptAggregationService(session).run_agent_once(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare a Praxis partner run.",
            use_semantic=False,
        ),
        stage_interaction=True,
        execute_llm=False,
    )

    assert result.status == "prepared"
    assert result.scheduler["status"] == "stubbed"
    assert result.prompt_package.agent.key == "praxis-planning-agent"
    assert result.staged_artifact_path is not None
    assert Path(result.staged_artifact_path).is_file()


def test_run_agent_once_executes_llm_and_records_task_report_and_tool_call(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    _seed_memory(session)

    result = PromptAggregationService(session, llm_client=FakeAgentLLMClient()).run_agent_once(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare a Praxis partner run.",
            use_semantic=False,
        ),
        stage_interaction=True,
        execute_llm=True,
    )

    assert result.status == "completed"
    assert result.output_text is not None
    assert "Praxis partner run completed" in result.output_text
    assert result.task_id is not None
    assert result.report_id is not None
    assert result.tool_calls[0]["tool_name"] == "llm.gateway"
    assert result.tool_calls[0]["status"] == "complete"
    assert result.staged_artifact_path is not None
    assert Path(result.staged_artifact_path).is_file()


def test_run_agent_once_executes_authorized_tool_before_llm(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    _seed_memory(session)
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github.issue.search",
        display_name="Maestro GitHub Issues",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )

    result = PromptAggregationService(
        session,
        llm_client=FakeToolAwareAgentLLMClient(),
        tool_adapters={"github.issue.search": FakeGitHubIssueSearchAdapter()},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction="Review GitHub issues related to tool integration.",
            use_semantic=False,
        ),
        tool_requests=[
            AgentToolRequest(
                tool_key="github.issue.search",
                payload={"query": "tool integration", "limit": 3},
            )
        ],
        execute_llm=True,
    )

    assert result.status == "completed"
    assert result.tool_calls[0]["tool_name"] == "github.issue.search"
    assert result.tool_calls[0]["status"] == "complete"
    assert result.tool_calls[0]["output_payload"]["issues"][0]["number"] == 42
    assert result.tool_calls[1]["tool_name"] == "llm.gateway"
    assert "Reviewed matching GitHub issues" in result.output_text


def test_run_agent_once_records_failed_llm_call(session: Session) -> None:
    _seed_memory(session)

    result = PromptAggregationService(session, llm_client=FailingAgentLLMClient()).run_agent_once(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare a Praxis partner run.",
            use_semantic=False,
        ),
        execute_llm=True,
    )

    assert result.status == "failed"
    assert result.error_message == "provider rejected the request"
    assert result.tool_calls[0]["status"] == "failed"


def test_interaction_artifact_packager_stages_package_for_curation(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    packager = InteractionArtifactPackager(session)
    package = packager.build_package(
        domain_key="praxis",
        agent_key="praxis-planning-agent",
        user_input="What should I do before the partner call?",
        maestro_tasking="Prepare a concise partner-call brief.",
        agent_output="Focus on training needs and transition risks.",
        tool_calls=[{"tool_name": "memory.context_bundle", "status": "complete"}],
        generated_artifacts=[{"name": "partner-call-brief.md", "uri": "reports/brief.md"}],
        open_questions=["Who owns the next follow-up?"],
        next_steps=["Draft agenda."],
    )

    staged = packager.stage_package(package)

    assert staged.path is not None
    staged_path = Path(staged.path)
    assert staged_path.is_file()
    assert staged_path.parent == tmp_path / "praxis" / "inbox"
    assert package.schema_version == "maestro.interaction_artifact.v1"
    artifact = session.query(Artifact).one()
    assert artifact.artifact_type == "interaction_package"
    assert artifact.metadata_["staged_for_curation"] is True
