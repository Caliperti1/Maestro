from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.integrations.credentials import CredentialEncryptionError
from app.integrations.oauth import IntegrationOAuthError, IntegrationOAuthService

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/providers")
def list_integration_providers(request: Request, db: Session = Depends(get_db)) -> dict:
    service = IntegrationOAuthService(db)
    providers = service.provider_configs(callback_base_url=_callback_base_url(request))
    encryption_ready = bool(get_settings().integration_credential_encryption_key)
    return {
        "providers": [
            {
                "key": provider.key,
                "name": provider.name,
                "configured": provider.configured and encryption_ready,
                "callback_url": provider.redirect_uri,
                "scope_count": len(provider.scopes),
                "setup_message": (
                    None
                    if provider.configured and encryption_ready
                    else _setup_message(provider.key, provider.configured, encryption_ready)
                ),
            }
            for provider in providers
        ]
    }


@router.post("/{provider}/{domain_key}/authorize")
def authorize_integration(
    provider: str,
    domain_key: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    try:
        authorization_url = IntegrationOAuthService(db).begin_authorization(
            provider=provider,
            domain_key=domain_key,
            callback_base_url=_callback_base_url(request),
        )
    except (IntegrationOAuthError, CredentialEncryptionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"authorization_url": authorization_url}


@router.get("/{provider}/callback", name="integration_oauth_callback")
def integration_oauth_callback(
    provider: str,
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if error:
        return _frontend_redirect(provider=provider, status="error", detail=error)
    if not state or not code:
        return _frontend_redirect(
            provider=provider,
            status="error",
            detail="The provider callback was missing its authorization code.",
        )
    try:
        domain_key, account_label = IntegrationOAuthService(db).complete_authorization(
            provider=provider,
            state=state,
            code=code,
            callback_base_url=_callback_base_url(request),
        )
    except (IntegrationOAuthError, CredentialEncryptionError) as exc:
        return _frontend_redirect(provider=provider, status="error", detail=str(exc))
    return _frontend_redirect(
        provider=provider,
        domain_key=domain_key,
        status="connected",
        detail=f"Connected {account_label}.",
    )


@router.delete("/{provider}/{domain_key}")
def disconnect_integration(
    provider: str,
    domain_key: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        disconnected = IntegrationOAuthService(db).disconnect(
            provider=provider,
            domain_key=domain_key,
        )
    except IntegrationOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"disconnected": disconnected, "provider": provider, "domain_key": domain_key}


def _callback_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _frontend_redirect(
    *,
    provider: str,
    status: str,
    detail: str,
    domain_key: str | None = None,
) -> RedirectResponse:
    settings = get_settings()
    query = {
        "surface": "tools",
        "integration": status,
        "provider": provider,
        "message": detail,
    }
    if domain_key:
        query["domain"] = domain_key
    destination = f"{settings.frontend_origin.rstrip('/')}?{urlencode(query)}"
    return RedirectResponse(destination, status_code=303)


def _setup_message(provider: str, provider_ready: bool, encryption_ready: bool) -> str:
    missing: list[str] = []
    if not provider_ready:
        prefix = provider.upper()
        missing.append(f"INTEGRATION_{prefix}_CLIENT_ID and CLIENT_SECRET")
    if not encryption_ready:
        missing.append("INTEGRATION_CREDENTIAL_ENCRYPTION_KEY")
    return "Deployment setup required: " + ", ".join(missing) + "."
