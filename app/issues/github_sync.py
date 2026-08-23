"""Deterministic two-way synchronization between canonical issues and GitHub issues."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ProductIssue, RepositoryProfile, ToolConnection
from app.issues.service import normalize_issue_title


@dataclass(frozen=True)
class IssueSyncResult:
    repository: str
    imported: int = 0
    updated_from_github: int = 0
    published: int = 0
    pushed_updates: int = 0
    closed: int = 0
    conflicts: int = 0
    unchanged: int = 0


class GitHubIssueClient(Protocol):
    def list_issues(self, repo: str) -> list[dict[str, Any]]: ...
    def create_issue(self, repo: str, *, title: str, body: str, labels: list[str]) -> dict[str, Any]: ...
    def update_issue(self, repo: str, number: int, *, title: str, body: str, state: str) -> dict[str, Any]: ...


class GitHubCliIssueClient:
    def __init__(self, env: dict[str, str] | None = None):
        self.env = env or os.environ.copy()

    def list_issues(self, repo: str) -> list[dict[str, Any]]:
        result = self._json([
            "api", "--paginate", f"repos/{repo}/issues?state=all&per_page=100",
            "--jq", ".[] | select(.pull_request == null)",
        ], stream=True)
        return result if isinstance(result, list) else []

    def create_issue(self, repo: str, *, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        args = ["api", "--method", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args.extend(["-f", f"labels[]={label}"])
        result = self._json(args)
        if not isinstance(result, dict):
            raise RuntimeError("GitHub issue creation returned an unexpected response.")  # noqa: TRY004
        return result

    def update_issue(self, repo: str, number: int, *, title: str, body: str, state: str) -> dict[str, Any]:
        result = self._json([
            "api", "--method", "PATCH", f"repos/{repo}/issues/{number}",
            "-f", f"title={title}", "-f", f"body={body}", "-f", f"state={state}",
        ])
        if not isinstance(result, dict):
            raise RuntimeError("GitHub issue update returned an unexpected response.")  # noqa: TRY004
        return result

    def _json(self, args: list[str], *, stream: bool = False) -> Any:
        completed = subprocess.run(
            ["gh", *args], env=self.env, text=True, capture_output=True, timeout=180,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "GitHub CLI failed.")
        text = completed.stdout.strip()
        if not stream:
            return json.loads(text or "null")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class GitHubIssueSyncService:
    def __init__(self, session: Session, *, client: GitHubIssueClient | None = None):
        self.session = session
        self.client = client

    def sync(self, profile: RepositoryProfile) -> IssueSyncResult:
        client = self.client or GitHubCliIssueClient(_github_env(self.session, profile.domain_id))
        remote = client.list_issues(profile.external_repo)
        by_number = {
            issue.external_number: issue
            for issue in self.session.scalars(select(ProductIssue).where(ProductIssue.repository_id == profile.id)).all()
            if issue.external_number is not None
        }
        counters = {key: 0 for key in ("imported", "updated_from_github", "published", "pushed_updates", "closed", "conflicts", "unchanged")}
        now = datetime.now(UTC)
        for item in remote:
            number = int(item["number"])
            remote_snapshot = _remote_snapshot(item)
            issue = by_number.get(number)
            if issue is None:
                issue = ProductIssue(
                    domain_id=profile.domain_id, project_id=profile.project_id, repository_id=profile.id,
                    issue_type=_issue_type(item), title=str(item.get("title") or f"GitHub issue #{number}"),
                    normalized_title=normalize_issue_title(str(item.get("title") or "")),
                    problem=str(item.get("body") or ""), desired_outcome="", acceptance_criteria=[], notes="",
                    priority="normal", status="completed" if item.get("state") == "closed" else "ready",
                    external_provider="github", external_repo=profile.external_repo,
                    external_number=number, external_url=item.get("html_url"), external_state=item.get("state"),
                    external_updated_at=_timestamp(item.get("updated_at")), sync_status="synced",
                    last_synced_at=now, sync_snapshot=remote_snapshot,
                    source_refs=[{"source_system": "github", "source_id": f"{profile.external_repo}#{number}", "url": item.get("html_url")}],
                    provenance={"source_system": "github", "ingested_at": now.isoformat()},
                    metadata_={"labels": [label.get("name") for label in item.get("labels") or [] if isinstance(label, dict)]},
                )
                self.session.add(issue)
                counters["imported"] += 1
                continue
            local_snapshot = _local_snapshot(issue)
            base = issue.sync_snapshot or {}
            remote_changed = remote_snapshot != base
            local_changed = local_snapshot != _shared_from_snapshot(base)
            if remote_changed and local_changed and issue.sync_status == "pending_push":
                issue.sync_status = "conflict"
                issue.metadata_ = {**(issue.metadata_ or {}), "sync_conflict": {"remote": remote_snapshot, "local": local_snapshot, "detected_at": now.isoformat()}}
                counters["conflicts"] += 1
            elif remote_changed:
                was_open = issue.status != "completed"
                _apply_remote(issue, item, remote_snapshot, now)
                counters["updated_from_github"] += 1
                if was_open and issue.status == "completed":
                    counters["closed"] += 1
            else:
                counters["unchanged"] += 1

        local_items = self.session.scalars(select(ProductIssue).where(ProductIssue.repository_id == profile.id)).all()
        for issue in local_items:
            if issue.external_number is None and issue.sync_status in {"local", "pending_push"}:
                issue.external_repo = profile.external_repo
                created = client.create_issue(profile.external_repo, title=issue.title, body=_github_body(issue), labels=[])
                _apply_remote(issue, created, _remote_snapshot(created), now)
                counters["published"] += 1
            elif issue.external_number is not None and issue.sync_status == "pending_push":
                updated = client.update_issue(
                    profile.external_repo, issue.external_number, title=issue.title,
                    body=_github_body(issue), state="closed" if issue.status == "completed" else "open",
                )
                _apply_remote(issue, updated, _remote_snapshot(updated), now)
                counters["pushed_updates"] += 1
        profile.last_synced_at = now
        self.session.commit()
        return IssueSyncResult(repository=profile.external_repo, **counters)


def _github_env(session: Session, domain_id: Any) -> dict[str, str]:
    env = os.environ.copy()
    connection = session.scalar(select(ToolConnection).where(ToolConnection.domain_id == domain_id, ToolConnection.tool_key == "github", ToolConnection.is_active.is_(True)))
    if connection is None:
        return env
    config = connection.config or {}
    env_name = str(config.get("env_token_name") or "").strip()
    if env_name:
        token = os.getenv(env_name) or _dotenv_value(env_name)
        if not token:
            raise RuntimeError(f"GitHub token env var is not set: {env_name}")
        env["GH_TOKEN"] = token
    if config.get("token"):
        env["GH_TOKEN"] = str(config["token"])
    return env


def _dotenv_value(key: str) -> str | None:
    for path in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return None


def _github_body(issue: ProductIssue) -> str:
    criteria = "\n".join(f"- [ ] {item}" for item in issue.acceptance_criteria or []) or "- [ ] Define during implementation"
    return (
        f"## Problem\n{issue.problem or 'Not specified.'}\n\n"
        f"## Desired outcome\n{issue.desired_outcome or 'Not specified.'}\n\n"
        f"## Acceptance criteria\n{criteria}\n\n"
        f"## Notes\n{issue.notes or 'None.'}\n\n"
        f"<!-- maestro-issue-id: {issue.id} -->"
    )


def _remote_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(item.get("title") or ""), "body": str(item.get("body") or ""),
        "state": str(item.get("state") or "open"), "updated_at": item.get("updated_at"),
    }


def _shared_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {"title": snapshot.get("title", ""), "body": snapshot.get("body", ""), "state": snapshot.get("state", "open")}


def _local_snapshot(issue: ProductIssue) -> dict[str, Any]:
    return {"title": issue.title, "body": _github_body(issue), "state": "closed" if issue.status == "completed" else "open"}


def _apply_remote(issue: ProductIssue, item: dict[str, Any], snapshot: dict[str, Any], now: datetime) -> None:
    issue.title = str(item.get("title") or issue.title)
    issue.normalized_title = normalize_issue_title(issue.title)
    if not issue.problem or issue.external_provider == "github":
        issue.problem = str(item.get("body") or issue.problem)
    issue.status = "completed" if item.get("state") == "closed" else ("ready" if issue.status == "completed" else issue.status)
    issue.external_provider = "github"
    issue.external_repo = issue.external_repo or ""
    issue.external_number = int(item["number"])
    issue.external_url = item.get("html_url") or issue.external_url
    issue.external_state = str(item.get("state") or "open")
    issue.external_updated_at = _timestamp(item.get("updated_at"))
    issue.sync_status = "synced"
    issue.last_synced_at = now
    issue.sync_snapshot = snapshot
    issue.metadata_ = {key: value for key, value in (issue.metadata_ or {}).items() if key != "sync_conflict"}


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _issue_type(item: dict[str, Any]) -> str:
    labels = {str(label.get("name") or "").lower() for label in item.get("labels") or [] if isinstance(label, dict)}
    for value in ("bug", "feature", "chore", "research", "architecture", "story", "idea"):
        if value in labels or f"maestro:{value}" in labels:
            return value
    return "feature"
