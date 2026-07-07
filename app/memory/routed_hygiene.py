import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CalendarEvent, Contact, ContactAlias, RuntimeSetting, Todo
from app.memory.routed_resolver import contact_aliases_for


@dataclass(frozen=True)
class RoutedHygieneReport:
    aliases_backfilled: int
    suggestions: list[dict[str, Any]]


class RoutedHygieneService:
    """Background hygiene for routed-object stores.

    This intentionally proposes duplicate merges instead of applying them automatically.
    """

    SETTING_KEY = "routed_hygiene_latest"

    def __init__(self, session: Session):
        self.session = session

    def run_once(self, *, persist_report: bool = True) -> RoutedHygieneReport:
        aliases_backfilled = self.backfill_contact_aliases()
        suggestions = [
            *self.contact_duplicate_suggestions(),
            *self.event_duplicate_suggestions(),
            *self.todo_duplicate_suggestions(),
        ]
        report = RoutedHygieneReport(aliases_backfilled=aliases_backfilled, suggestions=suggestions)
        if persist_report:
            setting = self.session.get(RuntimeSetting, self.SETTING_KEY)
            payload = {
                "aliases_backfilled": aliases_backfilled,
                "suggestions": suggestions,
            }
            if setting is None:
                setting = RuntimeSetting(key=self.SETTING_KEY, value=payload)
                self.session.add(setting)
            else:
                setting.value = payload
            self.session.commit()
        return report

    def backfill_contact_aliases(self) -> int:
        count = 0
        contacts = self.session.scalars(select(Contact).where(Contact.status != "archived")).all()
        for contact in contacts:
            aliases = set(contact_aliases_for(contact.name))
            metadata_aliases = (contact.metadata_ or {}).get("aliases") or []
            if isinstance(metadata_aliases, list):
                aliases.update(str(alias) for alias in metadata_aliases if str(alias).strip())
            for alias in aliases:
                normalized = _normalize(alias)
                existing = self.session.scalar(
                    select(ContactAlias).where(ContactAlias.normalized_alias == normalized)
                )
                if existing is None:
                    self.session.add(
                        ContactAlias(
                            contact_id=contact.id,
                            alias=alias,
                            normalized_alias=normalized,
                            source="hygiene_backfill",
                            source_refs=[],
                            metadata_={},
                        )
                    )
                    count += 1
        if count:
            self.session.commit()
        return count

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


def _normalize(value: str | None) -> str:
    import re

    normalized = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return re.sub(r"\s+", " ", normalized)
