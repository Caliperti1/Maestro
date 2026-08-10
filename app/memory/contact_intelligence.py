"""Contact identity, interaction, affiliation, and hybrid retrieval services."""

import hashlib
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    CalendarEvent,
    CalendarEventAttendee,
    Contact,
    ContactAlias,
    ContactDomainNote,
    ContactEmbedding,
    ContactInteraction,
    ContactOrganizationAffiliation,
    ContactRelationship,
    Domain,
    Entity,
)
from app.memory.embeddings import EmbeddingClient, build_embedding_client


@dataclass(frozen=True)
class ContactSearchResult:
    contact: Contact
    score: float
    match_reasons: list[str]
    semantic_similarity: float | None
    payload: dict[str, Any]


class ContactEmbeddingService:
    def __init__(self, session: Session, *, client: EmbeddingClient | None = None):
        self.session = session
        self.client = client

    def upsert(self, contact: Contact) -> str:
        try:
            client = self.client or build_embedding_client()
        except Exception as exc:
            return f"failed: {exc}"
        source_text = contact_profile_text(self.session, contact)
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        existing = self.session.scalar(
            select(ContactEmbedding).where(
                ContactEmbedding.contact_id == contact.id,
                ContactEmbedding.provider == client.provider,
                ContactEmbedding.model == client.model,
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
                ContactEmbedding(
                    contact_id=contact.id,
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
        statement = select(Contact).where(Contact.status != "archived").order_by(Contact.updated_at.desc())
        if limit is not None:
            statement = statement.limit(limit)
        counts = {"written": 0, "current": 0, "failed": 0}
        for contact in self.session.scalars(statement).all():
            status = self.upsert(contact)
            key = status if status in {"written", "current"} else "failed"
            counts[key] += 1
        self.session.commit()
        return counts


class ContactIntelligenceService:
    """Retrieves contacts using identity, graph, temporal, lexical, and semantic evidence."""

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
    ) -> list[ContactSearchResult]:
        query = query_text.strip()
        contacts = self._visible_contacts(domain_id)
        semantic_scores = self._semantic_scores(contacts, query) if use_semantic and query else {}
        results = [
            self._score_contact(contact, query, domain_id, semantic_scores.get(contact.id))
            for contact in contacts
        ]
        if query:
            results = [result for result in results if result.score >= 0.12]
        results.sort(key=lambda result: (result.score, result.contact.updated_at), reverse=True)
        return results[:limit]

    def get(self, contact_id: uuid.UUID, *, domain_id: uuid.UUID | None = None) -> dict[str, Any]:
        contact = self.session.get(Contact, contact_id)
        if contact is None or contact.status == "archived":
            raise ValueError("Contact not found.")
        if domain_id is not None and contact.id not in {item.id for item in self._visible_contacts(domain_id)}:
            raise ValueError("Contact is not visible in this domain.")
        return self.contact_payload(contact, domain_id=domain_id)

    def contact_payload(
        self,
        contact: Contact,
        *,
        domain_id: uuid.UUID | None = None,
        interaction_limit: int = 20,
    ) -> dict[str, Any]:
        aliases = list(
            self.session.scalars(
                select(ContactAlias)
                .where(ContactAlias.contact_id == contact.id)
                .order_by(ContactAlias.source.desc(), ContactAlias.normalized_alias)
            )
        )
        note_statement = select(ContactDomainNote, Domain).join(
            Domain, Domain.id == ContactDomainNote.domain_id, isouter=True
        ).where(ContactDomainNote.contact_id == contact.id)
        if domain_id is not None:
            note_statement = note_statement.where(ContactDomainNote.domain_id == domain_id)
        notes = self.session.execute(note_statement).all()
        interaction_statement = select(ContactInteraction, Domain).join(
            Domain, Domain.id == ContactInteraction.domain_id, isouter=True
        ).where(ContactInteraction.contact_id == contact.id)
        if domain_id is not None:
            interaction_statement = interaction_statement.where(ContactInteraction.domain_id == domain_id)
        interactions = self.session.execute(
            interaction_statement.order_by(ContactInteraction.occurred_at.desc()).limit(interaction_limit)
        ).all()
        upcoming_statement = (
            select(CalendarEventAttendee, CalendarEvent, Domain)
            .join(CalendarEvent, CalendarEvent.id == CalendarEventAttendee.event_id)
            .join(Domain, Domain.id == CalendarEvent.domain_id, isouter=True)
            .where(
                CalendarEventAttendee.contact_id == contact.id,
                CalendarEvent.status.notin_(["archived", "cancelled"]),
                CalendarEvent.start_at >= datetime.now(UTC),
            )
        )
        if domain_id is not None:
            upcoming_statement = upcoming_statement.where(CalendarEvent.domain_id == domain_id)
        upcoming_events = self.session.execute(
            upcoming_statement.order_by(CalendarEvent.start_at).limit(20)
        ).all()
        affiliation_statement = (
            select(ContactOrganizationAffiliation, Entity, Domain)
            .join(Entity, Entity.id == ContactOrganizationAffiliation.entity_id)
            .join(Domain, Domain.id == ContactOrganizationAffiliation.domain_id, isouter=True)
            .where(
                ContactOrganizationAffiliation.contact_id == contact.id,
                ContactOrganizationAffiliation.status == "active",
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
        relationships = self._relationship_payloads(contact, domain_id)
        return {
            "id": str(contact.id),
            "name": contact.name,
            "email": contact.email,
            "phone": contact.phone,
            "linkedin": contact.linkedin,
            "organization_entity_id": str(contact.organization_entity_id) if contact.organization_entity_id else None,
            "summary": contact.summary,
            "origination": contact.origination,
            "last_contact_at": contact.last_contact_at.isoformat() if contact.last_contact_at else None,
            "scheduled_event_ids": [str(event.id) for _, event, _ in upcoming_events],
            "source_refs": contact.source_refs,
            "provenance": contact.provenance,
            "status": contact.status,
            "metadata": contact.metadata_,
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
            "interactions": [
                {
                    "id": str(interaction.id),
                    "domain_key": domain.key if domain else None,
                    "interaction_type": interaction.interaction_type,
                    "channel": interaction.channel,
                    "direction": interaction.direction,
                    "occurred_at": interaction.occurred_at.isoformat(),
                    "summary": interaction.summary,
                    "source_refs": interaction.source_refs,
                    "provenance": interaction.provenance,
                }
                for interaction, domain in interactions
            ],
            "upcoming_events": [
                {
                    "id": str(event.id),
                    "title": event.title,
                    "domain_key": domain.key if domain else None,
                    "start_at": event.start_at.isoformat() if event.start_at else None,
                    "end_at": event.end_at.isoformat() if event.end_at else None,
                    "location": event.location,
                    "response_status": attendee.response_status,
                }
                for attendee, event, domain in upcoming_events
            ],
            "affiliations": [
                {
                    "id": str(affiliation.id),
                    "entity_id": str(entity.id),
                    "organization": entity.name,
                    "domain_key": domain.key if domain else None,
                    "role": affiliation.role,
                    "relationship_type": affiliation.relationship_type,
                    "is_primary": affiliation.is_primary,
                    "source_refs": affiliation.source_refs,
                }
                for affiliation, entity, domain in affiliations
            ],
            "relationships": relationships,
            "created_at": contact.created_at.isoformat() if contact.created_at else None,
        }

    def _visible_contacts(self, domain_id: uuid.UUID | None) -> list[Contact]:
        statement = select(Contact).where(Contact.status != "archived")
        if domain_id is None:
            return list(self.session.scalars(statement).all())
        visible_ids = set(
            self.session.scalars(
                select(ContactDomainNote.contact_id).where(ContactDomainNote.domain_id == domain_id)
            )
        )
        visible_ids.update(
            self.session.scalars(
                select(ContactInteraction.contact_id).where(ContactInteraction.domain_id == domain_id)
            )
        )
        visible_ids.update(
            self.session.scalars(
                select(ContactOrganizationAffiliation.contact_id).where(
                    ContactOrganizationAffiliation.domain_id == domain_id
                )
            )
        )
        return list(self.session.scalars(statement.where(Contact.id.in_(visible_ids))).all()) if visible_ids else []

    def _score_contact(
        self,
        contact: Contact,
        query: str,
        domain_id: uuid.UUID | None,
        semantic_similarity: float | None,
    ) -> ContactSearchResult:
        payload = self.contact_payload(contact, domain_id=domain_id, interaction_limit=8)
        if not query:
            return ContactSearchResult(contact, 0.5, ["recent contact"], semantic_similarity, payload)
        normalized_query = _normalize(query)
        query_tokens = _tokens(query)
        reasons: list[str] = []
        score = 0.0
        identity_values = [
            contact.name,
            contact.email or "",
            contact.phone or "",
            contact.linkedin or "",
            *(payload["aliases"] or []),
        ]
        normalized_identities = {_normalize(value) for value in identity_values if value}
        if normalized_query in normalized_identities:
            score += 1.0
            reasons.append("exact identity or alias match")
        elif any(normalized_query and normalized_query in value for value in normalized_identities):
            score += 0.65
            reasons.append("partial identity match")

        organization_text = " ".join(
            f"{item['organization']} {item['role']}" for item in payload["affiliations"]
        )
        relationship_text = " ".join(
            f"{item['name']} {item['relationship_type']} {item['description']}"
            for item in payload["relationships"]
        )
        interaction_text = " ".join(item["summary"] for item in payload["interactions"])
        profile_text = " ".join(
            [contact.name, contact.summary or "", organization_text, relationship_text, interaction_text]
        )
        lexical = _token_overlap(query_tokens, _tokens(profile_text))
        if lexical:
            score += lexical * 0.6
            reasons.append(f"profile overlap {lexical:.2f}")
        if organization_text and _token_overlap(query_tokens, _tokens(organization_text)) >= 0.4:
            score += 0.35
            reasons.append("organization or role match")
        if relationship_text and _token_overlap(query_tokens, _tokens(relationship_text)) >= 0.35:
            score += 0.3
            reasons.append("relationship match")
        if _query_is_recent(query) and payload["interactions"]:
            recent = _parse_datetime(payload["interactions"][0]["occurred_at"])
            if recent and recent >= datetime.now(UTC) - timedelta(days=14):
                score += 0.22
                reasons.append("recent interaction match")
        if semantic_similarity is not None:
            score += max(0.0, semantic_similarity) * 0.55
            reasons.append(f"semantic similarity {semantic_similarity:.2f}")
        return ContactSearchResult(
            contact=contact,
            score=round(score, 4),
            match_reasons=reasons or ["weak profile match"],
            semantic_similarity=None if semantic_similarity is None else round(semantic_similarity, 4),
            payload=payload,
        )

    def _semantic_scores(self, contacts: list[Contact], query: str) -> dict[uuid.UUID, float]:
        try:
            client = self.embedding_client or build_embedding_client()
        except Exception:
            return {}
        rows = self.session.scalars(
            select(ContactEmbedding).where(
                ContactEmbedding.contact_id.in_([contact.id for contact in contacts]),
                ContactEmbedding.provider == client.provider,
                ContactEmbedding.model == client.model,
            )
        ).all()
        if not rows:
            return {}
        try:
            query_vector = client.embed(query)
        except Exception:
            return {}
        return {row.contact_id: _cosine_similarity(query_vector, row.embedding) for row in rows}

    def _relationship_payloads(
        self,
        contact: Contact,
        domain_id: uuid.UUID | None,
    ) -> list[dict[str, Any]]:
        statement = select(ContactRelationship).where(
            or_(
                ContactRelationship.contact_id == contact.id,
                ContactRelationship.related_contact_id == contact.id,
            ),
            ContactRelationship.status == "active",
        )
        if domain_id is not None:
            statement = statement.where(
                or_(ContactRelationship.domain_id == domain_id, ContactRelationship.domain_id.is_(None))
            )
        rows = self.session.scalars(statement).all()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            related_id = row.related_contact_id if row.contact_id == contact.id else row.contact_id
            related = self.session.get(Contact, related_id)
            if related is None:
                continue
            payloads.append(
                {
                    "id": str(row.id),
                    "contact_id": str(related.id),
                    "name": related.name,
                    "relationship_type": row.relationship_type,
                    "description": row.description,
                    "confidence": row.confidence,
                    "source_refs": row.source_refs,
                }
            )
        return payloads


def contact_profile_text(session: Session, contact: Contact) -> str:
    payload = ContactIntelligenceService(session).contact_payload(contact, interaction_limit=30)
    return "\n".join(
        [
            f"Name: {contact.name}",
            f"Aliases: {', '.join(payload['aliases'])}",
            f"Email: {contact.email or ''}",
            f"Phone: {contact.phone or ''}",
            f"LinkedIn: {contact.linkedin or ''}",
            f"Summary: {contact.summary or ''}",
            "Affiliations: " + "; ".join(
                f"{item['organization']} ({item['role'] or item['relationship_type']})"
                for item in payload["affiliations"]
            ),
            "Relationships: " + "; ".join(
                f"{item['name']}: {item['description']}" for item in payload["relationships"]
            ),
            "Interactions: " + "; ".join(item["summary"] for item in payload["interactions"]),
        ]
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9@.+]+", " ", value.lower())).strip()


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 1}


def _token_overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left) if left else 0.0


def _query_is_recent(query: str) -> bool:
    return bool(re.search(r"\b(recent|recently|last week|this week|last month|yesterday)\b", query.lower()))


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _cosine_similarity(left: list[float], right) -> float:
    right_values = [float(value) for value in right]
    if not left or not right_values or len(left) != len(right_values):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right_values, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))
