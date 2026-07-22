from app.api import main


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_scheduler_heartbeat_uses_runtime_settings(monkeypatch) -> None:
    calls: list[dict] = []
    worker_settings = {
        "enabled": True,
        "interval_seconds": 17,
        "claim_limit": 3,
        "execute_llm": True,
        "auto_tool_loop": True,
    }

    class _FakeWorker:
        def __init__(self, session):
            assert isinstance(session, _FakeSession)

        def run_once(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(main, "SessionLocal", _FakeSession)
    monkeypatch.setattr(main, "scheduler_worker_settings", lambda session: worker_settings)
    monkeypatch.setattr(main, "SchedulerWorkerService", _FakeWorker)

    assert main._process_scheduler_work_once() == 17
    assert calls == [
        {
            "owner": "maestro-background-worker",
            "claim_limit": 3,
            "execute_llm": True,
            "auto_tool_loop": True,
        }
    ]


def test_disabled_scheduler_heartbeat_does_not_claim_work(monkeypatch) -> None:
    monkeypatch.setattr(main, "SessionLocal", _FakeSession)
    monkeypatch.setattr(
        main,
        "scheduler_worker_settings",
        lambda session: {
            "enabled": False,
            "interval_seconds": 31,
            "claim_limit": 4,
            "execute_llm": True,
            "auto_tool_loop": True,
        },
    )
    monkeypatch.setattr(
        main,
        "SchedulerWorkerService",
        lambda session: (_ for _ in ()).throw(AssertionError("worker should remain idle")),
    )

    assert main._process_scheduler_work_once() == 31


def test_gmail_trigger_heartbeat_uses_runtime_settings(monkeypatch) -> None:
    calls: list[dict] = []
    worker_settings = {
        "enabled": True,
        "interval_seconds": 23,
        "page_size": 75,
    }

    class _FakeTrigger:
        def __init__(self, session):
            assert isinstance(session, _FakeSession)

        def poll_once(self, **kwargs):
            calls.append(kwargs)
            return {"emitted_count": 0}

    monkeypatch.setattr(main, "SessionLocal", _FakeSession)
    monkeypatch.setattr(main, "gmail_trigger_worker_settings", lambda session: worker_settings)
    monkeypatch.setattr(main, "GmailTriggerService", _FakeTrigger)

    assert main._process_gmail_triggers_once() == 23
    assert calls == [{"page_size": 75}]


def test_disabled_gmail_trigger_heartbeat_does_not_poll(monkeypatch) -> None:
    monkeypatch.setattr(main, "SessionLocal", _FakeSession)
    monkeypatch.setattr(
        main,
        "gmail_trigger_worker_settings",
        lambda session: {"enabled": False, "interval_seconds": 41, "page_size": 100},
    )
    monkeypatch.setattr(
        main,
        "GmailTriggerService",
        lambda session: (_ for _ in ()).throw(AssertionError("trigger should remain idle")),
    )

    assert main._process_gmail_triggers_once() == 41
