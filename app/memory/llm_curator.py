import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.llm.memory_extraction import LLMMemoryExtractor
from app.db.models import RoutedItem
from app.memory.curator import CuratedMemoryBatch, StagedMemorySource
from app.memory.service import (
    MemoryCandidate,
    MemoryScope,
    MemorySemanticEvaluator,
    MemoryService,
    MemoryWriteResult,
)
from app.memory.routed_service import RoutedMemoryService


@dataclass(frozen=True)
class PreviewableMemoryBatch:
    source: StagedMemorySource
    candidates: Sequence[MemoryCandidate]
    routed_items: Sequence[RoutedItem]


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

    def extract_routed_items(
        self,
        source: StagedMemorySource,
        *,
        domain_key: str,
    ) -> list[RoutedItem]:
        extracted = self.extractor.extract(
            source_title=source.title or "Untitled staged source",
            source_text=source.content,
            domain_key=domain_key,
        )
        return self._routed_items_from_extraction(source, extracted.routed_items)

    def extract_source(
        self,
        source: StagedMemorySource,
        *,
        domain_key: str,
    ) -> tuple[list[MemoryCandidate], list[RoutedItem]]:
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
                "route_type": "memory",
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
        return candidates, self._routed_items_from_extraction(source, extracted.routed_items)

    def preview_source(
        self,
        source: StagedMemorySource,
        *,
        domain_key: str,
    ) -> PreviewableMemoryBatch:
        candidates, routed_items = self.extract_source(source, domain_key=domain_key)
        return PreviewableMemoryBatch(
            source=source,
            candidates=candidates,
            routed_items=routed_items,
        )

    def write_candidates(
        self,
        source: StagedMemorySource,
        candidates: Sequence[MemoryCandidate],
        routed_items: Sequence[RoutedItem] = (),
    ) -> CuratedMemoryBatch:
        self._write_routed_items(routed_items)
        results = [self.memory_service.write_candidate(candidate) for candidate in candidates]
        return CuratedMemoryBatch(source=source, candidates=candidates, results=results)

    def process_source(self, source: StagedMemorySource, *, domain_key: str) -> CuratedMemoryBatch:
        candidates, routed_items = self.extract_source(source, domain_key=domain_key)
        return self.write_candidates(source, candidates, routed_items)

    def _routed_items_from_extraction(
        self,
        source: StagedMemorySource,
        extracted_items,
    ) -> list[RoutedItem]:
        items: list[RoutedItem] = []
        seed_package_id = _uuid_or_none(source.metadata.get("seed_package_id"))
        artifact_id = _uuid_or_none(source.metadata.get("artifact_id"))
        for extracted_item in extracted_items:
            if extracted_item.route_type == "ignore":
                continue
            structured_data = extracted_item.structured_data or {}
            items.append(
                RoutedItem(
                    domain_id=source.domain_id,
                    agent_id=source.agent_id,
                    task_id=source.task_id,
                    report_id=source.report_id,
                    seed_package_id=seed_package_id,
                    artifact_id=artifact_id,
                    route_type=extracted_item.route_type,
                    title=extracted_item.title,
                    content=extracted_item.content,
                    priority=extracted_item.priority,
                    status=extracted_item.status or "open",
                    source_refs=[self._source_ref(source)],
                    metadata_={
                        "curator": "llm",
                        "source_title": source.title,
                        "rationale": extracted_item.rationale,
                        "llm_confidence": extracted_item.confidence,
                        "structured_data": structured_data,
                        **source.metadata,
                        **structured_data,
                    },
                )
            )
        return items

    def _write_routed_items(self, routed_items: Sequence[RoutedItem]) -> None:
        for item in routed_items:
            self.memory_service.session.add(item)
        if routed_items:
            self.memory_service.session.commit()
            RoutedMemoryService(self.memory_service.session).promote_items(list(routed_items))

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


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None
