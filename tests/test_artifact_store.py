from pathlib import Path

import pytest

from app.core.config import Settings
from app.storage.artifacts import LocalArtifactStore, S3ArtifactStore, normalize_key


class _Body:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


class _S3Client:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.last_put: dict | None = None

    def put_object(self, **kwargs):
        self.last_put = kwargs
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, *, Bucket: str, Key: str):
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int):
        keys = [
            {"Key": key}
            for bucket, key in sorted(self.objects)
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": keys[:MaxKeys]}

    def copy_object(self, *, Bucket: str, Key: str, CopySource: dict, **_kwargs):
        self.objects[(Bucket, Key)] = self.objects[(CopySource["Bucket"], CopySource["Key"])]

    def delete_object(self, *, Bucket: str, Key: str):
        self.objects.pop((Bucket, Key), None)


def test_local_artifact_store_round_trip(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    stored = store.put_bytes("personal/inbox/note.txt", b"hello", content_type="text/plain")

    assert stored.uri == str(tmp_path / "personal" / "inbox" / "note.txt")
    assert stored.size == 5
    assert store.get_bytes(stored.key) == b"hello"


def test_artifact_keys_cannot_escape_store() -> None:
    with pytest.raises(ValueError):
        normalize_key("../../secret")


def test_s3_artifact_store_is_private_and_encrypted() -> None:
    settings = Settings(
        _env_file=None,
        artifact_store_backend="s3",
        artifact_store_s3_bucket="maestro-private",
        artifact_store_s3_region="us-west-2",
        artifact_store_s3_prefix="staging",
    )
    client = _S3Client()
    store = S3ArtifactStore(settings, client=client)

    stored = store.put_bytes("personal/inbox/note.txt", b"hello")

    assert stored.uri == "s3://maestro-private/staging/personal/inbox/note.txt"
    assert client.last_put["ServerSideEncryption"] == "AES256"
    assert "ACL" not in client.last_put
    assert store.get_bytes(stored.key) == b"hello"

    assert store.list_keys("personal/inbox") == ["personal/inbox/note.txt"]
    moved_uri = store.move("personal/inbox/note.txt", "personal/processed/note.txt")
    assert moved_uri == "s3://maestro-private/staging/personal/processed/note.txt"
    assert store.list_keys("personal/inbox") == []
    assert store.get_bytes("personal/processed/note.txt") == b"hello"


def test_local_artifact_store_lists_and_moves(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put_bytes("personal/inbox/note.txt", b"hello")

    assert store.list_keys("personal/inbox") == ["personal/inbox/note.txt"]
    moved_uri = store.move("personal/inbox/note.txt", "personal/processed/note.txt")

    assert moved_uri == str(tmp_path / "personal" / "processed" / "note.txt")
    assert store.get_bytes("personal/processed/note.txt") == b"hello"
