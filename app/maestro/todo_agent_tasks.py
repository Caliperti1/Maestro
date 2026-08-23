"""Bridge low-urgency routed todos into Maestro's background workflow runtime."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Domain, Report, Task, Todo, WorkflowQueueItem, WorkflowRun
from app.maestro.channel import record_channel_message
from app.maestro.orchestrator import MaestroOrchestratorError, MaestroOrchestratorService
from app.memory.todo_scheduling import TodoSchedulingService


class TodoAgentTaskService:
    def __init__(self, session: Session, *, orchestrator: MaestroOrchestratorService | None = None):
        self.session = session
        self.orchestrator = orchestrator or MaestroOrchestratorService(session)

    def run_once(self, *, claim_limit: int = 2) -> dict[str, int]:
        reconciled = self._reconcile_active()
        started = 0
        pending = self.session.scalars(
            select(Todo)
            .where(
                Todo.agent_task.is_(True),
                Todo.agent_task_status.in_(["not_agent", "pending", "retry"]),
                Todo.status.notin_(["done", "archived"]),
                Todo.workflow_task_id.is_(None),
            )
            .order_by(Todo.priority.desc(), Todo.created_at)
            .limit(claim_limit)
        ).all()
        for todo in pending:
            if self._start(todo):
                started += 1
        return {"started": started, "reconciled": reconciled}

    def _start(self, todo: Todo) -> bool:
        todo.agent_task_status = "planning"
        todo.last_agent_task_attempt_at = datetime.now(UTC)
        todo.agent_task_error = None
        should_notify = not (todo.metadata_ or {}).get("planning_notified_at")
        if should_notify:
            todo.metadata_ = {
                **(todo.metadata_ or {}),
                "planning_notified_at": datetime.now(UTC).isoformat(),
            }
        self.session.commit()
        if should_notify:
            record_channel_message(
                self.session,
                sender="maestro",
                content=(
                    f"Chris, I picked up the background task **{todo.title}** and am preparing "
                    "a one-time workflow now. I will run it automatically and ask you here only "
                    "if I need missing information or approval for a protected action."
                ),
                metadata={
                    "channel_visibility": "global",
                    "message_type": "todo_agent_task_planning",
                    "todo_id": str(todo.id),
                },
            )
        domain = self.session.get(Domain, todo.domain_id) if todo.domain_id else None
        prompt = (
            "Create a one-time background agent workflow to complete this routed task. "
            "Do not merely discuss or re-route it as another todo. Ask Chris only when information "
            "that materially blocks a sound plan is missing. When work completes, produce the normal "
            "report, artifacts, and concise Maestro completion message.\n\n"
            f"Domain: {domain.key if domain else 'global'}\n"
            f"Task: {todo.title}\nDescription: {todo.description}\n"
            f"Due: {todo.due_at or 'none'}\nPriority: {todo.priority}"
        )
        try:
            plan = self.orchestrator.create_plan(prompt)
            parent_task = self.session.get(Task, uuid.UUID(plan.parent_task_id))
            if parent_task is None:
                raise MaestroOrchestratorError("Agent-task workflow parent was not persisted.")
            parent_task.source_type = "todo_agent_task"
            parent_task.input_payload = {
                **(parent_task.input_payload or {}),
                "originating_todo_id": str(todo.id),
                "originating_todo_title": todo.title,
            }
            self._link_clarification_todos(todo, parent_task)
            self.session.commit()
            blocking = [
                item.title
                for item in plan.work_items
                if item.needs_user_input and item.blocks_execution
            ]
            if plan.is_chat_only or blocking or not plan.subtasks:
                reason = f"I need more information before I can action `{todo.title}`. " + (
                    "Please clarify: " + "; ".join(blocking)
                    if blocking
                    else "Please add the missing outcome or constraints to the task."
                )
                todo.agent_task_status = "needs_input"
                todo.agent_task_error = reason
                record_channel_message(
                    self.session,
                    sender="maestro",
                    content=reason,
                    metadata={
                        "channel_visibility": "global",
                        "message_type": "todo_agent_task_rfi",
                        "todo_id": str(todo.id),
                    },
                )
                return False
            self.orchestrator.enqueue_plan(plan.plan_id)
            run = self.session.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.parent_task_id == uuid.UUID(plan.parent_task_id)
                )
            )
            todo.workflow_task_id = uuid.UUID(plan.parent_task_id)
            todo.workflow_run_id = run.id if run else None
            todo.agent_task_status = "queued"
            todo.metadata_ = {
                **(todo.metadata_ or {}),
                "agent_task_plan_id": plan.plan_id,
                "agent_task_parent_task_id": plan.parent_task_id,
                "agent_task_started_at": datetime.now(UTC).isoformat(),
            }
            self.session.commit()
            return True
        except (MaestroOrchestratorError, OSError, ValueError) as exc:
            todo.agent_task_status = "failed"
            todo.agent_task_error = str(exc)
            self.session.commit()
            return False

    def _reconcile_active(self) -> int:
        reconciled = 0
        todos = self.session.scalars(
            select(Todo).where(
                Todo.agent_task.is_(True),
                Todo.workflow_run_id.is_not(None),
                Todo.agent_task_status.in_(["queued", "running", "blocked"]),
            )
        ).all()
        for todo in todos:
            run = self.session.get(WorkflowRun, todo.workflow_run_id)
            if run is None:
                continue
            if run.status in {"queued", "running", "executing", "pending"}:
                todo.agent_task_status = "running" if run.status != "queued" else "queued"
                continue
            if run.status in {"blocked", "awaiting_approval", "needs_input"}:
                todo.agent_task_status = "blocked"
                todo.agent_task_error = (
                    run.error_message or "The workflow needs your input or approval."
                )
                continue
            if run.status in {"complete", "completed"}:
                quality = self._assess_completion(run)
                if not quality.passed:
                    todo.agent_task_status = "needs_input"
                    todo.agent_task_error = quality.reason
                    if not (todo.metadata_ or {}).get("quality_gate_notified_at"):
                        record_channel_message(
                            self.session,
                            sender="maestro",
                            content=(
                                f"Chris, the background workflow for **{todo.title}** finished, "
                                "but I did not close the task because its completion check failed.\n\n"
                                f"{quality.reason}"
                            ),
                            metadata={
                                "channel_visibility": "global",
                                "message_type": "todo_agent_task_quality_failed",
                                "todo_id": str(todo.id),
                                "workflow_run_id": str(run.id),
                            },
                        )
                        todo.metadata_ = {
                            **(todo.metadata_ or {}),
                            "quality_gate_notified_at": datetime.now(UTC).isoformat(),
                            "quality_gate_status": "failed",
                            "quality_gate_reason": quality.reason,
                        }
                    reconciled += 1
                    continue
                todo.agent_task_status = "completed"
                todo.status = "done"
                todo.agent_task_error = None
                todo.metadata_ = {
                    **(todo.metadata_ or {}),
                    "quality_gate_status": "passed",
                    "quality_gate_reason": quality.reason,
                }
                self._link_run_clarification_todos(todo, run)
                self._close_linked_clarification_todos(
                    todo,
                    resolution="The linked agent workflow completed and passed its quality check.",
                )
                TodoSchedulingService(self.session).sync_projection(todo, commit=False)
                if not (todo.metadata_ or {}).get("completion_notified_at"):
                    summary = _run_summary(run)
                    record_channel_message(
                        self.session,
                        sender="maestro",
                        content=f"Chris, I completed the background task **{todo.title}**.\n\n{summary}",
                        metadata={
                            "channel_visibility": "global",
                            "message_type": "todo_agent_task_complete",
                            "todo_id": str(todo.id),
                            "workflow_run_id": str(run.id),
                        },
                    )
                    todo.metadata_ = {
                        **(todo.metadata_ or {}),
                        "completion_notified_at": datetime.now(UTC).isoformat(),
                    }
                reconciled += 1
            elif run.status in {"failed", "cancelled", "archived"}:
                todo.agent_task_status = "failed"
                todo.agent_task_error = (
                    run.error_message or f"Workflow ended with status {run.status}."
                )
                reconciled += 1
        self.session.commit()
        return reconciled

    def apply_clarification(
        self,
        todo: Todo,
        *,
        clarification: str,
        message_id: uuid.UUID | str | None = None,
    ) -> Todo:
        """Attach a direct Maestro reply to a preflight RFI and retry the originating task."""
        now = datetime.now(UTC)
        clarifications = list((todo.metadata_ or {}).get("agent_task_clarifications") or [])
        clarifications.append(
            {
                "message_id": str(message_id) if message_id else None,
                "content": clarification,
                "received_at": now.isoformat(),
            }
        )
        metadata = {
            **(todo.metadata_ or {}),
            "agent_task_clarifications": clarifications,
            "last_agent_task_clarification_at": now.isoformat(),
        }
        metadata.pop("planning_notified_at", None)
        todo.metadata_ = metadata
        clarification_line = f"User clarification: {clarification}"
        if clarification_line not in (todo.description or ""):
            todo.description = "\n\n".join(
                part for part in ((todo.description or "").strip(), clarification_line) if part
            )
        todo.agent_task_status = "retry"
        todo.agent_task_error = None
        self._close_linked_clarification_todos(
            todo,
            resolution=f"Chris answered the workflow clarification: {clarification}",
        )
        self.session.commit()
        self.session.refresh(todo)
        return todo

    def _link_clarification_todos(self, originating_todo: Todo, parent_task: Task) -> None:
        for candidate in self._open_human_input_todos(originating_todo):
            if str((candidate.provenance or {}).get("task_id") or "") != str(parent_task.id):
                continue
            candidate.metadata_ = {
                **(candidate.metadata_ or {}),
                "blocking_for_todo_id": str(originating_todo.id),
                "blocking_for_task_id": str(parent_task.id),
            }

    def _link_run_clarification_todos(self, originating_todo: Todo, run: WorkflowRun) -> None:
        queue_items = self.session.scalars(
            select(WorkflowQueueItem).where(WorkflowQueueItem.workflow_run_id == run.id)
        ).all()
        task_ids = {str(run.parent_task_id)} if run.parent_task_id else set()
        report_ids: set[str] = set()
        artifact_ids: set[str] = set()
        for item in queue_items:
            if item.child_task_id:
                task_ids.add(str(item.child_task_id))
            output = item.output_payload or {}
            if output.get("task_id"):
                task_ids.add(str(output["task_id"]))
            if output.get("report_id"):
                report_ids.add(str(output["report_id"]))
            if output.get("artifact_id"):
                artifact_ids.add(str(output["artifact_id"]))

        for candidate in self._open_human_input_todos(originating_todo):
            provenance = candidate.provenance or {}
            direct_match = bool(
                str(provenance.get("task_id") or "") in task_ids
                or str(provenance.get("report_id") or "") in report_ids
                or str(provenance.get("artifact_id") or "") in artifact_ids
            )
            ref_match = any(
                str(ref.get("task_id") or ref.get("report_id") or ref.get("artifact_id") or "")
                in (task_ids | report_ids | artifact_ids)
                for ref in (candidate.source_refs or [])
                if isinstance(ref, dict)
            )
            if not direct_match and not ref_match:
                continue
            candidate.metadata_ = {
                **(candidate.metadata_ or {}),
                "blocking_for_todo_id": str(originating_todo.id),
                "blocking_for_workflow_run_id": str(run.id),
            }

    def _close_linked_clarification_todos(self, todo: Todo, *, resolution: str) -> None:
        scheduling = TodoSchedulingService(self.session)
        for candidate in self._open_human_input_todos(todo):
            if str((candidate.metadata_ or {}).get("blocking_for_todo_id") or "") != str(todo.id):
                continue
            candidate.status = "done"
            candidate.metadata_ = {
                **(candidate.metadata_ or {}),
                "resolved_by_agent_task_id": str(todo.id),
                "resolution": resolution,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
            scheduling.sync_projection(candidate, commit=False)

    def _open_human_input_todos(self, todo: Todo) -> list[Todo]:
        return list(
            self.session.scalars(
                select(Todo).where(
                    Todo.id != todo.id,
                    Todo.todo_type == "human_input",
                    Todo.status.notin_(["done", "archived"]),
                    (Todo.domain_id == todo.domain_id)
                    if todo.domain_id
                    else Todo.domain_id.is_(None),
                )
            ).all()
        )

    def _assess_completion(self, run: WorkflowRun) -> "CompletionAssessment":
        queue_items = self.session.scalars(
            select(WorkflowQueueItem).where(WorkflowQueueItem.workflow_run_id == run.id)
        ).all()
        unfinished = [item for item in queue_items if item.status != "completed"]
        if unfinished:
            labels = ", ".join(item.external_key for item in unfinished[:3])
            return CompletionAssessment(False, f"Workflow items are not complete: {labels}.")

        reports = self._reports_for_queue_items(queue_items)
        explicit_statuses: list[str] = []
        for report in reports:
            payload = _parse_report_payload(report.body_markdown)
            explicit_statuses.extend(_completion_statuses(payload, report.structured_data))
        failed_status = next(
            (status for status in explicit_statuses if _is_failed_completion_status(status)),
            None,
        )
        if failed_status:
            return CompletionAssessment(
                False,
                f"The agent reported its result as `{failed_status}`. Review the report before retrying.",
            )
        if explicit_statuses:
            return CompletionAssessment(True, f"Agent completion status: {explicit_statuses[0]}.")

        summary = _run_summary(run).lower()
        failure_phrases = (
            "did not execute",
            "could not complete",
            "couldn't complete",
            "unable to complete",
            "cannot responsibly",
            "needs additional information",
        )
        if any(phrase in summary for phrase in failure_phrases):
            return CompletionAssessment(
                False, "The workflow summary says the requested work is incomplete."
            )
        return CompletionAssessment(True, "No incomplete or failed result was reported.")

    def _reports_for_queue_items(self, queue_items: list[WorkflowQueueItem]) -> list[Report]:
        report_ids: list[uuid.UUID] = []
        for item in queue_items:
            raw_id = (item.output_payload or {}).get("report_id")
            try:
                report_id = uuid.UUID(str(raw_id)) if raw_id else None
            except ValueError:
                report_id = None
            if report_id and report_id not in report_ids:
                report_ids.append(report_id)
        if not report_ids:
            return []
        return list(self.session.scalars(select(Report).where(Report.id.in_(report_ids))).all())


@dataclass(frozen=True)
class CompletionAssessment:
    passed: bool
    reason: str


def _parse_report_payload(body: str) -> dict[str, Any]:
    stripped = body.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _completion_statuses(payload: dict[str, Any], structured_data: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for source in (payload, structured_data or {}):
        values.append(source.get("status"))
        completion = source.get("completion")
        if isinstance(completion, dict):
            values.append(completion.get("status"))
        summary = source.get("summary")
        if isinstance(summary, dict):
            values.append(summary.get("status"))
    return [str(value).strip() for value in values if str(value or "").strip()]


def _is_failed_completion_status(status: str) -> bool:
    normalized = status.lower().replace("_", " ").replace("-", " ")
    return any(
        token in normalized
        for token in ("incomplete", "blocked", "failed", "failure", "partial", "needs input")
    )


def _run_summary(run: WorkflowRun) -> str:
    payload = run.output_payload or {}
    return str(
        payload.get("chat_summary")
        or payload.get("summary")
        or run.error_message
        or "The linked workflow finished and its report and artifacts are available in Maestro."
    ).strip()
