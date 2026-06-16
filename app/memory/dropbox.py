import argparse
import json
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Artifact, Domain, SeedPackage
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.db.session import SessionLocal
from app.llm import LLMMemoryExtractor, OpenAILLMClient
from app.memory import LLMMemoryCurator, StagedMemorySource
from app.memory.document_extract import SUPPORTED_DROPBOX_SUFFIXES, extract_dropbox_text
from app.memory.service import MemoryCandidate, MemoryWriteResult

DROPBOX_SUBDIRS = ("inbox", "processed", "failed", "previews")


@dataclass(frozen=True)
class DropboxProcessResult:
    source_path: Path
    destination_path: Path
    preview_path: Path | None
    status: str
    candidate_count: int = 0
    written_count: int = 0
    pending_approval_count: int = 0
    error: str | None = None


class MemoryDropboxProcessor:
    def __init__(
        self,
        session: Session,
        *,
        root: Path | None = None,
        curator: LLMMemoryCurator | None = None,
    ):
        self.session = session
        self.root = root or Path(get_settings().memory_dropbox_root)
        self.curator = curator

    def ensure_directories(self) -> list[Path]:
        seed_default_domains(self.session)
        domain_keys = ["global"] + [
            domain.key for domain in DomainRepository(self.session).list_active()
        ]
        created: list[Path] = []
        for domain_key in domain_keys:
            for subdir in DROPBOX_SUBDIRS:
                path = self.root / domain_key / subdir
                path.mkdir(parents=True, exist_ok=True)
                created.append(path)
        return created

    def process_once(self) -> list[DropboxProcessResult]:
        self.ensure_directories()
        results: list[DropboxProcessResult] = []
        for domain_key, domain in self._domains_by_key().items():
            inbox = self.root / domain_key / "inbox"
            for path in sorted(inbox.iterdir()):
                if not self._is_supported_file(path):
                    continue
                results.append(self.process_file(path, domain_key=domain_key, domain=domain))
        return results

    def process_file(
        self,
        path: Path,
        *,
        domain_key: str,
        domain: Domain | None,
    ) -> DropboxProcessResult:
        seed_package: SeedPackage | None = None
        preview_path: Path | None = None
        try:
            content, extraction_metadata = extract_dropbox_text(path)
            seed_package, artifact = self._record_source_artifact(
                path,
                domain,
                extraction_metadata=extraction_metadata,
            )
            source = StagedMemorySource(
                source_type="artifact",
                source_id=artifact.id,
                domain_id=domain.id if domain is not None else None,
                title=path.name,
                uri=str(path),
                content=content,
                metadata={
                    "dropbox_domain": domain_key,
                    "seed_package_id": str(seed_package.id),
                    "artifact_id": str(artifact.id),
                    "original_path": str(path),
                    **extraction_metadata,
                },
            )
            curator = self._curator()
            preview = curator.preview_source(source, domain_key=domain_key)
            preview_path = self._write_preview(
                path,
                domain_key=domain_key,
                candidates=preview.candidates,
                results=None,
                status="previewed",
            )
            batch = curator.write_candidates(source, preview.candidates)
            destination = self._move_file(path, domain_key=domain_key, status="processed")
            self._finalize_provenance(
                seed_package=seed_package,
                artifact=artifact,
                results=batch.results,
                processed_path=destination,
            )
            preview_path = self._write_preview(
                path,
                domain_key=domain_key,
                candidates=batch.candidates,
                results=batch.results,
                status="written",
            )
            seed_package.status = "processed"
            seed_package.processed_at = datetime.now(UTC)
            self.session.commit()
            return DropboxProcessResult(
                source_path=path,
                destination_path=destination,
                preview_path=preview_path,
                status="processed",
                candidate_count=len(batch.candidates),
                written_count=sum(1 for result in batch.results if result.memory_item is not None),
                pending_approval_count=batch.pending_approval_count,
            )
        except Exception as exc:
            self.session.rollback()
            if seed_package is not None:
                seed_package.status = "failed"
                seed_package.metadata_ = {
                    **(seed_package.metadata_ or {}),
                    "error": str(exc),
                }
                self.session.commit()
            destination = self._move_file(path, domain_key=domain_key, status="failed")
            self._write_failure(destination, str(exc))
            return DropboxProcessResult(
                source_path=path,
                destination_path=destination,
                preview_path=preview_path,
                status="failed",
                error=str(exc),
            )

    def _domains_by_key(self) -> dict[str, Domain | None]:
        domains: dict[str, Domain | None] = {"global": None}
        for domain in DomainRepository(self.session).list_active():
            domains[domain.key] = domain
        return domains

    def _curator(self) -> LLMMemoryCurator:
        if self.curator is None:
            self.curator = LLMMemoryCurator(
                self.session,
                LLMMemoryExtractor(OpenAILLMClient()),
            )
        return self.curator

    def _record_source_artifact(
        self,
        path: Path,
        domain: Domain | None,
        *,
        extraction_metadata: dict[str, Any],
    ) -> tuple[SeedPackage, Artifact]:
        seed_package = SeedPackage(
            domain_id=domain.id if domain is not None else None,
            name=path.name,
            source_type="dropbox_file",
            status="processing",
            metadata_={
                "original_path": str(path),
                "suffix": path.suffix.lower(),
                **extraction_metadata,
            },
        )
        self.session.add(seed_package)
        self.session.flush()

        artifact = Artifact(
            seed_package_id=seed_package.id,
            artifact_type="raw_file",
            name=path.name,
            uri=str(path),
            mime_type=self._mime_type(path),
            metadata_={
                "dropbox": True,
                "original_path": str(path),
                **extraction_metadata,
            },
        )
        self.session.add(artifact)
        self.session.commit()
        self.session.refresh(seed_package)
        self.session.refresh(artifact)
        return seed_package, artifact

    def _finalize_provenance(
        self,
        *,
        seed_package: SeedPackage,
        artifact: Artifact,
        results: list[MemoryWriteResult] | tuple[MemoryWriteResult, ...] | Any,
        processed_path: Path,
    ) -> None:
        processed_path_text = str(processed_path)
        artifact.uri = processed_path_text
        artifact.metadata_ = {
            **(artifact.metadata_ or {}),
            "processed_path": processed_path_text,
        }
        seed_package.metadata_ = {
            **(seed_package.metadata_ or {}),
            "processed_path": processed_path_text,
        }

        for result in results:
            if result.memory_item is not None:
                result.memory_item.metadata_ = self._metadata_with_processed_path(
                    result.memory_item.metadata_,
                    artifact_id=str(artifact.id),
                    processed_path=processed_path_text,
                )
            if result.proposal is not None:
                result.proposal.metadata_ = self._metadata_with_processed_path(
                    result.proposal.metadata_,
                    artifact_id=str(artifact.id),
                    processed_path=processed_path_text,
                )
                result.proposal.source_refs = self._source_refs_with_processed_path(
                    result.proposal.source_refs,
                    artifact_id=str(artifact.id),
                    processed_path=processed_path_text,
                )

    def _metadata_with_processed_path(
        self,
        metadata: dict[str, Any] | None,
        *,
        artifact_id: str,
        processed_path: str,
    ) -> dict[str, Any]:
        updated = dict(metadata or {})
        updated["processed_path"] = processed_path
        if updated.get("artifact_id") == artifact_id:
            updated["artifact_uri"] = processed_path
        if "source_refs" in updated:
            updated["source_refs"] = self._source_refs_with_processed_path(
                updated["source_refs"],
                artifact_id=artifact_id,
                processed_path=processed_path,
            )
        return updated

    def _source_refs_with_processed_path(
        self,
        source_refs: list[dict[str, Any]] | Any,
        *,
        artifact_id: str,
        processed_path: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(source_refs, list):
            return []
        updated_refs: list[dict[str, Any]] = []
        for source_ref in source_refs:
            if not isinstance(source_ref, dict):
                continue
            updated_ref = dict(source_ref)
            if updated_ref.get("id") == artifact_id:
                updated_ref["uri"] = processed_path
                updated_ref["processed_path"] = processed_path
            updated_refs.append(updated_ref)
        return updated_refs

    def _write_preview(
        self,
        source_path: Path,
        *,
        domain_key: str,
        candidates: list[MemoryCandidate] | tuple[MemoryCandidate, ...] | Any,
        results: list[MemoryWriteResult] | tuple[MemoryWriteResult, ...] | None,
        status: str,
    ) -> Path:
        preview_dir = self.root / domain_key / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = preview_dir / f"{source_path.stem}.preview.json"
        payload = {
            "source_file": source_path.name,
            "status": status,
            "generated_at": datetime.now(UTC).isoformat(),
            "candidates": [self._candidate_preview(candidate) for candidate in candidates],
            "results": []
            if results is None
            else [self._result_preview(result) for result in results],
        }
        preview_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return preview_path

    def _candidate_preview(self, candidate: MemoryCandidate) -> dict[str, Any]:
        return {
            "scope": candidate.scope,
            "memory_type": candidate.memory_type,
            "title": candidate.title,
            "content": candidate.content,
            "rationale": candidate.rationale,
            "impact_level": candidate.impact_level,
            "importance": candidate.importance,
            "source_refs": candidate.source_refs,
            "metadata": candidate.metadata,
        }

    def _result_preview(self, result: MemoryWriteResult) -> dict[str, Any]:
        return {
            "outcome": result.outcome,
            "memory_item_id": (
                str(result.memory_item.id) if result.memory_item is not None else None
            ),
            "proposal_id": str(result.proposal.id) if result.proposal is not None else None,
            "proposal_status": result.proposal.status if result.proposal is not None else None,
        }

    def _move_file(self, path: Path, *, domain_key: str, status: str) -> Path:
        destination_dir = self.root / domain_key / status
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = self._available_destination(destination_dir / path.name)
        shutil.move(str(path), destination)
        return destination

    def _available_destination(self, destination: Path) -> Path:
        if not destination.exists():
            return destination
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        return destination.with_name(f"{destination.stem}.{timestamp}{destination.suffix}")

    def _write_failure(self, failed_path: Path, error: str) -> None:
        error_path = failed_path.with_suffix(f"{failed_path.suffix}.error.json")
        payload = {
            "file": failed_path.name,
            "error": error,
            "failed_at": datetime.now(UTC).isoformat(),
        }
        error_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _is_supported_file(self, path: Path) -> bool:
        return path.is_file() and not path.name.startswith(".") and path.suffix.lower() in (
            SUPPORTED_DROPBOX_SUFFIXES
        )

    def _mime_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".md":
            return "text/markdown"
        if suffix == ".json":
            return "application/json"
        if suffix == ".csv":
            return "text/csv"
        if suffix == ".tsv":
            return "text/tab-separated-values"
        if suffix in {".html", ".htm"}:
            return "text/html"
        if suffix == ".pdf":
            return "application/pdf"
        if suffix == ".docx":
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return "text/plain"


def run_once(*, root: Path | None = None) -> list[DropboxProcessResult]:
    with SessionLocal() as session:
        return MemoryDropboxProcessor(session, root=root).process_once()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process Maestro memory dropbox files.")
    parser.add_argument("--root", type=Path, default=None, help="Override MEMORY_DROPBOX_ROOT.")
    parser.add_argument("--watch", action="store_true", help="Poll the dropbox until interrupted.")
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Watch polling interval in seconds.",
    )
    args = parser.parse_args()

    if not args.watch:
        _print_results(run_once(root=args.root))
        return

    while True:
        _print_results(run_once(root=args.root))
        time.sleep(args.interval)


def _print_results(results: list[DropboxProcessResult]) -> None:
    if not results:
        print("No supported files found in domain inboxes.")
        return
    for result in results:
        print(json.dumps(asdict(result), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
