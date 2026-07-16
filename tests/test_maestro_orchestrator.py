from pathlib import Path
import json
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import PromptAggregationService
from app.agents.runtime import AgentRegistryService
from app.api import maestro as maestro_api
from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import (
    Artifact,
    Contact,
    ContactDomainNote,
    Conversation,
    Domain,
    Idea,
    MemoryItem,
    Message,
    Report,
    RoutedItem,
    Task,
    ToolCall,
    Todo,
    WorkflowDefinition,
    WorkflowQueueItem,
    WorkflowRun,
    WorkflowRunLogEntry,
)
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.llm.client import LLMClientError
from app.maestro.orchestrator import MaestroOrchestratorError, MaestroOrchestratorService
from app.maestro.intent_classifier import MaestroMessageUnderstandingResponse
from app.maestro.planner import MaestroPlannerResponse
from app.maestro.scheduler import SchedulerService
from app.tools.runtime import ToolExecutionResult


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


class FakeSameAgentCapacityPlannerLLMClient:
    provider = "test"
    model = "test-same-agent-capacity-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Plan with multiple downstream engineering tasks.",
            "direct_response": "I prepared a dependency-aware plan.",
            "planner_notes": "Fake planner response for agent capacity.",
            "work_items": [
                {
                    "id": "wi_research",
                    "type": "workflow_task",
                    "title": "Research integration options",
                    "description": "Research Google Workspace integration options.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["research"],
                    "required_tools": ["web.search"],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-capacity-researcher"],
                    "expected_output": "Research brief.",
                    "rationale": "Research is needed first.",
                },
                {
                    "id": "wi_product",
                    "type": "workflow_task",
                    "title": "Define product behavior",
                    "description": "Define user-facing product behavior.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["product planning"],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-capacity-product"],
                    "expected_output": "Product brief.",
                    "rationale": "Product planning is needed first.",
                },
                {
                    "id": "wi_security",
                    "type": "workflow_task",
                    "title": "Draft security model",
                    "description": "Draft security and permission model.",
                    "domain_key": "maestro-development",
                    "priority": "high",
                    "required_capabilities": ["security architecture"],
                    "required_tools": [],
                    "dependencies": ["wi_research"],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-capacity-engineer"],
                    "expected_output": "Security model.",
                    "rationale": "Engineering security design follows research.",
                },
                {
                    "id": "wi_roadmap",
                    "type": "workflow_task",
                    "title": "Draft implementation roadmap",
                    "description": "Draft implementation roadmap.",
                    "domain_key": "maestro-development",
                    "priority": "high",
                    "required_capabilities": ["architecture planning"],
                    "required_tools": [],
                    "dependencies": ["wi_research", "wi_product"],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-capacity-engineer"],
                    "expected_output": "Implementation roadmap.",
                    "rationale": "Engineering roadmap follows research and product work.",
                },
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeSameAgentDependentPlannerLLMClient:
    provider = "test"
    model = "test-same-agent-dependent-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Plan a feature with sequential engineering work.",
            "direct_response": "I prepared a dependency-aware engineering plan.",
            "planner_notes": "Fake planner response for same-agent dependency handling.",
            "work_items": [
                {
                    "id": "wi_1",
                    "type": "workflow_task",
                    "title": "Map existing upload architecture",
                    "description": "Inspect existing frontend and backend upload handling.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["architecture", "codebase inspection"],
                    "required_tools": ["github.read"],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-chief-engineer"],
                    "expected_output": "Architecture findings.",
                    "rationale": "The chief engineer should inspect the current system first.",
                },
                {
                    "id": "wi_2",
                    "type": "workflow_task",
                    "title": "Design chat file import behavior",
                    "description": "Design the product and API behavior for chat file import.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["architecture", "product design"],
                    "required_tools": [],
                    "dependencies": ["wi_1"],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-chief-engineer"],
                    "expected_output": "Design proposal.",
                    "rationale": "Design depends on knowing the existing upload architecture.",
                },
                {
                    "id": "wi_3",
                    "type": "workflow_task",
                    "title": "Draft implementation plan",
                    "description": "Draft phased implementation tasks for the feature.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": ["architecture", "implementation planning"],
                    "required_tools": [],
                    "dependencies": ["wi_1", "wi_2"],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-chief-engineer"],
                    "expected_output": "Implementation plan.",
                    "rationale": "Implementation planning depends on architecture and design.",
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


class FakeStandaloneContactPlannerLLMClient:
    provider = "test"
    model = "test-standalone-contact-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Capture Ben Daniels contact context.",
            "direct_response": None,
            "planner_notes": "Fake standalone contact response.",
            "work_items": [
                {
                    "id": "wi_capture_ben",
                    "type": "standalone_task",
                    "title": "Capture Ben Daniels from XVIII Airborne Corps as Praxis engagement contact",
                    "description": "Capture Ben Daniels from XVIII Airborne Corps as Praxis engagement contact.",
                    "domain_key": "praxis",
                    "priority": "normal",
                    "required_capabilities": [],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": False,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": True,
                    "suggested_agent_keys": [],
                    "expected_output": "Contact routed for CRM review.",
                    "rationale": "This should be contact context, not a todo.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeThinkTankPlannerLLMClient:
    provider = "test"
    model = "test-think-tank-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Capture CAD AI tool concept.",
            "direct_response": "That CAD-to-print concept is worth keeping in Think Tank.",
            "planner_notes": "Fake think tank response.",
            "work_items": [
                {
                    "id": "wi_cad_concept",
                    "type": "memory_candidate",
                    "title": "CAD AI tool feature concept",
                    "description": (
                        "A CAD AI tool could let mechanical design agents generate STL files "
                        "for later slicing and OctoPrint handoff."
                    ),
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": [],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": False,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": True,
                    "suggested_agent_keys": [],
                    "expected_output": "Idea saved for later feature design.",
                    "rationale": "This is an immature feature concept.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        raise AssertionError("Planner should use structured_response.")


class FakeWorkflowWithDuplicateTaskPlannerLLMClient:
    provider = "test"
    model = "test-workflow-with-duplicate-task-planner"

    def structured_response(self, **kwargs):
        return {
            "plan_summary": "Execute work without creating a personal reminder.",
            "direct_response": None,
            "planner_notes": "Fake duplicate task response.",
            "work_items": [
                {
                    "id": "wi_agent_work",
                    "type": "workflow_task",
                    "title": "Research CAD SOTA",
                    "description": "Have the SOTA researcher investigate current CAD AI tooling.",
                    "domain_key": "maestro-development",
                    "priority": "high",
                    "required_capabilities": ["research"],
                    "required_tools": ["web.search"],
                    "dependencies": [],
                    "needs_agent": True,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": False,
                    "suggested_agent_keys": ["maestro-introspection-agent"],
                    "expected_output": "Current-state research report.",
                    "rationale": "This is work for an agent to execute.",
                },
                {
                    "id": "wi_duplicate_todo",
                    "type": "standalone_task",
                    "title": "Research CAD SOTA",
                    "description": "Have the SOTA researcher investigate current CAD AI tooling.",
                    "domain_key": "maestro-development",
                    "priority": "normal",
                    "required_capabilities": [],
                    "required_tools": [],
                    "dependencies": [],
                    "needs_agent": False,
                    "needs_user_input": False,
                    "blocks_execution": False,
                    "can_log_directly": True,
                    "suggested_agent_keys": [],
                    "expected_output": "Task routed.",
                    "rationale": "Duplicate extraction of agent work.",
                },
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


def test_orchestrator_registry_snapshot_is_prompt_compact(session: Session) -> None:
    service = MaestroOrchestratorService(session)
    snapshot = service._registry_snapshot(
        service.registry.list_domain_contexts(),
        service.registry.list_specs(),
        service.registry.list_tools(),
        service.registry.list_skills(),
    )

    assert len(json.dumps(snapshot, default=str)) < 15000
    assert "allowed_tool_keys" in snapshot["agents"][0]
    assert "allowed_skill_keys" in snapshot["agents"][0]
    assert "allowed_tools" not in snapshot["agents"][0]
    assert "connected_domains" not in snapshot["tools"][0]
    assert "authorized_agents" not in snapshot["tools"][0]
    assert "skills" in snapshot
    assert all("instruction" not in skill for skill in snapshot["skills"])


def test_orchestrator_assigns_required_skills_and_model_to_email_work(session: Session) -> None:
    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FailingPlannerLLMClient(),
    ).create_plan(
        "For Praxis, review the latest email and extract contacts, events, and follow-up todos."
    )

    skill_keys = {
        skill
        for item in plan.scheduler["queue_items"]
        for skill in item.get("required_skills", [])
    }
    model_profiles = {
        item.get("model_profile")
        for item in plan.scheduler["queue_items"]
        if item.get("model_profile")
    }

    assert "email_triage" in skill_keys
    assert "contact_manager" in skill_keys
    assert "calendar_manager" in skill_keys
    assert "to_do_manager" in skill_keys
    assert "ollama:qwen3:8b" in model_profiles
    assert all(
        item.model_tier == "qwen"
        for item in plan.work_items
        if item.needs_agent and "email" in f"{item.title} {item.description}".lower()
    )


def test_orchestrator_routes_complex_design_work_to_advanced_model_tier(session: Session) -> None:
    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FailingPlannerLLMClient(),
    ).create_plan(
        "Design a multi-agent architecture for a new Maestro CAD workflow and compare options."
    )

    agent_items = [item for item in plan.work_items if item.needs_agent]

    assert agent_items
    assert all(item.model_tier == "sol" for item in agent_items)
    assert all(item.model_profile == "openrouter:openai/gpt-5.6-sol" for item in agent_items)
    assert all(item.model_rationale for item in agent_items)


def test_orchestrator_recognizes_maestro_ui_change_as_coding_work(session: Session) -> None:
    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FailingPlannerLLMClient(),
    ).create_plan("Change the color of the Send button in the Maestro app to blue, then hot reload the app.")

    coding_items = [item for item in plan.work_items if "codex.task.run" in item.required_tools]

    assert len(coding_items) == 1
    assert coding_items[0].domain_key == "maestro-development"
    assert coding_items[0].model_tier == "sol"


def test_orchestrator_generates_role_specific_subtasks(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("praxis-email-agent")
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
    assert "Praxis Email Agent" in email_subtask.objective
    assert "only on the portion" in email_subtask.objective
    assert email_subtask.rationale is not None
    assert email_subtask.work_item_ids == ["wi_2"]
    assert "wi_2" in email_subtask.rationale


def test_orchestrator_decomposes_with_llm_before_agent_matching(session: Session) -> None:
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


def test_orchestrator_serializes_multiple_subtasks_for_same_agent(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.create_agent_spec(
        domain_key="maestro-development",
        key="Maestro Capacity Researcher",
        name="Maestro Capacity Researcher",
        role_summary="Researches external integration options.",
        tool_permissions={"web.search": {"permission": "read"}},
    )
    registry.create_agent_spec(
        domain_key="maestro-development",
        key="Maestro Capacity Product",
        name="Maestro Capacity Product",
        role_summary="Defines product behavior and UX.",
    )
    registry.create_agent_spec(
        domain_key="maestro-development",
        key="Maestro Capacity Engineer",
        name="Maestro Capacity Engineer",
        role_summary="Designs architecture, security, and implementation roadmaps.",
    )

    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeSameAgentCapacityPlannerLLMClient(),
    ).create_plan("Plan Google Workspace integration.")

    assert all(len(stage) == len(set(stage)) for stage in plan.execution_stages)
    engineer_subtasks = [
        subtask
        for subtask in plan.subtasks
        if subtask.agent_key == "maestro-capacity-engineer"
    ]
    assert len(engineer_subtasks) == 2
    assert "wi_security" in engineer_subtasks[1].depends_on_work_item_ids


def test_orchestrator_splits_dependent_work_for_same_agent(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.create_agent_spec(
        domain_key="maestro-development",
        key="Maestro Chief Engineer",
        name="Maestro Chief Engineer",
        role_summary="Designs Maestro architecture, codebase integration, and implementation plans.",
        tool_permissions={"github.read": {"permission": "read"}},
    )

    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeSameAgentDependentPlannerLLMClient(),
    ).create_plan("Plan chat file import for Maestro.")

    chief_subtasks = [
        subtask
        for subtask in plan.subtasks
        if subtask.agent_key == "maestro-chief-engineer"
    ]
    assert [subtask.work_item_ids for subtask in chief_subtasks] == [["wi_1"], ["wi_2"], ["wi_3"]]
    assert chief_subtasks[0].depends_on_work_item_ids == []
    assert chief_subtasks[1].depends_on_work_item_ids == ["wi_1"]
    assert chief_subtasks[2].depends_on_work_item_ids == ["wi_1", "wi_2"]
    assert plan.execution_stages == [
        ["maestro-chief-engineer"],
        ["maestro-chief-engineer"],
        ["maestro-chief-engineer"],
    ]


def test_orchestrator_scheduler_step_reports_actionable_states(session: Session) -> None:
    service = MaestroOrchestratorService(session)

    assert service._current_scheduler_step([])["current_step"] == "No executable agent work."
    assert service._current_scheduler_step(
        [{"status": "pending", "agent_name": "Chief Engineer", "stage_index": 1, "id": "q1"}]
    )["current_step"] == "Ready to queue: Chief Engineer"
    assert service._current_scheduler_step(
        [{"status": "running", "agent_name": "Chief Engineer", "stage_index": 1, "id": "q1"}]
    )["current_step"] == "Running: Chief Engineer"
    assert service._current_scheduler_step(
        [{"status": "approval_required", "agent_name": "Chief Engineer", "stage_index": 1, "id": "q1"}]
    )["current_step"] == "Waiting for approval: Chief Engineer"
    assert service._current_scheduler_step(
        [
            {
                "status": "blocked",
                "agent_name": "Chief Engineer",
                "stage_index": 1,
                "id": "q1",
                "error_message": "Need Chris.",
            }
        ]
    )["current_step"] == "Waiting on Chief Engineer: Need Chris."
    assert service._current_scheduler_step(
        [{"status": "completed", "agent_name": "Chief Engineer", "stage_index": 1, "id": "q1"}]
    )["current_step"] == "Workflow complete."


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


def test_orchestrator_fallback_feature_plan_is_conversational_and_role_sized(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.create_agent_spec(
        domain_key="maestro-development",
        key="Maestro Chief Engineer",
        name="Maestro Chief Engineer",
        role_summary="Designs Maestro architecture, agent boundaries, permission models, and rollout plans.",
        tool_permissions={"memory.context_bundle": {"permission": "read"}},
    )
    registry.create_agent_spec(
        domain_key="maestro-development",
        key="Maestro SOTA Researcher",
        name="Maestro SOTA Researcher",
        role_summary="Researches current tools, APIs, SDKs, and state of the art for Maestro integrations.",
        tool_permissions={"web.search": {"permission": "read"}},
    )
    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FailingPlannerLLMClient(),
    ).create_plan(
        "Lets plan a new Maestro feature that will let Maestro interact with my Google Drive, "
        "Google Docs, Sheets etc"
    )

    assert plan.planner_mode == "deterministic"
    assert plan.direct_response is not None
    assert "feature-design conversation" in plan.direct_response
    assert [item.title for item in plan.work_items] == [
        "Draft Maestro feature architecture",
        "Research Google Workspace integration options",
    ]
    subtask_items = {subtask.agent_key: subtask.work_item_ids for subtask in plan.subtasks}
    assert subtask_items["maestro-chief-engineer"] == ["wi_1"]
    assert subtask_items["maestro-sota-researcher"] == ["wi_2"]
    assert "maestro-coding-agent" not in subtask_items


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
    assert assignments[("praxis-planning-agent", ("wi_8",))] == {"wi_1", "wi_3", "wi_4", "wi_6"}
    assert all(
        not (set(subtask.work_item_ids or []) & set(subtask.depends_on_work_item_ids or []))
        for subtask in plan.subtasks
    )

    crm_subtask = next(subtask for subtask in plan.subtasks if subtask.agent_key == "paxis-crm-manager")
    assert crm_subtask.depends_on_work_item_ids == ["wi_1"]
    synthesis_subtask = next(
        subtask for subtask in plan.subtasks if "wi_8" in (subtask.work_item_ids or [])
    )
    assert set(synthesis_subtask.depends_on_work_item_ids or []) == {"wi_1", "wi_3", "wi_4", "wi_6"}

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


def test_orchestrator_immediately_promotes_routed_work_items(session: Session) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakePlannerLLMClient(),
    )

    plan = service.create_plan(
        "Prepare for the partner call. Jane Smith is the partner lead. Confirm who owns follow-up."
    )

    assert plan.status == "proposed"
    routed = session.query(RoutedItem).order_by(RoutedItem.route_type).all()
    assert [item.route_type for item in routed] == ["contact", "human_input"]
    assert session.query(Contact).one().name == "Jane Smith"
    todo = session.query(Todo).one()
    assert todo.todo_type == "human_input"
    assert todo.status == "needs_input"


def test_orchestrator_proposed_workflow_is_not_enqueued_before_approval(
    session: Session,
) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeMaestroGithubPlannerLLMClient(),
    )

    plan = service.create_plan("Have Maestro inspect the latest Maestro pull request.")

    assert plan.status == "proposed"
    assert session.scalar(select(WorkflowRun).where(WorkflowRun.parent_task_id == uuid.UUID(plan.parent_task_id))) is None

    run = service.run_plan(plan.parent_task_id, execute_llm=False)

    assert run.status in {"completed", "blocked"}
    workflow_run = session.scalar(select(WorkflowRun).where(WorkflowRun.parent_task_id == uuid.UUID(plan.parent_task_id)))
    assert workflow_run is not None
    assert workflow_run.status == run.status


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


def test_schedule_candidate_ignores_hidden_topic_schedule_context(session: Session) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeScheduledPlannerLLMClient(
            title="Create GitHub issue for Maestro mobile tunnel feature",
        ),
    )

    plan = service.create_plan(
        "<latest_chris_message>\n"
        "Sweet now lets create a github issue for this please\n"
        "</latest_chris_message>\n\n"
        "<maestro_hidden_context purpose=\"topic_continuity\" do_not_copy=\"true\">\n"
        "Previous assistant message: I scheduled a recurring workflow yesterday.\n"
        "</maestro_hidden_context>"
    )

    assert plan.user_input == "Sweet now lets create a github issue for this please"
    assert plan.scheduler["schedule_candidate"] is None


def test_schedule_candidate_respects_run_now_negation(session: Session) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeScheduledPlannerLLMClient(
            title="Create GitHub issue for Maestro mobile tunnel feature",
        ),
    )

    plan = service.create_plan(
        "Do not schedule this; queue it for execution. This should be a run now task, not a schedule."
    )

    assert plan.scheduler["schedule_candidate"] is None


def test_classifier_one_time_timing_suppresses_schedule_candidate(session: Session) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeScheduledPlannerLLMClient(
            title="Create GitHub issue for Maestro mobile tunnel feature",
        ),
    )
    understanding = MaestroMessageUnderstandingResponse(
        topic_scope="active_topic",
        relationship_to_active_plan="refines_plan",
        intents=[
            {
                "type": "workflow_request",
                "span": "queue it for execution this should be a run now task not a schedule",
                "confidence": 0.94,
                "recommended_next_step": "plan",
                "workflow_timing": "one_time",
                "schedule_details": {},
            }
        ],
        recommended_next_step="plan",
        confidence=0.93,
        reason="Chris wants one immediate execution, not a schedule.",
    )
    message = maestro_api._message_with_intent_context(
        "<latest_chris_message>\n"
        "do not schedule this, queue it for execution this should be a run now task not a schedule\n"
        "</latest_chris_message>\n\n"
        "<maestro_hidden_context purpose=\"topic_continuity\" do_not_copy=\"true\">\n"
        "Prior message mentioned a scheduled workflow.\n"
        "</maestro_hidden_context>",
        understanding,
    )

    plan = service.create_plan(message)

    assert plan.scheduler["schedule_candidate"] is None


def test_classifier_recurring_timing_creates_schedule_candidate(session: Session) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeScheduledPlannerLLMClient(
            title="Review Maestro backlog",
        ),
    )
    understanding = MaestroMessageUnderstandingResponse(
        topic_scope="active_topic",
        relationship_to_active_plan="none",
        intents=[
            {
                "type": "workflow_request",
                "span": "make this a daily morning run",
                "confidence": 0.92,
                "recommended_next_step": "plan",
                "workflow_timing": "recurring",
                "schedule_details": {
                    "trigger_type": "recurring",
                    "time_of_day": "09:00",
                    "interval_minutes": 1440,
                },
            }
        ],
        recommended_next_step="plan",
        confidence=0.92,
        reason="Chris wants recurring scheduled work.",
    )
    message = maestro_api._message_with_intent_context(
        "Please set that up.",
        understanding,
    )

    plan = service.create_plan(message)

    candidate = plan.scheduler["schedule_candidate"]
    assert candidate["trigger_type"] == "recurring"
    assert candidate["trigger_config"]["time_of_day"] == "09:00"
    assert candidate["trigger_config"]["interval_minutes"] == 1440


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
    assert "completed" in run.chat_summary.lower()
    assert any(child.agent.name == "Praxis Planning Agent" for child in run.child_runs)
    assert any("Agent registry and scoped memory were available" in child.output_text for child in run.child_runs)
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
    assert "#47" in run.chat_summary
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
    assert "issue" in resumed_run.chat_summary.lower()
    assert "#123" in resumed_run.chat_summary
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


def test_maestro_api_plan_and_background_enqueue(session: Session, tmp_path: Path) -> None:
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
    assert run["status"] == "queued"
    assert run["child_runs"] == []
    assert run["staged_artifact_path"] is None
    assert "moved it to Active Workflows" in run["chat_summary"]
    workflow_run = session.scalar(
        select(WorkflowRun).where(WorkflowRun.parent_task_id == uuid.UUID(plan["parent_task_id"]))
    )
    assert workflow_run is not None
    assert workflow_run.status == "queued"
    queue_items = session.scalars(
        select(WorkflowQueueItem).where(WorkflowQueueItem.workflow_run_id == workflow_run.id)
    ).all()
    assert queue_items
    assert all(item.status in {"queued", "blocked"} for item in queue_items)


def test_maestro_context_bundle_combines_memory_reports_runs_routed_and_artifacts(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None
    memory = MemoryItem(
        domain_id=domain.id,
        scope="domain",
        memory_type="preference",
        title="Praxis partner briefing style",
        content="Chris prefers concise partner briefing context before calls.",
        metadata_={},
        importance=0.8,
        impact_level="low",
    )
    contact = Contact(
        name="Ben Daniels",
        normalized_name="ben daniels",
        email="ben@example.com",
        summary="Praxis partner lead for briefing context.",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    task = Task(
        domain_id=domain.id,
        status="completed",
        priority="normal",
        source_type="test",
        workflow_key="test.context",
        objective="Prepare partner briefing context.",
        input_payload={},
        completed_at=datetime.now(UTC),
    )
    session.add_all([memory, contact, task])
    session.flush()
    contact_note = ContactDomainNote(
        contact_id=contact.id,
        domain_id=domain.id,
        notes="Ben Daniels is the Praxis partner lead for briefing context.",
        interaction_log=[],
        source_refs=[],
        metadata_={},
    )
    report = Report(
        task_id=task.id,
        domain_id=domain.id,
        title="Praxis Partner Briefing Report",
        report_type="workflow_report",
        summary="Partner briefing report summary.",
        body_markdown="## Partner Briefing\nBen Daniels context and Praxis next steps.",
        structured_data={},
    )
    artifact = Artifact(
        task_id=task.id,
        artifact_type="workflow_summary",
        name="Praxis partner briefing artifact",
        uri="/tmp/praxis-partner-briefing.md",
        mime_type="text/markdown",
        metadata_={},
    )
    run = WorkflowRun(
        parent_task_id=task.id,
        domain_id=domain.id,
        source_type="manual",
        status="completed",
        priority="normal",
        input_payload={"summary": "Praxis partner briefing workflow"},
        completed_at=datetime.now(UTC),
    )
    session.add_all([contact_note, report, artifact, run])
    session.flush()
    run_log = WorkflowRunLogEntry(
        workflow_run_id=run.id,
        parent_task_id=task.id,
        domain_id=domain.id,
        status="completed",
        title="Praxis partner briefing workflow",
        summary="Workflow gathered partner briefing context.",
        run_completed_at=datetime.now(UTC),
        agent_work=[],
        report_ids=[str(report.id)],
        routed_item_ids=[],
        artifact_ids=[str(artifact.id)],
        notification_ids=[],
        metadata_={},
    )
    session.add(run_log)
    session.commit()

    response = client.get(
        "/maestro/context-bundle?domain_key=praxis&query_text=partner%20briefing&max_chars=5000"
    )

    assert response.status_code == 200
    payload = response.json()
    rendered = payload["rendered_text"]
    assert "Praxis partner briefing style" in rendered
    assert "Ben Daniels" in rendered
    assert "Praxis Partner Briefing Report" in rendered
    assert "Praxis partner briefing workflow" in rendered
    assert "Praxis partner briefing artifact" in rendered
    assert payload["sections"]["memory"]["included_count"] >= 1
    assert payload["sections"]["reports"]["items"][0]["id"] == str(report.id)


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
    assert "I prepared a plan to help with this" in payload["message"]
    assert "Coordinate requested workflow" in payload["message"]
    assert "run it after you approve" in payload["message"]
    assert payload["plan"]["status"] == "proposed"
    assert payload["chat_plan"] is None


def test_maestro_chat_approval_resumes_single_pending_tool_action(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(session, tmp_path)
    conversation = maestro_api._get_or_create_maestro_conversation(session, None)
    parent = Task(
        conversation_id=conversation.id,
        workflow_key="maestro.generic",
        objective="Deliver the reviewed Maestro code change.",
        input_payload={},
    )
    session.add(parent)
    session.flush()
    child = Task(
        parent_task_id=parent.id,
        workflow_key="scheduler.workflow_item",
        objective="Merge the reviewed pull request and reload the dedicated runtime.",
        input_payload={},
    )
    session.add(child)
    session.flush()
    pending = ToolCall(
        task_id=child.id,
        tool_name="local.app.deploy_pr",
        input_payload={"payload": {"pr_number": 89}},
        status="approval_required",
    )
    session.add(pending)
    session.commit()
    assert maestro_api._pending_tool_approvals_for_conversation(session, conversation) == [pending]
    assert maestro_api._is_plain_approval_message("approved") is True

    approved_ids: list[uuid.UUID] = []

    def approve(self, tool_call_id, **kwargs):
        approved_ids.append(uuid.UUID(str(tool_call_id)))
        return (
            ToolExecutionResult(
                tool_key="local.app.deploy_pr",
                status="complete",
                output={"pr_number": 89, "reloaded": True},
                error_message=None,
                tool_call_id=str(tool_call_id),
                connection_id=None,
            ),
            None,
        )

    monkeypatch.setattr(MaestroOrchestratorService, "approve_tool_call_and_resume", approve)

    response = client.post(
        "/maestro/respond",
        json={"conversation_id": str(conversation.id), "message": "Approved"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "tool_approved", payload
    assert payload["classification"] == "tool_approved"
    assert payload["plan"] is None
    assert approved_ids == [pending.id]
    assert "successfully" in payload["message"].lower()


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


def test_maestro_api_new_topic_filters_visible_channel_messages(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first = client.post(
        "/maestro/respond",
        json={"message": "Tell me about Maestro's current channel model."},
    )
    assert first.status_code == 200
    first_payload = first.json()
    first_topic_id = first_payload["channel_context"]["topic_id"]
    assert len(first_payload["conversation"]["messages"]) == 2

    second = client.post(
        "/maestro/respond",
        json={
            "message": (
                "Hey Maestro I want to brainstorm a new feature for Maestro Development, "
                "integrating a CAD tool so a mechanical design agent can generate STL files."
            )
        },
    )

    assert second.status_code == 200
    payload = second.json()
    assert payload["channel_context"]["scope"] == "new_topic"
    assert payload["channel_context"]["started_new_topic"] is True
    assert payload["channel_context"]["topic_id"] != first_topic_id
    assert "I started a fresh topic for this" in payload["message"]
    assert "I can help you think this through here first" in payload["message"]
    assert "mechanical design agent" not in payload["message"]
    visible_messages = payload["conversation"]["messages"]
    assert [message["sender"] for message in visible_messages] == ["user", "maestro"]
    assert "CAD tool" in visible_messages[0]["content"]
    assert "channel model" not in " ".join(message["content"] for message in visible_messages)
    session_artifact = next(
        artifact
        for artifact in session.query(Artifact).all()
        if (artifact.metadata_ or {}).get("canonical_session_artifact") is True
    )
    assert Path(session_artifact.uri).is_file()
    assert Path(session_artifact.uri).parent == tmp_path / "maestro-development" / "inbox"


def test_maestro_api_followup_stays_in_active_topic(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first = client.post(
        "/maestro/respond",
        json={
            "message": (
                "Hey Maestro I want to brainstorm a new feature for Maestro Development, "
                "integrating a CAD tool."
            )
        },
    )
    assert first.status_code == 200
    first_payload = first.json()
    topic_id = first_payload["channel_context"]["topic_id"]

    followup = client.post(
        "/maestro/respond",
        json={"message": "How will this feature interact with the existing tool registry?"},
    )

    assert followup.status_code == 200
    payload = followup.json()
    assert payload["channel_context"]["scope"] == "active_topic"
    assert payload["channel_context"]["topic_id"] == topic_id
    assert payload["channel_context"]["started_new_topic"] is False
    assert len(payload["conversation"]["messages"]) == 4

    second_followup = client.post(
        "/maestro/respond",
        json={"message": "This new feature should also support STEP files."},
    )

    assert second_followup.status_code == 200
    second_payload = second_followup.json()
    assert second_payload["channel_context"]["scope"] == "active_topic"
    assert second_payload["channel_context"]["topic_id"] == topic_id
    assert second_payload["channel_context"]["started_new_topic"] is False


def test_maestro_api_plan_new_feature_starts_fresh_topic(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first = client.post(
        "/maestro/respond",
        json={
            "message": (
                "Lets plan a new feature for Maestro Development that integrates iMessage "
                "notifications."
            )
        },
    )
    assert first.status_code == 200
    first_topic_id = first.json()["channel_context"]["topic_id"]

    second = client.post(
        "/maestro/respond",
        json={
            "message": (
                "Lets plan a new feature that allows for file import directly in the chat here."
            )
        },
    )

    assert second.status_code == 200
    payload = second.json()
    assert payload["channel_context"]["scope"] == "new_topic"
    assert payload["channel_context"]["started_new_topic"] is True
    assert payload["channel_context"]["topic_id"] != first_topic_id
    visible_text = " ".join(message["content"] for message in payload["conversation"]["messages"])
    assert "file import directly in the chat" in visible_text
    assert "iMessage" not in visible_text


def test_maestro_api_new_agent_plan_starts_fresh_topic_despite_typo(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    cad = client.post(
        "/maestro/respond",
        json={
            "message": (
                "I want to brainstorm a Maestro feature where mechanical design agents "
                "generate STL files."
            )
        },
    )
    assert cad.status_code == 200
    cad_topic_id = cad.json()["channel_context"]["topic_id"]

    praxis = client.post(
        "/maestro/respond",
        json={"message": "Iwant to plan a new agent for Praxis that will help generate proposals for us"},
    )

    assert praxis.status_code == 200
    payload = praxis.json()
    assert payload["channel_context"]["scope"] == "new_topic"
    assert payload["channel_context"]["started_new_topic"] is True
    assert payload["channel_context"]["topic_id"] != cad_topic_id
    visible_text = " ".join(message["content"] for message in payload["conversation"]["messages"])
    assert "generate proposals" in visible_text
    assert "STL files" not in visible_text


def test_maestro_api_new_topic_ignores_stale_active_plan_id(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    cad = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    assert cad.status_code == 200
    stale_plan_id = cad.json()["plan"]["parent_task_id"]
    cad_topic_id = cad.json()["channel_context"]["topic_id"]

    drive = client.post(
        "/maestro/respond",
        json={
            "message": (
                "I want to begin discussing a new feature for Maestro to interface with "
                "Google Drive and edit Google Docs and Sheets."
            ),
            "active_plan_id": stale_plan_id,
        },
    )

    assert drive.status_code == 200
    payload = drive.json()
    assert payload["channel_context"]["scope"] == "new_topic"
    assert payload["channel_context"]["topic_id"] != cad_topic_id
    assert payload["classification"] in {"planned", "chat_only", "routed"}
    assert "STL files" not in " ".join(message["content"] for message in payload["conversation"]["messages"])


def test_maestro_api_restart_session_archives_work_and_starts_clean_topic(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    assert first.status_code == 200
    assert first.json()["plan"] is not None

    restarted = client.post(
        "/maestro/respond",
        json={"message": "Restart session and clear current work so we can start fresh."},
    )

    assert restarted.status_code == 200
    payload = restarted.json()
    assert payload["classification"] == "restart_session"
    assert payload["plan"] is None
    assert payload["active_plan"] is None
    assert payload["channel_context"]["topic_title"] == "Fresh Maestro session"
    assert "fresh session topic" in payload["message"]
    assert all(task.status == "archived" for task in session.query(Task).all())
    assert [message["sender"] for message in payload["conversation"]["messages"]] == ["maestro"]


def test_maestro_api_topic_resolver_can_restore_existing_topic(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(session, tmp_path)
    cad = client.post(
        "/maestro/respond",
        json={"message": "Hey Maestro I want to brainstorm a new feature for CAD fabrication."},
    )
    cad_topic_id = cad.json()["channel_context"]["topic_id"]
    gmail = client.post(
        "/maestro/respond",
        json={"message": "Switching gears, let's discuss Gmail triage behavior."},
    )
    gmail_topic_id = gmail.json()["channel_context"]["topic_id"]
    assert gmail_topic_id != cad_topic_id

    class FakeTopicResolution:
        scope = "existing_topic"
        topic_id = cad_topic_id
        confidence = 0.94
        reason = "Message refers back to the CAD fabrication topic."
        suggested_title = None

    monkeypatch.setattr(
        "app.api.maestro.resolve_topic_with_local_llm",
        lambda **_: FakeTopicResolution(),
    )

    restored = client.post(
        "/maestro/respond",
        json={"message": "For the CAD idea, what file formats should we support first?"},
    )

    assert restored.status_code == 200
    payload = restored.json()
    assert payload["channel_context"]["scope"] == "existing_topic"
    assert payload["channel_context"]["topic_id"] == cad_topic_id
    visible_text = " ".join(message["content"] for message in payload["conversation"]["messages"])
    assert "CAD fabrication" in visible_text
    assert "file formats" in visible_text
    assert "Gmail triage" not in visible_text


def test_maestro_api_topic_resolver_can_choose_global_system(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(session, tmp_path)
    first = client.post(
        "/maestro/respond",
        json={"message": "Hey Maestro I want to brainstorm a new feature for CAD fabrication."},
    )
    topic_id = first.json()["channel_context"]["topic_id"]

    class FakeTopicResolution:
        scope = "global_system"
        topic_id = None
        confidence = 0.9
        reason = "User is asking about Maestro globally."
        suggested_title = None

    monkeypatch.setattr(
        "app.api.maestro.resolve_topic_with_local_llm",
        lambda **_: FakeTopicResolution(),
    )

    response = client.post(
        "/maestro/respond",
        json={"message": "What tools are available across the whole system?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["channel_context"]["scope"] == "global_system"
    assert payload["channel_context"]["topic_id"] == topic_id
    assert payload["channel_context"]["started_new_topic"] is False


def test_maestro_topic_history_is_capped(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)

    for index in range(30):
        response = client.post(
            "/maestro/respond",
            json={"message": f"Switching gears, new topic {index}: brainstorm feature {index}."},
        )
        assert response.status_code == 200

    conversation = response.json()["conversation"]
    stored = session.get(Conversation, uuid.UUID(conversation["id"]))
    assert stored is not None
    topics = (stored.metadata_ or {}).get("topics")
    assert isinstance(topics, list)
    assert len(topics) == 24
    assert all("summary" in topic for topic in topics)
    assert all(len(topic.get("keywords", [])) <= 12 for topic in topics)


def test_maestro_channel_websocket_sends_active_conversation(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    conversation_id = response.json()["conversation"]["id"]

    with client.websocket_connect("/maestro/channel/ws") as websocket:
        payload = websocket.receive_json()

    assert payload["type"] == "conversation"
    assert payload["conversation"]["id"] == conversation_id
    assert payload["conversation"]["messages"][0]["content"] == "Prepare a Praxis partner call workflow."


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


def test_maestro_api_respond_treats_standalone_followup_as_new_workflow(
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
            "message": "Prepare an Ophi research workflow.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "planned"
    assert payload["plan"]["parent_task_id"] != first_plan["parent_task_id"]
    task = session.get(Task, uuid.UUID(payload["plan"]["parent_task_id"]))
    assert task is not None
    assert "refined_from_plan_id" not in task.input_payload


def test_maestro_api_respond_deletes_active_workflow_instead_of_refining(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first_response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    first_plan = first_response.json()["plan"]
    second_response = client.post(
        "/maestro/respond",
        json={"message": "Prepare an Ophi research workflow."},
    )
    second_plan = second_response.json()["plan"]

    response = client.post(
        "/maestro/respond",
        json={
            "active_plan_id": second_plan["parent_task_id"],
            "message": "Please delete the workflow currently under development.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "chat_only"
    assert payload["classification"] == "delete_workflow"
    assert payload["plan"] is None
    assert payload["active_plan"] is None
    for plan in (first_plan, second_plan):
        task = session.get(Task, uuid.UUID(plan["parent_task_id"]))
        assert task is not None
        assert task.status == "archived"
        run = session.scalar(
            select(WorkflowRun).where(WorkflowRun.parent_task_id == uuid.UUID(plan["parent_task_id"]))
        )
        if run is not None:
            assert run.status == "archived"
    active = client.get("/maestro/sessions/active")
    assert active.json()["conversation"]["active_plan"] is None


def test_maestro_api_respond_clears_current_workflow_without_creating_new_workflow(
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
            "message": "Clear the current workflow.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "chat_only"
    assert payload["classification"] == "delete_workflow"
    assert payload["plan"] is None
    task = session.get(Task, uuid.UUID(first_plan["parent_task_id"]))
    assert task is not None
    assert task.status == "archived"
    assert session.query(Task).filter(Task.workflow_key == "maestro.generic").count() == 1


def test_maestro_api_cleanup_command_archives_latest_open_workflow_and_routed_items(
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
    assert first_response.status_code == 200
    first_plan = first_response.json()["plan"]
    routed_before = session.query(RoutedItem).filter(RoutedItem.status != "archived").count()
    assert routed_before > 0

    direct_response = client.post(
        "/maestro/respond",
        json={"message": "Tell me about Maestro."},
    )
    assert direct_response.status_code == 200
    task_count_before_cleanup = session.query(Task).count()

    response = client.post(
        "/maestro/respond",
        json={
            "message": (
                "Okay please clear the last workflow that is blocked and archive the routed "
                "items that resulted from it so we can try again from scratch."
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "chat_only"
    assert payload["classification"] == "delete_workflow"
    assert payload["plan"] is None
    assert "routed item" in payload["message"]
    task = session.get(Task, uuid.UUID(first_plan["parent_task_id"]))
    assert task is not None
    assert task.status == "archived"
    run = session.scalar(
        select(WorkflowRun).where(WorkflowRun.parent_task_id == uuid.UUID(first_plan["parent_task_id"]))
    )
    if run is not None:
        assert run.status == "archived"
    workflow_routed_items = [
        item
        for item in session.query(RoutedItem).all()
        if any(
            isinstance(ref, dict) and ref.get("task_id") == first_plan["parent_task_id"]
            for ref in (item.source_refs or [])
        )
    ]
    assert workflow_routed_items
    assert all(item.status == "archived" for item in workflow_routed_items)
    assert session.query(Task).count() == task_count_before_cleanup


def test_maestro_api_archive_plan_clears_candidate_from_active_session(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    assert response.status_code == 200
    plan = response.json()["plan"]

    archived = client.post(
        f"/maestro/plans/{plan['parent_task_id']}/archive",
        json={"reason": "Declined during test.", "conversation_id": response.json()["conversation"]["id"]},
    )

    assert archived.status_code == 200
    assert archived.json()["plan"]["status"] == "archived"
    task = session.get(Task, uuid.UUID(plan["parent_task_id"]))
    assert task is not None
    assert task.status == "archived"
    active = client.get("/maestro/sessions/active")
    assert active.status_code == 200
    assert active.json()["conversation"]["active_plan"] is None


def test_orchestrator_archive_plan_disables_saved_schedule_definition(
    session: Session,
) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeScheduledPlannerLLMClient(),
    )
    plan = service.create_plan(
        "Each morning at 9am please review the Maestro backlog and propose a work plan for the day"
    )
    service.run_plan(plan.parent_task_id, execute_llm=False)
    definition = session.query(WorkflowDefinition).one()
    workflow_run = session.query(WorkflowRun).one()
    assert definition.is_active is True
    assert workflow_run.status == "scheduled"

    archived = service.archive_plan(plan.parent_task_id, reason="Test archive saved schedule.")

    assert archived.status == "archived"
    session.refresh(definition)
    session.refresh(workflow_run)
    task = session.get(Task, uuid.UUID(plan.parent_task_id))
    assert task is not None
    assert task.status == "archived"
    assert definition.is_active is False
    assert definition.trigger_config["archive_reason"] == "Test archive saved schedule."
    assert workflow_run.status == "archived"
    assert SchedulerService(session).dashboard()["definitions"] == []


def test_maestro_api_refinement_supersedes_previous_queued_workflow(
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
    old_task = session.get(Task, uuid.UUID(first_plan["parent_task_id"]))
    new_task = session.get(Task, uuid.UUID(payload["plan"]["parent_task_id"]))
    assert old_task is not None
    assert new_task is not None
    assert old_task.status == "archived"
    assert new_task.status == "proposed"
    old_run = session.scalar(
        select(WorkflowRun).where(WorkflowRun.parent_task_id == uuid.UUID(first_plan["parent_task_id"]))
    )
    new_run = session.scalar(
        select(WorkflowRun).where(WorkflowRun.parent_task_id == uuid.UUID(payload["plan"]["parent_task_id"]))
    )
    if old_run is not None:
        assert old_run.status == "archived"
    if new_run is not None:
        assert new_run.status in {"queued", "proposed"}


def test_maestro_api_respond_answers_scheduled_workflow_status_without_queuing(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    created = client.post(
        "/scheduler/definitions",
        json={
            "key": "maestro-backlog-plan",
            "name": "Daily Maestro Backlog Plan",
            "trigger_type": "recurring",
            "trigger_config": {"time_of_day": "09:00", "interval_minutes": 1440},
            "workflow_spec": {
                "queue_items": [
                    {
                        "id": "plan-day",
                        "objective": "Review the Maestro backlog and propose a daily work plan.",
                        "domain_key": "maestro-development",
                    }
                ]
            },
        },
    )
    assert created.status_code == 200
    task_count = len(session.scalars(select(Task)).all())

    response = client.post(
        "/maestro/respond",
        json={"message": "Tell me all of the actively scheduled workflows."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "chat_only"
    assert payload["classification"] == "system_status"
    assert payload["plan"] is None
    assert "Daily Maestro Backlog Plan" in payload["message"]
    assert len(session.scalars(select(Task)).all()) == task_count


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


def test_maestro_api_respond_resolves_channel_context_without_explicit_plan_id(
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
        "tool_activity": [
            {
                "tool_name": "codex.task.run",
                "status": "complete",
                "details": "Opened PR #77 for review.",
                "output_payload": {
                    "pr_number": 77,
                    "pr_url": "https://github.com/example/maestro/pull/77",
                },
            }
        ],
    }
    session.commit()

    response = client.post(
        "/maestro/respond",
        json={"message": "Cool, merge the PR and reload the app."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "refined"
    refined_task = session.get(Task, uuid.UUID(payload["plan"]["parent_task_id"]))
    assert refined_task is not None
    assert "PR number: 77" in refined_task.input_payload["user_input"]


def test_maestro_api_implicit_channel_context_is_scoped_to_active_topic(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(session, tmp_path)
    old_response = client.post(
        "/maestro/respond",
        json={"message": "Prepare a Praxis partner call workflow."},
    )
    assert old_response.status_code == 200
    old_payload = old_response.json()
    old_topic_id = old_payload["channel_context"]["topic_id"]
    old_plan = old_payload["plan"]
    old_task = session.get(Task, uuid.UUID(old_plan["parent_task_id"]))
    assert old_task is not None
    assert old_task.input_payload["topic_id"] == old_topic_id

    class FakeTopicResolution:
        scope = "new_topic"
        topic_id = None
        confidence = 0.93
        reason = "Chris switched to a new work topic."
        suggested_title = "Maestro issue implementation"

    monkeypatch.setattr(
        "app.api.maestro.resolve_topic_with_local_llm",
        lambda **_: FakeTopicResolution(),
    )

    new_response = client.post(
        "/maestro/respond",
        json={
            "message": (
                "Switching gears: have the Maestro coding agent implement issue 99, "
                "then we can refine it here."
            )
        },
    )

    assert new_response.status_code == 200
    payload = new_response.json()
    assert payload["channel_context"]["scope"] == "new_topic"
    assert payload["channel_context"]["topic_id"] != old_topic_id
    new_plan = payload["plan"]
    assert new_plan is not None
    new_task = session.get(Task, uuid.UUID(new_plan["parent_task_id"]))
    assert new_task is not None
    assert new_task.input_payload["topic_id"] == payload["channel_context"]["topic_id"]
    assert new_task.input_payload.get("refined_from_plan_id") is None
    assert "Praxis partner call" not in new_task.input_payload["user_input"]


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


def test_maestro_api_respond_uses_llm_classifier_for_open_rfi_answer(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = MaestroOrchestratorService(
        session,
        planner_llm_client=FakePlannerLLMClient(),
    ).create_plan("Prepare a Praxis partner call workflow.")
    assert any(
        item.needs_user_input
        for item in plan.work_items
    )

    classifier_calls: list[dict] = []

    def fake_understanding(**kwargs):
        classifier_calls.append(kwargs)
        assert kwargs["active_plan"]["open_rfis"]
        return MaestroMessageUnderstandingResponse(
            topic_scope="active_topic",
            relationship_to_active_plan="answers_rfi",
            intents=[
                {
                    "type": "rfi_answer",
                    "rfi_ids": ["wi_partner_owner"],
                    "span": kwargs["message"],
                    "confidence": 0.91,
                    "recommended_next_step": "refine_plan",
                }
            ],
            recommended_next_step="answer_and_refine_plan",
            confidence=0.91,
            reason="Chris provided the owner context requested by an open RFI.",
        )

    monkeypatch.setattr("app.api.maestro.understand_message_with_local_llm", fake_understanding)
    message = (
        "Currently the partner follow-up should stay with Chris until we decide who "
        "owns the next outreach lane."
    )

    assert maestro_api._should_use_plan_context(message, plan) is True
    assert maestro_api._classify_active_session_message(message, plan) == "rfi_answered"
    assert classifier_calls


def test_message_understanding_supports_multiple_intents_with_per_intent_next_steps() -> None:
    understanding = MaestroMessageUnderstandingResponse.model_validate(
        {
            "topic_scope": "active_topic",
            "relationship_to_active_plan": "answers_rfi",
            "intents": [
                {
                    "type": "chat_response",
                    "span": "What tasks would be useful for a CFO agent?",
                    "confidence": 0.93,
                    "recommended_next_step": "respond",
                },
                {
                    "type": "rfi_answer",
                    "rfi_ids": ["wi_finance_stack"],
                    "span": "We use Google Sheets and Mercury.",
                    "confidence": 0.88,
                    "recommended_next_step": "refine_plan",
                },
                {
                    "type": "workflow_request",
                    "span": "Have the CFO agent research invoice automation.",
                    "confidence": 0.84,
                    "recommended_next_step": "plan",
                },
            ],
            "recommended_next_step": "answer_and_refine_plan",
            "confidence": 0.9,
            "reason": "The message contains a question, an RFI answer, and a new work request.",
        }
    )

    assert [intent.recommended_next_step for intent in understanding.intents] == [
        "respond",
        "refine_plan",
        "plan",
    ]
    assert understanding.legacy_intent() == "rfi_answered"


def test_maestro_intent_context_is_planner_guidance_not_user_text() -> None:
    understanding = MaestroMessageUnderstandingResponse(
        topic_scope="active_topic",
        relationship_to_active_plan="none",
        intents=[
            {
                "type": "chat_response",
                "span": "What tasks would be useful for a CFO agent?",
                "confidence": 0.93,
                "recommended_next_step": "respond",
            },
            {
                "type": "workflow_request",
                "span": "Have the CFO agent research invoice automation.",
                "confidence": 0.84,
                "recommended_next_step": "plan",
            },
        ],
        recommended_next_step="plan",
        confidence=0.9,
        reason="The message asks for an answer and executable work.",
    )

    message = maestro_api._message_with_intent_context(
        "Latest Chris message:\nWhat tasks would be useful, and have the CFO agent research invoices.",
        understanding,
    )

    assert "Use this as routing guidance only" in message
    assert "<maestro_hidden_context" in message
    assert "the planner still owns task decomposition" in message
    assert '"type": "chat_response"' in message
    assert '"recommended_next_step": "plan"' in message
    assert "Latest Chris message" in message


def test_maestro_topic_context_is_hidden_from_planner_copy_targets(
    session: Session,
    tmp_path: Path,
) -> None:
    client = _client(session, tmp_path)
    first = client.post(
        "/maestro/respond",
        json={"message": "I want to brainstorm a Maestro feature where mechanical agents use CAD."},
    )
    assert first.status_code == 200
    conversation = session.scalar(select(Conversation).where(Conversation.title == "Maestro channel"))
    assert conversation is not None
    topic_context = {**first.json()["channel_context"], "scope": "active_topic"}
    user_message = session.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.sender_type == "user")
        .order_by(Message.created_at.desc())
    )
    assert user_message is not None

    wrapped = maestro_api._message_with_topic_context(
        session,
        conversation,
        "How will this interact with the tool registry?",
        topic_context=topic_context,
        current_message_id=user_message.id,
    )

    assert "<maestro_hidden_context" in wrapped
    assert "</maestro_hidden_context>" in wrapped
    assert "<latest_chris_message>" in wrapped
    assert "Do not copy this context into user-facing responses" in wrapped
    assert "How will this interact with the tool registry?" in wrapped


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
    assert payload["plan"] is None
    assert payload["chat_plan"]["is_routing_only"] is True
    assert payload["active_plan"]["plan_id"] == first_plan["plan_id"]


def test_routed_only_message_completes_without_schedule_candidate(session: Session) -> None:
    service = MaestroOrchestratorService(session)

    plan = service.create_plan(
        "Praxis note: Jane Smith is the partner lead at Example Corp. "
        "RFI: Chris needs to confirm the follow-up owner. "
        "Due-out: Draft a partner follow-up email. "
        "Event: daily standup today at 1200."
    )

    assert plan.status == "completed"
    assert plan.is_chat_only is True
    assert plan.is_routing_only is True
    assert plan.approval_required is False
    assert plan.scheduler["schedule_candidate"] is None
    assert plan.scheduler["queue_items"] == []
    assert session.query(RoutedItem).count() >= 3


def test_orchestrator_routes_contact_shaped_standalone_work_item_as_contact(
    session: Session,
) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeStandaloneContactPlannerLLMClient(),
    )

    plan = service.create_plan("Capture Ben Daniels from XVIII Airborne Corps as Praxis engagement contact.")

    assert plan.is_routing_only is True
    assert session.query(Todo).count() == 0
    routed = session.query(RoutedItem).one()
    assert routed.route_type == "contact"
    assert session.query(Contact).one().name == "Ben Daniels"


def test_orchestrator_routes_feature_concepts_to_think_tank(session: Session) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeThinkTankPlannerLLMClient(),
    )

    plan = service.create_plan(
        "This new feature will be a CAD AI tool for mechanical design agents."
    )

    assert plan.is_routing_only is True
    assert plan.is_chat_only is True
    assert plan.approval_required is False
    assert plan.work_items[0].type == "think_tank"
    assert "Think Tank" in (plan.direct_response or "")
    routed = session.query(RoutedItem).one()
    assert routed.route_type == "think_tank"
    idea = session.query(Idea).one()
    assert "CAD AI tool" in idea.title


def test_orchestrator_drops_duplicate_todo_for_agent_work(session: Session) -> None:
    service = MaestroOrchestratorService(
        session,
        planner_llm_client=FakeWorkflowWithDuplicateTaskPlannerLLMClient(),
    )

    plan = service.create_plan("Have the SOTA researcher investigate current CAD AI tooling.")

    assert plan.is_routing_only is False
    assert [item.type for item in plan.work_items] == ["workflow_task"]
    assert session.query(Todo).count() == 0


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


def test_maestro_api_pure_chat_classifier_bypasses_planner(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(session, tmp_path)

    def fake_understanding(**kwargs):
        return MaestroMessageUnderstandingResponse(
            topic_scope="new_topic",
            relationship_to_active_plan="none",
            intents=[
                {
                    "type": "chat_response",
                    "span": kwargs["message"],
                    "confidence": 0.94,
                    "recommended_next_step": "respond",
                    "reason": "Chris is asking to brainstorm conversationally.",
                }
            ],
            recommended_next_step="respond",
            confidence=0.94,
            reason="This should be answered conversationally without agent work.",
        )

    monkeypatch.setattr("app.api.maestro.understand_message_with_local_llm", fake_understanding)
    monkeypatch.setattr(
        "app.api.maestro._direct_chat_response",
        lambda db, message: "Absolutely. Let's brainstorm this here before tasking agents.",
    )

    response = client.post(
        "/maestro/respond",
        json={"message": "What do you think about adding a Google Docs feature?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "chat_only"
    assert payload["classification"] == "direct_chat"
    assert payload["plan"] is None
    assert payload["chat_plan"] is None
    assert "brainstorm this here" in payload["message"]
    assert session.query(Task).count() == 0


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
