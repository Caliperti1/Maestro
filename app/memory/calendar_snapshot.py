"""Structured calendar snapshot ingestion for sanitized cross-boundary handoffs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.identity import is_maestro_user_reference
from app.db.models import CalendarEvent, Domain, RoutedItem
from app.memory.routed_service import RoutedMemoryService

MAX_SNAPSHOT_EVENTS = 2_500
WINDOWS_TIMEZONE_ALIASES = {
    "eastern standard time": "America/New_York",
    "utc": "UTC",
}


class CalendarSnapshotError(ValueError):
    """Raised when a structured calendar snapshot cannot be trusted or applied."""


@dataclass(frozen=True)
class CalendarSnapshot:
    snapshot_id: str
    source_system: str
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    events: tuple[dict[str, Any], ...]
    content_hash: str
    path: Path


def load_calendar_snapshot(
    attachments: list[dict[str, Any]],
    *,
    expected_source_system: str,
    expected_snapshot_id: str,
) -> CalendarSnapshot:
    candidates = [
        Path(str(item.get("path") or ""))
        for item in attachments
        if str(item.get("filename") or "").lower().endswith(".json")
    ]
    candidates = [path for path in candidates if path.is_file()]
    if len(candidates) != 1:
        raise CalendarSnapshotError(
            "Calendar snapshot handoff requires exactly one JSON attachment."
        )
    path = candidates[0]
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CalendarSnapshotError("Calendar snapshot attachment is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise CalendarSnapshotError("Calendar snapshot attachment must contain a JSON object.")
    if int(payload.get("schema_version") or 0) != 1:
        raise CalendarSnapshotError("Calendar snapshot schema_version must be 1.")
    source_system = str(payload.get("source_system") or "").strip().lower()
    if source_system != expected_source_system:
        raise CalendarSnapshotError("Calendar snapshot source_system does not match the email.")
    snapshot_id = str(payload.get("snapshot_id") or "").strip()
    if snapshot_id != expected_snapshot_id:
        raise CalendarSnapshotError("Calendar snapshot_id does not match the email source_id.")
    domain = str(payload.get("domain") or "").strip().lower().replace("_", " ")
    if domain not in {"usma", "west point"}:
        raise CalendarSnapshotError("Calendar snapshot domain must be USMA.")
    generated_at = _datetime(payload.get("generated_at"), "UTC", field="generated_at")
    window_start = _datetime(payload.get("window_start"), "UTC", field="window_start")
    window_end = _datetime(payload.get("window_end"), "UTC", field="window_end")
    if window_end <= window_start:
        raise CalendarSnapshotError("Calendar snapshot window_end must follow window_start.")
    events = payload.get("events")
    if not isinstance(events, list):
        raise CalendarSnapshotError("Calendar snapshot events must be a JSON array.")
    if len(events) > MAX_SNAPSHOT_EVENTS:
        raise CalendarSnapshotError(
            f"Calendar snapshot exceeds the {MAX_SNAPSHOT_EVENTS}-event limit."
        )
    normalized_events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise CalendarSnapshotError(f"Calendar snapshot event {index} is not an object.")
        source_id = str(event.get("source_id") or "").strip()
        if not source_id:
            raise CalendarSnapshotError(f"Calendar snapshot event {index} has no source_id.")
        if source_id in seen_ids:
            raise CalendarSnapshotError(f"Calendar snapshot repeats source_id {source_id}.")
        seen_ids.add(source_id)
        normalized_events.append({**event, "source_id": source_id})
    return CalendarSnapshot(
        snapshot_id=snapshot_id,
        source_system=source_system,
        generated_at=generated_at,
        window_start=window_start,
        window_end=window_end,
        events=tuple(normalized_events),
        content_hash=hashlib.sha256(raw).hexdigest(),
        path=path,
    )


class CalendarSnapshotService:
    def __init__(self, session: Session):
        self.session = session

    def apply(
        self,
        snapshot: CalendarSnapshot,
        *,
        domain: Domain,
        gmail_message_id: str,
    ) -> dict[str, Any]:
        items: list[RoutedItem] = []
        active_source_ids: set[str] = set()
        for event in snapshot.events:
            item = self._routed_item(
                snapshot,
                event=event,
                domain=domain,
                gmail_message_id=gmail_message_id,
            )
            items.append(item)
            self.session.add(item)
            if item.status != "cancelled":
                active_source_ids.add(str(event["source_id"]))
        self.session.flush()
        promotions = RoutedMemoryService(
            self.session,
            enable_llm_resolver=False,
        ).promote_items(items)
        seen_events = self.session.scalars(
            select(CalendarEvent).where(
                CalendarEvent.domain_id == domain.id,
                CalendarEvent.external_provider == snapshot.source_system,
                CalendarEvent.external_calendar_id == domain.key,
                CalendarEvent.external_event_id.in_(
                    [str(event["source_id"]) for event in snapshot.events]
                ),
            )
        ).all()
        for event in seen_events:
            event.metadata_ = {
                **(event.metadata_ or {}),
                "snapshot_missing_count": 0,
                "last_snapshot_id": snapshot.snapshot_id,
                "last_snapshot_at": snapshot.generated_at.isoformat(),
            }
        missing_count, cancelled_count = self._mark_missing(
            snapshot,
            domain=domain,
            active_source_ids=active_source_ids,
        )
        self.session.commit()
        actions: dict[str, int] = {}
        for promotion in promotions:
            actions[promotion.action] = actions.get(promotion.action, 0) + 1
        return {
            "route_type": "event_snapshot",
            "snapshot_id": snapshot.snapshot_id,
            "event_count": len(snapshot.events),
            "promoted_count": len(promotions),
            "actions": actions,
            "missing_count": missing_count,
            "cancelled_after_confirmation_count": cancelled_count,
            "window_start": snapshot.window_start.isoformat(),
            "window_end": snapshot.window_end.isoformat(),
        }

    def _routed_item(
        self,
        snapshot: CalendarSnapshot,
        *,
        event: dict[str, Any],
        domain: Domain,
        gmail_message_id: str,
    ) -> RoutedItem:
        action = str(event.get("action") or "upsert").strip().lower()
        status = (
            "cancelled"
            if action in {"cancelled", "canceled", "deleted", "removed"}
            else "open"
        )
        all_day = bool(event.get("all_day", False))
        source_timezone = _timezone(event.get("timezone"), all_day=all_day)
        start_at = _datetime(event.get("start"), source_timezone, field="start")
        end_at = _datetime(event.get("end"), source_timezone, field="end")
        if end_at <= start_at:
            raise CalendarSnapshotError(
                f"Calendar event {event['source_id']} ends before it starts."
            )
        organizer_name, organizer_email = _organizer(event.get("organizer"))
        attendees = _attendees(
            event.get("required_attendees"),
            event.get("optional_attendees"),
            organizer_name=organizer_name,
            organizer_email=organizer_email,
        )
        title = str(event.get("title") or "Untitled calendar event").strip()
        description = str(event.get("description") or event.get("body_preview") or title).strip()
        event_hash = hashlib.sha256(
            json.dumps(event, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        source_ref = {
            "type": "context_mailbox_calendar_snapshot",
            "source_system": snapshot.source_system,
            "source_id": event["source_id"],
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_generated_at": snapshot.generated_at.isoformat(),
            "gmail_message_id": gmail_message_id,
            "ical_uid": event.get("ical_uid"),
            "content_hash": event_hash,
        }
        return RoutedItem(
            domain_id=domain.id,
            route_type="event",
            title=title[:240],
            content=description,
            priority="normal",
            status=status,
            source_refs=[source_ref],
            metadata_={
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "timezone": get_settings().home_timezone,
                "source_timezone": source_timezone,
                "all_day": all_day,
                "location": _location(event.get("location")),
                "conferencing_url": str(
                    event.get("meeting_link") or event.get("web_link") or ""
                ).strip()
                or None,
                "organizer_name": organizer_name,
                "organizer_email": organizer_email,
                "attendees": attendees,
                "recurrence_rule": str(event.get("recurrence_rule") or "").strip() or None,
                "series_master_id": str(event.get("series_master_id") or "").strip() or None,
                "external_provider": snapshot.source_system,
                "external_calendar_id": domain.key,
                "external_event_id": event["source_id"],
                "external_etag": event_hash,
                "sync_status": "synced",
                "ical_uid": event.get("ical_uid"),
                "source_adapter": "context_mailbox_calendar_snapshot",
                "domain_key": domain.key,
                "snapshot_id": snapshot.snapshot_id,
                "enriched_at": datetime.now(UTC).isoformat(),
                "enrichment_source": "structured_calendar_snapshot_adapter",
            },
        )

    def _mark_missing(
        self,
        snapshot: CalendarSnapshot,
        *,
        domain: Domain,
        active_source_ids: set[str],
    ) -> tuple[int, int]:
        candidates = self.session.scalars(
            select(CalendarEvent).where(
                CalendarEvent.domain_id == domain.id,
                CalendarEvent.external_provider == snapshot.source_system,
                CalendarEvent.external_calendar_id == domain.key,
                CalendarEvent.start_at >= snapshot.window_start,
                CalendarEvent.start_at < snapshot.window_end,
                CalendarEvent.status.notin_(["archived", "cancelled"]),
            )
        ).all()
        missing = [
            event
            for event in candidates
            if str(event.external_event_id or "") not in active_source_ids
        ]
        cancelled = 0
        for event in missing:
            metadata = dict(event.metadata_ or {})
            previous_snapshot = str(metadata.get("last_missing_snapshot_id") or "")
            count = int(metadata.get("snapshot_missing_count") or 0)
            if previous_snapshot != snapshot.snapshot_id:
                count += 1
            metadata.update(
                {
                    "snapshot_missing_count": count,
                    "last_missing_snapshot_id": snapshot.snapshot_id,
                    "last_missing_snapshot_at": snapshot.generated_at.isoformat(),
                }
            )
            if count >= 2:
                event.status = "cancelled"
                metadata["snapshot_cancelled_at"] = datetime.now(UTC).isoformat()
                cancelled += 1
            event.metadata_ = metadata
        return len(missing), cancelled


def _timezone(value: Any, *, all_day: bool) -> str:
    raw = str(value or "").strip()
    if not raw:
        return get_settings().home_timezone if all_day else "UTC"
    timezone = WINDOWS_TIMEZONE_ALIASES.get(raw.lower(), raw)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise CalendarSnapshotError(f"Unknown calendar timezone: {raw}") from exc
    return timezone


def _datetime(value: Any, timezone: str, *, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise CalendarSnapshotError(f"Calendar snapshot is missing {field}.")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CalendarSnapshotError(f"Invalid calendar snapshot {field}: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed


def _organizer(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        email_address = value.get("emailAddress")
        if isinstance(email_address, dict):
            return (
                str(email_address.get("name") or "").strip() or None,
                str(email_address.get("address") or "").strip().lower() or None,
            )
        return (
            str(value.get("name") or "").strip() or None,
            str(value.get("email") or value.get("address") or "").strip().lower() or None,
        )
    email = str(value or "").strip().lower()
    return None, email or None


def _location(value: Any) -> str | None:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("name") or "").strip() or None
    return str(value or "").strip() or None


def _attendee_values(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, list):
        results: list[tuple[str, str]] = []
        for item in value:
            if isinstance(item, dict):
                address = item.get("emailAddress")
                if isinstance(address, dict):
                    name = str(address.get("name") or "").strip()
                    email = str(address.get("address") or "").strip().lower()
                else:
                    name = str(item.get("name") or "").strip()
                    email = str(item.get("email") or item.get("address") or "").strip().lower()
            else:
                name = ""
                email = str(item or "").strip().lower()
            if email:
                results.append((name or email, email))
        return results
    return [
        (email, email)
        for email in (
            token.strip().lower()
            for token in str(value or "").replace(",", ";").split(";")
        )
        if email
    ]


def _attendees(
    required: Any,
    optional: Any,
    *,
    organizer_name: str | None,
    organizer_email: str | None,
) -> list[dict[str, Any]]:
    candidates: list[tuple[str, str, str, bool]] = []
    if organizer_email:
        candidates.append(
            (organizer_name or organizer_email, organizer_email, "required", True)
        )
    candidates.extend((*item, "required", False) for item in _attendee_values(required))
    candidates.extend((*item, "optional", False) for item in _attendee_values(optional))
    attendees: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, email, attendee_type, is_organizer in candidates:
        if email in seen:
            continue
        seen.add(email)
        attendees.append(
            {
                "name": name,
                "email": email,
                "attendee_type": attendee_type,
                "response_status": "needs_action",
                "is_organizer": is_organizer,
                "is_user": is_maestro_user_reference(email=email),
            }
        )
    return attendees
