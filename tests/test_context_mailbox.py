from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import IngestionRecord, RuntimeSetting
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.memory.context_gateway import ContextGatewayService
from app.memory.context_mailbox import (
    ContextHandoffError,
    ContextMailboxError,
    ContextMailboxService,
    parse_context_handoff,
)
from app.memory.ingestion import envelope_for_file, strip_context_envelope


def _body(*, source_id: str = "chatgpt-conversation-123", note: str = "Initial note.") -> str:
    return f"""source_system: chatgpt
source_id: {source_id}
source_timestamp: 2026-08-15T14:00:00-04:00
domain: perti

# CAD workflow discussion

{note}
"""


def _message(
    message_id: str = "gmail-1",
    *,
    sender: str = "Chris Aliperti <chris@perti.io>",
    body: str | None = None,
    subject: str = "[MAESTRO-CONTEXT][chatgpt][PERTI] CAD workflow discussion",
) -> dict:
    return {
        "message_id": message_id,
        "thread_id": "thread-1",
        "label_ids": ["INBOX", "UNREAD"],
        "subject": subject,
        "from": sender,
        "date": "Sat, 15 Aug 2026 14:00:00 -0400",
        "internal_date": "1786816800000",
        "body_text": body if body is not None else _body(),
        "attachments": [],
    }


class FakeMailboxSource:
    def __init__(self, messages: list[dict], *, profile_email: str = "maestro@perti.io"):
        self.messages = {str(message["message_id"]): deepcopy(message) for message in messages}
        self.profile_email = profile_email
        self.label_actions: list[dict] = []
        self.attachment_data: dict[tuple[str, str], bytes] = {}

    def profile(self) -> dict:
        return {"emailAddress": self.profile_email}

    def ensure_labels(self, names: list[str]) -> dict[str, str]:
        return {name: f"label-{index}" for index, name in enumerate(names, start=1)}

    def inbox_message_ids(self, *, page_size: int) -> list[str]:
        return list(self.messages)[:page_size]

    def message(self, message_id: str) -> dict:
        return deepcopy(self.messages[message_id])

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        return self.attachment_data[(message_id, attachment_id)]

    def label_message(
        self,
        message_id: str,
        *,
        add_label_ids: list[str],
        remove_label_ids: list[str],
    ) -> None:
        self.label_actions.append(
            {
                "message_id": message_id,
                "add": add_label_ids,
                "remove": remove_label_ids,
            }
        )


def _settings(tmp_path):
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    settings.maestro_intake_email = "maestro@perti.io"
    settings.maestro_intake_google_client_id = "client"
    settings.maestro_intake_google_client_secret = "secret"
    settings.maestro_intake_google_refresh_token = "refresh"
    settings.maestro_intake_allowed_senders = "chris@perti.io, approved@example.com"
    return settings


def _service(session, tmp_path, source):
    seed_default_domains(session)
    settings = _settings(tmp_path)
    return ContextMailboxService(
        session,
        source=source,
        settings=settings,
        gateway=ContextGatewayService(session, root=tmp_path),
    )


def test_parse_context_handoff_normalizes_source_and_domain() -> None:
    handoff = parse_context_handoff(_message())

    assert handoff.source_system == "chatgpt"
    assert handoff.source_id == "chatgpt-conversation-123"
    assert handoff.domain_key == "perti-laboratories"
    assert handoff.title == "CAD workflow discussion"
    assert handoff.source_timestamp == datetime(2026, 8, 15, 18, 0, tzinfo=UTC)


def test_valid_handoff_stages_with_original_provenance(session, tmp_path) -> None:
    source = FakeMailboxSource([_message()])
    result = _service(session, tmp_path, source).poll_once()

    assert result["counts"]["staged"] == 1
    staged = list((tmp_path / "perti-laboratories" / "inbox").glob("*.md"))
    assert len(staged) == 1
    envelope = envelope_for_file(staged[0], domain_key="perti-laboratories")
    assert envelope.source_system == "chatgpt"
    assert envelope.external_id == "chatgpt-conversation-123"
    assert envelope.policy.transfer_method == "context_mailbox"
    assert envelope.metadata["gmail_message_id"] == "gmail-1"
    assert "Initial note." in strip_context_envelope(staged[0].read_text())
    record = session.scalar(select(IngestionRecord))
    assert record is not None and record.status == "staged"
    assert source.label_actions[0]["remove"] == ["UNREAD", "INBOX"]


def test_resend_is_duplicate_but_changed_source_version_is_staged(session, tmp_path) -> None:
    source = FakeMailboxSource([_message("gmail-1")])
    service = _service(session, tmp_path, source)
    first = service.poll_once()
    source.messages = {"gmail-2": _message("gmail-2")}
    duplicate = service.poll_once()
    source.messages = {
        "gmail-3": _message("gmail-3", body=_body(note="Corrected context note."))
    }
    changed = service.poll_once()

    assert first["counts"]["staged"] == 1
    assert duplicate["counts"]["duplicate"] == 1
    assert changed["counts"]["staged"] == 1
    assert len(session.scalars(select(IngestionRecord)).all()) == 2
    assert len(list((tmp_path / "perti-laboratories" / "inbox").glob("*.md"))) == 2


def test_unapproved_sender_and_malformed_handoff_are_quarantined(session, tmp_path) -> None:
    source = FakeMailboxSource(
        [
            _message("gmail-bad-sender", sender="Unknown <unknown@example.com>"),
            _message("gmail-bad-format", subject="ChatGPT notes"),
        ]
    )
    result = _service(session, tmp_path, source).poll_once()

    assert result["counts"]["quarantined"] == 2
    assert session.scalars(select(IngestionRecord)).all() == []
    assert len(source.label_actions) == 2
    assert all(action["remove"] == ["UNREAD"] for action in source.label_actions)


def test_supported_attachment_is_archived_extracted_and_hashed(session, tmp_path) -> None:
    message = _message()
    message["attachments"] = [
        {"attachment_id": "attachment-1", "filename": "notes.md", "mime_type": "text/markdown"}
    ]
    source = FakeMailboxSource([message])
    source.attachment_data[("gmail-1", "attachment-1")] = (
        b"# Attachment context\nDecision: proceed."
    )

    result = _service(session, tmp_path, source).poll_once()

    assert result["counts"]["staged"] == 1
    staged = next((tmp_path / "perti-laboratories" / "inbox").glob("*.md"))
    content = strip_context_envelope(staged.read_text())
    assert "## Attachment: notes.md" in content
    assert "Decision: proceed." in content
    envelope = envelope_for_file(staged, domain_key="perti-laboratories")
    attachment = envelope.metadata["attachments"][0]
    assert attachment["filename"] == "notes.md"
    assert len(attachment["sha256"]) == 64


def test_profile_must_match_configured_mailbox(session, tmp_path) -> None:
    service = _service(
        session,
        tmp_path,
        FakeMailboxSource([], profile_email="different@perti.io"),
    )

    with pytest.raises(ContextMailboxError, match="different@perti.io"):
        service.poll_once()

    status = session.get(RuntimeSetting, "context_mailbox_worker")
    assert status is not None
    assert status.value["status"] == "error"


def test_sanitized_context_requires_review_metadata() -> None:
    message = _message(
        subject="[MAESTRO-CONTEXT][usma_sanitized_context_drop][USMA] Daily context",
        body="""source_system: usma_sanitized_context_drop
source_id: usma-daily-1
domain: usma
review_status: reviewed
reviewed_by: Chris Aliperti
reviewed_at: 2026-08-15T17:00:00-04:00
contains_restricted: false

# Daily context
Prepare Lesson 6.
""",
    )
    assert parse_context_handoff(message).domain_key == "usma"

    with pytest.raises(ContextHandoffError, match="reviewed"):
        parse_context_handoff(
            _message(
                subject=message["subject"],
                body=message["body_text"].replace("review_status: reviewed\n", ""),
            )
        )
