"""Typed decision contract for durable email triage."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EmailClassification = Literal[
    "spam",
    "noise",
    "useful_information",
    "response_required",
    "action_required",
]
EmailRouteType = Literal["contact", "todo", "event", "organization"]


class EmailMetadataEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value_json: str


class EmailRoutedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_type: EmailRouteType
    title: str
    content: str
    metadata: list[EmailMetadataEntry] = Field(max_length=30)
    rationale: str


class EmailNotificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_notify: bool
    title: str
    message: str
    severity: Literal["info", "warning", "urgent"]
    reason: str


class EmailLinkedDocumentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    kind: str
    title: str
    access_status: Literal["read", "inaccessible", "not_read", "irrelevant"]
    summary: str


class EmailTriageDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    message_id: str
    thread_id: str
    subject: str
    sender: str
    classification: EmailClassification
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    conversation: str
    requires_chris_response: bool
    memory_worthy: bool
    memory_summary: str
    notification: EmailNotificationDecision
    routed_candidates: list[EmailRoutedCandidate] = Field(max_length=20)
    linked_documents: list[EmailLinkedDocumentDecision] = Field(max_length=20)
    read_state_action: Literal["none", "mark_read"]
    rationale: str


def decision_tool_requests(decision: EmailTriageDecision) -> list[dict[str, Any]]:
    source = {
        "type": "gmail_message",
        "message_id": decision.message_id,
        "thread_id": decision.thread_id,
        "subject": decision.subject,
        "from": decision.sender,
    }
    requests = [
        {
            "tool_key": "routed.item.create",
            "payload": {
                "route_type": candidate.route_type,
                "title": candidate.title,
                "content": candidate.content,
                "metadata": {
                    entry.key: _metadata_value(entry.value_json)
                    for entry in candidate.metadata
                },
                "source_refs": [source],
                "message_id": decision.message_id,
                "thread_id": decision.thread_id,
                "subject": decision.subject,
                "from": decision.sender,
            },
            "rationale": candidate.rationale,
        }
        for candidate in decision.routed_candidates
    ]
    if decision.notification.should_notify:
        requests.append(
            {
                "tool_key": "workflow.notification.create",
                "payload": {
                    "title": decision.notification.title,
                    "message": decision.notification.message,
                    "severity": decision.notification.severity,
                    "reason": decision.notification.reason,
                    "source": source,
                },
                "rationale": decision.notification.reason,
            }
        )
    if decision.read_state_action == "mark_read":
        requests.append(
            {
                "tool_key": "gmail.message.modify",
                "payload": {
                    "message_id": decision.message_id,
                    "remove_label_ids": ["UNREAD"],
                },
                "rationale": "Triage is complete; remove only the Gmail UNREAD label.",
            }
        )
    return requests


def _metadata_value(value_json: str) -> Any:
    try:
        return json.loads(value_json)
    except json.JSONDecodeError:
        return value_json
