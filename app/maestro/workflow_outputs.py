"""Canonical workflow output collection.

Workflows may produce reports, routed objects, tangible artifacts, notifications, and operational
trace data. This service turns the scheduler/orchestrator execution records into one durable run-log
entry that Maestro and Chris can inspect later.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Agent,
    Domain,
    Report,
    WorkflowNotification,
    WorkflowQueueItem,
    WorkflowRun,
    WorkflowRunLogEntry,
)


class WorkflowOutputService:
    def __init__(self, session: Session):
        self.session = session

    def record_run_log(self, run: WorkflowRun) -> WorkflowRunLogEntry:
        queue_items = self.session.scalars(
            select(WorkflowQueueItem)
            .where(
                WorkflowQueueItem.workflow_run_id == run.id,
                WorkflowQueueItem.status != "archived",
            )
            .order_by(WorkflowQueueItem.stage_index, WorkflowQueueItem.position)
        ).all()
        report_ids: list[str] = []
        artifact_ids: list[str] = []
        routed_item_ids: list[str] = []
        agent_work: list[dict[str, Any]] = []
        for item in queue_items:
            item_summary = self._queue_item_summary(item)
            agent_work.append(item_summary)
            report_id = item_summary.get("report_id")
            artifact_id = item_summary.get("artifact_id")
            if report_id and report_id not in report_ids:
                report_ids.append(str(report_id))
            if artifact_id and artifact_id not in artifact_ids:
                artifact_ids.append(str(artifact_id))
            for routed_id in item_summary.get("routed_item_ids") or []:
                if routed_id not in routed_item_ids:
                    routed_item_ids.append(str(routed_id))

        output_payload = run.output_payload or {}
        for report_id in self._string_list(output_payload.get("report_ids")):
            if report_id not in report_ids:
                report_ids.append(report_id)
        if output_payload.get("synthesis_report_id") and str(output_payload["synthesis_report_id"]) not in report_ids:
            report_ids.append(str(output_payload["synthesis_report_id"]))
        if output_payload.get("artifact_id") and str(output_payload["artifact_id"]) not in artifact_ids:
            artifact_ids.append(str(output_payload["artifact_id"]))
        for routed_id in self._string_list(output_payload.get("routed_item_ids")):
            if routed_id not in routed_item_ids:
                routed_item_ids.append(routed_id)

        notification_ids = [
            str(notification.id)
            for notification in self.session.scalars(
                select(WorkflowNotification).where(WorkflowNotification.workflow_run_id == run.id)
            ).all()
        ]

        existing = self.session.scalar(
            select(WorkflowRunLogEntry).where(WorkflowRunLogEntry.workflow_run_id == run.id)
        )
        entry = existing or WorkflowRunLogEntry(workflow_run_id=run.id)
        if existing is None:
            self.session.add(entry)
        entry.workflow_definition_id = run.workflow_definition_id
        entry.parent_task_id = run.parent_task_id
        entry.conversation_id = run.conversation_id
        entry.domain_id = run.domain_id
        entry.status = run.status
        entry.title = self._run_title(run)
        entry.summary = self._run_summary(run, agent_work=agent_work)
        entry.run_started_at = run.started_at
        entry.run_completed_at = run.completed_at or datetime.now(UTC)
        entry.agent_work = agent_work
        entry.report_ids = report_ids
        entry.routed_item_ids = routed_item_ids
        entry.artifact_ids = artifact_ids
        entry.notification_ids = notification_ids
        entry.metadata_ = {
            "source_type": run.source_type,
            "priority": run.priority,
            "fairness_group": run.fairness_group,
            "workflow_definition_id": str(run.workflow_definition_id) if run.workflow_definition_id else None,
            "parent_task_id": str(run.parent_task_id) if run.parent_task_id else None,
            "queue_item_count": len(queue_items),
        }
        self.session.commit()
        self.session.refresh(entry)
        return entry

    def create_notification(
        self,
        run: WorkflowRun | None,
        *,
        title: str,
        message: str,
        severity: str = "info",
        notification_type: str = "workflow",
        target: str = "maestro_chat",
        status: str = "pending",
        delivered_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowNotification:
        compact_title = _notification_preview(title)
        compact_message = _notification_preview(message)
        notification_metadata = {
            **(metadata or {}),
            "title_truncated": len(title) > len(compact_title),
            "message_truncated": len(message) > len(compact_message),
        }
        notification = WorkflowNotification(
            workflow_run_id=run.id if run is not None else None,
            conversation_id=run.conversation_id if run is not None else None,
            domain_id=run.domain_id if run is not None else None,
            severity=severity,
            status=status,
            title=compact_title,
            message=compact_message,
            notification_type=notification_type,
            target=target,
            delivered_at=delivered_at,
            metadata_=notification_metadata,
        )
        self.session.add(notification)
        self.session.commit()
        self.session.refresh(notification)
        return notification
    def _queue_item_summary(self, item: WorkflowQueueItem) -> dict[str, Any]:
        agent = self.session.get(Agent, item.agent_id) if item.agent_id else None
        domain = self.session.get(Domain, item.domain_id) if item.domain_id else None
        output_payload = item.output_payload or {}
        agent_run = (
            output_payload.get("agent_run")
            if isinstance(output_payload.get("agent_run"), dict)
            else output_payload
        )
        report_id = agent_run.get("report_id") if isinstance(agent_run, dict) else None
        report_title = None
        if report_id:
            report = self.session.get(Report, uuid.UUID(str(report_id)))
            report_title = report.title if report is not None else None
        return {
            "queue_item_id": str(item.id),
            "external_key": item.external_key,
            "status": item.status,
            "stage_index": item.stage_index,
            "position": item.position,
            "agent_key": agent.key if agent else None,
            "agent_name": agent.name if agent else None,
            "domain_key": domain.key if domain else None,
            "objective": item.objective,
            "dependency_keys": item.dependency_keys,
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            "error_message": item.error_message,
            "task_id": agent_run.get("task_id") if isinstance(agent_run, dict) else None,
            "report_id": report_id,
            "report_title": report_title,
            "artifact_id": agent_run.get("artifact_id") if isinstance(agent_run, dict) else None,
            "routed_item_ids": self._string_list(agent_run.get("routed_item_ids") if isinstance(agent_run, dict) else None),
            "output_preview": agent_run.get("output_preview") if isinstance(agent_run, dict) else None,
        }

    def _run_title(self, run: WorkflowRun) -> str:
        payload = run.input_payload or {}
        return str(payload.get("summary") or payload.get("definition_name") or run.id)[:240]

    def _run_summary(self, run: WorkflowRun, *, agent_work: list[dict[str, Any]]) -> str:
        output_payload = run.output_payload or {}
        for key in ("chat_summary", "synthesis", "summary"):
            value = str(output_payload.get(key) or "").strip()
            if value:
                return value[:4000]
        completed = sum(1 for item in agent_work if item.get("status") == "completed")
        blocked = sum(1 for item in agent_work if item.get("status") == "blocked")
        failed = sum(1 for item in agent_work if item.get("status") == "failed")
        return (
            f"Workflow finished with status {run.status}. "
            f"{completed} queue item(s) completed, {blocked} blocked, {failed} failed."
        )

    def _string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
        return []


def _notification_preview(value: str, *, max_chars: int = 240) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."
