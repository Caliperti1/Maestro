from __future__ import annotations

import json
from http.cookies import SimpleCookie
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.service import OwnerSessionService, csrf_matches
from app.core.config import get_settings
from app.db.session import SessionLocal


_PUBLIC_EXACT = {
    "/health",
    "/health/live",
    "/health/ready",
    "/auth/status",
    "/auth/login",
    "/auth/callback",
    "/nodes/enroll",
}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _session_cookie(scope: Scope, cookie_name: str) -> str | None:
    cookie = SimpleCookie()
    cookie.load(_headers(scope).get("cookie", ""))
    morsel = cookie.get(cookie_name)
    return morsel.value if morsel is not None else None


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith("/node/v1/")


async def _json_response(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class OwnerAuthMiddleware:
    """Authenticate browser HTTP and WebSocket traffic with a revocable owner session."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        settings = get_settings()
        if settings.owner_auth_mode == "disabled" or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if scope["type"] == "http" and _is_public(path):
            await self.app(scope, receive, send)
            return

        raw_token = _session_cookie(scope, settings.owner_session_cookie_name)
        with SessionLocal() as db:
            owner = OwnerSessionService(db, settings).authenticate(raw_token, touch=True)
        if owner is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401, "reason": "Authentication required"})
            else:
                await _json_response(send, 401, "Owner authentication required.")
            return

        headers = _headers(scope)
        if scope["type"] == "websocket":
            origin = headers.get("origin")
            if origin not in settings.cors_origins:
                await send({"type": "websocket.close", "code": 4403, "reason": "Origin not allowed"})
                return
        elif str(scope.get("method") or "GET").upper() not in _SAFE_METHODS:
            if not raw_token or not csrf_matches(settings, raw_token, headers.get("x-csrf-token")):
                await _json_response(send, 403, "Valid CSRF token required.")
                return

        scope.setdefault("state", {})["owner"] = owner
        await self.app(scope, receive, send)
