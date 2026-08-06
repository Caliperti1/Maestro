"""Organization profiles, affiliations, evidence, and hybrid retrieval."""

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    Contact,
    ContactInteraction,
    ContactOrganizationAffiliation,
    Domain,
    Entity,
    EntityDomainNote,
    OrganizationAlias,
    OrganizationEmbedding,
)
from app.memory.contact_intelligence import _cosine_similarity
from app.memory.embeddings import EmbeddingClient, build_embedding_client


@dataclass(frozen=True)
class OrganizationSearchResult:
    organization: Entity
    score: float
    match_reasons: list[str]
    semantic_similarity: float | None
    payload: dict[str, Any]


class OrganizationEmbeddingService:
    def __init__(self, session: Session, *, client: EmbeddingClient | None = None):
        self.session = session
        self.client = client

    def upsert(self, organization: Entity) -> str:
        try:
            client = self.client or build_embedding_client()
        except Exception as exc:
            return f"failed: {exc}"
        source_text = organization_profile_text(self.session, organization)
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        existing = self.session.scalar(
            select(OrganizationEmbedding).where(
                OrganizationEmbedding.entity_id == organization.id,
                OrganizationEmbedding.provider == client.provider,
                OrganizationEmbedding.model == client.model,
            )
        )
        if existing is not None and existing.source_text_hash == source_hash:
            return "current"
        try:
            vector = client.embed(source_text)
        except Exception as exc:
            return f"failed: {exc}"
        if existing is None:
            self.session.add(
                OrganizationEmbedding(
                    entity_id=organization.id,
                    provider=client.provider,
                    model=client.model,
                    dimensions=len(vector),
                    source_text_hash=source_hash,
                    embedding=vector,
                    metadata_={},
                )
            )
        else:
            existing.dimensions = len(vector)
            existing.source_text_hash = source_hash
            existing.embedding = vector
        self.session.flush()
        return "written"

    def backfill(self, *, limit: int | None = None) -> dict[str, int]:
        statement = select(Entity).where(Entity.status != "archived").order_by(Entity.updated_at.desc())
        if limit is not None:
            statement = statement.limit(limit)
        counts = {"written": 0, "current": 0, "failed": 0}
        for organization in self.session.scalars(statement).all():
            status = self.upsert(organization)
            counts[status if status in {"written", "current"} else "failed"] += 1
        self.session.commit()
        return counts


class OrganizationIntelligenceService:
    def __init__(self, session: Session, *, embedding_client: EmbeddingClient | None = None):
        self.session = session
        self.embedding_client = embedding_client

    def search(
        self,
        query_text: str,
        *,
        domain_id: uuid.UUID | None = None,
        limit: int = 10,
        use_semantic: bool = True,
    ) -> list[OrganizationSearchResult]:
        query = query_text.strip()
        organizations = self._visible_organizations(domain_id)
        semantic_scores = self._semantic_scores(organizations, query) if query and use_semantic else {}
        results = [
            self._score(organization, query, domain_id, semantic_scores.get(organization.id))
            for organization in organizations
        ]
        if query:
            results = [result for result in results if result.score >= 0.12]
        results.sort(key=lambda result: (result.score, result.organization.updated_at), reverse=True)
        return results[:limit]

    def get(self, organization_id: uuid.UUID, *, domain_id: uuid.UUID | None = None) -> dict[str, Any]:
        organization = self.session.get(Entity, organization_id)
        if organization is None or organization.status == "archived":
            raise ValueError("Organization not found.")
        if domain_id is not None and organization.id not in {
            item.id for item in self._visible_organizations(domain_id)
        }:
            raise ValueError("Organization is not visible in this domain.")
        return self.organization_payload(organization, domain_id=domain_id)

    def organization_payload(
        self,
        organization: Entity,
        *,
        domain_id: uuid.UUID | None = None,
        interaction_limit: int = 20,
    ) -> dict[str, Any]:
        aliases = list(
            self.session.scalars(
                select(OrganizationAlias)
                .where(OrganizationAlias.entity_id == organization.id)
                .order_by(OrganizationAlias.source.desc(), OrganizationAlias.normalized_alias)
            )
        )
        note_statement = select(EntityDomainNote, Domain).join(
            Domain, Domain.id == EntityDomainNote.domain_id, isouter=True
        ).where(EntityDomainNote.entity_id == organization.id)
        if domain_id is not None:
            note_statement = note_statement.where(EntityDomainNote.domain_id == domain_id)
        notes = self.session.execute(note_statement).all()
        affiliation_statement = (
            select(ContactOrganizationAffiliation, Contact, Domain)
            .join(Contact, Contact.id == ContactOrganizationAffiliation.contact_id)
            .join(Domain, Domain.id == ContactOrganizationAffiliation.domain_id, isouter=True)
            .where(
                ContactOrganizationAffiliation.entity_id == organization.id,
                ContactOrganizationAffiliation.status == "active",
                Contact.status != "archived",
            )
        )
        if domain_id is not None:
            affiliation_statement = affiliation_statement.where(
                or_(
                    ContactOrganizationAffiliation.domain_id == domain_id,
                    ContactOrganizationAffiliation.domain_id.is_(None),
                )
            )
        affiliations = self.session.execute(affiliation_statement).all()
        contact_ids = [affiliation.contact_id for affiliation, _, _ in affiliations]
        interactions: list[tuple[ContactInteraction, Contact, Domain | None]] = []
        if contact_ids:
            interaction_statement = (
                select(ContactInteraction, Contact, Domain)
                .join(Contact, Contact.id == ContactInteraction.contact_id)
                .join(Domain, Domain.id == ContactInteraction.domain_id, isouter=True)
                .where(ContactInteraction.contact_id.in_(contact_ids))
            )
            if domain_id is not None:
                interaction_statement = interaction_statement.where(ContactInteraction.domain_id == domain_id)
            interactions = list(
                self.session.execute(
                    interaction_statement.order_by(ContactInteraction.occurred_at.desc()).limit(interaction_limit)
                ).all()
            )
        return {
            "id": str(organization.id),
            "name": organization.name,
            "website": organization.website,
            "summary": organization.summary,
            "source_refs": organization.source_refs,
            "provenance": organization.provenance,
            "status": organization.status,
            "metadata": organization.metadata_,
            "aliases": [alias.alias for alias in aliases],
            "alias_records": [
                {"id": str(alias.id), "alias": alias.alias, "source": alias.source}
                for alias in aliases
            ],
            "domain_notes": [
                {
                    "domain_key": domain.key if domain else None,
                    "notes": note.notes,
                    "source_refs": note.source_refs,
                }
                for note, domain in notes
            ],
            "contacts": [
                {
                    "id": str(contact.id),
                    "name": contact.name,
                    "email": contact.email,
                    "domain_key": domain.key if domain else None,
                    "role": affiliation.role,
                    "relationship_type": affiliation.relationship_type,
                    "source_refs": affiliation.source_refs,
                }
                for affiliation, contact, domain in affiliations
            ],
            "interactions": [
                {
                    "id": str(interaction.id),
                    "contact_id": str(contact.id),
                    "contact_name": contact.name,
                    "domain_key": domain.key if domain else None,
                    "interaction_type": interaction.interaction_type,
                    "channel": interaction.channel,
                    "occurred_at": interaction.occurred_at.isoformat(),
                    "summary": interaction.summary,
                    "source_refs": interaction.source_refs,
                }
                for interaction, contact, domain in interactions
            ],
            "created_at": organization.created_at.isoformat() if organization.created_at else None,
        }

    def _visible_organizations(self, domain_id: uuid.UUID | None) -> list[Entity]:
        statement = select(Entity).where(Entity.status != "archived")
        if domain_id is None:
            return list(self.session.scalars(statement).all())
        visible_ids = set(
            self.session.scalars(
                select(EntityDomainNote.entity_id).where(EntityDomainNote.domain_id == domain_id)
            )
        )
        visible_ids.update(
            self.session.scalars(
                select(ContactOrganizationAffiliation.entity_id).where(
                    ContactOrganizationAffiliation.domain_id == domain_id
                )
            )
        )
        return list(self.session.scalars(statement.where(Entity.id.in_(visible_ids))).all()) if visible_ids else []

    def _score(
        self,
        organization: Entity,
        query: str,
        domain_id: uuid.UUID | None,
        semantic_similarity: float | None,
    ) -> OrganizationSearchResult:
        payload = self.organization_payload(organization, domain_id=domain_id, interaction_limit=8)
        if not query:
            return OrganizationSearchResult(organization, 0.5, ["recent organization"], semantic_similarity, payload)
        normalized_query = _normalize(query)
        identity_values = [organization.name, organization.website or "", *(payload["aliases"] or [])]
        normalized_identities = {_normalize(value) for value in identity_values if value}
        reasons: list[str] = []
        score = 0.0
        if normalized_query in normalized_identities:
            score += 1.0
            reasons.append("exact name, website, or alias match")
        elif any(normalized_query and normalized_query in value for value in normalized_identities):
            score += 0.65
            reasons.append("partial organization identity match")
        profile_text = " ".join(
            [
                organization.name,
                organization.website or "",
                organization.summary or "",
                " ".join(f"{item['name']} {item['role']}" for item in payload["contacts"]),
                " ".join(item["summary"] for item in payload["interactions"]),
                " ".join(str(item["notes"] or "") for item in payload["domain_notes"]),
            ]
        )
        lexical = _token_overlap(_tokens(query), _tokens(profile_text))
        if lexical:
            score += lexical * 0.7
            reasons.append(f"profile overlap {lexical:.2f}")
        if semantic_similarity is not None:
            score += max(0.0, semantic_similarity) * 0.55
            reasons.append(f"semantic similarity {semantic_similarity:.2f}")
        return OrganizationSearchResult(
            organization=organization,
            score=round(score, 4),
            match_reasons=reasons or ["weak organization match"],
            semantic_similarity=None if semantic_similarity is None else round(semantic_similarity, 4),
            payload=payload,
        )

    def _semantic_scores(self, organizations: list[Entity], query: str) -> dict[uuid.UUID, float]:
        try:
            client = self.embedding_client or build_embedding_client()
        except Exception:
            return {}
        rows = self.session.scalars(
            select(OrganizationEmbedding).where(
                OrganizationEmbedding.entity_id.in_([organization.id for organization in organizations]),
                OrganizationEmbedding.provider == client.provider,
                OrganizationEmbedding.model == client.model,
            )
        ).all()
        if not rows:
            return {}
        try:
            query_vector = client.embed(query)
        except Exception:
            return {}
        return {row.entity_id: _cosine_similarity(query_vector, row.embedding) for row in rows}


def organization_profile_text(session: Session, organization: Entity) -> str:
    payload = OrganizationIntelligenceService(session).organization_payload(
        organization,
        interaction_limit=30,
    )
    return "\n".join(
        [
            f"Name: {organization.name}",
            f"Aliases: {', '.join(payload['aliases'])}",
            f"Website: {organization.website or ''}",
            f"Summary: {organization.summary or ''}",
            "Contacts: " + "; ".join(
                f"{item['name']} ({item['role'] or item['relationship_type']})"
                for item in payload["contacts"]
            ),
            "Interactions: " + "; ".join(item["summary"] for item in payload["interactions"]),
            "Domain notes: " + "; ".join(str(item["notes"] or "") for item in payload["domain_notes"]),
        ]
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9@.+]+", " ", value.lower())).strip()


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 1}


def _token_overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left) if left else 0.0
