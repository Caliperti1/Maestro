import uuid
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import home_timezone
from app.db.models import CalendarEvent, Contact, ContactAlias, Entity, Idea, DecisionRecord, Todo
from app.memory.routed_hygiene import RoutedHygieneService
from app.memory.routed_resolver import contact_aliases_for
from app.memory.routed_service import RoutedMemoryService


@dataclass(frozen=True)
class RoutedContextBundle:
    query_text: str | None
    domain_id: uuid.UUID | None
    stores: dict[str, list[dict[str, Any]]]
    rendered_text: str


class ContactAliasConflictError(ValueError):
    """Raised when an alias is already attached to a substantive contact."""


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
        if "name" in updates:
            contact.normalized_name = _normalize_key(str(updates["name"]))
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            contact.metadata_ = {**(contact.metadata_ or {}), **updates["metadata"]}
        if "aliases" in updates:
            self._set_contact_aliases(contact, updates["aliases"])
        self.session.commit()
        self.session.refresh(contact)
        return contact

    def _set_contact_aliases(self, contact: Contact, raw_aliases: Any) -> None:
        requested = _alias_values(raw_aliases)
        normalized_requested = {_normalize_key(alias): alias for alias in requested}
        for normalized, alias_text in normalized_requested.items():
            if not normalized:
                continue
            existing = self.session.scalar(
                select(ContactAlias).where(ContactAlias.normalized_alias == normalized)
            )
            if existing is not None and existing.contact_id != contact.id:
                duplicate = self.session.get(Contact, existing.contact_id)
                if duplicate is None or not _safe_alias_merge_candidate(duplicate, normalized):
                    owner = duplicate.name if duplicate is not None else "another contact"
                    raise ContactAliasConflictError(
                        f"Alias '{alias_text}' already belongs to {owner}. "
                        "Resolve that contact manually."
                    )
                RoutedHygieneService(self.session).merge_contacts(
                    contact,
                    duplicate,
                    commit=False,
                )
                existing = self.session.scalar(
                    select(ContactAlias).where(ContactAlias.normalized_alias == normalized)
                )
            if existing is None:
                self.session.add(
                    ContactAlias(
                        contact_id=contact.id,
                        alias=alias_text,
                        normalized_alias=normalized,
                        source="manual",
                        source_refs=[],
                        metadata_={"edited_in_ui": True},
                    )
                )
            else:
                existing.contact_id = contact.id
                existing.alias = alias_text
                existing.source = "manual"

        for alias in self.session.scalars(
            select(ContactAlias).where(
                ContactAlias.contact_id == contact.id,
                ContactAlias.source == "manual",
            )
        ).all():
            if alias.normalized_alias not in normalized_requested:
                self.session.delete(alias)

        all_aliases = set(contact_aliases_for(contact.name))
        all_aliases.update(requested)
        contact.metadata_ = {
            **(contact.metadata_ or {}),
            "aliases": sorted(all_aliases),
        }

    def update_event(self, event_id: uuid.UUID, updates: dict[str, Any]) -> CalendarEvent:
        event = self.session.get(CalendarEvent, event_id)
        if event is None:
            raise ValueError("Event not found.")
        for key in ("title", "summary", "location", "status"):
            if key in updates:
                setattr(event, key, updates[key])
        for key in ("start_at", "end_at"):
            if key in updates:
                setattr(event, key, _parse_optional_datetime(updates[key]))
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
        if "due_at" in updates:
            todo.due_at = _parse_optional_datetime(updates["due_at"])
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            todo.metadata_ = {**(todo.metadata_ or {}), **updates["metadata"]}
        self.session.commit()
        self.session.refresh(todo)
        return todo

    def update_entity(self, entity_id: uuid.UUID, updates: dict[str, Any]) -> Entity:
        entity = self.session.get(Entity, entity_id)
        if entity is None:
            raise ValueError("Entity not found.")
        if "name" in updates:
            entity.name = updates["name"]
            entity.normalized_name = _normalize_key(str(updates["name"]))
        for key in ("website", "summary", "status"):
            if key in updates:
                setattr(entity, key, updates[key])
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            entity.metadata_ = {**(entity.metadata_ or {}), **updates["metadata"]}
        self.session.commit()
        self.session.refresh(entity)
        return entity

    def update_idea(self, idea_id: uuid.UUID, updates: dict[str, Any]) -> Idea:
        idea = self.session.get(Idea, idea_id)
        if idea is None:
            raise ValueError("Idea not found.")
        for key in ("title", "content", "status"):
            if key in updates:
                setattr(idea, key, updates[key])
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            idea.metadata_ = {**(idea.metadata_ or {}), **updates["metadata"]}
        self.session.commit()
        self.session.refresh(idea)
        return idea

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


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=home_timezone())
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=home_timezone())
    raise ValueError("Expected an ISO datetime string.")


def _alias_values(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,\n]", value)
    elif isinstance(value, list):
        values = [str(item) for item in value]
    else:
        raise ValueError("Aliases must be a list or comma-separated string.")
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def _safe_alias_merge_candidate(contact: Contact, normalized_alias: str) -> bool:
    if contact.normalized_name == normalized_alias and not contact.email and not contact.phone:
        return True
    return bool(
        (contact.metadata_ or {}).get("created_from_attendee")
        and not contact.email
        and not contact.phone
    )
