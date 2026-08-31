import uuid
from io import BytesIO
from urllib.error import HTTPError

import pytest

from app.db.models import ToolConnection
from app.tools import runtime


def test_google_oauth_access_token_is_reused_until_near_expiry(monkeypatch) -> None:
    connection = ToolConnection(
        id=uuid.uuid4(),
        domain_id=uuid.uuid4(),
        tool_key="google",
        display_name="Test Google Workspace",
        auth_type="oauth",
        config={
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
        },
        is_active=True,
    )
    refresh_calls: list[str] = []
    clock = [100.0]

    def fake_refresh_token(*, client_id: str, client_secret: str, refresh_token: str):
        refresh_calls.append(refresh_token)
        return {
            "access_token": f"token-{len(refresh_calls)}",
            "expires_in": 120,
        }

    monkeypatch.setattr(runtime, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime, "_google_oauth_refresh_access_token", fake_refresh_token)
    runtime._google_access_token_cache.clear()

    assert runtime._gmail_access_token(connection) == "token-1"
    clock[0] = 150.0
    assert runtime._gmail_access_token(connection) == "token-1"
    clock[0] = 161.0
    assert runtime._gmail_access_token(connection) == "token-2"
    assert refresh_calls == ["refresh-token", "refresh-token"]

    runtime._google_access_token_cache.clear()


def test_gmail_unauthorized_response_invalidates_cached_access_token(monkeypatch) -> None:
    cache_key = ("connection-id", "credential-fingerprint")
    runtime._google_access_token_cache[cache_key] = ("expired-token", 999.0)

    def unauthorized(*args, **kwargs):
        raise HTTPError(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"error": "invalid_token"}'),
        )

    monkeypatch.setattr(runtime, "urlopen", unauthorized)

    with pytest.raises(runtime.ToolExecutionError, match="failed: 401"):
        runtime._gmail_api_json(
            "GET",
            "/gmail/v1/users/me/profile",
            token="expired-token",
        )

    assert cache_key not in runtime._google_access_token_cache
