from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import OwnerSession
from app.nodes.security import issue_token, token_hash, token_resource_id


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class AuthenticatedOwner:
    session_id: uuid.UUID
    issuer: str
    subject: str
    expires_at: datetime


class OwnerSessionService:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def create(
        self,
        *,
        issuer: str,
        subject: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[OwnerSession, str]:
        now = utcnow()
        session_id = uuid.uuid4()
        raw_token = issue_token("mos", session_id)
        record = OwnerSession(
            id=session_id,
            token_hash=token_hash(raw_token),
            issuer=issuer,
            subject=subject,
            expires_at=now + timedelta(seconds=self.settings.owner_session_ttl_seconds),
            last_seen_at=now,
            metadata_=metadata or {},
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record, raw_token

    def authenticate(self, raw_token: str | None, *, touch: bool = False) -> AuthenticatedOwner | None:
        session_id = token_resource_id(raw_token or "", prefix="mos")
        if session_id is None or not raw_token:
            return None
        record = self.session.get(OwnerSession, session_id)
        now = utcnow()
        if (
            record is None
            or record.revoked_at is not None
            or _aware(record.expires_at) <= now
            or not hmac.compare_digest(record.token_hash, token_hash(raw_token))
        ):
            return None
        if touch and _aware(record.last_seen_at) < now - timedelta(minutes=5):
            record.last_seen_at = now
            self.session.commit()
        return AuthenticatedOwner(
            session_id=record.id,
            issuer=record.issuer,
            subject=record.subject,
            expires_at=_aware(record.expires_at),
        )

    def revoke(self, raw_token: str | None) -> None:
        session_id = token_resource_id(raw_token or "", prefix="mos")
        if session_id is None:
            return
        record = self.session.get(OwnerSession, session_id)
        if record is not None and record.revoked_at is None:
            record.revoked_at = utcnow()
            self.session.commit()

    def revoke_expired(self) -> int:
        now = utcnow()
        records = self.session.scalars(
            select(OwnerSession).where(
                OwnerSession.revoked_at.is_(None),
                OwnerSession.expires_at <= now,
            )
        ).all()
        for record in records:
            record.revoked_at = now
        if records:
            self.session.commit()
        return len(records)


def csrf_token(settings: Settings, raw_session_token: str) -> str:
    if not settings.owner_session_secret:
        raise RuntimeError("OWNER_SESSION_SECRET is required when owner auth is enabled.")
    return hmac.new(
        settings.owner_session_secret.encode("utf-8"),
        f"csrf:{raw_session_token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def csrf_matches(settings: Settings, raw_session_token: str, provided: str | None) -> bool:
    return bool(provided) and hmac.compare_digest(
        csrf_token(settings, raw_session_token),
        provided,
    )
