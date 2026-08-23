"""Bridge agent-marked product issues into background coding workflows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Domain,
    ProductIssue,
    ProductIssueExecution,
    ProductProject,
    RepositoryProfile,
    Task,
    WorkflowQueueItem,
    WorkflowRun,
)
from app.maestro.channel import record_channel_message
from app.maestro.orchestrator import MaestroOrchestratorError, MaestroOrchestratorService


class ProductIssueAgentTaskService:
    def __init__(self, session: Session, *, orchestrator: MaestroOrchestratorService | None = None):
        self.session = session
        self.orchestrator = orchestrator or MaestroOrchestratorService(session)

    def run_once(self, *, claim_limit: int = 1) -> dict[str, int]:
        reconciled = self._reconcile()
        started = 0
        issues = self.session.scalars(
            select(ProductIssue)
            .where(
                ProductIssue.agent_task.is_(True),
                ProductIssue.agent_task_status.in_(["pending", "retry"]),
                ProductIssue.status.notin_(["completed", "cancelled", "superseded"]),
                ProductIssue.workflow_task_id.is_(None),
            )
            .order_by(ProductIssue.priority.desc(), ProductIssue.created_at)
            .limit(claim_limit)
        ).all()
        for issue in issues:
            started += int(self._start(issue))
        return {"started": started, "reconciled": reconciled}

    def _start(self, issue: ProductIssue) -> bool:
        issue.agent_task_status = "planning"
        issue.status = "active"
        self.session.commit()
        project = self.session.get(ProductProject, issue.project_id)
        repository = self.session.get(RepositoryProfile, issue.repository_id) if issue.repository_id else None
        domain = self.session.get(Domain, issue.domain_id)
        record_channel_message(
            self.session,
            sender="maestro",
            content=f"Chris, I picked up **{issue.title}** and am preparing its coding workflow. I will ask here if scope or a protected action needs your decision.",
            metadata={"channel_visibility": "global", "message_type": "product_issue_planning", "product_issue_id": str(issue.id)},
        )
        prompt = (
            "Create and execute a one-time coding workflow for this canonical product issue. "
            "Use the repository's coding agent and GitHub tools. Preserve issue scope, work on an "
            "isolated feature branch, create a pull request linked with `Closes #N` when a GitHub "
            "issue number exists, and stop for Chris's PR approval before merge/hot reload. Ask one "
            "focused RFI if essential implementation information is missing.\n\n"
            f"Domain: {domain.key if domain else 'unknown'}\nProject: {project.name if project else 'unknown'}\n"
            f"Repository: {repository.external_repo if repository else 'not assigned'}\n"
            f"Issue ID: {issue.id}\nGitHub issue: {issue.external_number or 'not yet published'}\n"
            f"Title: {issue.title}\nProblem: {issue.problem}\nDesired outcome: {issue.desired_outcome}\n"
            f"Acceptance criteria:\n" + "\n".join(f"- {item}" for item in issue.acceptance_criteria or []) +
            f"\nNotes: {issue.notes}"
        )
        try:
            plan = self.orchestrator.create_plan(prompt)
            parent = self.session.get(Task, uuid.UUID(plan.parent_task_id))
            if parent is None:
                raise MaestroOrchestratorError("Product issue workflow parent was not persisted.")
            parent.source_type = "product_issue_agent_task"
            parent.input_payload = {**(parent.input_payload or {}), "originating_product_issue_id": str(issue.id), "repository_id": str(issue.repository_id) if issue.repository_id else None, "github_issue_number": issue.external_number}
            blocking = [item.title for item in plan.work_items if item.needs_user_input and item.blocks_execution]
            if plan.is_chat_only or blocking or not plan.subtasks:
                reason = "Please clarify: " + ("; ".join(blocking) if blocking else "the repository or expected behavior needed to implement this issue.")
                issue.agent_task_status = "needs_input"
                issue.status = "blocked"
                issue.metadata_ = {**(issue.metadata_ or {}), "agent_task_error": reason}
                record_channel_message(self.session, sender="maestro", content=f"Chris, I need one detail before I can implement **{issue.title}**. {reason}", metadata={"channel_visibility": "global", "message_type": "product_issue_rfi", "product_issue_id": str(issue.id)})
                self.session.commit()
                return False
            self.orchestrator.enqueue_plan(plan.plan_id)
            run = self.session.scalar(select(WorkflowRun).where(WorkflowRun.parent_task_id == parent.id))
            issue.workflow_task_id = parent.id
            issue.workflow_run_id = run.id if run else None
            issue.agent_task_status = "queued"
            execution = ProductIssueExecution(issue_id=issue.id, workflow_task_id=parent.id, workflow_run_id=run.id if run else None, status="queued", metadata_={"plan_id": plan.plan_id})
            self.session.add(execution)
            self.session.commit()
            return True
        except (MaestroOrchestratorError, OSError, ValueError) as exc:
            issue.agent_task_status = "failed"
            issue.status = "blocked"
            issue.metadata_ = {**(issue.metadata_ or {}), "agent_task_error": str(exc)}
            self.session.commit()
            return False

    def _reconcile(self) -> int:
        count = 0
        issues = self.session.scalars(select(ProductIssue).where(ProductIssue.agent_task.is_(True), ProductIssue.workflow_run_id.is_not(None), ProductIssue.agent_task_status.in_(["queued", "running", "blocked"]))).all()
        for issue in issues:
            run = self.session.get(WorkflowRun, issue.workflow_run_id)
            if run is None:
                continue
            execution = self.session.scalar(select(ProductIssueExecution).where(ProductIssueExecution.issue_id == issue.id, ProductIssueExecution.workflow_run_id == run.id))
            if run.status in {"queued", "running", "executing", "pending"}:
                issue.agent_task_status = "running" if run.status != "queued" else "queued"
                if execution:
                    execution.status = issue.agent_task_status
                continue
            if run.status in {"blocked", "awaiting_approval", "needs_input"}:
                issue.agent_task_status = "blocked"
                issue.status = "blocked"
                if execution:
                    execution.status = "blocked"
                    execution.error_message = run.error_message
                continue
            if run.status in {"complete", "completed"}:
                issue.agent_task_status = "completed"
                issue.status = "completed"
                details = self._execution_details(run)
                if execution:
                    execution.status = "completed"
                    execution.codex_session_id = details.get("session_id")
                    execution.branch_name = details.get("branch_name")
                    execution.pull_request_number = details.get("pr_number")
                    execution.pull_request_url = details.get("pr_url")
                record_channel_message(self.session, sender="maestro", content=f"Chris, I completed the coding workflow for **{issue.title}**. {(run.output_payload or {}).get('summary') or 'The run log and report are ready for review.'!s}", metadata={"channel_visibility": "global", "message_type": "product_issue_complete", "product_issue_id": str(issue.id), "workflow_run_id": str(run.id), **details})
                count += 1
            elif run.status in {"failed", "cancelled", "archived"}:
                issue.agent_task_status = "failed"
                issue.status = "blocked"
                if execution:
                    execution.status = "failed"
                    execution.error_message = run.error_message
                count += 1
        self.session.commit()
        return count

    def _execution_details(self, run: WorkflowRun) -> dict[str, object]:
        details: dict[str, object] = {}
        for item in self.session.scalars(select(WorkflowQueueItem).where(WorkflowQueueItem.workflow_run_id == run.id)).all():
            payload = item.output_payload or {}
            for source, target in (("session_id", "session_id"), ("branch_name", "branch_name"), ("pr_number", "pr_number"), ("pull_request_number", "pr_number"), ("pr_url", "pr_url"), ("pull_request_url", "pr_url")):
                if payload.get(source) and target not in details:
                    details[target] = payload[source]
        return details

    def apply_clarification(
        self,
        issue: ProductIssue,
        *,
        clarification: str,
        message_id: uuid.UUID | str | None = None,
    ) -> ProductIssue:
        entries = list((issue.metadata_ or {}).get("agent_task_clarifications") or [])
        entries.append({"message_id": str(message_id) if message_id else None, "content": clarification, "received_at": datetime.now(UTC).isoformat()})
        issue.notes = "\n\n".join(part for part in (issue.notes.strip(), f"User clarification: {clarification}") if part)
        issue.metadata_ = {key: value for key, value in {**(issue.metadata_ or {}), "agent_task_clarifications": entries}.items() if key != "agent_task_error"}
        issue.agent_task_status = "retry"
        issue.status = "ready"
        issue.workflow_task_id = None
        issue.workflow_run_id = None
        self.session.commit()
        self.session.refresh(issue)
        return issue
