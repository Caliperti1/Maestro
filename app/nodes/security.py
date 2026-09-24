"""Opaque-token helpers for node enrollment, authentication, and job leases."""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def token_hash(token: str) -> str:
    """Hash a high-entropy opaque token before persistence."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(prefix: str, resource_id: uuid.UUID) -> str:
    return f"{prefix}_{resource_id}_{secrets.token_urlsafe(32)}"


def token_resource_id(token: str, *, prefix: str) -> uuid.UUID | None:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != prefix or not parts[2]:
        return None
    try:
        return uuid.UUID(parts[1])
    except ValueError:
        return None


def token_matches(token: str, expected_hash: str | None) -> bool:
    return bool(expected_hash) and hmac.compare_digest(token_hash(token), expected_hash)


def verify_payload_signature(
    public_key: str | None,
    payload: dict[str, Any],
    signature: str | None,
) -> bool:
    """Verify an Ed25519 signature over the node protocol's canonical JSON encoding."""
    if not public_key or not signature:
        return False
    try:
        public_bytes = base64.urlsafe_b64decode(public_key.encode("ascii"))
        signature_bytes = base64.urlsafe_b64decode(signature.encode("ascii"))
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(signature_bytes, canonical)
    except (ValueError, TypeError, binascii.Error, InvalidSignature, UnicodeEncodeError):
        return False
    return True
