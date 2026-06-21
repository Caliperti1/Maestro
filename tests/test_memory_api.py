from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import MemoryItem, MemoryProposal, SeedPackage
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.db.session import get_db


def _client(session: Session, tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)

    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_memory_status_and_upload(session: Session, tmp_path: Path) -> None:
    client = _client(session, tmp_path)

    status = client.get("/memory/dropbox/status")
    assert status.status_code == 200
    assert status.json()["root"] == str(tmp_path)
    assert any(domain["key"] == "ophi" for domain in status.json()["domains"])

    upload = client.post(
        "/memory/dropbox/ophi/upload",
        files={"file": ("note.md", b"# Ophi note\nMemory test.", "text/markdown")},
    )

    assert upload.status_code == 200
    assert upload.json()["status"] == "uploaded"
    assert (tmp_path / "ophi" / "inbox" / "note.md").is_file()


def test_memory_preview_listing(session: Session, tmp_path: Path) -> None:
    client = _client(session, tmp_path)
    preview_dir = tmp_path / "ophi" / "previews"
    preview_dir.mkdir(parents=True)
    (preview_dir / "note.preview.json").write_text(
        """
        {
          "source_file": "note.md",
          "status": "written",
          "candidates": [{}],
          "results": [{"outcome": "written", "memory_item_id": "memory-1"}]
        }
        """,
        encoding="utf-8",
    )

    response = client.get("/memory/dropbox/previews?domain_key=ophi")

    assert response.status_code == 200
    previews = response.json()["previews"]
    assert len(previews) == 1
    assert previews[0]["source_file"] == "note.md"
    assert previews[0]["candidate_count"] == 1
    assert previews[0]["written_count"] == 1


def test_pending_approval_and_approve(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    proposal = MemoryProposal(
        scope="global",
        memory_type="standing_instruction",
        title="External approval",
        content="Do not send external messages without approval.",
        rationale="Authority-changing memory.",
        impact_level="very_high",
        status="pending_user_approval",
        source_refs=[],
        metadata_={},
    )
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    client = _client(session, tmp_path)

    pending = client.get("/memory/proposals/pending")
    assert pending.status_code == 200
    assert pending.json()["proposals"][0]["title"] == "External approval"

    approved = client.post(f"/memory/proposals/{proposal.id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["memory_item"]["title"] == "External approval"


def test_reject_pending_memory(session: Session, tmp_path: Path) -> None:
    proposal = MemoryProposal(
        scope="global",
        memory_type="standing_instruction",
        title="Reject me",
        content="This should be rejected.",
        rationale="Test rejection.",
        impact_level="very_high",
        status="pending_user_approval",
        source_refs=[],
        metadata_={},
    )
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    client = _client(session, tmp_path)

    rejected = client.post(
        f"/memory/proposals/{proposal.id}/reject",
        json={"reason": "Not appropriate."},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["proposal"]["metadata"]["rejection_reason"] == "Not appropriate."


def test_source_listing_and_reclassification(session: Session, tmp_path: Path) -> None:
    seed_default_domains(session)
    personal = DomainRepository(session).get_by_key("personal")
    assert personal is not None
    seed_package = SeedPackage(
        name="resume.pdf",
        source_type="dropbox_file",
        status="processed",
        metadata_={"seed": True},
    )
    session.add(seed_package)
    session.flush()
    memory_item = MemoryItem(
        scope="global",
        memory_type="fact",
        title="Resume fact",
        content="Chris has a resume.",
        impact_level="medium",
        importance=0.7,
        metadata_={"seed_package_id": str(seed_package.id), "dropbox_domain": "global"},
    )
    proposal = MemoryProposal(
        scope="global",
        memory_type="preference",
        title="Resume preference",
        content="Chris prefers durable context.",
        impact_level="medium",
        status="approved",
        source_refs=[],
        metadata_={"seed_package_id": str(seed_package.id), "dropbox_domain": "global"},
    )
    session.add_all([memory_item, proposal])
    session.commit()
    client = _client(session, tmp_path)

    sources = client.get("/memory/sources")

    assert sources.status_code == 200
    assert sources.json()["sources"][0]["name"] == "resume.pdf"
    assert sources.json()["sources"][0]["memory_count"] == 1
    assert sources.json()["sources"][0]["proposal_count"] == 1

    details = client.get(f"/memory/sources/{seed_package.id}")

    assert details.status_code == 200
    assert details.json()["source"]["memories"][0]["title"] == "Resume fact"

    reclassified = client.post(
        f"/memory/sources/{seed_package.id}/reclassify",
        json={"target_domain_key": "personal", "reason": "Resume belongs in Personal."},
    )

    assert reclassified.status_code == 200
    payload = reclassified.json()["source"]
    assert payload["domain_key"] == "personal"
    assert payload["memories"][0]["scope"] == "domain"
    assert payload["memories"][0]["metadata"]["dropbox_domain"] == "personal"
    assert payload["memories"][0]["metadata"]["reclassification_history"][0]["reason"] == (
        "Resume belongs in Personal."
    )
    session.refresh(memory_item)
    session.refresh(proposal)
    assert memory_item.domain_id == personal.id
    assert proposal.domain_id == personal.id


def test_memory_retrieval_endpoint_returns_scored_context(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    ophi = DomainRepository(session).get_by_key("ophi")
    assert praxis is not None
    assert ophi is not None
    praxis_memory = MemoryItem(
        scope="domain",
        domain_id=praxis.id,
        memory_type="fact",
        title="Praxis training model",
        content="Praxis trains Tactical Innovation Officers.",
        impact_level="medium",
        importance=0.8,
        metadata_={"source_refs": [{"type": "artifact", "id": "artifact-1"}]},
    )
    ophi_memory = MemoryItem(
        scope="domain",
        domain_id=ophi.id,
        memory_type="fact",
        title="Ophi research model",
        content="Ophi memory should not appear in Praxis-scoped retrieval.",
        impact_level="low",
        importance=1.0,
        metadata_={},
    )
    session.add_all([praxis_memory, ophi_memory])
    session.commit()
    client = _client(session, tmp_path)

    response = client.get(
        "/memory/retrieve",
        params={
            "audience": "maestro",
            "domain_key": "praxis",
            "query_text": "tactical innovation training",
            "use_semantic": "false",
            "limit": 5,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"]["domain_key"] == "praxis"
    assert payload["query"]["mode"] == "balanced"
    assert payload["query"]["use_semantic"] is False
    assert payload["semantic_status"] == "disabled"
    assert payload["filtered_count"] == 0
    assert payload["results"][0]["title"] == "Praxis training model"
    assert payload["results"][0]["domain_key"] == "praxis"
    assert payload["results"][0]["score"] > 0
    assert payload["results"][0]["query_relevance"] > 0
    assert payload["results"][0]["semantic_similarity"] is None
    assert payload["results"][0]["provenance"]["source_refs"][0]["id"] == "artifact-1"
    assert all(result["domain_key"] != "ophi" for result in payload["results"])
