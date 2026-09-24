from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import boto3

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class StoredObject:
    key: str
    uri: str
    size: int
    sha256: str
    content_type: str


class ArtifactStore(Protocol):
    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject: ...

    def get_bytes(self, key: str) -> bytes: ...


def normalize_key(value: str) -> str:
    key = str(PurePosixPath(value.strip().lstrip("/")))
    if not key or key == "." or key.startswith("../") or "/../" in key:
        raise ValueError("Artifact key must stay within the configured store prefix.")
    return key


def _content_type(key: str, explicit: str | None) -> str:
    return explicit or mimetypes.guess_type(key)[0] or "application/octet-stream"


class LocalArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        normalized = normalize_key(key)
        destination = (self.root / normalized).resolve()
        if self.root not in destination.parents:
            raise ValueError("Artifact key escaped the local store root.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return StoredObject(
            key=normalized,
            uri=str(destination),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=_content_type(normalized, content_type),
        )

    def get_bytes(self, key: str) -> bytes:
        normalized = normalize_key(key)
        source = (self.root / normalized).resolve()
        if self.root not in source.parents:
            raise ValueError("Artifact key escaped the local store root.")
        return source.read_bytes()


class S3ArtifactStore:
    def __init__(self, settings: Settings, *, client=None):
        if not settings.artifact_store_s3_bucket:
            raise ValueError("ARTIFACT_STORE_S3_BUCKET is required for the S3 artifact store.")
        self.bucket = settings.artifact_store_s3_bucket
        self.prefix = normalize_key(settings.artifact_store_s3_prefix)
        self.kms_key_id = settings.artifact_store_s3_kms_key_id
        self.client = client or boto3.client("s3", region_name=settings.artifact_store_s3_region)

    def _object_key(self, key: str) -> str:
        return f"{self.prefix}/{normalize_key(key)}"

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        normalized = normalize_key(key)
        object_key = self._object_key(normalized)
        request = {
            "Bucket": self.bucket,
            "Key": object_key,
            "Body": data,
            "ContentType": _content_type(normalized, content_type),
            "Metadata": {"sha256": hashlib.sha256(data).hexdigest()},
            "ServerSideEncryption": "aws:kms" if self.kms_key_id else "AES256",
        }
        if self.kms_key_id:
            request["SSEKMSKeyId"] = self.kms_key_id
        self.client.put_object(**request)
        return StoredObject(
            key=normalized,
            uri=f"s3://{self.bucket}/{object_key}",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=request["ContentType"],
        )

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))
        return response["Body"].read()


def get_artifact_store(settings: Settings | None = None) -> ArtifactStore:
    configured = settings or get_settings()
    if configured.artifact_store_backend == "s3":
        return S3ArtifactStore(configured)
    return LocalArtifactStore(
        configured.artifact_store_local_root or configured.memory_dropbox_root
    )
