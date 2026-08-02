from app.agents.email_triage import EmailTriageDecision, decision_tool_requests


def _decision(**overrides):
    payload = {
        "schema_version": "1.0",
        "message_id": "msg-1",
        "thread_id": "thread-1",
        "subject": "Partner update",
        "sender": "Jane Smith <jane@example.com>",
        "classification": "action_required",
        "confidence": 0.94,
        "summary": "Jane asked Chris to send the revised brief by Friday.",
        "conversation": "Chris, Jane asked you to send the revised brief by Friday.",
        "requires_chris_response": True,
        "memory_worthy": True,
        "memory_summary": "Jane Smith requested the revised partner brief by Friday.",
        "notification": {
            "should_notify": True,
            "title": "Partner brief due Friday",
            "message": "Jane asked you to send the revised brief by Friday.",
            "severity": "warning",
            "reason": "Chris owns a concrete deadline.",
        },
        "routed_candidates": [{
            "route_type": "todo",
            "title": "Send Jane the revised brief",
            "content": "Send the revised partner brief to Jane by Friday.",
            "metadata": [
                {"key": "owner", "value_json": '"Chris Aliperti"'},
                {"key": "due_text", "value_json": '"Friday"'},
            ],
            "rationale": "The email assigns Chris a concrete follow-up.",
        }],
        "linked_documents": [],
        "read_state_action": "mark_read",
        "rationale": "The request has a clear owner and deadline.",
    }
    payload.update(overrides)
    return EmailTriageDecision.model_validate(payload)


def test_email_triage_schema_requires_every_object_property() -> None:
    schema = EmailTriageDecision.model_json_schema()

    def assert_strict_object(node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert set(node.get("required", [])) == set(node.get("properties", {}))
        for value in node.values():
            if isinstance(value, dict):
                assert_strict_object(value)
            elif isinstance(value, list):
                for item in value:
                    assert_strict_object(item)

    assert_strict_object(schema)


def test_email_triage_decision_maps_to_constrained_operational_actions() -> None:
    requests = decision_tool_requests(_decision())

    assert [request["tool_key"] for request in requests] == [
        "routed.item.create",
        "workflow.notification.create",
        "gmail.message.modify",
    ]
    assert requests[0]["payload"]["source_refs"][0]["message_id"] == "msg-1"
    assert requests[-1]["payload"] == {
        "message_id": "msg-1",
        "remove_label_ids": ["UNREAD"],
    }


def test_silent_email_decision_does_not_create_notification_or_read_mutation() -> None:
    decision = _decision(
        classification="useful_information",
        requires_chris_response=False,
        notification={
            "should_notify": False,
            "title": "",
            "message": "",
            "severity": "info",
            "reason": "No interruption is warranted.",
        },
        routed_candidates=[],
        read_state_action="none",
    )

    assert decision_tool_requests(decision) == []
