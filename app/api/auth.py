from __future__ import annotations

import secrets
from functools import lru_cache
from http.cookies import SimpleCookie
from urllib.parse import urlparse

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.service import OwnerSessionService, csrf_token
from app.core.config import Settings, get_settings
from app.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


def _cookie(request: Request, name: str) -> str | None:
    cookie = SimpleCookie()
    cookie.load(request.headers.get("cookie", ""))
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else None


def _safe_return_to(value: str | None, settings: Settings) -> str:
    fallback = settings.frontend_origin.rstrip("/") + "/"
    if not value:
        return fallback
    candidate = urlparse(value)
    allowed = urlparse(settings.frontend_origin)
    if candidate.scheme == allowed.scheme and candidate.netloc == allowed.netloc:
        return value
    return fallback


@lru_cache
def _oauth_client(
    issuer: str,
    client_id: str,
    client_secret: str | None,
) -> OAuth:
    oauth = OAuth()
    registration = dict(
        name="owner",
        client_id=client_id,
        server_metadata_url=issuer.rstrip("/") + "/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid email profile",
            "code_challenge_method": "S256",
            "token_endpoint_auth_method": "client_secret_basic" if client_secret else "none",
        },
    )
    if client_secret:
        registration["client_secret"] = client_secret
    oauth.register(**registration)
    return oauth


def _configured_oauth(settings: Settings):
    if not settings.owner_oidc_configured:
        raise HTTPException(status_code=503, detail="Owner OIDC is not fully configured.")
    oauth = _oauth_client(
        settings.owner_oidc_issuer or "",
        settings.owner_oidc_client_id or "",
        settings.owner_oidc_client_secret,
    )
    return oauth.create_client("owner")


@router.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if settings.owner_auth_mode == "disabled":
        return {"required": False, "authenticated": True, "csrf_token": None}
    raw_token = _cookie(request, settings.owner_session_cookie_name)
    owner = OwnerSessionService(db, settings).authenticate(raw_token)
    return {
        "required": True,
        "authenticated": owner is not None,
        "login_url": "/auth/login",
        "expires_at": owner.expires_at if owner is not None else None,
        "csrf_token": csrf_token(settings, raw_token) if owner is not None and raw_token else None,
    }


@router.get("/login")
async def login(request: Request, return_to: str | None = None):
    settings = get_settings()
    if settings.owner_auth_mode == "disabled":
        return RedirectResponse(_safe_return_to(return_to, settings), status_code=303)
    client = _configured_oauth(settings)
    request.session["owner_return_to"] = _safe_return_to(return_to, settings)
    return await client.authorize_redirect(
        request,
        settings.owner_oidc_redirect_uri,
        nonce=secrets.token_urlsafe(32),
    )


@router.get("/callback")
async def callback(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    client = _configured_oauth(settings)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=401, detail="OIDC authorization failed.") from exc
    userinfo = token.get("userinfo") or {}
    subject = str(userinfo.get("sub") or "")
    if not subject or subject != settings.owner_oidc_subject:
        raise HTTPException(status_code=403, detail="This identity is not the Maestro owner.")
    record, raw_token = OwnerSessionService(db, settings).create(
        issuer=settings.owner_oidc_issuer or "",
        subject=subject,
        metadata={
            "email": str(userinfo.get("email") or ""),
            "email_verified": bool(userinfo.get("email_verified")),
        },
    )
    destination = str(request.session.pop("owner_return_to", settings.frontend_origin))
    request.session.clear()
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        settings.owner_session_cookie_name,
        raw_token,
        max_age=settings.owner_session_ttl_seconds,
        expires=record.expires_at,
        path="/",
        secure=settings.owner_cookie_secure,
        httponly=True,
        samesite=settings.owner_cookie_samesite,
    )
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    settings = get_settings()
    raw_token = _cookie(request, settings.owner_session_cookie_name)
    OwnerSessionService(db, settings).revoke(raw_token)
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(settings.owner_session_cookie_name, path="/")
    return response
