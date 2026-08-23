"""Product project, repository, and issue intelligence API."""

# FastAPI dependency injection intentionally uses Depends in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Domain,
    ProductIssue,
    ProductIssueRelation,
    ProductProject,
    RepositoryProfile,
)
from app.db.session import get_db
from app.issues.github_sync import GitHubIssueSyncService
from app.issues.repositories import ensure_repository_workflows
from app.issues.service import ProductIssueService, issue_payload, slug
from app.issues.worker import RepositoryIntelligenceWorker
from app.memory.repository_observer import RepositoryObserverService

router = APIRouter(prefix="/issues", tags=["issues"])


class ProjectRequest(BaseModel):
    domain_key: str
    key: str
    name: str
    summary: str = ""
    vision: str = ""


class RepositoryRequest(BaseModel):
    domain_key: str
    project_key: str
    key: str
    display_name: str
    external_repo: str
    local_path: str | None = None
    default_branch: str = "main"


class CaptureIssueRequest(BaseModel):
    domain_key: str | None = None
    project_key: str | None = None
    repository_key: str | None = None
    title: str | None = None
    problem: str = ""
    desired_outcome: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    notes: str = ""
    issue_type: str = "feature"
    priority: str = "normal"
    agent_task: bool = False
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    use_semantic_match: bool = True


class UpdateIssueRequest(BaseModel):
    updates: dict[str, Any]


class IssueRelationRequest(BaseModel):
    target_issue_id: uuid.UUID
    relation_type: str
    rationale: str = ""
    confidence: float = Field(default=1.0, ge=0, le=1)


@router.get("")
def list_issues(
    query: str = "",
    domain_key: str | None = None,
    project_key: str | None = None,
    repository_key: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=250),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    issues = ProductIssueService(db).search(
        query=query,
        domain_key=domain_key,
        project_key=project_key,
        repository_key=repository_key,
        status=status,
        limit=limit,
    )
    return {"issues": [_issue_detail(db, issue) for issue in issues]}


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)) -> dict[str, Any]:
    projects = db.scalars(select(ProductProject).order_by(ProductProject.name)).all()
    repositories = db.scalars(select(RepositoryProfile).order_by(RepositoryProfile.display_name)).all()
    return {
        "projects": [
            {
                "id": str(project.id), "domain_id": str(project.domain_id), "key": project.key,
                "name": project.name, "summary": project.summary, "vision": project.vision,
                "status": project.status,
                "repositories": [_repository_payload(repo) for repo in repositories if repo.project_id == project.id],
            }
            for project in projects
        ]
    }


@router.post("/projects")
def create_project(body: ProjectRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    domain = _domain(db, body.domain_key)
    key = slug(body.key)
    project = db.scalar(select(ProductProject).where(ProductProject.domain_id == domain.id, ProductProject.key == key))
    if project is None:
        project = ProductProject(
            domain_id=domain.id, key=key, name=body.name, summary=body.summary, vision=body.vision,
            provenance={"source_system": "maestro_ui", "created_at": datetime.now(UTC).isoformat()},
        )
        db.add(project)
    else:
        project.name, project.summary, project.vision = body.name, body.summary, body.vision
    db.commit()
    db.refresh(project)
    return {"project": {"id": str(project.id), "key": project.key, "name": project.name}}


@router.post("/repositories")
def register_repository(body: RepositoryRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    domain = _domain(db, body.domain_key)
    project = db.scalar(select(ProductProject).where(ProductProject.domain_id == domain.id, ProductProject.key == slug(body.project_key)))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    profile = db.scalar(select(RepositoryProfile).where(RepositoryProfile.provider == "github", RepositoryProfile.external_repo == body.external_repo))
    registration = None
    if body.local_path:
        try:
            registration = RepositoryObserverService(db).register(
                key=f"repo:{body.key}", path=body.local_path, domain=domain, display_name=body.display_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if profile is None:
        profile = RepositoryProfile(
            domain_id=domain.id, project_id=project.id,
            source_registration_id=registration.id if registration else None,
            key=body.key, display_name=body.display_name, external_repo=body.external_repo,
            local_path=body.local_path, default_branch=body.default_branch,
            provenance={"source_system": "maestro_ui", "created_at": datetime.now(UTC).isoformat()},
        )
        db.add(profile)
    else:
        profile.domain_id, profile.project_id = domain.id, project.id
        profile.key, profile.display_name = body.key, body.display_name
        profile.local_path, profile.default_branch = body.local_path, body.default_branch
        if registration:
            profile.source_registration_id = registration.id
    db.flush()
    workflows = ensure_repository_workflows(db, profile, domain)
    db.commit()
    db.refresh(profile)
    return {"repository": _repository_payload(profile), "workflows": workflows}


@router.post("/repositories/{repository_id}/sync")
def sync_repository_issues(repository_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    profile = db.get(RepositoryProfile, repository_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Repository not found.")
    try:
        result = GitHubIssueSyncService(db).sync(profile)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"result": result.__dict__}


@router.post("/maintenance/run")
def run_issue_maintenance(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Development/manual trigger; normal operation uses the quiet background worker."""
    return {"result": RepositoryIntelligenceWorker(db).run_once()}


@router.post("/capture")
def capture_issue(body: CaptureIssueRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = ProductIssueService(db).capture(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": result.status, "message": result.message, "clarification": result.clarification,
        "matched_issue_id": result.matched_issue_id, "confidence": result.confidence,
        "issue": _issue_detail(db, result.issue) if result.issue else None,
    }


@router.get("/{issue_id}")
def get_issue(issue_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    issue = db.get(ProductIssue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Product issue not found.")
    return {"issue": _issue_detail(db, issue)}


@router.patch("/{issue_id}")
def update_issue(issue_id: uuid.UUID, body: UpdateIssueRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        issue = ProductIssueService(db).update(issue_id, body.updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"issue": _issue_detail(db, issue)}


@router.post("/{issue_id}/relations")
def relate_issue(issue_id: uuid.UUID, body: IssueRelationRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        relation = ProductIssueService(db).relate(issue_id, body.target_issue_id, body.relation_type, rationale=body.rationale, confidence=body.confidence)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"relation": _relation_payload(relation)}


def _issue_detail(db: Session, issue: ProductIssue) -> dict[str, Any]:
    payload = issue_payload(issue)
    project = db.get(ProductProject, issue.project_id)
    repository = db.get(RepositoryProfile, issue.repository_id) if issue.repository_id else None
    relations = db.scalars(select(ProductIssueRelation).where((ProductIssueRelation.source_issue_id == issue.id) | (ProductIssueRelation.target_issue_id == issue.id))).all()
    payload.update({
        "project": {"key": project.key, "name": project.name} if project else None,
        "repository": _repository_payload(repository) if repository else None,
        "relations": [_relation_payload(relation) for relation in relations],
    })
    return payload


def _repository_payload(profile: RepositoryProfile) -> dict[str, Any]:
    return {
        "id": str(profile.id), "domain_id": str(profile.domain_id), "project_id": str(profile.project_id),
        "key": profile.key, "display_name": profile.display_name, "provider": profile.provider,
        "external_repo": profile.external_repo, "local_path": profile.local_path,
        "default_branch": profile.default_branch, "current_commit": profile.current_commit,
        "last_observed_at": profile.last_observed_at.isoformat() if profile.last_observed_at else None,
        "last_synced_at": profile.last_synced_at.isoformat() if profile.last_synced_at else None,
        "status": profile.status, "sync_config": profile.sync_config,
    }


def _relation_payload(relation: ProductIssueRelation) -> dict[str, Any]:
    return {
        "id": str(relation.id), "source_issue_id": str(relation.source_issue_id),
        "target_issue_id": str(relation.target_issue_id), "relation_type": relation.relation_type,
        "rationale": relation.rationale, "confidence": relation.confidence,
    }


def _domain(db: Session, key: str) -> Domain:
    domain = db.scalar(select(Domain).where(Domain.key == key.strip().lower()))
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found.")
    return domain
