import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import CalendarEvent, Contact, Entity, Idea, DecisionRecord, Todo
from app.memory.routed_service import RoutedMemoryService


@dataclass(frozen=True)
class RoutedContextBundle:
    query_text: str | None
    domain_id: uuid.UUID | None
    stores: dict[str, list[dict[str, Any]]]
    rendered_text: str


class RoutedRetrievalService:
    """Builds schematized routed-object context for Maestro and agent prompts."""

    def __init__(self, session: Session):
        self.session = session

    def build_context_bundle(
        self,
        *,
        domain_id: uuid.UUID | None = None,
        query_text: str | None = None,
        limit: int = 12,
        max_chars: int = 3000,
    ) -> RoutedContextBundle:
        stores = RoutedMemoryService(self.session).build_context_bundle(
            domain_id=domain_id,
            query_text=query_text,
            limit=limit,
        )
        rendered = self._render(stores, max_chars=max_chars)
        return RoutedContextBundle(
            query_text=query_text,
            domain_id=domain_id,
            stores=stores,
            rendered_text=rendered,
        )

    def _render(self, stores: dict[str, list[dict[str, Any]]], *, max_chars: int) -> str:
        lines: list[str] = []
        for label, items in stores.items():
            if not items:
                continue
            lines.append(f"{label.title()}:")
            for item in items:
                if label == "contacts":
                    detail = item.get("summary") or item.get("email") or ""
                    lines.append(f"- {item.get('name')}: {detail}")
                elif label == "events":
                    when = item.get("start_at") or "unscheduled"
                    lines.append(f"- {item.get('title')} ({when}): {item.get('summary') or ''}")
                elif label == "todos":
                    due = item.get("due_at") or "no due date"
                    lines.append(f"- {item.get('title')} [{item.get('status')}, {due}]: {item.get('description')}")
                else:
                    text = item.get("content") or item.get("decision") or item.get("summary") or ""
                    lines.append(f"- {item.get('title') or item.get('name')}: {text}")
        rendered = "\n".join(lines).strip()
        return rendered[:max_chars]


class RoutedEditService:
    """Small edit surface for canonical routed objects."""

    def __init__(self, session: Session):
        self.session = session

    def update_contact(self, contact_id: uuid.UUID, updates: dict[str, Any]) -> Contact:
        contact = self.session.get(Contact, contact_id)
        if contact is None:
            raise ValueError("Contact not found.")
        for key in ("name", "email", "phone", "linkedin", "summary", "origination", "status"):
            if key in updates:
                setattr(contact, key, updates[key])
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            contact.metadata_ = {**(contact.metadata_ or {}), **updates["metadata"]}
        self.session.commit()
        self.session.refresh(contact)
        return contact

    def update_event(self, event_id: uuid.UUID, updates: dict[str, Any]) -> CalendarEvent:
        event = self.session.get(CalendarEvent, event_id)
        if event is None:
            raise ValueError("Event not found.")
        for key in ("title", "summary", "location", "status"):
            if key in updates:
                setattr(event, key, updates[key])
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            event.metadata_ = {**(event.metadata_ or {}), **updates["metadata"]}
        self.session.commit()
        self.session.refresh(event)
        return event

    def update_todo(self, todo_id: uuid.UUID, updates: dict[str, Any]) -> Todo:
        todo = self.session.get(Todo, todo_id)
        if todo is None:
            raise ValueError("Todo not found.")
        for key in ("title", "description", "priority", "status", "owner_type", "owner_ref"):
            if key in updates:
                setattr(todo, key, updates[key])
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            todo.metadata_ = {**(todo.metadata_ or {}), **updates["metadata"]}
        self.session.commit()
        self.session.refresh(todo)
        return todo

    def archive_object(self, object_type: str, object_id: uuid.UUID):
        model = {
            "contact": Contact,
            "event": CalendarEvent,
            "todo": Todo,
            "entity": Entity,
            "idea": Idea,
            "decision": DecisionRecord,
        }.get(object_type)
        if model is None:
            raise ValueError("Unsupported routed object type.")
        obj = self.session.get(model, object_id)
        if obj is None:
            raise ValueError("Routed object not found.")
        obj.status = "archived"
        self.session.commit()
        return obj
