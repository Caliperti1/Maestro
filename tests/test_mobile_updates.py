import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.mobile_updates import router
from app.db.models import MessageReceipt, MobileNotificationDelivery
from app.db.session import get_db
from app.maestro.channel import record_channel_message
from app.maestro.mobile_notifications import APNsProvider, register_device


def _client(session) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_global_background_message_queues_generic_voice_worthy_push(session) -> None:
    endpoint = register_device(
        session,
        installation_id=uuid.UUID("5a119c4e-5100-4c35-9730-a8cdd39dd847"),
        device_token="ab" * 32,
        environment="sandbox",
        bundle_id="com.pertilaboratories.maestrovoice",
    )

    message = record_channel_message(
        session,
        sender="maestro",
        content="The private report includes a blocking authentication issue.",
        metadata={
            "source": "scheduler_worker",
            "event_type": "workflow_completed",
            "channel_visibility": "global",
        },
    )

    delivery = session.scalar(
        select(MobileNotificationDelivery).where(
            MobileNotificationDelivery.message_id == message.id,
            MobileNotificationDelivery.device_endpoint_id == endpoint.id,
        )
    )
    assert delivery is not None
    assert delivery.priority == "voice_worthy"
    assert message.metadata_["mobile_update"] is True

    payload = APNsProvider._payload(message, priority=delivery.priority)
    assert payload["aps"]["alert"]["body"] == "Maestro has an update"
    assert payload["aps"]["interruption-level"] == "time-sensitive"
    assert message.content not in str(payload)
    assert payload["maestro"]["update_id"] == str(message.id)


def test_latest_update_and_seen_receipt_are_shared(session) -> None:
    message = record_channel_message(
        session,
        sender="maestro",
        content="Your coding agent finished successfully.",
        metadata={
            "event_type": "workflow_completed",
            "channel_visibility": "global",
        },
    )
    client = _client(session)

    latest = client.get("/maestro/mobile/updates/latest")
    assert latest.status_code == 200
    assert latest.json()["update"]["message"] == message.content
    assert latest.json()["update"]["seen_at"] is None

    seen = client.post(
        f"/maestro/mobile/updates/{message.id}/seen",
        json={"via": "ios_voice", "detail": "Read aloud by Maestro Voice"},
    )
    assert seen.status_code == 200
    assert seen.json()["update"]["seen_via"] == "ios_voice"
    assert seen.json()["update"]["seen_at"] is not None
    assert session.scalar(
        select(MessageReceipt).where(MessageReceipt.message_id == message.id)
    ) is not None

    no_unread = client.get("/maestro/mobile/updates/latest")
    assert no_unread.status_code == 404
    most_recent = client.get("/maestro/mobile/updates/latest?unseen=false")
    assert most_recent.status_code == 200
    assert most_recent.json()["update"]["seen_via"] == "ios_voice"


def test_device_registration_is_idempotent_and_reports_provider_state(session) -> None:
    client = _client(session)
    body = {
        "installation_id": "ae9701d9-4b43-493d-8bd9-4ac7cb0b66bf",
        "device_token": "cd" * 32,
        "environment": "sandbox",
        "bundle_id": "com.pertilaboratories.maestrovoice",
        "app_version": "0.5.0 (11)",
        "device_name": "Chris's iPhone",
    }

    first = client.post("/maestro/mobile/devices", json=body)
    second = client.post("/maestro/mobile/devices", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["device"]["id"] == second.json()["device"]["id"]
    assert second.json()["apns_provider_configured"] is False
