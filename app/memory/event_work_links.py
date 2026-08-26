"""Canonical planning links between calendar events, todos, and product issues."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import home_isoformat
from app.db.models import CalendarEvent, CalendarEventWorkLink, ProductIssue, Todo

EVENT_WORK_RELATIONSHIPS = {"prerequisite", "during", "follow_up"}
EVENT_WORK_TARGETS = {"todo", "product_issue"}


class EventWorkLinkService:
    def __init__(self, session: Session):
        self.session = session

    def link(
        self,
        *,
        event_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        relationship_type: str,
        notes: str = "",
        provenance: dict[str, Any] | None = None,
    ) -> CalendarEventWorkLink:
        event = self.session.get(CalendarEvent, event_id)
        if event is None:
            raise ValueError("Calendar event not found.")
        target_type = target_type.strip().lower()
        relationship_type = relationship_type.strip().lower()
        if target_type not in EVENT_WORK_TARGETS:
            raise ValueError("Linked work must be a todo or product issue.")
        if relationship_type not in EVENT_WORK_RELATIONSHIPS:
            raise ValueError("Relationship must be prerequisite, during, or follow_up.")

        target_model = Todo if target_type == "todo" else ProductIssue
        target = self.session.get(target_model, target_id)
        if target is None:
            raise ValueError(f"{target_type.replace('_', ' ').title()} not found.")
        if event.domain_id and target.domain_id and event.domain_id != target.domain_id:
            raise ValueError("The event and linked work must belong to the same domain.")

        target_column = (
            CalendarEventWorkLink.todo_id
            if target_type == "todo"
            else CalendarEventWorkLink.product_issue_id
        )
        link = self.session.scalar(
            select(CalendarEventWorkLink).where(
                CalendarEventWorkLink.event_id == event.id,
                target_column == target.id,
            )
        )
        if link is None:
            link = CalendarEventWorkLink(
                event_id=event.id,
                todo_id=target.id if target_type == "todo" else None,
                product_issue_id=target.id if target_type == "product_issue" else None,
                relationship_type=relationship_type,
                notes=notes.strip(),
                provenance=provenance
                or {"source_system": "maestro", "linked_at": datetime.now(UTC).isoformat()},
            )
            self.session.add(link)
        else:
            link.relationship_type = relationship_type
            link.notes = notes.strip()
            link.provenance = {**(link.provenance or {}), **(provenance or {})}
        event.updated_at = datetime.now(UTC)
        target.updated_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(link)
        return link

    def update(
        self,
        link_id: uuid.UUID,
        *,
        relationship_type: str | None = None,
        notes: str | None = None,
    ) -> CalendarEventWorkLink:
        link = self.session.get(CalendarEventWorkLink, link_id)
        if link is None:
            raise ValueError("Event work link not found.")
        if relationship_type is not None:
            normalized = relationship_type.strip().lower()
            if normalized not in EVENT_WORK_RELATIONSHIPS:
                raise ValueError("Relationship must be prerequisite, during, or follow_up.")
            link.relationship_type = normalized
        if notes is not None:
            link.notes = notes.strip()
        self.session.commit()
        self.session.refresh(link)
        return link

    def unlink(self, link_id: uuid.UUID) -> None:
        link = self.session.get(CalendarEventWorkLink, link_id)
        if link is None:
            raise ValueError("Event work link not found.")
        self.session.delete(link)
        self.session.commit()

    def for_event(self, event_id: uuid.UUID) -> list[dict[str, Any]]:
        links = self.session.scalars(
            select(CalendarEventWorkLink)
            .where(CalendarEventWorkLink.event_id == event_id)
            .order_by(CalendarEventWorkLink.created_at)
        ).all()
        return [self.payload(link) for link in links]

    def for_todo(self, todo_id: uuid.UUID) -> list[dict[str, Any]]:
        return self._event_links(CalendarEventWorkLink.todo_id == todo_id)

    def for_issue(self, issue_id: uuid.UUID) -> list[dict[str, Any]]:
        return self._event_links(CalendarEventWorkLink.product_issue_id == issue_id)

    def payload(self, link: CalendarEventWorkLink) -> dict[str, Any]:
        todo = self.session.get(Todo, link.todo_id) if link.todo_id else None
        issue = (
            self.session.get(ProductIssue, link.product_issue_id)
            if link.product_issue_id
            else None
        )
        target = todo or issue
        return {
            "id": str(link.id),
            "event_id": str(link.event_id),
            "target_type": "todo" if todo else "product_issue",
            "target_id": str(target.id) if target else None,
            "title": target.title if target else "Deleted work item",
            "status": target.status if target else "deleted",
            "relationship_type": link.relationship_type,
            "notes": link.notes,
            "estimated_minutes": target.estimated_minutes if target else None,
            "provenance": link.provenance,
        }

    def _event_links(self, condition: Any) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(CalendarEventWorkLink, CalendarEvent)
            .join(CalendarEvent, CalendarEvent.id == CalendarEventWorkLink.event_id)
            .where(condition)
            .order_by(CalendarEvent.start_at, CalendarEvent.created_at)
        ).all()
        return [
            {
                "id": str(link.id),
                "event_id": str(event.id),
                "event_title": event.title,
                "event_status": event.status,
                "start_at": home_isoformat(event.start_at),
                "end_at": home_isoformat(event.end_at),
                "relationship_type": link.relationship_type,
                "notes": link.notes,
            }
            for link, event in rows
        ]
