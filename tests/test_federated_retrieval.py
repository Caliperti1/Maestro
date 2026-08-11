from datetime import UTC, datetime, timedelta

from app.db.models import Contact, ContactDomainNote, Domain, MemoryItem, Report
from app.memory.federated_retrieval import (
    FederatedIndexService,
    FederatedRetrievalRequest,
    FederatedRetrievalService,
)


def _domain(session, key: str) -> Domain:
    domain = Domain(key=key, name=key.title(), description="", is_active=True)
    session.add(domain)
    session.flush()
    return domain


def test_federated_retrieval_ranks_across_stores_and_explains_scores(session):
    praxis = _domain(session, "praxis")
    memory = MemoryItem(
        domain_id=praxis.id,
        scope="domain",
        memory_type="fact",
        title="Praxis ownership",
        content="Chris owns Praxis Defense and leads partner strategy.",
        metadata_={"source_policy": {"trust_level": "user_reviewed", "egress_policy": "external_allowed"}},
        importance=0.9,
        impact_level="high",
    )
    contact = Contact(
        name="Jane Smith",
        normalized_name="jane smith",
        email="jane@example.com",
        summary="Jane leads the Example Corp partnership with Praxis.",
        source_refs=[],
        provenance={"source_policy": {"trust_level": "user_provided"}},
        metadata_={},
    )
    session.add_all([memory, contact])
    session.flush()
    session.add(ContactDomainNote(contact_id=contact.id, domain_id=praxis.id, notes="Partner lead", source_refs=[], metadata_={}))
    session.add(Report(domain_id=praxis.id, title="Partner report", report_type="research", summary="Jane owns the next partner call.", body_markdown="Full partner findings.", structured_data={}))
    session.commit()

    bundle = FederatedRetrievalService(session).retrieve(
        FederatedRetrievalRequest(query_text="What does the report say about who owns the Praxis partner call?", use_semantic=False)
    )

    assert {item.document.store for item in bundle.results} >= {"memory", "contacts", "reports"}
    assert all(item.reasons for item in bundle.results)
    assert "[contacts] Jane Smith" in bundle.rendered_text
    assert bundle.used_chars <= bundle.request.max_chars


def test_agent_retrieval_cannot_cross_domain_boundary(session):
    praxis = _domain(session, "praxis")
    perti = _domain(session, "perti-laboratories")
    session.add_all([
        MemoryItem(domain_id=praxis.id, scope="domain", memory_type="fact", title="Praxis plan", content="Praxis partner plan", metadata_={}, importance=0.8, impact_level="low"),
        MemoryItem(domain_id=perti.id, scope="domain", memory_type="fact", title="Perti secret", content="Perti confidential roadmap", metadata_={}, importance=0.8, impact_level="low"),
    ])
    session.commit()

    bundle = FederatedRetrievalService(session).retrieve(
        FederatedRetrievalRequest(query_text="roadmap and partner plan", audience="agent", domain_id=praxis.id, use_semantic=False)
    )

    assert any(item.document.title == "Praxis plan" for item in bundle.results)
    assert all(item.document.title != "Perti secret" for item in bundle.results)
    assert bundle.plan.domains == ["praxis"]


def test_index_archives_removed_and_retrieval_filters_expired_or_local_only(session):
    praxis = _domain(session, "praxis")
    expired = MemoryItem(domain_id=praxis.id, scope="domain", memory_type="fact", title="Old state", content="No scheduling support", metadata_={}, importance=0.8, impact_level="low", valid_until=datetime.now(UTC) - timedelta(days=1))
    local = MemoryItem(domain_id=praxis.id, scope="domain", memory_type="fact", title="Local note", content="Private local evidence", metadata_={"source_policy": {"egress_policy": "local_only"}}, importance=0.8, impact_level="low")
    session.add_all([expired, local])
    session.commit()

    sync = FederatedIndexService(session).sync(embed_missing=False)
    bundle = FederatedRetrievalService(session).retrieve(FederatedRetrievalRequest(query_text="private scheduling", domain_id=praxis.id, egress_target="external", use_semantic=False))

    assert sync.projected == 2
    assert not bundle.results
    assert bundle.policy_filtered_count == 1
