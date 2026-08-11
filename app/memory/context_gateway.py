"""Shared staging gateway for normalized evidence produced by source adapters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Domain, IngestionRecord
from app.memory.ingestion import ContextEnvelope, IngestionLedgerService, SourcePolicy


@dataclass(frozen=True)
class GatewayItem:
    source_registration_key: str
    source_system: str
    external_id: str
    source_version: str
    content_type: str
    domain_key: str
    title: str
    content: str
    source_timestamp: datetime | None
    policy: SourcePolicy
    extension: str = ".md"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayIngestResult:
    status: str
    path: str | None
    ingestion_record_id: str
    duplicate_status: str | None = None


class ContextGatewayService:
    def __init__(self, session: Session, *, root: Path | None = None):
        self.session = session
        self.root = root or Path(get_settings().memory_dropbox_root)

    def ingest(self, item: GatewayItem, *, domain: Domain) -> GatewayIngestResult:
        content_hash = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        destination = self.root / item.domain_key / "inbox" / (
            f"{_slug(item.source_system)}-{_slug(item.title)}-{content_hash[:10]}{item.extension}"
        )
        envelope = ContextEnvelope(
            source_registration_key=item.source_registration_key,
            source_system=item.source_system,
            external_id=item.external_id,
            source_version=item.source_version,
            content_hash=content_hash,
            content_type=item.content_type,
            domain_key=item.domain_key,
            source_timestamp=item.source_timestamp,
            artifact_uri=str(destination),
            policy=item.policy,
            metadata={"title": item.title, **item.metadata},
        )
        ledger = IngestionLedgerService(self.session)
        ledger.ensure_registration(
            key=item.source_registration_key,
            source_system=item.source_system,
            display_name=item.title,
            adapter_type=str(item.metadata.get("adapter_type") or "context_gateway"),
            domain=domain,
            policy=item.policy,
            config=item.metadata.get("source_config") if isinstance(item.metadata.get("source_config"), dict) else None,
        )
        claim = ledger.claim(envelope, domain=domain)
        if not claim.should_process:
            return GatewayIngestResult("duplicate", None, str(claim.record.id), claim.duplicate_status)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item.content, encoding="utf-8")
        ledger.mark_processed(claim.record, processed_path=destination)
        return GatewayIngestResult("staged", str(destination), str(claim.record.id))


class ToolEvidenceLedgerService:
    """Records completed external reads; workflow reports remain the interpreted artifact."""

    SUPPORTED_PREFIXES = ("github.", "gmail.", "google.")

    def __init__(self, session: Session):
        self.session = session

    def record(
        self,
        *,
        tool_call_id: str,
        tool_key: str,
        domain: Domain,
        output: dict[str, Any] | None,
        task_id: str,
    ) -> IngestionRecord | None:
        if not tool_key.startswith(self.SUPPORTED_PREFIXES) or output is None:
            return None
        family = "google" if tool_key.startswith(("gmail.", "google.")) else "github"
        serialized = json.dumps(output, sort_keys=True, default=str)
        content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        policy = SourcePolicy(
            sensitivity="business_confidential" if domain.key != "personal" else "personal",
            trust_level="system_observed",
            transfer_method="authorized_tool_call",
        )
        envelope = ContextEnvelope(
            source_registration_key=f"tool:{family}:{domain.key}",
            source_system=family,
            external_id=tool_call_id,
            source_version=content_hash,
            content_hash=content_hash,
            content_type="tool_result",
            domain_key=domain.key,
            source_timestamp=datetime.now(UTC),
            artifact_uri=f"tool-call:{tool_call_id}",
            policy=policy,
            metadata={"tool_key": tool_key, "task_id": task_id, "summary": output.get("summary")},
        )
        ledger = IngestionLedgerService(self.session)
        ledger.ensure_registration(key=envelope.source_registration_key, source_system=family, display_name=f"{domain.name} {family} tool evidence", adapter_type="tool_result", domain=domain, policy=policy)
        claim = ledger.claim(envelope, domain=domain)
        if claim.should_process:
            claim.record.status = "processed"
            claim.record.processed_at = datetime.now(UTC)
            claim.record.metadata_ = {**(claim.record.metadata_ or {}), "tool_call_id": tool_call_id}
            self.session.commit()
        return claim.record


def parse_sanitized_context_manifest(text: str, *, expected_domain: str | None = None) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        raise ValueError("Sanitized context drops require a YAML-style metadata header.")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ValueError("Sanitized context metadata header is not closed.")
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip().strip('"\'')
    required = {"domain", "reviewed_by", "reviewed_at", "contains_restricted", "source_system"}
    missing = sorted(required - metadata.keys())
    if missing:
        raise ValueError(f"Sanitized context metadata is missing: {', '.join(missing)}")
    if metadata["domain"] not in {"usma", "l3"}:
        raise ValueError("Sanitized context domain must be usma or l3.")
    if expected_domain and metadata["domain"] != expected_domain:
        raise ValueError("Manifest domain does not match the selected destination.")
    if metadata["contains_restricted"].lower() not in {"false", "no"}:
        raise ValueError("Context marked as restricted cannot enter personal Maestro.")
    body = parts[2].strip()
    if not body:
        raise ValueError("Sanitized context drop has no content.")
    return metadata, body


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:70] or "context"
