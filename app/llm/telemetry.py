"""Durable attribution for model calls made outside and inside tool execution."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import LLMCallLog


def record_llm_call(
    session: Session,
    *,
    component: str,
    client: Any,
    task_id: uuid.UUID | None = None,
    workflow_run_id: str | uuid.UUID | None = None,
    prompt_chars: int | None = None,
    prompt_sections: dict[str, int] | None = None,
    status: str = "complete",
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
    started_at: datetime | None = None,
) -> LLMCallLog:
    usage = getattr(client, "last_usage", None)
    usage = usage if isinstance(usage, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    parsed_workflow_run_id = _uuid_or_none(workflow_run_id)
    entry = LLMCallLog(
        task_id=task_id,
        workflow_run_id=parsed_workflow_run_id,
        component=component,
        provider=str(getattr(client, "provider", "") or "") or None,
        model=str(getattr(client, "model", "") or "") or None,
        status=status,
        prompt_chars=prompt_chars,
        prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
        completion_tokens=_int_or_none(usage.get("completion_tokens")),
        cached_tokens=_int_or_none(prompt_details.get("cached_tokens")),
        cost=_float_or_none(usage.get("cost")),
        response_id=str(getattr(client, "last_response_id", "") or "") or None,
        prompt_sections=prompt_sections or {},
        metadata_=metadata or {},
        error_message=error_message,
        started_at=started_at,
        completed_at=datetime.now(UTC),
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def _uuid_or_none(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
