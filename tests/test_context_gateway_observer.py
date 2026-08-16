import subprocess
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import Domain, IngestionRecord, SourceRegistration
from app.memory.context_gateway import (
    ContextGatewayService,
    GatewayItem,
    ToolEvidenceLedgerService,
    parse_sanitized_context_manifest,
)
from app.memory.ingestion import SourcePolicy, envelope_for_file, strip_context_envelope
from app.memory.repository_observer import RepositoryObserverService


def _domain(session, key="perti-laboratories"):
    domain = Domain(key=key, name=key, description="", is_active=True)
    session.add(domain)
    session.commit()
    return domain


def test_context_gateway_is_idempotent(session, tmp_path):
    domain = _domain(session)
    item = GatewayItem(
        source_registration_key="test:perti",
        source_system="test",
        external_id="object-1",
        source_version="v1",
        content_type="note",
        domain_key=domain.key,
        title="Architecture note",
        content="# Architecture\nCurrent state.",
        source_timestamp=datetime.now(UTC),
        policy=SourcePolicy("business_confidential", "user_reviewed", "test"),
    )
    gateway = ContextGatewayService(session, root=tmp_path)

    first = gateway.ingest(item, domain=domain)
    second = gateway.ingest(item, domain=domain)

    assert first.status == "staged"
    assert second.status == "duplicate"
    files = list((tmp_path / domain.key / "inbox").glob("*.md"))
    assert len(files) == 1
    envelope = envelope_for_file(files[0], domain_key=domain.key)
    assert envelope.external_id == "object-1"
    assert envelope.source_system == "test"
    assert strip_context_envelope(files[0].read_text()) == item.content
    record = session.get(IngestionRecord, uuid.UUID(first.ingestion_record_id))
    assert record is not None and record.status == "staged"


def test_repository_observer_baseline_then_incremental(session, tmp_path):
    domain = _domain(session)
    repository = tmp_path / "project"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "README.md").write_text("# Product\nInitial capability.")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)
    gateway = ContextGatewayService(session, root=tmp_path / "dropbox")
    observer = RepositoryObserverService(session, gateway=gateway)
    registration = observer.register(key="repo:product", path=str(repository), domain=domain)

    baseline = observer.observe(registration)
    unchanged = observer.observe(registration)
    (repository / "app.py").write_text("FEATURE = True\n")
    subprocess.run(["git", "add", "app.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "add feature"], cwd=repository, check=True, capture_output=True)
    incremental = observer.observe(registration)

    assert baseline.mode == "full"
    assert baseline.gateway and baseline.gateway.status == "staged"
    assert unchanged.status == "unchanged"
    assert incremental.mode == "incremental"
    assert "app.py" in incremental.changed_files


def test_sanitized_manifest_requires_review_and_rejects_restricted_content():
    valid = """---
domain: usma
source_system: usma_context_drop
reviewed_by: Chris Aliperti
reviewed_at: 2026-08-11T09:00:00-04:00
contains_restricted: false
---
# Obligations
- Prepare Lesson 6 before Thursday.
"""
    metadata, body = parse_sanitized_context_manifest(valid, expected_domain="usma")
    assert metadata["reviewed_by"] == "Chris Aliperti"
    assert "Lesson 6" in body

    with pytest.raises(ValueError, match="restricted"):
        parse_sanitized_context_manifest(valid.replace("false", "true"))


def test_google_and_github_tool_evidence_share_ingestion_ledger(session):
    domain = _domain(session, "personal")
    service = ToolEvidenceLedgerService(session)
    service.record(tool_call_id="call-1", tool_key="google.docs.get", domain=domain, output={"summary": {"title": "Doc"}}, task_id="task-1")
    service.record(tool_call_id="call-2", tool_key="github.repo.get", domain=domain, output={"summary": {"repo": "owner/repo"}}, task_id="task-2")

    registrations = session.scalars(select(SourceRegistration)).all()
    records = session.scalars(select(IngestionRecord)).all()
    assert {registration.source_system for registration in registrations} == {"google", "github"}
    assert {record.status for record in records} == {"processed"}
