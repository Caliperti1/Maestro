"""Low-risk maintenance and review proposals for canonical durable memory."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MemoryHygieneRun, MemoryItem, MemoryProposal
from app.memory.embeddings import MemoryEmbeddingService
from app.memory.federated_retrieval import FederatedIndexService


class DurableMemoryHygieneService:
    """Repairs mechanics automatically while leaving semantic changes for review."""

    def __init__(self, session: Session, *, embedding_service: MemoryEmbeddingService | None = None):
        self.session = session
        self.embedding_service = embedding_service

    def run(self) -> MemoryHygieneRun:
        run = MemoryHygieneRun(status="running", details={})
        self.session.add(run)
        self.session.flush()
        try:
            memories = list(self.session.scalars(select(MemoryItem).order_by(MemoryItem.created_at.asc())).all())
            run.scanned_count = len(memories)
            run.provenance_repaired_count = self._repair_provenance(memories)
            run.duplicate_merged_count = self._merge_exact_duplicates(memories)
            run.proposal_count = self._propose_semantic_review(memories)
            embedding_results = (self.embedding_service or MemoryEmbeddingService(self.session)).backfill()
            run.embedding_backfilled_count = sum(result.status == "written" for result in embedding_results)
            index_result = FederatedIndexService(self.session).sync(embed_missing=False)
            run.details = {
                "embedding_failures": [result.error for result in embedding_results if result.status == "failed"],
                "retrieval_index": index_result.__dict__,
            }
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            self.session.commit()
        except Exception as exc:
            self.session.rollback()
            run = self.session.get(MemoryHygieneRun, run.id) or MemoryHygieneRun(id=run.id)
            run.status = "failed"
            run.error_message = str(exc)
            run.completed_at = datetime.now(UTC)
            self.session.add(run)
            self.session.commit()
        return run

    def _repair_provenance(self, memories: list[MemoryItem]) -> int:
        repaired = 0
        for memory in memories:
            metadata = dict(memory.metadata_ or {})
            source_refs = metadata.get("source_refs")
            if not isinstance(source_refs, list):
                source_refs = []
            changed = False
            if not source_refs and memory.created_from_proposal_id:
                source_refs.append({"type": "memory_proposal", "id": str(memory.created_from_proposal_id)})
                changed = True
            if "hygiene_last_checked_at" not in metadata:
                changed = True
            metadata["source_refs"] = source_refs
            metadata["hygiene_last_checked_at"] = datetime.now(UTC).isoformat()
            if changed:
                memory.metadata_ = metadata
                repaired += 1
        return repaired

    def _merge_exact_duplicates(self, memories: list[MemoryItem]) -> int:
        lanes: dict[tuple, MemoryItem] = {}
        merged = 0
        now = datetime.now(UTC)
        for memory in memories:
            if memory.valid_until is not None:
                continue
            key = (
                memory.domain_id,
                memory.agent_id,
                memory.scope,
                memory.memory_type,
                _normalize(memory.title),
                _normalize(memory.content),
            )
            canonical = lanes.get(key)
            if canonical is None:
                lanes[key] = memory
                continue
            canonical.metadata_ = _merge_metadata(canonical.metadata_, memory.metadata_)
            canonical.importance = max(canonical.importance, memory.importance)
            memory.valid_until = now
            memory.metadata_ = {
                **(memory.metadata_ or {}),
                "duplicate_of": str(canonical.id),
                "retired_by_hygiene_at": now.isoformat(),
            }
            merged += 1
        return merged

    def _propose_semantic_review(self, memories: list[MemoryItem]) -> int:
        active = [memory for memory in memories if memory.valid_until is None]
        proposed = 0
        for index, left in enumerate(active):
            for right in active[index + 1:]:
                if (left.domain_id, left.scope, left.memory_type) != (right.domain_id, right.scope, right.memory_type):
                    continue
                title_score = _overlap(left.title, right.title)
                content_score = _overlap(left.content, right.content)
                if max(title_score, content_score) < 0.72:
                    continue
                pair_key = ":".join(sorted([str(left.id), str(right.id)]))
                existing = next(
                    (
                        proposal
                        for proposal in self.session.scalars(select(MemoryProposal)).all()
                        if (proposal.metadata_ or {}).get("hygiene_pair_key") == pair_key
                    ),
                    None,
                )
                if existing is not None:
                    continue
                self.session.add(MemoryProposal(
                    domain_id=left.domain_id,
                    scope=left.scope,
                    memory_type=left.memory_type,
                    title=f"Review related memories: {left.title}",
                    content=f"A: {left.content}\n\nB: {right.content}",
                    rationale="Durable-memory hygiene found strong overlap. Review whether these reinforce, conflict, or supersede one another.",
                    impact_level="high",
                    status="proposed",
                    source_refs=[{"type": "memory_item", "id": str(left.id)}, {"type": "memory_item", "id": str(right.id)}],
                    metadata_={"hygiene_pair_key": pair_key, "title_overlap": title_score, "content_overlap": content_score},
                ))
                proposed += 1
        return proposed


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _overlap(left: str, right: str) -> float:
    left_terms = set(_normalize(left).split())
    right_terms = set(_normalize(right).split())
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / min(len(left_terms), len(right_terms))


def _merge_metadata(left: dict | None, right: dict | None) -> dict:
    merged = {**(left or {})}
    refs: list = []
    for metadata in (left or {}, right or {}):
        for ref in metadata.get("source_refs") or []:
            if ref not in refs:
                refs.append(ref)
    merged["source_refs"] = refs
    merged["reinforced_by_hygiene_at"] = datetime.now(UTC).isoformat()
    merged["reinforcement_count"] = int(merged.get("reinforcement_count") or 0) + 1
    return merged
