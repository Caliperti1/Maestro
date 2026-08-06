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
    ContactEmbedding,
    ContactInteraction,
    ContactOrganizationAffiliation,
    ContactRelationship,
    Entity,
    EntityDomainNote,
    OrganizationAlias,
    OrganizationEmbedding,
    RoutedObjectChangeLog,
    RoutedObjectLink,
    RuntimeSetting,
    Todo,
)
from app.memory.routed_resolver import contact_aliases_for


@dataclass(frozen=True)
class RoutedHygieneReport:
    aliases_backfilled: int
    aliases_pruned: int
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
        aliases_pruned = self.prune_unsubstantiated_contact_aliases()
        aliases_backfilled = self.backfill_contact_aliases()
        duplicates_merged = self.merge_high_confidence_duplicates()
        suggestions = [
            *self.contact_duplicate_suggestions(),
            *self.event_duplicate_suggestions(),
            *self.todo_duplicate_suggestions(),
        ]
        report = RoutedHygieneReport(
            aliases_backfilled=aliases_backfilled,
            aliases_pruned=aliases_pruned,
            display_fields_canonicalized=display_fields_canonicalized,
            duplicates_merged=duplicates_merged,
            suggestions=suggestions,
        )
        if persist_report:
            setting = self.session.get(RuntimeSetting, self.SETTING_KEY)
            payload = {
                "aliases_backfilled": aliases_backfilled,
                "aliases_pruned": aliases_pruned,
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

    def merge_contacts(
        self,
        survivor: Contact,
        duplicate: Contact,
        *,
        commit: bool = True,
    ) -> None:
        if survivor.id == duplicate.id:
            return
        self._merge_contact(survivor, duplicate)
        if commit:
            self.session.commit()

    def merge_entities(self, survivor: Entity, duplicate: Entity, *, commit: bool = True) -> None:
        if survivor.id == duplicate.id:
            return
        from app.memory.routed_service import _append_note, _merge_source_refs

        self._preserve_organization_merge_alias(survivor, duplicate)
        survivor.summary = _append_note(survivor.summary, duplicate.summary or "")
        survivor.website = survivor.website or duplicate.website
        survivor.source_refs = _merge_source_refs(survivor.source_refs, duplicate.source_refs)
        survivor.metadata_ = _merge_metadata(survivor.metadata_, duplicate.metadata_, duplicate_id=duplicate.id)
        for contact in self.session.scalars(
            select(Contact).where(Contact.organization_entity_id == duplicate.id)
        ):
            contact.organization_entity_id = survivor.id
        for affiliation in self.session.scalars(
            select(ContactOrganizationAffiliation).where(
                ContactOrganizationAffiliation.entity_id == duplicate.id
            )
        ):
            existing = self.session.scalar(
                select(ContactOrganizationAffiliation).where(
                    ContactOrganizationAffiliation.contact_id == affiliation.contact_id,
                    ContactOrganizationAffiliation.entity_id == survivor.id,
                    ContactOrganizationAffiliation.domain_id == affiliation.domain_id,
                    ContactOrganizationAffiliation.role == affiliation.role,
                )
            )
            if existing is None:
                affiliation.entity_id = survivor.id
            else:
                existing.source_refs = _merge_source_refs(existing.source_refs, affiliation.source_refs)
                existing.metadata_ = _merge_metadata(existing.metadata_, affiliation.metadata_)
                self.session.delete(affiliation)
        for note in self.session.scalars(
            select(EntityDomainNote).where(EntityDomainNote.entity_id == duplicate.id)
        ):
            existing = self.session.scalar(
                select(EntityDomainNote).where(
                    EntityDomainNote.entity_id == survivor.id,
                    EntityDomainNote.domain_id == note.domain_id,
                )
            )
            if existing is None:
                note.entity_id = survivor.id
            else:
                existing.notes = _append_note(existing.notes, note.notes or "")
                existing.interaction_log = [*(existing.interaction_log or []), *(note.interaction_log or [])]
                existing.source_refs = _merge_source_refs(existing.source_refs, note.source_refs)
                existing.metadata_ = _merge_metadata(existing.metadata_, note.metadata_)
                self.session.delete(note)
        for alias in self.session.scalars(
            select(OrganizationAlias).where(OrganizationAlias.entity_id == duplicate.id)
        ):
            existing = self.session.scalar(
                select(OrganizationAlias).where(
                    OrganizationAlias.normalized_alias == alias.normalized_alias
                )
            )
            if existing is None or existing.id == alias.id:
                alias.entity_id = survivor.id
            else:
                self.session.delete(alias)
        for embedding in self.session.scalars(
            select(OrganizationEmbedding).where(OrganizationEmbedding.entity_id == duplicate.id)
        ):
            self.session.delete(embedding)
        self._finalize_merge("entity", survivor.id, duplicate)
        if commit:
            self.session.commit()

    def canonicalize_display_fields(self) -> int:
        from app.memory.routed_service import (
            _event_title_from_text,
            _is_generic_route_title,
            _name_from_title,
        )

        count = 0
        contacts = self.session.scalars(select(Contact)).all()
        for contact in contacts:
            embedded_email = _contact_email_identity(contact)
            if embedded_email:
                contact.metadata_ = {
                    **(contact.metadata_ or {}),
                    "observed_email_identity": embedded_email,
                }
            if embedded_email and not contact.email:
                collision = self.session.scalar(
                    select(Contact).where(
                        Contact.email == embedded_email,
                        Contact.id != contact.id,
                        Contact.status != "archived",
                    )
                )
                if collision is None:
                    contact.email = embedded_email
            cleaned_name = _name_from_contact_email(contact) or _name_from_title(contact.name)
            if cleaned_name and _normalize(cleaned_name) != _normalize(contact.name):
                contact.metadata_ = {
                    **(contact.metadata_ or {}),
                    "previous_name": contact.name,
                    "canonicalized_by_hygiene": True,
                }
                contact.name = cleaned_name
                contact.normalized_name = _normalize(cleaned_name)
                count += 1
        entities = self.session.scalars(select(Entity)).all()
        for entity in entities:
            cleaned_name = _name_from_organization_identifier(entity.name)
            if cleaned_name and _normalize(cleaned_name) != _normalize(entity.name):
                collision = self.session.scalar(
                    select(Entity).where(
                        Entity.normalized_name == _normalize(cleaned_name),
                        Entity.id != entity.id,
                    )
                )
                if collision is not None:
                    continue
                entity.metadata_ = {
                    **(entity.metadata_ or {}),
                    "previous_name": entity.name,
                    "canonicalized_by_hygiene": True,
                }
                entity.name = cleaned_name
                entity.normalized_name = _normalize(cleaned_name)
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

    def prune_unsubstantiated_contact_aliases(self) -> int:
        count = 0
        contacts = {
            contact.id: contact
            for contact in self.session.scalars(select(Contact)).all()
        }
        for alias in self.session.scalars(select(ContactAlias)).all():
            contact = contacts.get(alias.contact_id)
            if contact is None:
                continue
            if alias.source not in {"manual", "duplicate_merge"} and _alias_is_synthetic(
                alias.alias,
                contact.name,
            ):
                self.session.delete(alias)
                count += 1
        for contact in contacts.values():
            metadata_aliases = (contact.metadata_ or {}).get("aliases") or []
            if not isinstance(metadata_aliases, list):
                continue
            kept = [
                str(alias).strip()
                for alias in metadata_aliases
                if str(alias).strip() and not _alias_is_synthetic(str(alias), contact.name)
            ]
            if kept != metadata_aliases:
                count += max(0, len(metadata_aliases) - len(kept))
                contact.metadata_ = {**(contact.metadata_ or {}), "aliases": sorted(set(kept))}
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
        for contact in sorted(
            contacts,
            key=lambda item: (
                0 if item.email else 1,
                0 if "@" not in item.name else 1,
                item.created_at or datetime.now(UTC),
            ),
        ):
            # Names are discovery signals, not safe merge keys. Keep name-only duplicates in the
            # review suggestions below and auto-merge only exact durable identifiers.
            email_identity = _contact_email_identity(contact)
            keys = [f"email:{email_identity}"] if email_identity else []
            if not keys:
                continue
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

        self._preserve_contact_merge_alias(survivor, duplicate)
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
        for event in self.session.scalars(select(CalendarEvent)).all():
            updated_attendees: list[dict[str, Any]] = []
            seen_contact_ids: set[str] = set()
            changed = False
            for attendee in event.attendees or []:
                if not isinstance(attendee, dict):
                    continue
                next_attendee = dict(attendee)
                if str(next_attendee.get("contact_id") or "") == str(duplicate.id):
                    next_attendee["contact_id"] = str(survivor.id)
                    next_attendee["name"] = survivor.name
                    changed = True
                contact_id = str(next_attendee.get("contact_id") or "")
                if contact_id and contact_id in seen_contact_ids:
                    changed = True
                    continue
                if contact_id:
                    seen_contact_ids.add(contact_id)
                updated_attendees.append(next_attendee)
            if changed:
                event.attendees = updated_attendees
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
        for interaction in self.session.scalars(
            select(ContactInteraction).where(ContactInteraction.contact_id == duplicate.id)
        ):
            interaction.contact_id = survivor.id
        for affiliation in self.session.scalars(
            select(ContactOrganizationAffiliation).where(
                ContactOrganizationAffiliation.contact_id == duplicate.id
            )
        ):
            existing = self.session.scalar(
                select(ContactOrganizationAffiliation).where(
                    ContactOrganizationAffiliation.contact_id == survivor.id,
                    ContactOrganizationAffiliation.entity_id == affiliation.entity_id,
                    ContactOrganizationAffiliation.domain_id == affiliation.domain_id,
                    ContactOrganizationAffiliation.role == affiliation.role,
                )
            )
            if existing is None:
                affiliation.contact_id = survivor.id
            else:
                existing.source_refs = _merge_source_refs(existing.source_refs, affiliation.source_refs)
                existing.metadata_ = _merge_metadata(existing.metadata_, affiliation.metadata_)
                self.session.delete(affiliation)
        for embedding in self.session.scalars(
            select(ContactEmbedding).where(ContactEmbedding.contact_id == duplicate.id)
        ):
            self.session.delete(embedding)
        self._finalize_merge("contact", survivor.id, duplicate)

    def _preserve_contact_merge_alias(self, survivor: Contact, duplicate: Contact) -> None:
        normalized = _normalize(duplicate.name)
        if not normalized or normalized == _normalize(survivor.name) or "@" in duplicate.name:
            return
        existing = self.session.scalar(
            select(ContactAlias).where(ContactAlias.normalized_alias == normalized)
        )
        if existing is None:
            self.session.add(
                ContactAlias(
                    contact_id=survivor.id,
                    alias=duplicate.name,
                    normalized_alias=normalized,
                    source="duplicate_merge",
                    source_refs=duplicate.source_refs,
                    metadata_={"duplicate_contact_id": str(duplicate.id)},
                )
            )
        elif existing.contact_id == duplicate.id:
            existing.contact_id = survivor.id
            existing.source = "duplicate_merge"
        explicit = {
            str(value).strip()
            for value in (survivor.metadata_ or {}).get("aliases") or []
            if str(value).strip()
        }
        explicit.update(
            str(value).strip()
            for value in (duplicate.metadata_ or {}).get("aliases") or []
            if str(value).strip()
        )
        explicit.add(duplicate.name)
        survivor.metadata_ = {**(survivor.metadata_ or {}), "aliases": sorted(explicit)}

    def _preserve_organization_merge_alias(self, survivor: Entity, duplicate: Entity) -> None:
        normalized = _normalize(duplicate.name)
        if not normalized or normalized == _normalize(survivor.name):
            return
        existing = self.session.scalar(
            select(OrganizationAlias).where(OrganizationAlias.normalized_alias == normalized)
        )
        if existing is None:
            self.session.add(
                OrganizationAlias(
                    entity_id=survivor.id,
                    alias=duplicate.name,
                    normalized_alias=normalized,
                    source="duplicate_merge",
                    source_refs=duplicate.source_refs,
                    metadata_={"duplicate_entity_id": str(duplicate.id)},
                )
            )
        elif existing.entity_id == duplicate.id:
            existing.entity_id = survivor.id
            existing.source = "duplicate_merge"
        explicit = {
            str(value).strip()
            for value in (survivor.metadata_ or {}).get("aliases") or []
            if str(value).strip()
        }
        explicit.update(
            str(value).strip()
            for value in (duplicate.metadata_ or {}).get("aliases") or []
            if str(value).strip()
        )
        explicit.add(duplicate.name)
        survivor.metadata_ = {**(survivor.metadata_ or {}), "aliases": sorted(explicit)}

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
    aliases = {
        str(value).strip()
        for source in (survivor or {}, duplicate or {})
        for value in source.get("aliases") or []
        if str(value).strip()
    }
    if aliases:
        merged["aliases"] = sorted(aliases)
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


def _name_from_contact_email(contact: Contact) -> str | None:
    import re

    email = _contact_email_identity(contact)
    if not email:
        return None
    header_match = re.match(r"\s*([^<]+?)\s*<[^>]+@[^>]+>\s*$", contact.name)
    if header_match:
        return header_match.group(1).strip().strip('"') or None
    normalized_name = _normalize(contact.name)
    normalized_email = _normalize(email)
    local = email.split("@", 1)[0]
    normalized_local = _normalize(local)
    if "@" not in contact.name and normalized_name not in {normalized_email, normalized_local}:
        return None
    ignored = {"army", "civ", "ctr", "mil", "usa", "usaf", "usarmy", "usmc", "usn"}
    parts: list[str] = []
    for raw_part in re.split(r"[._+-]+", local):
        part = re.sub(r"\d+$", "", raw_part.lower())
        if part and part not in ignored:
            parts.append(part.upper() if len(part) == 1 else part.capitalize())
    return " ".join(parts).strip() or None


def _contact_email_identity(contact: Contact) -> str | None:
    import re

    if contact.email:
        return contact.email.strip().lower()
    observed = str((contact.metadata_ or {}).get("observed_email_identity") or "").strip()
    if observed:
        return observed.lower()
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", contact.name)
    return match.group(0).lower() if match else None


def _alias_is_synthetic(alias: str, canonical_name: str) -> bool:
    normalized = _normalize(alias)
    canonical = _normalize(canonical_name)
    if not normalized or normalized == canonical:
        return False
    tokens = normalized.split()
    canonical_tokens = canonical.split()
    if "@" in alias or any(token in {"com", "edu", "mil", "net", "org"} for token in tokens):
        return True
    if tokens and all(len(token) == 1 for token in tokens):
        return True
    if len(tokens) == 2 and len(tokens[-1]) == 1:
        return True
    return bool(canonical_tokens and len(tokens) > len(canonical_tokens) + 1)


def _name_from_organization_identifier(name: str) -> str | None:
    import re

    cleaned = name.strip()
    candidate = cleaned.removeprefix("https://").removeprefix("http://").removeprefix("www.")
    candidate = candidate.split("/", 1)[0]
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[-1]
    if "." not in candidate or " " in candidate:
        return None
    parts = candidate.lower().split(".")
    stem = parts[-2] if len(parts) >= 2 else parts[0]
    words = [part for part in re.split(r"[-_]", stem) if part]
    return " ".join(word.upper() if len(word) <= 3 else word.capitalize() for word in words) or None
