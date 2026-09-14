"""Server-side GPT-Live WebRTC session creation for Maestro Voice."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

LIVE_SESSIONS_URL = "https://api.openai.com/v1/live/sessions"


class LiveVoiceNotConfiguredError(RuntimeError):
    """Raised when the opt-in GPT-Live integration is not configured."""


class LiveVoiceUpstreamError(RuntimeError):
    """Raised when OpenAI rejects or cannot complete session creation."""

    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class LiveVoiceInvalidResponseError(RuntimeError):
    """Raised when OpenAI returns an incomplete WebRTC answer."""


@dataclass(frozen=True)
class LiveVoiceSession:
    session_id: str
    sdp: str
    model: str


class GPTLiveSessionService:
    """Exchange a client SDP offer for a short-lived GPT-Live WebRTC session."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def create_session(
        self,
        *,
        sdp: str,
        client_identifier: str,
    ) -> LiveVoiceSession:
        api_key = (self.settings.openai_api_key or "").strip()
        if not self.settings.openai_live_enabled or not api_key:
            raise LiveVoiceNotConfiguredError(
                "GPT-Live is disabled or OPENAI_API_KEY is not configured."
            )

        model = self.settings.openai_live_model.strip() or "gpt-live-1"
        payload: dict[str, Any] = {
            "session": {
                "model": model,
                "instructions": _live_instructions(),
                "delegation": {"type": "client"},
            },
            "transport": {"type": "webrtc", "sdp": sdp},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "OpenAI-Safety-Identifier": _safety_identifier(client_identifier),
        }
        started = time.monotonic()
        client = self._client or httpx.AsyncClient(
            timeout=self.settings.openai_live_timeout_seconds
        )
        owns_client = self._client is None
        try:
            response = await client.post(LIVE_SESSIONS_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "GPT-Live session creation failed before response",
                extra={"model": model, "error_type": type(exc).__name__},
            )
            raise LiveVoiceUpstreamError(
                status_code=502,
                detail="OpenAI could not be reached to create a live voice session.",
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        elapsed_ms = round((time.monotonic() - started) * 1000)
        if response.status_code not in range(200, 300):
            logger.warning(
                "GPT-Live session creation rejected",
                extra={
                    "model": model,
                    "upstream_status": response.status_code,
                    "latency_ms": elapsed_ms,
                },
            )
            raise LiveVoiceUpstreamError(
                status_code=response.status_code,
                detail=_safe_upstream_detail(response),
            )

        try:
            body = response.json()
            session_id = str(body["session"]["id"])
            answer_sdp = str(body["transport"]["sdp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveVoiceInvalidResponseError(
                "OpenAI returned an incomplete live voice session."
            ) from exc
        if not session_id or not answer_sdp:
            raise LiveVoiceInvalidResponseError(
                "OpenAI returned an incomplete live voice session."
            )

        logger.info(
            "GPT-Live session created",
            extra={"model": model, "session_id": session_id, "latency_ms": elapsed_ms},
        )
        return LiveVoiceSession(session_id=session_id, sdp=answer_sdp, model=model)


def _live_instructions() -> str:
    return """
You are the real-time voice layer for Maestro. Speak naturally, warmly, and concisely.
Maestro is the intelligence, memory, workflow, and action layer; you are not a separate assistant.

Delegate every substantive, contextual, factual, planning, memory, workflow, or action request to
the client. Delegate whenever you are uncertain. Do not answer those requests from your own
knowledge. The client will return Maestro's result; deliver that result faithfully in natural
speech without inventing facts or claiming work that Maestro did not complete.

You may handle only brief conversational mechanics locally: greetings, asking the user to repeat
unclear audio, and acknowledging an interruption. The phone handles deterministic session-ending
phrases locally. Do not read Markdown syntax, raw URLs, or formatting aloud. Keep spoken answers
summary-first and easy to interrupt.
""".strip()


def _safety_identifier(client_identifier: str) -> str:
    normalized = client_identifier.strip() or "maestro-voice-anonymous"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_upstream_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return "OpenAI rejected the live voice session request."
