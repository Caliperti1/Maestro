from __future__ import annotations

import httpx

from maestro_node.api import NodeAPI


def test_lease_accepts_wrapped_job_and_sends_bearer_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "job": {
                    "job_id": "job-1",
                    "idempotency_key": "idem-1",
                    "capability": "diagnostic.echo",
                    "input_envelope": {"value": "hi"},
                    "lease_token": "lease-secret",
                    "lease_generation": 2,
                    "lease_seconds": 90,
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    api = NodeAPI("https://example.test", credential="device-secret", client=client)
    try:
        job = api.lease({"node_id": "node-1"}, wait_seconds=0)
    finally:
        api.close()

    assert job is not None
    assert job.job_id == "job-1"
    assert job.input_envelope == {"value": "hi"}
    assert requests[0].headers["authorization"] == "Bearer device-secret"


def test_lease_returns_none_when_queue_is_empty() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"job": None}))
    api = NodeAPI("https://example.test", client=httpx.Client(transport=transport))
    try:
        assert api.lease({}, wait_seconds=0) is None
    finally:
        api.close()

