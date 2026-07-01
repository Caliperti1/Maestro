import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Agent, Domain, Task, ToolCall, ToolConnection
from app.db.repositories import AgentRepository, DomainRepository


class ToolExecutionError(ValueError):
    pass


@dataclass(frozen=True)
class ToolExecutionRequest:
    agent_key: str
    tool_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


@dataclass(frozen=True)
class ToolExecutionResult:
    tool_key: str
    status: str
    output: dict[str, Any] | None
    error_message: str | None
    tool_call_id: str
    connection_id: str | None


@dataclass(frozen=True)
class ToolExecutionContext:
    session: Session
    agent: Agent
    domain: Domain
    task: Task
    connection: ToolConnection | None
    dry_run: bool = False


class ToolAdapter(Protocol):
    key: str

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class GitHubIssueLabelPlan:
    requested: list[str]
    preferred: list[str]
    required: list[str]
    to_apply: list[str]
    required_missing: list[str]
    optional_missing: list[str]


class ToolExecutionService:
    def __init__(
        self,
        session: Session,
        *,
        adapters: dict[str, ToolAdapter] | None = None,
    ):
        self.session = session
        self.adapters = adapters or default_tool_adapters()

    def execute_for_task(
        self,
        request: ToolExecutionRequest,
        *,
        task: Task,
    ) -> ToolExecutionResult:
        agent = AgentRepository(self.session).get_by_key(request.agent_key)
        if agent is None:
            raise ToolExecutionError(f"Unknown agent: {request.agent_key}")
        if task.assigned_agent_id != agent.id:
            raise ToolExecutionError("Tool calls must be executed by the task's assigned agent.")
        domain = DomainRepository(self.session).get(agent.domain_id)
        if domain is None:
            raise ToolExecutionError(f"Agent {agent.key} has no domain.")
        self._assert_agent_can_use_tool(agent, request.tool_key)
        connection = self._connection_for(domain, request.tool_key)
        adapter = self.adapters.get(request.tool_key)
        if adapter is None:
            raise ToolExecutionError(f"No tool adapter is registered for {request.tool_key}.")

        tool_call = ToolCall(
            task_id=task.id,
            agent_id=agent.id,
            tool_connection_id=connection.id if connection is not None else None,
            tool_name=request.tool_key,
            input_payload={
                "dry_run": request.dry_run,
                "payload": _redact_payload(request.payload),
                "connection": {
                    "id": str(connection.id),
                    "auth_type": connection.auth_type,
                    "display_name": connection.display_name,
                }
                if connection is not None
                else None,
            },
            status="running",
            started_at=datetime.now(UTC),
        )
        self.session.add(tool_call)
        self.session.commit()
        self.session.refresh(tool_call)

        context = ToolExecutionContext(
            session=self.session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
            dry_run=request.dry_run,
        )
        try:
            output = adapter.execute(context, request.payload)
            tool_call.status = "complete"
            tool_call.output_payload = output
            tool_call.completed_at = datetime.now(UTC)
            self.session.commit()
            self.session.refresh(tool_call)
            return ToolExecutionResult(
                tool_key=request.tool_key,
                status=tool_call.status,
                output=tool_call.output_payload,
                error_message=None,
                tool_call_id=str(tool_call.id),
                connection_id=str(connection.id) if connection is not None else None,
            )
        except Exception as exc:
            tool_call.status = "failed"
            tool_call.error_message = str(exc)
            tool_call.completed_at = datetime.now(UTC)
            self.session.commit()
            self.session.refresh(tool_call)
            return ToolExecutionResult(
                tool_key=request.tool_key,
                status=tool_call.status,
                output=tool_call.output_payload,
                error_message=tool_call.error_message,
                tool_call_id=str(tool_call.id),
                connection_id=str(connection.id) if connection is not None else None,
            )

    def propose_for_task(
        self,
        request: ToolExecutionRequest,
        *,
        task: Task,
        rationale: str | None = None,
        safety_level: str = "approval_required",
        reason: str = "Tool requires approval before execution.",
    ) -> ToolExecutionResult:
        agent = AgentRepository(self.session).get_by_key(request.agent_key)
        if agent is None:
            raise ToolExecutionError(f"Unknown agent: {request.agent_key}")
        if task.assigned_agent_id != agent.id:
            raise ToolExecutionError("Tool calls must be proposed by the task's assigned agent.")
        domain = DomainRepository(self.session).get(agent.domain_id)
        if domain is None:
            raise ToolExecutionError(f"Agent {agent.key} has no domain.")
        self._assert_agent_can_use_tool(agent, request.tool_key)
        connection = self._connection_for(domain, request.tool_key)
        tool_call = ToolCall(
            task_id=task.id,
            agent_id=agent.id,
            tool_connection_id=connection.id if connection is not None else None,
            tool_name=request.tool_key,
            input_payload={
                "dry_run": request.dry_run,
                "payload": _redact_payload(request.payload),
                "rationale": rationale,
                "connection": {
                    "id": str(connection.id),
                    "auth_type": connection.auth_type,
                    "display_name": connection.display_name,
                }
                if connection is not None
                else None,
            },
            output_payload={
                "approval_required": True,
                "safety_level": safety_level,
                "reason": reason,
                "write_status": "awaiting_approval",
                "approval": {
                    "tool_call_id": None,
                    "required": True,
                    "approved": False,
                    "rejected": False,
                    "safety_level": safety_level,
                    "reason": reason,
                },
            },
            status="approval_required",
            started_at=datetime.now(UTC),
        )
        self.session.add(tool_call)
        self.session.commit()
        self.session.refresh(tool_call)
        preview = _approval_preview(
            request.tool_key,
            domain=domain,
            connection=connection,
            payload=request.payload,
            safety_level=safety_level,
            reason=reason,
            rationale=rationale,
            tool_call_id=str(tool_call.id),
        )
        tool_call.output_payload = {
            **(tool_call.output_payload or {}),
            "approval": {
                **((tool_call.output_payload or {}).get("approval") or {}),
                "tool_call_id": str(tool_call.id),
            },
            "approval_preview": preview,
            "preview_summary": preview.get("summary"),
        }
        self.session.commit()
        self.session.refresh(tool_call)
        return ToolExecutionResult(
            tool_key=request.tool_key,
            status=tool_call.status,
            output=tool_call.output_payload,
            error_message=tool_call.error_message,
            tool_call_id=str(tool_call.id),
            connection_id=str(connection.id) if connection is not None else None,
        )

    def approve_tool_call(self, tool_call_id: uuid.UUID | str) -> ToolExecutionResult:
        tool_call = self.session.get(ToolCall, uuid.UUID(str(tool_call_id)))
        if tool_call is None:
            raise ToolExecutionError(f"Unknown tool call: {tool_call_id}")
        if tool_call.status != "approval_required":
            raise ToolExecutionError(f"Tool call is not awaiting approval: {tool_call.status}")
        task = self.session.get(Task, tool_call.task_id)
        if task is None:
            raise ToolExecutionError("Approved tool call has no task.")
        agent = self.session.get(Agent, tool_call.agent_id) if tool_call.agent_id else None
        if agent is None:
            raise ToolExecutionError("Approved tool call has no assigned agent.")
        domain = DomainRepository(self.session).get(agent.domain_id)
        if domain is None:
            raise ToolExecutionError(f"Agent {agent.key} has no domain.")
        self._assert_agent_can_use_tool(agent, tool_call.tool_name)
        connection = self._connection_for(domain, tool_call.tool_name)
        adapter = self.adapters.get(tool_call.tool_name)
        if adapter is None:
            raise ToolExecutionError(f"No tool adapter is registered for {tool_call.tool_name}.")
        payload = (tool_call.input_payload or {}).get("payload") or {}
        context = ToolExecutionContext(
            session=self.session,
            agent=agent,
            domain=domain,
            task=task,
            connection=connection,
            dry_run=bool((tool_call.input_payload or {}).get("dry_run")),
        )
        tool_call.status = "running"
        tool_call.output_payload = {
            **(tool_call.output_payload or {}),
            "approval_required": False,
            "write_status": "running",
            "approval": {
                **((tool_call.output_payload or {}).get("approval") or {}),
                "tool_call_id": str(tool_call.id),
                "required": True,
                "approved": True,
                "rejected": False,
                "approved_at": datetime.now(UTC).isoformat(),
            },
        }
        self.session.commit()
        self.session.refresh(tool_call)
        try:
            output = adapter.execute(context, payload)
            proposed_output = tool_call.output_payload or {}
            if isinstance(output, dict):
                output = {
                    **output,
                    "approval": {
                        **(
                            (proposed_output.get("approval") or {})
                            if isinstance(proposed_output, dict)
                            else {}
                        ),
                        "tool_call_id": str(tool_call.id),
                        "required": True,
                        "approved": True,
                        "rejected": False,
                        "approved_at": datetime.now(UTC).isoformat(),
                    },
                }
            tool_call.status = "complete"
            tool_call.output_payload = output
            tool_call.error_message = None
        except Exception as exc:
            tool_call.status = "failed"
            tool_call.error_message = str(exc)
        tool_call.completed_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(tool_call)
        return ToolExecutionResult(
            tool_key=tool_call.tool_name,
            status=tool_call.status,
            output=tool_call.output_payload,
            error_message=tool_call.error_message,
            tool_call_id=str(tool_call.id),
            connection_id=str(connection.id) if connection is not None else None,
        )

    def reject_tool_call(self, tool_call_id: uuid.UUID | str, *, reason: str | None = None) -> ToolExecutionResult:
        tool_call = self.session.get(ToolCall, uuid.UUID(str(tool_call_id)))
        if tool_call is None:
            raise ToolExecutionError(f"Unknown tool call: {tool_call_id}")
        if tool_call.status != "approval_required":
            raise ToolExecutionError(f"Tool call is not awaiting approval: {tool_call.status}")
        tool_call.status = "rejected"
        tool_call.error_message = reason or "Rejected by Chris."
        tool_call.output_payload = {
            **(tool_call.output_payload or {}),
            "approval_required": False,
            "rejected": True,
            "write_status": "rejected",
            "approval": {
                **((tool_call.output_payload or {}).get("approval") or {}),
                "tool_call_id": str(tool_call.id),
                "required": True,
                "approved": False,
                "rejected": True,
                "rejected_at": datetime.now(UTC).isoformat(),
            },
            "reason": tool_call.error_message,
        }
        tool_call.completed_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(tool_call)
        return ToolExecutionResult(
            tool_key=tool_call.tool_name,
            status=tool_call.status,
            output=tool_call.output_payload,
            error_message=tool_call.error_message,
            tool_call_id=str(tool_call.id),
            connection_id=str(tool_call.tool_connection_id) if tool_call.tool_connection_id else None,
        )

    def _assert_agent_can_use_tool(self, agent: Agent, tool_key: str) -> None:
        permissions = agent.tool_permissions or {}
        if tool_key not in permissions:
            raise ToolExecutionError(f"Agent {agent.key} is not authorized for {tool_key}.")
        raw_permission = permissions[tool_key]
        permission = (
            raw_permission
            if isinstance(raw_permission, str)
            else raw_permission.get("permission")
        )
        if str(permission or "use") not in {"use", "read", "write", "admin"}:
            raise ToolExecutionError(f"Agent {agent.key} has invalid permission for {tool_key}.")

    def _connection_for(self, domain: Domain, tool_key: str) -> ToolConnection | None:
        provider_key = _provider_key(tool_key)
        if provider_key != tool_key:
            provider = self.session.scalar(
                select(ToolConnection).where(
                    ToolConnection.domain_id == domain.id,
                    ToolConnection.tool_key == provider_key,
                    ToolConnection.is_active.is_(True),
                )
            )
            if provider is not None:
                return provider
        exact = self.session.scalar(
            select(ToolConnection).where(
                ToolConnection.domain_id == domain.id,
                ToolConnection.tool_key == tool_key,
                ToolConnection.is_active.is_(True),
            )
        )
        if exact is not None:
            return exact
        return None


class GitHubCliToolAdapter:
    def __init__(self, key: str):
        self.key = key

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if context.dry_run:
            return {"dry_run": True, "tool": self.key, "payload": payload}
        if shutil.which("gh") is None:
            raise ToolExecutionError("GitHub CLI (`gh`) is not installed or not on PATH.")
        env = _github_env(context.connection)
        if self.key == "github.repo.list":
            return self._repo_list(context.connection, payload, env=env)
        if self.key == "github.repo.create":
            return self._repo_create(context.connection, payload, env=env)
        repo = _repo_from(context.connection, payload)
        if self.key == "github.repo.get":
            return self._repo_get(repo, env=env)
        if self.key == "github.file.get":
            return self._file_get(repo, payload, env=env)
        if self.key == "github.file.search":
            return self._file_search(repo, payload, env=env)
        if self.key == "github.issue.search":
            return self._issue_search(repo, payload, env=env)
        if self.key == "github.issue.get":
            return self._issue_get(repo, payload, env=env)
        if self.key == "github.issue.create":
            return self._issue_create(context.connection, repo, payload, env=env)
        if self.key == "github.issue.comment":
            return self._issue_comment(repo, payload, env=env)
        if self.key == "github.issue.update":
            return self._issue_update(repo, payload, env=env)
        if self.key == "github.pr.search":
            return self._pr_search(repo, payload, env=env)
        if self.key == "github.pr.get":
            return self._pr_get(repo, payload, env=env)
        if self.key == "github.pr.diff":
            return self._pr_diff(repo, payload, env=env)
        if self.key == "github.pr.checks":
            return self._pr_checks(repo, payload, env=env)
        raise ToolExecutionError(f"Unsupported GitHub tool: {self.key}")

    def _repo_get(self, repo: str, *, env: dict[str, str]) -> dict[str, Any]:
        result = _run_gh_json(
            [
                "repo",
                "view",
                repo,
                "--json",
                "nameWithOwner,description,defaultBranchRef,url,isPrivate",
            ],
            env=env,
        )
        return {
            "repo": repo,
            "result": result,
            "summary": {
                "type": "github_repo",
                "repo": repo,
                "repo_url": result.get("url") if isinstance(result, dict) else None,
                "private": result.get("isPrivate") if isinstance(result, dict) else None,
            },
        }

    def _repo_list(
        self,
        connection: ToolConnection | None,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        owner = _repo_owner(connection, payload)
        limit = _bounded_int(payload.get("limit"), default=30, minimum=1, maximum=100)
        visibility = str(payload.get("visibility") or "all").strip().lower()
        args = [
            "repo",
            "list",
            owner,
            "--limit",
            str(limit),
            "--json",
            "name,nameWithOwner,description,isPrivate,url,updatedAt",
        ]
        if visibility in {"public", "private"}:
            args.extend(["--visibility", visibility])
        repos = _run_gh_json(args, env=env)
        return {
            "owner": owner,
            "limit": limit,
            "visibility": visibility,
            "repos": repos,
            "summary": {
                "type": "github_repo_list",
                "owner": owner,
                "count": len(repos) if isinstance(repos, list) else 0,
                "visibility": visibility,
            },
        }

    def _repo_create(
        self,
        connection: ToolConnection | None,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        name = _required_text(payload, "name")
        owner = str(payload.get("owner") or "").strip() or _repo_owner(connection, payload)
        full_name = name if "/" in name else f"{owner}/{name}"
        private = bool(payload.get("private", True))
        description = str(payload.get("description") or "").strip()
        args = ["repo", "create", full_name, "--private" if private else "--public"]
        if description:
            args.extend(["--description", description])
        if bool(payload.get("add_readme")):
            args.append("--add-readme")
        url = _run_gh_text(args, env=env).strip()
        return {
            "repo": full_name,
            "url": url,
            "repo_url": url,
            "private": private,
            "description": description,
            "write_status": "created",
            "summary": {
                "type": "github_repo",
                "repo": full_name,
                "repo_url": url,
                "private": private,
                "write_status": "created",
            },
        }

    def _file_get(self, repo: str, payload: dict[str, Any], *, env: dict[str, str]) -> dict[str, Any]:
        path = _required_text(payload, "path").lstrip("/")
        ref = str(payload.get("ref") or "").strip()
        max_chars = _bounded_int(payload.get("max_chars"), default=40000, minimum=1000, maximum=100000)
        endpoint = f"repos/{repo}/contents/{quote(path)}"
        if ref:
            endpoint = f"{endpoint}?ref={quote(ref)}"
        result = _run_gh_json(["api", "--method", "GET", endpoint], env=env)
        if isinstance(result, list):
            return {
                "repo": repo,
                "path": path,
                "ref": ref or None,
                "type": "directory",
                "entries": [
                    {
                        "name": item.get("name"),
                        "path": item.get("path"),
                        "type": item.get("type"),
                        "sha": item.get("sha"),
                    }
                    for item in result
                    if isinstance(item, dict)
                ],
            }
        if not isinstance(result, dict):
            raise ToolExecutionError("GitHub file read returned an unexpected response.")
        content = ""
        if result.get("type") == "file":
            encoded = str(result.get("content") or "")
            if result.get("encoding") == "base64" and encoded:
                content = base64.b64decode(encoded).decode("utf-8", errors="replace")
        return {
            "repo": repo,
            "path": path,
            "ref": ref or None,
            "type": result.get("type"),
            "name": result.get("name"),
            "sha": result.get("sha"),
            "size": result.get("size"),
            "download_url": result.get("download_url"),
            "truncated": len(content) > max_chars,
            "content": content[:max_chars],
        }

    def _file_search(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        query = _required_text(payload, "query")
        path = str(payload.get("path") or "").strip().strip("/")
        limit = _bounded_int(payload.get("limit"), default=10, minimum=1, maximum=30)
        search_query = f"{query} repo:{repo}"
        if path:
            search_query = f"{search_query} path:{path}"
        result = _run_gh_json(
            [
                "api",
                "--method",
                "GET",
                "search/code",
                "-f",
                f"q={search_query}",
                "-f",
                f"per_page={limit}",
            ],
            env=env,
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        return {
            "repo": repo,
            "query": query,
            "path": path or None,
            "limit": limit,
            "total_count": result.get("total_count") if isinstance(result, dict) else None,
            "files": [
                {
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "sha": item.get("sha"),
                    "url": item.get("html_url"),
                    "repository": (item.get("repository") or {}).get("full_name")
                    if isinstance(item, dict)
                    else None,
                }
                for item in items
                if isinstance(item, dict)
            ],
        }

    def _issue_search(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        query = _clean_github_search_query(str(payload.get("query") or ""), kind="issue")
        state = str(payload.get("state") or "open").strip() or "open"
        limit = _bounded_int(payload.get("limit"), default=10, minimum=1, maximum=30)
        args = [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,state,labels,url,updatedAt",
        ]
        if query:
            args.extend(["--search", query])
        issues = _run_gh_json(args, env=env)
        return {
            "repo": repo,
            "query": query,
            "state": state,
            "limit": limit,
            "issues": issues,
            "summary": {
                "type": "github_issue_list",
                "repo": repo,
                "count": len(issues) if isinstance(issues, list) else 0,
                "state": state,
                "query": query,
            },
        }

    def _issue_get(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        number = _bounded_int(
            payload.get("number") or payload.get("issue_number"),
            default=0,
            minimum=1,
            maximum=1_000_000,
        )
        issue = _run_gh_json(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,state,body,labels,url,author,createdAt,updatedAt",
            ],
            env=env,
        )
        return {
            "repo": repo,
            "number": number,
            "issue_number": number,
            "issue": issue,
            "issue_url": issue.get("url") if isinstance(issue, dict) else None,
            "title": issue.get("title") if isinstance(issue, dict) else None,
            "summary": {
                "type": "github_issue",
                "repo": repo,
                "issue_number": number,
                "issue_url": issue.get("url") if isinstance(issue, dict) else None,
                "title": issue.get("title") if isinstance(issue, dict) else None,
                "state": issue.get("state") if isinstance(issue, dict) else None,
            },
        }

    def _issue_create(
        self,
        connection: ToolConnection | None,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        title = _required_text(payload, "title")
        body = str(payload.get("body") or "")
        label_policy = _github_issue_label_policy(connection)
        requested_labels = _dedupe_strings(
            [
                *_string_list(payload.get("labels")),
                *label_policy["preferred"],
                *label_policy["required"],
            ]
        )
        labels = self._existing_labels(
            repo,
            requested_labels,
            required_labels=label_policy["required"],
            preferred_labels=label_policy["preferred"],
            env=env,
        )
        if labels.required_missing:
            missing = ", ".join(labels.required_missing)
            raise ToolExecutionError(
                f"Required GitHub issue label(s) are missing in {repo}: {missing}"
            )
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        for label in labels.to_apply:
            args.extend(["--label", label])
        for assignee in _string_list(payload.get("assignees")):
            args.extend(["--assignee", assignee])
        milestone = str(payload.get("milestone") or "").strip()
        if milestone:
            args.extend(["--milestone", milestone])
        url = _run_gh_text(args, env=env).strip()
        issue_number = _github_issue_number_from_url(url)
        return {
            "repo": repo,
            "issue_number": issue_number,
            "number": issue_number,
            "issue_url": url,
            "url": url,
            "title": title,
            "labels": labels.to_apply,
            "labels_applied": labels.to_apply,
            "labels_skipped": labels.optional_missing,
            "skipped_labels": labels.optional_missing,
            "required_labels": labels.required,
            "required_labels_missing": labels.required_missing,
            "requested_labels": labels.requested,
            "preferred_labels": labels.preferred,
            "write_status": "created",
            "summary": {
                "type": "github_issue",
                "repo": repo,
                "issue_number": issue_number,
                "issue_url": url,
                "title": title,
                "labels_applied": labels.to_apply,
                "labels_skipped": labels.optional_missing,
                "write_status": "created",
            },
        }

    def _existing_labels(
        self,
        repo: str,
        requested_labels: list[str],
        *,
        env: dict[str, str],
        required_labels: list[str] | None = None,
        preferred_labels: list[str] | None = None,
    ) -> GitHubIssueLabelPlan:
        required = _dedupe_strings(required_labels or [])
        preferred = _dedupe_strings(preferred_labels or [])
        if not requested_labels:
            return GitHubIssueLabelPlan([], preferred, required, [], required, [])
        raw_labels = _run_gh_json(
            [
                "label",
                "list",
                "--repo",
                repo,
                "--limit",
                "200",
                "--json",
                "name",
            ],
            env=env,
        )
        existing_names = {
            str(item.get("name") or "").lower()
            for item in raw_labels or []
            if isinstance(item, dict)
        }
        existing: list[str] = []
        required_missing: list[str] = []
        optional_missing: list[str] = []
        for label in requested_labels:
            if label.lower() in existing_names:
                existing.append(label)
            elif label.lower() in {item.lower() for item in required}:
                required_missing.append(label)
            else:
                optional_missing.append(label)
        return GitHubIssueLabelPlan(
            requested=_dedupe_strings(requested_labels),
            preferred=preferred,
            required=required,
            to_apply=existing,
            required_missing=required_missing,
            optional_missing=optional_missing,
        )

    def _issue_comment(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        number = _bounded_int(payload.get("number"), default=0, minimum=1, maximum=1_000_000)
        body = _required_text(payload, "body")
        _run_gh_text(["issue", "comment", str(number), "--repo", repo, "--body", body], env=env)
        return {
            "repo": repo,
            "number": number,
            "issue_number": number,
            "commented": True,
            "write_status": "commented",
        }

    def _issue_update(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        number = _bounded_int(payload.get("number"), default=0, minimum=1, maximum=1_000_000)
        args = ["issue", "edit", str(number), "--repo", repo]
        title = str(payload.get("title") or "").strip()
        body = payload.get("body")
        if title:
            args.extend(["--title", title])
        if body is not None:
            args.extend(["--body", str(body)])
        for label in _string_list(payload.get("add_labels")):
            args.extend(["--add-label", label])
        for label in _string_list(payload.get("remove_labels")):
            args.extend(["--remove-label", label])
        for assignee in _string_list(payload.get("add_assignees")):
            args.extend(["--add-assignee", assignee])
        for assignee in _string_list(payload.get("remove_assignees")):
            args.extend(["--remove-assignee", assignee])
        milestone = str(payload.get("milestone") or "").strip()
        if milestone:
            args.extend(["--milestone", milestone])
        if len(args) <= 5:
            raise ToolExecutionError("GitHub issue update requires at least one field to change.")
        _run_gh_text(args, env=env)
        return {
            "repo": repo,
            "number": number,
            "issue_number": number,
            "updated": True,
            "write_status": "updated",
        }

    def _pr_search(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        state = str(payload.get("state") or "open").strip() or "open"
        query = _clean_github_search_query(str(payload.get("query") or ""), kind="pr")
        limit = _bounded_int(payload.get("limit"), default=10, minimum=1, maximum=30)
        args = [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,state,isDraft,labels,url,updatedAt,author,headRefName,baseRefName",
        ]
        if query:
            args.extend(["--search", query])
        prs = _run_gh_json(args, env=env)
        return {
            "repo": repo,
            "query": query,
            "state": state,
            "limit": limit,
            "prs": prs,
            "summary": {
                "type": "github_pr_list",
                "repo": repo,
                "count": len(prs) if isinstance(prs, list) else 0,
                "state": state,
                "query": query,
            },
        }

    def _pr_get(self, repo: str, payload: dict[str, Any], *, env: dict[str, str]) -> dict[str, Any]:
        number = _bounded_int(payload.get("number"), default=0, minimum=1, maximum=1_000_000)
        pr = _run_gh_json(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                (
                    "number,title,state,isDraft,body,labels,url,author,createdAt,updatedAt,"
                    "headRefName,baseRefName,reviewDecision,mergeStateStatus,mergeable,"
                    "statusCheckRollup,files,comments,reviews"
                ),
            ],
            env=env,
        )
        return {
            "repo": repo,
            "number": number,
            "pr_number": number,
            "pr": pr,
            "pr_url": pr.get("url") if isinstance(pr, dict) else None,
            "title": pr.get("title") if isinstance(pr, dict) else None,
            "summary": {
                "type": "github_pr",
                "repo": repo,
                "pr_number": number,
                "pr_url": pr.get("url") if isinstance(pr, dict) else None,
                "title": pr.get("title") if isinstance(pr, dict) else None,
                "state": pr.get("state") if isinstance(pr, dict) else None,
                "review_decision": pr.get("reviewDecision") if isinstance(pr, dict) else None,
            },
        }

    def _pr_diff(self, repo: str, payload: dict[str, Any], *, env: dict[str, str]) -> dict[str, Any]:
        number = _bounded_int(payload.get("number"), default=0, minimum=1, maximum=1_000_000)
        args = ["pr", "diff", str(number), "--repo", repo, "--color", "never"]
        if bool(payload.get("name_only")):
            args.append("--name-only")
        elif bool(payload.get("patch")):
            args.append("--patch")
        diff = _run_gh_text(args, env=env)
        max_chars = _bounded_int(payload.get("max_chars"), default=20000, minimum=1000, maximum=60000)
        return {
            "repo": repo,
            "number": number,
            "pr_number": number,
            "truncated": len(diff) > max_chars,
            "diff": diff[:max_chars],
            "summary": {
                "type": "github_pr_diff",
                "repo": repo,
                "pr_number": number,
                "truncated": len(diff) > max_chars,
                "returned_chars": min(len(diff), max_chars),
            },
        }

    def _pr_checks(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        number = _bounded_int(payload.get("number"), default=0, minimum=1, maximum=1_000_000)
        checks = _run_gh_json(
            [
                "pr",
                "checks",
                str(number),
                "--repo",
                repo,
                "--json",
                "bucket,completedAt,description,event,link,name,startedAt,state,workflow",
            ],
            env=env,
            allowed_exit_codes={0, 8},
        )
        return {
            "repo": repo,
            "number": number,
            "pr_number": number,
            "checks": checks,
            "summary": {
                "type": "github_pr_checks",
                "repo": repo,
                "pr_number": number,
                "count": len(checks) if isinstance(checks, list) else 0,
            },
        }


class CodexCliToolAdapter:
    def __init__(self, key: str):
        self.key = key

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.key != "codex.task.run":
            raise ToolExecutionError(f"Unsupported Codex tool: {self.key}")
        if context.dry_run:
            return {"dry_run": True, "tool": self.key, "payload": payload}
        codex_bin = self._codex_bin(context.connection)
        if codex_bin is None:
            raise ToolExecutionError("Codex CLI is not installed or not on PATH.")
        target_path = self._target_path(context.connection, payload)
        prompt = _required_any_text(payload, ("prompt", "task", "instructions"))
        sandbox = str(payload.get("sandbox") or payload.get("sandbox_mode") or "workspace-write").strip()
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ToolExecutionError("Codex sandbox must be read-only, workspace-write, or danger-full-access.")
        timeout_seconds = _bounded_int(
            payload.get("timeout_seconds"),
            default=900,
            minimum=30,
            maximum=3600,
        )
        model = str(payload.get("model") or "").strip()
        profile = str(payload.get("profile") or "").strip()
        extra_context = str(payload.get("context") or "").strip()
        full_prompt = prompt if not extra_context else f"{prompt}\n\nAdditional context:\n{extra_context}"

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as output_file:
            output_path = output_file.name
        try:
            args = [
                codex_bin,
                "exec",
                "--json",
                "--cd",
                str(target_path),
                "--sandbox",
                sandbox,
                "--output-last-message",
                output_path,
            ]
            if model:
                args.extend(["--model", model])
            if profile:
                args.extend(["--profile", profile])
            if bool(payload.get("ephemeral", False)):
                args.append("--ephemeral")
            args.append(full_prompt)
            completed = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(target_path),
                env=os.environ.copy(),
            )
            events = _parse_jsonl(completed.stdout)
            final_message = ""
            try:
                final_message = Path(output_path).read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                final_message = ""
            if not final_message:
                final_message = _last_agent_message(events)
            result = {
                "target_path": str(target_path),
                "sandbox": sandbox,
                "model": model or None,
                "profile": profile or None,
                "returncode": completed.returncode,
                "session_id": _codex_session_id(events),
                "final_message": final_message,
                "changed_files": _codex_changed_files(events),
                "event_counts": _codex_event_counts(events),
                "stderr_tail": completed.stderr[-4000:],
            }
            if completed.returncode != 0:
                raise ToolExecutionError(
                    completed.stderr.strip()
                    or final_message
                    or f"Codex exited with status {completed.returncode}."
                )
            return result
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(f"Codex task timed out after {timeout_seconds} seconds.") from exc
        finally:
            try:
                Path(output_path).unlink()
            except FileNotFoundError:
                pass

    def _codex_bin(self, connection: ToolConnection | None) -> str | None:
        configured = ""
        if connection is not None:
            configured = str((connection.config or {}).get("codex_bin") or "").strip()
        candidates = [configured] if configured else []
        candidates.extend(
            [
                "codex",
                "/Applications/Codex.app/Contents/Resources/codex",
            ]
        )
        for candidate in candidates:
            if not candidate:
                continue
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
            if Path(candidate).expanduser().exists():
                return str(Path(candidate).expanduser())
        return None

    def _target_path(self, connection: ToolConnection | None, payload: dict[str, Any]) -> Path:
        configured_default = ""
        allowed_roots: list[str] = []
        if connection is not None:
            config = connection.config or {}
            configured_default = str(config.get("default_cwd") or "").strip()
            allowed_roots = _string_list(config.get("allowed_roots"))
        raw_target = str(
            payload.get("target_path")
            or payload.get("target_directory")
            or payload.get("cwd")
            or configured_default
            or os.getcwd()
        ).strip()
        raw_path = Path(raw_target).expanduser()
        if not raw_path.is_absolute() and configured_default:
            raw_path = Path(configured_default).expanduser() / raw_path
        target = raw_path.resolve()
        if not target.exists() or not target.is_dir():
            raise ToolExecutionError(f"Codex target path is not a directory: {target}")
        roots = [Path(root).expanduser().resolve() for root in allowed_roots] if allowed_roots else [target]
        if not any(target == root or root in target.parents for root in roots):
            allowed = ", ".join(str(root) for root in roots)
            raise ToolExecutionError(f"Codex target path must be inside an allowed root: {allowed}")
        return target


def default_tool_adapters() -> dict[str, ToolAdapter]:
    adapters: dict[str, ToolAdapter] = {
        key: GitHubCliToolAdapter(key)
        for key in (
            "github.repo.get",
            "github.repo.list",
            "github.repo.create",
            "github.file.get",
            "github.file.search",
            "github.issue.search",
            "github.issue.get",
            "github.issue.create",
            "github.issue.comment",
            "github.issue.update",
            "github.pr.search",
            "github.pr.get",
            "github.pr.diff",
            "github.pr.checks",
        )
    }
    adapters["codex.task.run"] = CodexCliToolAdapter("codex.task.run")
    return adapters


def tool_result_payload(result: ToolExecutionResult) -> dict[str, Any]:
    return {
        "id": result.tool_call_id,
        "tool_name": result.tool_key,
        "status": result.status,
        "error_message": result.error_message,
        "input_payload": None,
        "output_payload": result.output,
        "connection_id": result.connection_id,
    }


def _approval_preview(
    tool_key: str,
    *,
    domain: Domain,
    connection: ToolConnection | None,
    payload: dict[str, Any],
    safety_level: str,
    reason: str,
    rationale: str | None,
    tool_call_id: str,
) -> dict[str, Any]:
    if tool_key == "github.issue.create":
        return _github_issue_create_preview(
            domain=domain,
            connection=connection,
            payload=payload,
            safety_level=safety_level,
            reason=reason,
            rationale=rationale,
            tool_call_id=tool_call_id,
        )
    return {
        "tool_key": tool_key,
        "tool_call_id": tool_call_id,
        "domain_key": domain.key,
        "summary": f"Approve `{tool_key}` for domain `{domain.key}`.",
        "safety_level": safety_level,
        "reason": reason,
        "rationale": rationale,
        "notable_uncertainty": ["No specialized preview is available for this tool yet."],
    }


def _github_issue_create_preview(
    *,
    domain: Domain,
    connection: ToolConnection | None,
    payload: dict[str, Any],
    safety_level: str,
    reason: str,
    rationale: str | None,
    tool_call_id: str,
) -> dict[str, Any]:
    repo = _optional_repo_from(connection, payload)
    label_policy = _github_issue_label_policy(connection)
    payload_labels = _string_list(payload.get("labels"))
    labels_to_apply = _dedupe_strings(
        [*payload_labels, *label_policy["preferred"], *label_policy["required"]]
    )
    body = str(payload.get("body") or "")
    body_preview = _preview_text(body, max_chars=700)
    labels_skipped: list[str] = []
    labels_create: list[str] = []
    required_missing = [] if label_policy["required"] else []
    uncertainty = [
        (
            "Label existence is verified at approval time with GitHub; optional missing "
            "labels will be skipped."
        ),
        "No GitHub issue is created until this approval is accepted.",
    ]
    if label_policy["required"]:
        uncertainty.append(
            "Configured required labels must exist in the target repository or creation will block."
        )
    if not repo:
        uncertainty.append("Target repo is not configured or provided in the payload.")
    title = str(payload.get("title") or "").strip()
    summary_lines = [
        "GitHub issue creation approval",
        f"Target repo: {repo or 'unknown'}",
        f"Title: {title or '(missing title)'}",
    ]
    if labels_to_apply:
        summary_lines.append(f"Labels to apply if present: {', '.join(labels_to_apply)}")
    if label_policy["required"]:
        summary_lines.append(f"Required labels: {', '.join(label_policy['required'])}")
    if labels_skipped:
        summary_lines.append(f"Labels skipped: {', '.join(labels_skipped)}")
    return {
        "tool_key": "github.issue.create",
        "tool_call_id": tool_call_id,
        "domain_key": domain.key,
        "repo": repo,
        "title": title,
        "body_preview": body_preview,
        "body_truncated": len(body.strip()) > 700,
        "labels_requested": payload_labels,
        "labels_preferred": label_policy["preferred"],
        "labels_required": label_policy["required"],
        "labels_to_apply": labels_to_apply,
        "labels_skipped": labels_skipped,
        "labels_create": labels_create,
        "required_labels_missing": required_missing,
        "notable_uncertainty": uncertainty,
        "safety_level": safety_level,
        "reason": reason,
        "rationale": rationale,
        "summary": "\n".join(summary_lines),
    }


def _github_issue_label_policy(connection: ToolConnection | None) -> dict[str, list[str]]:
    config = connection.config if connection is not None else {}
    config = config or {}
    issue_labels = config.get("issue_labels")
    issue_label_config = issue_labels if isinstance(issue_labels, dict) else {}
    preferred = _dedupe_strings(
        [
            *_string_list(config.get("preferred_issue_labels")),
            *_string_list(config.get("preferred_labels")),
            *_string_list(issue_label_config.get("preferred")),
            *_string_list(issue_label_config.get("optional")),
        ]
    )
    required = _dedupe_strings(
        [
            *_string_list(config.get("required_issue_labels")),
            *_string_list(issue_label_config.get("required")),
        ]
    )
    return {"preferred": preferred, "required": required}


def _optional_repo_from(connection: ToolConnection | None, payload: dict[str, Any]) -> str | None:
    try:
        return _repo_from(connection, payload)
    except ToolExecutionError:
        return None


def _github_issue_number_from_url(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def _preview_text(value: str, *, max_chars: int) -> str:
    stripped = value.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 3].rstrip() + "..."


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        label = str(value).strip()
        lowered = label.lower()
        if not label or lowered in seen:
            continue
        seen.add(lowered)
        result.append(label)
    return result


def _repo_from(connection: ToolConnection | None, payload: dict[str, Any]) -> str:
    repo = str(payload.get("repo") or "").strip()
    if not repo and connection is not None:
        repo = str((connection.config or {}).get("repo") or "").strip()
    if not repo:
        raise ToolExecutionError("GitHub tool requires a repo, e.g. Caliperti1/Maestro.")
    if "/" not in repo:
        raise ToolExecutionError("GitHub repo must be in owner/name form.")
    return repo


def _repo_owner(connection: ToolConnection | None, payload: dict[str, Any]) -> str:
    owner = str(payload.get("owner") or "").strip()
    if owner:
        return owner
    repo = str(payload.get("repo") or "").strip()
    if not repo and connection is not None:
        config = connection.config or {}
        owner = str(config.get("owner") or "").strip()
        if owner:
            return owner
        repo = str(config.get("repo") or "").strip()
    if repo and "/" in repo:
        return repo.split("/", 1)[0]
    raise ToolExecutionError("GitHub tool requires an owner or repo in owner/name form.")


def _clean_github_search_query(query: str, *, kind: str) -> str:
    remove_tokens = {
        "repo:AUTHORIZED_REPOSITORY",
        "repo:CURRENT",
        "repo:CURRENT_REPOSITORY",
        "repo:AUTHORIZED_REPO",
        "repo:CONFIGURED_REPOSITORY",
        "repo:CONFIGURED_REPO",
    }
    if kind == "pr":
        remove_tokens.update({"is:pr", "type:pr"})
    if kind == "issue":
        remove_tokens.update({"is:issue", "type:issue"})
    tokens = [token for token in query.split() if token not in remove_tokens]
    return " ".join(tokens).strip()


def _provider_key(tool_key: str) -> str:
    if tool_key.startswith("github."):
        return "github"
    if tool_key.startswith("codex."):
        return "codex"
    return tool_key


def _github_env(connection: ToolConnection | None) -> dict[str, str]:
    env = dict(os.environ)
    if connection is None:
        return env
    config = connection.config or {}
    token_env_name = str(config.get("env_token_name") or "").strip()
    if token_env_name:
        token = os.environ.get(token_env_name) or _dotenv_value(token_env_name)
        if not token:
            raise ToolExecutionError(f"GitHub token env var is not set: {token_env_name}")
        env["GH_TOKEN"] = token
    token = str(config.get("token") or "").strip()
    if token:
        env["GH_TOKEN"] = token
    return env


def _dotenv_value(key: str) -> str | None:
    env_path = get_settings().model_config.get("env_file", ".env")
    if isinstance(env_path, (list, tuple)):
        paths = [str(path) for path in env_path]
    else:
        paths = [str(env_path)]
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    raw_key, raw_value = stripped.split("=", 1)
                    if raw_key.strip() != key:
                        continue
                    value = raw_value.strip().strip('"').strip("'")
                    return value or None
        except FileNotFoundError:
            continue
    return None


def _run_gh_json(
    args: list[str],
    *,
    env: dict[str, str],
    allowed_exit_codes: set[int] | None = None,
) -> Any:
    output = _run_gh_text(args, env=env, allowed_exit_codes=allowed_exit_codes)
    if not output.strip():
        return None
    return json.loads(output)


def _run_gh_text(
    args: list[str],
    *,
    env: dict[str, str],
    allowed_exit_codes: set[int] | None = None,
) -> str:
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    allowed = allowed_exit_codes or {0}
    if completed.returncode not in allowed:
        raise ToolExecutionError(
            (completed.stderr or completed.stdout or "GitHub CLI failed.").strip()
        )
    return completed.stdout


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _codex_session_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        for key in ("thread_id", "session_id"):
            value = event.get(key)
            if value:
                return str(value)
        item = event.get("item")
        if isinstance(item, dict):
            value = item.get("thread_id") or item.get("session_id")
            if value:
                return str(value)
    return None


def _last_agent_message(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        item = event.get("item")
        if isinstance(item, dict):
            text = item.get("text") or item.get("message")
            if item.get("type") == "agent_message" and text:
                return str(text).strip()
        text = event.get("text") or event.get("message")
        if event.get("type") in {"agent_message", "item.completed"} and text:
            return str(text).strip()
    return ""


def _codex_changed_files(events: list[dict[str, Any]]) -> list[str]:
    files: set[str] = set()
    for event in events:
        item = event.get("item")
        candidates = []
        if isinstance(item, dict):
            candidates.extend([item.get("path"), item.get("file"), item.get("filename")])
            changes = item.get("changes") or item.get("files")
            if isinstance(changes, list):
                candidates.extend(
                    change.get("path") if isinstance(change, dict) else change
                    for change in changes
                )
        candidates.extend([event.get("path"), event.get("file"), event.get("filename")])
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                files.add(candidate.strip())
    return sorted(files)


def _codex_event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if any(token in lowered for token in ("secret", "token", "api_key", "apikey", "password")):
            redacted[key] = "********" if value else ""
        else:
            redacted[key] = value
    return redacted


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ToolExecutionError(f"GitHub tool requires `{key}`.")
    return value


def _required_any_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    choices = "`, `".join(keys)
    raise ToolExecutionError(f"Tool payload requires one of `{choices}`.")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
