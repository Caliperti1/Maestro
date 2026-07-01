from pathlib import Path
import json
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
from app.tools.runtime import (
    CodexCliToolAdapter,
    GitHubCliToolAdapter,
    LocalAppReloadAdapter,
    ToolExecutionContext,
    ToolExecutionRequest,
    ToolExecutionService,
    _clean_github_search_query,
)


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


class FakeAutoToolLoopLLMClient:
    provider = "test"
    model = "test-agent-model"

    def __init__(self):
        self.structured_calls = 0

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        assert "execution planner" in instructions
        assert "check out the latest PR" in input_text
        self.structured_calls += 1
        if self.structured_calls == 1:
            return {
                "plan_summary": "Find the latest PR before reporting.",
                "requires_final_answer": True,
                "tool_calls": [
                    {
                        "tool_key": "github.pr.search",
                        "payload_json": '{"state":"all","limit":1}',
                        "rationale": "Find the latest pull request.",
                    }
                ],
            }
        return {
            "plan_summary": "Enough PR context has been gathered.",
            "requires_final_answer": True,
            "tool_calls": [],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "Tool Results" in input_text
        assert "github.pr.search" in input_text
        assert "Add GitHub read tools MVP" in input_text
        return "## Summary\nLatest PR reviewed.\n\n## Findings\nPR #44 is ready for inspection."


class FakeAutoWriteToolLoopLLMClient:
    provider = "test"
    model = "test-agent-model"

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        assert "proposed for Chris approval" in instructions
        return {
            "plan_summary": "Create a GitHub issue for Chris to approve.",
            "requires_final_answer": True,
            "tool_calls": [
                {
                    "tool_key": "github.issue.create",
                    "payload_json": '{"title":"Test issue","body":"Created by agent"}',
                    "rationale": "The task asks for a new GitHub issue.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "approval_required" in input_text
        assert "github.issue.create" in input_text
        return "## Summary\nI proposed a GitHub issue creation for approval."


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


class FakeGitHubPrSearchAdapter:
    key = "github.pr.search"

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, object],
    ) -> dict[str, object]:
        assert context.domain.key == "maestro-development"
        assert payload["limit"] == 1
        return {
            "repo": context.connection.config["repo"] if context.connection else "Caliperti1/Maestro",
            "prs": [
                {
                    "number": 44,
                    "title": "Add GitHub read tools MVP",
                    "state": "MERGED",
                    "url": "https://github.com/Caliperti1/Maestro/pull/44",
                }
            ],
        }


class FakeGitHubIssueCreateAdapter:
    key = "github.issue.create"

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, object],
    ) -> dict[str, object]:
        assert context.domain.key == "maestro-development"
        assert payload["title"] == "Test issue"
        return {
            "repo": context.connection.config["repo"] if context.connection else "Caliperti1/Maestro",
            "url": "https://github.com/Caliperti1/Maestro/issues/123",
            "title": payload["title"],
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
    coding = next(spec for spec in specs if spec.key == "maestro-coding-agent")
    assert coding.domain_key == "maestro-development"
    assert "codex.task.run" in [tool.key for tool in coding.allowed_tools]


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
    assert "github.repo.list" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.file.get" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.file.search" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.issue.create" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.repo.create" in [tool.key for tool in refreshed.allowed_tools]
    assert "github.pr.checks" in [tool.key for tool in refreshed.allowed_tools]
    assert "codex.task.run" in [tool.key for tool in refreshed.allowed_tools]


def test_codex_tool_manifest_inherits_codex_connection(session: Session, tmp_path: Path) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="codex",
        display_name="Local Codex",
        auth_type="local_cli",
        config={
            "default_cwd": str(tmp_path),
            "allowed_roots": [str(tmp_path)],
        },
    )

    spec = registry.get_spec("maestro-introspection-agent")
    tool = next(tool for tool in spec.allowed_tools if tool.key == "codex.task.run")

    assert tool.connection_id is not None
    assert tool.auth_type == "local_cli"


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


def test_github_adapter_skips_missing_issue_labels(
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
    captured_create_args: list[str] = []

    def fake_run(args, **kwargs):
        if args[:3] == ["gh", "label", "list"]:
            return CompletedProcess(args=args, returncode=0, stdout='[{"name":"enhancement"}]')
        captured_create_args.extend(args)
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
        {
            "title": "Tool-generated issue",
            "body": "Created by a test.",
            "labels": ["enhancement", "maestro-development"],
        },
    )

    assert output["url"] == "https://github.com/x/y/issues/99"
    assert output["labels"] == ["enhancement"]
    assert output["skipped_labels"] == ["maestro-development"]
    assert "--label" in captured_create_args
    assert "enhancement" in captured_create_args
    assert "maestro-development" not in captured_create_args


def test_github_adapter_applies_configured_preferred_issue_labels(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={
            "repo": "Caliperti1/Maestro",
            "preferred_issue_labels": ["enhancement", "tooling"],
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
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    captured_create_args: list[str] = []

    def fake_run(args, **kwargs):
        if args[:3] == ["gh", "label", "list"]:
            return CompletedProcess(args=args, returncode=0, stdout='[{"name":"enhancement"}]')
        captured_create_args.extend(args)
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

    assert output["issue_number"] == 99
    assert output["issue_url"] == "https://github.com/x/y/issues/99"
    assert output["html_url"] == "https://github.com/x/y/issues/99"
    assert output["owner"] == "Caliperti1"
    assert output["name"] == "Maestro"
    assert output["repo_name"] == "Maestro"
    assert output["labels_applied"] == ["enhancement"]
    assert output["labels_skipped"] == ["tooling"]
    assert output["preferred_labels"] == ["enhancement", "tooling"]
    assert output["write_status"] == "created"
    assert "enhancement" in captured_create_args
    assert "tooling" not in captured_create_args


def test_github_adapter_blocks_missing_required_issue_labels(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={
            "repo": "Caliperti1/Maestro",
            "issue_labels": {"required": ["security-review"]},
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
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")

    def fake_run(args, **kwargs):
        if args[:3] == ["gh", "label", "list"]:
            return CompletedProcess(args=args, returncode=0, stdout='[{"name":"enhancement"}]')
        raise AssertionError("Issue creation should not run when a required label is missing.")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    try:
        GitHubCliToolAdapter("github.issue.create").execute(
            ToolExecutionContext(
                session=session,
                agent=agent,
                domain=domain,
                task=task,
                connection=connection,
            ),
            {"title": "Tool-generated issue", "body": "Created by a test."},
        )
    except Exception as exc:
        assert "Required GitHub issue label(s) are missing" in str(exc)
    else:
        raise AssertionError("Expected required missing label to block issue creation.")


def test_github_issue_create_approval_preview_is_human_readable(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={
            "repo": "Caliperti1/Maestro",
            "preferred_issue_labels": ["enhancement"],
            "required_issue_labels": ["triage"],
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

    proposed = ToolExecutionService(session).propose_for_task(
        ToolExecutionRequest(
            agent_key="maestro-introspection-agent",
            tool_key="github.issue.create",
            payload={
                "title": "Harden issue creation",
                "body": "Line one.\n\nLine two.",
                "labels": ["bug"],
            },
        ),
        task=task,
        rationale="The user asked for a follow-up issue.",
        safety_level="external_write",
        reason="Creates an external GitHub issue and requires Chris approval.",
    )

    preview = proposed.output["approval_preview"]
    assert proposed.output["write_status"] == "awaiting_approval"
    assert preview["repo"] == "Caliperti1/Maestro"
    assert preview["title"] == "Harden issue creation"
    assert preview["body_preview"] == "Line one.\n\nLine two."
    assert preview["labels_to_apply"] == ["bug", "enhancement", "triage"]
    assert preview["labels_required"] == ["triage"]
    assert "Target repo: Caliperti1/Maestro" in preview["summary"]
    assert "Body preview: Line one. Line two." in preview["summary"]
    assert preview["labels_may_skip"] == ["bug", "enhancement"]
    assert preview["labels_create"] == []
    assert "Optional labels that may be skipped: bug, enhancement" in preview["summary"]
    assert "Labels proposed for creation: none." in preview["summary"]
    assert "Missing optional labels: skipped and reported at execution." in preview["summary"]
    assert "No GitHub issue is created" in " ".join(preview["notable_uncertainty"])
    assert "will not create repository labels" in " ".join(preview["notable_uncertainty"])


def test_github_issue_comment_requires_explicit_issue_number(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
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
        objective="Comment on a test issue.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")

    def fake_run(args, **kwargs):
        raise AssertionError("GitHub CLI should not run without an issue number.")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    try:
        GitHubCliToolAdapter("github.issue.comment").execute(
            ToolExecutionContext(
                session=session,
                agent=agent,
                domain=domain,
                task=task,
                connection=connection,
            ),
            {"body": "This should not post."},
        )
    except Exception as exc:
        assert "GitHub tool requires `number`, `issue_number`." in str(exc)
    else:
        raise AssertionError("Expected issue comment without number to fail.")


def test_github_issue_update_requires_explicit_issue_number(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
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
        objective="Update a test issue.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")

    def fake_run(args, **kwargs):
        raise AssertionError("GitHub CLI should not run without an issue number.")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    try:
        GitHubCliToolAdapter("github.issue.update").execute(
            ToolExecutionContext(
                session=session,
                agent=agent,
                domain=domain,
                task=task,
                connection=connection,
            ),
            {"title": "This should not update."},
        )
    except Exception as exc:
        assert "GitHub tool requires `number`, `issue_number`." in str(exc)
    else:
        raise AssertionError("Expected issue update without number to fail.")


def test_github_adapter_reads_specific_file_from_repo(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.github",
        objective="Read issue template.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                '{"type":"file","name":"issue_template.md","path":".github/issue_template.md",'
                '"sha":"abc123","size":16,"encoding":"base64","content":"SGVsbG8gdGVtcGxhdGU="}'
            ),
        )

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = GitHubCliToolAdapter("github.file.get").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"path": ".github/issue_template.md", "ref": "main"},
    )

    assert output["content"] == "Hello template"
    assert output["path"] == ".github/issue_template.md"
    assert output["name"] == "issue_template.md"
    assert output["repo_name"] == "Maestro"
    assert captured["args"] == [
        "gh",
        "api",
        "--method",
        "GET",
        "repos/Caliperti1/Maestro/contents/.github/issue_template.md?ref=main",
    ]


def test_github_issue_get_accepts_issue_number_alias(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.github",
        objective="Read issue.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout='{"number":50,"title":"Harden GitHub tool suite after MVP"}',
        )

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = GitHubCliToolAdapter("github.issue.get").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"issue_number": 50},
    )

    assert output["number"] == 50
    assert output["issue"]["number"] == 50
    assert captured["args"][2:5] == ["view", "50", "--repo"]


def test_codex_adapter_runs_local_codex_exec_json(
    session: Session,
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="codex",
        display_name="Local Codex",
        auth_type="local_cli",
        config={
            "codex_bin": "codex",
            "default_cwd": str(tmp_path),
            "allowed_roots": [str(tmp_path)],
        },
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="codex").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.codex",
        objective="Run a Codex task.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/codex")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        output_path = args[args.index("--output-last-message") + 1]
        Path(output_path).write_text("Implemented the requested change.", encoding="utf-8")
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"codex-session-1"}',
                '{"type":"item.completed","item":{"type":"file_change","path":"app/example.py"}}',
                '{"type":"turn.completed"}',
            ]
        )
        return CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = CodexCliToolAdapter("codex.task.run").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {
            "task": "Implement issue #50.",
            "sandbox_mode": "workspace-write",
            "target_directory": ".",
            "branch_workflow": False,
        },
    )

    assert output["session_id"] == "codex-session-1"
    assert output["final_message"] == "Implemented the requested change."
    assert output["changed_files"] == ["app/example.py"]
    assert output["returncode"] == 0
    assert captured["cwd"] == str(tmp_path)
    assert captured["args"][:6] == [
        "/usr/bin/codex",
        "exec",
        "--json",
        "--cd",
        str(tmp_path),
        "--sandbox",
    ]


def test_codex_adapter_runs_branch_workflow_and_returns_pr_metadata(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / ".git").mkdir()
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="codex",
        display_name="Local Codex",
        auth_type="local_cli",
        config={
            "default_cwd": str(tmp_path),
            "allowed_roots": [str(tmp_path)],
            "branch_prefix": "maestro/test",
        },
    )
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="codex").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.codex",
        objective="Run a Codex task.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/codex")
    calls: list[list[str]] = []
    pr_create_body = ""
    status_calls = 0

    def fake_run(args, **kwargs):
        nonlocal status_calls, pr_create_body
        calls.append(args)
        if args[0] == "git":
            if args[1:3] == ["status", "--porcelain"]:
                status_calls += 1
                return CompletedProcess(args=args, returncode=0, stdout="" if status_calls == 1 else " M app/example.py\n", stderr="")
            if args[1:4] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return CompletedProcess(args=args, returncode=0, stdout="main\n", stderr="")
            if args[1:3] == ["rev-parse", "HEAD"]:
                return CompletedProcess(args=args, returncode=0, stdout="abc123\n", stderr="")
            if args[1] == "diff":
                return CompletedProcess(args=args, returncode=0, stdout=" app/example.py | 1 +\n", stderr="")
            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[0] == "gh":
            if args[1:3] == ["pr", "create"]:
                pr_create_body = args[args.index("--body") + 1]
                return CompletedProcess(args=args, returncode=0, stdout="https://github.com/Caliperti1/Maestro/pull/77\n", stderr="")
            if args[1:3] == ["pr", "view"]:
                return CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 77,
                            "title": "Implement issue #50",
                            "body": "PR body",
                            "url": "https://github.com/Caliperti1/Maestro/pull/77",
                            "headRefName": "maestro/test/issue-50-implement-issue-50-",
                            "baseRefName": "main",
                        }
                    ),
                    stderr="",
                )
        output_path = args[args.index("--output-last-message") + 1]
        Path(output_path).write_text("Implemented the requested change.", encoding="utf-8")
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"codex-session-1"}',
                '{"type":"item.completed","item":{"type":"file_change","path":"app/example.py"}}',
                '{"type":"turn.completed"}',
            ]
        )
        return CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = CodexCliToolAdapter("codex.task.run").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {
            "task": "Implement issue #50.",
            "task_title": "Implement issue #50",
            "issue_number": 50,
            "sandbox_mode": "workspace-write",
            "target_directory": ".",
        },
    )

    assert output["branch_workflow"] is True
    assert output["base_branch"] == "main"
    assert output["branch"].startswith("maestro/test/issue-50-implement-issue-50")
    assert output["commit_sha"] == "abc123"
    assert output["changed_files"] == ["app/example.py"]
    assert output["diff_summary"] == "app/example.py | 1 +"
    assert output["pr_number"] == 77
    assert output["pr_url"] == "https://github.com/Caliperti1/Maestro/pull/77"
    assert output["review_status"] == "pr_opened"
    assert any(call[:3] == ["git", "worktree", "add"] for call in calls)
    assert any(call[:3] == ["git", "push", "-u"] for call in calls)
    assert any(call[:3] == ["gh", "pr", "create"] for call in calls)
    assert any(call[:4] == ["git", "worktree", "remove", "--force"] for call in calls)
    assert "Closes #50" in pr_create_body


def test_codex_adapter_rejects_target_outside_allowed_roots(
    session: Session,
    tmp_path: Path,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="codex",
        display_name="Local Codex",
        auth_type="local_cli",
        config={
            "default_cwd": str(allowed),
            "allowed_roots": [str(allowed)],
        },
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="codex").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.codex",
        objective="Run a Codex task.",
        input_payload={},
    )
    session.add(task)
    session.commit()

    try:
        CodexCliToolAdapter("codex.task.run").execute(
            ToolExecutionContext(
                session=session,
                agent=agent,
                domain=domain,
                task=task,
                connection=connection,
            ),
            {
                "prompt": "Implement issue #50.",
                "target_path": str(outside),
            },
        )
    except Exception as exc:
        assert "allowed root" in str(exc)
    else:
        raise AssertionError("Codex target outside allowed roots should fail.")


def test_github_pr_merge_runs_approved_merge_command(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.github",
        objective="Merge a PR.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = GitHubCliToolAdapter("github.pr.merge").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"pr_number": 77, "method": "squash", "delete_branch": True},
    )

    assert output["merged"] is True
    assert output["pr_number"] == 77
    assert captured["args"] == [
        "gh",
        "pr",
        "merge",
        "77",
        "--repo",
        "Caliperti1/Maestro",
        "--squash",
        "--delete-branch",
    ]


def test_local_app_reload_runs_configured_commands(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="local.app.reload",
        display_name="Maestro Local Reload",
        auth_type="local",
        config={
            "default_cwd": str(tmp_path),
            "allowed_roots": [str(tmp_path)],
            "reload_commands": [["npm", "run", "build"]],
        },
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="local.app.reload").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.reload",
        objective="Reload app.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = LocalAppReloadAdapter("local.app.reload").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"pull_latest": True},
    )

    assert output["write_status"] == "reloaded"
    assert calls == [["git", "pull", "--ff-only"], ["npm", "run", "build"]]


def test_github_adapter_searches_files_in_repo(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.github",
        objective="Search files.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                '{"total_count":1,"items":[{"name":"issue_template.md",'
                '"path":".github/issue_template.md","sha":"abc123","html_url":"https://github/x",'
                '"repository":{"full_name":"Caliperti1/Maestro"}}]}'
            ),
        )

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = GitHubCliToolAdapter("github.file.search").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"query": "issue_template", "path": ".github", "limit": 3},
    )

    assert output["files"][0]["path"] == ".github/issue_template.md"
    assert captured["args"] == [
        "gh",
        "api",
        "--method",
        "GET",
        "search/code",
        "-f",
        "q=issue_template repo:Caliperti1/Maestro path:.github",
        "-f",
        "per_page=3",
    ]


def test_github_search_outputs_include_stable_repo_fields(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.github",
        objective="Search GitHub.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")

    def fake_run(args, **kwargs):
        if args[:3] == ["gh", "issue", "list"]:
            return CompletedProcess(args=args, returncode=0, stdout="[]")
        if args[:3] == ["gh", "pr", "list"]:
            return CompletedProcess(args=args, returncode=0, stdout="[]")
        raise AssertionError(f"Unexpected GitHub command: {args}")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)
    context = ToolExecutionContext(
        session=session,
        agent=agent,
        domain=domain,
        task=task,
        connection=connection,
    )

    issue_output = GitHubCliToolAdapter("github.issue.search").execute(
        context,
        {"query": "label hardening"},
    )
    pr_output = GitHubCliToolAdapter("github.pr.search").execute(
        context,
        {"query": "label hardening"},
    )

    assert issue_output["owner"] == "Caliperti1"
    assert issue_output["name"] == "Maestro"
    assert issue_output["repo_name"] == "Maestro"
    assert issue_output["summary"]["owner"] == "Caliperti1"
    assert issue_output["summary"]["name"] == "Maestro"
    assert issue_output["summary"]["repo_name"] == "Maestro"
    assert pr_output["owner"] == "Caliperti1"
    assert pr_output["name"] == "Maestro"
    assert pr_output["repo_name"] == "Maestro"
    assert pr_output["summary"]["owner"] == "Caliperti1"
    assert pr_output["summary"]["name"] == "Maestro"
    assert pr_output["summary"]["repo_name"] == "Maestro"


def test_github_pr_checks_output_includes_normalized_status_fields(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.github",
        objective="Read PR checks.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")

    def fake_run(args, **kwargs):
        assert args[:4] == ["gh", "pr", "checks", "50"]
        return CompletedProcess(
            args=args,
            returncode=8,
            stdout=(
                "["
                '{"name":"unit tests","state":"PASS"},'
                '{"name":"lint","state":"FAIL"},'
                '{"name":"integration","state":"PENDING"}'
                "]"
            ),
        )

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    output = GitHubCliToolAdapter("github.pr.checks").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"number": 50},
    )

    assert output["pr_number"] == 50
    assert output["check_status"] == "failed"
    assert output["status"] == "failed"
    assert output["state"] == "failed"
    assert output["check_counts"] == {
        "passed": 1,
        "failed": 1,
        "pending": 1,
        "skipped": 0,
        "unknown": 0,
    }
    assert output["failed_checks"] == ["lint"]
    assert output["pending_checks"] == ["integration"]
    assert output["summary"]["check_status"] == "failed"


def test_github_adapter_lists_and_creates_repos_with_provider_connection(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"owner": "Caliperti1"},
    )
    agent = AgentRepository(session).get_by_key("maestro-introspection-agent")
    domain = DomainRepository(session).get_by_key("maestro-development")
    connection = session.query(ToolConnection).filter_by(tool_key="github").one()
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.github",
        objective="Manage repos.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["repo", "list"]:
            return CompletedProcess(
                args=args,
                returncode=0,
                stdout='[{"name":"Maestro","nameWithOwner":"Caliperti1/Maestro"}]',
            )
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout="https://github.com/Caliperti1/new-irad\n",
        )

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    listed = GitHubCliToolAdapter("github.repo.list").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"limit": 5},
    )
    created = GitHubCliToolAdapter("github.repo.create").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"name": "new-irad", "description": "Personal IRAD sandbox", "private": True},
    )

    assert listed["repos"][0]["nameWithOwner"] == "Caliperti1/Maestro"
    assert created["repo"] == "Caliperti1/new-irad"
    assert calls[0] == [
        "gh",
        "repo",
        "list",
        "Caliperti1",
        "--limit",
        "5",
        "--json",
        "name,nameWithOwner,description,isPrivate,url,updatedAt",
    ]
    assert calls[1] == [
        "gh",
        "repo",
        "create",
        "Caliperti1/new-irad",
        "--private",
        "--description",
        "Personal IRAD sandbox",
    ]


def test_github_adapter_can_resolve_token_ref_from_dotenv(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("MAESTRO_GITHUB_TOKEN_TEST=dotenv-token\n", encoding="utf-8")
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={
            "repo": "Caliperti1/Maestro",
            "env_token_name": "MAESTRO_GITHUB_TOKEN_TEST",
        },
    )
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github.repo.get",
        display_name="Stale per-tool GitHub repo metadata",
        auth_type="gh_cli",
        config={
            "repo": "Wrong/Repo",
            "env_token_name": "MISSING_STALE_TOKEN",
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
        objective="Read repo metadata.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout='{"nameWithOwner":"Caliperti1/Maestro"}\n',
        )

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    result = PromptAggregationService(
        session,
        llm_client=FakeToolAwareAgentLLMClient(),
        tool_adapters=None,
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction="Use repo metadata.",
            use_semantic=False,
        ),
        tool_requests=[AgentToolRequest(tool_key="github.repo.get")],
        execute_llm=False,
    )

    assert result.tool_calls[0]["status"] == "complete"
    assert captured["env"]["GH_TOKEN"] == "dotenv-token"
    assert captured["args"][3] == "Caliperti1/Maestro"
    get_settings.cache_clear()


def test_github_search_query_cleanup_removes_repo_placeholders() -> None:
    assert (
        _clean_github_search_query(
            "repo:AUTHORIZED_REPOSITORY is:pr sort:updated-desc",
            kind="pr",
        )
        == "sort:updated-desc"
    )
    assert (
        _clean_github_search_query(
            "repo:CURRENT is:issue label:test",
            kind="issue",
        )
        == "label:test"
    )


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


def test_run_agent_once_can_auto_plan_safe_tool_calls_before_final_report(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    llm_client = FakeAutoToolLoopLLMClient()

    result = PromptAggregationService(
        session,
        llm_client=llm_client,
        tool_adapters={"github.pr.search": FakeGitHubPrSearchAdapter()},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction="Please check out the latest PR.",
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
    )

    assert result.status == "completed"
    assert result.tool_loop["enabled"] is True
    assert result.tool_loop["iterations"][0]["requested_tools"][0]["tool_key"] == "github.pr.search"
    assert any(call["tool_name"] == "github.pr.search" for call in result.tool_calls)
    assert result.output_text is not None
    assert "Latest PR reviewed" in result.output_text


def test_run_agent_once_blocks_auto_planned_write_tools_for_approval(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )

    result = PromptAggregationService(
        session,
        llm_client=FakeAutoWriteToolLoopLLMClient(),
        tool_adapters={},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction="Create a GitHub issue for the next tool platform task.",
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
    )

    assert result.status == "blocked"
    blocked = [
        call for call in result.tool_calls if call["tool_name"] == "github.issue.create"
    ][0]
    assert blocked["status"] == "approval_required"
    assert blocked["output_payload"]["approval_required"] is True
    assert result.tool_loop["iterations"][0]["blocked"][0]["safety_level"] == "external_write"

    approved = ToolExecutionService(
        session,
        adapters={"github.issue.create": FakeGitHubIssueCreateAdapter()},
    ).approve_tool_call(blocked["id"])

    assert approved.status == "complete"
    assert approved.output["url"] == "https://github.com/Caliperti1/Maestro/issues/123"


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
