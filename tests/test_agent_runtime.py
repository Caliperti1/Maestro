from pathlib import Path
import base64
import json
import uuid
from subprocess import CompletedProcess

import pytest
from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentToolRequest,
    AgentRegistryService,
    InteractionArtifactPackager,
    PromptAggregationService,
    PromptPackageRequest,
    _compact_tool_results_for_prompt,
    _deterministic_tool_plan,
    _harden_email_tool_plan,
    _llm_client_for_model_profile,
)
from app.core.config import get_settings
from app.db.models import (
    Artifact,
    Contact,
    MemoryItem,
    Message,
    Report,
    Task,
    ToolCall,
    ToolConnection,
    WorkflowNotification,
    WorkflowRun,
)
from app.db.repositories import AgentRepository, DomainRepository
from app.db.seed import seed_default_domains
from app.tools.runtime import (
    CodexCliToolAdapter,
    GmailApiToolAdapter,
    GoogleWorkspaceToolAdapter,
    GitHubCliToolAdapter,
    LLMGatewayToolAdapter,
    LocalAppReloadAdapter,
    MemoryContextBundleToolAdapter,
    ToolExecutionContext,
    ToolExecutionError,
    ToolExecutionRequest,
    ToolExecutionService,
    WebSearchToolAdapter,
    RoutedItemCreateToolAdapter,
    WorkflowNotificationCreateToolAdapter,
    _clean_github_search_query,
    _github_read_search_terms,
    default_tool_adapters,
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


class FakeAutoGitHubReadToolLoopLLMClient:
    provider = "test"
    model = "test-agent-model"

    def __init__(self):
        self.structured_calls = 0

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        self.structured_calls += 1
        if self.structured_calls == 1:
            return {
                "plan_summary": "Inspect the repository with the aggregate read tool.",
                "requires_final_answer": True,
                "tool_calls": [
                    {
                        "tool_key": "github.read",
                        "payload_json": '{"request":"Inspect repository architecture only."}',
                        "rationale": "Read-only repository inspection.",
                    }
                ],
            }
        return {
            "plan_summary": "Enough repository context has been gathered.",
            "requires_final_answer": True,
            "tool_calls": [],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "github.read" in input_text
        return "## Summary\nRepository architecture inspected with read-only GitHub context."


class FakeGoogleSlidesFallbackLLMClient:
    provider = "test"
    model = "test-agent-model"

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        assert "execution planner" in instructions
        assert "docs.google.com/presentation" in input_text
        return {
            "plan_summary": "No tool call selected.",
            "requires_final_answer": True,
            "tool_calls": [],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "google.slides.get" in input_text
        return "## Summary\nSlides deck access verified through fallback."


class FakeGoogleSlidesLLMPlannerClient:
    provider = "test"
    model = "test-agent-model"

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        assert "execution planner" in instructions
        return {
            "plan_summary": "Use the LLM-selected Drive metadata tool first.",
            "requires_final_answer": True,
            "tool_calls": [
                {
                    "tool_key": "google.drive.file.get",
                    "payload_json": '{"file_id":"deck-123"}',
                    "rationale": "The LLM planner chose Drive metadata.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "google.drive.file.get" in input_text
        return "## Summary\nLLM-selected Google tool was used."


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


class FakeAutoLLMGatewayToolLoopLLMClient:
    provider = "test"
    model = "test-agent-model"

    def __init__(self):
        self.structured_calls = 0

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        assert "internal_reasoning" in input_text
        self.structured_calls += 1
        if self.structured_calls == 1:
            return {
                "plan_summary": "Use the internal gateway for a focused research synthesis.",
                "requires_final_answer": True,
                "tool_calls": [
                    {
                        "tool_key": "llm.gateway",
                        "payload_json": (
                            '{"prompt":"Survey CAD-to-STL agent architecture options.",'
                            '"context":"Maestro Development brainstorm"}'
                        ),
                        "rationale": "The task asks for architecture synthesis.",
                    }
                ],
            }
        return {
            "plan_summary": "Internal synthesis is available.",
            "requires_final_answer": True,
            "tool_calls": [],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "llm.gateway" in input_text
        assert "CAD toolchain synthesis" in input_text
        return "## Summary\nCAD integration options reviewed.\n\n## Next Steps\nPick a prototype path."


class FakeAutoMemoryContextToolLoopLLMClient:
    provider = "test"
    model = "test-agent-model"

    def __init__(self):
        self.structured_calls = 0

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        self.structured_calls += 1
        if self.structured_calls == 1:
            return {
                "plan_summary": "Retrieve Maestro memory before answering.",
                "requires_final_answer": True,
                "tool_calls": [
                    {
                        "tool_key": "memory.context_bundle",
                        "payload_json": (
                            '{"query_text":"CAD tool STL generation architecture",'
                            '"domain_key":"maestro-development","use_semantic":false,'
                            '"max_items":6,"max_chars":1200}'
                        ),
                        "rationale": "The agent needs scoped RAG context before reporting.",
                    }
                ],
            }
        return {
            "plan_summary": "Memory context is available.",
            "requires_final_answer": True,
            "tool_calls": [],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "memory.context_bundle" in input_text
        assert "CAD design agents should use shared CAD tool infrastructure" in input_text
        return "## Summary\nRetrieved Maestro memory before answering.\n\n## Next Steps\nUse the shared tool pattern."


class FakeSingleEmailTriageLLMClient:
    provider = "test"
    model = "test-email-triage-model"

    def __init__(self) -> None:
        self.structured_calls = 0

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        self.structured_calls += 1
        if self.structured_calls == 1:
            return {
                "plan_summary": "Read exactly the latest Praxis email metadata.",
                "requires_final_answer": True,
                "tool_calls": [
                    {
                        "tool_key": "gmail.message.list_recent",
                        "payload_json": '{"limit":1,"unread_only":false}',
                        "rationale": "Select exactly one latest message.",
                    }
                ],
            }
        if self.structured_calls == 2:
            assert "msg-atlas-1" in input_text
            return {
                "plan_summary": "Read the selected message body.",
                "requires_final_answer": True,
                "tool_calls": [
                    {
                        "tool_key": "gmail.message.get",
                        "payload_json": '{"message_id":"msg-atlas-1","max_body_chars":6000}',
                        "rationale": "The full body is required for triage.",
                    }
                ],
            }
        if self.structured_calls == 3:
            assert "Jordan Lee" in input_text
            return {
                "plan_summary": "Route the contact and notify Chris about the deadline.",
                "requires_final_answer": True,
                "tool_calls": [
                    {
                        "tool_key": "routed.item.create",
                        "payload_json": json.dumps(
                            {
                                "route_type": "contact",
                                "title": "Jordan Lee",
                                "content": "Jordan Lee is the partnerships director at Atlas Systems.",
                                "metadata": {
                                    "name": "Jordan Lee",
                                    "email": "jordan@example.com",
                                    "organization": "Atlas Systems",
                                },
                                "message_id": "msg-atlas-1",
                                "thread_id": "thread-atlas-1",
                                "subject": "Maestro triage test - Atlas partner sync",
                                "from": "Jordan Lee <jordan@example.com>",
                            }
                        ),
                        "rationale": "The sender is a durable Praxis contact.",
                    },
                    {
                        "tool_key": "workflow.notification.create",
                        "payload_json": json.dumps(
                            {
                                "title": "Praxis email needs your response",
                                "message": "Confirm Atlas call availability by July 21.",
                                "severity": "warning",
                                "reason": "The sender requested a decision by a deadline.",
                                "message_id": "msg-atlas-1",
                                "thread_id": "thread-atlas-1",
                                "subject": "Maestro triage test - Atlas partner sync",
                                "from": "Jordan Lee <jordan@example.com>",
                            }
                        ),
                        "rationale": "Chris must respond by a concrete deadline.",
                    },
                ],
            }
        return {
            "plan_summary": "Triage evidence and routed outputs are complete.",
            "requires_final_answer": True,
            "tool_calls": [],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "workflow.notification.create" in input_text
        assert "routed.item.create" in input_text
        return (
            "conversation: I reviewed the Atlas email, saved Jordan as a Praxis contact, and "
            "notified you that a response is due July 21.\n\n"
            "## Classification\nresponse_needed (0.98)\n\n"
            "## Routed Items\nJordan Lee contact created."
        )


class FakeSingleEmailGmailAdapter:
    def __init__(self, key: str):
        self.key = key

    def execute(self, context: ToolExecutionContext, payload: dict[str, object]) -> dict[str, object]:
        if self.key == "gmail.message.list_recent":
            assert payload["limit"] == 1
            return {
                "messages": [
                    {
                        "message_id": "msg-atlas-1",
                        "thread_id": "thread-atlas-1",
                        "subject": "Maestro triage test - Atlas partner sync",
                    }
                ],
                "summary": {"type": "gmail_message_list", "count": 1},
            }
        assert payload["message_id"] == "msg-atlas-1"
        return {
            "message_id": "msg-atlas-1",
            "thread_id": "thread-atlas-1",
            "subject": "Maestro triage test - Atlas partner sync",
            "from": "Jordan Lee <jordan@example.com>",
            "body": (
                "Jordan Lee at Atlas Systems asked Chris to confirm partner-call availability "
                "by July 21."
            ),
            "summary": {"type": "gmail_message", "message_id": "msg-atlas-1"},
        }


class FakeEmailTriageFinalizationLLMClient:
    provider = "test"
    model = "test-email-finalization-model"

    def __init__(self) -> None:
        self.structured_calls = 0

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        self.structured_calls += 1
        if self.structured_calls == 1:
            return {
                "plan_summary": "Select the latest Praxis email.",
                "requires_final_answer": True,
                "tool_calls": [{
                    "tool_key": "gmail.message.list_recent",
                    "payload_json": '{"limit":1,"unread_only":false}',
                    "rationale": "Select one message.",
                }],
            }
        if self.structured_calls == 2:
            return {
                "plan_summary": "Read the selected email.",
                "requires_final_answer": True,
                "tool_calls": [{
                    "tool_key": "gmail.message.get",
                    "payload_json": '{"message_id":"msg-atlas-1","max_body_chars":6000}',
                    "rationale": "Read the full message.",
                }],
            }
        if self.structured_calls == 3:
            return {
                "plan_summary": "Read thread context.",
                "requires_final_answer": True,
                "tool_calls": [{
                    "tool_key": "gmail.thread.get",
                    "payload_json": '{"thread_id":"thread-atlas-1"}',
                    "rationale": "Resolve ownership from thread context.",
                }],
            }
        if self.structured_calls == 4:
            return {
                "plan_summary": "Inspect the linked Drive folder.",
                "requires_final_answer": True,
                "tool_calls": [{
                    "tool_key": "google.drive.file.get",
                    "payload_json": '{"file_id":"folder-atlas-1"}',
                    "rationale": "Inspect supporting files when accessible.",
                }],
            }
        assert "operational finalizer" in instructions
        assert "Jordan Lee" in input_text
        return {
            "plan_summary": "Route the contact and notify Chris despite the optional Drive failure.",
            "requires_final_answer": True,
            "tool_calls": [
                {
                    "tool_key": "routed.item.create",
                    "payload_json": json.dumps({
                        "route_type": "contact",
                        "title": "Jordan Lee",
                        "content": "Jordan Lee is the partnerships director at Atlas Systems.",
                        "metadata": {
                            "name": "Jordan Lee",
                            "email": "jordan@example.com",
                            "organization": "Atlas Systems",
                        },
                        "message_id": "msg-atlas-1",
                        "thread_id": "thread-atlas-1",
                        "subject": "Atlas response requested",
                        "from": "Jordan Lee <jordan@example.com>",
                    }),
                    "rationale": "Preserve the durable contact.",
                },
                {
                    "tool_key": "workflow.notification.create",
                    "payload_json": json.dumps({
                        "title": "Atlas email needs your response",
                        "message": "Jordan asked you to confirm availability by July 21.",
                        "severity": "warning",
                        "reason": "Chris Aliperti personally owes a response.",
                        "message_id": "msg-atlas-1",
                        "thread_id": "thread-atlas-1",
                        "subject": "Atlas response requested",
                        "from": "Jordan Lee <jordan@example.com>",
                    }),
                    "rationale": "Surface the required response to Chris.",
                },
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "workflow.notification.create" in input_text
        return "conversation: I saved Jordan and notified you about the July 21 response."


class FakeEmailTriageEvidenceAdapter:
    def __init__(self, key: str):
        self.key = key

    def execute(self, context: ToolExecutionContext, payload: dict[str, object]) -> dict[str, object]:
        if self.key == "gmail.message.list_recent":
            return {"messages": [{"message_id": "msg-atlas-1"}]}
        if self.key == "gmail.message.get":
            return {
                "message_id": "msg-atlas-1",
                "thread_id": "thread-atlas-1",
                "subject": "Atlas response requested",
                "from": "Jordan Lee <jordan@example.com>",
                "to": "Chris Aliperti <chris.aliperti@praxis-defense.com>",
                "body_text": "Jordan Lee at Atlas Systems asked Chris to confirm by July 21.",
                "google_workspace_links": [{
                    "kind": "folder",
                    "file_id": "folder-atlas-1",
                    "url": "https://drive.google.com/drive/folders/folder-atlas-1",
                }],
            }
        if self.key == "gmail.thread.get":
            return {"thread_id": "thread-atlas-1", "text": "Chris Aliperti owns the response."}
        raise ToolExecutionError("Supporting Drive folder is not shared with this account.")


class FakeAutoMergeMissingPrNumberLLMClient:
    provider = "test"
    model = "test-agent-model"

    def structured_response(self, *, instructions: str, input_text: str, **kwargs):
        assert "pass that number as `pr_number`" in instructions
        assert "pr_number" in input_text or "PR number" in input_text
        return {
            "plan_summary": "Merge the PR Chris approved.",
            "requires_final_answer": True,
            "tool_calls": [
                {
                    "tool_key": "github.pr.merge",
                    "payload_json": '{"method":"squash","delete_branch":true}',
                    "rationale": "Chris asked to merge the PR from the active session.",
                }
            ],
        }

    def text_response(self, *, instructions: str, input_text: str) -> str:
        assert "github.pr.merge" in input_text
        return "## Summary\nI proposed merging the active PR."


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


class FakeLLMGatewayAdapter:
    key = "llm.gateway"

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, object],
    ) -> dict[str, object]:
        assert context.domain.key == "maestro-development"
        assert payload["prompt"] == "Survey CAD-to-STL agent architecture options."
        return {
            "summary": {"type": "llm_gateway_response", "model": "test-agent-model"},
            "output_text": "CAD toolchain synthesis: consider FreeCAD, build123d, and Blender.",
        }


class FakeWebSearchLLMClient:
    provider = "openrouter"
    model = "test-web-model"

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def web_search_response(
        self,
        *,
        instructions: str,
        input_text: str,
        search_parameters: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert "research" in instructions.lower()
        assert input_text == "current CAD AI STL generation tools"
        assert search_parameters == {
            "engine": "exa",
            "max_results": 3,
            "allowed_domains": ["freecad.org"],
        }
        return {
            "output_text": "FreeCAD and build123d are relevant options.",
            "annotations": [
                {
                    "type": "url_citation",
                    "url_citation": {
                        "url": "https://www.freecad.org/",
                        "title": "FreeCAD",
                        "content": "FreeCAD is an open source parametric 3D modeler.",
                    },
                }
            ],
            "usage": {"server_tool_use": {"web_search_requests": 1}},
        }


def test_seed_agent_registry_returns_domain_scoped_specs(session: Session) -> None:
    specs = AgentRegistryService(session).list_specs()

    praxis = next(spec for spec in specs if spec.key == "praxis-planning-agent")
    assert praxis.domain_key == "praxis"
    assert praxis.memory_profile == "agent_prompt"
    praxis_tools = [tool.key for tool in praxis.allowed_tools]
    assert "artifact.stage_interaction" in praxis_tools
    assert "llm.gateway" in praxis_tools
    assert "memory.context_bundle" in praxis_tools
    assert "gmail.message.search" in praxis_tools
    coding = next(spec for spec in specs if spec.key == "maestro-coding-agent")
    assert coding.domain_key == "maestro-development"
    assert "codex.task.run" in [tool.key for tool in coding.allowed_tools]
    introspection = next(spec for spec in specs if spec.key == "maestro-introspection-agent")
    assert "web.search" in [tool.key for tool in introspection.allowed_tools]


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


def test_prompt_aggregation_uses_domain_background_query_for_external_file_tasks(
    session: Session,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    session.add(
        MemoryItem(
            scope="domain",
            domain_id=praxis.id,
            memory_type="fact",
            title="Praxis operating context",
            content=(
                "Praxis Defense focuses on AI-enabled defense transition and training "
                "workflows, not ground combat vehicle programs."
            ),
            impact_level="high",
            importance=0.95,
            metadata_={},
        )
    )
    session.commit()

    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction=(
                "Read this Google Slides deck and summarize what it says: "
                "https://docs.google.com/presentation/d/deck-123/edit"
            ),
            query_text="https://docs.google.com/presentation/d/deck-123/edit",
            use_semantic=False,
        )
    )

    assert "Praxis Defense focuses on AI-enabled defense transition" in package.assembled_prompt
    assert package.memory_context.included_count >= 1
    assert "ground combat vehicle" in package.memory_context.rendered_text
    assert "domain background for Praxis" in (package.memory_context.request.query_text or "")


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
        key="Praxis Mailroom Agent",
        name="Praxis Mailroom Agent",
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

    assert agent.key == "praxis-mailroom-agent"
    assert connection.config["api_key"] == "********"
    assert connection.config["label"] == "praxis"
    assert registry.get_spec("praxis-mailroom-agent").allowed_tools[0].connection_id is not None


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


def test_tool_manifest_can_inherit_google_family_connection_for_gmail(session: Session) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="praxis",
        tool_key="google",
        display_name="Praxis Google Workspace",
        auth_type="oauth",
        config={
            "user_id": "me",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": "GOOGLE_CLIENT_SECRET",
            "refresh_token_env": "PRAXIS_GOOGLE_REFRESH_TOKEN",
        },
    )

    spec = registry.get_spec("praxis-planning-agent")
    tools = registry.list_tools()

    message_search = next(tool for tool in spec.allowed_tools if tool.key == "gmail.message.search")
    assert message_search.connection_id is not None
    assert message_search.auth_type == "oauth"
    registry_message_search = next(tool for tool in tools if tool.key == "gmail.message.search")
    assert "praxis" in registry_message_search.connected_domains


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


def test_internal_memory_tool_is_added_to_custom_agents_by_default(session: Session) -> None:
    registry = AgentRegistryService(session)
    custom = registry.create_agent_spec(
        domain_key="maestro-development",
        key="custom-maestro-agent",
        name="Custom Maestro Agent",
        role_summary="Tests default internal memory tools.",
        tool_permissions={},
    )

    assert "memory.context_bundle" in [tool.key for tool in custom.allowed_tools]


def test_seed_agent_merge_adds_gmail_read_permissions_to_existing_praxis_agent(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("praxis-planning-agent")
    agent = AgentRepository(session).get_by_key("praxis-planning-agent")
    assert agent is not None
    agent.tool_permissions = {
        "memory.context_bundle": {
            "permission": "read",
            "description": "Legacy seed permissions.",
        }
    }
    session.commit()

    refreshed = registry.get_spec("praxis-planning-agent")

    allowed = [tool.key for tool in refreshed.allowed_tools]
    assert "gmail.message.search" in allowed
    assert "gmail.message.list_recent" in allowed
    assert "gmail.message.get" in allowed
    assert "gmail.thread.get" in allowed


def test_seeded_praxis_email_agent_has_email_triage_tools_skills_and_luna_model(
    session: Session,
) -> None:
    spec = AgentRegistryService(session).get_spec("praxis-email-agent")

    allowed_tools = [tool.key for tool in spec.allowed_tools]
    allowed_skills = [skill.key for skill in spec.allowed_skills]

    assert spec.domain_key == "praxis"
    assert spec.model_profile == "openrouter:openai/gpt-5.6-luna"
    assert "gmail.message.list_recent" in allowed_tools
    assert "gmail.message.get" in allowed_tools
    assert "gmail.thread.get" in allowed_tools
    assert "gmail.message.modify" in allowed_tools
    assert "google.drive.file.get" in allowed_tools
    assert "google.drive.folder.list" in allowed_tools
    assert "google.drive.file.export" in allowed_tools
    assert "google.docs.get" in allowed_tools
    assert "google.slides.get" in allowed_tools
    assert "google.sheets.get" in allowed_tools
    assert "google.sheets.values.get" in allowed_tools
    assert "google.meet.conference_records.list" in allowed_tools
    assert "google.meet.conference_records.get" in allowed_tools
    assert "routed.item.create" in allowed_tools
    assert "workflow.notification.create" in allowed_tools
    assert "email_triage" in allowed_skills
    assert "contact_manager" in allowed_skills
    assert "to_do_manager" in allowed_skills
    client = _llm_client_for_model_profile(spec.model_profile)
    assert client.provider == "openrouter"
    assert client.model == "openai/gpt-5.6-luna"


def test_seed_refresh_migrates_praxis_email_agent_from_legacy_qwen_default(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.ensure_seed_agents()
    agent = AgentRepository(session).get_by_key("praxis-email-agent")
    assert agent is not None
    agent.capabilities = {
        **(agent.capabilities or {}),
        "model_profile": "ollama:qwen3:8b",
    }
    session.commit()

    registry.ensure_seed_agents()

    assert registry.get_spec("praxis-email-agent").model_profile == (
        "openrouter:openai/gpt-5.6-luna"
    )


def test_prompt_package_can_scope_required_skills(
    session: Session,
) -> None:
    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction="Review latest Praxis email for contacts and calendar events.",
            required_skills=["email_triage", "calendar_manager"],
            use_semantic=False,
        )
    )

    skill_keys = [skill.key for skill in package.skill_manifest]

    assert skill_keys == ["email_triage", "calendar_manager"]
    assert "Email Triage" in package.assembled_prompt
    assert "Calendar Manager" in package.assembled_prompt
    assert "Contact Manager" not in package.assembled_prompt


def test_deterministic_email_tool_plan_splits_latest_email_retrieval(
    session: Session,
) -> None:
    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction="Review the latest Praxis inbox email and summarize it.",
            required_skills=["email_triage"],
        )
    )

    first_step = _deterministic_tool_plan(
        package=package,
        prior_results=[],
        iteration=1,
    )

    assert first_step == [
        {
            "tool_key": "gmail.message.list_recent",
            "payload": {
                "limit": 1,
                "newer_than_days": 365,
                "unread_only": False,
            },
            "rationale": "Read recent Praxis Gmail message metadata before summarizing it.",
        }
    ]

    second_step = _deterministic_tool_plan(
        package=package,
        prior_results=[
            {
                "tool_name": "gmail.message.list_recent",
                "status": "complete",
                "output_payload": {
                    "messages": [
                        {
                            "message_id": "msg-123",
                            "thread_id": "thread-123",
                            "subject": "Praxis update",
                        }
                    ]
                },
            }
        ],
        iteration=2,
    )

    assert second_step == [
        {
            "tool_key": "gmail.message.get",
            "payload": {
                "message_id": "msg-123",
                "max_body_chars": 6000,
            },
            "rationale": "Read the selected Gmail message body for triage.",
        }
    ]


def test_deterministic_email_tool_plan_honors_latest_five_emails(
    session: Session,
) -> None:
    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction="Read the latest 5 emails in the Praxis inbox and summarize them.",
            required_skills=["email_triage"],
        )
    )

    first_step = _deterministic_tool_plan(
        package=package,
        prior_results=[],
        iteration=1,
    )

    assert first_step[0]["tool_key"] == "gmail.message.list_recent"
    assert first_step[0]["payload"]["limit"] == 5

    second_step = _deterministic_tool_plan(
        package=package,
        prior_results=[
            {
                "tool_name": "gmail.message.list_recent",
                "status": "complete",
                "output_payload": {
                    "messages": [
                        {"message_id": f"msg-{index}", "subject": f"Email {index}"}
                        for index in range(1, 6)
                    ]
                },
            }
        ],
        iteration=2,
    )

    assert [item["tool_key"] for item in second_step] == ["gmail.message.get"] * 5
    assert [item["payload"]["message_id"] for item in second_step] == [
        "msg-1",
        "msg-2",
        "msg-3",
        "msg-4",
        "msg-5",
    ]


def test_email_tool_plan_removes_placeholders_and_sequences_real_ids() -> None:
    first_step = _harden_email_tool_plan(
        [
            {
                "tool_key": "gmail.message.list_recent",
                "payload": {"count": 1},
                "rationale": "Find the latest message.",
            },
            {
                "tool_key": "gmail.message.get",
                "payload": {"message_id": "<latest_message_id>"},
                "rationale": "Read it.",
            },
            {
                "tool_key": "google.docs.get",
                "payload": {"file_id": "<linked_doc_id>"},
                "rationale": "Read linked notes.",
            },
            {
                "tool_key": "memory.context_bundle",
                "payload": {"query_text": "Praxis email context"},
                "rationale": "Ground the triage.",
            },
        ],
        [],
        task_instruction="Triage exactly the latest Praxis email.",
    )

    assert [item["tool_key"] for item in first_step] == [
        "gmail.message.list_recent",
        "memory.context_bundle",
    ]
    assert first_step[0]["payload"] == {"limit": 1}

    list_result = {
        "tool_name": "gmail.message.list_recent",
        "status": "complete",
        "output_payload": {
            "messages": [
                {
                    "message_id": "msg-real-1",
                    "thread_id": "thread-real-1",
                    "subject": "Partner notes",
                }
            ]
        },
    }
    second_step = _harden_email_tool_plan(
        [
            {
                "tool_key": "gmail.message.get",
                "payload": {"message_id": "<latest_message_id>"},
                "rationale": "Read it.",
            }
        ],
        [list_result],
        task_instruction="Triage exactly the latest Praxis email.",
    )

    assert second_step == [
        {
            "tool_key": "gmail.message.get",
            "payload": {"message_id": "msg-real-1", "max_body_chars": 6000},
            "rationale": "Read the selected Gmail message body before triage actions.",
        }
    ]

    message_result = {
        "tool_name": "gmail.message.get",
        "status": "complete",
        "output_payload": {
            "message_id": "msg-real-1",
            "body_text": "Meeting notes are linked below.",
            "google_workspace_links": [
                {
                    "kind": "document",
                    "file_id": "doc-real-1",
                    "url": "https://docs.google.com/document/d/doc-real-1/edit",
                }
            ],
        },
    }
    third_step = _harden_email_tool_plan(
        [
            {
                "tool_key": "google.docs.get",
                "payload": {"file_id": "<linked_doc_id>"},
                "rationale": "Read linked notes.",
            }
        ],
        [list_result, message_result],
        task_instruction="Triage exactly the latest Praxis email.",
    )

    assert third_step[0]["payload"] == {
        "file_id": "doc-real-1",
        "url": "https://docs.google.com/document/d/doc-real-1/edit",
    }


def test_email_tool_plan_reads_discovered_link_before_routing() -> None:
    message_result = {
        "tool_name": "gmail.message.get",
        "status": "complete",
        "output_payload": {
            "message_id": "msg-real-1",
            "thread_id": "thread-real-1",
            "body_text": "Review the linked notes before deciding what to route.",
            "google_workspace_links": [
                {
                    "kind": "document",
                    "file_id": "doc-real-1",
                    "url": "https://docs.google.com/document/d/doc-real-1/edit",
                }
            ],
        },
    }

    hardened = _harden_email_tool_plan(
        [
            {
                "tool_key": "routed.item.create",
                "payload": {"route_type": "contact", "title": "Premature candidate"},
                "rationale": "Route a candidate.",
            }
        ],
        [
            {
                "tool_name": "gmail.message.list_recent",
                "status": "complete",
                "output_payload": {"messages": [{"message_id": "msg-real-1"}]},
            },
            message_result,
        ],
        task_instruction="Inspect any linked Google document before completing email triage.",
        allowed_tool_keys={"google.docs.get", "routed.item.create"},
    )

    assert hardened == [
        {
            "tool_key": "google.docs.get",
            "payload": {
                "file_id": "doc-real-1",
                "url": "https://docs.google.com/document/d/doc-real-1/edit",
            },
            "rationale": "Read the linked Google Workspace artifact before email routing.",
        }
    ]


def test_email_tool_plan_lists_discovered_drive_folder_before_routing() -> None:
    message_result = {
        "tool_name": "gmail.message.get",
        "status": "complete",
        "output_payload": {
            "message_id": "msg-folder-1",
            "body_text": "Review the linked pitch resources.",
            "google_workspace_links": [
                {
                    "kind": "folder",
                    "file_id": "folder-123",
                    "url": "https://drive.google.com/drive/folders/folder-123?usp=sharing",
                }
            ],
        },
    }

    hardened = _harden_email_tool_plan(
        [
            {
                "tool_key": "routed.item.create",
                "payload": {"route_type": "event", "title": "Premature event"},
                "rationale": "Route a candidate.",
            }
        ],
        [message_result],
        task_instruction="Inspect linked Google Workspace context before completing email triage.",
        allowed_tool_keys={"google.drive.folder.list", "routed.item.create"},
    )

    assert hardened == [
        {
            "tool_key": "google.drive.folder.list",
            "payload": {
                "file_id": "folder-123",
                "url": "https://drive.google.com/drive/folders/folder-123?usp=sharing",
            },
            "rationale": "Read the linked Google Workspace artifact before email routing.",
        }
    ]


def test_deterministic_google_slides_plan_does_not_fall_back_to_email(
    session: Session,
) -> None:
    slides_url = "https://docs.google.com/presentation/d/deck-123/edit"
    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction=(
                "Verify access to this Google Slides file and report the title, file type, "
                f"and whether content is readable: {slides_url}"
            ),
            required_skills=["email_triage"],
        )
    )

    first_step = _deterministic_tool_plan(
        package=package,
        prior_results=[],
        iteration=1,
    )

    assert first_step == [
        {
            "tool_key": "google.slides.get",
            "payload": {"url": slides_url, "file_id": "deck-123"},
            "rationale": "Read the linked Google Slides deck enough to verify readability.",
        }
    ]

    second_step = _deterministic_tool_plan(
        package=package,
        prior_results=[
            {
                "tool_name": "google.slides.get",
                "status": "complete",
                "output_payload": {
                    "summary": {
                        "type": "google_slides",
                        "presentation_id": "deck-123",
                        "title": "Praxis deck",
                    }
                },
            }
        ],
        iteration=2,
    )

    assert second_step == []


def test_deterministic_google_folder_plan_lists_children(session: Session) -> None:
    folder_url = "https://drive.google.com/drive/folders/folder-123?usp=sharing"
    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction=f"Inspect the supporting files in this Drive folder: {folder_url}",
            required_skills=["email_triage"],
        )
    )

    first_step = _deterministic_tool_plan(
        package=package,
        prior_results=[],
        iteration=1,
    )

    assert first_step == [
        {
            "tool_key": "google.drive.folder.list",
            "payload": {"url": folder_url, "file_id": "folder-123"},
            "rationale": "List the linked Google Drive folder before selecting relevant files.",
        }
    ]


def test_email_tool_plan_normalizes_routes_and_rejects_another_chris_owner() -> None:
    prior_results = [
        {
            "tool_name": "gmail.message.list_recent",
            "status": "complete",
            "output_payload": {"messages": [{"message_id": "msg-1"}]},
        },
        {
            "tool_name": "gmail.message.get",
            "status": "complete",
            "output_payload": {
                "message_id": "msg-1",
                "to": "Chris Flournoy <chris.flournoy@praxis-defense.com>",
                "cc": "Chris Aliperti <chris.aliperti@praxis-defense.com>",
                "body_text": "Chris, please review the attached draft.",
            },
        },
    ]

    hardened = _harden_email_tool_plan(
        [
            {
                "tool_key": "routed.item.create",
                "payload": {"item_type": "contact", "name": "Caleb Holt"},
                "rationale": "Record the sender.",
            },
            {
                "tool_key": "routed.item.create",
                "payload": {
                    "item_type": "todo",
                    "owner": "Chris Flournoy",
                    "title": "Review draft",
                },
                "rationale": "Record the requested review.",
            },
            {
                "tool_key": "workflow.notification.create",
                "payload": {
                    "title": "Review requested",
                    "message": "Chris should review the draft.",
                },
                "rationale": "Notify Chris.",
            },
        ],
        prior_results,
        task_instruction="Triage exactly the latest Praxis email.",
    )

    assert hardened == [
        {
            "tool_key": "routed.item.create",
            "payload": {"name": "Caleb Holt", "route_type": "contact"},
            "rationale": "Record the sender.",
        }
    ]


def test_routed_item_create_tool_promotes_contact_candidate(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("praxis-email-agent")
    agent = AgentRepository(session).get_by_key("praxis-email-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.routed_item_create",
        objective="Route contact from latest Praxis email.",
        input_payload={},
    )
    session.add(task)
    session.commit()

    result = ToolExecutionService(
        session,
        adapters={"routed.item.create": RoutedItemCreateToolAdapter()},
    ).execute_for_task(
        ToolExecutionRequest(
            agent_key="praxis-email-agent",
            tool_key="routed.item.create",
            payload={
                "route_type": "contact",
                "title": "Jane Smith",
                "content": "Jane Smith at Example Corp asked about Praxis training.",
                "metadata": {
                    "name": "Jane Smith",
                    "email": "jane@example.com",
                    "organization": "Example Corp",
                },
                "message_id": "msg-1",
                "thread_id": "thread-1",
                "subject": "Praxis training",
                "from": "Jane Smith <jane@example.com>",
            },
        ),
        task=task,
    )

    assert result.status == "complete"
    assert result.output is not None
    assert result.output["created_count"] == 1
    assert result.output["promoted_count"] == 1
    contact = session.query(Contact).filter_by(email="jane@example.com").one()
    assert contact.name == "Jane Smith"
    assert contact.source_refs[0]["message_id"] == "msg-1"


def test_routed_item_create_accepts_model_aliases_and_nested_source(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("praxis-email-agent")
    agent = AgentRepository(session).get_by_key("praxis-email-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.routed_item_alias",
        objective="Route a contact from email.",
        input_payload={},
    )
    session.add(task)
    session.commit()

    result = ToolExecutionService(
        session,
        adapters={"routed.item.create": RoutedItemCreateToolAdapter()},
    ).execute_for_task(
        ToolExecutionRequest(
            agent_key="praxis-email-agent",
            tool_key="routed.item.create",
            payload={
                "item_type": "contact",
                "name": "Caleb Smotherman",
                "email": "caleb@example.com",
                "phone": "813-555-0100",
                "notes": "Sent the Praxis radio-trainer draft for review.",
                "source": {
                    "type": "gmail_message",
                    "message_id": "msg-caleb-1",
                    "thread_id": "thread-caleb-1",
                },
            },
        ),
        task=task,
    )

    assert result.status == "complete"
    contact = session.query(Contact).filter_by(email="caleb@example.com").one()
    assert contact.name == "Caleb Smotherman"
    assert contact.phone == "813-555-0100"
    assert contact.source_refs == [
        {
            "type": "gmail_message",
            "message_id": "msg-caleb-1",
            "thread_id": "thread-caleb-1",
        }
    ]


def test_email_attention_notification_is_delivered_once_with_provenance(
    session: Session,
) -> None:
    registry = AgentRegistryService(session)
    registry.get_spec("praxis-email-agent")
    agent = AgentRepository(session).get_by_key("praxis-email-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    parent = Task(
        domain_id=domain.id,
        status="running",
        priority="normal",
        source_type="maestro",
        workflow_key="maestro.generic",
        objective="Triage the latest Praxis email.",
        input_payload={},
    )
    session.add(parent)
    session.flush()
    task = Task(
        parent_task_id=parent.id,
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="scheduler",
        workflow_key="agent.execute",
        objective="Triage one Praxis email and notify Chris if action is required.",
        input_payload={},
    )
    run = WorkflowRun(
        parent_task_id=parent.id,
        domain_id=domain.id,
        source_type="manual",
        status="running",
        priority="normal",
        input_payload={"summary": "Triage one Praxis email."},
    )
    session.add_all([task, run])
    session.commit()
    service = ToolExecutionService(
        session,
        adapters={"workflow.notification.create": WorkflowNotificationCreateToolAdapter()},
    )
    request = ToolExecutionRequest(
        agent_key="praxis-email-agent",
        tool_key="workflow.notification.create",
        payload={
            "title": "Praxis email needs your response",
            "content": "Confirm Atlas partner-call availability by July 21.",
            "priority": "normal",
            "reason": "The sender requested a decision by a specific deadline.",
            "metadata": {
                "source_message_id": "msg-atlas-1",
                "source_thread_id": "thread-atlas-1",
                "subject": "Maestro triage test - Atlas partner sync",
                "sender": "Jordan Lee <jordan@example.com>",
            },
        },
    )

    first = service.execute_for_task(request, task=task)
    second = service.execute_for_task(request, task=task)

    assert first.status == "complete"
    assert first.output is not None
    assert first.output["duplicate"] is False
    assert second.status == "complete"
    assert second.output is not None
    assert second.output["duplicate"] is True
    notification = session.query(WorkflowNotification).one()
    assert notification.workflow_run_id == run.id
    assert notification.domain_id == domain.id
    assert notification.notification_type == "email_attention"
    assert notification.status == "delivered"
    assert notification.severity == "info"
    assert notification.metadata_["source_message_id"] == "msg-atlas-1"
    channel_messages = [
        message
        for message in session.query(Message).all()
        if (message.metadata_ or {}).get("event_type") == "email_attention"
    ]
    assert len(channel_messages) == 1
    assert "Confirm Atlas" in channel_messages[0].content
    assert channel_messages[0].metadata_["channel_visibility"] == "global"


def test_research_agents_are_granted_web_search_permission(session: Session) -> None:
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("maestro-development")
    assert domain is not None
    AgentRepository(session).create(
        domain_id=domain.id,
        key="maestro-sota-researcher",
        name="Maestro SOTA Researcher",
        agent_type="domain_agent",
        description="Researches current state-of-the-art tools and implementation patterns.",
        capabilities={"role_summary": "SOTA research agent", "memory_profile": "agent_prompt"},
        tool_permissions={"memory.context_bundle": {"permission": "read"}},
    )

    spec = AgentRegistryService(session).get_spec("maestro-sota-researcher")

    assert "web.search" in [tool.key for tool in spec.allowed_tools]


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


def test_github_adapter_accepts_owner_plus_repo_name_for_issue_create(
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
        config={},
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
    monkeypatch.setattr("app.tools.runtime.shutil.which", lambda name: "/usr/bin/gh")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return CompletedProcess(args=args, returncode=0, stdout="https://github.com/Praxis-Defense/GroundTruth/issues/99\n")

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
            "owner": "Praxis-Defense",
            "repo": "GroundTruth",
            "title": "Tool-generated issue",
            "body": "Created by a test.",
        },
    )

    assert output["url"] == "https://github.com/Praxis-Defense/GroundTruth/issues/99"
    assert "--repo" in captured["args"]
    assert captured["args"][captured["args"].index("--repo") + 1] == "Praxis-Defense/GroundTruth"


def test_web_search_adapter_uses_openrouter_server_tool_and_returns_citations(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    spec = registry.get_spec("maestro-introspection-agent")
    agent = AgentRepository(session).get_by_key(spec.key)
    domain = DomainRepository(session).get_by_key("maestro-development")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.web",
        objective="Search for current CAD AI STL generation tools.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr("app.tools.runtime.OpenAILLMClient", FakeWebSearchLLMClient)

    output = WebSearchToolAdapter().execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=None,
        ),
        {
            "query": "current CAD AI STL generation tools",
            "instructions": "Research current CAD AI tool options.",
            "engine": "exa",
            "max_results": 3,
            "allowed_domains": ["freecad.org"],
        },
    )

    assert output["summary"]["type"] == "web_search_response"
    assert output["citations"][0]["url"] == "https://www.freecad.org/"
    assert output["usage"]["server_tool_use"]["web_search_requests"] == 1


def test_gmail_adapter_searches_and_decodes_message_metadata(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="praxis",
        tool_key="gmail",
        display_name="Praxis Gmail",
        auth_type="oauth",
        config={
            "user_id": "me",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": "GOOGLE_CLIENT_SECRET",
            "refresh_token_env": "PRAXIS_GMAIL_REFRESH_TOKEN",
        },
    )
    registry.get_spec("praxis-planning-agent")
    agent = AgentRepository(session).get_by_key("praxis-planning-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.gmail",
        objective="Search Gmail.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    connection = session.query(ToolConnection).filter_by(tool_key="gmail").one()
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PRAXIS_GMAIL_REFRESH_TOKEN", "refresh-token")
    calls: list[dict[str, object]] = []

    def fake_refresh_token(*, client_id, client_secret, refresh_token):
        assert client_id == "client-id"
        assert client_secret == "client-secret"
        assert refresh_token == "refresh-token"
        return {"access_token": "gmail-token", "expires_in": 3600, "token_type": "Bearer"}

    def fake_gmail_api(method, path, *, token, params=None, body=None, timeout=60):
        calls.append({"method": method, "path": path, "token": token, "params": params, "body": body})
        if path.endswith("/messages") and method == "GET":
            return {"messages": [{"id": "msg-1", "threadId": "thread-1"}], "resultSizeEstimate": 1}
        if path.endswith("/messages/msg-1"):
            return {
                "id": "msg-1",
                "threadId": "thread-1",
                "labelIds": ["UNREAD", "INBOX"],
                "snippet": "Partner update",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Partner update"},
                        {"name": "From", "value": "Jane <jane@example.com>"},
                        {"name": "To", "value": "Chris <chris@example.com>"},
                        {"name": "Date", "value": "Thu, 2 Jul 2026 09:00:00 -0400"},
                    ]
                },
            }
        raise AssertionError(f"Unexpected Gmail API call: {method} {path}")

    monkeypatch.setattr("app.tools.runtime._google_oauth_refresh_access_token", fake_refresh_token)
    monkeypatch.setattr("app.tools.runtime._gmail_api_json", fake_gmail_api)

    output = GmailApiToolAdapter("gmail.message.search").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"query": "from:jane newer_than:7d", "limit": 5},
    )

    assert output["summary"]["type"] == "gmail_message_list"
    assert output["messages"][0]["message_id"] == "msg-1"
    assert output["messages"][0]["subject"] == "Partner update"
    assert calls[0]["token"] == "gmail-token"
    assert calls[0]["params"]["q"] == "from:jane newer_than:7d"


def test_gmail_adapter_gets_full_message_body(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="praxis",
        tool_key="gmail",
        display_name="Praxis Gmail",
        auth_type="oauth",
        config={"access_token": "inline-token"},
    )
    registry.get_spec("praxis-planning-agent")
    agent = AgentRepository(session).get_by_key("praxis-planning-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.gmail",
        objective="Read Gmail.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    connection = session.query(ToolConnection).filter_by(tool_key="gmail").one()
    encoded_body = base64.urlsafe_b64encode(b"Full body text for Maestro.").decode("ascii").rstrip("=")

    def fake_gmail_api(method, path, *, token, params=None, body=None, timeout=60):
        assert token == "inline-token"
        assert params == {"format": "full"}
        return {
            "id": "msg-2",
            "threadId": "thread-2",
            "payload": {
                "mimeType": "multipart/mixed",
                "body": {},
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encoded_body}},
                    {
                        "mimeType": "application/vnd.ms-powerpoint",
                        "filename": "radio-trainer.ppt",
                        "body": {"attachmentId": "attachment-1", "size": 2048},
                    },
                ],
                "headers": [
                    {"name": "Subject", "value": "Full update"},
                    {"name": "From", "value": "partner@example.com"},
                ],
            },
        }

    monkeypatch.setattr("app.tools.runtime._gmail_api_json", fake_gmail_api)

    output = GmailApiToolAdapter("gmail.message.get").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"message_id": "msg-2"},
    )

    assert output["message_id"] == "msg-2"
    assert output["body_text"] == "Full body text for Maestro."
    assert output["attachments"] == [
        {
            "filename": "radio-trainer.ppt",
            "mime_type": "application/vnd.ms-powerpoint",
            "attachment_id": "attachment-1",
            "size": 2048,
        }
    ]
    assert output["summary"]["subject"] == "Full update"


def test_gmail_adapter_preserves_google_doc_meeting_notes_links(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="praxis",
        tool_key="gmail",
        display_name="Praxis Gmail",
        auth_type="oauth",
        config={"access_token": "inline-token"},
    )
    registry.get_spec("praxis-email-agent")
    agent = AgentRepository(session).get_by_key("praxis-email-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.gmail",
        objective="Read Gmail meeting notes link.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    connection = session.query(ToolConnection).filter_by(tool_key="gmail").one()
    doc_url = "https://docs.google.com/document/d/doc-123/edit"
    folder_url = "https://drive.google.com/drive/folders/folder-123?usp=sharing"
    html_body = (
        f'<p>Here are the <a href="{doc_url}">Meeting notes</a> and '
        f'<a href="{folder_url}">supporting files</a> from today.</p>'
    )
    encoded_body = base64.urlsafe_b64encode(html_body.encode("utf-8")).decode("ascii").rstrip("=")

    def fake_gmail_api(method, path, *, token, params=None, body=None, timeout=60):
        return {
            "id": "msg-meeting",
            "threadId": "thread-meeting",
            "payload": {
                "mimeType": "text/html",
                "body": {"data": encoded_body},
                "headers": [
                    {"name": "Subject", "value": "Meeting summary"},
                    {"name": "From", "value": "partner@example.com"},
                ],
            },
        }

    monkeypatch.setattr("app.tools.runtime._gmail_api_json", fake_gmail_api)

    output = GmailApiToolAdapter("gmail.message.get").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {"message_id": "msg-meeting"},
    )

    assert doc_url in output["body_text"]
    assert output["google_workspace_links"][0]["file_id"] == "doc-123"
    assert output["google_workspace_links"][1]["file_id"] == "folder-123"
    assert output["google_workspace_links"][1]["kind"] == "folder"
    assert output["meeting_notes"][0]["url"] == doc_url


def test_gmail_tools_inherit_google_connection(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    google_connection = registry.upsert_tool_connection(
        domain_key="praxis",
        tool_key="google",
        display_name="Praxis Google Workspace",
        auth_type="oauth",
        config={"access_token": "google-token"},
    )
    registry.get_spec("praxis-email-agent")
    agent = AgentRepository(session).get_by_key("praxis-email-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.gmail.google_connection",
        objective="Read Gmail through the Google family connection.",
        input_payload={},
    )
    session.add(task)
    session.commit()

    def fake_gmail_api(method, path, *, token, params=None, body=None, timeout=60):
        assert token == "google-token"
        return {
            "id": "msg-google-family",
            "threadId": "thread-google-family",
            "payload": {
                "mimeType": "text/plain",
                "body": {
                    "data": base64.urlsafe_b64encode(b"Google family Gmail body.")
                    .decode("ascii")
                    .rstrip("=")
                },
                "headers": [
                    {"name": "Subject", "value": "Family connection"},
                ],
            },
        }

    monkeypatch.setattr("app.tools.runtime._gmail_api_json", fake_gmail_api)

    result = ToolExecutionService(session).execute_for_task(
        ToolExecutionRequest(
            agent_key="praxis-email-agent",
            tool_key="gmail.message.get",
            payload={"message_id": "msg-google-family"},
        ),
        task=task,
    )

    assert result.status == "complete"
    assert result.connection_id == str(google_connection.id)
    assert result.output is not None
    assert result.output["body_text"] == "Google family Gmail body."


def test_google_workspace_tools_read_drive_and_docs(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="praxis",
        tool_key="google",
        display_name="Praxis Google Workspace",
        auth_type="oauth",
        config={"access_token": "google-token"},
    )
    registry.get_spec("praxis-email-agent")
    agent = AgentRepository(session).get_by_key("praxis-email-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.google",
        objective="Read Google meeting notes.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    connection = session.query(ToolConnection).filter_by(tool_key="google").one()

    def fake_google_json(method, base_url, path, *, token, params=None, body=None, timeout=60):
        assert token == "google-token"
        if path == "/drive/v3/files":
            assert params["supportsAllDrives"] is True
            assert params["includeItemsFromAllDrives"] is True
            assert "folder-123" in params["q"]
            return {
                "files": [
                    {
                        "id": "deck-123",
                        "name": "Pitch Deck",
                        "mimeType": "application/vnd.google-apps.presentation",
                    }
                ]
            }
        if path.startswith("/drive/v3/files/"):
            assert params["supportsAllDrives"] is True
            return {
                "id": "doc-123",
                "name": "Meeting Notes",
                "mimeType": "application/vnd.google-apps.document",
                "webViewLink": "https://docs.google.com/document/d/doc-123/edit",
            }
        if path.startswith("/v1/documents/"):
            return {
                "title": "Meeting Notes",
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {"textRun": {"content": "Decision: move forward.\n"}}
                                ]
                            }
                        }
                    ]
                },
            }
        if path.startswith("/v1/presentations/"):
            return {
                "title": "Pitch Deck",
                "slides": [
                    {
                        "pageElements": [
                            {
                                "shape": {
                                    "text": {
                                        "textElements": [
                                            {"textRun": {"content": "Problem statement"}},
                                            {"textRun": {"content": "Solution overview"}},
                                        ]
                                    }
                                }
                            }
                        ]
                    }
                ],
            }
        if path.startswith("/v4/spreadsheets/sheet-123/values/"):
            return {
                "range": "Sheet1!A1:B2",
                "majorDimension": "ROWS",
                "values": [["Name", "Status"], ["Praxis", "Active"]],
            }
        if path.startswith("/v4/spreadsheets/"):
            return {
                "spreadsheetId": "sheet-123",
                "properties": {"title": "Praxis Tracker"},
                "sheets": [
                    {"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}},
                ],
            }
        if path == "/v2/conferenceRecords":
            return {
                "conferenceRecords": [
                    {
                        "name": "conferenceRecords/meet-123",
                        "startTime": "2026-07-15T13:00:00Z",
                    }
                ]
            }
        if path.startswith("/v2/conferenceRecords/"):
            return {
                "name": "conferenceRecords/meet-123",
                "startTime": "2026-07-15T13:00:00Z",
                "endTime": "2026-07-15T13:30:00Z",
            }
        raise AssertionError(f"Unexpected Google JSON call: {base_url}{path}")

    def fake_google_text(method, base_url, path, *, token, params=None, body=None, timeout=60):
        assert token == "google-token"
        assert params == {"mimeType": "text/plain"}
        return "Meeting notes export text."

    monkeypatch.setattr("app.tools.runtime._google_api_json", fake_google_json)
    monkeypatch.setattr("app.tools.runtime._google_api_text", fake_google_text)

    context = ToolExecutionContext(
        session=session,
        agent=agent,
        domain=domain,
        task=task,
        connection=connection,
    )
    drive = GoogleWorkspaceToolAdapter("google.drive.file.get").execute(
        context,
        {"url": "https://docs.google.com/document/d/doc-123/edit"},
    )
    folder = GoogleWorkspaceToolAdapter("google.drive.folder.list").execute(
        context,
        {"url": "https://drive.google.com/drive/folders/folder-123"},
    )
    exported = GoogleWorkspaceToolAdapter("google.drive.file.export").execute(
        context,
        {"file_id": "doc-123"},
    )
    doc = GoogleWorkspaceToolAdapter("google.docs.get").execute(
        context,
        {"document_id": "doc-123"},
    )
    slides = GoogleWorkspaceToolAdapter("google.slides.get").execute(
        context,
        {"presentation_id": "deck-123"},
    )
    sheets = GoogleWorkspaceToolAdapter("google.sheets.get").execute(
        context,
        {"spreadsheet_id": "sheet-123"},
    )
    sheet_values = GoogleWorkspaceToolAdapter("google.sheets.values.get").execute(
        context,
        {"spreadsheet_id": "sheet-123", "range": "Sheet1!A1:B2"},
    )
    meet_records = GoogleWorkspaceToolAdapter("google.meet.conference_records.list").execute(
        context,
        {"page_size": 5},
    )
    meet_record = GoogleWorkspaceToolAdapter("google.meet.conference_records.get").execute(
        context,
        {"conference_record_id": "meet-123"},
    )

    assert drive["summary"]["file_id"] == "doc-123"
    assert folder["summary"]["file_count"] == 1
    assert folder["files"][0]["id"] == "deck-123"
    assert exported["content_text"] == "Meeting notes export text."
    assert doc["title"] == "Meeting Notes"
    assert "Decision: move forward." in doc["content_text"]
    assert slides["title"] == "Pitch Deck"
    assert "Problem statement" in slides["content_text"]
    assert sheets["title"] == "Praxis Tracker"
    assert sheet_values["values"][1] == ["Praxis", "Active"]
    assert meet_records["summary"]["record_count"] == 1
    assert meet_record["summary"]["name"] == "conferenceRecords/meet-123"

    def missing_drive_item(*args, **kwargs):
        raise ToolExecutionError(
            'Google API request failed (404): {"error":{"message":"File not found: folder-123."}}'
        )

    monkeypatch.setattr("app.tools.runtime._google_api_json", missing_drive_item)
    with pytest.raises(ToolExecutionError, match=r"drive\.file.*arbitrary"):
        GoogleWorkspaceToolAdapter("google.drive.folder.list").execute(
            context,
            {"file_id": "folder-123"},
        )


def test_gmail_draft_create_builds_encoded_rfc822_message(
    session: Session,
    monkeypatch,
) -> None:
    registry = AgentRegistryService(session)
    registry.upsert_tool_connection(
        domain_key="praxis",
        tool_key="gmail",
        display_name="Praxis Gmail",
        auth_type="oauth",
        config={"access_token": "inline-token", "send_as": "praxis@example.com"},
    )
    registry.get_spec("praxis-planning-agent")
    agent = AgentRepository(session).get_by_key("praxis-planning-agent")
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    agent.tool_permissions = {
        **(agent.tool_permissions or {}),
        "gmail.draft.create": {"permission": "use", "description": "Create drafts when approved."},
    }
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.gmail",
        objective="Draft Gmail.",
        input_payload={},
    )
    session.add(task)
    session.commit()
    connection = session.query(ToolConnection).filter_by(tool_key="gmail").one()
    captured: dict[str, object] = {}

    def fake_gmail_api(method, path, *, token, params=None, body=None, timeout=60):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "draft-1", "message": {"id": "msg-3", "threadId": "thread-3"}}

    monkeypatch.setattr("app.tools.runtime._gmail_api_json", fake_gmail_api)

    output = GmailApiToolAdapter("gmail.draft.create").execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
        ),
        {
            "to": ["jane@example.com"],
            "subject": "Partner follow-up",
            "body": "Thanks Jane. Here are next steps.",
            "thread_id": "thread-3",
        },
    )

    raw = captured["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    assert output["draft_id"] == "draft-1"
    assert "From: praxis@example.com" in decoded
    assert "To: jane@example.com" in decoded
    assert "Subject: Partner follow-up" in decoded
    assert "Thanks Jane. Here are next steps." in decoded


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
    (tmp_path / ".git").mkdir()
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
        if args[:3] == ["git", "status", "--porcelain=v1"]:
            stdout = ""
        elif args[:3] == ["git", "branch", "--show-current"]:
            stdout = "main\n"
        elif args[:2] == ["git", "rev-parse"]:
            stdout = "abc123\n"
        else:
            stdout = "ok\n"
        return CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

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
    assert calls[-2:] == [["git", "pull", "--ff-only"], ["npm", "run", "build"]]


def test_local_app_reload_blocks_dirty_runtime(
    session: Session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / ".git").mkdir()
    registry = AgentRegistryService(session)
    registry.get_spec("maestro-introspection-agent")
    registry.upsert_tool_connection(
        domain_key="maestro-development",
        tool_key="local.app.reload",
        display_name="Maestro Local Runtime",
        auth_type="local",
        config={"default_cwd": str(tmp_path), "allowed_roots": [str(tmp_path)], "branch": "main"},
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
        stdout = " M app/api/main.py\n" if args[:3] == ["git", "status", "--porcelain=v1"] else "main\n"
        return CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.tools.runtime.subprocess.run", fake_run)

    with pytest.raises(ToolExecutionError, match="uncommitted changes"):
        LocalAppReloadAdapter("local.app.reload").execute(
            ToolExecutionContext(
                session=session,
                agent=agent,
                domain=domain,
                task=task,
                connection=connection,
            ),
            {"pull_latest": True},
        )

    assert ["git", "pull", "--ff-only"] not in calls


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
    assert result.scheduler["status"] == "manual_run"
    assert result.prompt_package.agent.key == "praxis-planning-agent"
    assert result.staged_artifact_path is not None
    assert Path(result.staged_artifact_path).is_file()


def test_single_email_triage_routes_notifies_reports_and_stages_memory_artifact(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    get_settings().memory_dropbox_root = str(tmp_path)
    _seed_memory(session)
    result = PromptAggregationService(
        session,
        llm_client=FakeSingleEmailTriageLLMClient(),
        tool_adapters={
            "gmail.message.list_recent": FakeSingleEmailGmailAdapter(
                "gmail.message.list_recent"
            ),
            "gmail.message.get": FakeSingleEmailGmailAdapter("gmail.message.get"),
            "routed.item.create": RoutedItemCreateToolAdapter(),
            "workflow.notification.create": WorkflowNotificationCreateToolAdapter(),
        },
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction=(
                "Run a one-time triage over exactly the latest Praxis inbox email. Route durable "
                "objects and notify Chris only if he must act."
            ),
            required_skills=["email_triage", "contact_manager"],
            use_semantic=False,
        ),
        stage_interaction=True,
        execute_llm=True,
        auto_tool_loop=True,
        max_tool_iterations=4,
    )

    assert result.status == "completed"
    assert result.report_id is not None
    assert result.artifact_id is not None
    assert result.staged_artifact_path is not None
    assert Path(result.staged_artifact_path).is_file()
    tool_names = [call["tool_name"] for call in result.tool_calls]
    assert "gmail.message.list_recent" in tool_names
    assert "gmail.message.get" in tool_names
    assert "routed.item.create" in tool_names
    assert "workflow.notification.create" in tool_names
    contact = session.query(Contact).filter_by(email="jordan@example.com").one()
    assert contact.name == "Jordan Lee"
    assert contact.source_refs[0]["message_id"] == "msg-atlas-1"
    notification = session.query(WorkflowNotification).one()
    assert notification.notification_type == "email_attention"
    assert notification.metadata_["source_message_id"] == "msg-atlas-1"
    report = session.get(Report, uuid.UUID(result.report_id))
    assert report is not None
    assert "response_needed" in report.body_markdown
    staged_artifact = session.get(Artifact, uuid.UUID(result.artifact_id))
    assert staged_artifact is not None
    assert staged_artifact.uri == result.staged_artifact_path


def test_email_triage_reserves_operational_finalization_after_evidence_budget(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    get_settings().memory_dropbox_root = str(tmp_path)
    _seed_memory(session)
    llm_client = FakeEmailTriageFinalizationLLMClient()
    result = PromptAggregationService(
        session,
        llm_client=llm_client,
        tool_adapters={
            key: FakeEmailTriageEvidenceAdapter(key)
            for key in (
                "gmail.message.list_recent",
                "gmail.message.get",
                "gmail.thread.get",
                "google.drive.file.get",
            )
        }
        | {
            "routed.item.create": RoutedItemCreateToolAdapter(),
            "workflow.notification.create": WorkflowNotificationCreateToolAdapter(),
        },
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction=(
                "Run a one-time triage over exactly the latest Praxis inbox email, inspect linked "
                "Google Workspace context, route durable objects, and notify Chris if he must act."
            ),
            required_skills=["email_triage", "contact_manager"],
            use_semantic=False,
        ),
        stage_interaction=True,
        execute_llm=True,
        auto_tool_loop=True,
        max_tool_iterations=4,
    )

    assert result.status == "completed"
    assert llm_client.structured_calls == 5
    assert result.tool_loop["iterations"][-1]["phase"] == "email_operational_finalization"
    tool_names = [call["tool_name"] for call in result.tool_calls]
    assert "google.drive.file.get" in tool_names
    assert "llm.email_triage_finalizer" in tool_names
    assert "routed.item.create" in tool_names
    assert "workflow.notification.create" in tool_names
    assert session.query(Contact).filter_by(email="jordan@example.com").one().name == "Jordan Lee"
    notification = session.query(WorkflowNotification).one()
    assert notification.title == "Atlas email needs your response"
    assert notification.status == "delivered"
    assert result.artifact_id is not None


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


def test_run_agent_once_auto_executes_aggregate_github_read_without_approval(
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

    class FakeGitHubReadAdapter:
        key = "github.read"

        def execute(self, context: ToolExecutionContext, payload: dict) -> dict:
            assert payload["request"] == "Inspect repository architecture only."
            return {"summary": "Read-only repository context returned."}

    result = PromptAggregationService(
        session,
        llm_client=FakeAutoGitHubReadToolLoopLLMClient(),
        tool_adapters={"github.read": FakeGitHubReadAdapter()},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction="Inspect the repo architecture for a planning task.",
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
    )

    assert result.status == "completed"
    github_read = next(call for call in result.tool_calls if call["tool_name"] == "github.read")
    assert github_read["status"] == "complete"
    assert not result.tool_loop["iterations"][0]["blocked"]
    assert "Repository architecture inspected" in (result.output_text or "")


def test_agent_tool_loop_prefers_llm_plan_over_deterministic_google_dispatch(
    session: Session,
) -> None:
    class FakeDriveMetadataAdapter:
        key = "google.drive.file.get"

        def execute(self, context: ToolExecutionContext, payload: dict) -> dict:
            assert payload["file_id"] == "deck-123"
            return {
                "summary": {
                    "type": "google_drive_file",
                    "file_id": "deck-123",
                    "name": "LLM selected deck metadata",
                }
            }

    result = PromptAggregationService(
        session,
        llm_client=FakeGoogleSlidesLLMPlannerClient(),
        tool_adapters={"google.drive.file.get": FakeDriveMetadataAdapter()},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction=(
                "Verify access to this Google Slides file: "
                "https://docs.google.com/presentation/d/deck-123/edit"
            ),
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
    )

    assert result.status == "completed"
    assert result.tool_loop["iterations"][0]["planner_source"] == "llm_planner"
    assert result.tool_loop["iterations"][0]["requested_tools"][0]["tool_key"] == "google.drive.file.get"
    assert any(call["tool_name"] == "google.drive.file.get" for call in result.tool_calls)
    assert not any(call["tool_name"] == "google.slides.get" for call in result.tool_calls)


def test_agent_tool_loop_uses_deterministic_fallback_for_obvious_google_link(
    session: Session,
) -> None:
    class FakeSlidesAdapter:
        key = "google.slides.get"

        def execute(self, context: ToolExecutionContext, payload: dict) -> dict:
            assert payload["file_id"] == "deck-123"
            return {
                "title": "Fallback deck",
                "content_text": "Slide 1: fallback readable text",
                "summary": {
                    "type": "google_slides",
                    "presentation_id": "deck-123",
                    "title": "Fallback deck",
                    "slide_count": 1,
                },
            }

    result = PromptAggregationService(
        session,
        llm_client=FakeGoogleSlidesFallbackLLMClient(),
        tool_adapters={"google.slides.get": FakeSlidesAdapter()},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="praxis-email-agent",
            task_instruction=(
                "Verify access to this Google Slides file: "
                "https://docs.google.com/presentation/d/deck-123/edit"
            ),
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
    )

    assert result.status == "completed"
    assert result.tool_loop["iterations"][0]["planner_source"] == "deterministic_fallback"
    assert result.tool_loop["iterations"][0]["requested_tools"][0]["tool_key"] == "google.slides.get"
    planner_call = next(call for call in result.tool_calls if call["tool_name"] == "llm.tool_planner")
    assert planner_call["output_payload"]["planner_source"] == "deterministic_fallback"
    assert any(call["tool_name"] == "google.slides.get" for call in result.tool_calls)
    assert "Slides deck access verified" in (result.output_text or "")


def test_default_tools_include_safe_aggregate_github_read() -> None:
    adapters = default_tool_adapters()

    assert "github.read" in adapters
    assert adapters["github.read"].key == "github.read"
    assert "workflow.notification.create" in adapters
    assert "google.drive.folder.list" in adapters


def test_report_retrieval_tools_search_and_get_domain_reports(session: Session) -> None:
    registry = AgentRegistryService(session)
    spec = registry.get_spec("praxis-planning-agent")
    agent = AgentRepository(session).get_by_key(spec.key)
    domain = DomainRepository(session).get_by_key("praxis")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.reports",
        objective="Find partner report context.",
        input_payload={},
    )
    session.add(task)
    session.flush()
    report = Report(
        task_id=task.id,
        domain_id=domain.id,
        agent_id=agent.id,
        title="Partner Follow-up Report",
        report_type="workflow_report",
        summary="Partner follow-up summary.",
        body_markdown="## Partner Follow-up\nPraxis partner details and next steps.",
        structured_data={},
    )
    session.add(report)
    session.commit()

    service = ToolExecutionService(session)
    search = service.execute_for_task(
        ToolExecutionRequest(
            agent_key=agent.key,
            tool_key="reports.search",
            payload={"query_text": "partner"},
        ),
        task=task,
    )
    get = service.execute_for_task(
        ToolExecutionRequest(
            agent_key=agent.key,
            tool_key="reports.get",
            payload={"report_id": str(report.id)},
        ),
        task=task,
    )

    assert search.status == "complete"
    assert search.output is not None
    assert search.output["reports"][0]["id"] == str(report.id)
    assert "body_preview" in search.output["reports"][0]
    assert get.status == "complete"
    assert get.output is not None
    assert get.output["report"]["body_markdown"].startswith("## Partner")


def test_github_read_search_terms_extract_architecture_hints() -> None:
    terms = _github_read_search_terms(
        "Inspect how Maestro's tool registry, credential handling, and workflow scheduler work."
    )

    assert "tool registry" in terms
    assert "credential" in terms
    assert "workflow" in terms
    assert len(terms) <= 8


def test_llm_gateway_accepts_task_payload_alias(session: Session) -> None:
    AgentRegistryService(session).create_agent_spec(
        domain_key="maestro-development",
        key="Maestro LLM Gateway Tester",
        name="Maestro LLM Gateway Tester",
        role_summary="Tests internal LLM gateway payload handling.",
        tool_permissions={"llm.gateway": {"permission": "use"}},
    )
    agent = AgentRepository(session).get_by_key("maestro-llm-gateway-tester")
    domain = DomainRepository(session).get_by_key("maestro-development")
    assert agent is not None
    assert domain is not None
    task = Task(
        domain_id=domain.id,
        assigned_agent_id=agent.id,
        status="running",
        priority="normal",
        source_type="test",
        workflow_key="test.llm_gateway",
        objective="Synthesize a test plan.",
        input_payload={},
    )
    session.add(task)
    session.commit()

    output = LLMGatewayToolAdapter().execute(
        ToolExecutionContext(
            session=session,
            agent=agent,
            domain=domain,
            task=task,
            connection=None,
            dry_run=True,
        ),
        {"task": "Synthesize a voice input feature plan.", "context": "Use Maestro constraints."},
    )

    assert output["dry_run"] is True
    assert output["prompt_preview"] == "Synthesize a voice input feature plan."
    assert output["context_chars"] > 0


def test_tool_results_are_compacted_before_prompt_reuse() -> None:
    raw_result = {
        "id": "tool-call-1",
        "tool_name": "memory.context_bundle",
        "status": "complete",
        "output_payload": {
            "summary": {"type": "memory_context_bundle", "included_count": 8},
            "rendered_text": "Important memory. " * 1000,
        },
    }

    compact = _compact_tool_results_for_prompt([raw_result], max_total_chars=1800)

    assert compact[0]["id"] == "tool-call-1"
    assert compact[0]["summary"]["included_count"] == 8
    assert compact[0]["raw_output_chars"] > 10000
    assert len(json.dumps(compact, default=str)) < 2200
    assert compact[0]["full_output"] == "stored_in_tool_call_output_payload"


def test_compacted_tool_results_preserve_email_and_google_document_text() -> None:
    email_marker = "ACTION: Chris must confirm the partner meeting by Tuesday."
    doc_marker = "DECISION: Pilot scope remains limited to the current unit."
    compact = _compact_tool_results_for_prompt(
        [
            {
                "id": "gmail-call",
                "tool_name": "gmail.message.get",
                "status": "complete",
                "output_payload": {
                    "summary": {"type": "gmail_message", "message_id": "msg-1"},
                    "body_text": f"Email introduction. {email_marker}",
                    "google_workspace_links": [
                        {
                            "kind": "document",
                            "file_id": "doc-1",
                            "url": "https://docs.google.com/document/d/doc-1/edit",
                        }
                    ],
                    "attachments": [
                        {
                            "filename": "radio-trainer.pptx",
                            "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        }
                    ],
                },
            },
            {
                "id": "docs-call",
                "tool_name": "google.docs.get",
                "status": "complete",
                "output_payload": {
                    "summary": {"type": "google_doc", "document_id": "doc-1"},
                    "content_text": f"Meeting notes. {doc_marker}",
                },
            },
        ]
    )

    rendered = json.dumps(compact)
    assert email_marker in rendered
    assert doc_marker in rendered
    assert "body_text" in compact[0]["evidence"]
    assert "radio-trainer.pptx" in compact[0]["evidence"]
    assert "content_text" in compact[1]["evidence"]


def test_run_agent_once_can_auto_execute_internal_llm_gateway_tool(
    session: Session,
) -> None:
    result = PromptAggregationService(
        session,
        llm_client=FakeAutoLLMGatewayToolLoopLLMClient(),
        tool_adapters={"llm.gateway": FakeLLMGatewayAdapter()},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction="Survey CAD-to-fabrication options for Maestro.",
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
    )

    assert result.status == "completed"
    assert result.tool_loop["enabled"] is True
    assert result.tool_loop["iterations"][0]["requested_tools"][0]["tool_key"] == "llm.gateway"
    assert result.tool_loop["iterations"][0]["executed"][0]["status"] == "complete"
    assert not result.tool_loop["iterations"][0]["blocked"]
    assert any(call["tool_name"] == "llm.gateway" for call in result.tool_calls)
    assert result.output_text is not None
    assert "CAD integration options reviewed" in result.output_text


def test_run_agent_once_can_auto_execute_memory_context_bundle_tool(
    session: Session,
) -> None:
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("maestro-development")
    assert domain is not None
    session.add(
        MemoryItem(
            scope="domain",
            domain_id=domain.id,
            memory_type="decision",
            title="Shared CAD tool infrastructure",
            content=(
                "CAD design agents should use shared CAD tool infrastructure instead of "
                "owning separate CAD integrations per agent."
            ),
            impact_level="medium",
            importance=0.9,
            metadata_={},
        )
    )
    session.commit()

    result = PromptAggregationService(
        session,
        llm_client=FakeAutoMemoryContextToolLoopLLMClient(),
        tool_adapters={"memory.context_bundle": MemoryContextBundleToolAdapter()},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction="Use memory to answer the CAD tool architecture question.",
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
    )

    assert result.status == "completed"
    assert result.tool_loop["iterations"][0]["requested_tools"][0]["tool_key"] == "memory.context_bundle"
    assert result.tool_loop["iterations"][0]["executed"][0]["status"] == "complete"
    assert not result.tool_loop["iterations"][0]["blocked"]
    memory_call = next(call for call in result.tool_calls if call["tool_name"] == "memory.context_bundle")
    assert memory_call["output_payload"]["summary"]["type"] == "memory_context_bundle"
    assert memory_call["output_payload"]["included_count"] >= 1
    assert result.output_text is not None
    assert "Retrieved Maestro memory" in result.output_text


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


def test_run_agent_once_hydrates_pr_number_for_followup_pr_tools(
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
        llm_client=FakeAutoMergeMissingPrNumberLLMClient(),
        tool_adapters={},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction="Cool, merge the PR.",
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
        initial_tool_results=[
            {
                "tool_name": "codex.task.run",
                "status": "complete",
                "output_payload": {
                    "pr_number": 77,
                    "pr_url": "https://github.com/Caliperti1/Maestro/pull/77",
                },
            }
        ],
    )

    assert result.status == "blocked"
    blocked = [
        call for call in result.tool_calls if call["tool_name"] == "github.pr.merge"
    ][0]
    assert blocked["status"] == "approval_required"
    tool_call = session.get(ToolCall, uuid.UUID(blocked["id"]))
    assert tool_call is not None
    assert tool_call.input_payload["payload"]["pr_number"] == 77
    assert result.tool_loop["iterations"][0]["blocked"][0]["payload"]["pr_number"] == 77


def test_run_agent_once_hydrates_pr_number_from_prompt_context(
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
        llm_client=FakeAutoMergeMissingPrNumberLLMClient(),
        tool_adapters={},
    ).run_agent_once(
        PromptPackageRequest(
            agent_key="maestro-introspection-agent",
            task_instruction=(
                "Previous run context: Tool codex.task.run finished with status complete; "
                "PR number: 88; PR URL: https://github.com/Caliperti1/Maestro/pull/88. "
                "Chris said: Cool, merge the PR."
            ),
            use_semantic=False,
        ),
        auto_tool_loop=True,
        execute_llm=True,
    )

    assert result.status == "blocked"
    blocked = [
        call for call in result.tool_calls if call["tool_name"] == "github.pr.merge"
    ][0]
    tool_call = session.get(ToolCall, uuid.UUID(blocked["id"]))
    assert tool_call is not None
    assert tool_call.input_payload["payload"]["pr_number"] == 88


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


def test_run_agent_once_fails_when_required_routed_write_failed(session: Session) -> None:
    _seed_memory(session)

    result = PromptAggregationService(session, llm_client=FakeAgentLLMClient()).run_agent_once(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare a Praxis partner run.",
            use_semantic=False,
        ),
        initial_tool_results=[
            {
                "id": "failed-route-1",
                "tool_name": "routed.item.create",
                "status": "failed",
                "error_message": "Invalid routed payload.",
                "input_payload": {},
                "output_payload": None,
            }
        ],
        execute_llm=True,
    )

    assert result.status == "failed"
    assert result.report_id is not None
    assert result.error_message == "Required operational tool calls failed: routed.item.create."


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
