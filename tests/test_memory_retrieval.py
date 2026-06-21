import uuid

import pytest
from sqlalchemy.orm import Session

from app.db.models import Artifact, MemoryEmbedding, MemoryItem, MemoryLink, SeedPackage
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.memory.retrieval import (
    MemoryContextBundleRequest,
    MemoryRetrievalError,
    MemoryRetrievalQuery,
    MemoryRetrievalService,
)


class FakeEmbeddingClient:
    provider = "test"
    model = "fake-embeddings"

    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    def embed(self, text: str) -> list[float]:
        for key, vector in self.vectors.items():
            if key in text:
                return vector
        return [0.0, 1.0, 0.0]


def _domain_ids(session: Session):
    seed_default_domains(session)
    repo = DomainRepository(session)
    praxis = repo.get_by_key("praxis")
    ophi = repo.get_by_key("ophi")
    assert praxis is not None
    assert ophi is not None
    return praxis.id, ophi.id


def _memory(
    session: Session,
    *,
    title: str,
    content: str,
    scope: str = "domain",
    domain_id=None,
    agent_id=None,
    memory_type: str = "fact",
    importance: float = 0.5,
    impact_level: str = "low",
    metadata: dict | None = None,
) -> MemoryItem:
    item = MemoryItem(
        scope=scope,
        domain_id=domain_id,
        agent_id=agent_id,
        memory_type=memory_type,
        title=title,
        content=content,
        importance=importance,
        impact_level=impact_level,
        metadata_=metadata or {},
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_retrieval_isolates_agent_visibility_and_ranks_query_matches(session: Session) -> None:
    praxis_id, ophi_id = _domain_ids(session)
    praxis_agent_id = uuid.uuid4()
    ophi_agent_id = uuid.uuid4()
    _memory(
        session,
        scope="global",
        title="Concise reports",
        content="Chris prefers concise reports.",
        importance=0.4,
    )
    praxis_match = _memory(
        session,
        domain_id=praxis_id,
        title="Praxis Tactical Innovation",
        content="Praxis operationalizes Tactical Innovation training.",
        importance=0.7,
    )
    praxis_agent_memory = _memory(
        session,
        scope="agent",
        domain_id=praxis_id,
        agent_id=praxis_agent_id,
        title="Praxis agent retrieval habit",
        content="Inspect Praxis memory before standup synthesis.",
        importance=0.8,
    )
    _memory(
        session,
        scope="agent",
        domain_id=ophi_id,
        agent_id=ophi_agent_id,
        title="Ophi hidden agent note",
        content="This should not leak into Praxis retrieval.",
        importance=1.0,
    )

    result = MemoryRetrievalService(session).retrieve(
        MemoryRetrievalQuery(
            audience="agent",
            domain_id=praxis_id,
            agent_id=praxis_agent_id,
            query_text="Praxis tactical innovation",
            use_semantic=False,
            limit=5,
        )
    )

    ids = [retrieved.memory.id for retrieved in result.results]
    assert ids[0] == praxis_match.id
    assert praxis_agent_memory.id in ids
    assert all("Ophi hidden" not in retrieved.memory.title for retrieved in result.results)
    assert "query relevance" in " ".join(result.results[0].score_reasons)


def test_retrieval_returns_provenance_and_visible_links(session: Session) -> None:
    praxis_id, ophi_id = _domain_ids(session)
    seed_package = SeedPackage(
        domain_id=praxis_id,
        name="Praxis Quad.pdf",
        source_type="dropbox_file",
        status="processed",
        metadata_={},
    )
    session.add(seed_package)
    session.flush()
    artifact = Artifact(
        seed_package_id=seed_package.id,
        artifact_type="raw_file",
        name="Praxis Quad.pdf",
        uri="maestro_dropbox/praxis/processed/Praxis Quad.pdf",
        mime_type="application/pdf",
        metadata_={},
    )
    session.add(artifact)
    session.commit()

    source = _memory(
        session,
        domain_id=praxis_id,
        title="Praxis mission",
        content="Praxis trains Tactical Innovation Officers.",
        importance=0.8,
        metadata={
            "seed_package_id": str(seed_package.id),
            "artifact_id": str(artifact.id),
            "processed_path": artifact.uri,
            "source_refs": [{"type": "artifact", "id": str(artifact.id), "uri": artifact.uri}],
        },
    )
    related = _memory(
        session,
        domain_id=praxis_id,
        title="Praxis overview",
        content="Praxis supports Soldier-led solution development.",
        importance=0.7,
    )
    hidden = _memory(
        session,
        domain_id=ophi_id,
        title="Hidden Ophi relation",
        content="This linked memory belongs to another domain.",
        importance=0.9,
    )
    session.add_all(
        [
            MemoryLink(
                source_memory_id=source.id,
                target_memory_id=related.id,
                relation_type="reinforces",
                metadata_={"confidence": 0.9},
            ),
            MemoryLink(
                source_memory_id=source.id,
                target_memory_id=hidden.id,
                relation_type="same_entity_as",
                metadata_={},
            ),
        ]
    )
    session.commit()

    result = MemoryRetrievalService(session).retrieve(
        MemoryRetrievalQuery(
            audience="maestro",
            domain_id=praxis_id,
            query_text="mission",
            use_semantic=False,
        )
    )
    retrieved_source = next(item for item in result.results if item.memory.id == source.id)

    assert retrieved_source.provenance.seed_package == {
        "id": str(seed_package.id),
        "name": "Praxis Quad.pdf",
        "source_type": "dropbox_file",
        "status": "processed",
    }
    assert retrieved_source.provenance.artifact is not None
    assert retrieved_source.provenance.processed_path == artifact.uri
    assert retrieved_source.provenance.source_refs[0]["uri"] == artifact.uri
    assert [link.memory.id for link in retrieved_source.links] == [related.id]
    assert retrieved_source.links[0].relation_type == "reinforces"


def test_agent_retrieval_requires_domain(session: Session) -> None:
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(session).retrieve(MemoryRetrievalQuery(audience="agent"))


def test_balanced_query_retrieval_filters_zero_match_noise(session: Session) -> None:
    praxis_id, _ = _domain_ids(session)
    relevant = _memory(
        session,
        domain_id=praxis_id,
        title="Praxis training model",
        content="Praxis trains Tactical Innovation Officers.",
        importance=0.6,
    )
    zero_match = _memory(
        session,
        domain_id=praxis_id,
        title="Contracting reminder",
        content="Review unrelated vendor paperwork.",
        importance=1.0,
        impact_level="high",
    )

    result = MemoryRetrievalService(session).retrieve(
        MemoryRetrievalQuery(
            audience="maestro",
            domain_id=praxis_id,
            query_text="tactical innovation",
            use_semantic=False,
            mode="balanced",
        )
    )

    assert [item.memory.id for item in result.results] == [relevant.id]
    assert result.results[0].query_relevance > 0
    assert result.filtered_count == 1
    assert zero_match.id not in [item.memory.id for item in result.results]


def test_broad_query_retrieval_keeps_zero_match_results_but_ranks_matches_first(
    session: Session,
) -> None:
    praxis_id, _ = _domain_ids(session)
    relevant = _memory(
        session,
        domain_id=praxis_id,
        title="Praxis Tactical Innovation",
        content="Praxis trains innovation officers.",
        importance=0.5,
    )
    zero_match = _memory(
        session,
        domain_id=praxis_id,
        title="High importance unrelated memory",
        content="This item should be visible only in broad retrieval.",
        importance=1.0,
        impact_level="high",
    )

    result = MemoryRetrievalService(session).retrieve(
        MemoryRetrievalQuery(
            audience="maestro",
            domain_id=praxis_id,
            query_text="tactical innovation",
            use_semantic=False,
            mode="broad",
        )
    )

    assert [item.memory.id for item in result.results] == [relevant.id, zero_match.id]
    assert result.results[0].query_relevance > result.results[1].query_relevance
    assert result.filtered_count == 0


def test_strict_query_retrieval_requires_stronger_match(session: Session) -> None:
    praxis_id, _ = _domain_ids(session)
    partial = _memory(
        session,
        domain_id=praxis_id,
        title="Praxis training",
        content="Training notes mention only one query term.",
        importance=0.9,
    )
    strong = _memory(
        session,
        domain_id=praxis_id,
        title="Praxis Tactical Innovation training",
        content="Tactical Innovation training is central to Praxis.",
        importance=0.7,
    )

    result = MemoryRetrievalService(session).retrieve(
        MemoryRetrievalQuery(
            audience="maestro",
            domain_id=praxis_id,
            query_text="tactical innovation training",
            use_semantic=False,
            mode="strict",
        )
    )

    assert [item.memory.id for item in result.results] == [strong.id]
    assert partial.id not in [item.memory.id for item in result.results]


def test_semantic_retrieval_can_surface_non_lexical_matches(session: Session) -> None:
    praxis_id, _ = _domain_ids(session)
    partner_memory = _memory(
        session,
        domain_id=praxis_id,
        title="THI relationship",
        content="THI is a strategic collaborator for Praxis programs.",
        importance=0.7,
    )
    unrelated = _memory(
        session,
        domain_id=praxis_id,
        title="Invoice process",
        content="Invoices should be reviewed monthly.",
        importance=0.95,
    )
    session.add_all(
        [
            MemoryEmbedding(
                memory_item_id=partner_memory.id,
                provider="test",
                model="fake-embeddings",
                dimensions=3,
                source_text_hash="partner",
                embedding=[1.0, 0.0, 0.0],
                metadata_={},
            ),
            MemoryEmbedding(
                memory_item_id=unrelated.id,
                provider="test",
                model="fake-embeddings",
                dimensions=3,
                source_text_hash="invoice",
                embedding=[0.0, 1.0, 0.0],
                metadata_={},
            ),
        ]
    )
    session.commit()

    result = MemoryRetrievalService(
        session,
        embedding_client=FakeEmbeddingClient({"partner call": [1.0, 0.0, 0.0]}),
    ).retrieve(
        MemoryRetrievalQuery(
            audience="maestro",
            domain_id=praxis_id,
            query_text="partner call",
            mode="balanced",
        )
    )

    assert result.semantic_status == "enabled"
    assert [item.memory.id for item in result.results] == [partner_memory.id]
    assert result.results[0].query_relevance == 0
    assert result.results[0].semantic_similarity == 1.0
    assert "semantic similarity 1.00" in result.results[0].score_reasons


def test_agent_context_bundle_groups_visible_memory_without_cross_domain_leaks(
    session: Session,
) -> None:
    praxis_id, ophi_id = _domain_ids(session)
    praxis_agent_id = uuid.uuid4()
    _memory(
        session,
        scope="global",
        title="Communication preference",
        content="Chris prefers concise operational context.",
        importance=0.9,
    )
    _memory(
        session,
        scope="maestro_session",
        title="Current session focus",
        content="The current sprint is focused on hardening memory retrieval.",
        importance=0.8,
    )
    praxis_memory = _memory(
        session,
        domain_id=praxis_id,
        title="Praxis partner call",
        content="Praxis partner calls should connect training, transitions, and stakeholder needs.",
        importance=0.8,
    )
    _memory(
        session,
        scope="agent",
        domain_id=praxis_id,
        agent_id=praxis_agent_id,
        title="Praxis agent habit",
        content="The Praxis agent should retrieve scoped memory before drafting recommendations.",
        importance=0.7,
    )
    _memory(
        session,
        domain_id=ophi_id,
        title="Ophi private context",
        content="This Ophi memory must not appear in a Praxis agent bundle.",
        importance=1.0,
    )

    bundle = MemoryRetrievalService(session).build_context_bundle(
        MemoryContextBundleRequest(
            profile="agent_prompt",
            audience="agent",
            domain_id=praxis_id,
            agent_id=praxis_agent_id,
            query_text="Praxis partner call",
            use_semantic=False,
            max_items=8,
            max_chars=2500,
        )
    )

    assert bundle.semantic_status == "disabled"
    assert [section.key for section in bundle.sections] == [
        "global",
        "domain",
        "agent",
    ]
    assert bundle.included_count == 3
    assert "Ophi private context" not in bundle.rendered_text
    assert str(praxis_memory.id) in bundle.rendered_text
    assert bundle.rendered_text.startswith("[Global Memory]")


def test_context_bundle_respects_item_and_character_budgets(session: Session) -> None:
    praxis_id, _ = _domain_ids(session)
    for index in range(6):
        _memory(
            session,
            domain_id=praxis_id,
            title=f"Praxis note {index}",
            content="Praxis memory " + ("long detail " * 80),
            importance=0.9 - (index * 0.05),
        )

    bundle = MemoryRetrievalService(session).build_context_bundle(
        MemoryContextBundleRequest(
            profile="memory_debug",
            audience="maestro",
            domain_id=praxis_id,
            query_text="Praxis memory",
            use_semantic=False,
            max_items=2,
            max_chars=600,
        )
    )

    snippets = [snippet for section in bundle.sections for snippet in section.snippets]
    assert bundle.included_count <= 2
    assert bundle.used_chars <= bundle.max_chars
    assert bundle.dropped_count > 0
    assert all(len(snippet.excerpt) < len(snippet.memory.content) for snippet in snippets)
