from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import AgentRegistryService
from app.core.config import Settings
from app.db.models import RuntimeSetting, ToolConnection
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.integrations.credentials import decrypt_credential_bundle
from app.integrations.oauth import IntegrationOAuthError, IntegrationOAuthService
from app.tools import runtime as tool_runtime


class FakeHttpClient:
    def __init__(self, responses: list[httpx.Response]):
        self.responses = responses

    def post(self, *args, **kwargs) -> httpx.Response:
        return self.responses.pop(0)

    def get(self, *args, **kwargs) -> httpx.Response:
        return self.responses.pop(0)


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status, json=payload, request=httpx.Request("GET", "https://example.test")
    )


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "integration_credential_encryption_key": "test-encryption-key-with-enough-entropy",
        "integration_google_client_id": "google-client-id",
        "integration_google_client_secret": "google-client-secret",
        "integration_google_redirect_uri": "https://api.example.test/integrations/google/callback",
        "integration_github_client_id": "github-client-id",
        "integration_github_client_secret": "github-client-secret",
        "integration_github_redirect_uri": "https://api.example.test/integrations/github/callback",
    }
    values.update(overrides)
    return Settings(**values)


def test_google_one_click_connection_encrypts_and_runtime_can_use_credentials(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_default_domains(session)
    session.add_all(
        [
            RuntimeSetting(
                key=f"{prefix}personal",
                value={
                    "status": "error",
                    "last_error": "invalid_grant",
                    "error_count": 3,
                    "auth_required": True,
                    "next_retry_at": "2099-01-01T00:00:00+00:00",
                },
            )
            for prefix in ("gmail_trigger_cursor:", "calendar_trigger_cursor:")
        ]
    )
    session.commit()
    settings = _settings()
    service = IntegrationOAuthService(
        session,
        settings=settings,
        http_client=FakeHttpClient(
            [
                _response(
                    200,
                    {
                        "access_token": "short-lived-access-token",
                        "refresh_token": "durable-refresh-token",
                        "scope": "openid email https://www.googleapis.com/auth/calendar",
                    },
                ),
                _response(200, {"email": "owner@example.test"}),
            ]
        ),
    )

    authorization_url = service.begin_authorization(
        provider="google",
        domain_key="personal",
        callback_base_url="https://ignored.example.test",
    )
    query = parse_qs(urlparse(authorization_url).query)
    state = query["state"][0]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent select_account"]

    domain_key, account = service.complete_authorization(
        provider="google",
        state=state,
        code="authorization-code",
        callback_base_url="https://ignored.example.test",
    )

    assert domain_key == "personal"
    assert account == "owner@example.test"
    personal = DomainRepository(session).get_by_key("personal")
    connection = session.scalar(
        select(ToolConnection).where(
            ToolConnection.domain_id == personal.id,
            ToolConnection.tool_key == "google",
        )
    )
    assert connection is not None
    serialized = str(connection.config)
    assert "durable-refresh-token" not in serialized
    credentials = decrypt_credential_bundle(
        connection.config["oauth_credential_ciphertext"], settings=settings
    )
    assert credentials["refresh_token"] == "durable-refresh-token"

    monkeypatch.setenv(
        "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY",
        settings.integration_credential_encryption_key or "",
    )
    monkeypatch.setattr(
        tool_runtime,
        "_google_oauth_refresh_access_token",
        lambda **kwargs: {
            "access_token": f"refreshed:{kwargs['refresh_token']}",
            "expires_in": 3600,
        },
    )
    assert tool_runtime._gmail_access_token(connection) == "refreshed:durable-refresh-token"

    listed = next(
        item
        for item in AgentRegistryService(session).list_tool_connections()
        if item.domain_key == "personal" and item.tool_key == "google"
    )
    assert listed.config["oauth_credential_ciphertext"] == "********"
    for prefix in ("gmail_trigger_cursor:", "calendar_trigger_cursor:"):
        cursor = session.get(RuntimeSetting, f"{prefix}personal")
        assert cursor.value["status"] == "reauthorizing"
        assert cursor.value["next_retry_at"] is None
        assert cursor.value["auth_required"] is False


def test_github_one_click_connection_supplies_token_to_existing_runtime(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_default_domains(session)
    settings = _settings()
    service = IntegrationOAuthService(
        session,
        settings=settings,
        http_client=FakeHttpClient(
            [
                _response(200, {"access_token": "github-oauth-token", "scope": "repo,read:org"}),
                _response(200, {"login": "octocat"}),
            ]
        ),
    )
    authorization_url = service.begin_authorization(
        provider="github",
        domain_key="perti-laboratories",
        callback_base_url="https://ignored.example.test",
    )
    state = parse_qs(urlparse(authorization_url).query)["state"][0]
    service.complete_authorization(
        provider="github",
        state=state,
        code="authorization-code",
        callback_base_url="https://ignored.example.test",
    )
    domain = DomainRepository(session).get_by_key("perti-laboratories")
    connection = session.scalar(
        select(ToolConnection).where(
            ToolConnection.domain_id == domain.id,
            ToolConnection.tool_key == "github",
        )
    )
    assert connection is not None

    from app.core.config import get_settings

    monkeypatch.setenv(
        "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY",
        settings.integration_credential_encryption_key or "",
    )
    get_settings.cache_clear()
    assert tool_runtime._github_env(connection)["GH_TOKEN"] == "github-oauth-token"


def test_oauth_state_is_single_use(session: Session) -> None:
    seed_default_domains(session)
    settings = _settings()
    service = IntegrationOAuthService(
        session,
        settings=settings,
        http_client=FakeHttpClient(
            [
                _response(200, {"access_token": "token", "scope": "repo"}),
                _response(200, {"login": "octocat"}),
            ]
        ),
    )
    authorization_url = service.begin_authorization(
        provider="github",
        domain_key="personal",
        callback_base_url="https://ignored.example.test",
    )
    state = parse_qs(urlparse(authorization_url).query)["state"][0]
    service.complete_authorization(
        provider="github",
        state=state,
        code="first-code",
        callback_base_url="https://ignored.example.test",
    )

    assert (
        session.scalar(
            select(RuntimeSetting).where(RuntimeSetting.key.contains("integration_oauth_state"))
        )
        is None
    )
    with pytest.raises(IntegrationOAuthError, match="already been used"):
        service.complete_authorization(
            provider="github",
            state=state,
            code="replayed-code",
            callback_base_url="https://ignored.example.test",
        )
