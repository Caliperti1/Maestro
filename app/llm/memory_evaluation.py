import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.db.models import MemoryItem
from app.llm.client import LLMClient, LLMClientError
from app.llm.telemetry import record_llm_call
from app.memory.service import MemoryCandidate, MemoryEvaluation
from app.prompts import load_prompt

SemanticDecision = Literal["write_new", "duplicate", "reinforce", "supersede", "conflict", "reject"]

MEMORY_EVALUATION_INSTRUCTIONS = load_prompt("memory_evaluation.md")


class ExistingMemoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope: str
    memory_type: str
    title: str
    content: str
    importance: float
    impact_level: str


class CandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    memory_type: str
    title: str
    content: str
    importance: float
    impact_level: str
    rationale: str | None


class MemoryEvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: SemanticDecision
    related_memory_id: str | None = Field(
        description="ID of the most relevant existing memory for duplicate/reinforce/supersede/conflict."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    proposed_title: str | None = Field(
        description="Optional replacement title when decision is supersede."
    )
    proposed_content: str | None = Field(
        description="Optional replacement content when decision is supersede."
    )


class LLMMemoryEvaluator:
    def __init__(self, llm_client: LLMClient, session: Session | None = None):
        self.llm_client = llm_client
        self.session = session

    def evaluate(
        self,
        *,
        candidate: MemoryCandidate,
        existing_memories: list[MemoryItem] | tuple[MemoryItem, ...],
    ) -> MemoryEvaluation:
        if not existing_memories:
            return MemoryEvaluation(decision="write_new", confidence=1.0)

        payload = {
            "candidate": CandidatePayload(
                scope=candidate.scope,
                memory_type=candidate.memory_type,
                title=candidate.title,
                content=candidate.content,
                importance=candidate.importance,
                impact_level=candidate.impact_level,
                rationale=candidate.rationale,
            ).model_dump(),
            "existing_memories": [
                ExistingMemoryPayload(
                    id=str(memory.id),
                    scope=memory.scope,
                    memory_type=memory.memory_type,
                    title=memory.title,
                    content=memory.content,
                    importance=memory.importance,
                    impact_level=memory.impact_level,
                ).model_dump()
                for memory in existing_memories
            ],
        }
        input_text = str(payload)
        raw_response = self.llm_client.structured_response(
            instructions=MEMORY_EVALUATION_INSTRUCTIONS,
            input_text=input_text,
            schema_name="memory_evaluation_response",
            schema=MemoryEvaluationResponse.model_json_schema(),
        )
        if self.session is not None:
            record_llm_call(
                self.session,
                component="memory.evaluation",
                client=self.llm_client,
                task_id=candidate.task_id,
                workflow_run_id=candidate.metadata.get("workflow_run_id"),
                prompt_chars=len(input_text),
                prompt_sections={
                    "candidate": len(candidate.title) + len(candidate.content),
                    "existing_memories": sum(
                        len(memory.title) + len(memory.content) for memory in existing_memories
                    ),
                },
                metadata={"existing_memory_count": len(existing_memories)},
            )
        try:
            response = MemoryEvaluationResponse.model_validate(raw_response)
        except ValidationError as exc:
            raise LLMClientError("LLM memory evaluation did not match the expected schema.") from exc

        related_memory_id = uuid.UUID(response.related_memory_id) if response.related_memory_id else None
        return MemoryEvaluation(
            decision=response.decision,
            related_memory_id=related_memory_id,
            confidence=response.confidence,
            rationale=response.rationale,
            proposed_title=response.proposed_title,
            proposed_content=response.proposed_content,
        )
