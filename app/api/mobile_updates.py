"""Maestro Voice device registration, update retrieval, and shared seen state."""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Message, MessageReceipt, MobileDeviceEndpoint, MobileNotificationDelivery
from app.db.session import get_db
from app.maestro.mobile_notifications import mark_message_seen, register_device, update_payload

router = APIRouter(prefix="/maestro/mobile", tags=["maestro-mobile"])


class MobileDeviceRegistration(BaseModel):
    installation_id: uuid.UUID
    device_token: str = Field(min_length=32, max_length=256)
    environment: Literal["sandbox", "production"] = "sandbox"
    bundle_id: str = Field(min_length=3, max_length=240)
    app_version: str | None = Field(default=None, max_length=80)
    device_name: str | None = Field(default=None, max_length=160)

    @field_validator("device_token")
    @classmethod
    def validate_device_token(cls, value: str) -> str:
        token = "".join(value.split()).lower()
        if any(character not in "0123456789abcdef" for character in token):
            raise ValueError("device_token must be a hexadecimal APNs token")
        return token


class MessageSeenBody(BaseModel):
    via: Literal["web", "ios", "ios_voice", "notification_action"]
    detail: str | None = Field(default=None, max_length=240)


@router.post("/devices")
def register_mobile_device(
    body: MobileDeviceRegistration,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    endpoint = register_device(
        db,
        installation_id=body.installation_id,
        device_token=body.device_token,
        environment=body.environment,
        bundle_id=body.bundle_id,
        metadata={
            "app_version": body.app_version,
            "device_name": body.device_name,
        },
    )
    return {
        "device": {
            "id": str(endpoint.id),
            "installation_id": str(endpoint.installation_id),
            "environment": endpoint.environment,
            "bundle_id": endpoint.bundle_id,
            "notifications_enabled": endpoint.notifications_enabled,
            "last_registered_at": endpoint.last_registered_at.isoformat(),
        },
        "apns_provider_configured": get_settings().apns_configured,
    }


@router.delete("/devices/{installation_id}")
def disable_mobile_device(
    installation_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    endpoint = db.scalar(
        select(MobileDeviceEndpoint).where(
            MobileDeviceEndpoint.installation_id == installation_id
        )
    )
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Unknown mobile installation.")
    endpoint.notifications_enabled = False
    endpoint.updated_at = datetime.now(UTC)
    db.commit()
    return {"disabled": True}


@router.get("/status")
def get_mobile_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    endpoints = db.scalars(
        select(MobileDeviceEndpoint).order_by(MobileDeviceEndpoint.last_registered_at.desc())
    ).all()
    pending_count = len(
        db.scalars(
            select(MobileNotificationDelivery.id).where(
                MobileNotificationDelivery.status.in_(("queued", "retry"))
            )
        ).all()
    )
    return {
        "apns_provider_configured": get_settings().apns_configured,
        "pending_delivery_count": pending_count,
        "devices": [
            {
                "id": str(endpoint.id),
                "installation_id": str(endpoint.installation_id),
                "environment": endpoint.environment,
                "bundle_id": endpoint.bundle_id,
                "notifications_enabled": endpoint.notifications_enabled,
                "last_registered_at": endpoint.last_registered_at.isoformat(),
                "token_suffix": endpoint.device_token[-8:],
                "metadata": endpoint.metadata_,
            }
            for endpoint in endpoints
        ],
    }


@router.get("/updates/latest")
def get_latest_mobile_update(
    unseen: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = (
        select(Message)
        .where(
            Message.sender_type == "maestro",
            Message.metadata_["mobile_update"].as_boolean().is_(True),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
    )
    if unseen:
        query = query.outerjoin(MessageReceipt, MessageReceipt.message_id == Message.id).where(
            MessageReceipt.id.is_(None)
        )
    message = db.scalar(query.limit(1))
    if message is None:
        raise HTTPException(status_code=404, detail="No unread Maestro updates.")
    receipt = db.scalar(select(MessageReceipt).where(MessageReceipt.message_id == message.id))
    return {"update": update_payload(message, receipt)}


@router.get("/updates/{message_id}")
def get_mobile_update(
    message_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    message = db.get(Message, message_id)
    if message is None or (message.metadata_ or {}).get("mobile_update") is not True:
        raise HTTPException(status_code=404, detail="Unknown Maestro update.")
    receipt = db.scalar(select(MessageReceipt).where(MessageReceipt.message_id == message.id))
    return {"update": update_payload(message, receipt)}


@router.post("/updates/{message_id}/seen")
def see_mobile_update(
    message_id: uuid.UUID,
    body: MessageSeenBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    message = db.get(Message, message_id)
    if message is None or (message.metadata_ or {}).get("mobile_update") is not True:
        raise HTTPException(status_code=404, detail="Unknown Maestro update.")
    receipt = mark_message_seen(
        db,
        message,
        via=body.via,
        metadata={"detail": body.detail} if body.detail else {},
    )
    return {"update": update_payload(message, receipt)}
