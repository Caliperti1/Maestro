"""Source normalization, policy, idempotency, and health for memory ingestion."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Artifact,
    Domain,
    IngestionRecord,
    SeedPackage,
    SourceCheckpoint,
    SourceRegistration,
)

EgressPolicy = Literal["external_allowed", "local_only"]
IngestionTarget = Literal["human", "local", "external"]


@dataclass(frozen=True)
class SourcePolicy:
    """Policy that travels with evidence and every memory derived from it."""

    sensitivity: str
    trust_level: str
    transfer_method: str
    egress_policy: EgressPolicy = "external_allowed"
    retention: str = "durable_source"
    requires_human_review: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextEnvelope:
    """Normalized evidence handed from a source adapter to the staging pipeline."""

    source_registration_key: str
    source_system: str
    external_id: str
    source_version: str
    content_hash: str
    content_type: str
    domain_key: str
    source_timestamp: datetime | None
    artifact_uri: str
    policy: SourcePolicy
    metadata: dict[str, Any] = field(default_factory=dict)

    def provenance(self, *, ingested_at: datetime | None = None) -> dict[str, Any]:
        return {
            "source_registration_key": self.source_registration_key,
            "source_system": self.source_system,
            "source_id": self.external_id,
            "source_version": self.source_version,
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp is not None else None
            ),
            "ingested_at": (ingested_at or datetime.now(UTC)).isoformat(),
            "content_hash": self.content_hash,
            "transfer_method": self.policy.transfer_method,
            "sensitivity": self.policy.sensitivity,
            "trust_level": self.policy.trust_level,
            "egress_policy": self.policy.egress_policy,
        }


class ContextSourceAdapter(Protocol):
    """Minimal contract for future pull, push, and file-based context sources."""

    key: str

    def fetch(self, checkpoint: dict[str, Any] | None = None) -> list[Any]: ...

    def normalize(self, source_object: Any) -> ContextEnvelope: ...


@dataclass(frozen=True)
class IngestionClaim:
    record: IngestionRecord
    should_process: bool
    duplicate_status: str | None = None


def policy_for_domain(domain_key: str) -> SourcePolicy:
    if domain_key == "usma":
        return SourcePolicy(
            sensitivity="sanitized_work_context",
            trust_level="user_reviewed",
            transfer_method="sanitized_context_drop",
            egress_policy="external_allowed",
        )
    if domain_key in {"praxis", "perti-laboratories", "maestro-development"}:
        return SourcePolicy(
            sensitivity="business_confidential",
            trust_level="user_provided",
            transfer_method="manual_drop",
        )
    return SourcePolicy(
        sensitivity="personal",
        trust_level="user_provided",
        transfer_method="manual_drop",
    )


_EMBEDDED_ENVELOPE_PREFIX = "<!-- maestro-context-envelope "
_EMBEDDED_ENVELOPE_SUFFIX = " -->"


def envelope_for_file(
    path: Path,
    *,
    domain_key: str,
    source_timestamp: datetime | None = None,
    policy: SourcePolicy | None = None,
) -> ContextEnvelope:
    embedded = _embedded_envelope(path, expected_domain_key=domain_key)
    if embedded is not None:
        return embedded
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    timestamp = source_timestamp or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    source_policy = policy or policy_for_domain(domain_key)
    return ContextEnvelope(
        source_registration_key=f"dropbox:{domain_key}",
        source_system="manual_dropbox",
        external_id=path.name,
        source_version=content_hash,
        content_hash=content_hash,
        content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        domain_key=domain_key,
        source_timestamp=timestamp,
        artifact_uri=str(path),
        policy=source_policy,
    )


def embed_context_envelope(envelope: ContextEnvelope, content: str) -> str:
    payload = {
        "source_registration_key": envelope.source_registration_key,
        "source_system": envelope.source_system,
        "external_id": envelope.external_id,
        "source_version": envelope.source_version,
        "content_hash": envelope.content_hash,
        "content_type": envelope.content_type,
        "domain_key": envelope.domain_key,
        "source_timestamp": (
            envelope.source_timestamp.isoformat() if envelope.source_timestamp else None
        ),
        "policy": envelope.policy.as_dict(),
        "metadata": envelope.metadata,
    }
    header = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{_EMBEDDED_ENVELOPE_PREFIX}{header}{_EMBEDDED_ENVELOPE_SUFFIX}\n{content}"


def strip_context_envelope(content: str) -> str:
    if not content.startswith(_EMBEDDED_ENVELOPE_PREFIX):
        return content
    _, separator, remainder = content.partition("\n")
    return remainder if separator else ""


def _embedded_envelope(path: Path, *, expected_domain_key: str) -> ContextEnvelope | None:
    if path.suffix.lower() not in {".md", ".txt"}:
        return None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        first_line = handle.readline().rstrip("\n")
    if not (
        first_line.startswith(_EMBEDDED_ENVELOPE_PREFIX)
        and first_line.endswith(_EMBEDDED_ENVELOPE_SUFFIX)
    ):
        return None
    raw = first_line[len(_EMBEDDED_ENVELOPE_PREFIX) : -len(_EMBEDDED_ENVELOPE_SUFFIX)]
    payload = json.loads(raw)
    domain_key = str(payload.get("domain_key") or "")
    if domain_key != expected_domain_key:
        raise ValueError("Embedded context envelope domain does not match its staging inbox.")
    policy_payload = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    source_timestamp = payload.get("source_timestamp")
    return ContextEnvelope(
        source_registration_key=str(payload["source_registration_key"]),
        source_system=str(payload["source_system"]),
        external_id=str(payload["external_id"]),
        source_version=str(payload["source_version"]),
        content_hash=str(payload["content_hash"]),
        content_type=str(payload["content_type"]),
        domain_key=domain_key,
        source_timestamp=(
            datetime.fromisoformat(str(source_timestamp).replace("Z", "+00:00"))
            if source_timestamp
            else None
        ),
        artifact_uri=str(path),
        policy=SourcePolicy(
            sensitivity=str(policy_payload.get("sensitivity") or "personal"),
            trust_level=str(policy_payload.get("trust_level") or "user_provided"),
            transfer_method=str(policy_payload.get("transfer_method") or "context_gateway"),
            egress_policy=str(policy_payload.get("egress_policy") or "external_allowed"),
            retention=str(policy_payload.get("retention") or "durable_source"),
            requires_human_review=bool(policy_payload.get("requires_human_review", False)),
        ),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    )


def memory_allowed_for_target(
    memory_metadata: dict[str, Any] | None,
    target: IngestionTarget,
) -> bool:
    if target != "external":
        return True
    policy = (memory_metadata or {}).get("source_policy")
    return not isinstance(policy, dict) or policy.get("egress_policy") != "local_only"


def payload_allowed_for_target(payload: dict[str, Any], target: IngestionTarget) -> bool:
    """Conservatively protect routed objects assembled from one or more sources."""

    if target != "external":
        return True
    if not memory_allowed_for_target(payload.get("metadata"), target):
        return False
    refs: list[Any] = []
    refs.extend(payload.get("source_refs") or [])
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        refs.extend(provenance.get("source_refs") or [])
    return not any(
        isinstance(ref, dict) and ref.get("egress_policy") == "local_only" for ref in refs
    )


class IngestionLedgerService:
    """Records source identity and processing state without deciding durable truth."""

    def __init__(self, session: Session):
        self.session = session

    def ensure_registration(
        self,
        *,
        key: str,
        source_system: str,
        display_name: str,
        adapter_type: str,
        domain: Domain | None,
        policy: SourcePolicy,
        config: dict[str, Any] | None = None,
    ) -> SourceRegistration:
        registration = self.session.scalar(
            select(SourceRegistration).where(SourceRegistration.key == key)
        )
        changed = False
        if registration is None:
            registration = SourceRegistration(
                key=key,
                source_system=source_system,
                display_name=display_name,
                adapter_type=adapter_type,
                domain_id=domain.id if domain is not None else None,
                policy=policy.as_dict(),
                config=config or {},
            )
            self.session.add(registration)
            changed = True
        else:
            updates = {
                "source_system": source_system,
                "display_name": display_name,
                "adapter_type": adapter_type,
                "domain_id": domain.id if domain is not None else None,
                "policy": policy.as_dict(),
                "config": config if config is not None else registration.config,
                "is_active": True,
            }
            for field_name, value in updates.items():
                if getattr(registration, field_name) != value:
                    setattr(registration, field_name, value)
                    changed = True
        if changed:
            self.session.commit()
            self.session.refresh(registration)
        return registration

    def claim(
        self,
        envelope: ContextEnvelope,
        *,
        domain: Domain | None,
        resume_staged: bool = False,
    ) -> IngestionClaim:
        registration = self.session.scalar(
            select(SourceRegistration).where(
                SourceRegistration.key == envelope.source_registration_key
            )
        )
        if registration is None:
            registration = self.ensure_registration(
                key=envelope.source_registration_key,
                source_system=envelope.source_system,
                display_name=f"{envelope.domain_key} context dropbox",
                adapter_type="filesystem_dropbox",
                domain=domain,
                policy=envelope.policy,
            )
        record = self.session.scalar(
            select(IngestionRecord).where(
                IngestionRecord.source_registration_id == registration.id,
                IngestionRecord.external_id == envelope.external_id,
                IngestionRecord.source_version == envelope.source_version,
            )
        )
        duplicate_statuses = {"processed", "duplicate", "processing"}
        if not resume_staged:
            duplicate_statuses.add("staged")
        if record is not None and record.status in duplicate_statuses:
            metadata = dict(record.metadata_ or {})
            metadata["last_duplicate_at"] = datetime.now(UTC).isoformat()
            record.metadata_ = metadata
            record.duplicate_count += 1
            self.session.commit()
            return IngestionClaim(
                record=record,
                should_process=False,
                duplicate_status=record.status,
            )

        now = datetime.now(UTC)
        if record is None:
            record = IngestionRecord(
                source_registration_id=registration.id,
                domain_id=domain.id if domain is not None else None,
                external_id=envelope.external_id,
                source_version=envelope.source_version,
                content_hash=envelope.content_hash,
                content_type=envelope.content_type,
                source_timestamp=envelope.source_timestamp,
                status="processing",
                attempt_count=1,
                processing_started_at=now,
                policy=envelope.policy.as_dict(),
                metadata_={"artifact_uri": envelope.artifact_uri, **envelope.metadata},
            )
            self.session.add(record)
        else:
            record.status = "processing"
            record.attempt_count += 1
            record.processing_started_at = now
            record.processed_at = None
            record.last_error = None
            record.policy = envelope.policy.as_dict()
            record.metadata_ = {"artifact_uri": envelope.artifact_uri, **envelope.metadata}
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            record = self.session.scalar(
                select(IngestionRecord).where(
                    IngestionRecord.source_registration_id == registration.id,
                    IngestionRecord.external_id == envelope.external_id,
                    IngestionRecord.source_version == envelope.source_version,
                )
            )
            if record is None:
                raise
            record.duplicate_count += 1
            record.metadata_ = {
                **(record.metadata_ or {}),
                "last_duplicate_at": datetime.now(UTC).isoformat(),
            }
            self.session.commit()
            return IngestionClaim(
                record=record,
                should_process=False,
                duplicate_status=record.status,
            )
        self.session.refresh(record)
        return IngestionClaim(record=record, should_process=True)

    def attach_staging(
        self,
        record: IngestionRecord,
        *,
        seed_package: SeedPackage,
        artifact: Artifact,
    ) -> None:
        record.seed_package_id = seed_package.id
        record.artifact_id = artifact.id
        self.session.commit()

    def mark_processed(self, record: IngestionRecord, *, processed_path: Path) -> None:
        record.status = "processed"
        record.processed_at = datetime.now(UTC)
        record.last_error = None
        record.metadata_ = {**(record.metadata_ or {}), "processed_path": str(processed_path)}
        self.session.commit()

    def mark_staged(self, record: IngestionRecord, *, staged_path: Path) -> None:
        record.status = "staged"
        record.processing_started_at = None
        record.last_error = None
        record.metadata_ = {**(record.metadata_ or {}), "staged_path": str(staged_path)}
        self.session.commit()

    def mark_failed(self, record: IngestionRecord, *, error: str, failed_path: Path) -> None:
        record.status = "failed"
        record.processed_at = datetime.now(UTC)
        record.last_error = error
        record.metadata_ = {**(record.metadata_ or {}), "failed_path": str(failed_path)}
        self.session.commit()

    def update_checkpoint(
        self,
        registration: SourceRegistration,
        *,
        cursor_key: str,
        cursor_value: dict[str, Any],
    ) -> SourceCheckpoint:
        checkpoint = self.session.scalar(
            select(SourceCheckpoint).where(
                SourceCheckpoint.source_registration_id == registration.id,
                SourceCheckpoint.cursor_key == cursor_key,
            )
        )
        if checkpoint is None:
            checkpoint = SourceCheckpoint(
                source_registration_id=registration.id,
                cursor_key=cursor_key,
            )
            self.session.add(checkpoint)
        checkpoint.cursor_value = cursor_value
        checkpoint.status = "ready"
        checkpoint.last_success_at = datetime.now(UTC)
        checkpoint.last_error = None
        self.session.commit()
        self.session.refresh(checkpoint)
        return checkpoint

    def recover_stale(self, *, stale_after: timedelta = timedelta(minutes=30)) -> int:
        cutoff = datetime.now(UTC) - stale_after
        records = list(
            self.session.scalars(
                select(IngestionRecord).where(
                    IngestionRecord.status == "processing",
                    IngestionRecord.processing_started_at < cutoff,
                )
            ).all()
        )
        now = datetime.now(UTC)
        for record in records:
            record.status = "failed"
            record.processed_at = now
            record.last_error = "Recovered stale ingestion after interrupted processing."
            if record.seed_package_id is not None:
                seed_package = self.session.get(SeedPackage, record.seed_package_id)
                if seed_package is not None and seed_package.status == "processing":
                    seed_package.status = "failed"
                    seed_package.processed_at = now
                    seed_package.metadata_ = {
                        **(seed_package.metadata_ or {}),
                        "error": record.last_error,
                    }
        linked_package_ids = {
            record.seed_package_id for record in records if record.seed_package_id
        }
        legacy_packages = list(
            self.session.scalars(
                select(SeedPackage).where(
                    SeedPackage.status == "processing",
                    SeedPackage.updated_at < cutoff,
                )
            ).all()
        )
        for seed_package in legacy_packages:
            if seed_package.id in linked_package_ids:
                continue
            seed_package.status = "failed"
            seed_package.processed_at = now
            seed_package.metadata_ = {
                **(seed_package.metadata_ or {}),
                "error": "Recovered stale legacy seed package after interrupted processing.",
            }
        self.session.commit()
        return len(records) + len(
            [package for package in legacy_packages if package.id not in linked_package_ids]
        )

    def status(self) -> dict[str, Any]:
        rows = self.session.execute(
            select(IngestionRecord.status, func.count(IngestionRecord.id)).group_by(
                IngestionRecord.status
            )
        ).all()
        registrations = self.session.scalar(select(func.count(SourceRegistration.id))) or 0
        duplicates_skipped = self.session.scalar(
            select(func.coalesce(func.sum(IngestionRecord.duplicate_count), 0))
        )
        return {
            "source_registrations": int(registrations),
            "records": {str(status): int(count) for status, count in rows},
            "duplicates_skipped": int(duplicates_skipped or 0),
        }
