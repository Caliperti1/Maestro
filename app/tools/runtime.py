import base64
import json
import os
import shutil
import subprocess
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
            },
            status="approval_required",
            started_at=datetime.now(UTC),
        )
        self.session.add(tool_call)
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
        try:
            output = adapter.execute(context, payload)
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
            return self._issue_create(repo, payload, env=env)
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
        return {
            "repo": repo,
            "result": _run_gh_json(
                [
                    "repo",
                    "view",
                    repo,
                    "--json",
                    "nameWithOwner,description,defaultBranchRef,url,isPrivate",
                ],
                env=env,
            ),
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
        return {
            "owner": owner,
            "limit": limit,
            "visibility": visibility,
            "repos": _run_gh_json(args, env=env),
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
        return {"repo": full_name, "url": url, "private": private, "description": description}

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
        return {
            "repo": repo,
            "query": query,
            "state": state,
            "limit": limit,
            "issues": _run_gh_json(args, env=env),
        }

    def _issue_get(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        number = _bounded_int(payload.get("number"), default=0, minimum=1, maximum=1_000_000)
        return {
            "repo": repo,
            "number": number,
            "issue": _run_gh_json(
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
            ),
        }

    def _issue_create(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        title = _required_text(payload, "title")
        body = str(payload.get("body") or "")
        requested_labels = _string_list(payload.get("labels"))
        labels = self._existing_labels(repo, requested_labels, env=env)
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        for label in labels["existing"]:
            args.extend(["--label", label])
        for assignee in _string_list(payload.get("assignees")):
            args.extend(["--assignee", assignee])
        milestone = str(payload.get("milestone") or "").strip()
        if milestone:
            args.extend(["--milestone", milestone])
        url = _run_gh_text(args, env=env).strip()
        return {
            "repo": repo,
            "url": url,
            "title": title,
            "labels": labels["existing"],
            "skipped_labels": labels["missing"],
        }

    def _existing_labels(
        self,
        repo: str,
        requested_labels: list[str],
        *,
        env: dict[str, str],
    ) -> dict[str, list[str]]:
        if not requested_labels:
            return {"existing": [], "missing": []}
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
        missing: list[str] = []
        for label in requested_labels:
            if label.lower() in existing_names:
                existing.append(label)
            else:
                missing.append(label)
        return {"existing": existing, "missing": missing}

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
        return {"repo": repo, "number": number, "commented": True}

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
        return {"repo": repo, "number": number, "updated": True}

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
        return {
            "repo": repo,
            "query": query,
            "state": state,
            "limit": limit,
            "prs": _run_gh_json(args, env=env),
        }

    def _pr_get(self, repo: str, payload: dict[str, Any], *, env: dict[str, str]) -> dict[str, Any]:
        number = _bounded_int(payload.get("number"), default=0, minimum=1, maximum=1_000_000)
        return {
            "repo": repo,
            "number": number,
            "pr": _run_gh_json(
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
            ),
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
            "truncated": len(diff) > max_chars,
            "diff": diff[:max_chars],
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
        return {"repo": repo, "number": number, "checks": checks}


def default_tool_adapters() -> dict[str, ToolAdapter]:
    return {
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


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
