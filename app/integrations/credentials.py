from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings, get_settings


class CredentialEncryptionError(RuntimeError):
    """Raised when integration credentials cannot be encrypted or decrypted."""


def encrypt_credential_bundle(
    value: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> str:
    cipher = _cipher(settings or get_settings())
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return cipher.encrypt(payload).decode("ascii")


def decrypt_credential_bundle(
    ciphertext: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    try:
        payload = _cipher(settings or get_settings()).decrypt(ciphertext.encode("ascii"))
        value = json.loads(payload.decode("utf-8"))
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CredentialEncryptionError(
            "Stored integration credentials could not be decrypted."
        ) from exc
    if not isinstance(value, dict):
        raise CredentialEncryptionError("Stored integration credentials are invalid.")
    return value


def encrypted_credentials(config: dict[str, Any]) -> dict[str, Any]:
    ciphertext = str(config.get("oauth_credential_ciphertext") or "").strip()
    if not ciphertext:
        return {}
    return decrypt_credential_bundle(ciphertext)


def _cipher(settings: Settings) -> Fernet:
    raw_key = str(settings.integration_credential_encryption_key or "").strip()
    if not raw_key:
        raise CredentialEncryptionError(
            "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY is required for one-click connections."
        )
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, TypeError):
        # Accept a high-entropy passphrase as well as a pre-generated Fernet key.
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode("utf-8")).digest())
        return Fernet(derived)
