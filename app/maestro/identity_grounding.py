"""Authoritative identity graph and compact prompt grounding for Maestro."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Domain, Entity, IdentityNode, IdentityRelationship, OrganizationAlias


@dataclass(frozen=True)
class IdentityGroundingPacket:
    domain_key: str | None
    nodes: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    rendered_text: str


class IdentityGroundingService:
    """Maintains explicit identity facts separately from probabilistic memory retrieval."""

    _cache: dict[tuple[int, str | None], tuple[float, IdentityGroundingPacket]] = {}
    _cache_seconds = 60.0

    def __init__(self, session: Session):
        self.session = session

    def seed_defaults(self) -> None:
        """Create missing defaults without replacing later user-authored edits."""

        settings = get_settings()
        domains = {
            domain.key: domain for domain in self.session.scalars(select(Domain)).all()
        }
        entities = self._organization_entities()
        node_seeds = [
            {
                "key": "person:chris-aliperti",
                "node_type": "person",
                "display_name": settings.user_full_name,
                "aliases": ["Christopher Aliperti", settings.user_email],
                "description": (
                    f"{settings.user_full_name} is the user and principal for whom Maestro works."
                ),
                "metadata": {
                    "is_maestro_user": True,
                    "email": settings.user_email,
                    "first_person_references": ["I", "me", "my", "myself"],
                },
            },
            {
                "key": "system:maestro",
                "node_type": "system",
                "display_name": "Maestro",
                "aliases": [],
                "description": "Maestro is Chris Aliperti's system-level orchestration assistant.",
                "metadata": {},
            },
            {
                "key": "organization:praxis-defense",
                "node_type": "organization",
                "display_name": "Praxis Defense",
                "aliases": ["Praxis"],
                "description": "Praxis Defense is Chris Aliperti's company.",
                "domain_key": "praxis",
                "entity_id": entities.get("praxis defense"),
                "metadata": {},
            },
            {
                "key": "organization:perti-laboratories",
                "node_type": "organization",
                "display_name": "Perti Laboratories",
                "aliases": ["Perti Labs", "Perti"],
                "description": "Perti Laboratories is a company started by Chris Aliperti.",
                "domain_key": "perti-laboratories",
                "entity_id": entities.get("perti laboratories"),
                "metadata": {},
            },
            {
                "key": "domain:maestro-development",
                "node_type": "domain",
                "display_name": "Maestro Development",
                "aliases": ["Maestro dev"],
                "description": "The Maestro Development domain covers the Maestro system itself.",
                "domain_key": "maestro-development",
                "metadata": {},
            },
            {
                "key": "domain:praxis",
                "node_type": "domain",
                "display_name": "Praxis",
                "aliases": [],
                "description": "The Praxis domain represents Chris's work through Praxis Defense.",
                "domain_key": "praxis",
                "metadata": {},
            },
            {
                "key": "domain:perti-laboratories",
                "node_type": "domain",
                "display_name": "Perti Laboratories",
                "aliases": ["Perti"],
                "description": "The Perti Laboratories domain represents Chris's Perti work.",
                "domain_key": "perti-laboratories",
                "metadata": {},
            },
            {
                "key": "domain:personal",
                "node_type": "domain",
                "display_name": "Personal",
                "aliases": [],
                "description": (
                    "The Personal domain represents Chris's personal context and obligations."
                ),
                "domain_key": "personal",
                "metadata": {},
            },
            {
                "key": "domain:usma",
                "node_type": "domain",
                "display_name": "USMA",
                "aliases": ["West Point"],
                "description": "USMA is a professional and academic context connected to Chris.",
                "domain_key": "usma",
                "metadata": {},
            },
        ]
        nodes: dict[str, IdentityNode] = {}
        changed = False
        for seed in node_seeds:
            node = self.session.scalar(
                select(IdentityNode).where(IdentityNode.key == seed["key"])
            )
            domain = domains.get(str(seed.get("domain_key") or ""))
            if node is None:
                node = IdentityNode(
                    key=str(seed["key"]),
                    node_type=str(seed["node_type"]),
                    display_name=str(seed["display_name"]),
                    aliases=list(seed["aliases"]),
                    description=str(seed["description"]),
                    domain_id=domain.id if domain is not None else None,
                    entity_id=seed.get("entity_id"),
                    is_authoritative=True,
                    metadata_={"seeded": True, **dict(seed["metadata"])},
                )
                self.session.add(node)
                changed = True
            else:
                if node.domain_id is None and domain is not None:
                    node.domain_id = domain.id
                    changed = True
                if node.entity_id is None and seed.get("entity_id") is not None:
                    node.entity_id = seed["entity_id"]
                    changed = True
            nodes[node.key] = node
        self.session.flush()

        source_refs = [
            {
                "source_system": "user_confirmed",
                "source_id": "maestro-product-direction",
                "confidence": 1.0,
            }
        ]
        relationship_seeds = [
            (
                "maestro-assists-chris",
                "system:maestro",
                "person:chris-aliperti",
                "assistant_to",
                "Maestro works for Chris Aliperti, addresses him directly, and explains what "
                "it did or plans to do from that perspective.",
            ),
            (
                "chris-owns-praxis",
                "person:chris-aliperti",
                "organization:praxis-defense",
                "owns",
                "Praxis Defense is Chris Aliperti's company; treat Praxis as his organization, "
                "not an unrelated third party.",
            ),
            (
                "chris-founded-perti",
                "person:chris-aliperti",
                "organization:perti-laboratories",
                "founded",
                "Chris Aliperti started Perti Laboratories; Perti Labs and Perti refer to that "
                "company.",
            ),
            (
                "chris-affiliated-usma",
                "person:chris-aliperti",
                "domain:usma",
                "professional_context",
                "USMA and West Point references concern Chris's professional and academic "
                "obligations; do not invent an unstated title.",
            ),
            (
                "praxis-domain-organization",
                "domain:praxis",
                "organization:praxis-defense",
                "represents",
                "The Praxis domain contains context and work for Praxis Defense.",
            ),
            (
                "perti-domain-organization",
                "domain:perti-laboratories",
                "organization:perti-laboratories",
                "represents",
                "The Perti Laboratories domain contains context and work for Perti Laboratories.",
            ),
        ]
        for key, subject_key, object_key, relationship_type, description in relationship_seeds:
            relationship = self.session.scalar(
                select(IdentityRelationship).where(IdentityRelationship.key == key)
            )
            if relationship is None:
                self.session.add(
                    IdentityRelationship(
                        key=key,
                        subject_node_id=nodes[subject_key].id,
                        object_node_id=nodes[object_key].id,
                        relationship_type=relationship_type,
                        description=description,
                        confidence=1.0,
                        is_current=True,
                        source_refs=source_refs,
                        metadata_={"seeded": True},
                    )
                )
                changed = True
        if changed:
            self.session.commit()
            self.clear_cache()

    def build_packet(
        self,
        *,
        domain_key: str | None = None,
        force_refresh: bool = False,
    ) -> IdentityGroundingPacket:
        self.seed_defaults()
        bind = self.session.get_bind()
        cache_key = (id(bind), domain_key)
        cached = self._cache.get(cache_key)
        if not force_refresh and cached and time.monotonic() - cached[0] < self._cache_seconds:
            return cached[1]

        all_nodes = list(
            self.session.scalars(
                select(IdentityNode)
                .where(IdentityNode.is_authoritative.is_(True))
                .order_by(IdentityNode.node_type, IdentityNode.display_name)
            ).all()
        )
        domain = (
            self.session.scalar(select(Domain).where(Domain.key == domain_key))
            if domain_key
            else None
        )
        core_keys = {"person:chris-aliperti", "system:maestro"}
        if domain is None:
            nodes = all_nodes
        else:
            nodes = [
                node
                for node in all_nodes
                if node.key in core_keys or node.domain_id == domain.id
            ]
        node_ids = {node.id for node in nodes}
        relationships = list(
            self.session.scalars(
                select(IdentityRelationship)
                .where(
                    IdentityRelationship.is_current.is_(True),
                    IdentityRelationship.subject_node_id.in_(node_ids),
                    IdentityRelationship.object_node_id.in_(node_ids),
                )
                .order_by(IdentityRelationship.key)
            ).all()
        ) if node_ids else []
        nodes = [node for node in all_nodes if node.id in node_ids]
        nodes_by_id = {node.id: node for node in nodes}
        node_payloads = [self._node_payload(node) for node in nodes]
        relationship_payloads = [
            self._relationship_payload(relationship, nodes_by_id)
            for relationship in relationships
        ]
        rendered = self._render(
            domain_key=domain_key,
            nodes=node_payloads,
            relationships=relationship_payloads,
        )
        packet = IdentityGroundingPacket(
            domain_key=domain_key,
            nodes=node_payloads,
            relationships=relationship_payloads,
            rendered_text=rendered,
        )
        self._cache[cache_key] = (time.monotonic(), packet)
        return packet

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()

    def _organization_entities(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        entities = self.session.scalars(select(Entity)).all()
        for entity in entities:
            result[entity.normalized_name] = entity.id
        aliases = self.session.execute(
            select(OrganizationAlias.normalized_alias, OrganizationAlias.entity_id)
        ).all()
        for normalized_alias, entity_id in aliases:
            result.setdefault(normalized_alias, entity_id)
        return result

    def _node_payload(self, node: IdentityNode) -> dict[str, Any]:
        return {
            "id": str(node.id),
            "key": node.key,
            "type": node.node_type,
            "display_name": node.display_name,
            "aliases": node.aliases,
            "description": node.description,
            "domain_id": str(node.domain_id) if node.domain_id else None,
            "entity_id": str(node.entity_id) if node.entity_id else None,
            "metadata": node.metadata_,
        }

    def _relationship_payload(
        self,
        relationship: IdentityRelationship,
        nodes_by_id: dict[Any, IdentityNode],
    ) -> dict[str, Any]:
        subject = nodes_by_id[relationship.subject_node_id]
        object_ = nodes_by_id[relationship.object_node_id]
        return {
            "id": str(relationship.id),
            "key": relationship.key,
            "subject_key": subject.key,
            "subject": subject.display_name,
            "relationship_type": relationship.relationship_type,
            "object_key": object_.key,
            "object": object_.display_name,
            "description": relationship.description,
            "confidence": relationship.confidence,
            "source_refs": relationship.source_refs,
        }

    def _render(
        self,
        *,
        domain_key: str | None,
        nodes: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
    ) -> str:
        settings = get_settings()
        lines = [
            "Treat these user-confirmed facts as authoritative and higher priority than model "
            "inference or conflicting low-confidence retrieval:",
            (
                f'- The user is {settings.user_full_name}. In a user message, "I", "me", and '
                f'"my" refer to {settings.user_full_name}. Address him as '
                f"{settings.user_display_name}."
            ),
            "- Keep Chris Aliperti distinct from any other person named Chris; do not create a "
            "contact record for the Maestro user.",
        ]
        descriptions: list[str] = []
        seen: set[str] = set()
        for relationship in relationships:
            description = str(relationship["description"]).strip()
            if description and description not in seen:
                descriptions.append(f"- {description}")
                seen.add(description)
        if not descriptions:
            for node in nodes:
                description = str(node["description"]).strip()
                if description and description not in seen:
                    descriptions.append(f"- {description}")
                    seen.add(description)
        lines.extend(descriptions)
        if domain_key:
            for node in nodes:
                if node["type"] != "domain":
                    continue
                description = str(node["description"]).strip()
                if description and description not in seen:
                    lines.append(f"- {description}")
                    seen.add(description)
        if domain_key:
            lines.append(
                f"- Current agent domain: {domain_key}. Do not retrieve or infer unrelated "
                "domain details."
            )
        return "\n".join(lines)
