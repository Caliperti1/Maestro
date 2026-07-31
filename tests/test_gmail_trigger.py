from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Domain,
    Message,
    RuntimeSetting,
    ToolConnection,
    WorkflowNotification,
    WorkflowQueueItem,
    WorkflowRun,
)
from app.db.seed import seed_default_domains
from app.maestro.gmail_trigger import (
    GMAIL_TRIGGER_CURSOR_PREFIX,
    GmailHistoryCursorExpired,
    GmailMessageUnavailable,
    GmailTriggerService,
)
from app.maestro.scheduler import SchedulerService


class FakeGmailHistorySource:
    def __init__(self) -> None:
        self.profile_history_id = "100"
        self.history_response: dict[str, Any] = {
            "historyId": "105",
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "msg-inbox"}},
                        {"message": {"id": "msg-sent"}},
                        {"message": {"id": "msg-inbox"}},
                    ]
                }
            ],
        }
        self.history_calls: list[dict[str, Any]] = []

    def profile(self, connection: ToolConnection) -> dict[str, Any]:
        return {
            "emailAddress": "chris.aliperti@praxis-defense.com",
            "historyId": self.profile_history_id,
        }

    def history_page(
        self,
        connection: ToolConnection,
        *,
        start_history_id: str,
        page_token: str | None,
        page_size: int,
    ) -> dict[str, Any]:
        self.history_calls.append(
            {
                "start_history_id": start_history_id,
                "page_token": page_token,
                "page_size": page_size,
            }
        )
        return self.history_response

    def message_metadata(
        self,
        connection: ToolConnection,
        *,
        message_id: str,
    ) -> dict[str, Any]:
        labels = ["INBOX", "UNREAD"] if message_id == "msg-inbox" else ["SENT"]
        return {
            "message_id": message_id,
            "thread_id": f"thread-{message_id}",
            "label_ids": labels,
            "subject": "Partner update",
            "from": "Partner <partner@example.com>",
            "to": "Chris <chris.aliperti@praxis-defense.com>",
            "date": "Tue, 21 Jul 2026 09:00:00 -0400",
            "internal_date": "1784638800000",
        }


class ExpiredGmailHistorySource(FakeGmailHistorySource):
    def history_page(self, *args, **kwargs) -> dict[str, Any]:
        raise GmailHistoryCursorExpired("Stored Gmail history cursor expired.")


class FailingGmailHistorySource(FakeGmailHistorySource):
    def history_page(self, *args, **kwargs) -> dict[str, Any]:
        raise RuntimeError("Gmail is temporarily unavailable.")


class MissingMessageGmailHistorySource(FakeGmailHistorySource):
    def message_metadata(
        self,
        connection: ToolConnection,
        *,
        message_id: str,
    ) -> dict[str, Any]:
        if message_id == "msg-inbox":
            raise GmailMessageUnavailable("Gmail message msg-inbox is no longer available.")
        return super().message_metadata(connection, message_id=message_id)


def _seed_trigger(session: Session) -> Domain:
    seed_default_domains(session)
    domain = session.scalar(select(Domain).where(Domain.key == "praxis"))
    assert domain is not None
    session.add(
        ToolConnection(
            domain_id=domain.id,
            tool_key="google",
            display_name="Praxis Google Workspace",
            auth_type="oauth",
            config={"user_id": "me", "access_token": "fake"},
            is_active=True,
        )
    )
    session.commit()
    SchedulerService(session).upsert_definition(
        key="praxis-email-triage",
        name="Praxis Email Triage",
        domain_id=domain.id,
        trigger_type="event",
        trigger_config={
            "event_type": "gmail.message.received",
            "filters": {"domain_key": "praxis"},
        },
        workflow_spec={
            "queue_items": [
                {
                    "id": "triage",
                    "objective": "Triage the exact Gmail message in the trigger event.",
                    "domain_key": "praxis",
                    "agent_key": "praxis-email-agent",
                    "required_tools": ["gmail.message.get"],
                    "max_attempts": 3,
                }
            ]
        },
        fairness_group="praxis",
    )
    return domain


def test_gmail_trigger_bootstraps_then_emits_exact_inbox_message_once(
    session: Session,
) -> None:
    domain = _seed_trigger(session)
    source = FakeGmailHistorySource()
    service = GmailTriggerService(session, source=source)

    initialized = service.poll_once(page_size=25)

    assert initialized["emitted_count"] == 0
    assert initialized["domains"][0]["status"] == "initialized"
    assert source.history_calls == []

    emitted = service.poll_once(page_size=25)

    assert emitted["emitted_count"] == 1
    assert emitted["domains"][0]["seen_count"] == 3
    assert emitted["domains"][0]["skipped_count"] == 1
    assert source.history_calls == [
        {"start_history_id": "100", "page_token": None, "page_size": 25}
    ]
    runs = session.scalars(select(WorkflowRun)).all()
    assert len(runs) == 1
    event = runs[0].input_payload["event"]
    assert event["event_id"] == "praxis:msg-inbox"
    assert event["payload"]["message_id"] == "msg-inbox"
    assert event["payload"]["domain_key"] == "praxis"
    queue_item = session.scalar(
        select(WorkflowQueueItem).where(WorkflowQueueItem.workflow_run_id == runs[0].id)
    )
    assert queue_item is not None
    assert queue_item.max_attempts == 3

    cursor = session.get(RuntimeSetting, f"{GMAIL_TRIGGER_CURSOR_PREFIX}{domain.key}")
    assert cursor is not None
    assert cursor.value["history_id"] == "105"
    cursor.value = {**cursor.value, "history_id": "100"}
    session.commit()

    duplicate = service.poll_once(page_size=25)

    assert duplicate["emitted_count"] == 1
    assert len(session.scalars(select(WorkflowRun)).all()) == 1


def test_gmail_trigger_emits_two_messages_as_independent_idempotent_runs(
    session: Session,
) -> None:
    _seed_trigger(session)
    source = FakeGmailHistorySource()
    source.history_response = {
        "historyId": "106",
        "history": [{
            "messagesAdded": [
                {"message": {"id": "msg-first"}},
                {"message": {"id": "msg-second"}},
            ]
        }],
    }
    source.message_metadata = lambda connection, message_id: {
        "message_id": message_id,
        "thread_id": f"thread-{message_id}",
        "label_ids": ["INBOX", "UNREAD"],
        "subject": f"Partner update {message_id}",
        "from": "Partner <partner@example.com>",
        "to": "Chris <chris.aliperti@praxis-defense.com>",
        "date": "Tue, 21 Jul 2026 09:00:00 -0400",
        "internal_date": "1784638800000",
    }
    service = GmailTriggerService(session, source=source)
    service.poll_once(page_size=25)

    emitted = service.poll_once(page_size=25)

    assert emitted["emitted_count"] == 2
    runs = session.scalars(select(WorkflowRun).order_by(WorkflowRun.created_at)).all()
    assert len(runs) == 2
    assert {
        run.input_payload["event"]["payload"]["message_id"] for run in runs
    } == {"msg-first", "msg-second"}
    assert {run.input_payload["event"]["event_id"] for run in runs} == {
        "praxis:msg-first",
        "praxis:msg-second",
    }

    cursor = session.get(RuntimeSetting, f"{GMAIL_TRIGGER_CURSOR_PREFIX}praxis")
    cursor.value = {**cursor.value, "history_id": "100"}
    session.commit()
    replayed = service.poll_once(page_size=25)

    assert replayed["emitted_count"] == 2
    assert len(session.scalars(select(WorkflowRun)).all()) == 2


def test_gmail_trigger_resets_expired_cursor_without_emitting_old_mail(
    session: Session,
) -> None:
    domain = _seed_trigger(session)
    cursor = RuntimeSetting(
        key=f"{GMAIL_TRIGGER_CURSOR_PREFIX}{domain.key}",
        value={"domain_key": domain.key, "history_id": "old-history", "status": "healthy"},
    )
    session.add(cursor)
    session.commit()
    source = ExpiredGmailHistorySource()
    source.profile_history_id = "current-history"

    result = GmailTriggerService(session, source=source).poll_once()

    assert result["emitted_count"] == 0
    assert result["domains"][0]["status"] == "cursor_reset"
    assert "expired" in result["domains"][0]["warning"]
    session.refresh(cursor)
    assert cursor.value["history_id"] == "current-history"
    assert cursor.value["status"] == "cursor_reset"
    assert cursor.value["error_count"] == 0
    assert session.scalars(select(WorkflowRun)).all() == []


def test_gmail_trigger_skips_missing_message_and_advances_cursor(
    session: Session,
) -> None:
    domain = _seed_trigger(session)
    source = MissingMessageGmailHistorySource()
    service = GmailTriggerService(session, source=source)
    service.poll_once(page_size=25)

    result = service.poll_once(page_size=25)

    assert result["emitted_count"] == 0
    assert result["domains"][0]["status"] == "healthy"
    assert result["domains"][0]["missing_count"] == 1
    assert result["domains"][0]["missing"] == [
        {
            "message_id": "msg-inbox",
            "reason": "Gmail message msg-inbox is no longer available.",
        }
    ]
    cursor = session.get(RuntimeSetting, f"{GMAIL_TRIGGER_CURSOR_PREFIX}{domain.key}")
    assert cursor is not None
    assert cursor.value["history_id"] == "105"
    assert cursor.value["last_missing_count"] == 1
    assert cursor.value["error_count"] == 0
    assert session.scalars(select(WorkflowRun)).all() == []
    assert session.scalars(select(WorkflowNotification)).all() == []


def test_gmail_trigger_ignores_domains_without_active_email_definition(
    session: Session,
) -> None:
    seed_default_domains(session)

    result = GmailTriggerService(session, source=FakeGmailHistorySource()).poll_once()

    assert result == {
        "event_type": "gmail.message.received",
        "domain_count": 0,
        "emitted_count": 0,
        "domains": [],
    }


def test_gmail_trigger_surfaces_persistent_poll_failure_in_maestro_channel(
    session: Session,
) -> None:
    domain = _seed_trigger(session)
    session.add(
        RuntimeSetting(
            key=f"{GMAIL_TRIGGER_CURSOR_PREFIX}{domain.key}",
            value={"domain_key": domain.key, "history_id": "100", "status": "healthy"},
        )
    )
    session.commit()
    service = GmailTriggerService(session, source=FailingGmailHistorySource())

    first = service.poll_once()
    second = service.poll_once()
    third = service.poll_once()

    assert first["domains"][0]["error_count"] == 1
    assert second["domains"][0]["error_count"] == 2
    assert third["domains"][0]["error_count"] == 3
    notification = session.scalar(select(WorkflowNotification))
    assert notification is not None
    assert notification.notification_type == "trigger_health"
    assert notification.status == "delivered"
    channel_message = next(
        (
            message
            for message in session.scalars(select(Message)).all()
            if (message.metadata_ or {}).get("source") == "gmail_trigger_worker"
        ),
        None,
    )
    assert channel_message is not None
    assert "failed three times" in channel_message.content
