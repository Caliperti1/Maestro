from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.llm.client import LLMClient, LLMClientError

ExtractedScope = Literal["global", "maestro_session", "domain", "agent"]
ExtractedImpact = Literal["low", "medium", "high", "very_high"]

MEMORY_EXTRACTION_INSTRUCTIONS = """\
Extract durable Maestro memory candidates from the staged source.

Return only memories that are likely to remain useful beyond this single source. Prefer
specific facts, decisions, preferences, project context, relationship/entity context,
standing instructions, and compact summaries.

Do not invent facts. If the source is ambiguous, either omit the memory or set a lower
confidence. Use very_high impact only for memories that would materially change Maestro's
authority, external commitments, permissions, durable strategy, or user-critical behavior.

For source files dropped into a domain folder, default to domain scope. Use global only when
the memory applies across Maestro. Use maestro_session for transient cross-domain session
context. Use agent only when the source clearly names an agent-specific instruction.
"""


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


class ExtractedMemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ExtractedMemoryCandidate]


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
