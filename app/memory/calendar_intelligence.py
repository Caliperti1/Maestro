"""Calendar aggregation, attendee linkage, conflicts, and contact interaction materialization."""

import html
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.identity import is_maestro_user_reference
from app.core.time import home_isoformat
from app.db.models import (
    CalendarEvent,
    CalendarEventAttendee,
    CalendarEventOrganization,
    Contact,
    ContactAlias,
    ContactInteraction,
    Domain,
    Entity,
    OrganizationAlias,
    OrganizationIdentifier,
    Todo,
)
from app.memory.event_work_links import EventWorkLinkService


class CalendarIntelligenceService:
    def __init__(self, session: Session):
        self.session = session

    def ensure_links(self, event: CalendarEvent) -> None:
        if not event.conferencing_url:
            event.conferencing_url = conferencing_url_from_values(
                event.metadata_,
                event.summary,
                event.location,
                event.supporting_refs,
                event.source_refs,
            )
        has_attendees = self.session.scalar(
            select(CalendarEventAttendee.id).where(CalendarEventAttendee.event_id == event.id).limit(1)
        )
        if has_attendees is None and event.attendees:
            self.replace_attendees(event, event.attendees, commit=False)
        self.materialize_contact_interactions(event, commit=False)
        self.session.flush()

    def replace_attendees(
        self,
        event: CalendarEvent,
        attendees: Any,
        *,
        commit: bool = True,
    ) -> None:
        rows = attendees if isinstance(attendees, list) else []
        self.session.execute(
            delete(CalendarEventAttendee).where(CalendarEventAttendee.event_id == event.id)
        )
        normalized_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows:
            attendee = raw if isinstance(raw, dict) else {"name": str(raw)}
            name, email = _attendee_identity(attendee)
            if not name and not email:
                continue
            is_user = bool(attendee.get("is_user")) or is_maestro_user_reference(
                name=name,
                email=email,
            )
            contact = None if is_user else self._resolve_contact(attendee, name=name, email=email)
            display_name = contact.name if contact is not None else (name or email or "Unknown attendee")
            normalized_identity = (
                f"contact:{contact.id}"
                if contact is not None
                else "maestro_user"
                if is_user
                else f"email:{email.lower()}"
                if email
                else f"name:{_normalize(display_name)}"
            )
            if normalized_identity in seen:
                continue
            seen.add(normalized_identity)
            record = CalendarEventAttendee(
                event_id=event.id,
                contact_id=contact.id if contact is not None else None,
                name=display_name,
                email=email or (contact.email if contact is not None else None),
                normalized_identity=normalized_identity,
                attendee_type=str(attendee.get("attendee_type") or "required"),
                response_status=str(attendee.get("response_status") or "needs_action"),
                is_organizer=bool(attendee.get("is_organizer")),
                is_user=is_user,
                source_refs=event.source_refs,
                metadata_={"legacy": attendee},
            )
            self.session.add(record)
            legacy_attendee: dict[str, Any] = {"name": display_name}
            if record.email:
                legacy_attendee["email"] = record.email
            if contact is not None:
                legacy_attendee["contact_id"] = str(contact.id)
            if is_user:
                legacy_attendee.update({"is_user": True, "identity": "maestro_user"})
            normalized_rows.append(legacy_attendee)
        event.attendees = normalized_rows
        self.materialize_contact_interactions(event, commit=False)
        if commit:
            self.session.commit()

    def replace_organizations(
        self,
        event: CalendarEvent,
        organizations: Any,
        *,
        commit: bool = True,
    ) -> None:
        values = organizations if isinstance(organizations, list) else []
        self.session.execute(
            delete(CalendarEventOrganization).where(CalendarEventOrganization.event_id == event.id)
        )
        seen: set[tuple[uuid.UUID, str]] = set()
        for raw in values:
            data = raw if isinstance(raw, dict) else {"name": str(raw)}
            entity = self._resolve_organization(data)
            if entity is None:
                continue
            role = str(data.get("role") or "related")
            key = (entity.id, role)
            if key in seen:
                continue
            seen.add(key)
            self.session.add(
                CalendarEventOrganization(
                    event_id=event.id,
                    entity_id=entity.id,
                    role=role,
                    source_refs=event.source_refs,
                    metadata_={},
                )
            )
        if commit:
            self.session.commit()

    def materialize_contact_interactions(
        self,
        event: CalendarEvent,
        *,
        commit: bool = True,
    ) -> int:
        if event.start_at is None or event.item_kind != "event":
            return 0
        start_at = _aware(event.start_at)
        occurred = start_at <= datetime.now(UTC)
        if not occurred:
            return 0
        created = 0
        attendees = self.session.scalars(
            select(CalendarEventAttendee).where(
                CalendarEventAttendee.event_id == event.id,
                CalendarEventAttendee.contact_id.is_not(None),
            )
        ).all()
        for attendee in attendees:
            existing = self.session.scalar(
                select(ContactInteraction).where(
                    ContactInteraction.contact_id == attendee.contact_id,
                    ContactInteraction.calendar_event_id == event.id,
                )
            )
            if existing is not None:
                continue
            self.session.add(
                ContactInteraction(
                    contact_id=attendee.contact_id,
                    domain_id=event.domain_id,
                    calendar_event_id=event.id,
                    interaction_type="meeting",
                    channel="calendar",
                    occurred_at=start_at,
                    summary=event.summary or event.title,
                    source_refs=event.source_refs,
                    provenance=event.provenance,
                    metadata_={"calendar_event_title": event.title},
                )
            )
            created += 1
        if commit:
            self.session.commit()
        return created

    def event_payload(self, event: CalendarEvent) -> dict[str, Any]:
        self.ensure_links(event)
        attendee_rows = self.session.scalars(
            select(CalendarEventAttendee)
            .where(CalendarEventAttendee.event_id == event.id)
            .order_by(CalendarEventAttendee.is_organizer.desc(), CalendarEventAttendee.name)
        ).all()
        organization_rows = self.session.execute(
            select(CalendarEventOrganization, Entity)
            .join(Entity, Entity.id == CalendarEventOrganization.entity_id)
            .where(CalendarEventOrganization.event_id == event.id)
        ).all()
        todo = self.session.get(Todo, event.todo_id) if event.todo_id else None
        return {
            "id": str(event.id),
            "domain_key": self._domain_key(event.domain_id),
            "title": event.title,
            "summary": event.summary,
            "start_at": home_isoformat(event.start_at),
            "end_at": home_isoformat(event.end_at),
            "timezone": event.timezone,
            "all_day": event.all_day,
            "recurrence_rule": event.recurrence_rule,
            "item_kind": event.item_kind,
            "todo_id": str(event.todo_id) if event.todo_id else None,
            "todo_status": todo.status if todo else None,
            "estimated_minutes": todo.estimated_minutes if todo else None,
            "context_type": event.context_type,
            "scheduling_effect": event.scheduling_effect,
            "blocks_time": event.blocks_time,
            "location": event.location,
            "conferencing_url": event.conferencing_url,
            "organizer_name": event.organizer_name,
            "organizer_email": event.organizer_email,
            "attendees": [self.attendee_payload(row) for row in attendee_rows],
            "organizations": [
                {"id": str(entity.id), "name": entity.name, "role": link.role}
                for link, entity in organization_rows
            ],
            "work_links": EventWorkLinkService(self.session).for_event(event.id),
            "conflicts": self.conflicts(event),
            "supporting_refs": event.supporting_refs,
            "source_refs": event.source_refs,
            "provenance": event.provenance,
            "status": event.status,
            "external_provider": event.external_provider,
            "external_calendar_id": event.external_calendar_id,
            "external_event_id": event.external_event_id,
            "sync_status": event.sync_status,
            "last_synced_at": event.last_synced_at.isoformat() if event.last_synced_at else None,
            "metadata": event.metadata_,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }

    def attendee_payload(
        self,
        attendee: CalendarEventAttendee,
        *,
        contact: Contact | None = None,
    ) -> dict[str, Any]:
        if contact is None and attendee.contact_id is not None:
            contact = self.session.get(Contact, attendee.contact_id)
        return {
            "id": str(attendee.id) if attendee.id else None,
            "contact_id": str(attendee.contact_id) if attendee.contact_id else None,
            "name": contact.name if contact is not None else attendee.name,
            "email": attendee.email or (contact.email if contact is not None else None),
            "attendee_type": attendee.attendee_type,
            "response_status": attendee.response_status,
            "is_organizer": attendee.is_organizer,
            "is_user": attendee.is_user,
        }

    def conflicts(self, event: CalendarEvent) -> list[dict[str, Any]]:
        if event.start_at is None or not event.blocks_time:
            return []
        start = _aware(event.start_at)
        end = _aware(event.end_at) if event.end_at else start + timedelta(hours=1)
        candidates = self.session.scalars(
            select(CalendarEvent).where(
                CalendarEvent.id != event.id,
                CalendarEvent.status.notin_(["archived", "cancelled"]),
                CalendarEvent.blocks_time.is_(True),
                CalendarEvent.start_at.is_not(None),
                CalendarEvent.start_at < end,
                or_(CalendarEvent.end_at.is_(None), CalendarEvent.end_at > start),
            )
        ).all()
        return [
            {
                "id": str(candidate.id),
                "title": candidate.title,
                "domain_key": self._domain_key(candidate.domain_id),
                "start_at": home_isoformat(candidate.start_at),
                "end_at": home_isoformat(candidate.end_at),
            }
            for candidate in candidates
        ]

    def _resolve_contact(
        self,
        attendee: dict[str, Any],
        *,
        name: str | None,
        email: str | None,
    ) -> Contact | None:
        contact_id = attendee.get("contact_id")
        if contact_id:
            try:
                contact = self.session.get(Contact, uuid.UUID(str(contact_id)))
            except ValueError:
                contact = None
            if contact is not None and contact.status != "archived":
                return contact
        if email:
            contact = self.session.scalar(
                select(Contact).where(Contact.email.ilike(email), Contact.status != "archived")
            )
            if contact is not None:
                return contact
        normalized = _normalize(name or "")
        if not normalized:
            return None
        contact = self.session.scalar(
            select(Contact).where(Contact.normalized_name == normalized, Contact.status != "archived")
        )
        if contact is not None:
            return contact
        alias = self.session.scalar(
            select(ContactAlias).where(ContactAlias.normalized_alias == normalized)
        )
        return self.session.get(Contact, alias.contact_id) if alias is not None else None

    def _resolve_organization(self, data: dict[str, Any]) -> Entity | None:
        entity_id = data.get("id") or data.get("entity_id")
        if entity_id:
            try:
                entity = self.session.get(Entity, uuid.UUID(str(entity_id)))
            except ValueError:
                entity = None
            if entity is not None and entity.status != "archived":
                return entity
        website = str(data.get("website") or "").strip()
        email_domain = str(data.get("email_domain") or "").strip()
        for identifier_type, value in (("website", website), ("email_domain", email_domain)):
            if not value:
                continue
            normalized = value.lower().rstrip("/")
            if identifier_type == "email_domain":
                normalized = normalized.removeprefix("www.")
            identifier = self.session.scalar(
                select(OrganizationIdentifier).where(
                    OrganizationIdentifier.identifier_type == identifier_type,
                    OrganizationIdentifier.normalized_value == normalized,
                )
            )
            if identifier is not None:
                return self.session.get(Entity, identifier.entity_id)
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        entity = self.session.scalar(
            select(Entity).where(Entity.normalized_name == _normalize(name), Entity.status != "archived")
        )
        if entity is not None:
            return entity
        alias = self.session.scalar(
            select(OrganizationAlias).where(OrganizationAlias.normalized_alias == _normalize(name))
        )
        return self.session.get(Entity, alias.entity_id) if alias is not None else None

    def _domain_key(self, domain_id: uuid.UUID | None) -> str | None:
        domain = self.session.get(Domain, domain_id) if domain_id else None
        return domain.key if domain else None


_CONFERENCING_METADATA_KEYS = (
    "conferencing_url",
    "conference_url",
    "join_url",
    "meeting_link",
    "meeting_url",
    "hangout_link",
    "hangoutLink",
    "online_meeting_url",
    "onlineMeetingUrl",
    "video_conference_url",
)
_CONFERENCING_HOST_MARKERS = (
    "meet.google.com",
    "zoom.us",
    "teams.microsoft.com",
    "teams.microsoft.us",
    "teams.live.com",
    "webex.com",
    "meet.jit.si",
    "whereby.com",
)


def conferencing_url_from_values(*values: Any) -> str | None:
    """Return the first explicit or recognizable conferencing URL in structured evidence."""
    for value in values:
        candidate = _conferencing_url_from_value(value)
        if candidate:
            return candidate
    return None


def _conferencing_url_from_value(value: Any, *, explicit: bool = False) -> str | None:
    if isinstance(value, dict):
        for key in _CONFERENCING_METADATA_KEYS:
            candidate = _conferencing_url_from_value(value.get(key), explicit=True)
            if candidate:
                return candidate
        for nested in value.values():
            candidate = _conferencing_url_from_value(nested)
            if candidate:
                return candidate
        return None
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            candidate = _conferencing_url_from_value(nested)
            if candidate:
                return candidate
        return None
    if not isinstance(value, str):
        return None
    for match in re.finditer(r"https?://[^\s<>\"']+", html.unescape(value), re.IGNORECASE):
        candidate = match.group(0).rstrip(".,;:!?)]}")
        hostname = (urlsplit(candidate).hostname or "").lower()
        if explicit or any(marker in hostname for marker in _CONFERENCING_HOST_MARKERS):
            return candidate
    return None


def _attendee_identity(attendee: dict[str, Any]) -> tuple[str | None, str | None]:
    value = str(attendee.get("value") or "").strip()
    name = str(attendee.get("name") or "").strip() or None
    email = str(attendee.get("email") or "").strip().lower() or None
    if not email:
        match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value or name or "")
        email = match.group(0).lower() if match else None
    if name and "<" in name:
        name = name.split("<", 1)[0].strip() or None
    if not name and value:
        name = value.split("<", 1)[0].strip() or None
    return name, email


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _aware(value: datetime) -> datetime:
    # Timestamp-with-time-zone columns are normalized to UTC. SQLite drops the tzinfo in tests,
    # so a naive value read from this store must still be treated as UTC rather than local wall time.
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
