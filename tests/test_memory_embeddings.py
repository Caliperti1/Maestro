from sqlalchemy.orm import Session

from app.db.models import MemoryEmbedding
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.memory.embeddings import MemoryEmbeddingService
from app.memory.service import MemoryCandidate, MemoryService


class FakeEmbeddingClient:
    provider = "test"
    model = "fake-embeddings"

    def embed(self, text: str) -> list[float]:
        if "Praxis" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]


def test_embedding_service_upserts_memory_embedding(session: Session) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    memory_item = MemoryService(session).write_candidate(
        MemoryCandidate(
            domain_id=praxis.id,
            scope="domain",
            memory_type="fact",
            title="Praxis mission",
            content="Praxis trains Tactical Innovation Officers.",
            impact_level="low",
        )
    ).memory_item
    assert memory_item is not None

    result = MemoryEmbeddingService(
        session,
        client=FakeEmbeddingClient(),
        best_effort=False,
    ).upsert_memory_embedding(memory_item)
    session.commit()

    assert result.status == "written"
    embedding = session.query(MemoryEmbedding).one()
    assert embedding.memory_item_id == memory_item.id
    assert embedding.provider == "test"
    assert embedding.model == "fake-embeddings"
    assert embedding.dimensions == 3
    assert list(embedding.embedding) == [1.0, 0.0, 0.0]


def test_memory_service_writes_embedding_when_configured(session: Session) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    embedding_service = MemoryEmbeddingService(
        session,
        client=FakeEmbeddingClient(),
        best_effort=False,
    )
    service = MemoryService(session, embedding_service=embedding_service)

    result = service.write_candidate(
        MemoryCandidate(
            domain_id=praxis.id,
            scope="domain",
            memory_type="fact",
            title="Praxis mission",
            content="Praxis trains Tactical Innovation Officers.",
            impact_level="low",
        )
    )

    assert result.memory_item is not None
    assert result.memory_item.metadata_["embedding_status"] == "written"
    assert session.query(MemoryEmbedding).count() == 1
