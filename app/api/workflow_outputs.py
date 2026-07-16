from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import Domain, Report, WorkflowNotification, WorkflowRunLogEntry
from app.db.repositories import WorkflowNotificationRepository, WorkflowRunLogRepository
from app.db.session import get_db

router = APIRouter(prefix="/workflow-outputs", tags=["workflow-outputs"])


@router.get("/run-log")
def list_run_log(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    entries = WorkflowRunLogRepository(db).list_recent(limit=limit)
    return {"entries": [_run_log_payload(db, entry) for entry in entries]}


@router.get("/run-log/{entry_id}")
def get_run_log_entry(entry_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    entry = db.get(WorkflowRunLogEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown workflow run-log entry.")
    return {"entry": _run_log_payload(db, entry)}


@router.get("/reports")
def list_reports(
    limit: int = Query(default=50, ge=1, le=200),
    include_archived: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    candidates = db.scalars(select(Report).order_by(Report.created_at.desc()).limit(limit * 3)).all()
    reports = [
        report for report in candidates if include_archived or not _report_is_archived(report)
    ][:limit]
    return {"reports": [_report_payload(db, report, include_body=False) for report in reports]}


@router.get("/reports/{report_id}")
def get_report(report_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Unknown report.")
    return {"report": _report_payload(db, report, include_body=True)}


@router.patch("/reports/{report_id}/archive")
def archive_report(report_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Unknown report.")
    _set_report_archived(report, True)
    db.commit()
    db.refresh(report)
    return {"report": _report_payload(db, report, include_body=True)}


@router.post("/reports/archive")
def archive_reports(
    include_archived: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    reports = db.scalars(select(Report)).all()
    archived_count = 0
    for report in reports:
        if _report_is_archived(report) and not include_archived:
            continue
        _set_report_archived(report, True)
        archived_count += 1
    db.commit()
    return {"archived_count": archived_count}


@router.get("/notifications")
def list_notifications(
    status: str = "pending",
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if status == "pending":
        notifications = WorkflowNotificationRepository(db).list_pending(limit=limit)
    else:
        notifications = db.scalars(
            select(WorkflowNotification)
            .where(WorkflowNotification.status == status)
            .order_by(WorkflowNotification.created_at.desc())
            .limit(limit)
        ).all()
    return {"notifications": [_notification_payload(db, notification) for notification in notifications]}


def _run_log_payload(db: Session, entry: WorkflowRunLogEntry) -> dict[str, Any]:
    domain = db.get(Domain, entry.domain_id) if entry.domain_id else None
    return {
        "id": str(entry.id),
        "workflow_run_id": str(entry.workflow_run_id),
        "workflow_definition_id": str(entry.workflow_definition_id) if entry.workflow_definition_id else None,
        "parent_task_id": str(entry.parent_task_id) if entry.parent_task_id else None,
        "conversation_id": str(entry.conversation_id) if entry.conversation_id else None,
        "domain_id": str(entry.domain_id) if entry.domain_id else None,
        "domain_key": domain.key if domain else None,
        "status": entry.status,
        "title": entry.title,
        "summary": entry.summary,
        "run_started_at": entry.run_started_at.isoformat() if entry.run_started_at else None,
        "run_completed_at": entry.run_completed_at.isoformat() if entry.run_completed_at else None,
        "agent_work": entry.agent_work,
        "report_ids": entry.report_ids,
        "routed_item_ids": entry.routed_item_ids,
        "artifact_ids": entry.artifact_ids,
        "notification_ids": entry.notification_ids,
        "metadata": entry.metadata_,
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }


def _report_payload(db: Session, report: Report, *, include_body: bool) -> dict[str, Any]:
    domain = db.get(Domain, report.domain_id) if report.domain_id else None
    payload = {
        "id": str(report.id),
        "task_id": str(report.task_id),
        "domain_id": str(report.domain_id) if report.domain_id else None,
        "domain_key": domain.key if domain else None,
        "title": report.title,
        "summary": report.summary,
        "source_type": report.report_type,
        "report_type": report.report_type,
        "archived": _report_is_archived(report),
        "created_at": report.created_at.isoformat(),
        "updated_at": report.updated_at.isoformat(),
    }
    if include_body:
        payload["body_markdown"] = report.body_markdown
    return payload


def _report_is_archived(report: Report) -> bool:
    return bool((report.structured_data or {}).get("archived"))


def _set_report_archived(report: Report, archived: bool) -> None:
    report.structured_data = {
        **(report.structured_data or {}),
        "archived": archived,
    }
    flag_modified(report, "structured_data")


def _notification_payload(db: Session, notification: WorkflowNotification) -> dict[str, Any]:
    domain = db.get(Domain, notification.domain_id) if notification.domain_id else None
    return {
        "id": str(notification.id),
        "workflow_run_id": str(notification.workflow_run_id) if notification.workflow_run_id else None,
        "conversation_id": str(notification.conversation_id) if notification.conversation_id else None,
        "domain_id": str(notification.domain_id) if notification.domain_id else None,
        "domain_key": domain.key if domain else None,
        "severity": notification.severity,
        "status": notification.status,
        "title": notification.title,
        "message": notification.message,
        "notification_type": notification.notification_type,
        "target": notification.target,
        "delivered_at": notification.delivered_at.isoformat() if notification.delivered_at else None,
        "metadata": notification.metadata_,
        "created_at": notification.created_at.isoformat(),
        "updated_at": notification.updated_at.isoformat(),
    }
