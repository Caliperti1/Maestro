"""Todo duration estimation and calendar projection synchronization."""

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import CalendarEvent, Domain, Todo
from app.llm.client import LLMClient, LLMClientError, OllamaLLMClient
from app.prompts import load_prompt


class TodoSchedulingService:
    def __init__(self, session: Session, *, estimator: LLMClient | None = None):
        self.session = session
        self.estimator = estimator

    def estimate_minutes(self, todo: Todo) -> int:
        if todo.estimated_minutes:
            return _bounded_minutes(todo.estimated_minutes)
        try:
            client = self.estimator or OllamaLLMClient(
                model=get_settings().routed_resolver_llm_model,
                base_url=get_settings().routed_resolver_llm_base_url,
                timeout_seconds=get_settings().routed_resolver_llm_timeout_seconds,
            )
            response = client.structured_response(
                instructions=load_prompt("todo_duration_estimation.md"),
                input_text=(
                    f"Title: {todo.title}\nDescription: {todo.description}\n"
                    f"Domain: {self._domain_key(todo.domain_id) or 'global'}\nDue: {todo.due_at or 'none'}"
                ),
                schema_name="todo_duration_estimate",
                schema={
                    "type": "object",
                    "properties": {
                        "estimated_minutes": {"type": "integer", "minimum": 5, "maximum": 480}
                    },
                    "required": ["estimated_minutes"],
                    "additionalProperties": False,
                },
            )
            return _bounded_minutes(response.get("estimated_minutes"))
        except (LLMClientError, OSError, TypeError, ValueError):
            return _fallback_minutes(todo)

    def sync_projection(self, todo: Todo, *, commit: bool = True) -> CalendarEvent | None:
        event = self.session.scalar(select(CalendarEvent).where(CalendarEvent.todo_id == todo.id))
        if todo.scheduled_start_at is None:
            if event is not None:
                self.session.delete(event)
            if commit:
                self.session.commit()
            return None

        todo.estimated_minutes = self.estimate_minutes(todo)
        if event is None:
            event = CalendarEvent(
                todo_id=todo.id,
                domain_id=todo.domain_id,
                title=todo.title,
                item_kind="scheduled_todo",
                context_type=None,
                scheduling_effect="hard",
                blocks_time=True,
                timezone="America/New_York",
                all_day=False,
                attendees=[],
                supporting_refs=[],
                source_refs=todo.source_refs,
                provenance={**(todo.provenance or {}), "projection": "scheduled_todo"},
                status="scheduled",
                metadata_={},
            )
            self.session.add(event)
        event.domain_id = todo.domain_id
        event.title = todo.title
        event.summary = todo.description
        event.start_at = todo.scheduled_start_at
        event.end_at = todo.scheduled_start_at + timedelta(minutes=todo.estimated_minutes)
        event.source_refs = todo.source_refs
        event.metadata_ = {
            **(event.metadata_ or {}),
            "todo_id": str(todo.id),
            "todo_status": todo.status,
            "estimated_minutes": todo.estimated_minutes,
        }
        if commit:
            self.session.commit()
            self.session.refresh(event)
        return event

    def _domain_key(self, domain_id) -> str | None:
        if domain_id is None:
            return None
        domain = self.session.get(Domain, domain_id)
        return domain.key if domain else None


def _bounded_minutes(value: Any) -> int:
    return min(480, max(5, int(value)))


def _fallback_minutes(todo: Todo) -> int:
    text = f"{todo.title} {todo.description}".lower()
    if any(word in text for word in ("draft", "review", "research", "analyze", "prepare", "plan")):
        return 60
    if any(word in text for word in ("call", "email", "confirm", "submit", "schedule", "send")):
        return 30
    return 45
