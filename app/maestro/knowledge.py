"""Knowledge-mode reasoning and validated direct actions.

Knowledge mode is deliberately separate from orchestration. It can reason over Maestro context and
mutate bounded canonical stores, but it cannot create tasks, plans, queue items, or agent runs.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    CalendarEvent,
    Contact,
    ContactAlias,
    ContactDomainNote,
    Domain,
    Entity,
    Idea,
    Message,
    OrganizationAlias,
    RoutedItem,
    Todo,
    WorkflowDefinition,
)
from app.db.repositories import DomainRepository
from app.llm.client import OpenAILLMClient
from app.maestro.context_assembler import MaestroContextAssembler
from app.maestro.scheduler import SchedulerService
from app.memory.calendar_recurrence import normalize_recurrence_rule
from app.memory.contact_intelligence import ContactEmbeddingService, ContactIntelligenceService
from app.memory.organization_intelligence import (
    OrganizationEmbeddingService,
    OrganizationIntelligenceService,
)
from app.memory.routed_retrieval import RoutedEditService
from app.memory.routed_service import RoutedMemoryService
from app.prompts import load_prompt

ALLOWED_ACTIONS = {
    "calendar.create",
    "calendar.update",
    "contact.create",
    "contact.update",
    "todo.create",
    "todo.update",
    "organization.create",
    "organization.update",
    "idea.create",
    "idea.update",
    "workflow.update",
    "workflow.archive",
}


@dataclass(frozen=True)
class KnowledgeActionResult:
    action_type: str
    status: str
    message: str
    object_type: str | None = None
    object_id: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "status": self.status,
            "message": self.message,
            "object_type": self.object_type,
            "object_id": self.object_id,
        }


@dataclass(frozen=True)
class KnowledgeTurn:
    message: str
    actions: list[dict[str, Any]]
    workflow_suggestion: str | None = None
    pending_clarification: str | None = None


@dataclass(frozen=True)
class KnowledgeResponse:
    message: str
    action_results: list[KnowledgeActionResult]
    workflow_suggestion: str | None = None
    pending_clarification: str | None = None


class KnowledgePlanner(Protocol):
    def plan(self, *, message: str, context_text: str, now: datetime) -> KnowledgeTurn: ...


class LLMKnowledgePlanner:
    def __init__(self, client: OpenAILLMClient | None = None):
        self.client = client or OpenAILLMClient()

    def plan(self, *, message: str, context_text: str, now: datetime) -> KnowledgeTurn:
        payload = self.client.structured_response(
            instructions=load_prompt("maestro_knowledge.md"),
            input_text=(
                f"Current time: {now.isoformat()}\n\n"
                f"Chris's message:\n{message}\n\n"
                f"Relevant Maestro context:\n{context_text or '(none retrieved)'}"
            ),
            schema_name="maestro_knowledge_turn",
            schema=_knowledge_schema(),
        )
        return KnowledgeTurn(
            message=str(payload.get("message") or "").strip(),
            actions=[
                parsed
                for item in payload.get("actions") or []
                if isinstance(item, dict) and (parsed := _parse_action(item)) is not None
            ],
            workflow_suggestion=_optional_text(payload.get("workflow_suggestion")),
            pending_clarification=_optional_text(payload.get("pending_clarification")),
        )


class MaestroKnowledgeService:
    """Answers from context and executes only the explicit Knowledge-mode action allowlist."""

    def __init__(self, session: Session, *, planner: KnowledgePlanner | None = None):
        self.session = session
        self.planner = planner

    def respond(
        self,
        message: str,
        *,
        conversation_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
    ) -> KnowledgeResponse:
        context = MaestroContextAssembler(self.session).build_bundle(
            query_text=message,
            max_chars=6500,
            memory_chars=2400,
            routed_chars=2200,
            report_limit=5,
            run_log_limit=5,
            artifact_limit=4,
        )
        workflow_context = self._workflow_context_text()
        conversation_context = self._conversation_context_text(
            conversation_id=conversation_id,
            current_message_id=message_id,
        )
        context_text = "\n\n".join(
            part for part in (conversation_context, context.rendered_text, workflow_context) if part
        )
        now = datetime.now(UTC)
        planner = self.planner or LLMKnowledgePlanner()
        turn = planner.plan(message=message, context_text=context_text, now=now)
        validation_source = "\n".join(part for part in (conversation_context, message) if part)
        results = [
            self._execute(
                action,
                conversation_id=conversation_id,
                message_id=message_id,
                now=now,
                source_message=validation_source,
            )
            for action in turn.actions
        ]
        result_lines = [result.message for result in results if result.status != "completed"]
        if result_lines:
            response_message = "I need one more detail before I can safely finish that change."
            response_message += "\n\n" + "\n".join(f"- {line}" for line in result_lines)
        else:
            response_message = turn.message or "I handled that."
        if (
            turn.workflow_suggestion
            and turn.workflow_suggestion.lower() not in response_message.lower()
        ):
            response_message = f"{response_message}\n\n{turn.workflow_suggestion}"
        return KnowledgeResponse(
            message=response_message,
            action_results=results,
            workflow_suggestion=turn.workflow_suggestion,
            pending_clarification=turn.pending_clarification,
        )

    def _conversation_context_text(
        self,
        *,
        conversation_id: uuid.UUID | None,
        current_message_id: uuid.UUID | None,
    ) -> str:
        if conversation_id is None:
            return ""
        statement = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(10)
        )
        messages = list(reversed(list(self.session.scalars(statement).all())))
        lines: list[str] = []
        pending: str | None = None
        for item in messages:
            if current_message_id is not None and item.id == current_message_id:
                continue
            role = "Chris" if item.sender_type == "user" else "Maestro"
            lines.append(f"{role}: {item.content.strip()}")
            candidate = (item.metadata_ or {}).get("pending_clarification")
            if item.sender_type == "maestro" and candidate:
                pending = str(candidate).strip()
            elif (
                item.sender_type == "maestro"
                and (item.metadata_ or {}).get("interaction_mode") == "knowledge"
            ):
                pending = None
        if not lines:
            return ""
        blocks = ["## Recent conversation\n" + "\n".join(lines)]
        if pending:
            blocks.append(
                "## Pending clarification\n"
                f"Maestro was waiting for Chris to answer: {pending}\n"
                "Interpret a terse reply against this pending question before treating it as a new request."
            )
        return "\n\n".join(blocks)

    def _workflow_context_text(self) -> str:
        definitions = SchedulerService(self.session).list_definitions()
        if not definitions:
            return ""
        lines = ["Existing durable workflows:"]
        for definition in definitions[:20]:
            lines.append(
                f"- id={definition.id}; key={definition.key}; name={definition.name}; "
                f"trigger={definition.trigger_type}; active={definition.is_active}; "
                f"config={json.dumps(definition.trigger_config or {}, sort_keys=True)}"
            )
        return "\n".join(lines)

    def _execute(
        self,
        action: dict[str, Any],
        *,
        conversation_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        now: datetime,
        source_message: str,
    ) -> KnowledgeActionResult:
        action_type = str(action.get("type") or "").strip()
        arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
        if action_type not in ALLOWED_ACTIONS:
            return KnowledgeActionResult(
                action_type,
                "rejected",
                "That action is not available in Knowledge mode.",
            )
        provenance = {
            "source_system": "maestro_chat",
            "interaction_mode": "knowledge",
            "conversation_id": str(conversation_id) if conversation_id else None,
            "message_id": str(message_id) if message_id else None,
            "acted_at": now.isoformat(),
        }
        try:
            if action_type.startswith("calendar."):
                arguments = _normalize_calendar_arguments(arguments, source_message=source_message)
            if action_type.endswith(".create"):
                return self._create(action_type, arguments, provenance)
            if action_type == "workflow.update":
                return self._update_workflow(arguments, provenance)
            if action_type == "workflow.archive":
                return self._archive_workflow(arguments)
            return self._update_routed(action_type, arguments, provenance)
        except (ValueError, TypeError) as exc:
            self.session.rollback()
            return KnowledgeActionResult(action_type, "needs_clarification", str(exc))
        except Exception:
            self.session.rollback()
            return KnowledgeActionResult(
                action_type, "failed", "I could not apply that change safely."
            )

    def _create(
        self,
        action_type: str,
        arguments: dict[str, Any],
        provenance: dict[str, Any],
    ) -> KnowledgeActionResult:
        route_type = {
            "calendar.create": "event",
            "contact.create": "contact",
            "todo.create": "task",
            "organization.create": "entity",
            "idea.create": "think_tank",
        }[action_type]
        title = str(arguments.get("title") or arguments.get("name") or "").strip()
        if not title:
            raise ValueError("I need a name or title before I can create that item.")
        domain = self._domain(
            arguments.get("domain_key"),
            required=route_type in {"event", "task", "think_tank"},
        )
        content = str(
            arguments.get("summary")
            or arguments.get("description")
            or arguments.get("content")
            or title
        ).strip()
        metadata = {
            key: value
            for key, value in arguments.items()
            if key not in {"title", "content", "description", "summary", "domain_key"}
            and value not in (None, "", [])
        }
        if route_type == "task":
            todo = Todo(
                domain_id=domain.id if domain else None,
                title=title,
                description=content,
                todo_type="reminder",
                owner_type="user",
                owner_ref=get_settings().user_display_name,
                due_at=_parse_datetime(arguments.get("due_at")),
                priority=str(arguments.get("priority") or "normal"),
                status="open",
                source_refs=[provenance],
                provenance=provenance,
                metadata_={**metadata, "knowledge_mode": True},
            )
            self.session.add(todo)
            self.session.commit()
            return KnowledgeActionResult(
                action_type,
                "completed",
                f"Created todo '{title}'.",
                "todo",
                str(todo.id),
            )
        if route_type == "contact":
            metadata["name"] = str(arguments.get("name") or title)
            if arguments.get("summary"):
                metadata["summary"] = arguments["summary"]
        if route_type == "entity":
            metadata["name"] = str(arguments.get("name") or title)
        item = RoutedItem(
            domain_id=domain.id if domain else None,
            route_type=route_type,
            title=title,
            content=content,
            priority=str(arguments.get("priority") or "normal"),
            status="open",
            source_refs=[provenance],
            metadata_={
                **metadata,
                "knowledge_mode": True,
                "provenance": provenance,
                "enriched_at": provenance["acted_at"],
                "enrichment_source": "knowledge_mode_validated",
            },
        )
        self.session.add(item)
        self.session.flush()
        promoted = RoutedMemoryService(self.session).promote_item(item)
        self.session.commit()
        if promoted is None:
            raise ValueError(
                "That item was recognized as Chris himself and was not added as a contact."
            )
        return KnowledgeActionResult(
            action_type,
            "completed",
            f"{promoted.action.title()} {promoted.object_type} '{title}'.",
            promoted.object_type,
            str(promoted.object_id),
        )

    def _update_routed(
        self,
        action_type: str,
        arguments: dict[str, Any],
        provenance: dict[str, Any],
    ) -> KnowledgeActionResult:
        object_type = action_type.split(".", 1)[0]
        target = (
            arguments.get("target")
            or arguments.get("id")
            or arguments.get("name")
            or arguments.get("title")
        )
        if not target:
            raise ValueError(f"I need to know which {object_type} you want me to update.")
        object_id = self._resolve_object(object_type, str(target), arguments.get("domain_key"))
        updates = (
            arguments.get("updates")
            if isinstance(arguments.get("updates"), dict)
            else {
                key: value
                for key, value in arguments.items()
                if key not in {"target", "id", "domain_key"}
            }
        )
        updates["metadata"] = {
            **(updates.get("metadata") if isinstance(updates.get("metadata"), dict) else {}),
            "last_knowledge_edit": provenance,
        }
        editor = RoutedEditService(self.session)
        if object_type == "contact":
            domain_note = updates.pop("domain_note", None)
            updated = editor.update_contact(object_id, updates)
            if domain_note:
                self._upsert_contact_domain_note(
                    updated,
                    str(arguments.get("domain_key") or ""),
                    str(domain_note),
                    provenance,
                )
            else:
                ContactEmbeddingService(self.session).upsert(updated)
                self.session.commit()
        elif object_type == "calendar":
            updated = editor.update_event(object_id, updates)
            object_type = "event"
        elif object_type == "todo":
            updated = editor.update_todo(object_id, updates)
        elif object_type == "organization":
            updated = editor.update_entity(object_id, updates)
            OrganizationEmbeddingService(self.session).upsert(updated)
            self.session.commit()
        elif object_type == "idea":
            updated = editor.update_idea(object_id, updates)
        else:
            raise ValueError("That routed object cannot be edited in Knowledge mode.")
        return KnowledgeActionResult(
            action_type,
            "completed",
            f"Updated {object_type} '{getattr(updated, 'name', None) or getattr(updated, 'title', object_id)}'.",
            object_type,
            str(updated.id),
        )

    def _update_workflow(
        self,
        arguments: dict[str, Any],
        provenance: dict[str, Any],
    ) -> KnowledgeActionResult:
        definition = self._resolve_workflow(
            str(arguments.get("target") or arguments.get("id") or "")
        )
        updates = arguments.get("updates") if isinstance(arguments.get("updates"), dict) else {}
        allowed = {
            "name",
            "description",
            "trigger_type",
            "trigger_config",
            "priority",
            "fairness_group",
            "is_active",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(
                f"Knowledge mode cannot edit workflow fields: {', '.join(sorted(unknown))}."
            )
        if not updates:
            raise ValueError("Tell me what should change on that workflow.")
        if "trigger_config" in updates and not isinstance(updates["trigger_config"], dict):
            raise ValueError("Workflow trigger_config must be an object.")
        definition.name = str(updates.get("name", definition.name))
        definition.description = updates.get("description", definition.description)
        definition.trigger_type = str(updates.get("trigger_type", definition.trigger_type))
        definition.trigger_config = {
            **(definition.trigger_config or {}),
            **(updates.get("trigger_config") or {}),
            "last_knowledge_edit": provenance,
        }
        definition.priority = str(updates.get("priority", definition.priority))
        definition.fairness_group = updates.get("fairness_group", definition.fairness_group)
        definition.is_active = bool(updates.get("is_active", definition.is_active))
        self.session.commit()
        return KnowledgeActionResult(
            "workflow.update",
            "completed",
            f"Updated workflow '{definition.name}'.",
            "workflow",
            str(definition.id),
        )

    def _archive_workflow(self, arguments: dict[str, Any]) -> KnowledgeActionResult:
        definition = self._resolve_workflow(
            str(arguments.get("target") or arguments.get("id") or "")
        )
        SchedulerService(self.session).archive_definition(
            definition.id,
            reason=str(arguments.get("reason") or "Archived from Maestro Knowledge mode."),
        )
        return KnowledgeActionResult(
            "workflow.archive",
            "completed",
            f"Archived workflow '{definition.name}'.",
            "workflow",
            str(definition.id),
        )

    def _domain(self, key: Any, *, required: bool) -> Domain | None:
        value = str(key or "").strip().lower()
        aliases = {
            "maestro": "maestro-development",
            "perti": "perti-laboratories",
            "perti labs": "perti-laboratories",
            "global": "",
        }
        value = aliases.get(value, value)
        if not value:
            if required:
                raise ValueError("I need the domain for that item.")
            return None
        domain = DomainRepository(self.session).get_by_key(value)
        if domain is None:
            raise ValueError(f"I could not find the domain '{key}'.")
        return domain

    def _resolve_object(self, object_type: str, target: str, domain_key: Any) -> uuid.UUID:
        try:
            identifier = uuid.UUID(target)
        except ValueError:
            identifier = None
        model = {
            "calendar": CalendarEvent,
            "contact": Contact,
            "todo": Todo,
            "organization": Entity,
            "idea": Idea,
        }.get(object_type)
        if model is None:
            raise ValueError("Unsupported routed object type.")
        if identifier and self.session.get(model, identifier):
            return identifier
        domain = self._domain(domain_key, required=False)
        normalized = _normalize(target)
        if object_type == "contact":
            alias = self.session.scalar(
                select(ContactAlias).where(ContactAlias.normalized_alias == normalized)
            )
            exact = self.session.scalars(
                select(Contact).where(
                    or_(
                        func.lower(Contact.email) == target.lower(),
                        Contact.normalized_name == normalized,
                    ),
                    Contact.status != "archived",
                )
            ).all()
            if alias:
                exact = [self.session.get(Contact, alias.contact_id)]
            if not exact:
                results = ContactIntelligenceService(self.session).search(
                    target,
                    domain_id=domain.id if domain else None,
                    limit=3,
                )
                exact = [
                    self.session.get(Contact, result.contact.id)
                    for result in results
                    if result.score >= 0.78
                ]
        elif object_type == "organization":
            alias = self.session.scalar(
                select(OrganizationAlias).where(OrganizationAlias.normalized_alias == normalized)
            )
            exact = list(
                self.session.scalars(
                    select(Entity).where(
                        Entity.normalized_name == normalized,
                        Entity.status != "archived",
                    )
                )
            )
            if alias:
                exact = [self.session.get(Entity, alias.entity_id)]
            if not exact:
                results = OrganizationIntelligenceService(self.session).search(
                    target,
                    domain_id=domain.id if domain else None,
                    limit=3,
                )
                exact = [
                    self.session.get(Entity, result.organization.id)
                    for result in results
                    if result.score >= 0.78
                ]
        else:
            title_field = model.title
            query = select(model).where(
                func.lower(title_field) == target.lower(), model.status != "archived"
            )
            if domain is not None:
                query = query.where(model.domain_id == domain.id)
            exact = list(self.session.scalars(query))
        matches = [item for item in exact if item is not None]
        unique = {item.id: item for item in matches}
        if len(unique) != 1:
            if not unique:
                raise ValueError(f"I could not find a unique {object_type} matching '{target}'.")
            names = ", ".join(
                str(getattr(item, "name", None) or getattr(item, "title", item.id))
                for item in unique.values()
            )
            raise ValueError(f"'{target}' matches multiple {object_type} records: {names}.")
        return next(iter(unique))

    def _resolve_workflow(self, target: str) -> WorkflowDefinition:
        if not target:
            raise ValueError("I need the workflow name, key, or ID.")
        try:
            identifier = uuid.UUID(target)
        except ValueError:
            identifier = None
        if identifier:
            definition = self.session.get(WorkflowDefinition, identifier)
            if definition:
                return definition
        normalized = _normalize(target)
        matches = list(
            self.session.scalars(
                select(WorkflowDefinition).where(
                    or_(
                        func.lower(WorkflowDefinition.key) == target.lower(),
                        func.lower(WorkflowDefinition.name) == target.lower(),
                    )
                )
            )
        )
        if not matches:
            matches = [
                item
                for item in SchedulerService(self.session).list_definitions()
                if normalized in _normalize(item.name)
            ]
        if len(matches) != 1:
            raise ValueError(f"I could not find one unambiguous workflow matching '{target}'.")
        return matches[0]

    def _upsert_contact_domain_note(
        self,
        contact: Contact,
        domain_key: str,
        note: str,
        provenance: dict[str, Any],
    ) -> None:
        domain = self._domain(domain_key, required=True)
        row = self.session.scalar(
            select(ContactDomainNote).where(
                ContactDomainNote.contact_id == contact.id,
                ContactDomainNote.domain_id == domain.id,
            )
        )
        if row is None:
            row = ContactDomainNote(
                contact_id=contact.id,
                domain_id=domain.id,
                notes=note,
                source_refs=[provenance],
            )
            self.session.add(row)
        elif note not in (row.notes or ""):
            row.notes = "\n\n".join(part for part in (row.notes, note) if part)
            row.source_refs = [*(row.source_refs or []), provenance]
        ContactEmbeddingService(self.session).upsert(contact)
        self.session.commit()


def _knowledge_schema() -> dict[str, Any]:
    action = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "arguments_json", "reason"],
        "properties": {
            "type": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
            "arguments_json": {"type": "string"},
            "reason": {"type": ["string", "null"]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "actions", "workflow_suggestion", "pending_clarification"],
        "properties": {
            "message": {"type": "string"},
            "actions": {"type": "array", "items": action, "maxItems": 8},
            "workflow_suggestion": {"type": ["string", "null"]},
            "pending_clarification": {"type": ["string", "null"]},
        },
    }


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Dates and times must include a timezone offset.")
    return parsed


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_calendar_arguments(
    arguments: dict[str, Any],
    *,
    source_message: str,
) -> dict[str, Any]:
    normalized = dict(arguments)
    updates = normalized.get("updates") if isinstance(normalized.get("updates"), dict) else None
    schedule = dict(updates) if updates is not None else normalized
    start_at = _parse_datetime(schedule.get("start_at"))
    end_at = _parse_datetime(schedule.get("end_at"))
    explicit_range = _time_range_from_message(source_message, anchor=start_at)
    if explicit_range is not None:
        start_at, end_at = explicit_range
    if start_at is not None:
        schedule["start_at"] = start_at.isoformat()
    if end_at is not None:
        schedule["end_at"] = end_at.isoformat()
    if start_at is not None and end_at is not None and end_at <= start_at:
        raise ValueError("The event end time must be after its start time.")
    recurrence = schedule.get("recurrence_rule")
    if recurrence and start_at is None:
        raise ValueError("A recurring event needs a first start date and time.")
    if recurrence:
        schedule["recurrence_rule"] = normalize_recurrence_rule(
            str(recurrence),
            start_at=start_at,
            source_text=source_message,
        )
    if updates is not None:
        normalized["updates"] = schedule
    return normalized


def _time_range_from_message(
    message: str,
    *,
    anchor: datetime | None,
) -> tuple[datetime, datetime] | None:
    if anchor is None:
        return None
    matches = list(
        re.finditer(
            r"(?<!\d)(\d{1,2}(?::?\d{2})?)(?:\s*(am|pm))?\s*"
            r"(?:-|–|—|to|until|through)\s*(\d{1,2}(?::?\d{2})?)"
            r"(?:\s*(am|pm))?(?!\d)",
            message,
            flags=re.IGNORECASE,
        )
    )
    if not matches:
        return None
    match = matches[-1]
    start_hour, start_minute = _clock_parts(match.group(1))
    end_hour, end_minute = _clock_parts(match.group(3))
    start_meridiem = (match.group(2) or match.group(4) or "").lower()
    end_meridiem = (match.group(4) or match.group(2) or "").lower()
    if start_meridiem and end_meridiem:
        start_hour = _hour_with_meridiem(start_hour, start_meridiem)
        end_hour = _hour_with_meridiem(end_hour, end_meridiem)
    elif anchor.hour >= 12 and start_hour < 12:
        start_hour += 12
        end_hour += 12
    start = anchor.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = anchor.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    return start, end


def _clock_parts(value: str) -> tuple[int, int]:
    digits = value.replace(":", "")
    if ":" in value:
        hour_text, minute_text = value.split(":", 1)
    elif len(digits) > 2:
        hour_text, minute_text = digits[:-2], digits[-2:]
    else:
        hour_text, minute_text = digits, "0"
    hour, minute = int(hour_text), int(minute_text)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("I could not interpret the requested event time range.")
    return hour, minute


def _hour_with_meridiem(hour: int, meridiem: str) -> int:
    if not 1 <= hour <= 12:
        raise ValueError("A time with AM or PM must use an hour from 1 through 12.")
    return (hour % 12) + (12 if meridiem == "pm" else 0)


def _parse_action(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        arguments = json.loads(str(payload.get("arguments_json") or "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    return {
        "type": payload.get("type"),
        "arguments": arguments,
        "reason": payload.get("reason"),
    }


def knowledge_fallback(message: str) -> KnowledgeResponse:
    """A safe response when live reasoning is unavailable; never guesses or writes."""
    return KnowledgeResponse(
        message=(
            "I can help with that from Knowledge mode, but my reasoning connection is unavailable "
            "right now. I did not change any records."
        ),
        action_results=[],
        workflow_suggestion=None,
    )
