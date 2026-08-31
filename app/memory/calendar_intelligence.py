"""Calendar aggregation, attendee linkage, conflicts, and contact interaction materialization."""

import html
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.identity import is_maestro_user_reference
from app.core.time import home_isoformat, home_timezone
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
from app.memory.calendar_recurrence import event_occurs_on, query_calendar_date
from app.memory.event_work_links import EventWorkLinkService


@dataclass(frozen=True)
class CalendarSearchResult:
    event: CalendarEvent
    score: float
    match_reasons: list[str]
    occurrence_start_at: datetime | None
    occurrence_end_at: datetime | None


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

    def search(
        self,
        query_text: str,
        *,
        domain_id: uuid.UUID | None = None,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[CalendarSearchResult]:
        """Rank calendar records using title, time, date, domain, and recurrence evidence."""
        statement = select(CalendarEvent).where(CalendarEvent.status != "archived")
        if domain_id is not None:
            statement = statement.where(CalendarEvent.domain_id == domain_id)
        events = list(self.session.scalars(statement).all())
        query = " ".join((query_text or "").split())
        local_now = (now or datetime.now(UTC)).astimezone(home_timezone())
        target_date = query_calendar_date(query, now=local_now) if query else None
        target_time = _query_calendar_time(query)
        query_tokens = _calendar_tokens(query)
        results: list[CalendarSearchResult] = []
        for event in events:
            occurrence_start, occurrence_end = _matching_occurrence(event, target_date)
            if target_date is not None and occurrence_start is None:
                continue
            domain_key = self._domain_key(event.domain_id) or ""
            event_tokens = _calendar_tokens(f"{_calendar_search_text(event)} {domain_key}")
            title_tokens = _calendar_tokens(event.title)
            title_overlap = (
                len(query_tokens & title_tokens) / len(title_tokens) if title_tokens else 0.0
            )
            context_overlap = (
                len(query_tokens & event_tokens) / len(query_tokens) if query_tokens else 0.0
            )
            score = 0.52 * title_overlap + 0.18 * context_overlap
            reasons: list[str] = []
            normalized_query = _normalize(query)
            normalized_title = _normalize(event.title)
            if normalized_title and normalized_title in normalized_query:
                score += 0.18
                reasons.append("title phrase")
            elif title_overlap:
                reasons.append(f"title tokens {title_overlap:.0%}")
            if context_overlap and not title_overlap:
                reasons.append(f"event context {context_overlap:.0%}")
            if target_date is not None and occurrence_start is not None:
                score += 0.2
                reasons.append(f"occurs on {target_date.isoformat()}")
            if target_time is not None and occurrence_start is not None:
                local_occurrence = occurrence_start.astimezone(_event_timezone(event))
                difference = abs(
                    local_occurrence.hour * 60
                    + local_occurrence.minute
                    - (target_time.hour * 60 + target_time.minute)
                )
                time_score = max(0.0, 1.0 - difference / 180.0)
                score += 0.22 * time_score
                if difference <= 30:
                    reasons.append(
                        f"time {local_occurrence.strftime('%-I:%M %p')}"
                    )
            elif occurrence_start is not None and target_date is None:
                days_away = abs((occurrence_start.astimezone(home_timezone()) - local_now).days)
                score += 0.08 * max(0.0, 1.0 - days_away / 30.0)
            if domain_id is not None:
                reasons.append("domain match")
            elif domain_key and domain_key in query_tokens:
                score += 0.08
                reasons.append(f"domain reference {domain_key}")
            if event.recurrence_rule and target_date is not None:
                reasons.append("recurring occurrence")
            if not query:
                score = 1.0
            if score >= 0.12:
                results.append(
                    CalendarSearchResult(
                        event=event,
                        score=round(min(score, 1.0), 4),
                        match_reasons=reasons,
                        occurrence_start_at=occurrence_start or event.start_at,
                        occurrence_end_at=occurrence_end or event.end_at,
                    )
                )
        results.sort(
            key=lambda result: (
                -result.score,
                result.occurrence_start_at or datetime.max.replace(tzinfo=UTC),
                result.event.title.lower(),
            )
        )
        return results[:limit]

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
            contact_relevance = str(attendee.get("contact_relevance") or "engaged")
            counts_as_interaction = contact_relevance != "roster_only"
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
                metadata_={
                    "legacy": attendee,
                    "contact_relevance": contact_relevance,
                    "counts_as_interaction": counts_as_interaction,
                },
            )
            self.session.add(record)
            legacy_attendee: dict[str, Any] = {"name": display_name}
            if record.email:
                legacy_attendee["email"] = record.email
            if contact is not None:
                legacy_attendee["contact_id"] = str(contact.id)
            if is_user:
                legacy_attendee.update({"is_user": True, "identity": "maestro_user"})
            if contact_relevance == "roster_only":
                legacy_attendee["contact_relevance"] = "roster_only"
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
            if (attendee.metadata_ or {}).get("counts_as_interaction") is False:
                continue
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
            "contact_relevance": (attendee.metadata_ or {}).get("contact_relevance", "engaged"),
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


_CALENDAR_QUERY_STOPWORDS = {
    "a",
    "am",
    "an",
    "and",
    "at",
    "calendar",
    "called",
    "change",
    "eastern",
    "est",
    "et",
    "event",
    "for",
    "from",
    "in",
    "meeting",
    "move",
    "my",
    "on",
    "only",
    "please",
    "pm",
    "scheduled",
    "shift",
    "the",
    "this",
    "time",
    "to",
    "today",
    "tomorrow",
}


def _calendar_search_text(event: CalendarEvent) -> str:
    attendee_text = " ".join(
        str(item.get("name") or item.get("email") or item.get("value") or "")
        for item in (event.attendees or [])
        if isinstance(item, dict)
    )
    return " ".join(
        str(value or "")
        for value in (
            event.title,
            event.summary,
            event.location,
            event.organizer_name,
            event.organizer_email,
            attendee_text,
        )
    )


def _calendar_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if len(token) > 1
        and token not in _CALENDAR_QUERY_STOPWORDS
        and not token.isdigit()
    }


def _query_calendar_time(query: str) -> time | None:
    normalized = " ".join((query or "").lower().split())
    colon = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", normalized)
    if colon:
        return _clock_time(int(colon.group(1)), int(colon.group(2)), colon.group(3))
    meridiem = re.search(r"\b(\d{1,2})\s*(am|pm|est|edt|et)\b", normalized)
    if meridiem:
        marker = meridiem.group(2) if meridiem.group(2) in {"am", "pm"} else None
        return _clock_time(int(meridiem.group(1)), 0, marker)
    compact = re.search(
        r"\b(?:at|from|around|to)\s+([01]?\d|2[0-3])([0-5]\d)\s*(?:est|edt|et)?\b",
        normalized,
    )
    if compact:
        return _clock_time(int(compact.group(1)), int(compact.group(2)), None)
    return None


def _clock_time(hour: int, minute: int, meridiem: str | None) -> time | None:
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if not 0 <= hour <= 23:
        return None
    return time(hour, minute)


def _matching_occurrence(
    event: CalendarEvent,
    target_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    if event.start_at is None:
        return None, None
    start = _aware(event.start_at)
    end = _aware(event.end_at) if event.end_at else None
    if target_date is None:
        return start, end
    if not event_occurs_on(
        start_at=start,
        recurrence_rule=event.recurrence_rule,
        target_date=target_date,
        timezone_name=event.timezone,
    ):
        return None, None
    timezone = _event_timezone(event)
    local_start = start.astimezone(timezone)
    occurrence_start = datetime.combine(target_date, local_start.timetz().replace(tzinfo=None), timezone)
    occurrence_start = occurrence_start.astimezone(UTC)
    if _is_excluded_occurrence(event, occurrence_start):
        return None, None
    duration = end - start if end is not None else None
    return occurrence_start, occurrence_start + duration if duration else None


def _is_excluded_occurrence(event: CalendarEvent, occurrence_start: datetime) -> bool:
    for raw in (event.metadata_ or {}).get("recurrence_exdates") or []:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if _aware(parsed) == occurrence_start:
            return True
    return False


def _event_timezone(event: CalendarEvent) -> ZoneInfo:
    try:
        return ZoneInfo(event.timezone or "America/New_York")
    except ZoneInfoNotFoundError:
        return home_timezone()


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
