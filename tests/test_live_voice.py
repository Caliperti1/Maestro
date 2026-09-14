import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.voice_live import get_live_session_service, router
from app.core.config import Settings
from app.maestro.live_voice import GPTLiveSessionService, LiveVoiceUpstreamError


def _settings(**overrides) -> Settings:
    return Settings(
        openai_live_enabled=True,
        openai_api_key="test-key",
        **overrides,
    )


def test_service_creates_client_delegation_session_without_exposing_key() -> None:
    captured: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(
            201,
            json={
                "session": {"id": "live_test"},
                "transport": {"type": "webrtc", "sdp": "answer-sdp"},
            },
        )

    async def create():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await GPTLiveSessionService(_settings(), client=client).create_session(
                sdp="offer-sdp-long-enough",
                client_identifier="installation-123",
            )

    result = asyncio.run(create())

    assert result.session_id == "live_test"
    assert result.sdp == "answer-sdp"
    assert captured is not None
    payload = json.loads(captured.content)
    assert payload["session"]["model"] == "gpt-live-1"
    assert payload["session"]["delegation"] == {"type": "client"}
    assert payload["transport"] == {"type": "webrtc", "sdp": "offer-sdp-long-enough"}
    assert captured.headers["authorization"] == "Bearer test-key"
    assert captured.headers["openai-safety-identifier"] != "installation-123"
    assert "test-key" not in str(payload)


def test_service_sanitizes_non_json_upstream_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="internal upstream details")

    async def create():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await GPTLiveSessionService(_settings(), client=client).create_session(
                sdp="offer-sdp-long-enough",
                client_identifier="installation-123",
            )

    with pytest.raises(LiveVoiceUpstreamError) as exc_info:
        asyncio.run(create())

    assert exc_info.value.status_code == 429
    assert str(exc_info.value) == "OpenAI rejected the live voice session request."


def test_endpoint_reports_disabled_live_voice_without_calling_upstream() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_live_session_service] = lambda: GPTLiveSessionService(
        Settings(openai_live_enabled=False, openai_api_key=None)
    )

    response = TestClient(app).post(
        "/maestro/voice/live/session",
        json={"sdp": "offer-sdp-long-enough", "client_identifier": "installation-123"},
    )

    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_endpoint_returns_webrtc_answer() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "session": {"id": "live_endpoint"},
                "transport": {"type": "webrtc", "sdp": "answer-sdp"},
            },
        )

    transport = httpx.MockTransport(handler)

    async def service_override():
        client = httpx.AsyncClient(transport=transport)
        return GPTLiveSessionService(_settings(), client=client)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_live_session_service] = service_override
    response = TestClient(app).post(
        "/maestro/voice/live/session",
        json={"sdp": "offer-sdp-long-enough", "client_identifier": "installation-123"},
    )

    assert response.status_code == 201
    assert response.json() == {
        "session_id": "live_endpoint",
        "sdp": "answer-sdp",
        "model": "gpt-live-1",
    }
