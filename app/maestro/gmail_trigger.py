"""Durable Gmail History producer for event-triggered Maestro workflows.

The producer is deliberately separate from the agent-facing Gmail tools. It watches only domains
with active ``gmail.message.received`` workflow definitions, persists a Gmail History cursor per
domain, and emits exact-message scheduler events. First activation bootstraps at the current Gmail
cursor so enabling the worker never processes an old inbox unexpectedly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
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
        message = _gmail_api_json(
            "GET",
            f"/gmail/v1/users/{quote(user_id, safe='')}/messages/{quote(message_id, safe='')}",
            token=token,
            params={
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "To", "Date"],
            },
        )
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
                results.append(self._record_error(domain, str(exc)))
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
        for message_id in dict.fromkeys(message_ids):
            metadata = self.source.message_metadata(connection, message_id=message_id)
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

        now = datetime.now(UTC).isoformat()
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
                "error_count": 0,
            },
        )
        return {
            "domain_key": domain.key,
            "status": "healthy",
            "history_id": history_id,
            "page_count": page_count,
            "seen_count": len(message_ids),
            "emitted_count": len(emitted),
            "skipped_count": len(skipped),
            "emitted": emitted,
            "skipped": skipped,
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
                "last_error": reason,
                "cursor_reset_at": now if status in {"reset", "cursor_reset"} else prior_payload.get("cursor_reset_at"),
            },
        )
        return {
            "domain_key": domain.key,
            "status": status,
            "history_id": history_id,
            "emitted_count": 0,
            "bootstrap": True,
        }

    def _record_error(self, domain: Domain, message: str) -> dict[str, Any]:
        prior = self._cursor_setting(domain)
        payload = dict(prior.value or {}) if prior else {}
        now = datetime.now(UTC).isoformat()
        error_count = int(payload.get("error_count") or 0) + 1
        updated = {
            **payload,
            "domain_key": domain.key,
            "status": "error",
            "last_polled_at": now,
            "last_error": message,
            "error_count": error_count,
        }
        self._write_cursor(
            domain,
            updated,
        )
        if error_count == 3:
            notification = WorkflowNotification(
                domain_id=domain.id,
                severity="warning",
                status="delivered",
                title=f"{domain.name} Gmail monitoring needs attention",
                message=(
                    f"Gmail trigger polling has failed three times and is not detecting new "
                    f"messages. {message}"
                ),
                notification_type="trigger_health",
                target="maestro_chat",
                delivered_at=datetime.now(UTC),
                metadata_={"domain_key": domain.key, "error_count": error_count},
            )
            self.session.add(notification)
            self.session.commit()
            record_channel_message(
                self.session,
                sender="maestro",
                content=(
                    f"I need your attention: {domain.name} Gmail monitoring has failed three "
                    f"times, so new-email workflows may be delayed. {message}"
                ),
                metadata={
                    "source": "gmail_trigger_worker",
                    "notification_id": str(notification.id),
                    "domain_key": domain.key,
                    "channel_visibility": "global",
                },
            )
        return {
            "domain_key": domain.key,
            "status": "error",
            "emitted_count": 0,
            "error": message,
            "error_count": error_count,
        }

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
