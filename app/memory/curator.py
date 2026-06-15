import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import Session

from app.db.models import MemoryItem, MemoryProposal
from app.memory.service import (
    ImpactLevel,
    MemoryCandidate,
    MemoryScope,
    MemoryService,
    MemoryWriteResult,
)

StagedSourceType = Literal[
    "message",
    "artifact",
    "tool_call",
    "report",
    "seed_package",
    "raw_note",
]

_MARKERS: dict[str, tuple[str, ImpactLevel]] = {
    "MEMORY": ("fact", "low"),
    "FACT": ("fact", "low"),
    "PREFERENCE": ("preference", "low"),
    "DECISION": ("decision", "medium"),
    "SUMMARY": ("summary", "medium"),
    "INSTRUCTION": ("standing_instruction", "high"),
    "VERY_HIGH": ("standing_instruction", "very_high"),
}

_SCOPE_ALIASES = {
    "global": "global",
    "session": "maestro_session",
    "maestro_session": "maestro_session",
    "domain": "domain",
    "agent": "agent",
}


@dataclass(frozen=True)
class StagedMemorySource:
    source_type: StagedSourceType
    content: str
    source_id: uuid.UUID | str | None = None
    domain_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    report_id: uuid.UUID | None = None
    title: str | None = None
    uri: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CuratedMemoryBatch:
    source: StagedMemorySource
    candidates: Sequence[MemoryCandidate]
    results: Sequence[MemoryWriteResult]

    @property
    def pending_approval_count(self) -> int:
        return sum(1 for result in self.results if result.outcome == "pending_user_approval")


class MemoryCurator:
    """Deterministic curator used to prove memory plumbing before LLM extraction."""

    def __init__(self, session: Session):
        self.memory_service = MemoryService(session)

    def extract_candidates(self, source: StagedMemorySource) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        for line_number, raw_line in enumerate(source.content.splitlines(), start=1):
            parsed = self._parse_marked_line(raw_line)
            if parsed is None:
                continue
            marker, scope, title, content = parsed
            memory_type, impact_level = _MARKERS[marker]
            domain_id, agent_id = self._ids_for_scope(source, scope)
            candidates.append(
                MemoryCandidate(
                    domain_id=domain_id,
                    agent_id=agent_id,
                    task_id=source.task_id,
                    report_id=source.report_id,
                    scope=scope,
                    memory_type=memory_type,
                    title=title,
                    content=content,
                    rationale="Extracted by deterministic Memory Curator marker parser.",
                    impact_level=impact_level,
                    importance=self._importance_for_impact(impact_level),
                    source_refs=[self._source_ref(source, line_number=line_number)],
                    metadata={
                        "curator": "deterministic",
                        "marker": marker.lower(),
                        "source_title": source.title,
                        **source.metadata,
                    },
                )
            )
        return candidates

    def process_source(self, source: StagedMemorySource) -> CuratedMemoryBatch:
        candidates = self.extract_candidates(source)
        results = [self.memory_service.write_candidate(candidate) for candidate in candidates]
        return CuratedMemoryBatch(source=source, candidates=candidates, results=results)

    def process_sources(self, sources: Iterable[StagedMemorySource]) -> list[CuratedMemoryBatch]:
        return [self.process_source(source) for source in sources]

    def list_pending_approvals(
        self,
        *,
        domain_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> Sequence[MemoryProposal]:
        return self.memory_service.list_pending_approvals(
            domain_id=domain_id,
            agent_id=agent_id,
            limit=limit,
        )

    def approve_pending_memory(self, proposal_id: uuid.UUID) -> MemoryItem:
        return self.memory_service.approve_proposal(proposal_id)

    def reject_pending_memory(
        self,
        proposal_id: uuid.UUID,
        *,
        reason: str | None = None,
    ) -> MemoryProposal:
        return self.memory_service.reject_proposal(proposal_id, reason=reason)

    def _parse_marked_line(
        self,
        raw_line: str,
    ) -> tuple[str, MemoryScope, str, str] | None:
        line = raw_line.strip()
        if not line or ":" not in line:
            return None

        marker_part, body = line.split(":", 1)
        marker_tokens = marker_part.strip().split()
        if not marker_tokens:
            return None

        marker = marker_tokens[0].upper()
        if marker not in _MARKERS:
            return None

        scope = self._scope_from_tokens(marker_tokens[1:])
        body = body.strip()
        if not body:
            return None

        title, content = self._title_and_content(marker, body)
        return marker, scope, title, content

    def _scope_from_tokens(self, tokens: Sequence[str]) -> MemoryScope:
        if not tokens:
            return "domain"

        token = tokens[0].strip().lower()
        if token.startswith("[") and token.endswith("]"):
            token = token[1:-1]
        return _SCOPE_ALIASES.get(token, "domain")  # type: ignore[return-value]

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
            return source.domain_id, source.agent_id
        return source.domain_id, None

    def _title_and_content(self, marker: str, body: str) -> tuple[str, str]:
        if " - " in body:
            title, content = body.split(" - ", 1)
            return title.strip(), content.strip()

        return marker.replace("_", " ").title(), body

    def _source_ref(self, source: StagedMemorySource, *, line_number: int) -> dict[str, object]:
        source_ref: dict[str, object] = {
            "type": source.source_type,
            "line": line_number,
        }
        if source.source_id is not None:
            source_ref["id"] = str(source.source_id)
        if source.uri is not None:
            source_ref["uri"] = source.uri
        return source_ref

    def _importance_for_impact(self, impact_level: ImpactLevel) -> float:
        if impact_level == "very_high":
            return 0.95
        if impact_level == "high":
            return 0.85
        if impact_level == "medium":
            return 0.7
        return 0.5
