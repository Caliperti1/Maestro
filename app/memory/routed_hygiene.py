import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CalendarEvent,
    Contact,
    ContactAlias,
    ContactDomainNote,
    ContactRelationship,
    RoutedObjectChangeLog,
    RoutedObjectLink,
    RuntimeSetting,
    Todo,
)
from app.memory.routed_resolver import contact_aliases_for


@dataclass(frozen=True)
class RoutedHygieneReport:
    aliases_backfilled: int
    display_fields_canonicalized: int
    duplicates_merged: int
    suggestions: list[dict[str, Any]]


class RoutedHygieneService:
    """Background hygiene for routed-object stores.

    This intentionally proposes duplicate merges instead of applying them automatically.
    """

    SETTING_KEY = "routed_hygiene_latest"

    def __init__(self, session: Session):
        self.session = session

    def run_once(self, *, persist_report: bool = True) -> RoutedHygieneReport:
        display_fields_canonicalized = self.canonicalize_display_fields()
        aliases_backfilled = self.backfill_contact_aliases()
        duplicates_merged = self.merge_high_confidence_duplicates()
        suggestions = [
            *self.contact_duplicate_suggestions(),
            *self.event_duplicate_suggestions(),
            *self.todo_duplicate_suggestions(),
        ]
        report = RoutedHygieneReport(
            aliases_backfilled=aliases_backfilled,
            display_fields_canonicalized=display_fields_canonicalized,
            duplicates_merged=duplicates_merged,
            suggestions=suggestions,
        )
        if persist_report:
            setting = self.session.get(RuntimeSetting, self.SETTING_KEY)
            payload = {
                "aliases_backfilled": aliases_backfilled,
                "display_fields_canonicalized": display_fields_canonicalized,
                "duplicates_merged": duplicates_merged,
                "suggestions": suggestions,
            }
            if setting is None:
                setting = RuntimeSetting(key=self.SETTING_KEY, value=payload)
                self.session.add(setting)
            else:
                setting.value = payload
            self.session.commit()
        return report

    def merge_high_confidence_duplicates(self) -> int:
        merged = 0
        merged += self._merge_duplicate_contacts()
        merged += self._merge_duplicate_events()
        merged += self._merge_duplicate_todos()
        if merged:
            self.session.commit()
        return merged

    def canonicalize_display_fields(self) -> int:
        from app.memory.routed_service import (
            _event_title_from_text,
            _is_generic_route_title,
            _name_from_title,
        )

        count = 0
        contacts = self.session.scalars(select(Contact)).all()
        for contact in contacts:
            cleaned_name = _name_from_title(contact.name)
            if cleaned_name and _normalize(cleaned_name) != _normalize(contact.name):
                contact.metadata_ = {
                    **(contact.metadata_ or {}),
                    "previous_name": contact.name,
                    "canonicalized_by_hygiene": True,
                }
                contact.name = cleaned_name
                contact.normalized_name = _normalize(cleaned_name)
                count += 1
        events = self.session.scalars(select(CalendarEvent)).all()
        for event in events:
            if not _is_generic_route_title(event.title):
                continue
            title = _event_title_from_text(event.summary or "")
            if title and _normalize(title) != _normalize(event.title):
                event.metadata_ = {
                    **(event.metadata_ or {}),
                    "previous_title": event.title,
                    "canonicalized_by_hygiene": True,
                }
                event.title = title
                count += 1
        if count:
            self.session.commit()
        return count

    def backfill_contact_aliases(self) -> int:
        count = 0
        contacts = self.session.scalars(select(Contact).where(Contact.status != "archived")).all()
        for contact in contacts:
            aliases = set(contact_aliases_for(contact.name))
            metadata_aliases = (contact.metadata_ or {}).get("aliases") or []
            if isinstance(metadata_aliases, list):
                aliases.update(str(alias) for alias in metadata_aliases if str(alias).strip())
            normalized_seen: set[str] = set()
            for alias in aliases:
                normalized = _normalize(alias)
                if not normalized or normalized in normalized_seen:
                    continue
                normalized_seen.add(normalized)
                existing = self.session.scalar(
                    select(ContactAlias).where(ContactAlias.normalized_alias == normalized)
                )
                if existing is None:
                    now = datetime.now(UTC)
                    self.session.add(
                        ContactAlias(
                            contact_id=contact.id,
                            alias=alias,
                            normalized_alias=normalized,
                            source="hygiene_backfill",
                            source_refs=[],
                            metadata_={},
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    count += 1
        if count:
            self.session.commit()
        return count

    def _merge_duplicate_contacts(self) -> int:
        contacts = list(self.session.scalars(select(Contact).where(Contact.status != "archived")))
        merged = 0
        by_key: dict[str, Contact] = {}
        for contact in sorted(contacts, key=lambda item: item.created_at or datetime.now(UTC)):
            keys = [f"name:{_normalize(contact.name)}"]
            if contact.email:
                keys.insert(0, f"email:{contact.email.lower()}")
            survivor = next((by_key[key] for key in keys if key in by_key), None)
            if survivor is None:
                for key in keys:
                    by_key.setdefault(key, contact)
                continue
            self._merge_contact(survivor, contact)
            merged += 1
        return merged

    def _merge_duplicate_events(self) -> int:
        events = list(self.session.scalars(select(CalendarEvent).where(CalendarEvent.status != "archived")))
        merged = 0
        by_key: dict[str, CalendarEvent] = {}
        for event in sorted(events, key=lambda item: item.created_at or datetime.now(UTC)):
            key = _event_merge_key(event)
            if not key:
                continue
            survivor = by_key.get(key)
            if survivor is None:
                by_key[key] = event
                continue
            self._merge_event(survivor, event)
            merged += 1
        return merged

    def _merge_duplicate_todos(self) -> int:
        todos = list(self.session.scalars(select(Todo).where(Todo.status.notin_(["done", "archived"]))))
        merged = 0
        by_key: dict[str, Todo] = {}
        for todo in sorted(todos, key=lambda item: item.created_at or datetime.now(UTC)):
            key = f"{todo.domain_id}:{_normalize(todo.title)}"
            survivor = by_key.get(key)
            if survivor is None:
                by_key[key] = todo
                continue
            self._merge_todo(survivor, todo)
            merged += 1
        return merged

    def _merge_contact(self, survivor: Contact, duplicate: Contact) -> None:
        from app.memory.routed_service import _append_note, _merge_source_refs

        survivor.summary = _append_note(survivor.summary, duplicate.summary or "")
        survivor.source_refs = _merge_source_refs(survivor.source_refs, duplicate.source_refs)
        survivor.metadata_ = _merge_metadata(survivor.metadata_, duplicate.metadata_, duplicate_id=duplicate.id)
        survivor.phone = survivor.phone or duplicate.phone
        survivor.email = survivor.email or duplicate.email
        survivor.linkedin = survivor.linkedin or duplicate.linkedin
        survivor.organization_entity_id = survivor.organization_entity_id or duplicate.organization_entity_id
        survivor.origination = survivor.origination or duplicate.origination
        survivor.last_contact_at = max(
            [value for value in (survivor.last_contact_at, duplicate.last_contact_at) if value],
            default=None,
        )
        survivor.scheduled_event_ids = sorted(
            {*(survivor.scheduled_event_ids or []), *(duplicate.scheduled_event_ids or [])}
        )
        for alias in self.session.scalars(select(ContactAlias).where(ContactAlias.contact_id == duplicate.id)):
            existing = self.session.scalar(
                select(ContactAlias).where(ContactAlias.normalized_alias == alias.normalized_alias)
            )
            if existing is None or existing.id == alias.id:
                alias.contact_id = survivor.id
            else:
                alias.source_refs = _merge_source_refs(existing.source_refs, alias.source_refs)
                alias.metadata_ = _merge_metadata(existing.metadata_, alias.metadata_)
                self.session.delete(alias)
        for note in self.session.scalars(select(ContactDomainNote).where(ContactDomainNote.contact_id == duplicate.id)):
            existing = self.session.scalar(
                select(ContactDomainNote).where(
                    ContactDomainNote.contact_id == survivor.id,
                    ContactDomainNote.domain_id == note.domain_id,
                )
            )
            if existing is None:
                note.contact_id = survivor.id
            else:
                existing.notes = _append_note(existing.notes, note.notes or "")
                existing.interaction_log = [*(existing.interaction_log or []), *(note.interaction_log or [])]
                existing.source_refs = _merge_source_refs(existing.source_refs, note.source_refs)
                existing.metadata_ = _merge_metadata(existing.metadata_, note.metadata_)
                self.session.delete(note)
        for relationship in self.session.scalars(
            select(ContactRelationship).where(
                (ContactRelationship.contact_id == duplicate.id)
                | (ContactRelationship.related_contact_id == duplicate.id)
            )
        ):
            if relationship.contact_id == duplicate.id:
                relationship.contact_id = survivor.id
            if relationship.related_contact_id == duplicate.id:
                relationship.related_contact_id = survivor.id
        self._finalize_merge("contact", survivor.id, duplicate)

    def _merge_event(self, survivor: CalendarEvent, duplicate: CalendarEvent) -> None:
        from app.memory.routed_service import _append_note, _merge_attendees, _merge_source_refs

        survivor.summary = _append_note(survivor.summary, duplicate.summary or "")
        survivor.start_at = survivor.start_at or duplicate.start_at
        survivor.end_at = survivor.end_at or duplicate.end_at
        survivor.location = survivor.location or duplicate.location
        survivor.attendees = _merge_attendees(survivor.attendees, duplicate.attendees)
        survivor.supporting_refs = _merge_source_refs(survivor.supporting_refs, duplicate.supporting_refs)
        survivor.source_refs = _merge_source_refs(survivor.source_refs, duplicate.source_refs)
        survivor.metadata_ = _merge_metadata(survivor.metadata_, duplicate.metadata_, duplicate_id=duplicate.id)
        self._finalize_merge("event", survivor.id, duplicate)

    def _merge_todo(self, survivor: Todo, duplicate: Todo) -> None:
        from app.memory.routed_service import _append_note, _merge_source_refs, _priority_rank

        survivor.description = _append_note(survivor.description, duplicate.description)
        survivor.due_at = survivor.due_at or duplicate.due_at
        survivor.owner_ref = survivor.owner_ref or duplicate.owner_ref
        survivor.source_refs = _merge_source_refs(survivor.source_refs, duplicate.source_refs)
        survivor.metadata_ = _merge_metadata(survivor.metadata_, duplicate.metadata_, duplicate_id=duplicate.id)
        if _priority_rank(duplicate.priority) > _priority_rank(survivor.priority):
            survivor.priority = duplicate.priority
        self._finalize_merge("todo", survivor.id, duplicate)

    def _finalize_merge(self, object_type: str, survivor_id: uuid.UUID, duplicate: Any) -> None:
        for link in self.session.scalars(
            select(RoutedObjectLink).where(
                RoutedObjectLink.object_type == object_type,
                RoutedObjectLink.object_id == duplicate.id,
            )
        ):
            existing = self.session.scalar(
                select(RoutedObjectLink).where(
                    RoutedObjectLink.routed_item_id == link.routed_item_id,
                    RoutedObjectLink.object_type == object_type,
                    RoutedObjectLink.object_id == survivor_id,
                )
            )
            if existing is None:
                link.object_id = survivor_id
            else:
                self.session.delete(link)
        duplicate.status = "archived"
        duplicate.metadata_ = {
            **(duplicate.metadata_ or {}),
            "merged_into": str(survivor_id),
            "merged_by_hygiene": True,
            "merged_at": datetime.now(UTC).isoformat(),
        }
        self.session.add(
            RoutedObjectChangeLog(
                object_type=object_type,
                object_id=survivor_id,
                action="merged_duplicate",
                changes={"duplicate_id": str(duplicate.id)},
                source_refs=[],
                metadata_={"hygiene": True},
            )
        )

    def contact_duplicate_suggestions(self) -> list[dict[str, Any]]:
        contacts = list(self.session.scalars(select(Contact).where(Contact.status != "archived")))
        suggestions: list[dict[str, Any]] = []
        for index, left in enumerate(contacts):
            for right in contacts[index + 1:]:
                if left.email and right.email and left.email == right.email:
                    suggestions.append(_suggestion("contact", left.id, right.id, 0.99, "same_email"))
                elif _normalize(left.name) == _normalize(right.name):
                    suggestions.append(_suggestion("contact", left.id, right.id, 0.92, "same_name"))
        return suggestions

    def event_duplicate_suggestions(self) -> list[dict[str, Any]]:
        events = list(self.session.scalars(select(CalendarEvent).where(CalendarEvent.status != "archived")))
        suggestions: list[dict[str, Any]] = []
        for index, left in enumerate(events):
            for right in events[index + 1:]:
                if left.domain_id == right.domain_id and _normalize(left.title) == _normalize(right.title):
                    if left.start_at and right.start_at and left.start_at == right.start_at:
                        suggestions.append(_suggestion("event", left.id, right.id, 0.95, "same_title_time"))
                    elif (left.summary or "") == (right.summary or ""):
                        suggestions.append(_suggestion("event", left.id, right.id, 0.86, "same_title_summary"))
        return suggestions

    def todo_duplicate_suggestions(self) -> list[dict[str, Any]]:
        todos = list(self.session.scalars(select(Todo).where(Todo.status.notin_(["done", "archived"]))))
        suggestions: list[dict[str, Any]] = []
        for index, left in enumerate(todos):
            for right in todos[index + 1:]:
                if left.domain_id == right.domain_id and _normalize(left.title) == _normalize(right.title):
                    suggestions.append(_suggestion("todo", left.id, right.id, 0.88, "same_title"))
        return suggestions


def _suggestion(object_type: str, left_id: uuid.UUID, right_id: uuid.UUID, score: float, reason: str) -> dict[str, Any]:
    return {
        "object_type": object_type,
        "left_id": str(left_id),
        "right_id": str(right_id),
        "score": score,
        "reason": reason,
        "action": "review_merge",
    }


def _event_merge_key(event: CalendarEvent) -> str | None:
    title = _normalize(event.title)
    if not title:
        return None
    if event.start_at:
        return f"{event.domain_id}:{title}:{event.start_at.isoformat()}"
    if event.summary:
        return f"{event.domain_id}:{title}:summary:{_normalize(event.summary)}"
    return None


def _merge_metadata(
    survivor: dict[str, Any] | None,
    duplicate: dict[str, Any] | None,
    *,
    duplicate_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    merged = {**(survivor or {}), **(duplicate or {})}
    duplicate_ids = list((survivor or {}).get("merged_duplicate_ids") or [])
    if duplicate_id is not None:
        duplicate_ids.append(str(duplicate_id))
    if duplicate_ids:
        merged["merged_duplicate_ids"] = sorted(set(duplicate_ids))
        merged["merged_by_hygiene"] = True
    return merged


def _normalize(value: str | None) -> str:
    import re

    normalized = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return re.sub(r"\s+", " ", normalized)
