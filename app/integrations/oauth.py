from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import RuntimeSetting, ToolConnection
from app.db.repositories import DomainRepository
from app.integrations.credentials import (
    encrypt_credential_bundle,
)

GOOGLE_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.meet.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/meetings.space.readonly",
    "https://www.googleapis.com/auth/calendar",
)
GITHUB_SCOPES = ("repo", "read:org", "user:email")
_STATE_TTL = timedelta(minutes=10)


class IntegrationOAuthError(RuntimeError):
    """A safe, user-facing integration connection error."""


@dataclass(frozen=True)
class ProviderConfig:
    key: str
    name: str
    client_id: str | None
    client_secret: str | None
    redirect_uri: str
    authorization_url: str
    token_url: str
    scopes: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


class IntegrationOAuthService:
    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
    ):
        self.session = session
        self.settings = settings or get_settings()
        self.http = http_client or httpx.Client(timeout=30.0, follow_redirects=True)

    def provider_configs(self, *, callback_base_url: str | None = None) -> list[ProviderConfig]:
        base = (callback_base_url or "").rstrip("/")
        return [
            ProviderConfig(
                key="google",
                name="Google Workspace",
                client_id=self.settings.integration_google_client_id,
                client_secret=self.settings.integration_google_client_secret,
                redirect_uri=(
                    self.settings.integration_google_redirect_uri
                    or (f"{base}/integrations/google/callback" if base else "")
                ),
                authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
                token_url="https://oauth2.googleapis.com/token",
                scopes=GOOGLE_SCOPES,
            ),
            ProviderConfig(
                key="github",
                name="GitHub",
                client_id=self.settings.integration_github_client_id,
                client_secret=self.settings.integration_github_client_secret,
                redirect_uri=(
                    self.settings.integration_github_redirect_uri
                    or (f"{base}/integrations/github/callback" if base else "")
                ),
                authorization_url="https://github.com/login/oauth/authorize",
                token_url="https://github.com/login/oauth/access_token",
                scopes=GITHUB_SCOPES,
            ),
        ]

    def provider_config(
        self, provider: str, *, callback_base_url: str | None = None
    ) -> ProviderConfig:
        normalized = provider.strip().lower()
        config = next(
            (
                item
                for item in self.provider_configs(callback_base_url=callback_base_url)
                if item.key == normalized
            ),
            None,
        )
        if config is None:
            raise IntegrationOAuthError(f"Unsupported integration provider: {provider}")
        return config

    def begin_authorization(
        self,
        *,
        provider: str,
        domain_key: str,
        callback_base_url: str,
    ) -> str:
        domain = DomainRepository(self.session).get_by_key(domain_key)
        if domain is None:
            raise IntegrationOAuthError(f"Unknown domain: {domain_key}")
        config = self.provider_config(provider, callback_base_url=callback_base_url)
        if not config.configured:
            raise IntegrationOAuthError(
                f"{config.name} one-click connection is not configured on this Maestro deployment."
            )
        # Fail before sending the browser away if encrypted credential storage is unavailable.
        encrypt_credential_bundle({"probe": True}, settings=self.settings)
        self.session.execute(
            delete(RuntimeSetting).where(
                RuntimeSetting.key.like("integration_oauth_state:%"),
                RuntimeSetting.updated_at < datetime.now(UTC) - _STATE_TTL,
            )
        )
        state = secrets.token_urlsafe(40)
        self.session.add(
            RuntimeSetting(
                key=_state_key(state),
                value={
                    "provider": config.key,
                    "domain_key": domain.key,
                    "redirect_uri": config.redirect_uri,
                    "expires_at": (datetime.now(UTC) + _STATE_TTL).isoformat(),
                },
            )
        )
        self.session.commit()
        query: dict[str, str] = {
            "client_id": config.client_id or "",
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(config.scopes),
            "state": state,
        }
        if config.key == "google":
            query.update(
                {
                    "access_type": "offline",
                    "include_granted_scopes": "true",
                    "prompt": "consent select_account",
                }
            )
        return f"{config.authorization_url}?{urlencode(query)}"

    def complete_authorization(
        self,
        *,
        provider: str,
        state: str,
        code: str,
        callback_base_url: str,
    ) -> tuple[str, str]:
        state_record = self.session.get(RuntimeSetting, _state_key(state))
        if state_record is None:
            raise IntegrationOAuthError("This connection link is invalid or has already been used.")
        state_value = state_record.value or {}
        expires_at = _parse_datetime(state_value.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(UTC):
            self.session.delete(state_record)
            self.session.commit()
            raise IntegrationOAuthError(
                "This connection link expired. Start again from the Tools page."
            )
        if str(state_value.get("provider")) != provider:
            raise IntegrationOAuthError(
                "The integration provider did not match the connection request."
            )
        domain_key = str(state_value.get("domain_key") or "")
        redirect_uri = str(state_value.get("redirect_uri") or "")
        config = self.provider_config(provider, callback_base_url=callback_base_url)
        if redirect_uri != config.redirect_uri:
            raise IntegrationOAuthError(
                "The integration callback address changed. Start again from Tools."
            )

        # Make the CSRF state one-time before exchanging the authorization code.
        self.session.delete(state_record)
        self.session.commit()
        if provider == "google":
            credentials, account_label, granted_scopes = self._complete_google(
                config=config, code=code
            )
        elif provider == "github":
            credentials, account_label, granted_scopes = self._complete_github(
                config=config, code=code
            )
        else:  # guarded by provider_config, retained for type safety
            raise IntegrationOAuthError(f"Unsupported integration provider: {provider}")
        self._store_connection(
            provider=provider,
            domain_key=domain_key,
            credentials=credentials,
            account_label=account_label,
            scopes=granted_scopes,
        )
        return domain_key, account_label

    def disconnect(self, *, provider: str, domain_key: str) -> bool:
        domain = DomainRepository(self.session).get_by_key(domain_key)
        if domain is None:
            raise IntegrationOAuthError(f"Unknown domain: {domain_key}")
        connection = self.session.scalar(
            select(ToolConnection).where(
                ToolConnection.domain_id == domain.id,
                ToolConnection.tool_key == provider,
            )
        )
        if connection is None:
            return False
        config = dict(connection.config or {})
        config.pop("oauth_credential_ciphertext", None)
        config.pop("oauth_connected_at", None)
        config.pop("oauth_scopes", None)
        config.pop("account_label", None)
        connection.config = config
        connection.is_active = False
        self.session.commit()
        return True

    def _complete_google(
        self, *, config: ProviderConfig, code: str
    ) -> tuple[dict[str, Any], str, list[str]]:
        response = self.http.post(
            config.token_url,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": config.redirect_uri,
            },
        )
        token = _response_json(response, provider_name=config.name)
        access_token = str(token.get("access_token") or "").strip()
        refresh_token = str(token.get("refresh_token") or "").strip()
        if not access_token or not refresh_token:
            raise IntegrationOAuthError(
                "Google did not return durable offline access. Reconnect and approve the consent prompt."
            )
        profile_response = self.http.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        profile = _response_json(profile_response, provider_name="Google profile")
        account_label = str(profile.get("email") or profile.get("name") or "Google account")
        scopes = str(token.get("scope") or " ".join(config.scopes)).split()
        return (
            {
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "refresh_token": refresh_token,
            },
            account_label,
            scopes,
        )

    def _complete_github(
        self, *, config: ProviderConfig, code: str
    ) -> tuple[dict[str, Any], str, list[str]]:
        response = self.http.post(
            config.token_url,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code": code,
                "redirect_uri": config.redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        token = _response_json(response, provider_name=config.name)
        access_token = str(token.get("access_token") or "").strip()
        if not access_token:
            raise IntegrationOAuthError("GitHub did not return an access token.")
        profile_response = self.http.get(
            "https://api.github.com/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        profile = _response_json(profile_response, provider_name="GitHub profile")
        account_label = str(profile.get("login") or profile.get("name") or "GitHub account")
        scopes = [item.strip() for item in str(token.get("scope") or "").split(",") if item.strip()]
        return {"access_token": access_token}, account_label, scopes

    def _store_connection(
        self,
        *,
        provider: str,
        domain_key: str,
        credentials: dict[str, Any],
        account_label: str,
        scopes: list[str],
    ) -> None:
        domain = DomainRepository(self.session).get_by_key(domain_key)
        if domain is None:
            raise IntegrationOAuthError(f"Unknown domain: {domain_key}")
        connection = self.session.scalar(
            select(ToolConnection).where(
                ToolConnection.domain_id == domain.id,
                ToolConnection.tool_key == provider,
            )
        )
        display_provider = "Google Workspace" if provider == "google" else "GitHub"
        config = dict(connection.config or {}) if connection else {}
        config.update(
            {
                "oauth_credential_ciphertext": encrypt_credential_bundle(
                    credentials, settings=self.settings
                ),
                "oauth_connected_at": datetime.now(UTC).isoformat(),
                "oauth_scopes": scopes,
                "account_label": account_label,
            }
        )
        if provider == "google":
            config.setdefault("user_id", "me")
            config.setdefault("calendar_id", "primary")
        if connection is None:
            connection = ToolConnection(
                domain_id=domain.id,
                tool_key=provider,
                display_name=f"{domain.name} {display_provider}",
                auth_type="oauth",
                config=config,
                is_active=True,
            )
            self.session.add(connection)
        else:
            connection.auth_type = "oauth"
            connection.config = config
            connection.is_active = True
        if provider == "google":
            self._clear_google_source_backoff(domain_key)
        self.session.commit()

    def _clear_google_source_backoff(self, domain_key: str) -> None:
        """Let the background ingestors retry immediately after credentials change."""
        now = datetime.now(UTC).isoformat()
        for prefix in ("gmail_trigger_cursor:", "calendar_trigger_cursor:"):
            setting = self.session.get(RuntimeSetting, f"{prefix}{domain_key}")
            if setting is None:
                continue
            setting.value = {
                **dict(setting.value or {}),
                "status": "reauthorizing",
                "last_error": None,
                "error_count": 0,
                "auth_required": False,
                "next_retry_at": None,
                "credentials_updated_at": now,
            }


def _state_key(state: str) -> str:
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    return f"integration_oauth_state:{digest}"


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _response_json(response: httpx.Response, *, provider_name: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise IntegrationOAuthError(f"{provider_name} returned an invalid response.") from exc
    if response.is_error:
        detail = payload.get("error_description") or payload.get("error") or response.reason_phrase
        raise IntegrationOAuthError(f"{provider_name} authorization failed: {detail}")
    if not isinstance(payload, dict):
        raise IntegrationOAuthError(f"{provider_name} returned an invalid response.")
    return payload
