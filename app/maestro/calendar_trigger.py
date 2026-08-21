"""Incremental Google Calendar producer for durable domain monitor workflows."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, Protocol
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import CalendarEvent, Domain, RuntimeSetting, ToolConnection, WorkflowDefinition
from app.maestro.scheduler import SchedulerService
from app.tools.runtime import ToolExecutionError, _gmail_access_token, _google_api_json

CALENDAR_TRIGGER_EVENT_TYPE = "google.calendar.event.changed"
CALENDAR_TRIGGER_SETTING_KEY = "calendar_trigger_worker"
CALENDAR_TRIGGER_CURSOR_PREFIX = "calendar_trigger_cursor:"


class CalendarTriggerError(RuntimeError):
    pass


class CalendarSyncTokenExpired(CalendarTriggerError):
    pass


class CalendarChangeSource(Protocol):
    def changes_page(
        self,
        connection: ToolConnection,
        *,
        sync_token: str | None,
        page_token: str | None,
        page_size: int,
        bootstrap_at: datetime | None = None,
    ) -> dict[str, Any]: ...


class GoogleCalendarChangeSource:
    def __init__(self) -> None:
        self._access_tokens: dict[str, str] = {}

    def _token(self, connection: ToolConnection) -> str:
        key = str(connection.id)
        if key not in self._access_tokens:
            self._access_tokens[key] = _gmail_access_token(connection)
        return self._access_tokens[key]

    def changes_page(
        self,
        connection: ToolConnection,
        *,
        sync_token: str | None,
        page_token: str | None,
        page_size: int,
        bootstrap_at: datetime | None = None,
    ) -> dict[str, Any]:
        config = connection.config or {}
        calendar_id = str(config.get("calendar_id") or "primary")
        params: dict[str, Any] = {
            "maxResults": page_size,
            "showDeleted": "true",
            "singleEvents": "false",
        }
        if sync_token:
            params["syncToken"] = sync_token
        elif bootstrap_at:
            params["updatedMin"] = bootstrap_at.isoformat().replace("+00:00", "Z")
        if page_token:
            params["pageToken"] = page_token
        try:
            return _google_api_json(
                "GET",
                "https://www.googleapis.com",
                f"/calendar/v3/calendars/{quote(calendar_id, safe='')}/events",
                token=self._token(connection),
                params=params,
            )
        except ToolExecutionError as exc:
            if "410" in str(exc):
                raise CalendarSyncTokenExpired("Google Calendar sync token expired.") from exc
            raise CalendarTriggerError(str(exc)) from exc


def calendar_trigger_worker_settings(session: Session) -> dict[str, Any]:
    settings = get_settings()
    defaults = {
        "enabled": settings.calendar_trigger_autorun,
        "interval_seconds": settings.calendar_trigger_interval_seconds,
        "page_size": settings.calendar_trigger_page_size,
        "source": "env",
    }
    stored = session.get(RuntimeSetting, CALENDAR_TRIGGER_SETTING_KEY)
    if stored is None:
        return defaults
    payload = stored.value or {}
    return {
        **defaults,
        **{key: payload[key] for key in ("enabled", "interval_seconds", "page_size") if key in payload},
        "source": "runtime",
    }


def update_calendar_trigger_worker_settings(
    session: Session,
    *,
    enabled: bool | None = None,
    interval_seconds: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    current = calendar_trigger_worker_settings(session)
    if enabled is not None:
        current["enabled"] = enabled
    if interval_seconds is not None:
        current["interval_seconds"] = interval_seconds
    if page_size is not None:
        current["page_size"] = page_size
    stored = session.get(RuntimeSetting, CALENDAR_TRIGGER_SETTING_KEY)
    if stored is None:
        stored = RuntimeSetting(key=CALENDAR_TRIGGER_SETTING_KEY, value={})
        session.add(stored)
    stored.value = {
        "enabled": bool(current["enabled"]),
        "interval_seconds": int(current["interval_seconds"]),
        "page_size": int(current["page_size"]),
    }
    session.commit()
    return calendar_trigger_worker_settings(session)


def sync_calendar_trigger_worker_settings(session: Session) -> dict[str, Any]:
    definitions = session.scalars(
        select(WorkflowDefinition).where(
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.trigger_type == "event",
        )
    ).all()
    enabled = any(
        (definition.trigger_config or {}).get("event_type") == CALENDAR_TRIGGER_EVENT_TYPE
        and (definition.trigger_config or {}).get("calendar_watch_enabled", True) is not False
        for definition in definitions
    )
    return update_calendar_trigger_worker_settings(session, enabled=enabled)


class CalendarTriggerService:
    def __init__(self, session: Session, *, source: CalendarChangeSource | None = None):
        self.session = session
        self.source = source or GoogleCalendarChangeSource()
        self.scheduler = SchedulerService(session)

    def status(self) -> dict[str, Any]:
        domains = self._watched_domains()
        return {
            "worker": calendar_trigger_worker_settings(self.session),
            "event_type": CALENDAR_TRIGGER_EVENT_TYPE,
            "domains": [self._cursor_payload(domain) for domain in domains],
        }

    def poll_once(self, *, page_size: int | None = None) -> dict[str, Any]:
        configured = calendar_trigger_worker_settings(self.session)
        effective_page_size = max(1, min(2500, int(page_size or configured["page_size"])))
        results: list[dict[str, Any]] = []
        for domain in self._watched_domains():
            try:
                results.append(self._poll_domain(domain, page_size=effective_page_size))
            except CalendarSyncTokenExpired as exc:
                self.session.rollback()
                results.append(self._bootstrap_domain(domain, status="token_reset", reason=str(exc)))
            except Exception as exc:
                self.session.rollback()
                results.append(self._record_error(domain, str(exc)))
        return {
            "event_type": CALENDAR_TRIGGER_EVENT_TYPE,
            "domain_count": len(results),
            "emitted_count": sum(int(item.get("emitted_count") or 0) for item in results),
            "domains": results,
        }

    def reset_domain(self, domain_key: str) -> dict[str, Any]:
        domain = self.session.scalar(select(Domain).where(Domain.key == domain_key))
        if domain is None:
            raise CalendarTriggerError(f"Unknown domain: {domain_key}")
        return self._bootstrap_domain(domain, status="reset", reason="Calendar cursor manually reset.")

    def _poll_domain(self, domain: Domain, *, page_size: int) -> dict[str, Any]:
        cursor = self._cursor_setting(domain)
        payload = dict(cursor.value or {}) if cursor else {}
        sync_token = str(payload.get("sync_token") or "").strip()
        if not sync_token:
            return self._bootstrap_domain(domain, status="initialized")
        connection = self._connection_for(domain)
        page_token: str | None = None
        next_sync_token = sync_token
        events: list[dict[str, Any]] = []
        page_count = 0
        while True:
            response = self.source.changes_page(
                connection,
                sync_token=sync_token,
                page_token=page_token,
                page_size=page_size,
            )
            page_count += 1
            events.extend(item for item in response.get("items") or [] if isinstance(item, dict))
            page_token = str(response.get("nextPageToken") or "").strip() or None
            next_sync_token = str(response.get("nextSyncToken") or next_sync_token)
            if not page_token:
                break
            if page_count >= 100:
                raise CalendarTriggerError("Calendar polling exceeded 100 pages in one cycle.")

        emitted: list[dict[str, Any]] = []
        skipped_past = 0
        calendar_id = str((connection.config or {}).get("calendar_id") or "primary")
        for event in events:
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                continue
            if self._is_past_event(domain, calendar_id, event_id, event):
                skipped_past += 1
                continue
            version = str(event.get("etag") or event.get("updated") or "unknown").strip()
            event_payload = {
                "id": event_id,
                "provider": "google_calendar",
                "domain_key": domain.key,
                "calendar_id": calendar_id,
                "event_id": event_id,
                "event_version": version,
                "status": event.get("status"),
                "google_event": event,
                "detected_at": datetime.now(UTC).isoformat(),
            }
            delivery_id = f"{domain.key}:{calendar_id}:{event_id}:{version}"
            runs = self.scheduler.enqueue_event_workflows(
                event_type=CALENDAR_TRIGGER_EVENT_TYPE,
                event_payload=event_payload,
                event_id=delivery_id,
            )
            emitted.append({
                "event_id": event_id,
                "delivery_id": delivery_id,
                "workflow_run_ids": [str(run.id) for run in runs],
            })

        now = datetime.now(UTC).isoformat()
        self._write_cursor(domain, {
            **payload,
            "domain_key": domain.key,
            "connection_id": str(connection.id),
            "calendar_id": calendar_id,
            "sync_token": next_sync_token,
            "status": "healthy",
            "last_polled_at": now,
            "last_emitted_at": now if emitted else payload.get("last_emitted_at"),
            "last_event_id": emitted[-1]["event_id"] if emitted else payload.get("last_event_id"),
            "last_emitted_count": len(emitted),
            "last_skipped_past_count": skipped_past,
            "last_error": None,
            "error_count": 0,
        })
        return {
            "domain_key": domain.key,
            "status": "healthy",
            "seen_count": len(events),
            "emitted_count": len(emitted),
            "skipped_past_count": skipped_past,
            "page_count": page_count,
            "emitted": emitted,
        }

    def _is_past_event(
        self,
        domain: Domain,
        calendar_id: str,
        event_id: str,
        event: dict[str, Any],
    ) -> bool:
        end_at = _google_event_datetime(event.get("end")) or _google_event_datetime(event.get("start"))
        if end_at is None:
            existing = self.session.scalar(
                select(CalendarEvent).where(
                    CalendarEvent.domain_id == domain.id,
                    CalendarEvent.external_provider == "google_calendar",
                    CalendarEvent.external_calendar_id == calendar_id,
                    CalendarEvent.external_event_id == event_id,
                )
            )
            end_at = existing.end_at or existing.start_at if existing else None
        if end_at is None:
            return False
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=UTC)
        return end_at.astimezone(UTC) < datetime.now(UTC)

    def _bootstrap_domain(self, domain: Domain, *, status: str, reason: str | None = None) -> dict[str, Any]:
        connection = self._connection_for(domain)
        page_token: str | None = None
        next_sync_token = ""
        bootstrap_at = datetime.now(UTC)
        page_count = 0
        while True:
            response = self.source.changes_page(
                connection,
                sync_token=None,
                page_token=page_token,
                page_size=250,
                bootstrap_at=bootstrap_at,
            )
            page_count += 1
            page_token = str(response.get("nextPageToken") or "").strip() or None
            next_sync_token = str(response.get("nextSyncToken") or next_sync_token).strip()
            if not page_token:
                break
        if not next_sync_token:
            raise CalendarTriggerError("Google Calendar did not return an incremental sync token.")
        now = datetime.now(UTC).isoformat()
        prior = self._cursor_setting(domain)
        prior_payload = dict(prior.value or {}) if prior else {}
        self._write_cursor(domain, {
            **prior_payload,
            "domain_key": domain.key,
            "connection_id": str(connection.id),
            "calendar_id": str((connection.config or {}).get("calendar_id") or "primary"),
            "sync_token": next_sync_token,
            "status": status,
            "initialized_at": prior_payload.get("initialized_at") or now,
            "last_polled_at": now,
            "last_error": reason if status == "token_reset" else None,
            "error_count": 0,
        })
        return {"domain_key": domain.key, "status": status, "emitted_count": 0, "bootstrap": True}

    def _record_error(self, domain: Domain, message: str) -> dict[str, Any]:
        prior = self._cursor_setting(domain)
        payload = dict(prior.value or {}) if prior else {}
        error_count = int(payload.get("error_count") or 0) + 1
        self._write_cursor(domain, {
            **payload,
            "domain_key": domain.key,
            "status": "error",
            "last_polled_at": datetime.now(UTC).isoformat(),
            "last_error": message,
            "error_count": error_count,
        })
        return {"domain_key": domain.key, "status": "error", "emitted_count": 0, "error": message, "error_count": error_count}

    def _watched_domains(self) -> list[Domain]:
        definitions = self.session.scalars(
            select(WorkflowDefinition).where(
                WorkflowDefinition.is_active.is_(True),
                WorkflowDefinition.trigger_type == "event",
            )
        ).all()
        domain_ids = {
            definition.domain_id
            for definition in definitions
            if (definition.trigger_config or {}).get("event_type") == CALENDAR_TRIGGER_EVENT_TYPE
            and (definition.trigger_config or {}).get("calendar_watch_enabled", True) is not False
            and definition.domain_id
        }
        return self.session.scalars(
            select(Domain).where(Domain.id.in_(domain_ids), Domain.is_active.is_(True)).order_by(Domain.key)
        ).all() if domain_ids else []

    def _connection_for(self, domain: Domain) -> ToolConnection:
        connection = self.session.scalar(
            select(ToolConnection).where(
                ToolConnection.domain_id == domain.id,
                ToolConnection.tool_key == "google",
                ToolConnection.is_active.is_(True),
            )
        )
        if connection is None:
            raise CalendarTriggerError(f"Domain {domain.key} has no active Google Workspace connection.")
        return connection

    def _cursor_setting(self, domain: Domain) -> RuntimeSetting | None:
        return self.session.get(RuntimeSetting, f"{CALENDAR_TRIGGER_CURSOR_PREFIX}{domain.key}")

    def _cursor_payload(self, domain: Domain) -> dict[str, Any]:
        setting = self._cursor_setting(domain)
        payload = dict(setting.value or {}) if setting else {"status": "not_initialized"}
        sync_token_present = bool(payload.pop("sync_token", None))
        return {
            "domain_key": domain.key,
            **payload,
            "sync_token_present": sync_token_present,
        }

    def _write_cursor(self, domain: Domain, payload: dict[str, Any]) -> None:
        key = f"{CALENDAR_TRIGGER_CURSOR_PREFIX}{domain.key}"
        setting = self.session.get(RuntimeSetting, key)
        if setting is None:
            setting = RuntimeSetting(key=key, value={})
            self.session.add(setting)
        setting.value = payload
        self.session.commit()


def _google_event_datetime(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    raw = str(value.get("dateTime") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    raw_date = str(value.get("date") or "").strip()
    if raw_date:
        try:
            return datetime.combine(date.fromisoformat(raw_date), time.min, tzinfo=UTC)
        except ValueError:
            return None
    return None
