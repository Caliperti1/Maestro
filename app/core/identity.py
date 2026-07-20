"""Deterministic identity rules for the Maestro system owner."""

import re
from dataclasses import dataclass

from app.core.config import get_settings


@dataclass(frozen=True)
class MaestroUserIdentity:
    display_name: str
    full_name: str
    email: str

    def attendee_payload(self) -> dict[str, str | bool]:
        return {
            "name": self.full_name,
            "email": self.email,
            "is_user": True,
            "identity": "maestro_user",
        }


def maestro_user_identity() -> MaestroUserIdentity:
    settings = get_settings()
    return MaestroUserIdentity(
        display_name=settings.user_display_name.strip(),
        full_name=settings.user_full_name.strip(),
        email=settings.user_email.strip().lower(),
    )


def is_maestro_user_reference(*, name: str | None = None, email: str | None = None) -> bool:
    identity = maestro_user_identity()
    candidate_email = (email or "").strip().lower()
    combined = " ".join(value for value in (name, email) if value)
    embedded_emails = {value.lower() for value in re.findall(r"[\w.+-]+@[\w.-]+", combined)}
    if identity.email and (candidate_email == identity.email or identity.email in embedded_emails):
        return True

    normalized = _normalize_person_reference(name or "")
    if not normalized:
        return False
    full_name = _normalize_person_reference(identity.full_name)
    parts = full_name.split()
    aliases = {full_name, "me", "myself", "maestro user", "the user"}
    if len(parts) >= 2:
        aliases.update(
            {
                f"{parts[0]} {parts[-1][0]}",
                f"{parts[0]} {parts[-1][0]}.",
                f"{parts[0][0]} {parts[-1]}",
            }
        )
    return normalized in {_normalize_person_reference(alias) for alias in aliases}


def _normalize_person_reference(value: str) -> str:
    without_email = re.sub(r"<[^>]*@[^>]*>", " ", value.lower())
    normalized = re.sub(r"[^a-z0-9]+", " ", without_email).strip()
    return re.sub(r"\s+", " ", normalized)
