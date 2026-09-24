import asyncio

import pytest
from sqlalchemy import create_engine

from app.core.config import Settings
from app.operations import worker
from app.operations import deployment_check
from app.operations.deployment_check import deployment_findings
from app.operations.readiness import check_readiness


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_render_postgres_url_uses_psycopg_driver() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://maestro:secret@internal/maestro",
    )

    assert settings.database_url == "postgresql+psycopg://maestro:secret@internal/maestro"


def test_scheduler_cycle_uses_standalone_worker_owner(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeWorker:
        def __init__(self, session):
            assert isinstance(session, _FakeSession)

        def run_once(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setenv("MAESTRO_WORKER_OWNER", "render-worker-1")
    monkeypatch.setattr(worker, "SessionLocal", _FakeSession)
    monkeypatch.setattr(
        worker,
        "scheduler_worker_settings",
        lambda session: {
            "enabled": True,
            "interval_seconds": 19,
            "claim_limit": 2,
            "execute_llm": True,
            "auto_tool_loop": False,
        },
    )
    monkeypatch.setattr(worker, "SchedulerWorkerService", _FakeWorker)

    assert worker.process_scheduler_once() == 19
    assert calls == [
        {
            "owner": "render-worker-1",
            "claim_limit": 2,
            "execute_llm": True,
            "auto_tool_loop": False,
        }
    ]


def test_cycle_loop_stops_without_waiting_for_next_interval() -> None:
    calls: list[str] = []

    async def exercise() -> None:
        stop_event = asyncio.Event()

        def cycle() -> int:
            calls.append("ran")
            stop_event.set()
            return 300

        await worker.run_cycle_loop(
            name="test",
            cycle=cycle,
            stop_event=stop_event,
            minimum_interval_seconds=5,
            initial_interval_seconds=5,
        )

    asyncio.run(exercise())
    assert calls == ["ran"]


def test_readiness_checks_database_connection() -> None:
    result = check_readiness(create_engine("sqlite+pysqlite:///:memory:"))

    assert result.ready is True
    assert result.database == "available"


def test_deployment_check_accepts_cloud_endpoints_and_reports_release_gates() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://maestro:secret@internal/maestro",
        frontend_origin="https://maestro.example.com",
        cors_allow_origins="https://maestro.example.com",
        openrouter_api_key="configured",
        embedding_provider="openai",
        openai_api_key="configured",
    )

    findings = deployment_findings(settings)

    assert not [finding for finding in findings if finding.level == "error"]
    assert any(finding.key == "OWNER_AUTH_MODE" and finding.level == "gate" for finding in findings)
    assert any(finding.key == "ARTIFACT_STORE" and finding.level == "gate" for finding in findings)


def test_deployment_check_accepts_complete_production_security_configuration() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://maestro:secret@internal/maestro",
        frontend_origin="https://maestro.example.com",
        cors_allow_origins="https://maestro.example.com",
        openrouter_api_key="configured",
        embedding_provider="openai",
        openai_api_key="configured",
        owner_auth_mode="oidc",
        owner_oidc_issuer="https://issuer.example.com",
        owner_oidc_client_id="client-id",
        owner_oidc_client_secret="client-secret",
        owner_oidc_subject="owner-subject",
        owner_oidc_redirect_uri="https://api.maestro.example.com/auth/callback",
        owner_session_secret="x" * 32,
        owner_cookie_secure=True,
        owner_cookie_samesite="none",
        artifact_store_backend="s3",
        artifact_store_s3_bucket="maestro-private",
        artifact_store_s3_region="us-west-2",
    )

    assert deployment_findings(settings) == []


def test_deployment_check_refuses_promotion_while_auth_gate_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://maestro:secret@internal/maestro",
        frontend_origin="https://maestro.example.com",
        cors_allow_origins="https://maestro.example.com",
        openrouter_api_key="configured",
        embedding_provider="openai",
        openai_api_key="configured",
    )
    monkeypatch.setattr(deployment_check, "get_settings", lambda: settings)

    with pytest.raises(SystemExit) as exc_info:
        deployment_check.main()

    assert exc_info.value.code == 1
