"""Dedicated Gmail intake adapter for structured external context handoffs."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.identity import is_maestro_user_reference
from app.db.models import Domain, RoutedItem, RuntimeSetting
from app.db.repositories import DomainRepository
from app.memory.context_gateway import ContextGatewayService, GatewayItem
from app.memory.document_extract import SUPPORTED_DROPBOX_SUFFIXES, extract_dropbox_text
from app.memory.ingestion import SourcePolicy, policy_for_domain
from app.memory.routed_service import RoutedMemoryService
from app.tools.runtime import (
    ToolExecutionError,
    _gmail_api_json,
    _gmail_message_payload,
    _google_oauth_refresh_access_token,
)

CONTEXT_MAILBOX_SETTING_KEY = "context_mailbox_worker"
CONTEXT_SUBJECT_PATTERN = re.compile(
    r"^\s*\[MAESTRO-CONTEXT\]\[([^\]]+)\]\[([^\]]+)\](?:\s+(.+))?$",
    re.IGNORECASE,
)
INGEST_SUBJECT_PATTERN = re.compile(
    r"^\s*\[MAESTRO-INGEST\]\[([^\]]+)\]\[([^\]]+)\](?:\s+(.+))?$",
    re.IGNORECASE,
)
TERMINAL_LABELS = {
    "processed": "Maestro/Processed",
    "failed": "Maestro/Failed",
    "quarantine": "Maestro/Quarantine",
}
DOMAIN_ALIASES = {
    "maestro": "maestro-development",
    "maestro development": "maestro-development",
    "personal": "personal",
    "perti": "perti-laboratories",
    "perti labs": "perti-laboratories",
    "perti laboratories": "perti-laboratories",
    "ophi": "perti-laboratories",
    "praxis": "praxis",
    "usma": "usma",
    "west point": "usma",
}
MAX_BODY_CHARS = 250_000
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
OUTLOOK_SOURCE_SYSTEMS = {"microsoft_outlook", "office365_outlook", "usma_outlook"}
WINDOWS_TIMEZONE_ALIASES = {
    "eastern standard time": "America/New_York",
    "utc": "UTC",
}


class ContextMailboxError(RuntimeError):
    """Raised when mailbox polling cannot continue safely."""


class ContextHandoffError(ValueError):
    """Raised when one message is not a valid context handoff."""


@dataclass(frozen=True)
class ParsedContextHandoff:
    source_system: str
    source_id: str
    source_timestamp: datetime
    domain_key: str
    title: str
    content: str
    metadata: dict[str, str]
    record_type: str | None = None


class ContextMailboxSource(Protocol):
    def profile(self) -> dict[str, Any]: ...

    def ensure_labels(self, names: list[str]) -> dict[str, str]: ...

    def inbox_message_ids(self, *, page_size: int) -> list[str]: ...

    def message(self, message_id: str) -> dict[str, Any]: ...

    def attachment(self, message_id: str, attachment_id: str) -> bytes: ...

    def label_message(
        self,
        message_id: str,
        *,
        add_label_ids: list[str],
        remove_label_ids: list[str],
    ) -> None: ...


class GoogleContextMailboxSource:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._token: str | None = None

    def _access_token(self) -> str:
        if self._token:
            return self._token
        if not self.settings.context_mailbox_configured:
            raise ContextMailboxError("Context mailbox OAuth credentials are incomplete.")
        token = _google_oauth_refresh_access_token(
            client_id=str(self.settings.maestro_intake_google_client_id),
            client_secret=str(self.settings.maestro_intake_google_client_secret),
            refresh_token=str(self.settings.maestro_intake_google_refresh_token),
        )
        self._token = str(token.get("access_token") or "").strip()
        if not self._token:
            raise ContextMailboxError("Google OAuth refresh did not return an access token.")
        return self._token

    def profile(self) -> dict[str, Any]:
        return _gmail_api_json(
            "GET",
            "/gmail/v1/users/me/profile",
            token=self._access_token(),
        )

    def ensure_labels(self, names: list[str]) -> dict[str, str]:
        response = _gmail_api_json(
            "GET",
            "/gmail/v1/users/me/labels",
            token=self._access_token(),
        )
        labels = {
            str(item.get("name")): str(item.get("id"))
            for item in response.get("labels") or []
            if isinstance(item, dict) and item.get("name") and item.get("id")
        }
        for name in names:
            if name in labels:
                continue
            created = _gmail_api_json(
                "POST",
                "/gmail/v1/users/me/labels",
                token=self._access_token(),
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            labels[name] = str(created["id"])
        return labels

    def inbox_message_ids(self, *, page_size: int) -> list[str]:
        response = _gmail_api_json(
            "GET",
            "/gmail/v1/users/me/messages",
            token=self._access_token(),
            params={
                "labelIds": "INBOX",
                "maxResults": page_size,
                "q": (
                    '-label:"Maestro/Processed" '
                    '-label:"Maestro/Failed" '
                    '-label:"Maestro/Quarantine"'
                ),
            },
        )
        return [
            str(item["id"])
            for item in response.get("messages") or []
            if isinstance(item, dict) and item.get("id")
        ]

    def message(self, message_id: str) -> dict[str, Any]:
        raw = _gmail_api_json(
            "GET",
            f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}",
            token=self._access_token(),
            params={"format": "full"},
        )
        return _gmail_message_payload(raw, max_body_chars=MAX_BODY_CHARS)

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = _gmail_api_json(
            "GET",
            (
                f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}/attachments/"
                f"{quote(attachment_id, safe='')}"
            ),
            token=self._access_token(),
        )
        data = str(response.get("data") or "")
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    def label_message(
        self,
        message_id: str,
        *,
        add_label_ids: list[str],
        remove_label_ids: list[str],
    ) -> None:
        _gmail_api_json(
            "POST",
            f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}/modify",
            token=self._access_token(),
            body={"addLabelIds": add_label_ids, "removeLabelIds": remove_label_ids},
        )


class ContextMailboxService:
    def __init__(
        self,
        session: Session,
        *,
        source: ContextMailboxSource | None = None,
        settings: Settings | None = None,
        gateway: ContextGatewayService | None = None,
    ):
        self.session = session
        self.settings = settings or get_settings()
        self.source = source or GoogleContextMailboxSource(self.settings)
        self.gateway = gateway or ContextGatewayService(session)

    def status(self) -> dict[str, Any]:
        stored = self.session.get(RuntimeSetting, CONTEXT_MAILBOX_SETTING_KEY)
        runtime = dict(stored.value or {}) if stored else {}
        return {
            "configured": self.settings.context_mailbox_configured,
            "enabled": bool(
                self.settings.context_mailbox_autorun
                and self.settings.context_mailbox_configured
            ),
            "mailbox": self.settings.maestro_intake_email,
            "interval_seconds": self.settings.context_mailbox_interval_seconds,
            "page_size": self.settings.context_mailbox_page_size,
            "allowed_sender_count": len(self.settings.context_mailbox_allowed_senders),
            "status": runtime.get("status") or (
                "not_configured" if not self.settings.context_mailbox_configured else "ready"
            ),
            "last_polled_at": runtime.get("last_polled_at"),
            "last_success_at": runtime.get("last_success_at"),
            "last_error": runtime.get("last_error"),
            "last_counts": runtime.get("last_counts") or {},
        }

    def poll_once(self, *, page_size: int | None = None) -> dict[str, Any]:
        if not self.settings.context_mailbox_configured:
            raise ContextMailboxError("Context mailbox is not configured.")
        if not self.settings.context_mailbox_allowed_senders:
            raise ContextMailboxError("Context mailbox sender allowlist is empty.")
        try:
            profile = self.source.profile()
            actual_email = str(profile.get("emailAddress") or "").lower()
            expected_email = str(self.settings.maestro_intake_email or "").lower()
            if actual_email != expected_email:
                raise ContextMailboxError(
                    "Context mailbox OAuth resolves to "
                    f"{actual_email or 'unknown'}, not {expected_email}."
                )
            labels = self.source.ensure_labels(list(TERMINAL_LABELS.values()))
            message_ids = self.source.inbox_message_ids(
                page_size=page_size or self.settings.context_mailbox_page_size
            )
            counts = {
                "seen": len(message_ids),
                "staged": 0,
                "duplicate": 0,
                "quarantined": 0,
                "failed": 0,
                "skipped": 0,
            }
            details: list[dict[str, Any]] = []
            terminal_ids = {labels[name] for name in TERMINAL_LABELS.values()}
            for message_id in message_ids:
                message = self.source.message(message_id)
                if terminal_ids.intersection(set(message.get("label_ids") or [])):
                    counts["skipped"] += 1
                    continue
                result = self._process_message(message, labels=labels)
                counts[result["count_key"]] += 1
                details.append(result)
            self._record_status("healthy", counts=counts, error=None)
            return {
                "status": "healthy",
                "mailbox": actual_email,
                "counts": counts,
                "messages": details,
            }
        except (ToolExecutionError, ContextMailboxError) as exc:
            self.session.rollback()
            self._record_status("error", counts={}, error=str(exc))
            raise ContextMailboxError(str(exc)) from exc

    def _process_message(
        self,
        message: dict[str, Any],
        *,
        labels: dict[str, str],
    ) -> dict[str, Any]:
        message_id = str(message.get("message_id") or "")
        sender = parseaddr(str(message.get("from") or ""))[1].lower()
        if sender not in self.settings.context_mailbox_allowed_senders:
            reason = f"Sender is not allowlisted: {sender or 'unknown'}"
            self._finish_message(message_id, labels, state="quarantine")
            return {
                "message_id": message_id,
                "status": "quarantined",
                "count_key": "quarantined",
                "reason": reason,
            }
        try:
            handoff = parse_context_handoff(message)
            domain = DomainRepository(self.session).get_by_key(handoff.domain_key)
            if domain is None:
                raise ContextHandoffError(f"Unknown Maestro domain: {handoff.domain_key}")
            content, attachments, raw_path = self._context_content(message, handoff)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            source_policy = _mailbox_policy(handoff)
            structured_route = self._route_structured_handoff(
                handoff,
                domain=domain,
                message=message,
                content_hash=content_hash,
            )
            result = self.gateway.ingest(
                GatewayItem(
                    source_registration_key=f"context-mailbox:{handoff.source_system}:{domain.key}",
                    source_system=handoff.source_system,
                    external_id=handoff.source_id,
                    source_version=content_hash,
                    content_type="context_mailbox_handoff",
                    domain_key=domain.key,
                    title=handoff.title,
                    content=content,
                    source_timestamp=handoff.source_timestamp,
                    policy=source_policy,
                    metadata={
                        "adapter_type": "context_mailbox",
                        "gmail_message_id": message_id,
                        "gmail_thread_id": message.get("thread_id"),
                        "gmail_sender": sender,
                        "gmail_subject": message.get("subject"),
                        "gmail_date": message.get("date"),
                        "raw_archive_path": str(raw_path),
                        "attachments": attachments,
                        "manifest": handoff.metadata,
                        "structured_route_promoted": structured_route is not None,
                        "structured_route": structured_route,
                        "source_config": {"mailbox": self.settings.maestro_intake_email},
                    },
                ),
                domain=domain,
            )
            self._finish_message(message_id, labels, state="processed")
            return {
                "message_id": message_id,
                "status": result.status,
                "count_key": "duplicate" if result.status == "duplicate" else "staged",
                "domain_key": domain.key,
                "source_id": handoff.source_id,
                "ingestion_record_id": result.ingestion_record_id,
                "structured_route": structured_route,
            }
        except ContextHandoffError as exc:
            self.session.rollback()
            self._finish_message(message_id, labels, state="quarantine")
            return {
                "message_id": message_id,
                "status": "quarantined",
                "count_key": "quarantined",
                "reason": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            self.session.rollback()
            self._finish_message(message_id, labels, state="failed")
            return {
                "message_id": message_id,
                "status": "failed",
                "count_key": "failed",
                "reason": str(exc),
            }

    def _route_structured_handoff(
        self,
        handoff: ParsedContextHandoff,
        *,
        domain: Domain,
        message: dict[str, Any],
        content_hash: str,
    ) -> dict[str, Any] | None:
        if handoff.record_type != "calendar_event":
            return None
        metadata = handoff.metadata
        all_day = str(metadata.get("all_day") or "false").strip().lower() in {
            "true",
            "yes",
            "1",
        }
        source_timezone = _handoff_source_timezone(
            metadata,
            source_system=handoff.source_system,
            fallback_timezone=self.settings.home_timezone,
            all_day=all_day,
        )
        start_at = _handoff_datetime(metadata.get("start"), source_timezone)
        end_at = _handoff_datetime(metadata.get("end"), source_timezone)
        if start_at is None:
            raise ContextHandoffError("Calendar handoff is missing a valid start time.")
        action = _slug_token(metadata.get("action") or "added")
        organizer_email = str(metadata.get("organizer") or "").strip().lower() or None
        attendees = _handoff_attendees(
            metadata,
            organizer_email=organizer_email,
            owner_emails={
                *self.settings.user_emails,
                parseaddr(str(message.get("from") or ""))[1].strip().lower(),
            },
        )
        source_ref = {
            "type": "context_mailbox_calendar_event",
            "source_system": handoff.source_system,
            "source_id": handoff.source_id,
            "source_timestamp": handoff.source_timestamp.isoformat(),
            "gmail_message_id": message.get("message_id"),
            "gmail_thread_id": message.get("thread_id"),
            "ical_uid": metadata.get("ical_uid"),
            "content_hash": content_hash,
        }
        item = RoutedItem(
            domain_id=domain.id,
            route_type="event",
            title=str(metadata.get("title") or handoff.title or "Untitled calendar event")[:240],
            content=handoff.content,
            priority="normal",
            status="cancelled" if action in {"cancelled", "canceled", "deleted", "removed"} else "open",
            source_refs=[source_ref],
            metadata_={
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat() if end_at else None,
                "timezone": self.settings.home_timezone,
                "source_timezone": source_timezone,
                "all_day": all_day,
                "location": str(metadata.get("location") or "").strip() or None,
                "organizer_email": organizer_email,
                "attendees": attendees,
                "recurrence_rule": str(metadata.get("recurrence_rule") or "").strip() or None,
                "series_master_id": str(metadata.get("series_master_id") or "").strip() or None,
                "external_provider": handoff.source_system,
                "external_calendar_id": handoff.domain_key,
                "external_event_id": handoff.source_id,
                "external_etag": content_hash,
                "sync_status": "synced",
                "ical_uid": metadata.get("ical_uid"),
                "source_adapter": "context_mailbox",
                "domain_key": handoff.domain_key,
                "enriched_at": datetime.now(UTC).isoformat(),
                "enrichment_source": "structured_context_mailbox_adapter",
            },
        )
        self.session.add(item)
        self.session.flush()
        promotions = RoutedMemoryService(
            self.session,
            enable_llm_resolver=False,
        ).promote_items([item])
        if not promotions:
            raise ContextHandoffError("Calendar handoff could not be promoted.")
        promotion = promotions[0]
        return {
            "route_type": "event",
            "action": promotion.action,
            "object_id": str(promotion.object_id),
            "routed_item_id": str(item.id),
        }

    def _context_content(
        self,
        message: dict[str, Any],
        handoff: ParsedContextHandoff,
    ) -> tuple[str, list[dict[str, Any]], Path]:
        message_id = str(message.get("message_id") or "unknown")
        raw_path = (
            Path(self.settings.memory_dropbox_root)
            / handoff.domain_key
            / "mailbox_raw"
            / message_id
        )
        raw_path.mkdir(parents=True, exist_ok=True)
        (raw_path / "message.md").write_text(handoff.content, encoding="utf-8")
        attachment_metadata: list[dict[str, Any]] = []
        sections = [handoff.content]
        for attachment in message.get("attachments") or []:
            attachment_id = str(attachment.get("attachment_id") or "")
            filename = _safe_filename(str(attachment.get("filename") or "attachment"))
            if not attachment_id:
                continue
            data = self.source.attachment(message_id, attachment_id)
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise ContextHandoffError(f"Attachment exceeds 20 MB limit: {filename}")
            destination = raw_path / filename
            destination.write_bytes(data)
            suffix = destination.suffix.lower()
            metadata = {
                "filename": filename,
                "mime_type": attachment.get("mime_type"),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "path": str(destination),
            }
            attachment_metadata.append(metadata)
            if suffix in SUPPORTED_DROPBOX_SUFFIXES:
                extracted, extraction_metadata = extract_dropbox_text(destination)
                metadata["extraction"] = extraction_metadata
                if extracted.strip():
                    sections.extend([f"## Attachment: {filename}", extracted.strip()])
        (raw_path / "manifest.json").write_text(
            json.dumps(
                {
                    "message_id": message_id,
                    "thread_id": message.get("thread_id"),
                    "subject": message.get("subject"),
                    "from": message.get("from"),
                    "date": message.get("date"),
                    "handoff": handoff.metadata,
                    "attachments": attachment_metadata,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        content = "\n\n".join(section for section in sections if section.strip())
        return content, attachment_metadata, raw_path

    def _finish_message(self, message_id: str, labels: dict[str, str], *, state: str) -> None:
        remove = ["UNREAD"]
        if state == "processed":
            remove.append("INBOX")
        self.source.label_message(
            message_id,
            add_label_ids=[labels[TERMINAL_LABELS[state]]],
            remove_label_ids=remove,
        )

    def _record_status(self, status: str, *, counts: dict[str, int], error: str | None) -> None:
        now = datetime.now(UTC).isoformat()
        setting = self.session.get(RuntimeSetting, CONTEXT_MAILBOX_SETTING_KEY)
        if setting is None:
            setting = RuntimeSetting(key=CONTEXT_MAILBOX_SETTING_KEY, value={})
            self.session.add(setting)
        prior = dict(setting.value or {})
        setting.value = {
            **prior,
            "status": status,
            "last_polled_at": now,
            "last_success_at": now if status == "healthy" else prior.get("last_success_at"),
            "last_error": error,
            "last_counts": counts,
        }
        self.session.commit()


def parse_context_handoff(message: dict[str, Any]) -> ParsedContextHandoff:
    subject = str(message.get("subject") or "")
    context_match = CONTEXT_SUBJECT_PATTERN.match(subject)
    ingest_match = INGEST_SUBJECT_PATTERN.match(subject)
    if not context_match and not ingest_match:
        raise ContextHandoffError(
            "Subject must use [MAESTRO-CONTEXT][SOURCE][DOMAIN] or "
            "[MAESTRO-INGEST][DOMAIN][RECORD_TYPE]."
        )
    body = str(message.get("body_text") or "").strip()
    if not body:
        raise ContextHandoffError("Context handoff email has no readable body.")
    metadata = _manifest_fields(body)
    if context_match:
        subject_source = _slug_token(context_match.group(1))
        subject_domain = _domain_key(context_match.group(2), allow_placeholder=True)
        source_system = _slug_token(metadata.get("source_system") or subject_source)
        if source_system != subject_source:
            raise ContextHandoffError("Subject source and body source_system do not match.")
        record_type = _normalized_record_type(metadata.get("record_type"))
        title_fallback = context_match.group(3)
    else:
        assert ingest_match is not None
        subject_domain = _domain_key(ingest_match.group(1), allow_placeholder=True)
        subject_record_type = _normalized_record_type(ingest_match.group(2))
        record_type = _normalized_record_type(metadata.get("record_type"))
        if not record_type:
            record_type = subject_record_type
        if record_type != subject_record_type:
            raise ContextHandoffError("Subject and body record_type do not match.")
        source_system = _slug_token(metadata.get("source_system") or "")
        if not source_system:
            raise ContextHandoffError("Ingest handoff is missing source_system.")
        title_fallback = ingest_match.group(3)
    domain_key = _domain_key(metadata.get("domain") or subject_domain)
    if subject_domain and subject_domain != "domain" and subject_domain != domain_key:
        raise ContextHandoffError("Subject domain and body domain do not match.")
    source_id = str(metadata.get("source_id") or "").strip()
    if not source_id:
        raise ContextHandoffError("Context handoff is missing source_id.")
    source_timestamp = _source_timestamp(
        metadata.get("source_timestamp"),
        fallback_date=str(message.get("date") or ""),
        internal_date=str(message.get("internal_date") or ""),
    )
    if source_system == "usma_sanitized_context_drop":
        _validate_sanitized_manifest(metadata)
    title = _handoff_title(body, source_system=source_system, fallback=title_fallback)
    return ParsedContextHandoff(
        source_system=source_system,
        source_id=source_id,
        source_timestamp=source_timestamp,
        domain_key=domain_key,
        title=title,
        content=body,
        metadata=metadata,
        record_type=record_type,
    )


def _manifest_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in body.splitlines()[:60]:
        cleaned = line.strip().strip("`").rstrip("\\").strip()
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", cleaned)
        if match:
            fields[match.group(1).lower().replace("-", "_")] = match.group(2).strip()
        elif cleaned.startswith("#") or (fields and cleaned.startswith("- ")):
            break
    return fields


def _domain_key(value: str | None, *, allow_placeholder: bool = False) -> str:
    normalized = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if allow_placeholder and normalized == "domain":
        return "domain"
    key = DOMAIN_ALIASES.get(normalized)
    if not key:
        raise ContextHandoffError(f"Unknown or missing domain: {value or 'none'}")
    return key


def _slug_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _normalized_record_type(value: str | None) -> str | None:
    token = _slug_token(str(value or ""))
    if not token:
        return None
    return {
        "calendar": "calendar_event",
        "event": "calendar_event",
    }.get(token, token)


def _source_timestamp(value: str | None, *, fallback_date: str, internal_date: str) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            pass
    if fallback_date:
        try:
            parsed = parsedate_to_datetime(fallback_date)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except (TypeError, ValueError):
            pass
    if internal_date.isdigit():
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
    return datetime.now(UTC)


def _handoff_datetime(value: str | None, timezone: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ContextHandoffError(f"Invalid calendar timestamp: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed


def _handoff_source_timezone(
    metadata: dict[str, str],
    *,
    source_system: str,
    fallback_timezone: str,
    all_day: bool,
) -> str:
    explicit = next(
        (
            str(metadata.get(key) or "").strip()
            for key in ("timezone", "time_zone", "start_timezone")
            if str(metadata.get(key) or "").strip()
        ),
        None,
    )
    if explicit:
        timezone = WINDOWS_TIMEZONE_ALIASES.get(explicit.lower(), explicit)
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ContextHandoffError(f"Unknown calendar timezone: {explicit}") from exc
        return timezone
    if all_day:
        return fallback_timezone
    if _slug_token(source_system) in OUTLOOK_SOURCE_SYSTEMS:
        return "UTC"
    return fallback_timezone


def _handoff_attendees(
    metadata: dict[str, str],
    *,
    organizer_email: str | None,
    owner_emails: set[str],
) -> list[dict[str, Any]]:
    attendees: list[dict[str, Any]] = []
    seen: set[str] = set()
    owner_emails = {email for email in owner_emails if email}
    attendee_groups = (
        (metadata.get("required_attendees"), "required"),
        (metadata.get("optional_attendees"), "optional"),
    )
    candidates: list[tuple[str, str]] = []
    if organizer_email:
        candidates.append((organizer_email, "required"))
    for raw, attendee_type in attendee_groups:
        candidates.extend(
            (email.strip().lower(), attendee_type)
            for email in re.split(r"[;,]", str(raw or ""))
            if email.strip()
        )
    for email, attendee_type in candidates:
        if email in seen:
            continue
        seen.add(email)
        attendees.append(
            {
                "name": email,
                "email": email,
                "attendee_type": attendee_type,
                "response_status": "needs_action",
                "is_organizer": email == organizer_email,
                "is_user": email in owner_emails
                or is_maestro_user_reference(email=email),
            }
        )
    return attendees


def _handoff_title(body: str, *, source_system: str, fallback: str | None) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:240]
    suffix = str(fallback or "").strip()
    return (suffix or f"{source_system.replace('_', ' ').title()} context handoff")[:240]


def _validate_sanitized_manifest(metadata: dict[str, str]) -> None:
    if metadata.get("review_status", "").lower() not in {"reviewed", "approved"}:
        raise ContextHandoffError("Sanitized work context must be marked reviewed or approved.")
    if not metadata.get("reviewed_by") or not metadata.get("reviewed_at"):
        raise ContextHandoffError("Sanitized work context requires reviewed_by and reviewed_at.")
    restricted_flags = (
        "contains_restricted",
        "contains_cui",
        "contains_classified",
        "contains_proprietary_technical_data",
    )
    if any(metadata.get(key, "false").lower() not in {"false", "no"} for key in restricted_flags):
        raise ContextHandoffError("Context marked as restricted cannot enter Maestro.")


def _mailbox_policy(handoff: ParsedContextHandoff) -> SourcePolicy:
    base = policy_for_domain(handoff.domain_key)
    trust_level = (
        "assistant_generated" if handoff.source_system == "chatgpt" else base.trust_level
    )
    return SourcePolicy(
        sensitivity=base.sensitivity,
        trust_level=trust_level,
        transfer_method="context_mailbox",
        egress_policy=base.egress_policy,
        retention=base.retention,
        requires_human_review=base.requires_human_review,
    )


def _safe_filename(value: str) -> str:
    name = Path(value).name
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "attachment"
