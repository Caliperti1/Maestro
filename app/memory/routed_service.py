import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    CalendarEvent,
    Contact,
    ContactDomainNote,
    DecisionRecord,
    Entity,
    EntityDomainNote,
    Idea,
    RoutedItem,
    RoutedObjectChangeLog,
    RoutedObjectLink,
    Todo,
)


@dataclass(frozen=True)
class RoutedPromotionResult:
    routed_item_id: uuid.UUID
    route_type: str
    object_type: str
    object_id: uuid.UUID
    action: str


class RoutedMemoryService:
    """Promotes raw routed extraction ledger rows into canonical routed-memory stores."""

    def __init__(self, session: Session):
        self.session = session

    def process_pending(self, *, limit: int = 100) -> list[RoutedPromotionResult]:
        items = self.session.scalars(
            select(RoutedItem)
            .where(
                RoutedItem.status.notin_(["archived", "ignored"]),
                ~select(RoutedObjectLink.id)
                .where(RoutedObjectLink.routed_item_id == RoutedItem.id)
                .exists(),
            )
            .order_by(RoutedItem.created_at)
            .limit(limit)
        ).all()
        return self.promote_items(items)

    def promote_items(self, items: list[RoutedItem] | tuple[RoutedItem, ...]) -> list[RoutedPromotionResult]:
        results: list[RoutedPromotionResult] = []
        for item in items:
            if self._has_link(item):
                continue
            result = self.promote_item(item)
            if result is not None:
                results.append(result)
        if results:
            self.session.commit()
        return results

    def promote_item(self, item: RoutedItem) -> RoutedPromotionResult | None:
        route_type = item.route_type
        if route_type == "ignore":
            return None
        if route_type in {"task", "human_input", "project", "integration_note"}:
            return self._promote_todo(item)
        if route_type == "event":
            return self._promote_event(item)
        if route_type == "contact":
            return self._promote_contact(item)
        if route_type == "entity":
            return self._promote_entity(item)
        if route_type == "think_tank":
            return self._promote_idea(item)
        if route_type == "decision_log":
            return self._promote_decision(item)
        return self._promote_idea(item, object_type="routed_note")

    def build_context_bundle(
        self,
        *,
        domain_id: uuid.UUID | None = None,
        query_text: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        query = (query_text or "").strip().lower()
        return {
            "events": [self._event_payload(item) for item in self._events(domain_id, query, limit)],
            "todos": [self._todo_payload(item) for item in self._todos(domain_id, query, limit)],
            "contacts": [self._contact_payload(item) for item in self._contacts(domain_id, query, limit)],
            "entities": [self._entity_payload(item) for item in self._entities(domain_id, query, limit)],
            "ideas": [self._idea_payload(item) for item in self._ideas(domain_id, query, limit)],
            "decisions": [self._decision_payload(item) for item in self._decisions(domain_id, query, limit)],
        }

    def _promote_todo(self, item: RoutedItem) -> RoutedPromotionResult:
        todo = Todo(
            domain_id=item.domain_id,
            title=item.title,
            description=item.content,
            todo_type="human_input" if item.route_type == "human_input" else item.route_type,
            owner_type="user" if item.route_type == "human_input" else "maestro",
            owner_ref="Chris" if item.route_type == "human_input" else None,
            due_at=_datetime_from_metadata(item.metadata_, "due_at"),
            priority=item.priority,
            status="needs_input" if item.route_type == "human_input" else "open",
            source_refs=item.source_refs,
            provenance=self._provenance(item),
            metadata_=self._canonical_metadata(item),
        )
        self.session.add(todo)
        self.session.flush()
        return self._link(item, "todo", todo.id, "created")

    def _promote_event(self, item: RoutedItem) -> RoutedPromotionResult:
        event = CalendarEvent(
            domain_id=item.domain_id,
            title=item.title,
            summary=item.content,
            start_at=_datetime_from_metadata(item.metadata_, "start_at"),
            end_at=_datetime_from_metadata(item.metadata_, "end_at"),
            location=_string_from_metadata(item.metadata_, "location"),
            attendees=_list_from_metadata(item.metadata_, "attendees"),
            supporting_refs=item.source_refs,
            source_refs=item.source_refs,
            provenance=self._provenance(item),
            status=item.status if item.status not in {"open", "needs_input"} else "scheduled",
            metadata_=self._canonical_metadata(item),
        )
        self.session.add(event)
        self.session.flush()
        return self._link(item, "event", event.id, "created")

    def _promote_contact(self, item: RoutedItem) -> RoutedPromotionResult:
        email = _email_from_text(item.content) or _string_from_metadata(item.metadata_, "email")
        name = (
            _string_from_metadata(item.metadata_, "name")
            or _name_from_content(item.content)
            or _name_from_title(item.title)
        )
        normalized_name = _normalize_key(name)
        contact = self.session.scalar(
            select(Contact).where(Contact.email == email)
        ) if email else None
        if contact is None:
            contact = self.session.scalar(
                select(Contact).where(Contact.normalized_name == normalized_name)
            )
        action = "updated" if contact is not None else "created"
        if contact is None:
            contact = Contact(
                name=name,
                normalized_name=normalized_name,
                email=email,
                phone=_phone_from_text(item.content) or _string_from_metadata(item.metadata_, "phone"),
                linkedin=_linkedin_from_text(item.content) or _string_from_metadata(item.metadata_, "linkedin"),
                summary=item.content,
                origination=_string_from_metadata(item.metadata_, "origination"),
                last_contact_at=_datetime_from_metadata(item.metadata_, "last_contact_at"),
                source_refs=item.source_refs,
                provenance=self._provenance(item),
                metadata_=self._canonical_metadata(item),
            )
            self.session.add(contact)
            self.session.flush()
        else:
            contact.summary = _append_note(contact.summary, item.content)
            contact.source_refs = _merge_source_refs(contact.source_refs, item.source_refs)
            contact.metadata_ = {**(contact.metadata_ or {}), **self._canonical_metadata(item)}
            if email and not contact.email:
                contact.email = email
        organization = _organization_from_text(item.content) or _string_from_metadata(item.metadata_, "organization")
        if organization:
            entity = self._upsert_entity(organization, item)
            contact.organization_entity_id = entity.id
        self._upsert_contact_domain_note(contact, item)
        return self._link(item, "contact", contact.id, action)

    def _promote_entity(self, item: RoutedItem) -> RoutedPromotionResult:
        entity = self._upsert_entity(item.title, item)
        self._upsert_entity_domain_note(entity, item)
        return self._link(item, "entity", entity.id, "upserted")

    def _promote_idea(self, item: RoutedItem, *, object_type: str = "idea") -> RoutedPromotionResult:
        idea = Idea(
            domain_id=item.domain_id,
            title=item.title,
            content=item.content,
            status="open",
            source_refs=item.source_refs,
            provenance=self._provenance(item),
            metadata_=self._canonical_metadata(item),
        )
        self.session.add(idea)
        self.session.flush()
        return self._link(item, object_type, idea.id, "created")

    def _promote_decision(self, item: RoutedItem) -> RoutedPromotionResult:
        decision = DecisionRecord(
            domain_id=item.domain_id,
            title=item.title,
            decision=item.content,
            rationale=str((item.metadata_ or {}).get("rationale") or "") or None,
            source_refs=item.source_refs,
            provenance=self._provenance(item),
            metadata_=self._canonical_metadata(item),
        )
        self.session.add(decision)
        self.session.flush()
        return self._link(item, "decision", decision.id, "created")

    def _upsert_entity(self, name: str, item: RoutedItem) -> Entity:
        normalized = _normalize_key(name)
        entity = self.session.scalar(select(Entity).where(Entity.normalized_name == normalized))
        if entity is None:
            entity = Entity(
                name=name.strip() or item.title,
                normalized_name=normalized,
                summary=item.content,
                source_refs=item.source_refs,
                provenance=self._provenance(item),
                metadata_=self._canonical_metadata(item),
            )
            self.session.add(entity)
            self.session.flush()
        else:
            entity.summary = _append_note(entity.summary, item.content)
            entity.source_refs = _merge_source_refs(entity.source_refs, item.source_refs)
            entity.metadata_ = {**(entity.metadata_ or {}), **self._canonical_metadata(item)}
        return entity

    def _upsert_contact_domain_note(self, contact: Contact, item: RoutedItem) -> None:
        note = self.session.scalar(
            select(ContactDomainNote).where(
                ContactDomainNote.contact_id == contact.id,
                ContactDomainNote.domain_id == item.domain_id,
            )
        )
        entry = self._interaction_entry(item)
        if note is None:
            note = ContactDomainNote(
                contact_id=contact.id,
                domain_id=item.domain_id,
                notes=item.content,
                interaction_log=[entry],
                source_refs=item.source_refs,
                metadata_=self._canonical_metadata(item),
            )
            self.session.add(note)
        else:
            note.notes = _append_note(note.notes, item.content)
            note.interaction_log = [*(note.interaction_log or []), entry]
            note.source_refs = _merge_source_refs(note.source_refs, item.source_refs)
            note.metadata_ = {**(note.metadata_ or {}), **self._canonical_metadata(item)}

    def _upsert_entity_domain_note(self, entity: Entity, item: RoutedItem) -> None:
        note = self.session.scalar(
            select(EntityDomainNote).where(
                EntityDomainNote.entity_id == entity.id,
                EntityDomainNote.domain_id == item.domain_id,
            )
        )
        entry = self._interaction_entry(item)
        if note is None:
            note = EntityDomainNote(
                entity_id=entity.id,
                domain_id=item.domain_id,
                notes=item.content,
                interaction_log=[entry],
                source_refs=item.source_refs,
                metadata_=self._canonical_metadata(item),
            )
            self.session.add(note)
        else:
            note.notes = _append_note(note.notes, item.content)
            note.interaction_log = [*(note.interaction_log or []), entry]
            note.source_refs = _merge_source_refs(note.source_refs, item.source_refs)
            note.metadata_ = {**(note.metadata_ or {}), **self._canonical_metadata(item)}

    def _link(
        self,
        item: RoutedItem,
        object_type: str,
        object_id: uuid.UUID,
        action: str,
    ) -> RoutedPromotionResult:
        self.session.add(
            RoutedObjectLink(
                routed_item_id=item.id,
                object_type=object_type,
                object_id=object_id,
            )
        )
        self.session.add(
            RoutedObjectChangeLog(
                object_type=object_type,
                object_id=object_id,
                routed_item_id=item.id,
                action=action,
                changes={
                    "route_type": item.route_type,
                    "title": item.title,
                    "priority": item.priority,
                    "status": item.status,
                },
                source_refs=item.source_refs,
                metadata_=self._canonical_metadata(item),
            )
        )
        item.metadata_ = {
            **(item.metadata_ or {}),
            "canonical_object_type": object_type,
            "canonical_object_id": str(object_id),
            "canonical_promotion_action": action,
            "canonical_promoted_at": datetime.now(UTC).isoformat(),
        }
        return RoutedPromotionResult(
            routed_item_id=item.id,
            route_type=item.route_type,
            object_type=object_type,
            object_id=object_id,
            action=action,
        )

    def _has_link(self, item: RoutedItem) -> bool:
        return self.session.scalar(
            select(RoutedObjectLink.id).where(RoutedObjectLink.routed_item_id == item.id).limit(1)
        ) is not None

    def _canonical_metadata(self, item: RoutedItem) -> dict[str, Any]:
        return {
            "routed_item_id": str(item.id),
            "route_type": item.route_type,
            "routed_priority": item.priority,
            **(item.metadata_ or {}),
        }

    def _provenance(self, item: RoutedItem) -> dict[str, Any]:
        return {
            "created_from": "routed_item",
            "routed_item_id": str(item.id),
            "task_id": str(item.task_id) if item.task_id else None,
            "report_id": str(item.report_id) if item.report_id else None,
            "artifact_id": str(item.artifact_id) if item.artifact_id else None,
            "seed_package_id": str(item.seed_package_id) if item.seed_package_id else None,
            "source_refs": item.source_refs,
        }

    def _interaction_entry(self, item: RoutedItem) -> dict[str, Any]:
        return {
            "routed_item_id": str(item.id),
            "title": item.title,
            "content": item.content,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "source_refs": item.source_refs,
        }

    def _events(self, domain_id: uuid.UUID | None, query: str, limit: int) -> list[CalendarEvent]:
        statement = select(CalendarEvent).where(CalendarEvent.status != "archived")
        if domain_id is not None:
            statement = statement.where(CalendarEvent.domain_id == domain_id)
        if query:
            statement = statement.where(_text_match(CalendarEvent.title, CalendarEvent.summary, query=query))
        return list(self.session.scalars(statement.order_by(CalendarEvent.start_at, CalendarEvent.created_at.desc()).limit(limit)).all())

    def _todos(self, domain_id: uuid.UUID | None, query: str, limit: int) -> list[Todo]:
        statement = select(Todo).where(Todo.status.notin_(["done", "archived"]))
        if domain_id is not None:
            statement = statement.where(Todo.domain_id == domain_id)
        if query:
            statement = statement.where(_text_match(Todo.title, Todo.description, query=query))
        return list(self.session.scalars(statement.order_by(Todo.due_at, Todo.created_at.desc()).limit(limit)).all())

    def _contacts(self, domain_id: uuid.UUID | None, query: str, limit: int) -> list[Contact]:
        statement = select(Contact).where(Contact.status != "archived")
        if domain_id is not None:
            statement = statement.join(ContactDomainNote, ContactDomainNote.contact_id == Contact.id).where(
                ContactDomainNote.domain_id == domain_id
            )
        if query:
            statement = statement.where(_text_match(Contact.name, Contact.summary, query=query))
        return list(self.session.scalars(statement.order_by(Contact.updated_at.desc()).limit(limit)).all())

    def _entities(self, domain_id: uuid.UUID | None, query: str, limit: int) -> list[Entity]:
        statement = select(Entity).where(Entity.status != "archived")
        if domain_id is not None:
            statement = statement.join(EntityDomainNote, EntityDomainNote.entity_id == Entity.id).where(
                EntityDomainNote.domain_id == domain_id
            )
        if query:
            statement = statement.where(_text_match(Entity.name, Entity.summary, query=query))
        return list(self.session.scalars(statement.order_by(Entity.updated_at.desc()).limit(limit)).all())

    def _ideas(self, domain_id: uuid.UUID | None, query: str, limit: int) -> list[Idea]:
        statement = select(Idea).where(Idea.status.notin_(["done", "archived"]))
        if domain_id is not None:
            statement = statement.where(Idea.domain_id == domain_id)
        if query:
            statement = statement.where(_text_match(Idea.title, Idea.content, query=query))
        return list(self.session.scalars(statement.order_by(Idea.updated_at.desc()).limit(limit)).all())

    def _decisions(self, domain_id: uuid.UUID | None, query: str, limit: int) -> list[DecisionRecord]:
        statement = select(DecisionRecord).where(DecisionRecord.status != "archived")
        if domain_id is not None:
            statement = statement.where(DecisionRecord.domain_id == domain_id)
        if query:
            statement = statement.where(_text_match(DecisionRecord.title, DecisionRecord.decision, query=query))
        return list(self.session.scalars(statement.order_by(DecisionRecord.updated_at.desc()).limit(limit)).all())

    def _event_payload(self, item: CalendarEvent) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "title": item.title,
            "summary": item.summary,
            "start_at": item.start_at.isoformat() if item.start_at else None,
            "end_at": item.end_at.isoformat() if item.end_at else None,
            "location": item.location,
            "status": item.status,
            "metadata": item.metadata_,
        }

    def _todo_payload(self, item: Todo) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "title": item.title,
            "description": item.description,
            "todo_type": item.todo_type,
            "owner_type": item.owner_type,
            "due_at": item.due_at.isoformat() if item.due_at else None,
            "priority": item.priority,
            "status": item.status,
            "metadata": item.metadata_,
        }

    def _contact_payload(self, item: Contact) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "name": item.name,
            "email": item.email,
            "phone": item.phone,
            "linkedin": item.linkedin,
            "summary": item.summary,
            "status": item.status,
            "metadata": item.metadata_,
        }

    def _entity_payload(self, item: Entity) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "name": item.name,
            "website": item.website,
            "summary": item.summary,
            "status": item.status,
            "metadata": item.metadata_,
        }

    def _idea_payload(self, item: Idea) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "title": item.title,
            "content": item.content,
            "status": item.status,
            "metadata": item.metadata_,
        }

    def _decision_payload(self, item: DecisionRecord) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "title": item.title,
            "decision": item.decision,
            "rationale": item.rationale,
            "status": item.status,
            "metadata": item.metadata_,
        }


def _text_match(*columns, query: str):
    pattern = f"%{query}%"
    return or_(*(column.ilike(pattern) for column in columns))


def _normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", normalized) or "unknown"


def _name_from_title(title: str) -> str:
    title = title.strip()
    for prefix in ("contact:", "new contact:", "person:"):
        if title.lower().startswith(prefix):
            return title[len(prefix):].strip() or title
    return title or "Unknown contact"


def _name_from_content(content: str) -> str | None:
    match = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s+(?:is|serves|works|prefers|leads)\b", content)
    return match.group(1).strip() if match else None


def _email_from_text(text: str) -> str | None:
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    return match.group(0).lower() if match else None


def _phone_from_text(text: str) -> str | None:
    match = re.search(r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}", text)
    return match.group(0) if match else None


def _linkedin_from_text(text: str) -> str | None:
    match = re.search(r"https?://(?:www\.)?linkedin\.com/[^\s)]+", text)
    return match.group(0) if match else None


def _organization_from_text(text: str) -> str | None:
    match = re.search(
        r"(?:at|from|with|works for|partner lead at)\s+([A-Z][A-Za-z0-9&.\- ]{2,80}?)(?:\.|\s{2,}|,|;|$)",
        text,
    )
    return match.group(1).strip(" .") if match else None


def _datetime_from_metadata(metadata: dict[str, Any], key: str) -> datetime | None:
    value = metadata.get(key)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _string_from_metadata(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list_from_metadata(metadata: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = metadata.get(key)
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"value": str(item)} for item in value]
    if value:
        return [{"value": str(value)}]
    return []


def _append_note(existing: str | None, addition: str) -> str:
    addition = addition.strip()
    if not existing:
        return addition
    if addition and addition not in existing:
        return f"{existing}\n\n{addition}"
    return existing


def _merge_source_refs(existing: list[dict[str, Any]], new_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = list(existing or [])
    for ref in new_refs or []:
        if ref not in merged:
            merged.append(ref)
    return merged
