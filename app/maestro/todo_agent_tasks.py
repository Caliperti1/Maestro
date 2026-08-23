"""Bridge low-urgency routed todos into Maestro's background workflow runtime."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Domain, Todo, WorkflowRun
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
                todo.agent_task_status = "completed"
                todo.status = "done"
                todo.agent_task_error = None
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


def _run_summary(run: WorkflowRun) -> str:
    payload = run.output_payload or {}
    return str(
        payload.get("chat_summary")
        or payload.get("summary")
        or run.error_message
        or "The linked workflow finished and its report and artifacts are available in Maestro."
    ).strip()
