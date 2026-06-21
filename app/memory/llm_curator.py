import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.llm.memory_extraction import LLMMemoryExtractor
from app.memory.curator import CuratedMemoryBatch, StagedMemorySource
from app.memory.service import (
    MemoryCandidate,
    MemoryScope,
    MemorySemanticEvaluator,
    MemoryService,
    MemoryWriteResult,
)


@dataclass(frozen=True)
class PreviewableMemoryBatch:
    source: StagedMemorySource
    candidates: Sequence[MemoryCandidate]


class LLMMemoryCurator:
    def __init__(
        self,
        session: Session,
        extractor: LLMMemoryExtractor,
        *,
        semantic_evaluator: MemorySemanticEvaluator | None = None,
        embedding_service=None,
    ):
        self.memory_service = MemoryService(
            session,
            semantic_evaluator=semantic_evaluator,
            embedding_service=embedding_service,
        )
        self.extractor = extractor

    def extract_candidates(
        self,
        source: StagedMemorySource,
        *,
        domain_key: str,
    ) -> list[MemoryCandidate]:
        extracted = self.extractor.extract(
            source_title=source.title or "Untitled staged source",
            source_text=source.content,
            domain_key=domain_key,
        )
        candidates: list[MemoryCandidate] = []
        for extracted_candidate in extracted.candidates:
            scope = extracted_candidate.scope
            domain_id, agent_id = self._ids_for_scope(source, scope)
            metadata = {
                "curator": "llm",
                "source_title": source.title,
                "llm_confidence": extracted_candidate.confidence,
                **source.metadata,
            }
            candidates.append(
                MemoryCandidate(
                    domain_id=domain_id,
                    agent_id=agent_id,
                    task_id=source.task_id,
                    report_id=source.report_id,
                    scope=scope,
                    memory_type=extracted_candidate.memory_type,
                    title=extracted_candidate.title,
                    content=extracted_candidate.content,
                    rationale=extracted_candidate.rationale,
                    impact_level=extracted_candidate.impact_level,
                    importance=extracted_candidate.importance,
                    source_refs=[self._source_ref(source)],
                    metadata=metadata,
                )
            )
        return candidates

    def preview_source(
        self,
        source: StagedMemorySource,
        *,
        domain_key: str,
    ) -> PreviewableMemoryBatch:
        return PreviewableMemoryBatch(
            source=source,
            candidates=self.extract_candidates(source, domain_key=domain_key),
        )

    def write_candidates(
        self,
        source: StagedMemorySource,
        candidates: Sequence[MemoryCandidate],
    ) -> CuratedMemoryBatch:
        results = [self.memory_service.write_candidate(candidate) for candidate in candidates]
        return CuratedMemoryBatch(source=source, candidates=candidates, results=results)

    def process_source(self, source: StagedMemorySource, *, domain_key: str) -> CuratedMemoryBatch:
        candidates = self.extract_candidates(source, domain_key=domain_key)
        return self.write_candidates(source, candidates)

    def _ids_for_scope(
        self,
        source: StagedMemorySource,
        scope: MemoryScope,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        if scope == "global":
            return None, None
        if scope == "maestro_session":
            return source.domain_id, None
        if scope == "agent":
            if source.agent_id is None:
                return source.domain_id, None
            return source.domain_id, source.agent_id
        return source.domain_id, None

    def _source_ref(self, source: StagedMemorySource) -> dict[str, object]:
        source_ref: dict[str, object] = {"type": source.source_type}
        if source.source_id is not None:
            source_ref["id"] = str(source.source_id)
        if source.uri is not None:
            source_ref["uri"] = source.uri
        return source_ref
