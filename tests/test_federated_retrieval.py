from datetime import UTC, datetime, timedelta

from app.db.models import (
    Contact,
    ContactDomainNote,
    Domain,
    MemoryEmbedding,
    MemoryItem,
    Report,
    RetrievalDocument,
)
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


def test_bounded_index_sync_advances_cyclic_source_cursor(session):
    praxis = _domain(session, "praxis")
    session.add_all(
        [
            MemoryItem(
                domain_id=praxis.id,
                scope="domain",
                memory_type="fact",
                title=f"Bounded memory {index}",
                content=f"Content {index}",
                metadata_={},
                importance=0.5,
                impact_level="low",
            )
            for index in range(3)
        ]
    )
    session.commit()

    first = FederatedIndexService(session).sync(
        embed_missing=False,
        source_limit_per_store=2,
    )
    session.commit()
    second = FederatedIndexService(session).sync(
        embed_missing=False,
        source_limit_per_store=2,
    )

    assert first.projected == 2
    assert second.projected == 1
    assert session.query(MemoryItem).count() == 3


def test_index_reembeds_legacy_vectors_for_configured_cloud_provider(session):
    praxis = _domain(session, "praxis")
    memory = MemoryItem(
        domain_id=praxis.id,
        scope="domain",
        memory_type="fact",
        title="Legacy vector",
        content="This memory was embedded by the local provider.",
        metadata_={},
        importance=0.5,
        impact_level="low",
    )
    session.add(memory)
    session.flush()
    session.add(
        MemoryEmbedding(
            memory_item_id=memory.id,
            provider="ollama",
            model="nomic-embed-text",
            dimensions=3,
            source_text_hash="legacy",
            embedding=[0.1, 0.2, 0.3],
            metadata_={},
        )
    )
    session.commit()

    class CloudEmbeddingClient:
        provider = "openai"
        model = "text-embedding-3-small"

        def __init__(self):
            self.calls = 0

        def embed(self, _text: str) -> list[float]:
            self.calls += 1
            return [0.4, 0.5, 0.6, 0.7]

    client = CloudEmbeddingClient()
    service = FederatedIndexService(session, embedding_client=client)

    first = service.sync(embed_missing=True)
    session.commit()
    second = service.sync(embed_missing=True)
    document = session.query(RetrievalDocument).one()

    assert first.embedded == 1
    assert second.embedded == 0
    assert client.calls == 1
    assert document.embedding_provider == "openai"
    assert document.embedding_model == "text-embedding-3-small"
    assert document.embedding_dimensions == 4
    assert list(document.embedding) == [0.4, 0.5, 0.6, 0.7]


def test_index_bounds_cloud_embedding_calls_per_cycle(session):
    praxis = _domain(session, "praxis")
    session.add_all(
        [
            MemoryItem(
                domain_id=praxis.id,
                scope="domain",
                memory_type="fact",
                title=f"Needs cloud vector {index}",
                content=f"Content {index}",
                metadata_={},
                importance=0.5,
                impact_level="low",
            )
            for index in range(2)
        ]
    )
    session.commit()

    class CountingEmbeddingClient:
        provider = "openai"
        model = "text-embedding-3-small"

        def __init__(self):
            self.calls = 0

        def embed(self, _text: str) -> list[float]:
            self.calls += 1
            return [0.1, 0.2]

    client = CountingEmbeddingClient()
    result = FederatedIndexService(session, embedding_client=client).sync(
        embed_missing=True,
        embedding_limit=1,
    )

    assert result.projected == 2
    assert result.embedded == 1
    assert client.calls == 1
    assert session.query(RetrievalDocument).count() == 2


def test_explicit_store_selection_overrides_query_router_hints(session):
    praxis = _domain(session, "praxis")
    session.add(
        Report(
            domain_id=praxis.id,
            title="Jane Smith briefing",
            report_type="research",
            summary="Jane owns the next partner call.",
            body_markdown="Current partner context.",
            structured_data={},
        )
    )
    session.commit()

    bundle = FederatedRetrievalService(session).retrieve(
        FederatedRetrievalRequest(
            query_text="Who is the Jane Smith contact?",
            domain_id=praxis.id,
            stores={"reports"},
            use_semantic=False,
        )
    )

    assert [result.document.store for result in bundle.results] == ["reports"]
    assert bundle.plan.stores == ["reports"]
