from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.llm.client import LLMClient, LLMClientError

WorkItemType = Literal[
    "workflow_task",
    "standalone_task",
    "contact",
    "event",
    "decision",
    "rfi",
    "memory_candidate",
    "think_tank",
    "direct_response",
]
PlannerPriority = Literal["low", "normal", "high", "urgent"]

MAESTRO_PLANNER_INSTRUCTIONS = """\
You are Maestro's top-level orchestration planner.

You are speaking directly with Chris Aliperti. Chris is the user. When Chris says "I", "me",
"my", or "we" in the chat, interpret that through Chris's personal context and Maestro's current
session context. Do not write user-facing responses or RFI titles as if Chris is a third party you
need to ask; phrase missing inputs as questions to "you" when the response will be shown in chat.

Your job is to decompose Chris's input before final agent tasking. Use the active agent roster,
domain contexts, and tool registry to understand available specialties while decomposing, but do
not skip straight to "send the whole request to these agents." First identify the work and retained
information inside the request. A single message can contain multiple things at once: workflow
tasks, standalone tasks, contacts, events, decisions, RFIs, memory candidates, think tank notes,
and direct-response content.

Rules:
- Preserve the user's intent and uncertainty. Do not invent facts, owners, dates, or agent names.
- Use the active domain list and assign each work item to the best domain when possible.
- Use global only for cross-system Maestro behavior. Use maestro-development for Maestro product
  or architecture work.
- Set needs_agent true only when an agent should do actual work.
- Set can_log_directly true for items that should be captured/routed without agent execution.
- Use required_capabilities to describe what expertise is needed before matching an agent.
- Use required_tools only for tool classes that are clearly needed.
- For requests to implement, code, fix, action a GitHub issue, or change Maestro code, create a
  maestro-development work item that requires `codex.task.run` when a coding agent with that tool
  exists in the roster. Use GitHub read tools as dependencies/context tools when the request names
  a specific issue or PR.
- Use dependencies to reference other work item IDs that must complete first.
- Set blocks_execution true for RFIs or missing inputs that should pause the workflow until you
  answer. Set it false when useful work can proceed while waiting.
- Workflow work items should be role-sized. Do not create one broad workflow_task that requires
  every agent in a domain. If product demo planning, CRM context, technical feasibility, and meeting
  capture are all needed, create separate work items with dependencies.
- Use suggested_agent_keys only as hints for the later matching pass. Suggested agents must not
  be the only reason a work item exists.
- If the request is just a note, idea, simple task, or memory/logging item, say so. Do not invent
  a workflow.
- If the workflow cannot proceed without your answer, emit an RFI work item.

Return strict JSON matching the schema. Every work item ID must be unique and stable inside the
response, such as wi_1, wi_2, wi_3.
"""


class PlannerWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: WorkItemType
    title: str
    description: str
    domain_key: str | None
    priority: PlannerPriority
    required_capabilities: list[str]
    required_tools: list[str]
    dependencies: list[str]
    needs_agent: bool
    needs_user_input: bool
    blocks_execution: bool
    can_log_directly: bool
    suggested_agent_keys: list[str]
    expected_output: str
    rationale: str


class MaestroPlannerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_summary: str
    direct_response: str | None
    work_items: list[PlannerWorkItem]
    planner_notes: str


class LLMMaestroPlanner:
    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    def decompose(
        self,
        *,
        user_input: str,
        planning_context: dict[str, Any],
    ) -> MaestroPlannerResponse:
        raw_response = self.llm_client.structured_response(
            instructions=MAESTRO_PLANNER_INSTRUCTIONS,
            input_text=(
                "User input:\n"
                f"{user_input}\n\n"
                "Planning context JSON:\n"
                f"{planning_context}"
            ),
            schema_name="maestro_planner_response",
            schema=MaestroPlannerResponse.model_json_schema(),
        )
        try:
            return MaestroPlannerResponse.model_validate(raw_response)
        except ValidationError as exc:
            raise LLMClientError("LLM Maestro planner did not match the expected schema.") from exc
