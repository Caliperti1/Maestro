from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from app.llm.client import LLMClient, LLMClientError
from app.llm.telemetry import record_llm_call
from app.prompts import load_prompt

WorkItemType = Literal[
    "workflow_task",
    "standalone_task",
    "contact",
    "event",
    "decision",
    "rfi",
    "memory_candidate",
    "direct_response",
]
PlannerPriority = Literal["low", "normal", "high", "urgent"]
PlannerModelTier = Literal["auto", "qwen", "luna", "terra", "sol"]

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
    model_tier: PlannerModelTier
    model_rationale: str


class MaestroPlannerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_summary: str
    direct_response: str | None
    work_items: list[PlannerWorkItem]
    planner_notes: str


class LLMMaestroPlanner:
    def __init__(self, llm_client: LLMClient, session: Session | None = None):
        self.llm_client = llm_client
        self.session = session
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
        try:
            raw_response = self.llm_client.structured_response(
                instructions=MAESTRO_PLANNER_INSTRUCTIONS,
                input_text=input_text,
                schema_name="maestro_planner_response",
                schema=schema,
            )
        except (LLMClientError, OSError, TypeError, ValueError) as exc:
            self._record_call(status="failed", error_message=str(exc))
            raise
        self._record_call(status="complete", error_message=None)
        try:
            normalized = _normalize_planner_response(raw_response)
            return MaestroPlannerResponse.model_validate(normalized)
        except ValidationError as exc:
            raise LLMClientError("LLM Maestro planner did not match the expected schema.") from exc

    def _record_call(self, *, status: str, error_message: str | None) -> None:
        if self.session is None:
            return
        record_llm_call(
            self.session,
            component="maestro.workflow_planner",
            client=self.llm_client,
            prompt_chars=(
                self.last_prompt_metrics.get("system_prompt_chars", 0)
                + self.last_prompt_metrics.get("input_chars", 0)
                + self.last_prompt_metrics.get("schema_chars", 0)
            ),
            prompt_sections=self.last_prompt_metrics,
            status=status,
            error_message=error_message,
        )


def _normalize_planner_response(raw_response: Any) -> Any:
    """Backfill additive planner hints without accepting malformed work items."""
    if not isinstance(raw_response, dict):
        return raw_response
    work_items = raw_response.get("work_items")
    if not isinstance(work_items, list):
        return raw_response
    normalized_items = []
    for item in work_items:
        if not isinstance(item, dict):
            normalized_items.append(item)
            continue
        normalized_type = item.get("type")
        if normalized_type == "think_tank":
            normalized_type = "direct_response"
        normalized_items.append(
            {
                **item,
                "type": normalized_type,
                "model_tier": item.get("model_tier") or "auto",
                "model_rationale": item.get("model_rationale")
                or "Runtime routing will select the appropriate model tier.",
            }
        )
    return {**raw_response, "work_items": normalized_items}
