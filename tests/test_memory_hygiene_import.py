import json

from app.db.models import MemoryHygieneRun, MemoryItem, MemoryProposal
from app.db.seed import seed_default_domains
from app.memory.chatgpt_import import ChatGPTExportImporter
from app.memory.hygiene import DurableMemoryHygieneService


class _NoopEmbeddingService:
    def backfill(self, **_kwargs):
        return []


def _memory(domain_id, title, content):
    return MemoryItem(
        domain_id=domain_id,
        scope="domain",
        memory_type="fact",
        title=title,
        content=content,
        metadata_={},
        importance=0.7,
        impact_level="low",
    )


def test_durable_hygiene_merges_exact_duplicates_and_proposes_semantic_review(session):
    domains = {domain.key: domain for domain in seed_default_domains(session)}
    praxis = domains["praxis"]
    session.add_all([
        _memory(praxis.id, "Current partner strategy", "Praxis will pursue the Example Corp partnership in August."),
        _memory(praxis.id, "Current partner strategy", "Praxis will pursue the Example Corp partnership in August."),
        _memory(praxis.id, "Updated partner strategy", "Praxis will pursue the Example Corp partnership after August review."),
    ])
    session.commit()

    run = DurableMemoryHygieneService(session, embedding_service=_NoopEmbeddingService()).run()

    assert run.status == "completed"
    assert run.duplicate_merged_count == 1
    assert run.proposal_count >= 1
    assert session.query(MemoryProposal).filter_by(status="proposed").count() >= 1


def test_durable_hygiene_bounds_each_maintenance_pass(session):
    domains = {domain.key: domain for domain in seed_default_domains(session)}
    praxis = domains["praxis"]
    session.add_all(
        [_memory(praxis.id, f"Memory {index}", f"Bounded content {index}") for index in range(25)]
    )
    session.commit()

    run = DurableMemoryHygieneService(
        session,
        embedding_service=_NoopEmbeddingService(),
    ).run(batch_size=10, embedding_batch_size=5, index_batch_size=5)

    assert run.status == "completed"
    assert run.scanned_count == 10
    assert run.details["bounded"] is True
    assert run.details["batch_size"] == 10
    assert run.details["retrieval_index"] == {"delegated": True}


def test_durable_hygiene_closes_interrupted_prior_runs(session):
    interrupted = MemoryHygieneRun(status="running", details={})
    session.add(interrupted)
    session.commit()

    completed = DurableMemoryHygieneService(
        session,
        embedding_service=_NoopEmbeddingService(),
    ).run(batch_size=10, embedding_batch_size=5, index_batch_size=5)

    session.refresh(interrupted)
    assert completed.status == "completed"
    assert interrupted.status == "failed"
    assert interrupted.completed_at is not None
    assert "Interrupted" in interrupted.error_message


def test_chatgpt_export_import_is_incremental_and_stages_changed_conversations(session, tmp_path):
    seed_default_domains(session)
    conversation = {
        "id": "conversation-123",
        "title": "Daily planning reflection",
        "create_time": 1000,
        "update_time": 2000,
        "mapping": {
            "one": {"message": {"author": {"role": "user"}, "create_time": 1000, "content": {"parts": ["Help me plan tomorrow."]}}},
            "two": {"message": {"author": {"role": "assistant"}, "create_time": 2000, "content": {"parts": ["Start with the partner call."]}}},
        },
    }
    importer = ChatGPTExportImporter(session, root=tmp_path)
    first = importer.import_bytes(json.dumps([conversation]).encode(), filename="conversations.json", default_domain_key="personal")
    repeated = importer.import_bytes(json.dumps([conversation]).encode(), filename="conversations.json", default_domain_key="personal")
    conversation["mapping"]["three"] = {"message": {"author": {"role": "user"}, "create_time": 3000, "content": {"parts": ["Also block time to exercise."]}}}
    changed = importer.import_bytes(json.dumps([conversation]).encode(), filename="conversations.json", default_domain_key="personal")

    assert first.staged == 1
    assert repeated.unchanged == 1
    assert changed.staged == 1
    staged_files = list((tmp_path / "personal" / "inbox").glob("chatgpt-*.md"))
    assert len(staged_files) == 2
    assert "Source: ChatGPT export" in staged_files[0].read_text()
