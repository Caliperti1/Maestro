"""Quiet change detection that emits visible repository and issue-hygiene workflow runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Report,
    RepositoryProfile,
    SourceRegistration,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunLogEntry,
)
from app.issues.github_sync import GitHubIssueSyncService
from app.issues.hygiene import ProductIssueHygieneService
from app.memory.repository_observer import RepositoryObserverService


class RepositoryIntelligenceWorker:
    def __init__(self, session: Session):
        self.session = session

    def run_once(self) -> dict[str, int]:
        observed = hygiene = failures = 0
        for profile in self.session.scalars(select(RepositoryProfile).where(RepositoryProfile.status == "active")).all():
            try:
                if profile.source_registration_id:
                    observed += int(self._observe(profile))
                if not profile.last_synced_at or profile.last_synced_at <= datetime.now(UTC) - timedelta(hours=24):
                    hygiene += int(self._hygiene(profile))
            except Exception as exc:  # noqa: BLE001 - one repository failure must not stop others.
                self.session.rollback()
                self._record_failure(profile, str(exc))
                failures += 1
        return {"observed": observed, "hygiene": hygiene, "failures": failures}

    def _observe(self, profile: RepositoryProfile) -> bool:
        registration = self.session.get(SourceRegistration, profile.source_registration_id)
        if registration is None:
            return False
        result = RepositoryObserverService(self.session).observe(registration)
        if result.status == "unchanged":
            return False
        profile.current_commit = result.commit
        profile.last_observed_at = datetime.now(UTC)
        definition = self._definition(f"repository-intelligence:{profile.key}")
        summary = f"Observed {len(result.changed_files)} changed or baseline files at {result.commit[:10]}."
        self._record_run(definition, profile, summary, result.report_markdown or summary, "repository_state")
        self.session.commit()
        return True

    def _hygiene(self, profile: RepositoryProfile) -> bool:
        sync = GitHubIssueSyncService(self.session).sync(profile)
        reconciled = ProductIssueHygieneService(self.session).run(profile)
        changed = sum((sync.imported, sync.updated_from_github, sync.published, sync.pushed_updates, sync.closed, sync.conflicts, reconciled.merged, reconciled.related, reconciled.conflicts))
        if not changed:
            return False
        definition = self._definition(f"issue-hygiene:{profile.key}")
        summary = (
            f"Synchronized {profile.external_repo}: {sync.imported} imported, {sync.published} published, "
            f"{sync.closed} closed, {sync.conflicts} sync conflicts, {reconciled.merged} duplicates merged, "
            f"and {reconciled.related + reconciled.conflicts} issue relationships recorded."
        )
        body = f"# Issue Hygiene - {profile.display_name}\n\n{summary}\n\nGitHub sync: `{sync}`\n\nSemantic reconciliation: `{reconciled}`"
        self._record_run(definition, profile, summary, body, "issue_hygiene")
        self.session.commit()
        return True

    def _definition(self, key: str) -> WorkflowDefinition:
        definition = self.session.scalar(select(WorkflowDefinition).where(WorkflowDefinition.key == key))
        if definition is None:
            raise ValueError(f"Missing repository workflow definition: {key}")
        return definition

    def _record_run(self, definition: WorkflowDefinition, profile: RepositoryProfile, summary: str, body: str, report_type: str) -> None:
        now = datetime.now(UTC)
        run = WorkflowRun(workflow_definition_id=definition.id, domain_id=profile.domain_id, source_type="repository_intelligence", status="completed", priority=definition.priority, fairness_group=definition.fairness_group, input_payload={"repository_id": str(profile.id)}, output_payload={"summary": summary}, scheduled_for=now, started_at=now, completed_at=now)
        self.session.add(run)
        self.session.flush()
        report = Report(domain_id=profile.domain_id, title=f"{now.date().isoformat()} - {definition.name}", report_type=report_type, summary=summary, body_markdown=body, structured_data={"workflow_run_id": str(run.id), "repository_id": str(profile.id), "source_system": "repository_intelligence"})
        self.session.add(report)
        self.session.flush()
        self.session.add(WorkflowRunLogEntry(workflow_run_id=run.id, workflow_definition_id=definition.id, domain_id=profile.domain_id, status="completed", title=definition.name, summary=summary, run_started_at=now, run_completed_at=now, report_ids=[str(report.id)], routed_item_ids=[], artifact_ids=[], notification_ids=[], metadata_={"repository_id": str(profile.id), "quiet_background": True}))

    def _record_failure(self, profile: RepositoryProfile, error: str) -> None:
        definition = self.session.scalar(select(WorkflowDefinition).where(WorkflowDefinition.key == f"issue-hygiene:{profile.key}"))
        if definition is None:
            return
        now = datetime.now(UTC)
        run = WorkflowRun(workflow_definition_id=definition.id, domain_id=profile.domain_id, source_type="repository_intelligence", status="failed", priority="low", fairness_group=definition.fairness_group, input_payload={"repository_id": str(profile.id)}, error_message=error, scheduled_for=now, started_at=now, completed_at=now)
        self.session.add(run)
        self.session.flush()
        self.session.add(WorkflowRunLogEntry(workflow_run_id=run.id, workflow_definition_id=definition.id, domain_id=profile.domain_id, status="failed", title=definition.name, summary=error, run_started_at=now, run_completed_at=now, agent_work=[], report_ids=[], routed_item_ids=[], artifact_ids=[], notification_ids=[], metadata_={"repository_id": str(profile.id)}))
        self.session.commit()
