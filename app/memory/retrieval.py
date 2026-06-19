import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Artifact, MemoryItem, MemoryLink, SeedPackage

RetrievalAudience = Literal["maestro", "agent"]


@dataclass(frozen=True)
class MemoryRetrievalQuery:
    audience: RetrievalAudience = "maestro"
    domain_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    query_text: str | None = None
    memory_types: set[str] | None = None
    min_importance: float | None = None
    include_agent_memory: bool = False
    include_session_memory: bool = True
    include_links: bool = True
    limit: int = 12


@dataclass(frozen=True)
class MemoryProvenance:
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    seed_package: dict[str, Any] | None = None
    artifact: dict[str, Any] | None = None
    processed_path: str | None = None


@dataclass(frozen=True)
class RetrievedMemoryLink:
    relation_type: str
    direction: Literal["outgoing", "incoming"]
    memory: MemoryItem
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievedMemory:
    memory: MemoryItem
    score: float
    score_reasons: list[str]
    provenance: MemoryProvenance
    links: list[RetrievedMemoryLink] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryRetrievalResult:
    query: MemoryRetrievalQuery
    results: list[RetrievedMemory]
    total_visible: int


class MemoryRetrievalError(ValueError):
    pass


class MemoryRetrievalService:
    def __init__(self, session: Session):
        self.session = session

    def retrieve(self, query: MemoryRetrievalQuery) -> MemoryRetrievalResult:
        self._validate_query(query)
        visible_memories = self._visible_memories(query)
        scored = [self._score_memory(memory, query) for memory in visible_memories]
        scored.sort(key=lambda result: (result.score, result.memory.created_at), reverse=True)
        limited = scored[: max(0, query.limit)]
        if query.include_links and limited:
            limited = self._with_links(limited, query)
        return MemoryRetrievalResult(
            query=query,
            results=limited,
            total_visible=len(visible_memories),
        )

    def _validate_query(self, query: MemoryRetrievalQuery) -> None:
        if query.audience == "agent" and query.domain_id is None:
            raise MemoryRetrievalError("Agent retrieval requires a domain_id.")
        if query.limit < 1:
            raise MemoryRetrievalError("Retrieval limit must be at least 1.")

    def _visible_memories(self, query: MemoryRetrievalQuery) -> list[MemoryItem]:
        statement = select(MemoryItem).where(*self._active_predicates())
        if query.min_importance is not None:
            statement = statement.where(MemoryItem.importance >= query.min_importance)
        if query.memory_types:
            statement = statement.where(MemoryItem.memory_type.in_(query.memory_types))
        statement = statement.where(self._visibility_predicate(query))
        return list(self.session.scalars(statement).all())

    def _active_predicates(self):
        now = datetime.now(UTC)
        return (
            or_(MemoryItem.valid_from.is_(None), MemoryItem.valid_from <= now),
            or_(MemoryItem.valid_until.is_(None), MemoryItem.valid_until > now),
        )

    def _visibility_predicate(self, query: MemoryRetrievalQuery):
        if query.audience == "agent":
            visible = [
                MemoryItem.scope == "global",
                (MemoryItem.scope == "domain") & (MemoryItem.domain_id == query.domain_id),
            ]
            if query.agent_id is not None:
                visible.append(
                    (MemoryItem.scope == "agent")
                    & (MemoryItem.domain_id == query.domain_id)
                    & (MemoryItem.agent_id == query.agent_id)
                )
            return or_(*visible)

        scopes = ["global", "domain"]
        if query.include_session_memory:
            scopes.append("maestro_session")
        if query.include_agent_memory:
            scopes.append("agent")
        predicate = MemoryItem.scope.in_(scopes)
        if query.domain_id is not None:
            predicate = predicate & or_(
                MemoryItem.scope.in_(["global", "maestro_session"]),
                MemoryItem.domain_id == query.domain_id,
            )
        return predicate

    def _score_memory(self, memory: MemoryItem, query: MemoryRetrievalQuery) -> RetrievedMemory:
        reasons: list[str] = []
        importance_score = max(0.0, min(1.0, memory.importance or 0.0))
        score = importance_score * 0.55
        reasons.append(f"importance {importance_score:.2f}")

        recency_score = self._recency_score(memory.created_at)
        score += recency_score * 0.15
        reasons.append(f"recency {recency_score:.2f}")

        impact_score = self._impact_score(memory.impact_level)
        score += impact_score * 0.10
        reasons.append(f"impact {memory.impact_level}")

        if query.query_text:
            lexical_score = self._lexical_score(memory, query.query_text)
            score += lexical_score * 0.35
            reasons.append(f"lexical match {lexical_score:.2f}")

        if query.domain_id is not None and memory.domain_id == query.domain_id:
            score += 0.08
            reasons.append("domain match")
        if query.agent_id is not None and memory.agent_id == query.agent_id:
            score += 0.08
            reasons.append("agent match")
        if memory.scope == "global":
            score += 0.03
            reasons.append("global context")

        return RetrievedMemory(
            memory=memory,
            score=round(score, 4),
            score_reasons=reasons,
            provenance=self._provenance(memory),
        )

    def _recency_score(self, created_at: datetime | None) -> float:
        if created_at is None:
            return 0.0
        now = datetime.now(UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age_days = max(0.0, (now - created_at).total_seconds() / 86400)
        return 1 / (1 + math.log1p(age_days))

    def _impact_score(self, impact_level: str) -> float:
        return {
            "very_high": 1.0,
            "high": 0.85,
            "medium": 0.65,
            "low": 0.45,
        }.get(impact_level, 0.4)

    def _lexical_score(self, memory: MemoryItem, query_text: str) -> float:
        query_terms = set(_tokens(query_text))
        if not query_terms:
            return 0.0
        memory_terms = set(_tokens(f"{memory.title} {memory.content} {memory.memory_type}"))
        if not memory_terms:
            return 0.0
        overlap = query_terms & memory_terms
        return len(overlap) / len(query_terms)

    def _provenance(self, memory: MemoryItem) -> MemoryProvenance:
        metadata = memory.metadata_ or {}
        source_refs = _list_of_dicts(metadata.get("source_refs"))
        seed_package = self._seed_package_payload(_uuid_from_metadata(metadata, "seed_package_id"))
        artifact = self._artifact_payload(_uuid_from_metadata(metadata, "artifact_id"))
        return MemoryProvenance(
            source_refs=source_refs,
            seed_package=seed_package,
            artifact=artifact,
            processed_path=metadata.get("processed_path") or metadata.get("artifact_uri"),
        )

    def _seed_package_payload(self, seed_package_id: uuid.UUID | None) -> dict[str, Any] | None:
        if seed_package_id is None:
            return None
        seed_package = self.session.get(SeedPackage, seed_package_id)
        if seed_package is None:
            return None
        return {
            "id": str(seed_package.id),
            "name": seed_package.name,
            "source_type": seed_package.source_type,
            "status": seed_package.status,
        }

    def _artifact_payload(self, artifact_id: uuid.UUID | None) -> dict[str, Any] | None:
        if artifact_id is None:
            return None
        artifact = self.session.get(Artifact, artifact_id)
        if artifact is None:
            return None
        return {
            "id": str(artifact.id),
            "name": artifact.name,
            "artifact_type": artifact.artifact_type,
            "uri": artifact.uri,
            "mime_type": artifact.mime_type,
        }

    def _with_links(
        self,
        results: list[RetrievedMemory],
        query: MemoryRetrievalQuery,
    ) -> list[RetrievedMemory]:
        result_ids = {result.memory.id for result in results}
        links = self.session.scalars(
            select(MemoryLink).where(
                or_(
                    MemoryLink.source_memory_id.in_(result_ids),
                    MemoryLink.target_memory_id.in_(result_ids),
                )
            )
        ).all()
        visible_by_id = {memory.id: memory for memory in self._visible_memories(query)}
        links_by_memory: dict[uuid.UUID, list[RetrievedMemoryLink]] = {
            result.memory.id: [] for result in results
        }
        for link in links:
            if link.source_memory_id in result_ids and link.target_memory_id in visible_by_id:
                links_by_memory[link.source_memory_id].append(
                    RetrievedMemoryLink(
                        relation_type=link.relation_type,
                        direction="outgoing",
                        memory=visible_by_id[link.target_memory_id],
                        metadata=link.metadata_ or {},
                    )
                )
            if link.target_memory_id in result_ids and link.source_memory_id in visible_by_id:
                links_by_memory[link.target_memory_id].append(
                    RetrievedMemoryLink(
                        relation_type=link.relation_type,
                        direction="incoming",
                        memory=visible_by_id[link.source_memory_id],
                        metadata=link.metadata_ or {},
                    )
                )
        return [
            RetrievedMemory(
                memory=result.memory,
                score=result.score,
                score_reasons=result.score_reasons,
                provenance=result.provenance,
                links=links_by_memory.get(result.memory.id, []),
            )
            for result in results
        ]


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2]


def _uuid_from_metadata(metadata: dict[str, Any], key: str) -> uuid.UUID | None:
    value = metadata.get(key)
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
