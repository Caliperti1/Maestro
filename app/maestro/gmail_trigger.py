"""Durable Gmail History producer for event-triggered Maestro workflows.

The producer is deliberately separate from the agent-facing Gmail tools. It watches only domains
with active ``gmail.message.received`` workflow definitions, persists a Gmail History cursor per
domain, and emits exact-message scheduler events. First activation bootstraps at the current Gmail
cursor so enabling the worker never processes an old inbox unexpectedly.
"""

from __future__ import annotations

import errno
import socket
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Domain,
    RuntimeSetting,
    ToolConnection,
    WorkflowDefinition,
    WorkflowNotification,
)
from app.maestro.channel import record_channel_message
from app.maestro.scheduler import SchedulerService
from app.tools.runtime import (
    ToolExecutionError,
    _gmail_access_token,
    _gmail_api_json,
    _gmail_message_payload,
    _gmail_user_id,
)

GMAIL_TRIGGER_EVENT_TYPE = "gmail.message.received"
GMAIL_TRIGGER_SETTING_KEY = "gmail_trigger_worker"
GMAIL_TRIGGER_CURSOR_PREFIX = "gmail_trigger_cursor:"
_INELIGIBLE_LABELS = {"DRAFT", "SENT", "SPAM", "TRASH"}


class GmailTriggerError(RuntimeError):
    """Raised when Gmail trigger polling cannot safely continue."""


class GmailHistoryCursorExpired(GmailTriggerError):
    """Raised when Gmail no longer retains the configured history cursor."""


class GmailMessageUnavailable(GmailTriggerError):
    """Raised when a History entry points to a message Gmail can no longer return."""


class GmailHistorySource(Protocol):
    def profile(self, connection: ToolConnection) -> dict[str, Any]: ...

    def history_page(
        self,
        connection: ToolConnection,
        *,
        start_history_id: str,
        page_token: str | None,
        page_size: int,
    ) -> dict[str, Any]: ...

    def message_metadata(
        self,
        connection: ToolConnection,
        *,
        message_id: str,
    ) -> dict[str, Any]: ...


class GoogleGmailHistorySource:
    """Thin Gmail API client using a domain's existing Google OAuth connection."""

    def __init__(self) -> None:
        self._access_tokens: dict[str, str] = {}

    def _token(self, connection: ToolConnection) -> str:
        key = str(connection.id)
        if key not in self._access_tokens:
            self._access_tokens[key] = _gmail_access_token(connection)
        return self._access_tokens[key]

    def profile(self, connection: ToolConnection) -> dict[str, Any]:
        token = self._token(connection)
        user_id = _gmail_user_id(connection, {})
        return _gmail_api_json(
            "GET",
            f"/gmail/v1/users/{quote(user_id, safe='')}/profile",
            token=token,
        )

    def history_page(
        self,
        connection: ToolConnection,
        *,
        start_history_id: str,
        page_token: str | None,
        page_size: int,
    ) -> dict[str, Any]:
        token = self._token(connection)
        user_id = _gmail_user_id(connection, {})
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "maxResults": page_size,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            return _gmail_api_json(
                "GET",
                f"/gmail/v1/users/{quote(user_id, safe='')}/history",
                token=token,
                params=params,
            )
        except ToolExecutionError as exc:
            if "404" in str(exc):
                raise GmailHistoryCursorExpired(
                    f"Gmail history cursor {start_history_id} is no longer available."
                ) from exc
            raise GmailTriggerError(str(exc)) from exc

    def message_metadata(
        self,
        connection: ToolConnection,
        *,
        message_id: str,
    ) -> dict[str, Any]:
        token = self._token(connection)
        user_id = _gmail_user_id(connection, {})
        try:
            message = _gmail_api_json(
                "GET",
                f"/gmail/v1/users/{quote(user_id, safe='')}/messages/{quote(message_id, safe='')}",
                token=token,
                params={
                    "format": "metadata",
                    "metadataHeaders": ["Subject", "From", "To", "Date"],
                },
            )
        except ToolExecutionError as exc:
            if "404" in str(exc):
                raise GmailMessageUnavailable(
                    f"Gmail message {message_id} is no longer available."
                ) from exc
            raise GmailTriggerError(str(exc)) from exc
        return _gmail_message_payload(message, max_body_chars=0)


def gmail_trigger_worker_settings(session: Session) -> dict[str, Any]:
    settings = get_settings()
    defaults = {
        "enabled": settings.gmail_trigger_autorun,
        "interval_seconds": settings.gmail_trigger_interval_seconds,
        "page_size": settings.gmail_trigger_page_size,
        "source": "env",
    }
    stored = session.get(RuntimeSetting, GMAIL_TRIGGER_SETTING_KEY)
    if stored is None:
        return defaults
    payload = stored.value or {}
    return {
        **defaults,
        **{
            key: payload[key]
            for key in ("enabled", "interval_seconds", "page_size")
            if key in payload
        },
        "source": "runtime",
    }


def update_gmail_trigger_worker_settings(
    session: Session,
    *,
    enabled: bool | None = None,
    interval_seconds: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    current = gmail_trigger_worker_settings(session)
    if enabled is not None:
        current["enabled"] = enabled
    if interval_seconds is not None:
        current["interval_seconds"] = interval_seconds
    if page_size is not None:
        current["page_size"] = page_size
    stored = session.get(RuntimeSetting, GMAIL_TRIGGER_SETTING_KEY)
    if stored is None:
        stored = RuntimeSetting(key=GMAIL_TRIGGER_SETTING_KEY, value={})
        session.add(stored)
    stored.value = {
        "enabled": bool(current["enabled"]),
        "interval_seconds": int(current["interval_seconds"]),
        "page_size": int(current["page_size"]),
    }
    session.commit()
    return gmail_trigger_worker_settings(session)


def sync_gmail_trigger_worker_settings(session: Session) -> dict[str, Any]:
    """Keep the shared poller on only while a workflow-specific Gmail watch is active."""
    definitions = session.scalars(
        select(WorkflowDefinition).where(
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.trigger_type == "event",
        )
    ).all()
    enabled = any(
        (definition.trigger_config or {}).get("event_type") == GMAIL_TRIGGER_EVENT_TYPE
        and (definition.trigger_config or {}).get("gmail_watch_enabled", True) is not False
        for definition in definitions
    )
    return update_gmail_trigger_worker_settings(session, enabled=enabled)


class GmailTriggerService:
    def __init__(
        self,
        session: Session,
        *,
        source: GmailHistorySource | None = None,
    ):
        self.session = session
        self.source = source or GoogleGmailHistorySource()
        self.scheduler = SchedulerService(session)

    def status(self) -> dict[str, Any]:
        domains = self._watched_domains()
        return {
            "worker": gmail_trigger_worker_settings(self.session),
            "event_type": GMAIL_TRIGGER_EVENT_TYPE,
            "domains": [self._cursor_payload(domain) for domain in domains],
        }

    def poll_once(self, *, page_size: int | None = None) -> dict[str, Any]:
        configured = gmail_trigger_worker_settings(self.session)
        effective_page_size = max(1, min(500, int(page_size or configured["page_size"])))
        results: list[dict[str, Any]] = []
        for domain in self._watched_domains():
            try:
                results.append(self._poll_domain(domain, page_size=effective_page_size))
            except GmailHistoryCursorExpired as exc:
                self.session.rollback()
                results.append(self._reset_domain(domain, reason=str(exc), status="cursor_reset"))
            except Exception as exc:
                self.session.rollback()
                results.append(self._record_error(domain, exc))
        return {
            "event_type": GMAIL_TRIGGER_EVENT_TYPE,
            "domain_count": len(results),
            "emitted_count": sum(int(item.get("emitted_count") or 0) for item in results),
            "domains": results,
        }

    def reset_domain(self, domain_key: str) -> dict[str, Any]:
        domain = self.session.scalar(select(Domain).where(Domain.key == domain_key))
        if domain is None:
            raise GmailTriggerError(f"Unknown domain: {domain_key}")
        return self._reset_domain(
            domain,
            reason="Gmail trigger cursor was manually reset.",
            status="reset",
        )

    def _poll_domain(self, domain: Domain, *, page_size: int) -> dict[str, Any]:
        connection = self._connection_for(domain)
        cursor = self._cursor_setting(domain)
        cursor_payload = dict(cursor.value or {}) if cursor else {}
        start_history_id = str(cursor_payload.get("history_id") or "").strip()
        if not start_history_id:
            return self._bootstrap_domain(domain, connection, status="initialized")

        history_id = start_history_id
        page_token: str | None = None
        page_count = 0
        message_ids: list[str] = []
        while True:
            response = self.source.history_page(
                connection,
                start_history_id=start_history_id,
                page_token=page_token,
                page_size=page_size,
            )
            page_count += 1
            history_id = str(response.get("historyId") or history_id)
            message_ids.extend(_history_message_ids(response))
            page_token = str(response.get("nextPageToken") or "").strip() or None
            if not page_token:
                break
            if page_count >= 100:
                raise GmailTriggerError("Gmail history polling exceeded 100 pages in one cycle.")

        emitted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for message_id in dict.fromkeys(message_ids):
            try:
                metadata = self.source.message_metadata(connection, message_id=message_id)
            except GmailMessageUnavailable as exc:
                missing.append({"message_id": message_id, "reason": str(exc)})
                continue
            labels = {str(label) for label in metadata.get("label_ids") or []}
            if not _is_eligible_inbox_message(labels):
                skipped.append({"message_id": message_id, "label_ids": sorted(labels)})
                continue
            event_payload = _gmail_event_payload(
                domain=domain,
                metadata=metadata,
                history_id=history_id,
            )
            event_id = f"{domain.key}:{message_id}"
            runs = self.scheduler.enqueue_event_workflows(
                event_type=GMAIL_TRIGGER_EVENT_TYPE,
                event_payload=event_payload,
                event_id=event_id,
            )
            emitted.append(
                {
                    "event_id": event_id,
                    "message_id": message_id,
                    "workflow_run_ids": [str(run.id) for run in runs],
                }
            )

        now_datetime = datetime.now(UTC)
        now = now_datetime.isoformat()
        prior_was_unhealthy = cursor_payload.get("status") in {"degraded", "error"}
        health_alert_was_active = bool(cursor_payload.get("health_alert_active"))
        outage_seconds = _outage_duration_seconds(
            cursor_payload.get("outage_started_at"),
            now_datetime,
        )
        self._write_cursor(
            domain,
            {
                **cursor_payload,
                "domain_key": domain.key,
                "connection_id": str(connection.id),
                "history_id": history_id,
                "status": "healthy",
                "last_polled_at": now,
                "last_error": None,
                "last_emitted_at": now if emitted else cursor_payload.get("last_emitted_at"),
                "last_message_id": emitted[-1]["message_id"] if emitted else cursor_payload.get("last_message_id"),
                "last_page_count": page_count,
                "last_seen_count": len(message_ids),
                "last_emitted_count": len(emitted),
                "last_skipped_count": len(skipped),
                "last_missing_count": len(missing),
                "error_count": 0,
                "failure_kind": None,
                "outage_started_at": None,
                "health_alert_active": False,
                "last_recovered_at": now if prior_was_unhealthy else cursor_payload.get("last_recovered_at"),
                "last_outage_seconds": outage_seconds if prior_was_unhealthy else cursor_payload.get("last_outage_seconds"),
            },
        )
        if health_alert_was_active:
            self._record_recovery(
                domain,
                recovered_at=now_datetime,
                outage_seconds=outage_seconds,
            )
        return {
            "domain_key": domain.key,
            "status": "healthy",
            "history_id": history_id,
            "page_count": page_count,
            "seen_count": len(message_ids),
            "emitted_count": len(emitted),
            "skipped_count": len(skipped),
            "missing_count": len(missing),
            "emitted": emitted,
            "skipped": skipped,
            "missing": missing,
        }

    def _reset_domain(self, domain: Domain, *, reason: str, status: str) -> dict[str, Any]:
        connection = self._connection_for(domain)
        result = self._bootstrap_domain(domain, connection, status=status, reason=reason)
        result["warning"] = reason
        return result

    def _bootstrap_domain(
        self,
        domain: Domain,
        connection: ToolConnection,
        *,
        status: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        profile = self.source.profile(connection)
        history_id = str(profile.get("historyId") or "").strip()
        if not history_id:
            raise GmailTriggerError("Gmail profile did not return a historyId cursor.")
        now = datetime.now(UTC).isoformat()
        prior = self._cursor_setting(domain)
        prior_payload = dict(prior.value or {}) if prior else {}
        self._write_cursor(
            domain,
            {
                **prior_payload,
                "domain_key": domain.key,
                "connection_id": str(connection.id),
                "account_email": profile.get("emailAddress"),
                "history_id": history_id,
                "status": status,
                "initialized_at": prior_payload.get("initialized_at") or now,
                "last_polled_at": now,
                "last_error": reason if status == "cursor_reset" else None,
                "last_reset_reason": reason or prior_payload.get("last_reset_reason"),
                "cursor_reset_at": now if status in {"reset", "cursor_reset"} else prior_payload.get("cursor_reset_at"),
                "error_count": 0,
            },
        )
        return {
            "domain_key": domain.key,
            "status": status,
            "history_id": history_id,
            "emitted_count": 0,
            "bootstrap": True,
        }

    def _record_error(self, domain: Domain, error: Exception) -> dict[str, Any]:
        prior = self._cursor_setting(domain)
        payload = dict(prior.value or {}) if prior else {}
        now_datetime = datetime.now(UTC)
        now = now_datetime.isoformat()
        message = str(error)
        transient_network = _is_transient_network_failure(error)
        error_count = int(payload.get("error_count") or 0) + 1
        prior_status = str(payload.get("status") or "")
        outage_started_at = (
            _parse_timestamp(payload.get("outage_started_at"))
            if prior_status in {"degraded", "error"}
            else None
        ) or now_datetime
        outage_seconds = max(0, int((now_datetime - outage_started_at).total_seconds()))
        health_alert_active = bool(payload.get("health_alert_active"))
        should_alert = not health_alert_active and (
            (
                transient_network
                and outage_seconds >= get_settings().gmail_trigger_network_alert_seconds
            )
            or (not transient_network and error_count >= 3)
        )
        updated = {
            **payload,
            "domain_key": domain.key,
            "status": "degraded" if transient_network else "error",
            "last_polled_at": now,
            "last_error": message,
            "error_count": error_count,
            "failure_kind": "transient_network" if transient_network else "poll_error",
            "outage_started_at": outage_started_at.isoformat(),
            "health_alert_active": health_alert_active or should_alert,
            "health_alerted_at": now if should_alert else payload.get("health_alerted_at"),
        }
        self._write_cursor(
            domain,
            updated,
        )
        if should_alert:
            self._record_health_alert(
                domain,
                message=message,
                error_count=error_count,
                transient_network=transient_network,
                outage_started_at=outage_started_at,
                outage_seconds=outage_seconds,
            )
        return {
            "domain_key": domain.key,
            "status": "degraded" if transient_network else "error",
            "emitted_count": 0,
            "error": message,
            "error_count": error_count,
            "failure_kind": "transient_network" if transient_network else "poll_error",
            "outage_seconds": outage_seconds,
            "alerted": should_alert,
        }

    def _record_health_alert(
        self,
        domain: Domain,
        *,
        message: str,
        error_count: int,
        transient_network: bool,
        outage_started_at: datetime,
        outage_seconds: int,
    ) -> None:
        if transient_network:
            duration_minutes = max(1, round(outage_seconds / 60))
            notification_message = (
                f"Gmail monitoring has been unable to reach Google for about "
                f"{duration_minutes} minutes, so new-email workflows are delayed. {message}"
            )
            channel_content = (
                f"I need your attention: {domain.name} Gmail monitoring has been unable to "
                f"reach Google for about {duration_minutes} minutes, so new-email workflows "
                f"are delayed. {message}"
            )
        else:
            failure_phrase = (
                "three times consecutively"
                if error_count == 3
                else f"{error_count} consecutive times"
            )
            notification_message = (
                f"Gmail trigger polling has failed {failure_phrase} and is "
                f"not detecting new messages. {message}"
            )
            channel_content = (
                f"I need your attention: {domain.name} Gmail monitoring has failed "
                f"{failure_phrase}, so new-email workflows may be delayed. "
                f"{message}"
            )
        notification = WorkflowNotification(
            domain_id=domain.id,
            severity="warning",
            status="delivered",
            title=f"{domain.name} Gmail monitoring needs attention",
            message=notification_message,
            notification_type="trigger_health",
            target="maestro_chat",
            delivered_at=datetime.now(UTC),
            metadata_={
                "domain_key": domain.key,
                "error_count": error_count,
                "failure_kind": "transient_network" if transient_network else "poll_error",
                "outage_started_at": outage_started_at.isoformat(),
                "outage_seconds": outage_seconds,
            },
        )
        self.session.add(notification)
        self.session.commit()
        record_channel_message(
            self.session,
            sender="maestro",
            content=channel_content,
            metadata={
                "source": "gmail_trigger_worker",
                "notification_id": str(notification.id),
                "domain_key": domain.key,
                "channel_visibility": "global",
            },
        )

    def _record_recovery(
        self,
        domain: Domain,
        *,
        recovered_at: datetime,
        outage_seconds: int,
    ) -> None:
        duration_minutes = max(1, round(outage_seconds / 60))
        message = (
            f"Gmail monitoring recovered after about {duration_minutes} minutes. Polling has "
            f"resumed and retained Gmail history has been processed."
        )
        notification = WorkflowNotification(
            domain_id=domain.id,
            severity="info",
            status="delivered",
            title=f"{domain.name} Gmail monitoring recovered",
            message=message,
            notification_type="trigger_health_recovery",
            target="maestro_chat",
            delivered_at=recovered_at,
            metadata_={
                "domain_key": domain.key,
                "outage_seconds": outage_seconds,
            },
        )
        self.session.add(notification)
        self.session.commit()
        record_channel_message(
            self.session,
            sender="maestro",
            content=f"Good news: {domain.name} {message}",
            metadata={
                "source": "gmail_trigger_worker",
                "notification_id": str(notification.id),
                "domain_key": domain.key,
                "channel_visibility": "global",
                "recovery": True,
            },
        )

    def _watched_domains(self) -> list[Domain]:
        definitions = self.session.scalars(
            select(WorkflowDefinition).where(
                WorkflowDefinition.is_active.is_(True),
                WorkflowDefinition.trigger_type == "event",
            )
        ).all()
        domain_ids: set[Any] = set()
        domain_keys: set[str] = set()
        for definition in definitions:
            config = definition.trigger_config or {}
            if config.get("event_type") != GMAIL_TRIGGER_EVENT_TYPE:
                continue
            if config.get("gmail_watch_enabled", True) is False:
                continue
            if definition.domain_id:
                domain_ids.add(definition.domain_id)
            filters = config.get("filters") if isinstance(config.get("filters"), dict) else {}
            if filters.get("domain_key"):
                domain_keys.add(str(filters["domain_key"]))
        query = select(Domain).where(Domain.is_active.is_(True)).order_by(Domain.key)
        domains = self.session.scalars(query).all()
        return [
            domain
            for domain in domains
            if domain.id in domain_ids or domain.key in domain_keys
        ]

    def _connection_for(self, domain: Domain) -> ToolConnection:
        connections = self.session.scalars(
            select(ToolConnection).where(
                ToolConnection.domain_id == domain.id,
                ToolConnection.tool_key.in_(["google", "gmail"]),
                ToolConnection.is_active.is_(True),
            )
        ).all()
        by_key = {connection.tool_key: connection for connection in connections}
        connection = by_key.get("google") or by_key.get("gmail")
        if connection is None:
            raise GmailTriggerError(
                f"Domain {domain.key} has no active Google Workspace or Gmail connection."
            )
        return connection

    def _cursor_setting(self, domain: Domain) -> RuntimeSetting | None:
        return self.session.get(RuntimeSetting, f"{GMAIL_TRIGGER_CURSOR_PREFIX}{domain.key}")

    def _cursor_payload(self, domain: Domain) -> dict[str, Any]:
        setting = self._cursor_setting(domain)
        return {
            "domain_key": domain.key,
            **(dict(setting.value or {}) if setting else {"status": "not_initialized"}),
        }

    def _write_cursor(self, domain: Domain, payload: dict[str, Any]) -> None:
        key = f"{GMAIL_TRIGGER_CURSOR_PREFIX}{domain.key}"
        setting = self.session.get(RuntimeSetting, key)
        if setting is None:
            setting = RuntimeSetting(key=key, value={})
            self.session.add(setting)
        setting.value = payload
        self.session.commit()


_TRANSIENT_NETWORK_ERRNOS = {
    code
    for code in (
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ECONNREFUSED", None),
    )
    if code is not None
}
_TRANSIENT_NETWORK_MARKERS = (
    "network is down",
    "network is unreachable",
    "no route to host",
    "nodename nor servname provided",
    "temporary failure in name resolution",
    "name or service not known",
    "handshake operation timed out",
    "timed out",
    "connection reset",
    "connection aborted",
    "connection refused",
)


def _is_transient_network_failure(error: BaseException) -> bool:
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, HTTPError) and (current.code == 429 or current.code >= 500):
            return True
        if isinstance(current, socket.gaierror | TimeoutError | ConnectionError):
            return True
        if isinstance(current, OSError) and current.errno in _TRANSIENT_NETWORK_ERRNOS:
            return True
        if any(marker in str(current).lower() for marker in _TRANSIENT_NETWORK_MARKERS):
            return True
        if isinstance(current, URLError) and isinstance(current.reason, BaseException):
            pending.append(current.reason)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _outage_duration_seconds(value: Any, now: datetime) -> int:
    started_at = _parse_timestamp(value)
    if started_at is None:
        return 0
    return max(0, int((now - started_at).total_seconds()))


def _history_message_ids(response: dict[str, Any]) -> list[str]:
    message_ids: list[str] = []
    for history in response.get("history") or []:
        if not isinstance(history, dict):
            continue
        for added in history.get("messagesAdded") or []:
            message = added.get("message") if isinstance(added, dict) else None
            message_id = str(message.get("id") or "").strip() if isinstance(message, dict) else ""
            if message_id:
                message_ids.append(message_id)
    return message_ids


def _is_eligible_inbox_message(labels: set[str]) -> bool:
    return "INBOX" in labels and not bool(labels & _INELIGIBLE_LABELS)


def _gmail_event_payload(
    *,
    domain: Domain,
    metadata: dict[str, Any],
    history_id: str,
) -> dict[str, Any]:
    message_id = str(metadata.get("message_id") or metadata.get("id") or "").strip()
    return {
        "id": message_id,
        "provider": "gmail",
        "domain_key": domain.key,
        "message_id": message_id,
        "thread_id": metadata.get("thread_id"),
        "history_id": history_id,
        "label_ids": metadata.get("label_ids") or [],
        "subject": metadata.get("subject"),
        "from": metadata.get("from"),
        "to": metadata.get("to"),
        "date": metadata.get("date"),
        "internal_date": metadata.get("internal_date"),
        "detected_at": datetime.now(UTC).isoformat(),
    }
