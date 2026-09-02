import uuid
from collections.abc import Iterable
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.llm.client import LLMClient, LLMClientError
from app.llm.telemetry import record_llm_call
from app.maestro.identity_grounding import IdentityGroundingService
from app.prompts import load_prompt

ExtractedScope = Literal["global", "maestro_session", "domain", "agent"]
ExtractedImpact = Literal["low", "medium", "high", "very_high"]
ExtractedRouteType = Literal[
    "task",
    "human_input",
    "event",
    "contact",
    "entity",
    "decision_log",
    "project",
    "artifact_history",
    "integration_note",
    "ignore",
]
ExtractedPriority = Literal["low", "normal", "high", "urgent"]
ExtractedStructuredKey = Literal[
    "start_at",
    "end_at",
    "date",
    "time",
    "location",
    "attendees",
    "supporting_refs",
    "due_at",
    "estimated_minutes",
    "scheduled_start_at",
    "agent_task",
    "recurrence_rule",
    "recurrence_timezone",
    "owner",
    "assignee",
    "blocking",
    "related_contact",
    "name",
    "email",
    "phone",
    "linkedin",
    "organization",
    "role",
    "origination",
    "last_contact_at",
    "website",
    "organization_type",
    "aliases",
    "decision_maker",
    "decided_at",
    "supersedes",
]

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
    "perti-laboratories": (
        "Perti Laboratories domain covering software products, applied research, independent R&D, "
        "market development, and technical operations."
    ),
    "usma": "USMA domain covering teaching, administration, cadet support, and academic work.",
    "l3": "L3 domain covering L3 work context and related professional obligations.",
}

ModelT = TypeVar("ModelT", bound=BaseModel)


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


class ExtractedStructuredField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: ExtractedStructuredKey
    value: str | bool | float | list[str] | None


class ExtractedRoutedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_type: ExtractedRouteType
    title: str
    content: str
    rationale: str
    priority: ExtractedPriority
    confidence: float = Field(ge=0.0, le=1.0)
    status: str
    structured_data: list[ExtractedStructuredField]


class ExtractedMemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ExtractedMemoryCandidate]
    routed_items: list[ExtractedRoutedItem]


class LLMMemoryExtractor:
    def __init__(
        self,
        llm_client: LLMClient,
        session: Session | None = None,
        *,
        chunk_chars: int | None = None,
        max_source_chars: int | None = None,
    ):
        self.llm_client = llm_client
        self.session = session
        settings = get_settings()
        self.chunk_chars = chunk_chars or settings.memory_extraction_chunk_chars
        self.max_source_chars = max_source_chars or settings.memory_extraction_max_source_chars

    def extract(
        self,
        *,
        source_title: str,
        source_text: str,
        domain_key: str,
        task_id: uuid.UUID | None = None,
        workflow_run_id: str | uuid.UUID | None = None,
    ) -> ExtractedMemoryResponse:
        source_chunks = _source_chunks(
            source_text,
            chunk_chars=self.chunk_chars,
            max_source_chars=self.max_source_chars,
        )
        identity_context = (
            IdentityGroundingService(self.session)
            .build_packet(domain_key=domain_key)
            .rendered_text
            if self.session is not None
            else ""
        )
        responses: list[ExtractedMemoryResponse] = []
        for chunk_index, source_chunk in enumerate(source_chunks, start=1):
            input_text = f"""\
Domain key: {domain_key}
Domain context: {_domain_context(domain_key)}
Authoritative identity context:
{identity_context}
Source title: {source_title}
Source chunk: {chunk_index} of {len(source_chunks)}

Source:
{source_chunk}
"""
            try:
                raw_response = self.llm_client.structured_response(
                    instructions=MEMORY_EXTRACTION_INSTRUCTIONS,
                    input_text=input_text,
                    schema_name="memory_extraction_response",
                    schema=ExtractedMemoryResponse.model_json_schema(),
                )
            except (LLMClientError, OSError, ValueError) as exc:
                self._record_call(
                    source_title=source_title,
                    source_chunk=source_chunk,
                    source_total_chars=len(source_text),
                    domain_key=domain_key,
                    identity_context=identity_context,
                    input_text=input_text,
                    chunk_index=chunk_index,
                    chunk_count=len(source_chunks),
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                    status="failed",
                    error_message=str(exc),
                )
                raise
            self._record_call(
                source_title=source_title,
                source_chunk=source_chunk,
                source_total_chars=len(source_text),
                domain_key=domain_key,
                identity_context=identity_context,
                input_text=input_text,
                chunk_index=chunk_index,
                chunk_count=len(source_chunks),
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                status="complete",
                error_message=None,
            )
            try:
                responses.append(ExtractedMemoryResponse.model_validate(raw_response))
            except ValidationError as exc:
                raise LLMClientError(
                    "LLM memory extraction did not match the expected schema."
                ) from exc
        return ExtractedMemoryResponse(
            candidates=_deduplicate_models(
                candidate for response in responses for candidate in response.candidates
            ),
            routed_items=_deduplicate_models(
                item for response in responses for item in response.routed_items
            ),
        )

    def _record_call(
        self,
        *,
        source_title: str,
        source_chunk: str,
        source_total_chars: int,
        domain_key: str,
        identity_context: str,
        input_text: str,
        chunk_index: int,
        chunk_count: int,
        task_id: uuid.UUID | None,
        workflow_run_id: str | uuid.UUID | None,
        status: str,
        error_message: str | None,
    ) -> None:
        if self.session is None:
            return
        record_llm_call(
            self.session,
            component="memory.extraction",
            client=self.llm_client,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            prompt_chars=len(input_text),
            prompt_sections={
                "source": len(source_chunk),
                "source_total": source_total_chars,
                "domain_context": len(_domain_context(domain_key)),
                "identity_grounding": len(identity_context),
            },
            status=status,
            error_message=error_message,
            metadata={
                "source_title": source_title,
                "domain_key": domain_key,
                "chunk_index": chunk_index,
                "chunk_count": chunk_count,
            },
        )


def _source_chunks(
    source_text: str,
    *,
    chunk_chars: int,
    max_source_chars: int,
) -> list[str]:
    if len(source_text) > max_source_chars:
        raise LLMClientError(
            "Memory extraction source blocked before transmission: "
            f"{len(source_text):,} characters exceeds the configured "
            f"{max_source_chars:,}-character per-source limit. Split the source into smaller "
            "documents or compact the staged artifact before retrying."
        )
    if len(source_text) <= chunk_chars:
        return [source_text]

    chunks: list[str] = []
    start = 0
    while start < len(source_text):
        hard_end = min(start + chunk_chars, len(source_text))
        end = hard_end
        if hard_end < len(source_text):
            paragraph_end = source_text.rfind("\n\n", start + chunk_chars // 2, hard_end)
            line_end = source_text.rfind("\n", start + chunk_chars // 2, hard_end)
            end = max(
                paragraph_end + 2 if paragraph_end >= 0 else 0,
                line_end + 1 if line_end >= 0 else 0,
            )
            if end <= start:
                end = hard_end
        chunks.append(source_text[start:end])
        start = end
    return chunks


def _deduplicate_models(values: Iterable[ModelT]) -> list[ModelT]:
    unique: list[ModelT] = []
    seen: set[str] = set()
    for value in values:
        key = value.model_dump_json(exclude_none=False)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _domain_context(domain_key: str) -> str:
    return DOMAIN_CONTEXTS.get(
        domain_key,
        "Unknown domain. Default to domain-scoped memory unless the source clearly "
        "applies globally.",
    )
