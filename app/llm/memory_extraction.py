import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

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
    def __init__(self, llm_client: LLMClient, session: Session | None = None):
        self.llm_client = llm_client
        self.session = session

    def extract(
        self,
        *,
        source_title: str,
        source_text: str,
        domain_key: str,
        task_id: uuid.UUID | None = None,
        workflow_run_id: str | uuid.UUID | None = None,
    ) -> ExtractedMemoryResponse:
        identity_context = (
            IdentityGroundingService(self.session)
            .build_packet(domain_key=domain_key)
            .rendered_text
            if self.session is not None
            else ""
        )
        input_text = f"""\
Domain key: {domain_key}
Domain context: {_domain_context(domain_key)}
Authoritative identity context:
{identity_context}
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
        if self.session is not None:
            record_llm_call(
                self.session,
                component="memory.extraction",
                client=self.llm_client,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                prompt_chars=len(input_text),
                prompt_sections={
                    "source": len(source_text),
                    "domain_context": len(_domain_context(domain_key)),
                    "identity_grounding": len(identity_context),
                },
                metadata={"source_title": source_title, "domain_key": domain_key},
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
