"""Federated, explainable retrieval across Maestro's durable context stores."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib import error, request

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Artifact,
    CalendarEvent,
    Contact,
    ContactAlias,
    ContactDomainNote,
    ContactEmbedding,
    DecisionRecord,
    Domain,
    Entity,
    EntityDomainNote,
    Idea,
    IdentityNode,
    MemoryEmbedding,
    MemoryItem,
    OrganizationAlias,
    OrganizationEmbedding,
    Report,
    RetrievalDocument,
    Todo,
    WorkflowRunLogEntry,
)
from app.memory.embeddings import EmbeddingClient, build_embedding_client
from app.memory.ingestion import memory_allowed_for_target
from app.prompts import load_prompt

STORE_NAMES = {
    "memory", "contacts", "organizations", "events", "todos", "ideas", "decisions",
    "reports", "run_log", "artifacts", "identity",
}


class RetrievalQueryPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    domains: list[str] = Field(default_factory=list)
    stores: list[str] = Field(default_factory=lambda: sorted(STORE_NAMES))
    query_variants: list[str] = Field(default_factory=list, max_length=3)
    entities: list[str] = Field(default_factory=list)
    time_horizon: str | None = None
    current_truth: bool = True
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = ""


@dataclass(frozen=True)
class FederatedRetrievalRequest:
    query_text: str
    audience: Literal["maestro", "agent"] = "maestro"
    domain_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    egress_target: Literal["human", "local", "external"] = "external"
    stores: set[str] | None = None
    max_items: int = 12
    max_chars: int = 5000
    use_semantic: bool = True


@dataclass(frozen=True)
class FederatedResult:
    document: RetrievalDocument
    domain_key: str | None
    score: float
    lexical_score: float
    semantic_similarity: float | None
    recency_score: float
    reasons: list[str]


@dataclass(frozen=True)
class FederatedContextBundle:
    request: FederatedRetrievalRequest
    plan: RetrievalQueryPlan
    results: list[FederatedResult]
    rendered_text: str
    used_chars: int
    dropped_count: int
    policy_filtered_count: int
    semantic_status: str
    store_counts: dict[str, int]


@dataclass(frozen=True)
class RetrievalIndexSyncResult:
    projected: int
    created: int
    updated: int
    unchanged: int
    archived: int
    embedded: int
    embedding_failures: int


@dataclass
class _Projection:
    key: str
    store: str
    source_id: str
    domain_id: uuid.UUID | None
    title: str
    content: str
    status: str = "active"
    source_timestamp: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    trust_score: float = 0.7
    importance: float = 0.5
    relationship_weight: float = 0.0
    policy: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None


class RetrievalQueryRouter:
    def route(
        self,
        *,
        query_text: str,
        available_domains: list[str],
        forced_domain: str | None,
    ) -> RetrievalQueryPlan:
        settings = get_settings()
        fallback = self._fallback(query_text, available_domains, forced_domain)
        if settings.retrieval_router_provider != "ollama":
            return fallback
        payload = {
            "model": settings.retrieval_router_model,
            "stream": False,
            "format": RetrievalQueryPlan.model_json_schema(),
            "messages": [
                {"role": "system", "content": load_prompt("retrieval_query_router.md")},
                {"role": "user", "content": json.dumps({
                    "query": query_text,
                    "available_domains": available_domains,
                    "forced_domain": forced_domain,
                })},
            ],
        }
        req = request.Request(
            f"{settings.retrieval_router_base_url.rstrip('/')}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=settings.retrieval_router_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body.get("message", {}).get("content")
            plan = RetrievalQueryPlan.model_validate_json(content)
        except (OSError, error.URLError, TimeoutError, json.JSONDecodeError, ValidationError, ValueError, TypeError):
            return fallback
        allowed_domains = set(available_domains)
        plan.domains = [value for value in plan.domains if value in allowed_domains]
        plan.stores = [value for value in plan.stores if value in STORE_NAMES]
        plan.query_variants = [value.strip() for value in plan.query_variants if value.strip()][:3]
        if forced_domain:
            plan.domains = [forced_domain]
        if not plan.domains and forced_domain:
            plan.domains = [forced_domain]
        if not plan.stores:
            plan.stores = fallback.stores
        return plan

    def _fallback(self, query: str, domains: list[str], forced_domain: str | None) -> RetrievalQueryPlan:
        terms = set(_tokens(query))
        stores: list[str] = ["memory", "identity"]
        hints = {
            "contacts": {"who", "contact", "person", "people", "know"},
            "organizations": {"company", "organization", "works", "employer"},
            "events": {"meeting", "calendar", "when", "schedule", "event"},
            "todos": {"todo", "task", "due", "deadline", "owe"},
            "ideas": {"idea", "brainstorm", "think"},
            "decisions": {"decision", "decided", "why"},
            "reports": {"report", "research", "learned", "summary"},
            "run_log": {"ran", "workflow", "completed", "failed"},
            "artifacts": {"file", "artifact", "document", "code"},
        }
        stores.extend(store for store, words in hints.items() if terms & words)
        if len(stores) == 2:
            stores.extend(["contacts", "organizations", "events", "todos", "reports"])
        matched_domains = [domain for domain in domains if domain.replace("-", " ") in query.lower()]
        return RetrievalQueryPlan(
            domains=[forced_domain] if forced_domain else matched_domains,
            stores=list(dict.fromkeys(stores)),
            query_variants=[query],
            current_truth=True,
            confidence=0.45,
            reason="Conservative local fallback routing.",
        )


class FederatedIndexService:
    def __init__(self, session: Session, *, embedding_client: EmbeddingClient | None = None):
        self.session = session
        self.embedding_client = embedding_client

    def sync(self, *, embed_missing: bool = True) -> RetrievalIndexSyncResult:
        projections = list(self._projections())
        existing = {
            document.document_key: document
            for document in self.session.scalars(select(RetrievalDocument)).all()
        }
        seen: set[str] = set()
        created = updated = unchanged = embedded = failures = 0
        client = self.embedding_client
        if embed_missing and client is None:
            try:
                client = build_embedding_client()
            except Exception:
                client = None
        for projection in projections:
            seen.add(projection.key)
            content_hash = _hash(f"{projection.title}\n{projection.content}")
            document = existing.get(projection.key)
            changed = document is None or document.content_hash != content_hash
            if document is None:
                document = RetrievalDocument(document_key=projection.key, store=projection.store, source_id=projection.source_id, title=projection.title, content=projection.content, content_hash=content_hash)
                self.session.add(document)
                created += 1
            elif changed:
                updated += 1
            else:
                unchanged += 1
            for attr, value in (
                ("store", projection.store), ("source_id", projection.source_id), ("domain_id", projection.domain_id),
                ("title", projection.title), ("content", projection.content), ("status", projection.status),
                ("source_timestamp", projection.source_timestamp), ("valid_from", projection.valid_from),
                ("valid_until", projection.valid_until), ("trust_score", projection.trust_score),
                ("importance", projection.importance), ("relationship_weight", projection.relationship_weight),
                ("content_hash", content_hash), ("policy", projection.policy), ("provenance", projection.provenance),
                ("metadata_", projection.metadata),
            ):
                setattr(document, attr, value)
            if projection.embedding is not None:
                document.embedding = projection.embedding
                document.embedding_provider = projection.embedding_provider
                document.embedding_model = projection.embedding_model
                document.embedding_dimensions = len(projection.embedding)
            elif changed and client is not None and embed_missing:
                try:
                    vector = client.embed(f"{projection.title}\n{projection.content}"[:12000])
                    document.embedding = vector
                    document.embedding_provider = client.provider
                    document.embedding_model = client.model
                    document.embedding_dimensions = len(vector)
                    embedded += 1
                except Exception:
                    failures += 1
        archived = 0
        for key, document in existing.items():
            if key not in seen and document.status != "inactive":
                document.status = "inactive"
                archived += 1
        self.session.flush()
        return RetrievalIndexSyncResult(len(projections), created, updated, unchanged, archived, embedded, failures)

    def _projections(self):
        domains = {domain.id: domain.key for domain in self.session.scalars(select(Domain)).all()}
        memory_embeddings = {item.memory_item_id: item for item in self.session.scalars(select(MemoryEmbedding)).all()}
        for item in self.session.scalars(select(MemoryItem)).all():
            metadata = item.metadata_ or {}
            embedding = memory_embeddings.get(item.id)
            yield _Projection(
                key=f"memory:{item.id}", store="memory", source_id=str(item.id), domain_id=item.domain_id,
                title=item.title, content=item.content, status="active" if item.valid_until is None or _aware(item.valid_until) > datetime.now(UTC) else "superseded",
                source_timestamp=_parse_datetime(metadata.get("source_timestamp")) or item.created_at,
                valid_from=item.valid_from, valid_until=item.valid_until, trust_score=_trust(metadata),
                importance=item.importance, relationship_weight=0.2 if item.scope == "global" else 0.0,
                policy=_policy(metadata), provenance={"source_refs": metadata.get("source_refs", []), **metadata},
                metadata={"scope": item.scope, "memory_type": item.memory_type, "impact_level": item.impact_level, "domain_key": domains.get(item.domain_id)},
                embedding=list(embedding.embedding) if embedding else None,
                embedding_provider=embedding.provider if embedding else None,
                embedding_model=embedding.model if embedding else None,
            )
        contact_aliases: dict[uuid.UUID, list[str]] = {}
        for alias in self.session.scalars(select(ContactAlias)).all():
            contact_aliases.setdefault(alias.contact_id, []).append(alias.alias)
        contact_embeddings = {item.contact_id: item for item in self.session.scalars(select(ContactEmbedding)).all()}
        notes_by_contact: dict[uuid.UUID, list[ContactDomainNote]] = {}
        for note in self.session.scalars(select(ContactDomainNote)).all():
            notes_by_contact.setdefault(note.contact_id, []).append(note)
        for contact in self.session.scalars(select(Contact)).all():
            notes = notes_by_contact.get(contact.id) or [None]
            for note in notes:
                domain_id = note.domain_id if note else None
                suffix = f":{domain_id}" if domain_id else ""
                aliases = ", ".join(contact_aliases.get(contact.id, []))
                content = "\n".join(value for value in [contact.summary, note.notes if note else None, f"Email: {contact.email}" if contact.email else None, f"Phone: {contact.phone}" if contact.phone else None, f"Aliases: {aliases}" if aliases else None] if value)
                embedding = contact_embeddings.get(contact.id)
                yield _Projection(key=f"contact:{contact.id}{suffix}", store="contacts", source_id=str(contact.id), domain_id=domain_id, title=contact.name, content=content or contact.name, status=contact.status, source_timestamp=contact.last_contact_at or contact.updated_at, trust_score=_trust(contact.provenance), importance=0.65, relationship_weight=0.35, policy=_policy(contact.provenance), provenance={"source_refs": contact.source_refs, **contact.provenance}, metadata={"aliases": contact_aliases.get(contact.id, []), "email": contact.email, "domain_key": domains.get(domain_id)}, embedding=list(embedding.embedding) if embedding else None, embedding_provider=embedding.provider if embedding else None, embedding_model=embedding.model if embedding else None)
        org_aliases: dict[uuid.UUID, list[str]] = {}
        for alias in self.session.scalars(select(OrganizationAlias)).all():
            org_aliases.setdefault(alias.entity_id, []).append(alias.alias)
        org_embeddings = {item.entity_id: item for item in self.session.scalars(select(OrganizationEmbedding)).all()}
        notes_by_org: dict[uuid.UUID, list[EntityDomainNote]] = {}
        for note in self.session.scalars(select(EntityDomainNote)).all():
            notes_by_org.setdefault(note.entity_id, []).append(note)
        for entity in self.session.scalars(select(Entity)).all():
            notes = notes_by_org.get(entity.id) or [None]
            for note in notes:
                domain_id = note.domain_id if note else None
                suffix = f":{domain_id}" if domain_id else ""
                aliases = ", ".join(org_aliases.get(entity.id, []))
                content = "\n".join(value for value in [entity.summary, note.notes if note else None, f"Website: {entity.website}" if entity.website else None, f"Aliases: {aliases}" if aliases else None] if value)
                embedding = org_embeddings.get(entity.id)
                yield _Projection(key=f"organization:{entity.id}{suffix}", store="organizations", source_id=str(entity.id), domain_id=domain_id, title=entity.name, content=content or entity.name, status=entity.status, source_timestamp=entity.updated_at, trust_score=_trust(entity.provenance), importance=0.65, relationship_weight=0.3, policy=_policy(entity.provenance), provenance={"source_refs": entity.source_refs, **entity.provenance}, metadata={"aliases": org_aliases.get(entity.id, []), "domain_key": domains.get(domain_id)}, embedding=list(embedding.embedding) if embedding else None, embedding_provider=embedding.provider if embedding else None, embedding_model=embedding.model if embedding else None)
        yield from self._simple_projections(CalendarEvent, "events", lambda item: "\n".join(value for value in [item.summary, _date_line(item.start_at, item.end_at), item.location, item.conferencing_url] if value), lambda item: item.start_at, domains)
        yield from self._simple_projections(Todo, "todos", lambda item: "\n".join(value for value in [item.description, f"Due: {item.due_at.isoformat()}" if item.due_at else None, f"Owner: {item.owner_ref or item.owner_type}"] if value), lambda item: item.due_at or item.updated_at, domains)
        yield from self._simple_projections(Idea, "ideas", lambda item: item.content, lambda item: item.updated_at, domains)
        yield from self._simple_projections(DecisionRecord, "decisions", lambda item: "\n".join(value for value in [item.decision, item.rationale] if value), lambda item: item.updated_at, domains)
        for report in self.session.scalars(select(Report)).all():
            if not (report.structured_data or {}).get("archived"):
                yield _Projection(key=f"report:{report.id}", store="reports", source_id=str(report.id), domain_id=report.domain_id, title=report.title, content="\n".join(value for value in [report.summary, report.body_markdown] if value), source_timestamp=report.created_at, trust_score=_trust(report.structured_data), importance=0.62, policy=_policy(report.structured_data), provenance=report.structured_data, metadata={"report_type": report.report_type, "domain_key": domains.get(report.domain_id)})
        for entry in self.session.scalars(select(WorkflowRunLogEntry).where(WorkflowRunLogEntry.status != "archived")).all():
            yield _Projection(key=f"run_log:{entry.id}", store="run_log", source_id=str(entry.id), domain_id=entry.domain_id, title=entry.title, content=entry.summary, status=entry.status, source_timestamp=entry.run_completed_at or entry.created_at, trust_score=_trust(entry.metadata_), importance=0.45, policy=_policy(entry.metadata_), provenance=entry.metadata_, metadata={"workflow_run_id": str(entry.workflow_run_id), "domain_key": domains.get(entry.domain_id)})
        for artifact in self.session.scalars(select(Artifact)).all():
            yield _Projection(key=f"artifact:{artifact.id}", store="artifacts", source_id=str(artifact.id), domain_id=None, title=artifact.name, content=f"{artifact.artifact_type}\n{artifact.uri}\n{artifact.mime_type or ''}", source_timestamp=artifact.created_at, trust_score=_trust(artifact.metadata_), importance=0.4, policy=_policy(artifact.metadata_), provenance=artifact.metadata_, metadata={"uri": artifact.uri, "artifact_type": artifact.artifact_type})
        for node in self.session.scalars(select(IdentityNode).where(IdentityNode.is_authoritative.is_(True))).all():
            yield _Projection(key=f"identity:{node.id}", store="identity", source_id=str(node.id), domain_id=node.domain_id, title=node.display_name, content="\n".join([node.description, f"Aliases: {', '.join(node.aliases)}"]), source_timestamp=node.updated_at, trust_score=1.0, importance=0.95, relationship_weight=1.0, policy={"egress_policy": "external_allowed"}, provenance={"authoritative": True}, metadata={"node_type": node.node_type, "domain_key": domains.get(node.domain_id)})

    def _simple_projections(self, model, store: str, content_fn, timestamp_fn, domains):
        for item in self.session.scalars(select(model)).all():
            provenance = item.provenance or {}
            yield _Projection(key=f"{store[:-1]}:{item.id}", store=store, source_id=str(item.id), domain_id=item.domain_id, title=item.title, content=content_fn(item) or item.title, status=item.status, source_timestamp=timestamp_fn(item), trust_score=_trust(provenance), importance=0.58, policy=_policy(provenance), provenance={"source_refs": item.source_refs, **provenance}, metadata={"domain_key": domains.get(item.domain_id)})


class FederatedRetrievalService:
    def __init__(self, session: Session, *, embedding_client: EmbeddingClient | None = None):
        self.session = session
        self.embedding_client = embedding_client

    def retrieve(self, request_data: FederatedRetrievalRequest) -> FederatedContextBundle:
        FederatedIndexService(self.session, embedding_client=self.embedding_client).sync(embed_missing=request_data.use_semantic)
        domains = self.session.scalars(select(Domain).where(Domain.is_active.is_(True))).all()
        by_key = {domain.key: domain for domain in domains}
        forced_domain = next((domain.key for domain in domains if domain.id == request_data.domain_id), None)
        plan = RetrievalQueryRouter().route(query_text=request_data.query_text, available_domains=list(by_key), forced_domain=forced_domain if request_data.audience == "agent" else None)
        domain_ids = {by_key[key].id for key in plan.domains if key in by_key}
        stores = set(plan.stores) & (request_data.stores or STORE_NAMES)
        query = select(RetrievalDocument).where(RetrievalDocument.status.not_in({"inactive", "archived", "superseded"}))
        if request_data.audience == "agent":
            if request_data.domain_id is None:
                raise ValueError("Agent retrieval requires a domain.")
            query = query.where(or_(RetrievalDocument.domain_id == request_data.domain_id, RetrievalDocument.store == "identity"))
        elif domain_ids:
            query = query.where(or_(RetrievalDocument.domain_id.in_(domain_ids), RetrievalDocument.domain_id.is_(None)))
        if stores:
            query = query.where(RetrievalDocument.store.in_(stores))
        documents = self.session.scalars(query).all()
        now = datetime.now(UTC)
        policy_filtered = 0
        visible: list[RetrievalDocument] = []
        for document in documents:
            if document.valid_until and _aware(document.valid_until) <= now:
                continue
            if not memory_allowed_for_target({"source_policy": document.policy, **document.provenance}, request_data.egress_target):
                policy_filtered += 1
                continue
            visible.append(document)
        variants = [request_data.query_text, *plan.query_variants]
        query_text = " ".join(dict.fromkeys(value.strip() for value in variants if value.strip()))
        query_embedding, semantic_status = self._query_embedding(query_text, request_data.use_semantic)
        domain_keys = {domain.id: domain.key for domain in domains}
        scored = [self._score(document, query_text, query_embedding, domain_keys.get(document.domain_id), set(plan.domains), now) for document in visible]
        scored.sort(key=lambda result: (result.score, result.document.source_timestamp or datetime.min.replace(tzinfo=UTC)), reverse=True)
        deduped: list[FederatedResult] = []
        fingerprints: set[str] = set()
        for result in scored:
            fingerprint = _hash(_normalize(f"{result.document.title} {result.document.content}"))[:20]
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            deduped.append(result)
        selected = deduped[: request_data.max_items]
        rendered, included = self._render(selected, request_data.max_chars)
        selected = selected[:included]
        counts: dict[str, int] = {}
        for result in selected:
            counts[result.document.store] = counts.get(result.document.store, 0) + 1
        return FederatedContextBundle(request_data, plan, selected, rendered, len(rendered), max(0, len(deduped) - included), policy_filtered, semantic_status, counts)

    def _query_embedding(self, text: str, enabled: bool):
        if not enabled:
            return None, "disabled"
        try:
            client = self.embedding_client or build_embedding_client()
            return client.embed(text), f"enabled:{client.provider}/{client.model}"
        except Exception as exc:
            return None, f"unavailable:{exc}"

    def _score(self, document: RetrievalDocument, query: str, query_embedding: list[float] | None, domain_key: str | None, selected_domains: set[str], now: datetime) -> FederatedResult:
        lexical = _lexical(query, f"{document.title} {document.content}")
        semantic = _cosine(query_embedding, list(document.embedding)) if query_embedding is not None and document.embedding is not None else None
        domain_score = 1.0 if domain_key and domain_key in selected_domains else (0.75 if document.domain_id is None else 0.45)
        recency = _recency(document.source_timestamp, now)
        semantic_value = max(0.0, semantic or 0.0)
        score = 0.45 * semantic_value + 0.20 * lexical + 0.10 * domain_score + 0.08 * document.trust_score + 0.07 * recency + 0.07 * document.importance + 0.03 * document.relationship_weight
        reasons = [f"semantic {semantic_value:.2f}" if semantic is not None else "semantic unavailable", f"lexical {lexical:.2f}", f"domain {domain_score:.2f}", f"trust {document.trust_score:.2f}", f"recency {recency:.2f}", f"importance {document.importance:.2f}"]
        return FederatedResult(document, domain_key, round(score, 4), round(lexical, 4), round(semantic, 4) if semantic is not None else None, round(recency, 4), reasons)

    def _render(self, results: list[FederatedResult], max_chars: int):
        blocks: list[str] = []
        used = 0
        for result in results:
            document = result.document
            content = " ".join(document.content.split())
            snippet = content[:650] + ("..." if len(content) > 650 else "")
            block = f"### [{document.store}] {document.title}\n{snippet}\nSource: {document.document_key}; domain: {result.domain_key or 'global'}; score: {result.score:.2f}"
            addition = len(block) + (2 if blocks else 0)
            if blocks and used + addition > max_chars:
                break
            if not blocks and len(block) > max_chars:
                block = block[:max_chars]
                addition = len(block)
            blocks.append(block)
            used += addition
        return "\n\n".join(blocks), len(blocks)


def federated_bundle_payload(bundle: FederatedContextBundle) -> dict[str, Any]:
    return {
        "query": bundle.request.query_text,
        "plan": bundle.plan.model_dump(),
        "semantic_status": bundle.semantic_status,
        "used_chars": bundle.used_chars,
        "dropped_count": bundle.dropped_count,
        "policy_filtered_count": bundle.policy_filtered_count,
        "store_counts": bundle.store_counts,
        "rendered_text": bundle.rendered_text,
        "results": [
            {
                "id": str(result.document.id), "document_key": result.document.document_key,
                "store": result.document.store, "source_id": result.document.source_id,
                "domain_key": result.domain_key, "title": result.document.title,
                "content": result.document.content, "score": result.score,
                "lexical_score": result.lexical_score, "semantic_similarity": result.semantic_similarity,
                "recency_score": result.recency_score, "score_reasons": result.reasons,
                "source_timestamp": result.document.source_timestamp.isoformat() if result.document.source_timestamp else None,
                "trust_score": result.document.trust_score, "importance": result.document.importance,
                "policy": result.document.policy, "provenance": result.document.provenance,
                "metadata": result.document.metadata_,
            }
            for result in bundle.results
        ],
    }


def _tokens(value: str):
    return re.findall(r"[a-z0-9]+", value.lower())


def _normalize(value: str):
    return " ".join(_tokens(value))


def _hash(value: str):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lexical(query: str, text: str):
    query_terms = set(_tokens(query))
    if not query_terms:
        return 0.0
    text_terms = set(_tokens(text))
    return len(query_terms & text_terms) / len(query_terms)


def _cosine(left, right):
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right, strict=False)) / denominator if denominator else 0.0


def _recency(value: datetime | None, now: datetime):
    if value is None:
        return 0.35
    days = max(0.0, (now - _aware(value)).total_seconds() / 86400)
    return 1.0 / (1.0 + math.log1p(days) / 4.0)


def _aware(value: datetime):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _policy(value):
    if not isinstance(value, dict):
        return {"egress_policy": "external_allowed"}
    nested = value.get("source_policy") or value.get("policy")
    if isinstance(nested, dict):
        return nested
    return {key: value[key] for key in ("egress_policy", "sensitivity", "trust_level", "transfer_method") if key in value} or {"egress_policy": "external_allowed"}


def _trust(value):
    policy = _policy(value)
    return {"authoritative": 1.0, "user_reviewed": 0.95, "user_provided": 0.9, "system_observed": 0.8, "agent_inferred": 0.65}.get(str(policy.get("trust_level") or ""), 0.72)


def _date_line(start, end):
    if not start:
        return ""
    return f"When: {start.isoformat()}" + (f" to {end.isoformat()}" if end else "")
