from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Agent,
    Contact,
    ContactAlias,
    ContactHydrationCandidate,
    ContactOrganizationAffiliation,
    Entity,
    OrganizationAlias,
)
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.memory.contact_hydration import ContactHydrationService, _display_name
from app.memory.contact_intelligence import ContactEmbeddingService
from app.memory.organization_intelligence import OrganizationEmbeddingService, OrganizationIntelligenceService
from app.memory.routed_hygiene import RoutedHygieneService


class FakeGmailTools:
    def __init__(self, pages: list[dict]):
        self.pages = list(pages)
        self.requests = []

    def execute_for_task(self, request, *, task):
        self.requests.append(request)
        if request.tool_key == "gmail.message.search":
            output = self.pages.pop(0)
        else:
            output = {"messages": []}
        return SimpleNamespace(status="complete", output=output, error_message=None)


class MisidentifyingHydrationLLM:
    provider = "test"
    model = "test-hydration"

    def structured_response(self, **kwargs):
        return {
            "canonical_name": "Chris Aliperti",
            "contact_aliases": ["Chris Aliperti"],
            "organization": "Example Corp",
            "organization_aliases": [],
            "role": "Partner Lead",
            "summary": "Coordinates partner planning with Praxis.",
            "relationship_context": "Works with Chris on Praxis partner planning.",
            "identity_evidence": ["Chris Aliperti"],
            "confidence": 0.94,
        }


class EvidenceHydrationLLM:
    provider = "test"
    model = "test-evidence-hydration"

    def structured_response(self, **kwargs):
        return {
            "canonical_name": "William Rollins",
            "contact_aliases": ["Will Rollins", "LTC Rollins", "WR"],
            "organization": "US Army",
            "organization_aliases": ["United States Army", "USA-X"],
            "role": "Army partner",
            "summary": "Coordinates Army partner work with Praxis.",
            "relationship_context": "Works with Chris on Praxis partner activity.",
            "identity_evidence": ["William Rollins", "LTC Will Rollins", "US Army"],
            "confidence": 0.96,
        }


class EvidenceGmailTools(FakeGmailTools):
    def execute_for_task(self, request, *, task):
        if request.tool_key == "gmail.thread.get":
            return SimpleNamespace(
                status="complete",
                output={
                    "messages": [
                        {
                            "subject": "Praxis planning",
                            "from": "William Rollins <wrollins@army.mil>",
                            "to": "Chris Aliperti <chris.aliperti@praxis-defense.com>",
                            "date": "Mon, 3 Aug 2026 10:00:00 -0400",
                            "body_text": "William Rollins\nLTC Will Rollins\nUS Army",
                        }
                    ]
                },
                error_message=None,
            )
        return super().execute_for_task(request, task=task)


def _gmail_agent(session: Session):
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    agent = Agent(
        domain_id=praxis.id,
        key="praxis-hydration-test-agent",
        name="Praxis Hydration Test Agent",
        agent_type="domain_agent",
        tool_permissions={"gmail.message.search": {"permission": "use"}, "gmail.thread.get": {"permission": "use"}},
        skill_permissions={},
        capabilities={},
    )
    session.add(agent)
    session.commit()
    return praxis, agent


def _message(message_id: str, *, sender: str, to: str, subject: str = "Partner update") -> dict:
    return {
        "message_id": message_id,
        "thread_id": f"thread-{message_id}",
        "from": sender,
        "to": to,
        "cc": "",
        "subject": subject,
        "date": "Mon, 3 Aug 2026 10:00:00 -0400",
        "snippet": "Discussed partner planning and next steps.",
        "headers": {},
    }


def test_display_name_separates_email_and_cleans_military_headers() -> None:
    assert _display_name("", "william.r.sitze2.mil@army.mil") == (
        "William R Sitze",
        "email_local",
        set(),
    )
    name, source, aliases = _display_name(
        "Rollins, Wallace W LTC USARMY XVIII ABN CORPS (USA)",
        "wallace.w.rollins.mil@army.mil",
    )
    assert name == "Wallace W Rollins"
    assert source == "header"
    assert {"Wallace W Rollins", "LTC Rollins", "LTC Wallace Rollins"} <= aliases


def test_hydration_pages_gmail_and_builds_contact_and_organization_candidates(session: Session) -> None:
    praxis, _ = _gmail_agent(session)
    tools = FakeGmailTools(
        [
            {
                "messages": [
                    _message("1", sender="Jane Smith <jane@example.com>", to="Chris Aliperti <chris.aliperti@praxis-defense.com>"),
                    _message("2", sender="no-reply@example.com", to="Chris Aliperti <chris.aliperti@praxis-defense.com>"),
                ],
                "next_page_token": "page-2",
            },
            {
                "messages": [
                    _message("3", sender="Chris Aliperti <chris.aliperti@praxis-defense.com>", to="Jane Smith <jane@example.com>"),
                ],
                "next_page_token": None,
            },
        ]
    )
    service = ContactHydrationService(session, tool_service=tools)
    job = service.create_job(
        domain_id=praxis.id,
        query="newer_than:30d",
        enable_enrichment=False,
    )

    service.process_once()
    session.refresh(job)
    assert job.status == "scanning"
    assert tools.requests[0].payload["page_token"] is None

    service.process_once()
    session.refresh(job)
    assert job.status == "review"
    assert job.messages_scanned == 3
    assert tools.requests[1].payload["page_token"] == "page-2"

    candidates = list(session.scalars(select(ContactHydrationCandidate).where(ContactHydrationCandidate.job_id == job.id)))
    jane = next(item for item in candidates if item.identity_key == "jane@example.com")
    organization = next(item for item in candidates if item.candidate_type == "organization")
    excluded = next(item for item in candidates if item.identity_key == "no-reply@example.com")
    assert jane.evidence["message_count"] == 2
    assert jane.evidence["inbound_count"] == 1
    assert jane.evidence["outbound_count"] == 1
    assert organization.proposed_data["email_domain"] == "example.com"
    assert excluded.status == "excluded"
    assert not any(item.identity_key == "chris.aliperti@praxis-defense.com" for item in candidates)


def test_hydration_same_name_different_email_requires_review(session: Session) -> None:
    praxis, _ = _gmail_agent(session)
    session.add(Contact(name="Jane Smith", normalized_name="jane smith", email="jane@old.example", source_refs=[], provenance={}, metadata_={}))
    session.commit()
    tools = FakeGmailTools(
        [{"messages": [_message("4", sender="Jane Smith <jane@new.example>", to="Chris Aliperti <chris.aliperti@praxis-defense.com>")], "next_page_token": None}]
    )
    service = ContactHydrationService(session, tool_service=tools)
    job = service.create_job(domain_id=praxis.id, query="newer_than:30d", enable_enrichment=False)

    service.process_once()

    candidate = session.scalar(
        select(ContactHydrationCandidate).where(
            ContactHydrationCandidate.job_id == job.id,
            ContactHydrationCandidate.identity_key == "jane@new.example",
        )
    )
    assert candidate is not None
    assert candidate.action == "needs_review"
    assert candidate.status == "review"


def test_hydration_preserves_existing_canonical_name_and_records_header_variant(
    session: Session,
) -> None:
    praxis, _ = _gmail_agent(session)
    existing = Contact(
        name="William Rollins",
        normalized_name="william rollins",
        email="william.rollins@example.com",
        source_refs=[],
        provenance={},
        metadata_={},
    )
    session.add(existing)
    session.commit()
    tools = FakeGmailTools(
        [
            {
                "messages": [
                    _message(
                        "existing-alias",
                        sender="LTC Will Rollins <william.rollins@example.com>",
                        to="Chris Aliperti <chris.aliperti@praxis-defense.com>",
                    )
                ],
                "next_page_token": None,
            }
        ]
    )
    service = ContactHydrationService(session, tool_service=tools)
    job = service.create_job(
        domain_id=praxis.id,
        query="newer_than:30d",
        enable_enrichment=False,
    )

    service.process_once()

    candidate = session.scalar(
        select(ContactHydrationCandidate).where(
            ContactHydrationCandidate.job_id == job.id,
            ContactHydrationCandidate.identity_key == "william.rollins@example.com",
        )
    )
    assert candidate is not None
    assert candidate.display_name == "William Rollins"
    assert candidate.action == "update"
    assert candidate.proposed_data["aliases"] == ["LTC Rollins", "LTC Will Rollins", "Will Rollins"]


def test_hydration_llm_cannot_overwrite_header_identity_with_maestro_owner(
    session: Session,
) -> None:
    praxis, _ = _gmail_agent(session)
    tools = FakeGmailTools(
        [
            {
                "messages": [
                    _message(
                        "identity-1",
                        sender="Jane Smith <jane@example.com>",
                        to="Chris Aliperti <chris.aliperti@praxis-defense.com>",
                    )
                ],
                "next_page_token": None,
            }
        ]
    )
    service = ContactHydrationService(
        session,
        tool_service=tools,
        llm_factory=lambda profile: MisidentifyingHydrationLLM(),
    )
    job = service.create_job(
        domain_id=praxis.id,
        query="newer_than:30d",
        enable_enrichment=True,
    )

    service.process_once()
    service.process_once()

    candidate = session.scalar(
        select(ContactHydrationCandidate).where(
            ContactHydrationCandidate.job_id == job.id,
            ContactHydrationCandidate.identity_key == "jane@example.com",
        )
    )
    assert candidate is not None
    assert candidate.display_name == "Jane Smith"
    assert candidate.proposed_data["name"] == "Jane Smith"
    assert candidate.proposed_data["aliases"] == []
    assert candidate.proposed_data["organization"] == "Example"


def test_hydration_accepts_only_evidence_backed_contact_and_organization_aliases(
    session: Session,
) -> None:
    praxis, _ = _gmail_agent(session)
    tools = EvidenceGmailTools(
        [
            {
                "messages": [
                    _message(
                        "evidence-1",
                        sender="wrollins@army.mil",
                        to="Chris Aliperti <chris.aliperti@praxis-defense.com>",
                    )
                ],
                "next_page_token": None,
            }
        ]
    )
    service = ContactHydrationService(
        session,
        tool_service=tools,
        llm_factory=lambda profile: EvidenceHydrationLLM(),
    )
    job = service.create_job(
        domain_id=praxis.id,
        query="newer_than:30d",
        enable_enrichment=True,
    )

    service.process_once()
    service.process_once()

    contact = session.scalar(
        select(ContactHydrationCandidate).where(
            ContactHydrationCandidate.job_id == job.id,
            ContactHydrationCandidate.identity_key == "wrollins@army.mil",
        )
    )
    organization = session.scalar(
        select(ContactHydrationCandidate).where(
            ContactHydrationCandidate.job_id == job.id,
            ContactHydrationCandidate.identity_key == "domain:army.mil",
        )
    )
    assert contact is not None
    assert contact.display_name == "William Rollins"
    assert contact.proposed_data["email"] == "wrollins@army.mil"
    assert contact.proposed_data["aliases"] == ["LTC Rollins", "Will Rollins"]
    assert "WR" not in contact.proposed_data["aliases"]
    assert organization is not None
    assert organization.display_name == "US Army"
    assert organization.proposed_data["aliases"] == ["Army"]


def test_hydration_promotes_contacts_and_organizations_in_one_run(session: Session, monkeypatch) -> None:
    praxis, _ = _gmail_agent(session)
    tools = FakeGmailTools(
        [{"messages": [_message("5", sender="Jane Smith <jane@example.com>", to="Chris Aliperti <chris.aliperti@praxis-defense.com>")], "next_page_token": None}]
    )
    monkeypatch.setattr(ContactEmbeddingService, "upsert", lambda self, item: "skipped:test")
    monkeypatch.setattr(ContactEmbeddingService, "backfill", lambda self, **kwargs: {"written": 0})
    monkeypatch.setattr(OrganizationEmbeddingService, "upsert", lambda self, item: "skipped:test")
    monkeypatch.setattr(OrganizationEmbeddingService, "backfill", lambda self, **kwargs: {"written": 0})
    monkeypatch.setattr(RoutedHygieneService, "run_once", lambda self, **kwargs: SimpleNamespace())
    service = ContactHydrationService(session, tool_service=tools)
    job = service.create_job(domain_id=praxis.id, query="newer_than:30d", enable_enrichment=False)
    service.process_once()
    service.approve_candidates(job.id, minimum_confidence=0.8)

    for _ in range(4):
        service.process_once()

    contact = session.scalar(select(Contact).where(Contact.email == "jane@example.com"))
    organization = session.scalar(select(Entity).where(Entity.normalized_name == "example"))
    assert contact is not None
    assert organization is not None
    assert contact.organization_entity_id == organization.id
    profile = OrganizationIntelligenceService(session).organization_payload(organization, domain_id=praxis.id)
    assert profile["contacts"][0]["name"] == "Jane Smith"


def test_organization_merge_preserves_people_and_aliases(session: Session) -> None:
    praxis, _ = _gmail_agent(session)
    survivor = Entity(name="Example Corporation", normalized_name="example corporation", source_refs=[], provenance={}, metadata_={})
    duplicate = Entity(name="Example Corp", normalized_name="example corp", website="https://example.com", source_refs=[], provenance={}, metadata_={})
    session.add_all([survivor, duplicate])
    session.flush()
    contact = Contact(name="Jane Smith", normalized_name="jane smith", email="jane@example.com", organization_entity_id=duplicate.id, source_refs=[], provenance={}, metadata_={})
    session.add(contact)
    session.flush()
    session.add_all([
        ContactOrganizationAffiliation(contact_id=contact.id, entity_id=duplicate.id, domain_id=praxis.id, role="Partner Lead", relationship_type="works_at", source_refs=[], metadata_={}),
        OrganizationAlias(entity_id=duplicate.id, alias="Example", normalized_alias="example", source_refs=[], metadata_={}),
    ])
    session.commit()

    RoutedHygieneService(session).merge_entities(survivor, duplicate)

    session.refresh(contact)
    assert contact.organization_entity_id == survivor.id
    assert session.scalar(select(OrganizationAlias).where(OrganizationAlias.normalized_alias == "example")).entity_id == survivor.id
    merged_name_alias = session.scalar(
        select(OrganizationAlias).where(OrganizationAlias.normalized_alias == "example corp")
    )
    assert merged_name_alias is not None
    assert merged_name_alias.entity_id == survivor.id
    assert merged_name_alias.source == "duplicate_merge"
    profile = OrganizationIntelligenceService(session).organization_payload(survivor, domain_id=praxis.id)
    assert profile["website"] == "https://example.com"
    assert profile["contacts"][0]["name"] == "Jane Smith"
    assert duplicate.status == "archived"


def test_contact_merge_preserves_duplicate_name_as_observed_alias(session: Session) -> None:
    survivor = Contact(
        name="William Rollins",
        normalized_name="william rollins",
        email="william.rollins@example.com",
        source_refs=[{"type": "gmail_message", "id": "formal-name"}],
        provenance={},
        metadata_={},
    )
    duplicate = Contact(
        name="Will Rollins",
        normalized_name="will rollins",
        source_refs=[{"type": "gmail_message", "id": "signed-name"}],
        provenance={},
        metadata_={},
    )
    session.add_all([survivor, duplicate])
    session.commit()

    RoutedHygieneService(session).merge_contacts(survivor, duplicate)

    alias = session.scalar(select(ContactAlias).where(ContactAlias.normalized_alias == "will rollins"))
    assert alias is not None
    assert alias.contact_id == survivor.id
    assert alias.alias == "Will Rollins"
    assert alias.source == "duplicate_merge"
    assert duplicate.status == "archived"
