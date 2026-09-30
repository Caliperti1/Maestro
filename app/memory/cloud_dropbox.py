"""Bounded S3-backed inbox adapter for Maestro's existing memory curator."""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.repositories import DomainRepository
from app.memory.document_extract import SUPPORTED_DROPBOX_SUFFIXES
from app.memory.dropbox import DropboxProcessResult, MemoryDropboxProcessor
from app.storage.artifacts import ArtifactStore, get_artifact_store


class CloudMemoryDropboxProcessor:
    """Stage a small object-store batch locally, then reuse the canonical curator pipeline."""

    def __init__(self, session: Session, *, store: ArtifactStore | None = None):
        self.session = session
        self.store = store or get_artifact_store()

    def process_once(self, *, limit: int | None = None) -> list[DropboxProcessResult]:
        batch_limit = limit or get_settings().memory_dropbox_batch_size
        results: list[DropboxProcessResult] = []
        domains = {"global": None}
        domains.update({domain.key: domain for domain in DomainRepository(self.session).list_active()})

        with tempfile.TemporaryDirectory(prefix="maestro-cloud-dropbox-") as temporary_root:
            processor = MemoryDropboxProcessor(self.session, root=Path(temporary_root))
            processor.ensure_directories()
            for domain_key, domain in domains.items():
                if len(results) >= batch_limit:
                    break
                remaining = batch_limit - len(results)
                # Interrupted objects remain under processing and are resumed before new inbox work.
                processing_keys = self.store.list_keys(
                    f"{domain_key}/processing",
                    limit=remaining,
                )
                inbox_keys = self.store.list_keys(
                    f"{domain_key}/inbox",
                    limit=remaining,
                )
                for key in [*processing_keys, *inbox_keys]:
                    if len(results) >= batch_limit:
                        break
                    suffix = PurePosixPath(key).suffix.lower()
                    if suffix not in SUPPORTED_DROPBOX_SUFFIXES:
                        continue
                    filename = PurePosixPath(key).name
                    processing_key = f"{domain_key}/processing/{filename}"
                    if "/inbox/" in f"/{key}":
                        self.store.move(key, processing_key)
                    local_path = Path(temporary_root) / domain_key / "processing" / filename
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(self.store.get_bytes(processing_key))
                    processed_key = f"{domain_key}/processed/{filename}"
                    failed_key = f"{domain_key}/failed/{filename}"
                    result = processor.process_file(
                        local_path,
                        domain_key=domain_key,
                        domain=domain,
                        original_path=Path(filename),
                        source_uri=self.store.uri_for(processing_key),
                        processed_uri=self.store.uri_for(processed_key),
                        failed_uri=self.store.uri_for(failed_key),
                    )
                    final_key = failed_key if result.status == "failed" else processed_key
                    self.store.move(processing_key, final_key)
                    if result.preview_path and result.preview_path.exists():
                        self.store.put_bytes(
                            f"{domain_key}/previews/{result.preview_path.name}",
                            result.preview_path.read_bytes(),
                            content_type="application/json",
                        )
                    results.append(result)
        return results
