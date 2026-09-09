"""Durable mobile update delivery for messages in Maestro's primary channel."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    Conversation,
    Message,
    MessageReceipt,
    MobileDeviceEndpoint,
    MobileNotificationDelivery,
)

logger = logging.getLogger(__name__)

MOBILE_PRIORITIES = {"quiet", "important", "voice_worthy"}
VOICE_WORTHY_EVENTS = {
    "email_attention",
    "workflow_completed",
    "product_issue_complete",
    "todo_agent_task_complete",
}


def mobile_update_metadata(sender: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Mark background channel messages as canonical mobile updates."""
    stored = dict(metadata or {})
    if sender == "user" or stored.get("channel_visibility") != "global":
        return stored
    requested = str(stored.get("notification_priority") or "").lower()
    event_type = str(stored.get("event_type") or stored.get("message_type") or "").lower()
    priority = requested if requested in MOBILE_PRIORITIES else (
        "voice_worthy" if event_type in VOICE_WORTHY_EVENTS else "important"
    )
    stored.update(
        {
            "mobile_update": True,
            "mobile_update_priority": priority,
        }
    )
    return stored


def queue_message_deliveries(session: Session, message: Message) -> int:
    metadata = message.metadata_ or {}
    if message.sender_type == "user" or metadata.get("mobile_update") is not True:
        return 0
    endpoints = session.scalars(
        select(MobileDeviceEndpoint).where(MobileDeviceEndpoint.notifications_enabled.is_(True))
    ).all()
    queued = 0
    for endpoint in endpoints:
        exists = session.scalar(
            select(MobileNotificationDelivery.id).where(
                MobileNotificationDelivery.message_id == message.id,
                MobileNotificationDelivery.device_endpoint_id == endpoint.id,
            )
        )
        if exists is not None:
            continue
        session.add(
            MobileNotificationDelivery(
                message_id=message.id,
                device_endpoint_id=endpoint.id,
                priority=str(metadata.get("mobile_update_priority") or "important"),
                status="queued",
                attempts=0,
                next_attempt_at=datetime.now(UTC),
                metadata_={"source": "maestro_channel"},
            )
        )
        queued += 1
    if queued:
        session.commit()
    return queued


def register_device(
    session: Session,
    *,
    installation_id: uuid.UUID,
    device_token: str,
    environment: str,
    bundle_id: str,
    metadata: dict[str, Any] | None = None,
) -> MobileDeviceEndpoint:
    now = datetime.now(UTC)
    normalized_token = "".join(device_token.split()).lower()
    endpoint = session.scalar(
        select(MobileDeviceEndpoint).where(
            MobileDeviceEndpoint.installation_id == installation_id
        )
    )
    token_owners = session.scalars(
        select(MobileDeviceEndpoint).where(MobileDeviceEndpoint.device_token == normalized_token)
    ).all()
    for token_owner in token_owners:
        if endpoint is None or token_owner.id != endpoint.id:
            token_owner.notifications_enabled = False
    if endpoint is None:
        endpoint = MobileDeviceEndpoint(
            installation_id=installation_id,
            device_token=normalized_token,
            environment=environment,
            bundle_id=bundle_id,
            notifications_enabled=True,
            last_registered_at=now,
            metadata_=metadata or {},
        )
        session.add(endpoint)
    else:
        endpoint.device_token = normalized_token
        endpoint.environment = environment
        endpoint.bundle_id = bundle_id
        endpoint.notifications_enabled = True
        endpoint.last_registered_at = now
        endpoint.metadata_ = {**(endpoint.metadata_ or {}), **(metadata or {})}
    session.commit()
    session.refresh(endpoint)
    return endpoint


def mark_message_seen(
    session: Session,
    message: Message,
    *,
    via: str,
    metadata: dict[str, Any] | None = None,
) -> MessageReceipt:
    receipt = session.scalar(
        select(MessageReceipt).where(MessageReceipt.message_id == message.id)
    )
    if receipt is None:
        seen_at = datetime.now(UTC)
        receipt = MessageReceipt(
            message_id=message.id,
            seen_at=seen_at,
            seen_via=via,
            metadata_=metadata or {},
        )
        session.add(receipt)
        conversation = session.get(Conversation, message.conversation_id)
        if conversation is not None:
            conversation.updated_at = seen_at
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            receipt = session.scalar(
                select(MessageReceipt).where(MessageReceipt.message_id == message.id)
            )
            if receipt is None:
                raise
    session.refresh(receipt)
    return receipt


def update_payload(
    message: Message,
    receipt: MessageReceipt | None = None,
) -> dict[str, Any]:
    metadata = message.metadata_ or {}
    return {
        "id": str(message.id),
        "message_id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "title": str(metadata.get("notification_title") or "Maestro update"),
        "message": message.content,
        "priority": str(metadata.get("mobile_update_priority") or "important"),
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "seen_at": receipt.seen_at.isoformat() if receipt else None,
        "seen_via": receipt.seen_via if receipt else None,
        "metadata": {
            key: value
            for key, value in metadata.items()
            if key in {
                "event_type",
                "message_type",
                "workflow_run_id",
                "product_issue_id",
                "todo_id",
            }
        },
    }


class APNsDeliveryError(RuntimeError):
    def __init__(self, status_code: int, reason: str, apns_id: str | None = None):
        super().__init__(f"APNs returned {status_code}: {reason}")
        self.status_code = status_code
        self.reason = reason
        self.apns_id = apns_id


class APNsProvider:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._token: str | None = None
        self._token_created_at: datetime | None = None

    def send(
        self,
        *,
        endpoint: MobileDeviceEndpoint,
        message: Message,
        priority: str,
    ) -> str | None:
        host = (
            "https://api.push.apple.com"
            if endpoint.environment == "production"
            else "https://api.sandbox.push.apple.com"
        )
        url = f"{host}/3/device/{endpoint.device_token}"
        payload = self._payload(message, priority=priority)
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": endpoint.bundle_id or self.settings.apns_topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-collapse-id": str(message.id),
        }
        with httpx.Client(http2=True, timeout=15) as client:
            response = client.post(url, json=payload, headers=headers)
        apns_id = response.headers.get("apns-id")
        if response.status_code != 200:
            try:
                reason = str(response.json().get("reason") or response.text)
            except (ValueError, AttributeError):
                reason = response.text or "Unknown APNs error"
            raise APNsDeliveryError(response.status_code, reason, apns_id)
        return apns_id

    def _provider_token(self) -> str:
        now = datetime.now(UTC)
        if (
            self._token
            and self._token_created_at
            and now - self._token_created_at < timedelta(minutes=50)
        ):
            return self._token
        key_path = Path(str(self.settings.apns_private_key_path or ""))
        private_key = key_path.read_text(encoding="utf-8")
        self._token = jwt.encode(
            {"iss": self.settings.apns_team_id, "iat": int(now.timestamp())},
            private_key,
            algorithm="ES256",
            headers={"kid": self.settings.apns_key_id},
        )
        self._token_created_at = now
        return self._token

    @staticmethod
    def _payload(message: Message, *, priority: str) -> dict[str, Any]:
        interruption = {
            "quiet": "passive",
            "important": "active",
            "voice_worthy": "time-sensitive",
        }.get(priority, "active")
        return {
            "aps": {
                "alert": {"title": "Maestro", "body": "Maestro has an update"},
                "category": "MAESTRO_UPDATE",
                "thread-id": "maestro-updates",
                "interruption-level": interruption,
            },
            "maestro": {
                "update_id": str(message.id),
                "message_id": str(message.id),
                "conversation_id": str(message.conversation_id),
                "priority": priority,
            },
        }


class MobileNotificationWorker:
    def __init__(self, session: Session, *, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()
        self.provider = APNsProvider(self.settings)

    def run_once(self, *, limit: int = 25) -> int:
        if not self.settings.apns_configured:
            return 0
        now = datetime.now(UTC)
        deliveries = self.session.scalars(
            select(MobileNotificationDelivery)
            .where(
                MobileNotificationDelivery.status.in_(("queued", "retry")),
                or_(
                    MobileNotificationDelivery.next_attempt_at.is_(None),
                    MobileNotificationDelivery.next_attempt_at <= now,
                ),
            )
            .order_by(MobileNotificationDelivery.created_at, MobileNotificationDelivery.id)
            .limit(limit)
        ).all()
        completed = 0
        for delivery in deliveries:
            endpoint = self.session.get(MobileDeviceEndpoint, delivery.device_endpoint_id)
            message = self.session.get(Message, delivery.message_id)
            if endpoint is None or message is None or not endpoint.notifications_enabled:
                delivery.status = "cancelled"
                delivery.last_error = "Message or enabled endpoint is no longer available."
                continue
            delivery.attempts += 1
            try:
                delivery.apns_id = self.provider.send(
                    endpoint=endpoint,
                    message=message,
                    priority=delivery.priority,
                )
                delivery.status = "sent"
                delivery.sent_at = now
                delivery.next_attempt_at = None
                delivery.last_error = None
                completed += 1
            except APNsDeliveryError as error:
                delivery.apns_id = error.apns_id
                delivery.last_error = error.reason[:1000]
                if error.status_code == 410 or error.reason in {"BadDeviceToken", "Unregistered"}:
                    delivery.status = "failed"
                    endpoint.notifications_enabled = False
                elif delivery.attempts >= 5:
                    delivery.status = "failed"
                else:
                    delivery.status = "retry"
                    delivery.next_attempt_at = now + timedelta(
                        seconds=min(900, 15 * (2 ** (delivery.attempts - 1)))
                    )
            except Exception as error:  # transport and local credential failures are retryable
                logger.warning("APNs delivery failed: %s", error)
                delivery.last_error = str(error)[:1000]
                if delivery.attempts >= 5:
                    delivery.status = "failed"
                else:
                    delivery.status = "retry"
                    delivery.next_attempt_at = now + timedelta(
                        seconds=min(900, 15 * (2 ** (delivery.attempts - 1)))
                    )
        if deliveries:
            self.session.commit()
        return completed
