import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Artifact, MemoryEmbedding, MemoryItem, MemoryLink, SeedPackage
from app.memory.embeddings import EmbeddingClient, build_embedding_client

RetrievalAudience = Literal["maestro", "agent"]
RetrievalMode = Literal["broad", "balanced", "strict"]
MemoryContextProfile = Literal[
    "agent_prompt",
    "daily_standup",
    "direct_user_question",
    "curator_context",
    "memory_debug",
]


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
    use_semantic: bool = True
    mode: RetrievalMode = "balanced"
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
    query_relevance: float
    semantic_similarity: float | None
    score_reasons: list[str]
    provenance: MemoryProvenance
    links: list[RetrievedMemoryLink] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryRetrievalResult:
    query: MemoryRetrievalQuery
    results: list[RetrievedMemory]
    total_visible: int
    filtered_count: int
    semantic_status: str


@dataclass(frozen=True)
class MemoryContextBundleRequest:
    profile: MemoryContextProfile = "agent_prompt"
    audience: RetrievalAudience = "agent"
    domain_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    query_text: str | None = None
    memory_types: set[str] | None = None
    min_importance: float | None = None
    use_semantic: bool = True
    max_items: int = 12
    max_chars: int = 4000


@dataclass(frozen=True)
class MemoryContextSnippet:
    memory: MemoryItem
    excerpt: str
    score: float
    query_relevance: float
    semantic_similarity: float | None
    score_reasons: list[str]
    provenance: MemoryProvenance
    links: list[RetrievedMemoryLink] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryContextSection:
    key: str
    label: str
    snippets: list[MemoryContextSnippet]
    used_chars: int


@dataclass(frozen=True)
class MemoryContextBundle:
    request: MemoryContextBundleRequest
    retrieval_query: MemoryRetrievalQuery
    sections: list[MemoryContextSection]
    rendered_text: str
    total_visible: int
    filtered_count: int
    retrieved_count: int
    included_count: int
    dropped_count: int
    used_chars: int
    max_chars: int
    semantic_status: str


class MemoryRetrievalError(ValueError):
    pass


class MemoryRetrievalService:
    def __init__(self, session: Session, *, embedding_client: EmbeddingClient | None = None):
        self.session = session
        self.embedding_client = embedding_client

    def retrieve(self, query: MemoryRetrievalQuery) -> MemoryRetrievalResult:
        self._validate_query(query)
        visible_memories = self._visible_memories(query)
        semantic_scores, semantic_status = self._semantic_scores(visible_memories, query)
        scored = [
            self._score_memory(memory, query, semantic_scores.get(memory.id))
            for memory in visible_memories
        ]
        filtered = [result for result in scored if self._passes_query_gate(result, query)]
        filtered.sort(key=lambda result: (result.score, result.memory.created_at), reverse=True)
        limited = filtered[: max(0, query.limit)]
        if query.include_links and limited:
            limited = self._with_links(limited, query)
        return MemoryRetrievalResult(
            query=query,
            results=limited,
            total_visible=len(visible_memories),
            filtered_count=len(scored) - len(filtered),
            semantic_status=semantic_status,
        )

    def build_context_bundle(
        self,
        request: MemoryContextBundleRequest,
    ) -> MemoryContextBundle:
        self._validate_context_bundle_request(request)
        retrieval_query = self._context_retrieval_query(request)
        result = self.retrieve(retrieval_query)
        selected = self._select_context_memories(result.results, request)
        sections = self._context_sections(selected)
        rendered_text = self._render_context_sections(sections)
        used_chars = sum(section.used_chars for section in sections)
        included_count = sum(len(section.snippets) for section in sections)
        return MemoryContextBundle(
            request=request,
            retrieval_query=retrieval_query,
            sections=sections,
            rendered_text=rendered_text,
            total_visible=result.total_visible,
            filtered_count=result.filtered_count,
            retrieved_count=len(result.results),
            included_count=included_count,
            dropped_count=max(0, len(result.results) - included_count),
            used_chars=used_chars,
            max_chars=request.max_chars,
            semantic_status=result.semantic_status,
        )

    def _validate_query(self, query: MemoryRetrievalQuery) -> None:
        if query.audience == "agent" and query.domain_id is None:
            raise MemoryRetrievalError("Agent retrieval requires a domain_id.")
        if query.limit < 1:
            raise MemoryRetrievalError("Retrieval limit must be at least 1.")
        if query.mode not in {"broad", "balanced", "strict"}:
            raise MemoryRetrievalError("Retrieval mode must be broad, balanced, or strict.")

    def _validate_context_bundle_request(self, request: MemoryContextBundleRequest) -> None:
        if request.profile not in _CONTEXT_PROFILES:
            raise MemoryRetrievalError(
                "Context profile must be agent_prompt, daily_standup, "
                "direct_user_question, curator_context, or memory_debug."
            )
        if request.audience == "agent" and request.domain_id is None:
            raise MemoryRetrievalError("Agent context bundles require a domain_id.")
        if request.max_items < 1:
            raise MemoryRetrievalError("Context bundle max_items must be at least 1.")
        if request.max_chars < 200:
            raise MemoryRetrievalError("Context bundle max_chars must be at least 200.")

    def _context_retrieval_query(
        self,
        request: MemoryContextBundleRequest,
    ) -> MemoryRetrievalQuery:
        profile = _CONTEXT_PROFILES[request.profile]
        memory_types = request.memory_types
        if memory_types is None and profile["memory_types"] is not None:
            memory_types = set(profile["memory_types"])
        return MemoryRetrievalQuery(
            audience=request.audience,
            domain_id=request.domain_id,
            agent_id=request.agent_id,
            query_text=request.query_text,
            memory_types=memory_types,
            min_importance=request.min_importance,
            include_agent_memory=bool(profile["include_agent_memory"]),
            include_session_memory=bool(profile["include_session_memory"]),
            include_links=bool(profile["include_links"]),
            use_semantic=request.use_semantic,
            mode=profile["mode"],  # type: ignore[arg-type]
            limit=max(request.max_items * 4, request.max_items),
        )

    def _select_context_memories(
        self,
        results: list[RetrievedMemory],
        request: MemoryContextBundleRequest,
    ) -> list[MemoryContextSnippet]:
        selected_by_id: dict[uuid.UUID, MemoryContextSnippet] = {}
        remaining_chars = request.max_chars

        def add_result(result: RetrievedMemory) -> None:
            nonlocal remaining_chars
            if len(selected_by_id) >= request.max_items or result.memory.id in selected_by_id:
                return
            max_excerpt_chars = min(900, remaining_chars - len(result.memory.title) - 120)
            if max_excerpt_chars <= 20:
                return
            excerpt = _truncate(result.memory.content, max_excerpt_chars)
            snippet = MemoryContextSnippet(
                memory=result.memory,
                excerpt=excerpt,
                score=result.score,
                query_relevance=result.query_relevance,
                semantic_similarity=result.semantic_similarity,
                score_reasons=result.score_reasons,
                provenance=result.provenance,
                links=result.links,
            )
            cost = _snippet_cost(snippet)
            if cost > remaining_chars:
                return
            selected_by_id[result.memory.id] = snippet
            remaining_chars -= min(cost, remaining_chars)

        by_scope: dict[str, list[RetrievedMemory]] = {}
        for result in results:
            by_scope.setdefault(result.memory.scope, []).append(result)

        for scope in _SECTION_ORDER:
            for result in by_scope.get(scope, [])[:1]:
                add_result(result)

        for result in results:
            add_result(result)

        selected_order = {memory_id: index for index, memory_id in enumerate(selected_by_id)}
        return sorted(
            selected_by_id.values(),
            key=lambda snippet: (
                _SECTION_ORDER_INDEX.get(snippet.memory.scope, 99),
                selected_order[snippet.memory.id],
            ),
        )

    def _context_sections(
        self,
        snippets: list[MemoryContextSnippet],
    ) -> list[MemoryContextSection]:
        grouped: dict[str, list[MemoryContextSnippet]] = {}
        for snippet in snippets:
            grouped.setdefault(snippet.memory.scope, []).append(snippet)
        sections: list[MemoryContextSection] = []
        for key in _SECTION_ORDER:
            section_snippets = grouped.get(key, [])
            if not section_snippets:
                continue
            sections.append(
                MemoryContextSection(
                    key=key,
                    label=_SECTION_LABELS.get(key, key.replace("_", " ").title()),
                    snippets=section_snippets,
                    used_chars=sum(_snippet_cost(snippet) for snippet in section_snippets),
                )
            )
        return sections

    def _render_context_sections(self, sections: list[MemoryContextSection]) -> str:
        lines: list[str] = []
        for section in sections:
            lines.append(f"[{section.label}]")
            for snippet in section.snippets:
                memory = snippet.memory
                source = snippet.provenance.processed_path
                source_text = f" source={source}" if source else ""
                lines.append(
                    "- "
                    f"{memory.title} "
                    f"(id={memory.id}, type={memory.memory_type}, "
                    f"importance={memory.importance:.2f}, impact={memory.impact_level}, "
                    f"score={snippet.score:.2f}{source_text}): "
                    f"{snippet.excerpt}"
                )
            lines.append("")
        return "\n".join(lines).strip()

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

    def _score_memory(
        self,
        memory: MemoryItem,
        query: MemoryRetrievalQuery,
        semantic_similarity: float | None,
    ) -> RetrievedMemory:
        reasons: list[str] = []
        query_relevance = 0.0
        importance_score = max(0.0, min(1.0, memory.importance or 0.0))
        has_query = bool(query.query_text and _tokens(query.query_text))
        semantic_weight = 0.45 if semantic_similarity is not None else 0.0
        lexical_weight = 0.25 if semantic_similarity is not None else 0.55
        score = importance_score * (0.18 if has_query else 0.55)
        reasons.append(f"importance {importance_score:.2f}")

        recency_score = self._recency_score(memory.created_at)
        score += recency_score * (0.08 if has_query else 0.15)
        reasons.append(f"recency {recency_score:.2f}")

        impact_score = self._impact_score(memory.impact_level)
        score += impact_score * (0.07 if has_query else 0.10)
        reasons.append(f"impact {memory.impact_level}")

        if has_query and query.query_text:
            query_relevance = self._lexical_score(memory, query.query_text)
            score += query_relevance * lexical_weight
            reasons.append(f"query relevance {query_relevance:.2f}")
        if semantic_similarity is not None:
            score += semantic_similarity * semantic_weight
            reasons.append(f"semantic similarity {semantic_similarity:.2f}")

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
            query_relevance=round(query_relevance, 4),
            semantic_similarity=None
            if semantic_similarity is None
            else round(semantic_similarity, 4),
            score_reasons=reasons,
            provenance=self._provenance(memory),
        )

    def _passes_query_gate(self, result: RetrievedMemory, query: MemoryRetrievalQuery) -> bool:
        if not query.query_text or not _tokens(query.query_text):
            return True
        if query.mode == "broad":
            return True
        if query.mode == "strict":
            return result.query_relevance >= 0.5
        if result.query_relevance > 0 or (result.semantic_similarity or 0) >= 0.45:
            return True
        return self._is_exceptional_context(result.memory)

    def _semantic_scores(
        self,
        memories: list[MemoryItem],
        query: MemoryRetrievalQuery,
    ) -> tuple[dict[uuid.UUID, float], str]:
        if not query.use_semantic:
            return {}, "disabled"
        if not query.query_text or not query.query_text.strip() or not memories:
            return {}, "not_requested"

        settings = get_settings()
        try:
            client = self.embedding_client or build_embedding_client()
            query_embedding = client.embed(query.query_text)
        except Exception as exc:
            return {}, f"failed: {exc}"

        memory_ids = [memory.id for memory in memories]
        embeddings = self.session.scalars(
            select(MemoryEmbedding).where(
                MemoryEmbedding.memory_item_id.in_(memory_ids),
                MemoryEmbedding.provider == client.provider,
                MemoryEmbedding.model == client.model,
            )
        ).all()
        if not embeddings:
            return {}, f"unavailable: no embeddings for {settings.embedding_provider}/{settings.embedding_model}"
        return {
            embedding.memory_item_id: _cosine_similarity(query_embedding, embedding.embedding)
            for embedding in embeddings
        }, "enabled"

    def _is_exceptional_context(self, memory: MemoryItem) -> bool:
        return (
            memory.scope in {"global", "maestro_session"}
            and (memory.importance or 0.0) >= 0.9
            and memory.impact_level in {"high", "very_high"}
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
        title_terms = set(_tokens(memory.title))
        memory_terms = set(_tokens(f"{memory.title} {memory.content} {memory.memory_type}"))
        if not memory_terms:
            return 0.0
        overlap = query_terms & memory_terms
        if not overlap:
            return 0.0
        title_overlap = query_terms & title_terms
        base_score = len(overlap) / len(query_terms)
        title_bonus = 0.25 * (len(title_overlap) / len(query_terms))
        phrase_bonus = 0.15 if query_text.lower().strip() in f"{memory.title} {memory.content}".lower() else 0
        return min(1.0, base_score + title_bonus + phrase_bonus)

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
                query_relevance=result.query_relevance,
                semantic_similarity=result.semantic_similarity,
                score_reasons=result.score_reasons,
                provenance=result.provenance,
                links=links_by_memory.get(result.memory.id, []),
            )
            for result in results
        ]


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2]


_CONTEXT_PROFILES: dict[str, dict[str, Any]] = {
    "agent_prompt": {
        "mode": "broad",
        "memory_types": None,
        "include_agent_memory": True,
        "include_session_memory": True,
        "include_links": True,
    },
    "daily_standup": {
        "mode": "broad",
        "memory_types": {"decision", "fact", "preference", "task", "workflow"},
        "include_agent_memory": True,
        "include_session_memory": True,
        "include_links": True,
    },
    "direct_user_question": {
        "mode": "balanced",
        "memory_types": None,
        "include_agent_memory": False,
        "include_session_memory": True,
        "include_links": True,
    },
    "curator_context": {
        "mode": "broad",
        "memory_types": {"fact", "preference", "decision", "workflow", "relationship"},
        "include_agent_memory": False,
        "include_session_memory": False,
        "include_links": True,
    },
    "memory_debug": {
        "mode": "broad",
        "memory_types": None,
        "include_agent_memory": True,
        "include_session_memory": True,
        "include_links": True,
    },
}

_SECTION_ORDER = ("global", "maestro_session", "domain", "agent")
_SECTION_ORDER_INDEX = {scope: index for index, scope in enumerate(_SECTION_ORDER)}
_SECTION_LABELS = {
    "global": "Global Memory",
    "maestro_session": "Maestro Session Memory",
    "domain": "Domain Memory",
    "agent": "Agent Memory",
}


def _truncate(text: str, max_chars: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max(0, max_chars - 3)].rstrip()}..."


def _snippet_cost(snippet: MemoryContextSnippet) -> int:
    return len(snippet.memory.title) + len(snippet.excerpt) + 120


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


def _cosine_similarity(left: list[float], right) -> float:
    right_values = [float(value) for value in right]
    if not left or not right_values or len(left) != len(right_values):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right_values, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))
