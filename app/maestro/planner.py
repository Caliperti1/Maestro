from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.llm.client import LLMClient, LLMClientError
from app.prompts import load_prompt

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

MAESTRO_PLANNER_INSTRUCTIONS = load_prompt("maestro_planner.md")


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
        self.last_prompt_metrics: dict[str, int] = {}

    def decompose(
        self,
        *,
        user_input: str,
        planning_context: dict[str, Any],
    ) -> MaestroPlannerResponse:
        input_text = (
            "User input:\n"
            f"{user_input}\n\n"
            "Planning context JSON:\n"
            f"{planning_context}"
        )
        schema = MaestroPlannerResponse.model_json_schema()
        self.last_prompt_metrics = {
            "system_prompt_chars": len(MAESTRO_PLANNER_INSTRUCTIONS),
            "input_chars": len(input_text),
            "schema_chars": len(str(schema)),
            "planning_context_chars": len(str(planning_context)),
            "registry_chars": len(str(planning_context.get("registry", ""))),
            "memory_chars": len(str((planning_context.get("retrieved_memory") or {}).get("rendered_text", ""))),
        }
        raw_response = self.llm_client.structured_response(
            instructions=MAESTRO_PLANNER_INSTRUCTIONS,
            input_text=input_text,
            schema_name="maestro_planner_response",
            schema=schema,
        )
        try:
            return MaestroPlannerResponse.model_validate(raw_response)
        except ValidationError as exc:
            raise LLMClientError("LLM Maestro planner did not match the expected schema.") from exc
