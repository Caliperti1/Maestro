from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import IngestionRecord, MemoryItem, SeedPackage, SourceRegistration
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.llm import LLMMemoryExtractor
from app.memory import LLMMemoryCurator
from app.memory.dropbox import MemoryDropboxProcessor
from app.memory.ingestion import IngestionLedgerService, envelope_for_file, policy_for_domain


class FakeLLMClient:
    def __init__(self, payload: dict):
        self.payload = payload

    def structured_response(self, **_kwargs):
        return self.payload


def _curator(session: Session, payload: dict) -> LLMMemoryCurator:
    return LLMMemoryCurator(session, LLMMemoryExtractor(FakeLLMClient(payload)))


def _empty_extraction() -> dict:
    return {"candidates": [], "routed_items": []}


def test_dropbox_tracks_provenance_and_skips_identical_source_version(
    session: Session,
    tmp_path: Path,
) -> None:
    processor = MemoryDropboxProcessor(
        session,
        root=tmp_path,
        curator=_curator(session, _empty_extraction()),
    )
    processor.ensure_directories()
    inbox = tmp_path / "praxis" / "inbox"
    first = inbox / "context.md"
    first.write_text("Praxis context version one.", encoding="utf-8")

    first_result = processor.process_once()
    first.write_text("Praxis context version one.", encoding="utf-8")
    second_result = processor.process_once()

    assert first_result[0].status == "processed"
    assert second_result[0].status == "duplicate"
    assert session.query(IngestionRecord).count() == 1
    assert session.query(SeedPackage).count() == 1
    record = session.query(IngestionRecord).one()
    assert record.status == "processed"
    assert record.duplicate_count == 1
    assert record.policy["egress_policy"] == "external_allowed"
    assert IngestionLedgerService(session).status()["duplicates_skipped"] == 1


def test_sanitized_dropbox_policy_reaches_memory_and_provenance(
    session: Session,
    tmp_path: Path,
) -> None:
    payload = {
        "candidates": [
            {
                "scope": "domain",
                "memory_type": "fact",
                "title": "Teaching obligation",
                "content": "Chris must prepare Lesson 6 before Thursday.",
                "rationale": "Explicit obligation.",
                "impact_level": "low",
                "importance": 0.8,
                "confidence": 0.95,
            }
        ],
        "routed_items": [],
    }
    processor = MemoryDropboxProcessor(session, root=tmp_path, curator=_curator(session, payload))
    processor.ensure_directories()
    source = tmp_path / "usma" / "inbox" / "maestro_context.md"
    source.write_text("Prepare Lesson 6 before Thursday.", encoding="utf-8")

    result = processor.process_once()

    assert result[0].status == "processed"
    memory = session.query(MemoryItem).one()
    assert memory.metadata_["source_policy"]["egress_policy"] == "external_allowed"
    assert memory.metadata_["source_system"] == "manual_dropbox"
    assert memory.metadata_["source_timestamp"]
    source_ref = memory.metadata_["source_refs"][0]
    assert source_ref["external_id"] == "maestro_context.md"
    assert source_ref["sensitivity"] == "sanitized_work_context"
    assert source_ref["egress_policy"] == "external_allowed"


def test_sanitized_dropbox_uses_standard_external_model_policy(tmp_path: Path) -> None:
    source = tmp_path / "context.md"
    source.write_text("Sanitized work context.", encoding="utf-8")

    envelope = envelope_for_file(source, domain_key="usma")

    assert envelope.policy.egress_policy == "external_allowed"
    assert envelope.policy.sensitivity == "sanitized_work_context"
    assert envelope.policy.transfer_method == "sanitized_context_drop"


def test_changed_file_version_is_reprocessed(session: Session, tmp_path: Path) -> None:
    processor = MemoryDropboxProcessor(
        session,
        root=tmp_path,
        curator=_curator(session, _empty_extraction()),
    )
    processor.ensure_directories()
    source = tmp_path / "praxis" / "inbox" / "context.md"
    source.write_text("Version one.", encoding="utf-8")
    processor.process_once()
    source.write_text("Version two.", encoding="utf-8")

    result = processor.process_once()

    assert result[0].status == "processed"
    assert session.query(IngestionRecord).count() == 2
    assert session.query(SeedPackage).count() == 2


def test_ingestion_ledger_recovers_stale_processing_record(
    session: Session,
    tmp_path: Path,
) -> None:
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("praxis")
    assert domain is not None
    source = tmp_path / "context.md"
    source.write_text("Stale context.", encoding="utf-8")
    envelope = envelope_for_file(source, domain_key="praxis")
    ledger = IngestionLedgerService(session)
    registration = ledger.ensure_registration(
        key=envelope.source_registration_key,
        source_system=envelope.source_system,
        display_name="Praxis context dropbox",
        adapter_type="filesystem_dropbox",
        domain=domain,
        policy=envelope.policy,
    )
    claim = ledger.claim(envelope, domain=domain)
    claim.record.processing_started_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    recovered = ledger.recover_stale(stale_after=timedelta(minutes=30))

    session.refresh(claim.record)
    assert registration.key == "dropbox:praxis"
    assert recovered == 1
    assert claim.record.status == "failed"
    assert "Recovered stale ingestion" in (claim.record.last_error or "")


def test_source_checkpoint_is_updated_idempotently(session: Session) -> None:
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("praxis")
    assert domain is not None
    ledger = IngestionLedgerService(session)
    registration = ledger.ensure_registration(
        key="gmail:praxis",
        source_system="gmail",
        display_name="Praxis Gmail",
        adapter_type="gmail_history",
        domain=domain,
        policy=policy_for_domain("praxis"),
    )

    first = ledger.update_checkpoint(
        registration,
        cursor_key="history_id",
        cursor_value={"history_id": "100"},
    )
    second = ledger.update_checkpoint(
        registration,
        cursor_key="history_id",
        cursor_value={"history_id": "101"},
    )

    assert first.id == second.id
    assert second.cursor_value == {"history_id": "101"}
    assert session.query(SourceRegistration).count() == 1
