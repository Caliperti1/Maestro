"""Project/repository registration and durable maintenance workflow setup."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Domain, ProductProject, RepositoryProfile, WorkflowDefinition
from app.memory.repository_observer import RepositoryObserverService


def ensure_repository_workflows(
    session: Session,
    profile: RepositoryProfile,
    domain: Domain,
) -> list[dict[str, Any]]:
    specs = [
        (
            f"repository-intelligence:{profile.key}",
            f"Repository Intelligence - {profile.display_name}",
            "Observe repository changes and produce an inspectable repository-state report.",
            "event",
            {"event_type": "repository_changed", "repository_id": str(profile.id), "quiet_when_unchanged": True, "managed_by": "repository_intelligence_worker"},
        ),
        (
            f"issue-hygiene:{profile.key}",
            f"Issue Hygiene - {profile.display_name}",
            "Synchronize GitHub state and reconcile duplicate, contradictory, stale, or completed product issues.",
            "recurring",
            {"interval_minutes": 1440, "repository_id": str(profile.id), "quiet_when_unchanged": True, "managed_by": "repository_intelligence_worker"},
        ),
    ]
    results: list[dict[str, Any]] = []
    for key, name, description, trigger_type, trigger_config in specs:
        definition = session.scalar(select(WorkflowDefinition).where(WorkflowDefinition.key == key))
        if definition is None:
            definition = WorkflowDefinition(
                domain_id=domain.id, key=key, name=name, description=description,
                trigger_type=trigger_type, trigger_config=trigger_config,
                workflow_spec={"workflow_kind": key.split(":", 1)[0], "repository_id": str(profile.id), "report_required": True, "visible": True},
                priority="low", fairness_group=domain.key, is_active=True,
            )
            session.add(definition)
        else:
            definition.trigger_config = {**(definition.trigger_config or {}), **trigger_config}
            definition.workflow_spec = {**(definition.workflow_spec or {}), "repository_id": str(profile.id), "report_required": True, "visible": True}
        results.append({"key": key, "name": name, "trigger_type": trigger_type})
    return results


def ensure_runtime_repository(session: Session, *, path: str | Path = ".") -> RepositoryProfile | None:
    """Register the running Maestro checkout without hardcoding a machine-specific path."""
    repository = Path(path).expanduser().resolve()
    if not (repository / ".git").exists():
        return None
    domain = session.scalar(select(Domain).where(Domain.key == "maestro-development"))
    if domain is None:
        return None
    project = session.scalar(select(ProductProject).where(ProductProject.domain_id == domain.id, ProductProject.key == "maestro"))
    if project is None:
        project = ProductProject(domain_id=domain.id, key="maestro", name="Maestro", summary="Maestro personal orchestration system.", vision="Provide Chris one intelligent interface for knowledge, routed context, and delegated work.", source_refs=[], provenance={"source_system": "runtime_registration"})
        session.add(project)
        session.flush()
    remote = _git(repository, "remote", "get-url", "origin")
    external_repo = _github_repo(remote) if remote else ""
    if not external_repo:
        return None
    profile = session.scalar(select(RepositoryProfile).where(RepositoryProfile.provider == "github", RepositoryProfile.external_repo == external_repo))
    registration = RepositoryObserverService(session).register(key="repo:maestro", path=str(repository), domain=domain, display_name="Maestro")
    if profile is None:
        profile = RepositoryProfile(domain_id=domain.id, project_id=project.id, source_registration_id=registration.id, key="maestro", display_name="Maestro", external_repo=external_repo, local_path=str(repository), default_branch=_git(repository, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/") or "main", sync_config={}, provenance={"source_system": "runtime_registration"})
        session.add(profile)
        session.flush()
    else:
        profile.project_id = project.id
        profile.domain_id = domain.id
        profile.source_registration_id = registration.id
        profile.local_path = str(repository)
    ensure_repository_workflows(session, profile, domain)
    session.commit()
    session.refresh(profile)
    return profile


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(repository), *args], text=True, capture_output=True, timeout=20, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _github_repo(remote: str) -> str:
    value = remote.strip().removesuffix(".git")
    match = re.search(r"github\.com[/:]([^/]+/[^/]+)$", value)
    return match.group(1) if match else ""
