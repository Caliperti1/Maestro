"""Knowledge-mode reasoning and validated direct actions.

Knowledge mode can reason over Maestro context, mutate bounded canonical stores, and invoke an
existing approved on-demand workflow. Designing new delegated work remains orchestration's job.
"""

from __future__ import annotations

import json
import logging
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
    CalendarEventWorkLink,
    Contact,
    ContactAlias,
    ContactDomainNote,
    Domain,
    Entity,
    Message,
    OrganizationAlias,
    ProductIssue,
    ProductProject,
    RepositoryProfile,
    RoutedItem,
    Todo,
    WorkflowDefinition,
)
from app.db.repositories import DomainRepository
from app.issues.service import ProductIssueService
from app.llm.client import LLMClientError, OpenAILLMClient
from app.llm.telemetry import record_llm_call
from app.maestro.context_assembler import MaestroContextAssembler
from app.maestro.knowledge_tools import (
    READ_ACTIONS,
    KnowledgeActionResult,
    KnowledgeReadToolService,
)
from app.maestro.scheduler import SchedulerService
from app.memory.calendar_recurrence import normalize_recurrence_rule
from app.memory.contact_intelligence import ContactEmbeddingService, ContactIntelligenceService
from app.memory.event_work_links import EventWorkLinkService
from app.memory.organization_intelligence import (
    OrganizationEmbeddingService,
    OrganizationIntelligenceService,
)
from app.memory.routed_retrieval import RoutedEditService
from app.memory.routed_service import RoutedMemoryService
from app.prompts import load_prompt

ALLOWED_ACTIONS = {
    "context.search",
    "web.search",
    "calendar.create",
    "calendar.update",
    "calendar.link_work",
    "calendar.unlink_work",
    "contact.create",
    "contact.update",
    "todo.create",
    "todo.update",
    "organization.create",
    "organization.update",
    "workflow.update",
    "workflow.archive",
    "workflow.search",
    "workflow.get",
    "workflow.run",
    "issue.search",
    "issue.get",
    "issue.capture",
    "issue.update",
}
MAX_KNOWLEDGE_ACTION_ROUNDS = 4
KNOWLEDGE_REASONING_ATTEMPTS = 2

logger = logging.getLogger(__name__)


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
    iterations: int = 1


class KnowledgePlanner(Protocol):
    def plan(self, *, message: str, context_text: str, now: datetime) -> KnowledgeTurn: ...


class LLMKnowledgePlanner:
    def __init__(
        self,
        client: OpenAILLMClient | None = None,
        *,
        session: Session | None = None,
    ):
        self.client = client or OpenAILLMClient()
        self.session = session

    def plan(self, *, message: str, context_text: str, now: datetime) -> KnowledgeTurn:
        instructions = load_prompt("maestro_knowledge.md")
        input_text = (
            f"Current time: {now.isoformat()}\n\n"
            f"Chris's message:\n{message}\n\n"
            f"Relevant Maestro context:\n{context_text or '(none retrieved)'}"
        )
        payload: dict[str, Any] | None = None
        for attempt in range(1, KNOWLEDGE_REASONING_ATTEMPTS + 1):
            try:
                payload = self.client.structured_response(
                    instructions=instructions,
                    input_text=input_text,
                    schema_name="maestro_knowledge_turn",
                    schema=_knowledge_schema(),
                )
            except (LLMClientError, OSError, ValueError) as exc:
                self._record_call(
                    prompt_chars=len(instructions) + len(input_text),
                    status="failed",
                    error_message=str(exc),
                    attempt=attempt,
                )
                if attempt >= KNOWLEDGE_REASONING_ATTEMPTS:
                    raise
                logger.warning(
                    "Knowledge reasoning attempt %s failed; retrying once: %s",
                    attempt,
                    exc,
                )
                continue
            self._record_call(
                prompt_chars=len(instructions) + len(input_text),
                status="complete",
                error_message=None,
                attempt=attempt,
            )
            break
        if payload is None:
            raise LLMClientError("Knowledge reasoning returned no response.")
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

    def _record_call(
        self,
        *,
        prompt_chars: int,
        status: str,
        error_message: str | None,
        attempt: int,
    ) -> None:
        if self.session is None:
            return
        try:
            record_llm_call(
                self.session,
                component="maestro.knowledge",
                client=self.client,
                prompt_chars=prompt_chars,
                status=status,
                error_message=error_message,
                metadata={"attempt": attempt},
            )
        except Exception:
            logger.exception("Could not persist Knowledge reasoning telemetry.")


class MaestroKnowledgeService:
    """Answers from context and executes only the explicit Knowledge-mode action allowlist."""

    def __init__(
        self,
        session: Session,
        *,
        planner: KnowledgePlanner | None = None,
        web_client: OpenAILLMClient | None = None,
    ):
        self.session = session
        self.planner = planner
        self.web_client = web_client

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
            include_sections=_knowledge_context_sections(message),
        )
        portfolio_context = self._product_portfolio_context_text()
        workflow_context = self._workflow_context_text(
            include_all=_message_needs_workflow_context(message)
        )
        conversation_context = self._conversation_context_text(
            conversation_id=conversation_id,
            current_message_id=message_id,
        )
        context_text = "\n\n".join(
            part
            for part in (
                conversation_context,
                portfolio_context,
                context.rendered_text,
                workflow_context,
            )
            if part
        )
        now = datetime.now(UTC)
        planner = self.planner or LLMKnowledgePlanner(session=self.session)
        validation_source = "\n".join(part for part in (conversation_context, message) if part)
        turn, results, iterations = self._run_action_loop(
            planner=planner,
            message=message,
            context_text=context_text,
            conversation_id=conversation_id,
            message_id=message_id,
            now=now,
            validation_source=validation_source,
        )
        result_lines = [
            result.message for result in results if result.status not in {"completed", "skipped"}
        ]
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
            iterations=iterations,
        )

    def _run_action_loop(
        self,
        *,
        planner: KnowledgePlanner,
        message: str,
        context_text: str,
        conversation_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        now: datetime,
        validation_source: str,
    ) -> tuple[KnowledgeTurn, list[KnowledgeActionResult], int]:
        results: list[KnowledgeActionResult] = []
        execution_blocks: list[str] = []
        completed_writes: set[str] = set()
        completed_reads: dict[str, KnowledgeActionResult] = {}
        turn = KnowledgeTurn(message="", actions=[])
        iterations = 0
        for round_number in range(1, MAX_KNOWLEDGE_ACTION_ROUNDS + 1):
            iterations += 1
            turn = planner.plan(
                message=message,
                context_text=self._loop_context(context_text, execution_blocks),
                now=now,
            )
            if not turn.actions or turn.pending_clarification:
                return turn, results, iterations
            actions = turn.actions
            reads = [action for action in actions if action.get("type") in READ_ACTIONS]
            if reads:
                actions = _coalesce_read_actions(reads)
            round_results: list[KnowledgeActionResult] = []
            executed = 0
            for action in actions:
                action_type = str(action.get("type") or "")
                signature = _action_signature(action)
                read_cache_key = _read_cache_key(action)
                if action_type in READ_ACTIONS and read_cache_key in completed_reads:
                    cached = completed_reads[read_cache_key]
                    round_results.append(
                        KnowledgeActionResult(
                            action_type,
                            "reused",
                            f"Reused the earlier completed result: {cached.message}",
                            cached.object_type,
                            cached.object_id,
                            data={"cached_action_signature": signature},
                        )
                    )
                    continue
                if action_type not in READ_ACTIONS and signature in completed_writes:
                    round_results.append(
                        KnowledgeActionResult(
                            action_type,
                            "skipped",
                            "The identical write already completed in this Knowledge turn.",
                        )
                    )
                    continue
                result = self._execute(
                    action,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    now=now,
                    source_message=validation_source,
                )
                executed += 1
                round_results.append(result)
                if action_type in READ_ACTIONS and result.status == "completed":
                    completed_reads[read_cache_key] = result
                if action_type not in READ_ACTIONS and result.status == "completed":
                    completed_writes.add(signature)
                    completed_reads.clear()
            results.extend(result for result in round_results if result.status != "reused")
            execution_blocks.append(_render_action_results(round_number, round_results))
            execution_blocks = _bounded_execution_blocks(execution_blocks)
            if any(result.status == "needs_clarification" for result in round_results):
                return turn, results, iterations
            if executed == 0:
                iterations += 1
                final_context = self._loop_context(context_text, execution_blocks)
                final_context += (
                    "\n\n## Execution boundary\n"
                    "The requested actions were already completed earlier in this turn. Emit no "
                    "more actions. Use the earlier authoritative results to answer Chris now."
                )
                turn = planner.plan(message=message, context_text=final_context, now=now)
                return (
                    KnowledgeTurn(
                        message=turn.message,
                        actions=[],
                        workflow_suggestion=turn.workflow_suggestion,
                        pending_clarification=turn.pending_clarification,
                    ),
                    results,
                    iterations,
                )

        iterations += 1
        final_context = self._loop_context(context_text, execution_blocks)
        final_context += (
            "\n\n## Execution boundary\n"
            "The immediate-action limit has been reached. Emit no more actions. Give Chris a "
            "grounded conversational summary of completed results, failures, and anything still open."
        )
        turn = planner.plan(message=message, context_text=final_context, now=now)
        return (
            KnowledgeTurn(
                message=turn.message,
                actions=[],
                workflow_suggestion=turn.workflow_suggestion,
                pending_clarification=turn.pending_clarification,
            ),
            results,
            iterations,
        )

    def _loop_context(self, base_context: str, execution_blocks: list[str]) -> str:
        if not execution_blocks:
            return base_context
        return f"{base_context}\n\n" + "\n\n".join(execution_blocks)

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

    def _workflow_context_text(self, *, include_all: bool = False) -> str:
        definitions = SchedulerService(self.session).list_definitions()
        if not include_all:
            definitions = [
                definition
                for definition in definitions
                if definition.trigger_type == "manual" and definition.is_active
            ]
        if not definitions:
            return ""
        lines = ["Existing durable workflows available to this turn:"]
        for definition in definitions[:20]:
            config = definition.trigger_config or {}
            compact_config = {
                key: config[key]
                for key in (
                    "invocation_aliases",
                    "parameter_schema",
                    "approval_policy",
                    "workflow_version",
                    "event_type",
                    "time_of_day",
                    "next_run_at",
                )
                if key in config
            }
            lines.append(
                f"- id={definition.id}; key={definition.key}; name={definition.name}; "
                f"trigger={definition.trigger_type}; active={definition.is_active}; "
                f"config={json.dumps(compact_config, sort_keys=True)}"
            )
        return "\n".join(lines)

    def _product_portfolio_context_text(self) -> str:
        rows = self.session.execute(
            select(Domain, ProductProject, RepositoryProfile)
            .join(ProductProject, ProductProject.domain_id == Domain.id)
            .outerjoin(RepositoryProfile, RepositoryProfile.project_id == ProductProject.id)
            .where(ProductProject.status == "active")
            .order_by(Domain.name, ProductProject.name, RepositoryProfile.display_name)
        ).all()
        if not rows:
            return ""
        lines = [
            "## Canonical Product Portfolio",
            (
                "Use these exact domain_key, project_key, and repository_key values for Product "
                "Issues. Project ownership is already canonical here; do not duplicate it into "
                "memory or organizations."
            ),
        ]
        for domain, project, repository in rows:
            repository_text = (
                f"; repository_key={repository.key}; repository={repository.external_repo}"
                if repository
                else ""
            )
            lines.append(
                f"- domain_key={domain.key} ({domain.name}); "
                f"project_key={project.key} ({project.name}){repository_text}"
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
            if action_type in READ_ACTIONS:
                return KnowledgeReadToolService(
                    self.session,
                    domain_resolver=self._domain,
                    web_client=self.web_client,
                ).execute(action_type, arguments)
            if action_type == "issue.capture":
                return self._capture_issue(arguments, provenance)
            if action_type == "issue.update":
                return self._update_issue(arguments)
            if action_type == "calendar.link_work":
                return self._link_event_work(arguments, provenance)
            if action_type == "calendar.unlink_work":
                return self._unlink_event_work(arguments)
            if action_type.startswith("calendar."):
                arguments = _normalize_calendar_arguments(arguments, source_message=source_message)
            if action_type.endswith(".create"):
                return self._create(action_type, arguments, provenance)
            if action_type == "workflow.update":
                return self._update_workflow(arguments, provenance)
            if action_type == "workflow.archive":
                return self._archive_workflow(arguments)
            if action_type == "workflow.run":
                return self._run_on_demand_workflow(
                    arguments,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    now=now,
                    source_message=source_message,
                )
            return self._update_routed(action_type, arguments, provenance)
        except (ValueError, TypeError) as exc:
            self.session.rollback()
            return KnowledgeActionResult(action_type, "needs_clarification", str(exc))
        except Exception:
            self.session.rollback()
            return KnowledgeActionResult(
                action_type, "failed", "I could not apply that change safely."
            )

    def _capture_issue(
        self,
        arguments: dict[str, Any],
        provenance: dict[str, Any],
    ) -> KnowledgeActionResult:
        result = ProductIssueService(self.session).capture(
            domain_key=_optional_text(arguments.get("domain_key")),
            project_key=_optional_text(arguments.get("project_key")),
            repository_key=_optional_text(arguments.get("repository_key")),
            title=_optional_text(arguments.get("title")),
            problem=str(arguments.get("problem") or ""),
            desired_outcome=str(arguments.get("desired_outcome") or ""),
            acceptance_criteria=(
                arguments.get("acceptance_criteria")
                if isinstance(arguments.get("acceptance_criteria"), list)
                else []
            ),
            notes=str(arguments.get("notes") or ""),
            issue_type=str(arguments.get("issue_type") or "feature"),
            priority=str(arguments.get("priority") or "normal"),
            agent_task=bool(arguments.get("agent_task", False)),
            source_refs=[provenance],
            provenance=provenance,
        )
        if result.status == "needs_clarification":
            return KnowledgeActionResult(
                "issue.capture", "needs_clarification", result.clarification or result.message
            )
        return KnowledgeActionResult(
            "issue.capture",
            "completed",
            result.message,
            "product_issue",
            str(result.issue.id) if result.issue else None,
            data={
                "capture_status": result.status,
                "matched_issue_id": result.matched_issue_id,
                "confidence": result.confidence,
            },
        )

    def _update_issue(self, arguments: dict[str, Any]) -> KnowledgeActionResult:
        target = str(arguments.get("target") or arguments.get("id") or arguments.get("title") or "").strip()
        if not target:
            raise ValueError("I need the issue ID or title to update it.")
        try:
            issue_id = uuid.UUID(target)
            issue = self.session.get(ProductIssue, issue_id)
        except ValueError:
            matches = ProductIssueService(self.session).search(
                query=target,
                domain_key=_optional_text(arguments.get("domain_key")),
                project_key=_optional_text(arguments.get("project_key")),
                limit=3,
            )
            issue = matches[0] if len(matches) == 1 else None
        if issue is None:
            raise ValueError(f"I could not find one unambiguous product issue matching '{target}'.")
        updates = arguments.get("updates") if isinstance(arguments.get("updates"), dict) else {
            key: value for key, value in arguments.items()
            if key not in {"target", "id", "domain_key", "project_key"}
        }
        updated = ProductIssueService(self.session).update(issue.id, updates)
        return KnowledgeActionResult(
            "issue.update", "completed", f"Updated issue '{updated.title}'.",
            "product_issue", str(updated.id),
        )

    def _link_event_work(
        self,
        arguments: dict[str, Any],
        provenance: dict[str, Any],
    ) -> KnowledgeActionResult:
        event_target = str(
            arguments.get("event_target") or arguments.get("event_id") or ""
        ).strip()
        work_target = str(
            arguments.get("work_target") or arguments.get("target_id") or ""
        ).strip()
        target_type = str(arguments.get("target_type") or "todo").strip().lower()
        if not event_target or not work_target:
            raise ValueError("I need both the event and the todo or issue to link.")
        event_id = self._resolve_object("calendar", event_target, arguments.get("domain_key"))
        if target_type == "todo":
            target_id = self._resolve_object("todo", work_target, arguments.get("domain_key"))
        elif target_type == "product_issue":
            target_id = self._resolve_issue_id(work_target, arguments)
        else:
            raise ValueError("Linked work must be a todo or product issue.")
        link = EventWorkLinkService(self.session).link(
            event_id=event_id,
            target_type=target_type,
            target_id=target_id,
            relationship_type=str(arguments.get("relationship_type") or "prerequisite"),
            notes=str(arguments.get("notes") or ""),
            provenance=provenance,
        )
        payload = EventWorkLinkService(self.session).payload(link)
        return KnowledgeActionResult(
            "calendar.link_work",
            "completed",
            (
                f"Linked '{payload['title']}' to the event as "
                f"{link.relationship_type.replace('_', ' ')} work."
            ),
            "event_work_link",
            str(link.id),
            data=payload,
        )

    def _unlink_event_work(self, arguments: dict[str, Any]) -> KnowledgeActionResult:
        link_id = str(arguments.get("link_id") or arguments.get("id") or "").strip()
        try:
            identifier = uuid.UUID(link_id)
        except ValueError as exc:
            raise ValueError("I need the event work link ID to remove that relationship.") from exc
        link = self.session.get(CalendarEventWorkLink, identifier)
        if link is None:
            raise ValueError("I could not find that event work link.")
        EventWorkLinkService(self.session).unlink(identifier)
        return KnowledgeActionResult(
            "calendar.unlink_work",
            "completed",
            "Removed the linked work from the event.",
            "event_work_link",
            link_id,
        )

    def _resolve_issue_id(self, target: str, arguments: dict[str, Any]) -> uuid.UUID:
        try:
            identifier = uuid.UUID(target)
        except ValueError:
            identifier = None
        if identifier is not None and self.session.get(ProductIssue, identifier):
            return identifier
        matches = ProductIssueService(self.session).search(
            query=target,
            domain_key=_optional_text(arguments.get("domain_key")),
            project_key=_optional_text(arguments.get("project_key")),
            limit=3,
        )
        if len(matches) != 1:
            raise ValueError(f"I could not find one unambiguous product issue matching '{target}'.")
        return matches[0].id

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
        }[action_type]
        title = str(arguments.get("title") or arguments.get("name") or "").strip()
        if not title:
            raise ValueError("I need a name or title before I can create that item.")
        domain = self._domain(
            arguments.get("domain_key"),
            required=route_type in {"event", "task"},
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
            agent_task = bool(arguments.get("agent_task", False))
            todo = Todo(
                domain_id=domain.id if domain else None,
                title=title,
                description=content,
                todo_type="task",
                owner_type="maestro" if agent_task else "user",
                owner_ref="Maestro" if agent_task else get_settings().user_display_name,
                due_at=_parse_datetime(arguments.get("due_at")),
                estimated_minutes=(
                    int(arguments["estimated_minutes"])
                    if arguments.get("estimated_minutes") not in (None, "")
                    else None
                ),
                scheduled_start_at=_parse_datetime(arguments.get("scheduled_start_at")),
                agent_task=agent_task,
                agent_task_status="pending" if agent_task else "not_agent",
                priority=str(arguments.get("priority") or "normal"),
                status="open",
                source_refs=[provenance],
                provenance=provenance,
                metadata_={**metadata, "knowledge_mode": True},
            )
            self.session.add(todo)
            self.session.flush()
            from app.memory.todo_scheduling import TodoSchedulingService

            if todo.estimated_minutes is None:
                todo.estimated_minutes = TodoSchedulingService(self.session).estimate_minutes(todo)
            TodoSchedulingService(self.session).sync_projection(todo, commit=False)
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

    def _run_on_demand_workflow(
        self,
        arguments: dict[str, Any],
        *,
        conversation_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        now: datetime,
        source_message: str,
    ) -> KnowledgeActionResult:
        definition = self._resolve_workflow(
            str(arguments.get("target") or arguments.get("id") or "")
        )
        parameters = arguments.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError("Workflow parameters must be an object.")
        run = SchedulerService(self.session).enqueue_on_demand_workflow(
            definition,
            parameters=parameters or {},
            invocation_text=str(arguments.get("invocation_text") or source_message).strip(),
            conversation_id=conversation_id,
            message_id=message_id,
            now=now,
        )
        return KnowledgeActionResult(
            "workflow.run",
            "completed",
            f"Started on-demand workflow '{definition.name}'.",
            "workflow_run",
            str(run.id),
            data={
                "workflow_definition_id": str(definition.id),
                "workflow_key": definition.key,
                "workflow_name": definition.name,
                "run_id": str(run.id),
                "status": run.status,
                "parameters": ((run.input_payload or {}).get("invocation") or {}).get(
                    "parameters", {}
                ),
            },
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
                or _normalize(item.name) in normalized
                or any(
                    normalized == _normalize(str(alias))
                    for alias in (item.trigger_config or {}).get("invocation_aliases", [])
                )
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


def _knowledge_context_sections(message: str) -> set[str]:
    normalized = _normalize(message)
    issue_terms = ("product issue", "github issue", "backlog", "code issue", "story")
    if any(term in normalized for term in issue_terms):
        return {"identity", "memory", "web_search"}
    routed_terms = (
        "calendar",
        "event",
        "contact",
        "organization",
        "task",
        "todo",
    )
    if any(term in normalized for term in routed_terms):
        return {"identity", "memory", "routed_objects", "web_search"}
    history_terms = ("report", "run log", "artifact")
    if any(term in normalized for term in history_terms):
        return {
            "identity",
            "federated",
            "memory",
            "reports",
            "run_log",
            "artifacts",
            "web_search",
        }
    return {
        "identity",
        "federated",
        "memory",
        "routed_objects",
        "reports",
        "run_log",
        "artifacts",
        "web_search",
    }


def _message_needs_workflow_context(message: str) -> bool:
    normalized = _normalize(message)
    return any(
        term in normalized
        for term in ("workflow", "schedule", "scheduled", "recurring", "trigger", "queue")
    )


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


def _action_signature(action: dict[str, Any]) -> str:
    action_type = str(action.get("type") or "")
    arguments = dict(action.get("arguments")) if isinstance(action.get("arguments"), dict) else {}
    if action_type == "issue.search":
        query_value = arguments.pop("query_text", None) or arguments.pop("query", None)
        if query_value:
            query_terms = sorted(
                term
                for term in re.findall(r"[a-z0-9]+", str(query_value).lower())
                if term not in {"and", "or"}
            )
            arguments["query"] = " ".join(query_terms)
        for key in ("domain_keys", "project_keys", "repository_keys"):
            if isinstance(arguments.get(key), list):
                arguments[key] = sorted(str(value).lower() for value in arguments[key])
    return json.dumps(
        {"type": action_type, "arguments": arguments},
        sort_keys=True,
        default=str,
    )


def _read_cache_key(action: dict[str, Any]) -> str:
    if str(action.get("type") or "") == "issue.search":
        return "issue.search:portfolio"
    return _action_signature(action)


def _coalesce_read_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issue_searches = [action for action in actions if action.get("type") == "issue.search"]
    if len(issue_searches) <= 1:
        return actions
    combined_arguments: dict[str, Any] = {}
    query_terms: set[str] = set()
    max_items = 0
    for action in issue_searches:
        arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
        query = arguments.get("query") or arguments.get("query_text") or ""
        query_terms.update(
            term
            for term in re.findall(r"[a-z0-9]+", str(query).lower())
            if term not in {"and", "or"}
        )
        for plural, singular in (
            ("domain_keys", "domain_key"),
            ("project_keys", "project_key"),
            ("repository_keys", "repository_key"),
        ):
            values = arguments.get(plural)
            values = values if isinstance(values, list) else []
            if arguments.get(singular):
                values = [*values, arguments[singular]]
            combined_arguments[plural] = list(
                dict.fromkeys(
                    [
                        *combined_arguments.get(plural, []),
                        *(str(value).strip().lower() for value in values if str(value).strip()),
                    ]
                )
            )
        if arguments.get("status"):
            combined_arguments["status"] = arguments["status"]
        try:
            max_items = max(max_items, int(arguments.get("max_items") or 0))
        except (TypeError, ValueError):
            pass
    combined_arguments["query"] = " ".join(sorted(query_terms))
    combined_arguments["max_items"] = min(10, max_items or 8)
    combined = {
        "type": "issue.search",
        "arguments": combined_arguments,
        "reason": "Combined overlapping Product Issue reads into one portfolio search.",
    }
    non_issue_searches = [action for action in actions if action.get("type") != "issue.search"]
    return [combined, *non_issue_searches]


def _render_action_results(
    round_number: int,
    results: list[KnowledgeActionResult],
) -> str:
    header = (
        f"## Immediate action results: round {round_number}\n"
        "These are authoritative results from Maestro's own services. Reason over them before "
        "choosing another action. Do not repeat a completed read or write.\n"
    )
    if not results:
        return header + "[]"
    total_budget = 9000
    per_result_budget = max(900, (total_budget - len(header)) // len(results))
    rendered_results = [
        json.dumps(
            _bounded_action_result_payload(result, max_chars=per_result_budget),
            default=str,
            separators=(",", ":"),
        )
        for result in results
    ]
    return header + "[\n" + ",\n".join(rendered_results) + "\n]"


def _bounded_action_result_payload(
    result: KnowledgeActionResult,
    *,
    max_chars: int,
) -> dict[str, Any]:
    payload = result.payload()
    if len(json.dumps(payload, default=str)) <= max_chars:
        return payload
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    issues = data.get("issues") if isinstance(data.get("issues"), list) else None
    if issues is not None:
        bounded_data = {
            key: value
            for key, value in data.items()
            if key != "issues"
        }
        bounded_data["issues"] = []
        bounded_data["omitted_issue_count"] = len(issues)
        payload["data"] = bounded_data
        for issue in issues:
            compact_issue = _compact_issue_result(issue)
            bounded_data["issues"].append(compact_issue)
            bounded_data["omitted_issue_count"] = len(issues) - len(bounded_data["issues"])
            if len(json.dumps(payload, default=str)) > max_chars:
                bounded_data["issues"].pop()
                bounded_data["omitted_issue_count"] = len(issues) - len(bounded_data["issues"])
                break
        return payload
    payload["data"] = _compact_result_value(data, depth=0)
    if len(json.dumps(payload, default=str)) <= max_chars:
        return payload
    payload["data"] = {
        "truncated": True,
        "summary": str(data.get("rendered_text") or data.get("output_text") or "")[:600],
    }
    return payload


def _compact_issue_result(value: Any) -> dict[str, Any]:
    issue = value if isinstance(value, dict) else {}
    keys = (
        "id",
        "title",
        "summary",
        "status",
        "priority",
        "domain_key",
        "project_key",
        "repository_key",
        "external_number",
        "external_url",
        "relevance_score",
    )
    compact = {key: issue.get(key) for key in keys if issue.get(key) is not None}
    if compact.get("summary"):
        compact["summary"] = str(compact["summary"])[:320]
    return compact


def _compact_result_value(value: Any, *, depth: int) -> Any:
    if depth >= 3:
        return str(value)[:300]
    if isinstance(value, str):
        return value if len(value) <= 700 else value[:697] + "..."
    if isinstance(value, list):
        return [_compact_result_value(item, depth=depth + 1) for item in value[:5]]
    if isinstance(value, dict):
        return {
            str(key): _compact_result_value(item, depth=depth + 1)
            for key, item in list(value.items())[:12]
        }
    return value


def _bounded_execution_blocks(blocks: list[str], *, max_chars: int = 14000) -> list[str]:
    retained: list[str] = []
    used = 0
    for block in reversed(blocks):
        addition = len(block) + (2 if retained else 0)
        if retained and used + addition > max_chars:
            break
        retained.append(block[-max_chars:] if not retained and len(block) > max_chars else block)
        used += min(addition, max_chars)
    return list(reversed(retained))


def _normalize_calendar_arguments(
    arguments: dict[str, Any],
    *,
    source_message: str,
) -> dict[str, Any]:
    normalized = dict(arguments)
    updates = normalized.get("updates") if isinstance(normalized.get("updates"), dict) else None
    schedule = dict(updates) if updates is not None else normalized
    raw_item_kind = schedule.get("item_kind")
    item_kind = (
        str(raw_item_kind or "event").strip().lower()
        if raw_item_kind is not None or updates is None
        else None
    )
    if item_kind is not None and item_kind not in {"event", "scheduled_todo", "context_window"}:
        raise ValueError("I could not identify that calendar item type.")
    if item_kind is not None:
        schedule["item_kind"] = item_kind
    if item_kind == "context_window":
        context_type = str(schedule.get("context_type") or "routine").strip().lower()
        if context_type not in {
            "availability",
            "childcare",
            "energy",
            "household",
            "location",
            "routine",
        }:
            raise ValueError("I need a recognized context type for that calendar context window.")
        scheduling_effect = str(
            schedule.get("scheduling_effect") or "informational"
        ).strip().lower()
        if scheduling_effect not in {
            "informational",
            "prefer",
            "prefer_avoid",
            "strongly_avoid",
        }:
            raise ValueError("I could not identify how that context should affect scheduling.")
        schedule.update(
            {
                "context_type": context_type,
                "scheduling_effect": scheduling_effect,
                "blocks_time": False,
            }
        )
    elif item_kind is not None:
        schedule.update({"context_type": None, "blocks_time": bool(schedule.get("blocks_time", True))})
        if schedule["blocks_time"]:
            schedule["scheduling_effect"] = "hard"
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
