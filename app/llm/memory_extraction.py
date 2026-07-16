from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.llm.client import LLMClient, LLMClientError
from app.prompts import load_prompt

ExtractedScope = Literal["global", "maestro_session", "domain", "agent"]
ExtractedImpact = Literal["low", "medium", "high", "very_high"]
ExtractedRouteType = Literal[
    "task",
    "human_input",
    "event",
    "contact",
    "entity",
    "think_tank",
    "decision_log",
    "project",
    "artifact_history",
    "integration_note",
    "ignore",
]
ExtractedPriority = Literal["low", "normal", "high", "urgent"]

MEMORY_EXTRACTION_INSTRUCTIONS = load_prompt("memory_extraction.md")

DOMAIN_CONTEXTS = {
    "global": (
        "Cross-domain Maestro operating context. Use only for system-wide behavior preferences, "
        "approval rules, and principles that every domain agent must apply."
    ),
    "personal": (
        "Personal domain covering Chris's biography, resume, career history, personal goals, "
        "life admin, planning, reminders, priorities, and personal preferences."
    ),
    "maestro-development": (
        "Maestro Development domain covering the Maestro product, architecture, backlog, "
        "repo work, Codex handoffs, and self-improvement."
    ),
    "praxis": (
        "Praxis domain covering company strategy, product, engineering, growth, and operations."
    ),
    "ophi": "Ophi domain covering product research, market research, operations, and strategy.",
    "usma": "USMA domain covering teaching, administration, cadet support, and academic work.",
    "personal-irad-projects": (
        "Personal IRAD Projects domain covering independent research, prototypes, and build plans."
    ),
    "l3": "L3 domain covering L3 work context and related professional obligations.",
}


class ExtractedMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ExtractedScope
    memory_type: str = Field(
        description=(
            "One of fact, preference, decision, summary, standing_instruction, entity, "
            "relationship, project, source_summary, or another concise memory type."
        )
    )
    title: str
    content: str
    rationale: str
    impact_level: ExtractedImpact
    importance: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedRoutedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_type: ExtractedRouteType
    title: str
    content: str
    rationale: str
    priority: ExtractedPriority
    confidence: float = Field(ge=0.0, le=1.0)
    status: str
    structured_data: dict[str, Any]


class ExtractedMemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ExtractedMemoryCandidate]
    routed_items: list[ExtractedRoutedItem]


class LLMMemoryExtractor:
    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    def extract(
        self,
        *,
        source_title: str,
        source_text: str,
        domain_key: str,
    ) -> ExtractedMemoryResponse:
        input_text = f"""\
Domain key: {domain_key}
Domain context: {_domain_context(domain_key)}
Source title: {source_title}

Source:
{source_text}
"""
        raw_response = self.llm_client.structured_response(
            instructions=MEMORY_EXTRACTION_INSTRUCTIONS,
            input_text=input_text,
            schema_name="memory_extraction_response",
            schema=ExtractedMemoryResponse.model_json_schema(),
        )
        try:
            return ExtractedMemoryResponse.model_validate(raw_response)
        except ValidationError as exc:
            raise LLMClientError(
                "LLM memory extraction did not match the expected schema."
            ) from exc


def _domain_context(domain_key: str) -> str:
    return DOMAIN_CONTEXTS.get(
        domain_key,
        "Unknown domain. Default to domain-scoped memory unless the source clearly "
        "applies globally.",
    )
