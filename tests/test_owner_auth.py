from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app.auth.middleware import OwnerAuthMiddleware
from app.auth.service import OwnerSessionService, csrf_token
from app.core.config import Settings, get_settings
from app.db.models import Base, OwnerSession


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "owner_auth_mode": "oidc",
        "owner_oidc_issuer": "https://issuer.example.com",
        "owner_oidc_client_id": "client-id",
        "owner_oidc_client_secret": "client-secret",
        "owner_oidc_subject": "owner-subject",
        "owner_oidc_redirect_uri": "https://api.example.com/auth/callback",
        "owner_session_secret": "s" * 32,
        "owner_cookie_secure": False,
        "frontend_origin": "https://maestro.example.com",
        "cors_allow_origins": "https://maestro.example.com",
    }
    values.update(overrides)
    return Settings(**values)


def _protected_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(OwnerAuthMiddleware)

    @app.get("/protected")
    def protected_get():
        return {"ok": True}

    @app.post("/protected")
    def protected_post():
        return {"ok": True}

    @app.websocket("/socket")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    return app


def test_owner_session_is_opaque_revocable_and_expirable() -> None:
    factory = _session_factory()
    settings = _settings()
    with factory() as db:
        service = OwnerSessionService(db, settings)
        record, raw_token = service.create(
            issuer=settings.owner_oidc_issuer or "",
            subject=settings.owner_oidc_subject or "",
        )

        assert raw_token.startswith(f"mos_{record.id}_")
        assert raw_token not in record.token_hash
        assert service.authenticate(raw_token) is not None

        service.revoke(raw_token)
        assert service.authenticate(raw_token) is None

        expired, expired_token = service.create(issuer="issuer", subject="subject")
        expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        assert service.authenticate(expired_token) is None


def test_owner_middleware_requires_session_and_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _session_factory()
    settings = _settings()
    monkeypatch.setattr("app.auth.middleware.SessionLocal", factory)
    monkeypatch.setattr("app.auth.middleware.get_settings", lambda: settings)

    with factory() as db:
        _, raw_token = OwnerSessionService(db, settings).create(
            issuer=settings.owner_oidc_issuer or "",
            subject=settings.owner_oidc_subject or "",
        )

    client = TestClient(_protected_app())
    assert client.get("/protected").status_code == 401

    client.cookies.set(settings.owner_session_cookie_name, raw_token)
    assert client.get("/protected").status_code == 200
    assert client.post("/protected").status_code == 403
    assert client.post(
        "/protected",
        headers={"X-CSRF-Token": csrf_token(settings, raw_token)},
    ).status_code == 200


def test_owner_middleware_checks_websocket_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _session_factory()
    settings = _settings()
    monkeypatch.setattr("app.auth.middleware.SessionLocal", factory)
    monkeypatch.setattr("app.auth.middleware.get_settings", lambda: settings)
    with factory() as db:
        _, raw_token = OwnerSessionService(db, settings).create(
            issuer=settings.owner_oidc_issuer or "",
            subject=settings.owner_oidc_subject or "",
        )

    client = TestClient(_protected_app())
    client.cookies.set(settings.owner_session_cookie_name, raw_token)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/socket", headers={"origin": "https://evil.example"}):
            pass
    assert exc_info.value.code == 4403

    with client.websocket_connect(
        "/socket",
        headers={"origin": "https://maestro.example.com"},
    ) as socket:
        assert socket.receive_text() == "ok"


def test_owner_middleware_is_noop_when_auth_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.auth.middleware.get_settings",
        lambda: _settings(owner_auth_mode="disabled"),
    )
    assert TestClient(_protected_app()).post("/protected").status_code == 200


def test_revoke_expired_marks_sessions() -> None:
    factory = _session_factory()
    settings = _settings()
    with factory() as db:
        service = OwnerSessionService(db, settings)
        record, _ = service.create(issuer="issuer", subject="subject")
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

        assert service.revoke_expired() == 1
        assert db.get(OwnerSession, record.id).revoked_at is not None
