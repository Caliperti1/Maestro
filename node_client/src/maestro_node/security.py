"""Device key generation and result-envelope signing."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .config import atomic_write_private


def create_device_key(path: Path) -> str:
    """Create an Ed25519 key and return its URL-safe base64 public key."""

    if path.exists():
        raise FileExistsError(f"Refusing to replace existing device key at {path}")
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    atomic_write_private(path, private_bytes.decode("ascii"))
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(public_bytes).decode("ascii")


def remove_device_key(path: Path) -> None:
    """Remove a key created during an enrollment attempt that did not complete."""

    try:
        path.unlink()
    except FileNotFoundError:
        return


def sign_payload(private_key_path: Path, payload: dict[str, Any]) -> str:
    key_data = private_key_path.read_bytes()
    os.chmod(private_key_path, 0o600)
    private_key = serialization.load_pem_private_key(key_data, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Configured device key is not an Ed25519 private key.")
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(private_key.sign(canonical)).decode("ascii")

