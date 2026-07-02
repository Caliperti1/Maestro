from pathlib import Path
import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import PromptAggregationService
from app.agents.runtime import AgentRegistryService
from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import Artifact, Report, Task, WorkflowDefinition, WorkflowRun
from app.db.session import get_db
from app.llm.client import LLMClientError
from app.maestro.orchestrator import MaestroOrchestratorError, MaestroOrchestratorService
from app.maestro.planner import MaestroPlannerResponse


class FakeOrchestratorLLMClient:
    provider = "test"
    model = "test-orchestrator-agent"

    def structured_response(self, **kwargs):
        raise AssertionError("Orchestrated agent runs should use text_response.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        return (
            "## Summary\n"
            "Prepared the requested domain contribution.\n\n"
            "## Findings\n"
            "- Agent registry and scoped memory were available.\n\n"
            "## Next Steps\n"
            "- Return to Maestro for synthesis."
        )


class RecordingOrchestratorLLMClient:
    provider = "test"
    model = "test-recording-agent"

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def structured_response(self, **kwargs):
        raise AssertionError("Orchestrated agent runs should use text_response.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        self.inputs.append(input_text)
        if "Praxis Planning Agent" in input_text:
            return "Praxis upstream output for the partner call."
        return "Maestro used dependency context to inspect the workflow."


class FlakyOrchestratorLLMClient:
    provider = "test"
    model = "test-flaky-agent"

    def __init__(self) -> None:
        self.calls = 0

    def structured_response(self, **kwargs):
        raise AssertionError("Orchestrated agent runs should use text_response.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Transient tool/LLM failure.")
        return "Recovered after retry and completed the assigned work."


class AlwaysFailPraxisLLMClient:
    provider = "test"
    model = "test-failing-praxis-agent"

    def structured_response(self, **kwargs):
        raise AssertionError("Orchestrated agent runs should use text_response.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        if "Praxis Planning Agent" in input_text:
            raise RuntimeError("Persistent Praxis agent failure.")
        return "This should not run if it depends on failed Praxis work."


class FakePlannerLLMClient:
    provider = "test"
    model = "test-planner-model"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Plan decomposed before delegation.",
            "direct_response": None,
            "planner_notes": "Fake structured planner response.",
            "work_items": [
                {
                    "id": "wi_partner_prep",
                    "type": "workflow_task",
                    "title": "Prepare Praxis partner call",
                    "description": "Prepare the partner call brief and identify follow-up risks.",
                    "domain_key": "praxis",
                    "priority": "high",
                    "required_capabilities": ["partner planning", "follow-up planning"],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["praxis-planning-agent"],
                    "expected_output": "Partner-call prep report with RFIs and next steps.",
                    "rationale": "This is substantive Praxis planning work.",
                },
                {
                    "id": "wi_partner_contact",
                    "type": "contact",
                    "title": "Capture partner contact",
                    "description": "Jane Smith is the partner lead at Example Corp.",
                    "domain_key": "praxis",
                    "priority": "normal",
                    "required_capabilities": ["relationship management"],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": False,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": True,
                    "suggested_agent_keys": [],
                    "expected_output": "Contact routed for CRM review.",
                    "rationale": "This should be routed separately from agent work.",
                },
                {
                    "id": "wi_owner_rfi",
                    "type": "rfi",
                    "title": "Confirm follow-up owner",
                    "description": "Chris needs to confirm who owns the next partner follow-up.",
                    "domain_key": "praxis",
                    "priority": "normal",
                    "required_capabilities": [],
                    "required_tools": [],
                    "dependencies": ["wi_partner_prep"],
                    "needs_agent": False,
                    "needs_user_input": True,
                    "blocks_execution": False,
                    "can_log_directly": True,
                    "suggested_agent_keys": [],
                    "expected_output": "RFI surfaced for Chris.",
                    "rationale": "This blocks confident follow-up assignment.",
                },
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeMultiDomainPlannerLLMClient:
    provider = "test"
    model = "test-multi-domain-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Coordinate Praxis and Maestro Development work.",
            "direct_response": None,
            "planner_notes": "Fake multi-domain structured planner response.",
            "work_items": [
                {
                    "id": "wi_praxis",
                    "type": "workflow_task",
                    "title": "Prepare Praxis partner-call contribution",
                    "description": "Prepare Praxis partner call context and next steps.",
                    "domain_key": "praxis",
                    "priority": "high",
                    "required_capabilities": ["partner planning", "training context"],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["praxis-planning-agent"],
                    "expected_output": "Praxis partner-call prep report.",
                    "rationale": "Praxis owns partner context.",
                },
                {
                    "id": "wi_maestro",
                    "type": "workflow_task",
                    "title": "Assess Maestro system gaps",
                    "description": "Identify Maestro orchestration gaps exposed by this workflow.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["system introspection", "architecture"],
                    "required_tools": [],
                    "dependencies": ["wi_praxis"],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-introspection-agent"],
                    "expected_output": "Maestro gap report with implementation recommendations.",
                    "rationale": "Maestro Development owns system improvement.",
                },
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeMaestroGithubPlannerLLMClient:
    provider = "test"
    model = "test-maestro-github-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Ask Maestro Development to inspect the latest PR.",
            "direct_response": None,
            "planner_notes": "Fake GitHub tool orchestration planner response.",
            "work_items": [
                {
                    "id": "wi_latest_pr",
                    "type": "workflow_task",
                    "title": "Inspect latest Maestro PR",
                    "description": "Use GitHub PR context to inspect the latest Maestro pull request.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["system introspection", "github review"],
                    "required_tools": ["github.pr.search"],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-introspection-agent"],
                    "expected_output": "Readable PR inspection report.",
                    "rationale": "Maestro Development owns repository inspection.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeMaestroIssueCreatePlannerLLMClient:
    provider = "test"
    model = "test-maestro-issue-create-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Ask Maestro Development to create a GitHub issue.",
            "direct_response": None,
            "planner_notes": "Fake GitHub issue creation planner response.",
            "work_items": [
                {
                    "id": "wi_create_issue",
                    "type": "workflow_task",
                    "title": "Create Maestro GitHub issue",
                    "description": "Create a GitHub issue using the repository story template.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["system introspection", "github issue creation"],
                    "required_tools": ["github.issue.create"],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-introspection-agent"],
                    "expected_output": "Created GitHub issue URL and summary.",
                    "rationale": "Maestro Development owns issue creation.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeMaestroCodingPlannerLLMClient:
    provider = "test"
    model = "test-maestro-coding-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Action a Maestro issue with coding-agent support.",
            "direct_response": None,
            "planner_notes": "Fake coding planner response.",
            "work_items": [
                {
                    "id": "wi_issue_50",
                    "type": "workflow_task",
                    "title": "Action Maestro issue #50",
                    "description": "Use Codex to action Maestro issue #50 after reviewing the issue.",
                    "domain_key": "maestro-development",
                    "priority": "high",
                    "required_capabilities": ["software implementation", "issue triage"],
                    "required_tools": ["github.issue.get", "codex.task.run"],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": [],
                    "expected_output": "Codex task result and implementation summary.",
                    "rationale": "Coding issue action should be owned by the coding agent.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakePlanningOnlyIssuePlannerLLMClient:
    provider = "test"
    model = "test-planning-only-issue-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Plan issue #50 without code changes.",
            "direct_response": None,
            "planner_notes": "Fake planning-only response.",
            "work_items": [
                {
                    "id": "wi_issue_50_plan",
                    "type": "workflow_task",
                    "title": "Prepare issue #50 action plan",
                    "description": "Review issue #50 and produce a minimal plan only. Do not make code changes.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["issue triage", "implementation planning"],
                    "required_tools": ["github.issue.get"],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": [],
                    "expected_output": "Minimal plan only with no code changes.",
                    "rationale": "Planning-only work should use read tools, not Codex execution.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeOrchestratedToolLoopLLMClient:
    provider = "test"
    model = "test-orchestrated-tool-loop-agent"

    def __init__(self) -> None:
        self.structured_calls = 0

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        assert "execution planner" in instructions
        assert "latest Maestro pull request" in input_text
        self.structured_calls += 1
        if self.structured_calls == 1:
            return {
                "plan_summary": "Search GitHub for the latest PR before reporting.",
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
            "plan_summary": "Enough GitHub context has been gathered.",
            "requires_final_answer": True,
            "tool_calls": [],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "Tool Results" in input_text
        assert "github.pr.search" in input_text
        assert "Wire orchestrator tool loop" in input_text
        assert "top-level `conversation` field" in instructions
        return json.dumps(
            {
                "conversation": (
                    "I checked PR #47. It wires Maestro's orchestrator to child-agent "
                    "safe tool use and shows the tool activity back in the workflow result."
                ),
                "summary": {
                    "latest_pr": {
                        "number": 47,
                        "title": "Wire orchestrator tool loop",
                        "status": "OPEN / Draft",
                    },
                    "change_summary": "Reviewed the latest Maestro PR with GitHub context.",
                    "ci_status": "No checks were reported.",
                },
                "findings": ["PR #47 wires the orchestrator to agent-planned tools."],
                "open_questions": [],
                "next_steps": [
                    {
                        "title": "Manual pre-merge test",
                        "steps": ["Run a Maestro workflow that asks an agent to inspect the latest PR."],
                        "expected_outcome": ["Tool activity appears in the workflow result."],
                    }
                ],
                "artifact_refs": [],
            }
        )


class FakeOrchestratedIssueCreateToolLoopLLMClient:
    provider = "test"
    model = "test-orchestrated-issue-create-agent"

    def __init__(self) -> None:
        self.structured_calls = 0

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        assert "execution planner" in instructions
        self.structured_calls += 1
        if self.structured_calls == 1:
            return {
                "plan_summary": "Create the requested GitHub issue after Chris approves.",
                "requires_final_answer": True,
                "tool_calls": [
                    {
                        "tool_key": "github.issue.create",
                        "payload_json": '{"title":"Test issue","body":"Created by Maestro"}',
                        "rationale": "The task asks for a new GitHub issue.",
                    }
                ],
            }
        assert "github.issue.create" in input_text
        assert "https://github.com/Caliperti1/Maestro/issues/123" in input_text
        return {
            "plan_summary": "The approved GitHub issue creation result is enough to report.",
            "requires_final_answer": True,
            "tool_calls": [],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "Tool Results" in input_text
        assert "github.issue.create" in input_text
        assert "https://github.com/Caliperti1/Maestro/issues/123" in input_text
        return json.dumps(
            {
                "conversation": "I created the GitHub issue after your approval.",
                "summary": {
                    "change_summary": "Created GitHub issue #123 for the requested Maestro work.",
                },
                "findings": [],
                "open_questions": [],
                "next_steps": [],
                "artifact_refs": [],
            }
        )


class FakeGitHubPrSearchAdapter:
    key = "github.pr.search"

    def execute(self, context, payload: dict[str, object]) -> dict[str, object]:
        assert context.domain.key == "maestro-development"
        assert context.connection is not None
        assert payload["limit"] == 1
        return {
            "repo": context.connection.config["repo"],
            "prs": [
                {
                    "number": 47,
                    "title": "Wire orchestrator tool loop",
                    "state": "OPEN",
                    "url": "https://github.com/Caliperti1/Maestro/pull/47",
                }
            ],
        }


class FakeGitHubIssueCreateAdapter:
    key = "github.issue.create"

    def execute(self, context, payload: dict[str, object]) -> dict[str, object]:
        assert context.domain.key == "maestro-development"
        assert payload["title"] == "Test issue"
        return {
            "repo": context.connection.config["repo"] if context.connection else "Caliperti1/Maestro",
            "url": "https://github.com/Caliperti1/Maestro/issues/123",
            "number": 123,
            "title": payload["title"],
        }


class FakeGroundTruthDemoPlannerLLMClient:
    provider = "test"
    model = "test-groundtruth-demo-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Prepare a GroundTruth demo for a new potential end user.",
            "direct_response": None,
            "planner_notes": "Fake GroundTruth demo structured planner response.",
            "work_items": [
                {
                    "id": "wi_1",
                    "type": "rfi",
                    "title": "Request missing GroundTruth demo details from Chris",
                    "description": "Need attendee, organization, role, and calendar details.",
                    "domain_key": "praxis",
                    "priority": "urgent",
                    "required_capabilities": ["demo preparation intake"],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": False,
                    "needs_user_input": True,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": [],
                    "expected_output": "Missing inputs for Chris.",
                    "rationale": "The demo can proceed generically but contact-specific work needs details.",
                },
                {
                    "id": "wi_3",
                    "type": "workflow_task",
                    "title": "Prepare GroundTruth demo narrative and run-of-show",
                    "description": "Create the demo talk track and flow.",
                    "domain_key": "praxis",
                    "priority": "urgent",
                    "required_capabilities": ["product demo planning"],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["praxis-planning-agent"],
                    "expected_output": "Demo run-of-show.",
                    "rationale": "Planning owns the demo narrative.",
                },
                {
                    "id": "wi_4",
                    "type": "workflow_task",
                    "title": "Assess GroundTruth technical demo readiness",
                    "description": "Identify product demo risks and fallback plan.",
                    "domain_key": "praxis",
                    "priority": "urgent",
                    "required_capabilities": ["technical risk assessment"],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["groundtruth-chief-engineer"],
                    "expected_output": "Technical readiness checklist.",
                    "rationale": "GroundTruth engineer owns technical readiness.",
                },
                {
                    "id": "wi_6",
                    "type": "workflow_task",
                    "title": "Prepare CRM/contact intake shell for the potential end user",
                    "description": "Prepare CRM contact and opportunity shell for the attendee.",
                    "domain_key": "praxis",
                    "priority": "high",
                    "required_capabilities": ["CRM hygiene", "contact capture"],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["paxis-crm-manager"],
                    "expected_output": "CRM shell.",
                    "rationale": "CRM owns contact capture.",
                },
                {
                    "id": "wi_8",
                    "type": "workflow_task",
                    "title": "Assemble final GroundTruth demo prep packet",
                    "description": "Synthesize narrative, technical readiness, and CRM context.",
                    "domain_key": "praxis",
                    "priority": "urgent",
                    "required_capabilities": ["executive synthesis"],
                    "required_tools": [],
                    "dependencies": ["wi_3", "wi_4", "wi_6"],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["praxis-planning-agent"],
                    "expected_output": "Final demo prep packet.",
                    "rationale": "Planning should synthesize the final packet.",
                },
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeDirectChatPlannerLLMClient:
    provider = "test"
    model = "test-direct-chat-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Answered directly in chat.",
            "direct_response": "Maestro can answer that directly without delegating work.",
            "planner_notes": "No executable work was detected.",
            "work_items": [
                {
                    "id": "wi_direct",
                    "type": "direct_response",
                    "title": "Direct chat answer",
                    "description": "Answer the user's question directly.",
                    "domain_key": None,
                    "priority": "normal",
                    "required_capabilities": [],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": False,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": [],
                    "expected_output": "Plain text answer.",
                    "rationale": "No routing, memory write, or agent work is needed.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeScheduledPlannerLLMClient:
    provider = "test"
    model = "test-scheduled-planner"

    def __init__(self, *, title: str = "Review Maestro backlog", domain_key: str = "maestro-development"):
        self.title = title
        self.domain_key = domain_key

    def structured_response(self, **kwargs):
        return {
            "plan_summary": self.title,
            "direct_response": None,
            "planner_notes": "Fake scheduled workflow planner response.",
            "work_items": [
                {
                    "id": "wi_scheduled_work",
                    "type": "workflow_task",
                    "title": self.title,
                    "description": self.title,
                    "domain_key": self.domain_key,
                    "priority": "normal",
                    "required_capabilities": ["planning", "synthesis"],
                    "required_tools": ["github.issue.search"],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-introspection-agent"],
                    "expected_output": "Scheduled workflow output.",
                    "rationale": "Chris asked Maestro to schedule recurring or triggered work.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FailingPlannerLLMClient:
    provider = "test"
    model = "test-failing-planner"

    def structured_response(self, **kwargs):
        raise LLMClientError("Planner unavailable in deterministic fallback test.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


def _client(session: Session, tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    settings.openrouter_api_key = ""
    settings.openai_api_key = ""
    settings.openrouter_api_key = ""
    settings.openai_api_key = ""

    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_orchestrator_plan_is_registry_aware_and_plan_first(session: Session) -> None:
    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FailingPlannerLLMClient(),
    ).create_plan(
        "Prepare a Praxis partner call workflow and identify RFIs, tasks, contacts, and events."
    )

    assert plan.status == "proposed"
    assert plan.approval_required is True
    assert plan.execution_mode == "propose_first"
    assert any(intent.type == "workflow" for intent in plan.intents)
    assert any(intent.type == "rfi" for intent in plan.intents)
    assert any(subtask.agent_key == "praxis-planning-agent" for subtask in plan.subtasks)
    assert any(
        agent["key"] == "praxis-planning-agent" for agent in plan.registry_snapshot["agents"]
    )
    assert plan.scheduler["status"] == "queue_foundation"
    assert plan.execution_stages
    assert session.query(Task).filter(Task.status == "proposed").count() == 1
    assert session.query(Report).count() == 0


def test_orchestrator_generates_role_specific_subtasks(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.create_agent_spec(
        domain_key="praxis",
        key="Praxis Email Agent",
        name="Praxis Email Agent",
        role_summary="Triages Praxis email, partner messages, and follow-up drafts.",
    )
    registry.create_agent_spec(
        domain_key="praxis",
        key="Praxis Finance Agent",
        name="Praxis Finance Agent",
        role_summary="Tracks Praxis budgets, invoices, and financial assumptions.",
    )

    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FailingPlannerLLMClient(),
    ).create_plan(
        "Prepare a Praxis partner email follow-up workflow for the next call."
    )

    selected_agent_keys = {subtask.agent_key for subtask in plan.subtasks}
    assert "praxis-planning-agent" in selected_agent_keys
    assert "praxis-email-agent" in selected_agent_keys
    assert "praxis-finance-agent" not in selected_agent_keys
    email_subtask = next(
        subtask for subtask in plan.subtasks if subtask.agent_key == "praxis-email-agent"
    )
    assert "Triages Praxis email" in email_subtask.objective
    assert "only on the portion" in email_subtask.objective
    assert email_subtask.rationale is not None
    assert "wi_1" in email_subtask.rationale


def test_orchestrator_decomposes_with_llm_before_agent_matching(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.create_agent_spec(
        domain_key="praxis",
        key="Praxis Email Agent",
        name="Praxis Email Agent",
        role_summary="Drafts partner emails and communication follow-ups.",
    )

    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FakePlannerLLMClient(),
    ).create_plan(
        "Prepare for the partner call. Jane Smith is the partner lead. Confirm who owns follow-up."
    )

    assert plan.planner_mode == "llm"
    assert [item.id for item in plan.work_items] == [
        "wi_partner_prep",
        "wi_partner_contact",
        "wi_owner_rfi",
    ]
    assert any(item.type == "contact" and item.can_log_directly for item in plan.work_items)
    assert any(item.type == "rfi" and item.needs_user_input for item in plan.work_items)
    assert len(plan.subtasks) == 1
    subtask = plan.subtasks[0]
    assert subtask.agent_key == "praxis-planning-agent"
    assert subtask.work_item_ids == ["wi_partner_prep"]
    assert "Work item wi_partner_prep" in subtask.objective
    assert "Jane Smith is the partner lead" not in subtask.objective


def test_orchestrator_assigns_each_work_item_to_one_best_agent(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.create_agent_spec(
        domain_key="maestro-development",
        key="Maestro Chief Engineer",
        name="Maestro Chief Engineer",
        role_summary="Manages the Maestro backlog and delegates coding work.",
        tool_permissions={
            "github.issue.get": {"permission": "read"},
            "codex.task.run": {"permission": "use"},
        },
    )

    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeMaestroCodingPlannerLLMClient(),
    ).create_plan("Action Maestro issue #50.")

    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].agent_key == "maestro-coding-agent"
    assert plan.subtasks[0].work_item_ids == ["wi_issue_50"]


def test_planning_only_issue_work_does_not_require_codex(session: Session) -> None:
    AgentRegistryService(session).create_agent_spec(
        domain_key="maestro-development",
        key="Maestro Chief Engineer",
        name="Maestro Chief Engineer",
        role_summary="Manages Maestro backlog issue triage and implementation planning without execution.",
        tool_permissions={
            "github.issue.get": {"permission": "read"},
            "github.file.search": {"permission": "read"},
        },
    )
    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FakePlanningOnlyIssuePlannerLLMClient(),
    ).create_plan("Action Maestro issue #50 with a minimal plan only; do not make code changes yet.")

    assert plan.work_items[0].required_tools == ["github.issue.get"]
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].agent_key == "maestro-chief-engineer"


def test_orchestrator_prevents_broad_assignment_and_self_dependencies(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.create_agent_spec(
        domain_key="praxis",
        key="GroundTruth Chief Engineer",
        name="GroundTruth Chief Engineer",
        role_summary="Manages GroundTruth application development and technical demo readiness.",
    )
    registry.create_agent_spec(
        domain_key="praxis",
        key="Paxis CRM Manager",
        name="Paxis CRM Manager",
        role_summary="Manages Praxis CRM contacts, opportunities, and contact capture.",
    )
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeGroundTruthDemoPlannerLLMClient(),
    )

    plan = service.create_plan(
        "We have a GroundTruth demo this afternoon with a new potential end user."
    )

    rfi = next(item for item in plan.work_items if item.id == "wi_1")
    assert rfi.blocks_execution is True

    assignments = {
        (subtask.agent_key, tuple(subtask.work_item_ids or [])): set(subtask.depends_on_work_item_ids or [])
        for subtask in plan.subtasks
    }
    assert assignments[("groundtruth-chief-engineer", ("wi_4",))] == set()
    assert assignments[("paxis-crm-manager", ("wi_6",))] == {"wi_1"}
    assert assignments[("praxis-planning-agent", ("wi_3",))] == set()
    assert assignments[("praxis-planning-agent", ("wi_8",))] == {"wi_1", "wi_4", "wi_6"}
    assert all(
        not (set(subtask.work_item_ids or []) & set(subtask.depends_on_work_item_ids or []))
        for subtask in plan.subtasks
    )

    crm_subtask = next(subtask for subtask in plan.subtasks if subtask.agent_key == "paxis-crm-manager")
    assert crm_subtask.depends_on_work_item_ids == ["wi_1"]
    synthesis_subtask = next(
        subtask for subtask in plan.subtasks if "wi_8" in (subtask.work_item_ids or [])
    )
    assert set(synthesis_subtask.depends_on_work_item_ids or []) == {"wi_1", "wi_4", "wi_6"}

    assert set(plan.workflow_graph["stages"][0]["work_item_ids"]) == {"wi_3", "wi_4"}
    assert all(
        work_item_id not in stage["waits_for_work_item_ids"]
        for stage in plan.workflow_graph["stages"]
        for work_item_id in stage["work_item_ids"]
    )
    assert any(item["status"] == "pending" for item in plan.scheduler["queue_items"])

    run = service.run_plan(plan.parent_task_id, execute_llm=False)

    assert run.status == "blocked"
    queue_by_work_item = {
        tuple(item["work_item_ids"]): item["status"]
        for item in run.scheduler["queue_items"]
    }
    assert queue_by_work_item[("wi_3",)] == "completed"
    assert queue_by_work_item[("wi_4",)] == "completed"
    assert queue_by_work_item[("wi_6",)] == "blocked"
    assert queue_by_work_item[("wi_8",)] == "blocked"


def test_orchestrator_direct_chat_has_no_executable_plan(session: Session) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeDirectChatPlannerLLMClient(),
    )

    plan = service.create_plan("What is Maestro?")

    assert plan.is_chat_only is True
    assert plan.direct_response == "Maestro can answer that directly without delegating work."
    assert plan.subtasks == []
    assert plan.execution_stages == []
    assert plan.scheduler["queue_items"] == []
    assert session.query(WorkflowRun).count() == 0

    try:
        service.run_plan(plan.parent_task_id, execute_llm=False)
    except MaestroOrchestratorError as exc:
        assert "Direct chat responses" in str(exc)
    else:
        raise AssertionError("Direct chat plans should not be executable.")


def test_orchestrator_saves_recurring_workflow_without_immediate_execution(
    session: Session,
) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeScheduledPlannerLLMClient(),
    )

    plan = service.create_plan(
        "Each morning at 9am please review the Maestro backlog and propose a work plan for the day"
    )

    assert plan.scheduler["schedule_candidate"]["trigger_type"] == "recurring"
    assert plan.scheduler["schedule_candidate"]["trigger_config"]["time_of_day"] == "09:00"

    run = service.run_plan(plan.parent_task_id, execute_llm=False)

    assert run.status == "scheduled"
    assert run.child_runs == []
    definition = session.query(WorkflowDefinition).one()
    assert definition.trigger_type == "recurring"
    assert definition.trigger_config["time_of_day"] == "09:00"
    assert definition.workflow_spec["queue_items"][0]["status"] == "pending"
    workflow_run = session.query(WorkflowRun).one()
    assert workflow_run.status == "scheduled"


def test_orchestrator_saves_email_event_triggered_workflow(
    session: Session,
) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeScheduledPlannerLLMClient(
            title="Triage new email",
            domain_key="praxis",
        ),
    )

    plan = service.create_plan(
        "Each time a new email arrives identify if it is worth notifying me about, "
        "extract relevant contact info, events, or to dos and route them appropriately"
    )

    candidate = plan.scheduler["schedule_candidate"]
    assert candidate["trigger_type"] == "event"
    assert candidate["trigger_config"]["event_type"] == "gmail.message.received"

    run = service.run_plan(plan.parent_task_id, execute_llm=False)

    assert run.status == "scheduled"
    definition = session.query(WorkflowDefinition).one()
    assert definition.trigger_type == "event"
    assert definition.trigger_config["event_type"] == "gmail.message.received"
    assert definition.workflow_spec["queue_items"][0]["domain_key"] == "praxis"


def test_orchestrator_run_dispatches_children_and_stages_one_artifact(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    runtime = PromptAggregationService(session, llm_client=FakeOrchestratorLLMClient())
    service = MaestroOrchestratorService(
        session,
        runtime=runtime,
        planner_llm_client=FakeMultiDomainPlannerLLMClient(),
    )
    plan = service.create_plan(
        "Prepare a Praxis partner call workflow and ask Maestro Development to note system gaps."
    )

    assert plan.execution_stages == [
        ["praxis-planning-agent"],
        ["maestro-introspection-agent"],
    ]
    assert plan.workflow_graph["edges"] == [
        {
            "from_work_item_id": "wi_praxis",
            "to_work_item_id": "wi_maestro",
            "relation": "must_complete_before",
        }
    ]
    assert plan.workflow_graph["stages"][0]["work_item_ids"] == ["wi_praxis"]
    assert plan.workflow_graph["stages"][1]["waits_for_work_item_ids"] == ["wi_praxis"]
    queue_items = plan.scheduler["queue_items"]
    assert [item["status"] for item in queue_items] == ["pending", "pending"]
    assert [item["stage_index"] for item in queue_items] == [1, 2]

    run = service.run_plan(plan.parent_task_id, execute_llm=True)

    assert run.status == "completed"
    assert run.synthesis_report_id is not None
    assert run.staged_artifact_path is not None
    assert Path(run.staged_artifact_path).is_file()
    assert len(run.child_runs) >= 2
    parent_task_id = uuid.UUID(plan.parent_task_id)
    child_tasks = session.query(Task).filter(Task.parent_task_id == parent_task_id).all()
    assert len(child_tasks) == len(run.child_runs)
    assert all(task.source_type == "maestro_orchestrator" for task in child_tasks)
    assert all((task.input_payload or {}).get("prompt_context") for task in child_tasks)
    artifacts = session.query(Artifact).all()
    assert len(artifacts) == 1
    assert artifacts[0].metadata_["canonical_workflow_artifact"] is True
    parent = session.get(Task, parent_task_id)
    assert parent is not None
    assert parent.status == "completed"
    assert parent.output_payload["synthesis_report_id"] == run.synthesis_report_id
    completed_queue = parent.input_payload["scheduler"]["queue_items"]
    assert parent.input_payload["scheduler"]["status"] == "completed"
    assert [item["status"] for item in completed_queue] == ["completed", "completed"]
    assert all(item["child_task_id"] for item in completed_queue)
    assert parent.output_payload["scheduler"]["queue_items"] == completed_queue
    assert parent.output_payload["scheduler"]["status"] == "completed"


def test_orchestrator_passes_dependency_outputs_to_later_agent(session: Session, tmp_path: Path) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    recording_client = RecordingOrchestratorLLMClient()
    runtime = PromptAggregationService(session, llm_client=recording_client)
    service = MaestroOrchestratorService(
        session,
        runtime=runtime,
        planner_llm_client=FakeMultiDomainPlannerLLMClient(),
    )
    plan = service.create_plan(
        "Prepare a Praxis partner call workflow and ask Maestro Development to note system gaps."
    )

    run = service.run_plan(plan.parent_task_id, execute_llm=True)

    assert run.status == "completed"
    assert len(recording_client.inputs) == 2
    assert "Dependency context" not in recording_client.inputs[0]
    assert "Upstream output for wi_praxis" in recording_client.inputs[1]
    assert "Praxis upstream output for the partner call." in recording_client.inputs[1]
    child_tasks = session.scalars(
        select(Task).where(Task.parent_task_id == uuid.UUID(plan.parent_task_id)).order_by(Task.created_at)
    ).all()
    assert "Dependency context" not in child_tasks[0].input_payload["prompt_context"]["user_context"]
    assert "Upstream output for wi_praxis" in child_tasks[1].input_payload["prompt_context"]["user_context"]
    parent = session.get(Task, uuid.UUID(plan.parent_task_id))
    assert parent is not None
    assert parent.output_payload["execution_stages"] == [
        ["praxis-planning-agent"],
        ["maestro-introspection-agent"],
    ]
    assert parent.output_payload["scheduler"]["queue_items"][1]["status"] == "completed"


def test_orchestrator_can_delegate_agent_planned_safe_tool_loop(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    AgentRegistryService(session).upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    runtime = PromptAggregationService(
        session,
        llm_client=FakeOrchestratedToolLoopLLMClient(),
        tool_adapters={"github.pr.search": FakeGitHubPrSearchAdapter()},
    )
    service = MaestroOrchestratorService(
        session,
        runtime=runtime,
        planner_llm_client=FakeMaestroGithubPlannerLLMClient(),
    )
    plan = service.create_plan("Have Maestro inspect the latest Maestro pull request.")

    run = service.run_plan(
        plan.parent_task_id,
        execute_llm=True,
        auto_tool_loop=True,
        max_tool_iterations=2,
    )

    assert run.status == "completed"
    assert run.tool_activity
    assert any(item["tool_name"] == "github.pr.search" for item in run.tool_activity)
    assert any(item["tool_name"] == "llm.tool_planner" for item in run.tool_activity)
    assert "## Tool Activity" in run.synthesis
    assert "github.pr.search" in run.synthesis
    assert "I checked PR #47" in run.synthesis
    assert "I checked PR #47" in run.chat_summary
    assert "findings" not in run.chat_summary
    child = run.child_runs[0]
    assert child.tool_loop["enabled"] is True
    assert any(call["tool_name"] == "github.pr.search" for call in child.tool_calls)
    parent = session.get(Task, uuid.UUID(plan.parent_task_id))
    assert parent is not None
    assert parent.output_payload["tool_activity"][0]["tool_name"] == "llm.tool_planner"


def test_orchestrator_resumes_workflow_after_tool_approval(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    AgentRegistryService(session).upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="github",
        display_name="Maestro GitHub",
        auth_type="gh_cli",
        config={"repo": "Caliperti1/Maestro"},
    )
    llm_client = FakeOrchestratedIssueCreateToolLoopLLMClient()
    runtime = PromptAggregationService(
        session,
        llm_client=llm_client,
        tool_adapters={"github.issue.create": FakeGitHubIssueCreateAdapter()},
    )
    service = MaestroOrchestratorService(
        session,
        runtime=runtime,
        planner_llm_client=FakeMaestroIssueCreatePlannerLLMClient(),
    )
    plan = service.create_plan("Add a GitHub issue for the next Maestro tool platform task.")

    blocked_run = service.run_plan(
        plan.parent_task_id,
        execute_llm=True,
        auto_tool_loop=True,
        max_tool_iterations=2,
    )

    assert blocked_run.status == "blocked"
    approval = [
        item
        for item in blocked_run.tool_activity
        if item["tool_name"] == "github.issue.create"
    ][0]
    assert approval["status"] == "approval_required"
    parent = session.get(Task, uuid.UUID(plan.parent_task_id))
    assert parent is not None
    assert parent.input_payload["scheduler"]["queue_items"][0]["status"] == "blocked"

    result, resumed_run = service.approve_tool_call_and_resume(
        approval["tool_call_id"],
        execute_llm=True,
        auto_tool_loop=True,
        max_tool_iterations=2,
    )

    assert result.status == "complete"
    assert resumed_run is not None
    assert resumed_run.status == "completed"
    assert "I created the GitHub issue after your approval." in resumed_run.chat_summary
    assert llm_client.structured_calls == 1
    parent = session.get(Task, uuid.UUID(plan.parent_task_id))
    assert parent is not None
    assert parent.input_payload["scheduler"]["queue_items"][0]["status"] == "completed"
    assert parent.output_payload["tool_activity"][0]["status"] == "complete"


def test_orchestrator_retries_failed_queue_item_before_marking_complete(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    flaky_client = FlakyOrchestratorLLMClient()
    runtime = PromptAggregationService(session, llm_client=flaky_client)
    service = MaestroOrchestratorService(
        session,
        runtime=runtime,
        planner_llm_client=FakeMultiDomainPlannerLLMClient(),
    )
    plan = service.create_plan(
        "Prepare a Praxis partner call workflow and ask Maestro Development to note system gaps."
    )

    run = service.run_plan(plan.parent_task_id, execute_llm=True)

    assert run.status == "completed"
    assert flaky_client.calls == 3
    assert len(run.child_runs) == 3
    queue_item = run.scheduler["queue_items"][0]
    assert queue_item["status"] == "completed"
    assert queue_item["retry_count"] == 1
    parent = session.get(Task, uuid.UUID(plan.parent_task_id))
    assert parent is not None
    assert parent.output_payload["phase_syntheses"][0]["completed_count"] == 1
    assert "Phase Syntheses" in run.synthesis


def test_orchestrator_blocks_downstream_work_after_retry_exhaustion(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    runtime = PromptAggregationService(session, llm_client=AlwaysFailPraxisLLMClient())
    service = MaestroOrchestratorService(
        session,
        runtime=runtime,
        planner_llm_client=FakeMultiDomainPlannerLLMClient(),
    )
    plan = service.create_plan(
        "Prepare a Praxis partner call workflow and ask Maestro Development to note system gaps."
    )

    run = service.run_plan(plan.parent_task_id, execute_llm=True)

    assert run.status == "failed"
    queue_by_work_item = {
        tuple(item["work_item_ids"]): item for item in run.scheduler["queue_items"]
    }
    assert run.scheduler["status"] == "failed"
    assert queue_by_work_item[("wi_praxis",)]["status"] == "failed"
    assert queue_by_work_item[("wi_praxis",)]["retry_count"] == 1
    assert queue_by_work_item[("wi_maestro",)]["status"] == "blocked"
    assert "upstream agent task failed" in queue_by_work_item[("wi_maestro",)]["error_message"]
    assert len(run.child_runs) == 2
    assert all(child.agent.key == "praxis-planning-agent" for child in run.child_runs)


def test_maestro_api_plan_and_stub_run(session: Session, tmp_path: Path) -> None:
    client = _client(session, tmp_path)

    plan_response = client.post(
        "/maestro/plan",
        json={
            "message": (
                "Coordinate Praxis and Maestro Development on a partner prep workflow, "
                "and produce a synthesized plan."
            )
        },
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()["plan"]
    assert plan["status"] == "proposed"
    assert plan["subtasks"]
    assert plan["execution_stages"]
    assert "workflow_graph" in plan
    assert "nodes" in plan["workflow_graph"]
    assert plan["scheduler"]["queue_items"]

    run_response = client.post(
        f"/maestro/plans/{plan['parent_task_id']}/run",
        json={"execute_llm": False},
    )

    assert run_response.status_code == 200
    run = run_response.json()["run"]
    assert run["status"] == "completed"
    assert run["child_runs"]
    assert run["staged_artifact_path"]
    assert "Maestro Synthesis" in run["synthesis"]
    assert run["chat_summary"]


def test_maestro_api_refines_plan_with_existing_context(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    plan_response = client.post(
        "/maestro/plan",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    assert plan_response.status_code == 200
    first_plan = plan_response.json()["plan"]

    refine_response = client.post(
        f"/maestro/plans/{first_plan['parent_task_id']}/refine",
        json={"message": "Also include GroundTruth demo technical readiness."},
    )

    assert refine_response.status_code == 200
    refined_plan = refine_response.json()["plan"]
    assert refined_plan["status"] == "proposed"
    assert refined_plan["plan_id"] != first_plan["plan_id"]
    refined_task = session.get(Task, uuid.UUID(refined_plan["parent_task_id"]))
    assert refined_task is not None
    assert refined_task.input_payload["refined_from_plan_id"] == first_plan["plan_id"]
    assert "GroundTruth demo technical readiness" in refined_task.input_payload["refinement"]


def test_maestro_api_respond_plans_without_active_plan(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "planned"
    assert payload["message"].startswith("I drafted a plan")
    assert payload["plan"]["status"] == "proposed"
    assert payload["chat_plan"] is None


def test_maestro_api_persists_active_session_history(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    assert response.status_code == 200
    conversation = response.json()["conversation"]

    active = client.get("/maestro/sessions/active")
    assert active.status_code == 200
    active_conversation = active.json()["conversation"]
    assert active_conversation["id"] == conversation["id"]
    assert [message["sender"] for message in active_conversation["messages"]] == [
        "user",
        "maestro",
    ]
    assert active_conversation["messages"][0]["content"] == "Prepare a Praxis partner call workflow."
    assert active_conversation["active_plan"]["parent_task_id"] == response.json()["plan"]["parent_task_id"]

    sessions = client.get("/maestro/sessions")
    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["id"] == conversation["id"]

    restored = client.get(f"/maestro/sessions/{conversation['id']}")
    assert restored.status_code == 200
    restored_conversation = restored.json()["conversation"]
    assert restored_conversation["active_plan"]["status"] == "proposed"
    assert restored_conversation["active_plan"]["summary"] == response.json()["plan"]["summary"]


def test_maestro_api_archives_sessions_from_history(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    conversation = response.json()["conversation"]

    archived = client.patch(
        f"/maestro/sessions/{conversation['id']}/archive",
        json={"archived": True},
    )

    assert archived.status_code == 200
    assert archived.json()["conversation"]["archived"] is True
    sessions = client.get("/maestro/sessions")
    assert sessions.status_code == 200
    assert sessions.json()["sessions"] == []
    archived_sessions = client.get("/maestro/sessions?include_archived=true")
    assert archived_sessions.status_code == 200
    assert archived_sessions.json()["sessions"][0]["id"] == conversation["id"]


def test_maestro_historical_session_restore_does_not_replace_primary_channel(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    channel_id = first.json()["conversation"]["id"]
    historical = client.post("/maestro/sessions/start")
    historical_id = historical.json()["conversation"]["id"]
    assert historical_id == channel_id

    restored = client.get(f"/maestro/sessions/{channel_id}")
    assert restored.status_code == 200

    second = client.post(
        "/maestro/respond",
        json={"message": "What is the current channel model?"},
    )
    assert second.status_code == 200
    assert second.json()["conversation"]["id"] == channel_id


def test_maestro_api_respond_refines_active_plan(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first_response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    first_plan = first_response.json()["plan"]

    refine_response = client.post(
        "/maestro/respond",
        json={
            "active_plan_id": first_plan["parent_task_id"],
            "message": "Also include GroundTruth demo technical readiness.",
        },
    )

    assert refine_response.status_code == 200
    payload = refine_response.json()
    assert payload["kind"] == "refined"
    assert payload["message"]
    assert payload["plan"]["plan_id"] != first_plan["plan_id"]


def test_maestro_api_respond_classifies_specific_plan_guidance_as_refinement(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first_response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    first_plan = first_response.json()["plan"]

    guidance_messages = [
        "Remove the CRM task but preserve the rest of the plan.",
        "Have only the planning agent do the executive summary.",
        "Actually this belongs in Personal, not Praxis.",
        "Do this first, then have the email agent draft the follow-up.",
    ]

    for message in guidance_messages:
        response = client.post(
            "/maestro/respond",
            json={
                "active_plan_id": first_plan["parent_task_id"],
                "message": message,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "refined"
        refined_task = session.get(Task, uuid.UUID(payload["plan"]["parent_task_id"]))
        assert refined_task is not None
        assert refined_task.input_payload["refined_from_plan_id"] == first_plan["plan_id"]
        assert message in refined_task.input_payload["refinement"]
        assert "Previous work items:" in refined_task.input_payload["user_input"]


def test_maestro_api_respond_uses_previous_pr_context_for_merge_followup(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first_response = client.post(
        "/maestro/respond",
        json={"message": "Have the Maestro coding agent implement issue 42."},
    )
    first_plan = first_response.json()["plan"]
    parent = session.get(Task, uuid.UUID(first_plan["parent_task_id"]))
    assert parent is not None
    parent.output_payload = {
        "chat_summary": "Created PR #77 for issue 42 and left it ready for review.",
        "synthesis_report_id": str(uuid.uuid4()),
        "tool_activity": [
            {
                "tool_name": "codex.task.run",
                "status": "complete",
                "details": "Opened PR #77 for review.",
                "output_payload": {
                    "pr_number": 77,
                    "pr_url": "https://github.com/example/maestro/pull/77",
                    "branch": "maestro/issue-42",
                    "base_branch": "main",
                    "changed_files": ["app/example.py"],
                },
            }
        ],
    }
    session.commit()

    response = client.post(
        "/maestro/respond",
        json={
            "active_plan_id": first_plan["parent_task_id"],
            "message": "Cool, merge the PR and reload the app.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "refined"
    refined_task = session.get(Task, uuid.UUID(payload["plan"]["parent_task_id"]))
    assert refined_task is not None
    refined_input = refined_task.input_payload["user_input"]
    assert "Previous run context:" in refined_input
    assert "PR number: 77" in refined_input
    assert "https://github.com/example/maestro/pull/77" in refined_input
    assert "maestro/issue-42" in refined_input


def test_maestro_api_respond_side_chat_keeps_active_plan(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first_response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    first_plan = first_response.json()["plan"]

    response = client.post(
        "/maestro/respond",
        json={
            "active_plan_id": first_plan["parent_task_id"],
            "message": "What does the workflow order mean?",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "chat_only"
    assert payload["classification"] == "side_chat"
    assert payload["plan"] is None
    assert payload["active_plan"]["plan_id"] == first_plan["plan_id"]
    assert "without changing the proposed workflow" in payload["message"]


def test_maestro_api_respond_applies_blocking_rfi_answer(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first_response = client.post(
        "/maestro/respond",
        json={
            "message": (
                "Coordinate a Praxis partner prep workflow and confirm which follow-up owner "
                "Chris wants assigned."
            )
        },
    )
    first_plan = first_response.json()["plan"]
    assert any(item["blocks_execution"] for item in first_plan["work_items"])

    response = client.post(
        "/maestro/respond",
        json={
            "active_plan_id": first_plan["parent_task_id"],
            "message": "Chris owns the partner follow-up.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "rfi_answered"
    assert payload["classification"] == "rfi_answered"
    assert payload["plan"]["plan_id"] != first_plan["plan_id"]
    if any(item["blocks_execution"] for item in payload["plan"]["work_items"]):
        assert "Answer here in chat" in payload["message"]


def test_maestro_api_respond_surfaces_blocking_rfi_in_chat(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    response = client.post(
        "/maestro/respond",
        json={
            "message": (
                "Coordinate a Praxis partner prep workflow and confirm which follow-up owner "
                "Chris wants assigned."
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "planned"
    assert any(item["blocks_execution"] for item in payload["plan"]["work_items"])
    assert payload["message"].startswith("I need one answer before this can run")
    assert "Answer here in chat" in payload["message"]


def test_maestro_api_respond_routes_context_inside_active_session(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first_response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    first_plan = first_response.json()["plan"]

    response = client.post(
        "/maestro/respond",
        json={
            "active_plan_id": first_plan["parent_task_id"],
            "message": "Remember that Jane Smith prefers a short agenda before partner calls.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "routed"
    assert payload["classification"] == "routed"
    assert payload["plan"]["plan_id"] != first_plan["plan_id"]


def test_maestro_api_marks_direct_chat_plan(session: Session, tmp_path: Path) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeDirectChatPlannerLLMClient(),
    )
    plan = service.create_plan("What is Maestro?")

    response = client.get(f"/maestro/plans/{plan.parent_task_id}")

    assert response.status_code == 200
    payload = response.json()["plan"]
    assert payload["is_chat_only"] is True
    assert payload["subtasks"] == []
    assert payload["direct_response"] == "Maestro can answer that directly without delegating work."


def test_maestro_api_respond_returns_chat_only_without_plan(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    response = client.post(
        "/maestro/respond",
        json={"message": "Tell me about Maestro"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "chat_only"
    assert payload["plan"] is None
    assert payload["chat_plan"]["is_chat_only"] is True
    assert payload["message"]

    active = client.get("/maestro/sessions/active")
    assert active.status_code == 200
    assert active.json()["conversation"]["active_plan"] is None


def test_maestro_api_close_session_stages_transcript_artifact(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    response = client.post(
        "/maestro/sessions/close",
        json={
            "messages": [
                {"sender": "user", "content": "Think through a Praxis workflow."},
                {"sender": "maestro", "content": "I proposed a staged plan."},
            ]
        },
    )

    assert response.status_code == 200
    staged_path = Path(response.json()["staged_artifact_path"])
    assert staged_path.is_file()
    assert staged_path.parent == tmp_path / "maestro-development" / "inbox"
    artifact = session.query(Artifact).one()
    assert artifact.metadata_["canonical_session_artifact"] is True


def test_maestro_api_blocks_run_when_plan_needs_chris(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    plan_response = client.post(
        "/maestro/plan",
        json={
            "message": (
                "Coordinate a Praxis partner prep workflow and confirm which follow-up owner "
                "Chris wants assigned."
            )
        },
    )
    plan = plan_response.json()["plan"]
    assert any(item["blocks_execution"] for item in plan["work_items"])

    run_response = client.post(
        f"/maestro/plans/{plan['parent_task_id']}/run",
        json={"execute_llm": False},
    )

    assert run_response.status_code == 400
    assert "needs Chris" in run_response.json()["detail"]


def test_maestro_planner_schema_requires_every_declared_property() -> None:
    schema = MaestroPlannerResponse.model_json_schema()

    def assert_required_matches_properties(node: dict) -> None:
        properties = set(node.get("properties", {}))
        if properties:
            assert set(node.get("required", [])) == properties
        for child in node.get("$defs", {}).values():
            assert_required_matches_properties(child)
        for child in node.get("properties", {}).values():
            if isinstance(child, dict):
                assert_required_matches_properties(child)
        items = node.get("items")
        if isinstance(items, dict):
            assert_required_matches_properties(items)

    assert_required_matches_properties(schema)
