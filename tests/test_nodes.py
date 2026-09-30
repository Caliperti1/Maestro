import base64
import json
import uuid
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.nodes import router
from app.db.models import NodeEnrollmentToken
from app.db.session import get_db
from app.nodes.service import NodeService, utcnow

_PRIVATE_KEY = Ed25519PrivateKey.generate()
_PUBLIC_KEY = base64.urlsafe_b64encode(
    _PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
).decode("ascii")


def _sign(payload: dict) -> str:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(_PRIVATE_KEY.sign(canonical)).decode("ascii")


def _client(session: Session) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _enroll(client: TestClient, *, allowed_capabilities: list[str] | None = None):
    token_response = client.post(
        "/nodes/enrollment-tokens",
        json={"allowed_capabilities": allowed_capabilities or []},
    )
    assert token_response.status_code == 200
    enrollment_code = token_response.json()["enrollment_code"]
    response = client.post(
        "/nodes/enroll",
        json={
            "enrollment_code": enrollment_code,
            "display_name": "Personal Mac",
            "platform": "macos",
            "client_version": "0.1.0",
            "public_key": _PUBLIC_KEY,
            "capabilities": [
                {"key": "diagnostic.echo", "version": "1.0.0", "mode": "read"}
            ],
        },
    )
    assert response.status_code == 200
    return token_response.json(), response.json()


def test_one_time_enrollment_hashes_tokens_and_authenticates_node(session: Session) -> None:
    client = _client(session)
    enrollment, enrolled = _enroll(client, allowed_capabilities=["diagnostic.echo"])

    token_record = session.get(NodeEnrollmentToken, uuid.UUID(enrollment["token_id"]))
    assert token_record is not None
    assert token_record.token_hash != enrollment["enrollment_code"]
    assert enrollment["enrollment_code"] not in token_record.token_hash

    reused = client.post(
        "/nodes/enroll",
        json={
            "enrollment_code": enrollment["enrollment_code"],
            "display_name": "Second Mac",
            "platform": "macos",
            "public_key": _PUBLIC_KEY,
            "capabilities": [],
        },
    )
    assert reused.status_code == 409

    heartbeat = client.post(
        "/node/v1/heartbeat",
        headers={"Authorization": f"Bearer {enrolled['access_token']}"},
        json={
            "node_id": enrolled["node_id"],
            "client_version": "0.1.1",
            "hostname": "personal-mac",
            "capabilities": [
                {"key": "diagnostic.echo", "version": "1.0.0", "mode": "read"}
            ],
        },
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["node"]["effective_status"] == "online"
    assert heartbeat.json()["node"]["capabilities"][0]["details"]["mode"] == "read"

    rejected = client.post(
        "/node/v1/heartbeat",
        headers={"Authorization": "Bearer invalid"},
        json={},
    )
    assert rejected.status_code == 401


def test_enrollment_capability_allowlist_is_enforced(session: Session) -> None:
    client = _client(session)
    created = client.post(
        "/nodes/enrollment-tokens",
        json={"allowed_capabilities": ["diagnostic.echo"]},
    ).json()

    response = client.post(
        "/nodes/enroll",
        json={
            "enrollment_code": created["enrollment_code"],
            "display_name": "Overpowered Mac",
            "platform": "macos",
            "public_key": _PUBLIC_KEY,
            "capabilities": ["coding.codex"],
        },
    )

    assert response.status_code == 403
    assert "coding.codex" in response.json()["detail"]


def test_diagnostic_job_round_trip_matches_mac_client_contract(session: Session) -> None:
    client = _client(session)
    _, enrolled = _enroll(client)
    headers = {"Authorization": f"Bearer {enrolled['access_token']}"}

    created = client.post(
        f"/nodes/{enrolled['node_id']}/diagnostic-jobs",
        json={"message": "hello node", "idempotency_key": "diagnostic:test:1"},
    )
    assert created.status_code == 200
    assert created.json()["job"]["status"] == "queued"

    leased = client.post(
        "/node/v1/jobs/lease",
        headers=headers,
        json={
            "node_id": enrolled["node_id"],
            "capabilities": ["diagnostic.echo"],
            "wait_seconds": 0,
        },
    )
    assert leased.status_code == 200
    job = leased.json()["job"]
    assert job["job_id"] == created.json()["job"]["id"]
    assert job["idempotency_key"] == "diagnostic:test:1"
    assert job["capability"] == "diagnostic.echo"
    assert job["input_envelope"] == {"value": "hello node"}
    assert job["lease_generation"] == 1

    renewed = client.post(
        f"/node/v1/jobs/{job['job_id']}/renew",
        headers=headers,
        json={
            "lease_token": job["lease_token"],
            "lease_generation": job["lease_generation"],
        },
    )
    assert renewed.status_code == 200

    result_envelope = {
        "job_id": job["job_id"],
        "capability": "diagnostic.echo",
        "result": {"value": "hello node"},
    }
    signature = _sign(result_envelope)
    invalid = client.post(
        f"/node/v1/jobs/{job['job_id']}/complete",
        headers=headers,
        json={
            "lease_token": job["lease_token"],
            "lease_generation": job["lease_generation"],
            "result_envelope": result_envelope,
            "signature": base64.urlsafe_b64encode(b"not-a-valid-signature").decode("ascii"),
        },
    )
    assert invalid.status_code == 403

    completed = client.post(
        f"/node/v1/jobs/{job['job_id']}/complete",
        headers=headers,
        json={
            "lease_token": job["lease_token"],
            "lease_generation": job["lease_generation"],
            "result_envelope": result_envelope,
            "signature": signature,
        },
    )
    assert completed.status_code == 200
    assert completed.json()["job"]["status"] == "completed"
    assert completed.json()["job"]["output_envelope"] == {
        "result_envelope": result_envelope,
        "signature": signature,
    }

    # A lost HTTP response can be retried without executing the local action again.
    repeated = client.post(
        f"/node/v1/jobs/{job['job_id']}/complete",
        headers=headers,
        json={
            "lease_token": job["lease_token"],
            "lease_generation": job["lease_generation"],
            "result_envelope": result_envelope,
            "signature": signature,
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["job"]["status"] == "completed"

    detail = client.get(f"/nodes/{enrolled['node_id']}")
    assert detail.status_code == 200
    assert [event["event_type"] for event in detail.json()["events"]] == [
        "completed",
        "lease_renewed",
        "leased",
        "created",
    ]


def test_retryable_failure_returns_job_to_waiting_and_increments_generation(
    session: Session,
) -> None:
    client = _client(session)
    _, enrolled = _enroll(client)
    headers = {"Authorization": f"Bearer {enrolled['access_token']}"}
    created = client.post(
        f"/nodes/{enrolled['node_id']}/diagnostic-jobs",
        json={"message": "retry me"},
    ).json()["job"]
    first = client.post(
        "/node/v1/jobs/lease",
        headers=headers,
        json={"capabilities": ["diagnostic.echo"]},
    ).json()["job"]

    failed = client.post(
        f"/node/v1/jobs/{created['id']}/fail",
        headers=headers,
        json={
            "lease_token": first["lease_token"],
            "lease_generation": 1,
            "error_code": "temporary",
            "message": "Try again later.",
            "retryable": True,
        },
    )
    assert failed.status_code == 200
    assert failed.json()["job"]["status"] == "waiting_for_node"

    second = client.post(
        "/node/v1/jobs/lease",
        headers=headers,
        json={"capabilities": ["diagnostic.echo"]},
    ).json()["job"]
    assert second["job_id"] == first["job_id"]
    assert second["lease_generation"] == 2
    assert second["lease_token"] != first["lease_token"]

    stale = client.post(
        f"/node/v1/jobs/{created['id']}/renew",
        headers=headers,
        json={"lease_token": second["lease_token"], "lease_generation": 1},
    )
    assert stale.status_code == 409


def test_node_list_reports_stale_heartbeat_as_offline(session: Session) -> None:
    client = _client(session)
    _, enrolled = _enroll(client)
    service = NodeService(session)
    node = service.get_node(uuid.UUID(enrolled["node_id"]))
    node.last_heartbeat_at = utcnow() - timedelta(minutes=5)
    session.commit()

    response = client.get("/nodes")

    assert response.status_code == 200
    assert response.json()["nodes"][0]["status"] == "online"
    assert response.json()["nodes"][0]["effective_status"] == "offline"
