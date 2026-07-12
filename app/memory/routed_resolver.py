import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib import error, request

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import CalendarEvent, Contact, ContactAlias, ContactDomainNote, Todo, RoutedItem
from app.prompts import load_prompt


@dataclass(frozen=True)
class ResolutionCandidate:
    object_type: str
    object_id: uuid.UUID
    score: float
    strategy: str
    reason: str


@dataclass(frozen=True)
class ResolutionDecision:
    action: str
    object_type: str
    object_id: uuid.UUID | None
    confidence: float
    strategy: str
    reason: str
    candidates: list[ResolutionCandidate]


class RoutedResolverLLM(Protocol):
    def choose_match(
        self,
        *,
        item: RoutedItem,
        object_type: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        pass


class OllamaRoutedResolverLLM:
    def __init__(self, *, model: str | None = None, base_url: str | None = None, timeout_seconds: float = 8.0):
        settings = get_settings()
        self.model = model or settings.routed_resolver_llm_model
        self.base_url = (base_url or settings.routed_resolver_llm_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def choose_match(
        self,
        *,
        item: RoutedItem,
        object_type: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": load_prompt("routed_item_resolution.md"),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "object_type": object_type,
                            "new_item": {
                                "route_type": item.route_type,
                                "title": item.title,
                                "content": item.content,
                                "domain_id": str(item.domain_id) if item.domain_id else None,
                                "metadata": item.metadata_ or {},
                            },
                            "candidates": candidates,
                        },
                        default=str,
                    ),
                },
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, TimeoutError, json.JSONDecodeError):
            return None
        message = body.get("message") if isinstance(body, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not content:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None


class LLMResolverResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    object_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class RoutedObjectResolver:
    """Resolves routed item identity before canonical object writes."""

    def __init__(
        self,
        session: Session,
        *,
        llm: RoutedResolverLLM | None = None,
        enable_llm: bool = True,
    ):
        self.session = session
        settings = get_settings()
        self.llm = llm
        if enable_llm and self.llm is None and settings.routed_resolver_llm_provider == "ollama":
            self.llm = OllamaRoutedResolverLLM()

    def resolve_contact(
        self,
        item: RoutedItem,
        *,
        name: str,
        email: str | None,
    ) -> ResolutionDecision:
        normalized = _normalize_key(name)
        candidates: list[ResolutionCandidate] = []
        if email:
            contact = self.session.scalar(select(Contact).where(Contact.email == email))
            if contact is not None:
                return self._decision("update_existing", "contact", contact.id, 0.99, "email", "Exact email match.", [])

        contacts = list(
            self.session.scalars(
                select(Contact).where(Contact.status != "archived").order_by(Contact.updated_at.desc()).limit(50)
            )
        )
        for contact in contacts:
            score, strategy, reason = self._score_contact(item, contact, normalized, name)
            if score >= 0.45:
                candidates.append(ResolutionCandidate("contact", contact.id, score, strategy, reason))
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)

        if candidates and candidates[0].score >= 0.92:
            top = candidates[0]
            return self._decision("update_existing", "contact", top.object_id, top.score, top.strategy, top.reason, candidates)
        if candidates and candidates[0].score >= 0.74 and (len(candidates) == 1 or candidates[0].score - candidates[1].score >= 0.18):
            top = candidates[0]
            return self._decision("update_existing", "contact", top.object_id, top.score, top.strategy, top.reason, candidates)
        return self._llm_or_create(item, "contact", candidates)

    def resolve_event(
        self,
        item: RoutedItem,
        *,
        start_at: datetime | None,
    ) -> ResolutionDecision:
        candidates: list[ResolutionCandidate] = []
        statement = select(CalendarEvent).where(CalendarEvent.status != "archived")
        if item.domain_id is not None:
            statement = statement.where(CalendarEvent.domain_id == item.domain_id)
        events = list(self.session.scalars(statement.order_by(CalendarEvent.updated_at.desc()).limit(50)))
        for event in events:
            score, strategy, reason = self._score_event(item, event, start_at)
            if score >= 0.45:
                candidates.append(ResolutionCandidate("event", event.id, score, strategy, reason))
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        if candidates and candidates[0].score >= 0.9:
            top = candidates[0]
            return self._decision("update_existing", "event", top.object_id, top.score, top.strategy, top.reason, candidates)
        if candidates and candidates[0].score >= 0.76 and (len(candidates) == 1 or candidates[0].score - candidates[1].score >= 0.16):
            top = candidates[0]
            return self._decision("update_existing", "event", top.object_id, top.score, top.strategy, top.reason, candidates)
        return self._llm_or_create(item, "event", candidates)

    def resolve_todo(self, item: RoutedItem, *, due_at: datetime | None) -> ResolutionDecision:
        candidates: list[ResolutionCandidate] = []
        statement = select(Todo).where(Todo.status.notin_(["done", "archived"]))
        if item.domain_id is not None:
            statement = statement.where(Todo.domain_id == item.domain_id)
        todos = list(self.session.scalars(statement.order_by(Todo.updated_at.desc()).limit(50)))
        for todo in todos:
            score, strategy, reason = self._score_todo(item, todo, due_at)
            if score >= 0.5:
                candidates.append(ResolutionCandidate("todo", todo.id, score, strategy, reason))
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        if candidates and candidates[0].score >= 0.88:
            top = candidates[0]
            return self._decision("update_existing", "todo", top.object_id, top.score, top.strategy, top.reason, candidates)
        return self._llm_or_create(item, "todo", candidates)

    def _score_contact(self, item: RoutedItem, contact: Contact, normalized: str, name: str) -> tuple[float, str, str]:
        aliases = _contact_aliases(contact)
        aliases.update(
            self.session.scalars(
                select(ContactAlias.normalized_alias).where(ContactAlias.contact_id == contact.id)
            ).all()
        )
        if normalized and normalized in aliases:
            return 0.95, "alias", "Matched stored contact alias."
        contact_name = _normalize_key(contact.name)
        if normalized == contact_name:
            return 0.94, "normalized_name", "Exact normalized name match."
        if _initial_alias_match(normalized, contact_name):
            return 0.86, "initial_alias", "Matched first name plus last initial."
        item_org = _organization_from_text(item.content) or _string_from_metadata(item.metadata_ or {}, "organization")
        contact_org = _string_from_metadata(contact.metadata_ or {}, "organization")
        if item_org and contact_org and _normalize_key(item_org) == _normalize_key(contact_org):
            overlap = _token_similarity(name, contact.name)
            if overlap >= 0.45:
                return 0.78, "organization_context", "Name and organization context overlap."
        if _first_name(name) and _first_name(name) == _first_name(contact.name):
            domain_score = self._contact_domain_score(contact, item)
            if domain_score >= 0.2 and _token_similarity(item.content, contact.summary or contact.name) >= 0.35:
                return 0.74 + domain_score, "domain_context", "Unique first name in the same domain context."
        similarity = _token_similarity(f"{item.title} {item.content}", f"{contact.name} {contact.summary or ''}")
        return similarity, "lexical", "Lexical overlap with existing contact."

    def _score_event(self, item: RoutedItem, event: CalendarEvent, start_at: datetime | None) -> tuple[float, str, str]:
        title_similarity = _token_similarity(item.title, event.title)
        content_similarity = _token_similarity(item.content, f"{event.title} {event.summary or ''}")
        participant_similarity = _event_participant_similarity(item, event)
        if start_at and event.start_at:
            delta = abs(_aware(event.start_at) - _aware(start_at))
            if delta <= timedelta(minutes=5) and title_similarity >= 0.5:
                return 0.94, "time_title", "Same event time and similar title."
            if delta <= timedelta(hours=2) and content_similarity >= 0.55:
                return 0.82, "near_time_context", "Nearby event time and similar context."
            if delta <= timedelta(hours=2) and participant_similarity >= 0.65:
                return 0.86, "near_time_participant", "Nearby event time and matching participant."
        if start_at and not event.start_at and participant_similarity >= 0.65:
            return 0.88, "fills_missing_time_participant", "Follow-up supplies time for matching incomplete event."
        if participant_similarity >= 0.8 and max(title_similarity, content_similarity) >= 0.45:
            return 0.82, "participant_context", "Matching participant and event context."
        if title_similarity >= 0.86:
            return 0.83, "title", "Strong event title match."
        if content_similarity >= 0.72:
            return 0.76, "event_context", "Strong event context match."
        return max(title_similarity, content_similarity, participant_similarity * 0.7), "lexical", "Lexical event overlap."

    def _score_todo(self, item: RoutedItem, todo: Todo, due_at: datetime | None) -> tuple[float, str, str]:
        title_similarity = _token_similarity(item.title, todo.title)
        content_similarity = _token_similarity(item.content, f"{todo.title} {todo.description}")
        if due_at and todo.due_at and abs(_aware(todo.due_at) - _aware(due_at)) <= timedelta(hours=2):
            if title_similarity >= 0.65:
                return 0.9, "due_time_title", "Same due time and similar todo title."
            if content_similarity >= 0.55:
                return 0.88, "due_time_context", "Similar todo with nearby due time."
        if title_similarity >= 0.86:
            return 0.86, "title", "Strong todo title match."
        if content_similarity >= 0.76:
            return 0.78, "todo_context", "Strong todo context match."
        return max(title_similarity, content_similarity), "lexical", "Lexical todo overlap."

    def _contact_domain_score(self, contact: Contact, item: RoutedItem) -> float:
        if item.domain_id is None:
            return 0.0
        note = self.session.scalar(
            select(ContactDomainNote).where(
                ContactDomainNote.contact_id == contact.id,
                ContactDomainNote.domain_id == item.domain_id,
            )
        )
        return 0.1 if note is not None else 0.0

    def _llm_or_create(
        self,
        item: RoutedItem,
        object_type: str,
        candidates: list[ResolutionCandidate],
    ) -> ResolutionDecision:
        llm_decision = self._llm_decision(item, object_type, candidates)
        if llm_decision is not None:
            return llm_decision
        return self._decision("create_new", object_type, None, 0.0, "no_match", "No confident existing routed object match.", candidates)

    def _llm_decision(
        self,
        item: RoutedItem,
        object_type: str,
        candidates: list[ResolutionCandidate],
    ) -> ResolutionDecision | None:
        if self.llm is None or not candidates:
            return None
        candidate_payloads = [self._candidate_payload(candidate) for candidate in candidates[:5]]
        raw = self.llm.choose_match(item=item, object_type=object_type, candidates=candidate_payloads)
        if raw is None:
            return None
        try:
            response = LLMResolverResponse.model_validate(raw)
        except ValidationError:
            return None
        if response.action not in {"update_existing", "create_new", "needs_review"}:
            return None
        object_id = None
        if response.object_id:
            try:
                object_id = uuid.UUID(response.object_id)
            except ValueError:
                return None
        candidate_ids = {candidate.object_id for candidate in candidates}
        if response.action == "update_existing" and object_id not in candidate_ids:
            return None
        if response.action == "update_existing" and response.confidence < 0.72:
            return None
        return self._decision(
            response.action,
            object_type,
            object_id,
            response.confidence,
            "llm_resolver",
            response.reason,
            candidates,
        )

    def _candidate_payload(self, candidate: ResolutionCandidate) -> dict[str, Any]:
        if candidate.object_type == "contact":
            contact = self.session.get(Contact, candidate.object_id)
            if contact is None:
                return {}
            return {
                "object_id": str(contact.id),
                "name": contact.name,
                "email": contact.email,
                "summary": contact.summary,
                "aliases": sorted(_contact_aliases(contact)),
                "score": candidate.score,
                "strategy": candidate.strategy,
                "reason": candidate.reason,
            }
        if candidate.object_type == "event":
            event = self.session.get(CalendarEvent, candidate.object_id)
            if event is None:
                return {}
            return {
                "object_id": str(event.id),
                "title": event.title,
                "summary": event.summary,
                "start_at": event.start_at.isoformat() if event.start_at else None,
                "score": candidate.score,
                "strategy": candidate.strategy,
                "reason": candidate.reason,
            }
        if candidate.object_type == "todo":
            todo = self.session.get(Todo, candidate.object_id)
            if todo is None:
                return {}
            return {
                "object_id": str(todo.id),
                "title": todo.title,
                "description": todo.description,
                "due_at": todo.due_at.isoformat() if todo.due_at else None,
                "score": candidate.score,
                "strategy": candidate.strategy,
                "reason": candidate.reason,
            }
        return {}

    def _decision(
        self,
        action: str,
        object_type: str,
        object_id: uuid.UUID | None,
        confidence: float,
        strategy: str,
        reason: str,
        candidates: list[ResolutionCandidate],
    ) -> ResolutionDecision:
        return ResolutionDecision(
            action=action,
            object_type=object_type,
            object_id=object_id,
            confidence=confidence,
            strategy=strategy,
            reason=reason,
            candidates=candidates[:5],
        )


def resolution_metadata(decision: ResolutionDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "object_type": decision.object_type,
        "object_id": str(decision.object_id) if decision.object_id else None,
        "confidence": round(decision.confidence, 3),
        "strategy": decision.strategy,
        "reason": decision.reason,
        "candidates": [
            {
                "object_type": candidate.object_type,
                "object_id": str(candidate.object_id),
                "score": round(candidate.score, 3),
                "strategy": candidate.strategy,
                "reason": candidate.reason,
            }
            for candidate in decision.candidates
        ],
    }


def contact_aliases_for(name: str) -> set[str]:
    normalized = _normalize_key(name)
    aliases = {normalized} if normalized else set()
    parts = normalized.split()
    if len(parts) >= 2:
        aliases.add(f"{parts[0]} {parts[-1][0]}")
        aliases.add(f"{parts[0]} {parts[-1][0]}.")
        aliases.add(" ".join(part[0] for part in parts if part))
    return {alias for alias in aliases if alias}


def _contact_aliases(contact: Contact) -> set[str]:
    aliases = set(contact_aliases_for(contact.name))
    metadata_aliases = (contact.metadata_ or {}).get("aliases") or []
    if isinstance(metadata_aliases, list):
        aliases.update(_normalize_key(str(alias)) for alias in metadata_aliases if str(alias).strip())
    return aliases


def _initial_alias_match(candidate: str, contact_name: str) -> bool:
    candidate_parts = candidate.split()
    contact_parts = contact_name.split()
    if len(candidate_parts) != 2 or len(contact_parts) < 2:
        return False
    return candidate_parts[0] == contact_parts[0] and candidate_parts[1].rstrip(".") == contact_parts[-1][0]


def _first_name(name: str) -> str | None:
    parts = _normalize_key(name).split()
    return parts[0] if parts else None


def _event_participant_similarity(item: RoutedItem, event: CalendarEvent) -> float:
    item_names = _participant_names_from_item(item)
    event_names = _participant_names_from_event(event)
    if not item_names or not event_names:
        return 0.0
    best = 0.0
    for item_name in item_names:
        for event_name in event_names:
            item_tokens = _tokens(item_name)
            event_tokens = _tokens(event_name)
            if not item_tokens or not event_tokens:
                continue
            if item_tokens <= event_tokens or event_tokens <= item_tokens:
                best = max(best, 1.0)
            elif item_tokens & event_tokens:
                best = max(best, len(item_tokens & event_tokens) / min(len(item_tokens), len(event_tokens)))
    return best


def _participant_names_from_item(item: RoutedItem) -> set[str]:
    names = _participant_names_from_attendees((item.metadata_ or {}).get("attendees"))
    names.update(_names_after_meeting_with(item.title))
    names.update(_names_after_meeting_with(item.content))
    return names


def _participant_names_from_event(event: CalendarEvent) -> set[str]:
    names = _participant_names_from_attendees(event.attendees)
    names.update(_names_after_meeting_with(event.title))
    names.update(_names_after_meeting_with(event.summary or ""))
    return names


def _participant_names_from_attendees(attendees: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(attendees, list):
        for attendee in attendees:
            if isinstance(attendee, dict):
                value = attendee.get("name") or attendee.get("value") or attendee.get("email")
            else:
                value = attendee
            if value:
                names.add(str(value))
    elif attendees:
        names.add(str(attendees))
    return {name for name in names if name.strip()}


def _names_after_meeting_with(text: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(
        r"\b(?:meeting|call|sync)\s+with\s+([A-Z][a-z]+(?:\s+[A-Z][A-Za-z.'-]+){0,3})",
        text or "",
    ):
        candidate = re.split(r"\s+(?:about|at|on|for|was|is)\b", match.group(1).strip())[0].strip()
        if candidate:
            names.add(candidate)
    return names


def _token_similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return overlap / max(len(left_tokens), len(right_tokens))


def _tokens(value: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "with", "for", "to", "of", "in", "at", "on", "is", "are"}
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if token not in stop}


def _normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", normalized) or "unknown"


def _organization_from_text(text: str) -> str | None:
    match = re.search(
        r"(?:at|from|with|works for|partner lead at)\s+([A-Z][A-Za-z0-9&.\- ]{2,80}?)(?:\.|\s{2,}|,|;|$)",
        text,
    )
    return match.group(1).strip(" .") if match else None


def _string_from_metadata(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
