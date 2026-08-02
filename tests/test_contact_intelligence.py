from sqlalchemy.orm import Session

from app.db.models import (
    Contact,
    ContactInteraction,
    ContactOrganizationAffiliation,
    Entity,
    RoutedItem,
)
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.memory.contact_intelligence import ContactIntelligenceService, _cosine_similarity
from app.memory.routed_service import RoutedMemoryService


def _contact_item(
    session: Session,
    *,
    title: str,
    content: str,
    source_id: str,
    metadata: dict,
) -> RoutedItem:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    item = RoutedItem(
        domain_id=praxis.id,
        route_type="contact",
        title=title,
        content=content,
        priority="normal",
        status="open",
        source_refs=[{"type": "gmail_message", "message_id": source_id}],
        metadata_=metadata,
    )
    session.add(item)
    session.commit()
    return item


def test_contact_promotion_creates_interaction_and_affiliation(session: Session) -> None:
    item = _contact_item(
        session,
        title="Jane Smith",
        content="Jane Smith is the partner lead at Example Corp and discussed invoice automation.",
        source_id="msg-contact-1",
        metadata={
            "name": "Jane Smith",
            "email": "jane@example.com",
            "organization": "Example Corp",
            "role": "Partner Lead",
            "interaction_type": "email",
            "channel": "email",
            "direction": "inbound",
        },
    )

    RoutedMemoryService(session, enable_llm_resolver=False).promote_items([item])

    contact = session.query(Contact).one()
    interaction = session.query(ContactInteraction).one()
    affiliation = session.query(ContactOrganizationAffiliation).one()
    organization = session.query(Entity).one()
    assert interaction.contact_id == contact.id
    assert interaction.routed_item_id == item.id
    assert interaction.channel == "email"
    assert "invoice automation" in interaction.summary
    assert affiliation.contact_id == contact.id
    assert affiliation.entity_id == organization.id
    assert affiliation.role == "Partner Lead"


def test_contact_search_uses_organization_and_interaction_context(session: Session) -> None:
    item = _contact_item(
        session,
        title="Jane Smith",
        content="Jane Smith at Example Corp discussed invoice automation with Praxis last week.",
        source_id="msg-contact-2",
        metadata={
            "name": "Jane Smith",
            "email": "jane@example.com",
            "organization": "Example Corp",
            "role": "Finance Partner",
            "interaction_type": "email",
        },
    )
    RoutedMemoryService(session, enable_llm_resolver=False).promote_items([item])
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None

    results = ContactIntelligenceService(session).search(
        "who at Example Corp discussed invoice automation last week",
        domain_id=praxis.id,
        use_semantic=False,
    )

    assert results
    assert results[0].contact.name == "Jane Smith"
    assert "organization or role match" in results[0].match_reasons
    assert results[0].payload["interactions"][0]["summary"].startswith("Jane Smith")


def test_contact_resolution_uses_exact_phone_across_name_variants(session: Session) -> None:
    first = _contact_item(
        session,
        title="Benjamin Daniels",
        content="Benjamin Daniels can be reached at (202) 555-0199.",
        source_id="msg-contact-3",
        metadata={"name": "Benjamin Daniels", "phone": "(202) 555-0199"},
    )
    second = _contact_item(
        session,
        title="Ben Daniels",
        content="Ben Daniels shared a program update. Phone: 202-555-0199.",
        source_id="msg-contact-4",
        metadata={"name": "Ben Daniels", "phone": "202-555-0199"},
    )

    results = RoutedMemoryService(session, enable_llm_resolver=False).promote_items([first, second])

    assert session.query(Contact).count() == 1
    assert session.query(ContactInteraction).count() == 2
    assert results[1].action == "updated"
    assert second.metadata_["resolution"]["strategy"] == "phone"


def test_contact_cosine_similarity_accepts_pgvector_array_shape() -> None:
    class AmbiguousVector(list):
        def __bool__(self):
            raise ValueError("array truth value is ambiguous")

    assert _cosine_similarity([1.0, 0.0], AmbiguousVector([1.0, 0.0])) == 1.0
