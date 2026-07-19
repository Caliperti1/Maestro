"""Shared tool execution runtime and built-in tool adapters.

Agents never call external systems directly. They propose or execute tool calls through this
service, which resolves domain credentials, enforces agent permissions, records approval state, and
normalizes tool results for orchestration and memory artifacts. The file is still large because it
contains several tool families; future cleanup should split adapters by provider.
"""

import base64
import html
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Agent,
    Domain,
    Report,
    RoutedItem,
    Task,
    ToolCall,
    ToolConnection,
    WorkflowNotification,
    WorkflowRun,
)
from app.db.repositories import AgentRepository, DomainRepository
from app.llm.client import OpenAILLMClient
from app.maestro.channel import record_channel_message
from app.maestro.workflow_outputs import WorkflowOutputService
from app.memory.retrieval import (
    MemoryContextBundle,
    MemoryContextBundleRequest,
    MemoryContextSection,
    MemoryContextSnippet,
    MemoryRetrievalError,
    MemoryRetrievalService,
)
from app.memory.routed_service import RoutedMemoryService


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
            is_recoverable_runtime_block = (
                tool_call.tool_name == "local.app.deploy_pr"
                and "Runtime checkout has uncommitted changes" in str(exc)
            )
            tool_call.status = "approval_required" if is_recoverable_runtime_block else "failed"
            if is_recoverable_runtime_block:
                tool_call.output_payload = {
                    **(tool_call.output_payload or {}),
                    "approval_required": True,
                    "write_status": "recovery_required",
                    "recovery_required": True,
                    "recovery_instruction": (
                        "Inspect the runtime, approve a stash recovery if appropriate, then retry this delivery approval."
                    ),
                }
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
        tool_call.error_message = reason or f"Rejected by {get_settings().user_display_name}."
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
            if tool_key.startswith("gmail."):
                legacy_gmail = self.session.scalar(
                    select(ToolConnection).where(
                        ToolConnection.domain_id == domain.id,
                        ToolConnection.tool_key == "gmail",
                        ToolConnection.is_active.is_(True),
                    )
                )
                if legacy_gmail is not None:
                    return legacy_gmail
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
        if self.key == "github.read":
            return self._aggregate_read(repo, payload, env=env)
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
        if self.key == "github.pr.merge":
            return self._pr_merge(repo, payload, env=env)
        raise ToolExecutionError(f"Unsupported GitHub tool: {self.key}")

    def _aggregate_read(self, repo: str, payload: dict[str, Any], *, env: dict[str, str]) -> dict[str, Any]:
        request_text = str(
            payload.get("request")
            or payload.get("purpose")
            or payload.get("query")
            or ""
        ).strip()
        search_terms = _string_list(payload.get("search_terms"))
        if not search_terms and request_text:
            search_terms = _github_read_search_terms(request_text)
        max_files = _bounded_int(payload.get("max_files"), default=12, minimum=1, maximum=30)
        include_file_tree = bool(payload.get("include_file_tree", False))
        repo_result = self._repo_get(repo, env=env)
        file_results = []
        remaining_files = max_files
        for term in search_terms[:8]:
            if remaining_files <= 0:
                break
            try:
                result = self._file_search(
                    repo,
                    {"query": term, "limit": min(remaining_files, 8)},
                    env=env,
                )
            except ToolExecutionError as exc:
                file_results.append({"query": term, "error": str(exc), "files": []})
                continue
            files = result.get("files") if isinstance(result, dict) else []
            remaining_files -= len(files or [])
            file_results.append(
                {
                    "query": term,
                    "total_count": result.get("total_count") if isinstance(result, dict) else None,
                    "files": files,
                }
            )
        tree = None
        if include_file_tree:
            try:
                tree = self._file_get(repo, {"path": "", "max_chars": 1000}, env=env)
            except ToolExecutionError as exc:
                tree = {"error": str(exc)}
        return {
            "repo": repo,
            "request": request_text,
            "search_terms": search_terms,
            "repo_metadata": repo_result.get("summary") or repo_result,
            "file_searches": file_results,
            "file_tree": tree,
            "summary": {
                "type": "github_read",
                "repo": repo,
                "search_count": len(file_results),
                "file_count": sum(len(item.get("files") or []) for item in file_results),
                "request": request_text[:300],
            },
        }

    def _repo_get(self, repo: str, *, env: dict[str, str]) -> dict[str, Any]:
        owner, name = _repo_parts(repo)
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
            "owner": owner,
            "name": name,
            "repo_name": name,
            "result": result,
            "summary": {
                "type": "github_repo",
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
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
        owner_name, repo_name = _repo_parts(full_name)
        return {
            "repo": full_name,
            "owner": owner_name,
            "name": repo_name,
            "repo_name": repo_name,
            "url": url,
            "repo_url": url,
            "private": private,
            "description": description,
            "write_status": "created",
            "summary": {
                "type": "github_repo",
                "repo": full_name,
                "owner": owner_name,
                "name": repo_name,
                "repo_name": repo_name,
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
        owner, name = _repo_parts(repo)
        if isinstance(result, list):
            return {
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
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
            "owner": owner,
            "repo_name": name,
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
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
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
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
            "query": query,
            "state": state,
            "limit": limit,
            "issues": issues,
            "summary": {
                "type": "github_issue_list",
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
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
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
            "number": number,
            "issue_number": number,
            "issue": issue,
            "issue_url": issue.get("url") if isinstance(issue, dict) else None,
            "html_url": issue.get("url") if isinstance(issue, dict) else None,
            "title": issue.get("title") if isinstance(issue, dict) else None,
            "state": issue.get("state") if isinstance(issue, dict) else None,
            "status": issue.get("state") if isinstance(issue, dict) else None,
            "summary": {
                "type": "github_issue",
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
                "issue_number": number,
                "issue_url": issue.get("url") if isinstance(issue, dict) else None,
                "html_url": issue.get("url") if isinstance(issue, dict) else None,
                "title": issue.get("title") if isinstance(issue, dict) else None,
                "state": issue.get("state") if isinstance(issue, dict) else None,
                "status": issue.get("state") if isinstance(issue, dict) else None,
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
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
            "issue_number": issue_number,
            "number": issue_number,
            "issue_url": url,
            "html_url": url,
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
                "owner": owner,
                "name": name,
                "repo_name": name,
                "issue_number": issue_number,
                "issue_url": url,
                "html_url": url,
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
        number = _required_int(payload, ("number", "issue_number"), label="GitHub issue number")
        body = _required_text(payload, "body")
        _run_gh_text(["issue", "comment", str(number), "--repo", repo, "--body", body], env=env)
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
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
        number = _required_int(payload, ("number", "issue_number"), label="GitHub issue number")
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
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
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
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
            "query": query,
            "state": state,
            "limit": limit,
            "prs": prs,
            "summary": {
                "type": "github_pr_list",
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
                "count": len(prs) if isinstance(prs, list) else 0,
                "state": state,
                "query": query,
            },
        }

    def _pr_get(self, repo: str, payload: dict[str, Any], *, env: dict[str, str]) -> dict[str, Any]:
        number = _required_int(payload, ("number", "pr_number"), label="GitHub PR number")
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
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
            "number": number,
            "pr_number": number,
            "pr": pr,
            "pr_url": pr.get("url") if isinstance(pr, dict) else None,
            "html_url": pr.get("url") if isinstance(pr, dict) else None,
            "title": pr.get("title") if isinstance(pr, dict) else None,
            "state": pr.get("state") if isinstance(pr, dict) else None,
            "status": pr.get("state") if isinstance(pr, dict) else None,
            "summary": {
                "type": "github_pr",
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
                "pr_number": number,
                "pr_url": pr.get("url") if isinstance(pr, dict) else None,
                "html_url": pr.get("url") if isinstance(pr, dict) else None,
                "title": pr.get("title") if isinstance(pr, dict) else None,
                "state": pr.get("state") if isinstance(pr, dict) else None,
                "status": pr.get("state") if isinstance(pr, dict) else None,
                "review_decision": pr.get("reviewDecision") if isinstance(pr, dict) else None,
            },
        }

    def _pr_diff(self, repo: str, payload: dict[str, Any], *, env: dict[str, str]) -> dict[str, Any]:
        number = _required_int(payload, ("number", "pr_number"), label="GitHub PR number")
        args = ["pr", "diff", str(number), "--repo", repo, "--color", "never"]
        if bool(payload.get("name_only")):
            args.append("--name-only")
        elif bool(payload.get("patch")):
            args.append("--patch")
        diff = _run_gh_text(args, env=env)
        max_chars = _bounded_int(payload.get("max_chars"), default=20000, minimum=1000, maximum=60000)
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
            "number": number,
            "pr_number": number,
            "truncated": len(diff) > max_chars,
            "diff": diff[:max_chars],
            "summary": {
                "type": "github_pr_diff",
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
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
        number = _required_int(payload, ("number", "pr_number"), label="GitHub PR number")
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
        owner, name = _repo_parts(repo)
        check_summary = _github_check_summary(checks)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
            "number": number,
            "pr_number": number,
            "checks": checks,
            "check_status": check_summary["check_status"],
            "status": check_summary["check_status"],
            "state": check_summary["check_status"],
            "check_counts": check_summary["check_counts"],
            "failed_checks": check_summary["failed_checks"],
            "pending_checks": check_summary["pending_checks"],
            "summary": {
                "type": "github_pr_checks",
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
                "pr_number": number,
                "count": len(checks) if isinstance(checks, list) else 0,
                "check_status": check_summary["check_status"],
                "status": check_summary["check_status"],
                "state": check_summary["check_status"],
                "check_counts": check_summary["check_counts"],
                "failed_checks": check_summary["failed_checks"],
                "pending_checks": check_summary["pending_checks"],
            },
        }

    def _pr_merge(
        self,
        repo: str,
        payload: dict[str, Any],
        *,
        env: dict[str, str],
    ) -> dict[str, Any]:
        number = _required_int(payload, ("number", "pr_number"), label="GitHub PR number")
        method = str(payload.get("method") or "squash").strip().lower()
        if method not in {"merge", "squash", "rebase"}:
            raise ToolExecutionError("GitHub PR merge method must be merge, squash, or rebase.")
        delete_branch = _as_bool(payload.get("delete_branch"), default=True)
        args = ["pr", "merge", str(number), "--repo", repo, f"--{method}"]
        if delete_branch:
            args.append("--delete-branch")
        subject = str(payload.get("subject") or payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if subject:
            args.extend(["--subject", subject])
        if body:
            args.extend(["--body", body])
        _run_gh_text(args, env=env, timeout=120)
        owner, name = _repo_parts(repo)
        return {
            "repo": repo,
            "owner": owner,
            "name": name,
            "repo_name": name,
            "number": number,
            "pr_number": number,
            "merged": True,
            "merge_method": method,
            "delete_branch": delete_branch,
            "write_status": "merged",
            "summary": {
                "type": "github_pr_merge",
                "repo": repo,
                "owner": owner,
                "name": name,
                "repo_name": name,
                "pr_number": number,
                "merged": True,
                "merge_method": method,
                "delete_branch": delete_branch,
            },
        }


class GmailApiToolAdapter:
    def __init__(self, key: str):
        self.key = key

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if context.dry_run:
            return {"dry_run": True, "tool": self.key, "payload": payload}
        token = _gmail_access_token(context.connection)
        user_id = _gmail_user_id(context.connection, payload)
        if self.key == "gmail.message.search":
            return self._message_search(context.connection, payload, token=token, user_id=user_id)
        if self.key == "gmail.message.list_recent":
            return self._message_list_recent(context.connection, payload, token=token, user_id=user_id)
        if self.key == "gmail.message.get":
            return self._message_get(payload, token=token, user_id=user_id)
        if self.key == "gmail.thread.get":
            return self._thread_get(payload, token=token, user_id=user_id)
        if self.key == "gmail.draft.create":
            return self._draft_create(context.connection, payload, token=token, user_id=user_id)
        if self.key == "gmail.message.modify":
            return self._message_modify(payload, token=token, user_id=user_id)
        raise ToolExecutionError(f"Unsupported Gmail tool: {self.key}")

    def _message_search(
        self,
        connection: ToolConnection | None,
        payload: dict[str, Any],
        *,
        token: str,
        user_id: str,
    ) -> dict[str, Any]:
        query = str(payload.get("query") or payload.get("q") or "").strip()
        default_query = str(_connection_config(connection).get("default_query") or "").strip()
        if not query:
            query = default_query
        limit = _bounded_int(payload.get("limit") or payload.get("max_results"), default=10, minimum=1, maximum=50)
        params: dict[str, Any] = {"maxResults": limit}
        if query:
            params["q"] = query
        label_ids = _string_list(payload.get("label_ids") or payload.get("labels"))
        if label_ids:
            params["labelIds"] = label_ids
        include_spam_trash = _as_bool(payload.get("include_spam_trash"), default=False)
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
        response = _gmail_api_json(
            "GET",
            f"/gmail/v1/users/{quote(user_id, safe='')}/messages",
            token=token,
            params=params,
        )
        messages = response.get("messages", []) if isinstance(response, dict) else []
        hydrated = [
            self._message_metadata(str(message.get("id")), token=token, user_id=user_id)
            for message in messages
            if isinstance(message, dict) and message.get("id")
        ]
        return {
            "query": query,
            "limit": limit,
            "messages": hydrated,
            "result_size_estimate": response.get("resultSizeEstimate") if isinstance(response, dict) else None,
            "summary": {
                "type": "gmail_message_list",
                "query": query,
                "count": len(hydrated),
                "user_id": user_id,
            },
        }

    def _message_list_recent(
        self,
        connection: ToolConnection | None,
        payload: dict[str, Any],
        *,
        token: str,
        user_id: str,
    ) -> dict[str, Any]:
        limit = _bounded_int(payload.get("limit") or payload.get("max_results"), default=10, minimum=1, maximum=50)
        unread_only = _as_bool(payload.get("unread_only"), default=False)
        newer_than_days = _bounded_int(payload.get("newer_than_days"), default=14, minimum=1, maximum=365)
        query_parts = [f"newer_than:{newer_than_days}d"]
        if unread_only:
            query_parts.append("is:unread")
        configured_query = str(_connection_config(connection).get("default_query") or "").strip()
        if configured_query:
            query_parts.append(configured_query)
        return self._message_search(
            connection,
            {**payload, "query": " ".join(query_parts), "limit": limit},
            token=token,
            user_id=user_id,
        )

    def _message_get(self, payload: dict[str, Any], *, token: str, user_id: str) -> dict[str, Any]:
        message_id = _required_any_text(payload, ("message_id", "id"))
        max_body_chars = _bounded_int(payload.get("max_body_chars"), default=12000, minimum=500, maximum=50000)
        message = _gmail_api_json(
            "GET",
            f"/gmail/v1/users/{quote(user_id, safe='')}/messages/{quote(message_id, safe='')}",
            token=token,
            params={"format": "full"},
        )
        parsed = _gmail_message_payload(message, max_body_chars=max_body_chars)
        return {
            **parsed,
            "message": message,
            "summary": {
                "type": "gmail_message",
                "message_id": parsed["message_id"],
                "thread_id": parsed.get("thread_id"),
                "subject": parsed.get("subject"),
                "from": parsed.get("from"),
                "date": parsed.get("date"),
                "body_truncated": parsed.get("body_truncated"),
            },
        }

    def _thread_get(self, payload: dict[str, Any], *, token: str, user_id: str) -> dict[str, Any]:
        thread_id = _required_any_text(payload, ("thread_id", "id"))
        max_body_chars = _bounded_int(payload.get("max_body_chars"), default=8000, minimum=500, maximum=30000)
        thread = _gmail_api_json(
            "GET",
            f"/gmail/v1/users/{quote(user_id, safe='')}/threads/{quote(thread_id, safe='')}",
            token=token,
            params={"format": "full"},
        )
        raw_messages = thread.get("messages", []) if isinstance(thread, dict) else []
        messages = [
            _gmail_message_payload(message, max_body_chars=max_body_chars)
            for message in raw_messages
            if isinstance(message, dict)
        ]
        return {
            "thread_id": thread_id,
            "messages": messages,
            "message_count": len(messages),
            "thread": thread,
            "summary": {
                "type": "gmail_thread",
                "thread_id": thread_id,
                "message_count": len(messages),
                "subjects": _dedupe_strings([message.get("subject") for message in messages]),
            },
        }

    def _draft_create(
        self,
        connection: ToolConnection | None,
        payload: dict[str, Any],
        *,
        token: str,
        user_id: str,
    ) -> dict[str, Any]:
        to = _string_list(payload.get("to") or payload.get("recipients"))
        if not to:
            raise ToolExecutionError("Gmail draft creation requires at least one recipient.")
        subject = _required_text(payload, "subject")
        body = _required_any_text(payload, ("body", "body_text", "content"))
        cc = _string_list(payload.get("cc"))
        bcc = _string_list(payload.get("bcc"))
        reply_to_message_id = str(payload.get("reply_to_message_id") or "").strip()
        thread_id = str(payload.get("thread_id") or "").strip()
        from_address = str(payload.get("from") or _connection_config(connection).get("send_as") or "").strip()
        raw = _gmail_rfc822_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            from_address=from_address,
            reply_to_message_id=reply_to_message_id,
        )
        request_body: dict[str, Any] = {"message": {"raw": _base64url(raw.encode("utf-8"))}}
        if thread_id:
            request_body["message"]["threadId"] = thread_id
        response = _gmail_api_json(
            "POST",
            f"/gmail/v1/users/{quote(user_id, safe='')}/drafts",
            token=token,
            body=request_body,
        )
        draft_id = response.get("id") if isinstance(response, dict) else None
        message = response.get("message") if isinstance(response, dict) else {}
        return {
            "draft_id": draft_id,
            "message_id": message.get("id") if isinstance(message, dict) else None,
            "thread_id": message.get("threadId") if isinstance(message, dict) else thread_id or None,
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "body_preview": _preview_text(body, max_chars=700),
            "write_status": "draft_created",
            "summary": {
                "type": "gmail_draft",
                "draft_id": draft_id,
                "message_id": message.get("id") if isinstance(message, dict) else None,
                "thread_id": message.get("threadId") if isinstance(message, dict) else thread_id or None,
                "to": to,
                "subject": subject,
                "write_status": "draft_created",
            },
        }

    def _message_modify(self, payload: dict[str, Any], *, token: str, user_id: str) -> dict[str, Any]:
        message_id = _required_any_text(payload, ("message_id", "id"))
        add_labels = _string_list(payload.get("add_label_ids") or payload.get("add_labels"))
        remove_labels = _string_list(payload.get("remove_label_ids") or payload.get("remove_labels"))
        if not add_labels and not remove_labels:
            raise ToolExecutionError("Gmail message modify requires labels to add or remove.")
        response = _gmail_api_json(
            "POST",
            f"/gmail/v1/users/{quote(user_id, safe='')}/messages/{quote(message_id, safe='')}/modify",
            token=token,
            body={"addLabelIds": add_labels, "removeLabelIds": remove_labels},
        )
        return {
            "message_id": message_id,
            "thread_id": response.get("threadId") if isinstance(response, dict) else None,
            "label_ids": response.get("labelIds") if isinstance(response, dict) else [],
            "add_label_ids": add_labels,
            "remove_label_ids": remove_labels,
            "write_status": "modified",
            "summary": {
                "type": "gmail_message_modify",
                "message_id": message_id,
                "add_label_ids": add_labels,
                "remove_label_ids": remove_labels,
                "write_status": "modified",
            },
        }

    def _message_metadata(self, message_id: str, *, token: str, user_id: str) -> dict[str, Any]:
        message = _gmail_api_json(
            "GET",
            f"/gmail/v1/users/{quote(user_id, safe='')}/messages/{quote(message_id, safe='')}",
            token=token,
            params={"format": "metadata", "metadataHeaders": ["Subject", "From", "To", "Date"]},
        )
        return _gmail_message_payload(message, max_body_chars=0)


class GoogleWorkspaceToolAdapter:
    def __init__(self, key: str):
        self.key = key

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if context.dry_run:
            return {"dry_run": True, "tool": self.key, "payload": payload}
        token = _gmail_access_token(context.connection)
        if self.key == "google.drive.file.get":
            return self._drive_file_get(payload, token=token)
        if self.key == "google.drive.file.export":
            return self._drive_file_export(payload, token=token)
        if self.key == "google.docs.get":
            return self._docs_get(payload, token=token)
        if self.key == "google.slides.get":
            return self._slides_get(payload, token=token)
        if self.key == "google.sheets.get":
            return self._sheets_get(payload, token=token)
        if self.key == "google.sheets.values.get":
            return self._sheets_values_get(payload, token=token)
        if self.key == "google.meet.conference_records.list":
            return self._meet_conference_records_list(payload, token=token)
        if self.key == "google.meet.conference_records.get":
            return self._meet_conference_record_get(payload, token=token)
        raise ToolExecutionError(f"Unsupported Google Workspace tool: {self.key}")

    def _drive_file_get(self, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        file_id = _google_file_id(payload)
        fields = str(
            payload.get("fields")
            or "id,name,mimeType,webViewLink,webContentLink,modifiedTime,createdTime,owners(emailAddress,displayName),description,size"
        )
        file_metadata = _google_api_json(
            "GET",
            "https://www.googleapis.com",
            f"/drive/v3/files/{quote(file_id, safe='')}",
            token=token,
            params={"fields": fields},
        )
        return {
            "file": file_metadata,
            "summary": {
                "type": "google_drive_file",
                "file_id": file_metadata.get("id") or file_id,
                "name": file_metadata.get("name"),
                "mime_type": file_metadata.get("mimeType"),
                "web_view_link": file_metadata.get("webViewLink"),
            },
        }

    def _drive_file_export(self, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        file_id = _google_file_id(payload)
        mime_type = str(payload.get("mime_type") or "text/plain").strip()
        max_chars = _bounded_int(payload.get("max_chars"), default=20000, minimum=500, maximum=100000)
        content = _google_api_text(
            "GET",
            "https://www.googleapis.com",
            f"/drive/v3/files/{quote(file_id, safe='')}/export",
            token=token,
            params={"mimeType": mime_type},
        )
        return {
            "file_id": file_id,
            "mime_type": mime_type,
            "content_text": content[:max_chars],
            "truncated": len(content) > max_chars,
            "summary": {
                "type": "google_drive_file_export",
                "file_id": file_id,
                "mime_type": mime_type,
                "content_chars": min(len(content), max_chars),
                "truncated": len(content) > max_chars,
            },
        }

    def _docs_get(self, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        document_id = _google_file_id(payload)
        max_chars = _bounded_int(payload.get("max_chars"), default=20000, minimum=500, maximum=100000)
        document = _google_api_json(
            "GET",
            "https://docs.googleapis.com",
            f"/v1/documents/{quote(document_id, safe='')}",
            token=token,
        )
        content = _google_doc_text(document)
        return {
            "document_id": document_id,
            "title": document.get("title"),
            "content_text": content[:max_chars],
            "truncated": len(content) > max_chars,
            "document": document if _as_bool(payload.get("include_raw"), default=False) else None,
            "summary": {
                "type": "google_doc",
                "document_id": document_id,
                "title": document.get("title"),
                "content_chars": min(len(content), max_chars),
                "truncated": len(content) > max_chars,
            },
        }

    def _slides_get(self, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        presentation_id = _google_file_id(payload)
        max_chars = _bounded_int(payload.get("max_chars"), default=20000, minimum=500, maximum=100000)
        presentation = _google_api_json(
            "GET",
            "https://slides.googleapis.com",
            f"/v1/presentations/{quote(presentation_id, safe='')}",
            token=token,
        )
        content = _google_slides_text(presentation)
        return {
            "presentation_id": presentation_id,
            "title": presentation.get("title"),
            "content_text": content[:max_chars],
            "truncated": len(content) > max_chars,
            "presentation": presentation if _as_bool(payload.get("include_raw"), default=False) else None,
            "summary": {
                "type": "google_slides",
                "presentation_id": presentation_id,
                "title": presentation.get("title"),
                "slide_count": len(presentation.get("slides") or []),
                "content_chars": min(len(content), max_chars),
                "truncated": len(content) > max_chars,
            },
        }

    def _sheets_get(self, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        spreadsheet_id = _google_file_id(payload)
        include_grid_data = _as_bool(payload.get("include_grid_data"), default=False)
        spreadsheet = _google_api_json(
            "GET",
            "https://sheets.googleapis.com",
            f"/v4/spreadsheets/{quote(spreadsheet_id, safe='')}",
            token=token,
            params={"includeGridData": str(include_grid_data).lower()},
        )
        sheets = spreadsheet.get("sheets") if isinstance(spreadsheet.get("sheets"), list) else []
        return {
            "spreadsheet_id": spreadsheet_id,
            "title": spreadsheet.get("properties", {}).get("title")
            if isinstance(spreadsheet.get("properties"), dict)
            else None,
            "sheets": [
                sheet.get("properties", {})
                for sheet in sheets
                if isinstance(sheet, dict) and isinstance(sheet.get("properties"), dict)
            ],
            "spreadsheet": spreadsheet if _as_bool(payload.get("include_raw"), default=False) else None,
            "summary": {
                "type": "google_sheets",
                "spreadsheet_id": spreadsheet_id,
                "title": spreadsheet.get("properties", {}).get("title")
                if isinstance(spreadsheet.get("properties"), dict)
                else None,
                "sheet_count": len(sheets),
            },
        }

    def _sheets_values_get(self, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        spreadsheet_id = _google_file_id(payload)
        range_name = str(payload.get("range") or payload.get("range_name") or "").strip()
        if not range_name:
            raise ToolExecutionError("Google Sheets values tool requires range or range_name.")
        values = _google_api_json(
            "GET",
            "https://sheets.googleapis.com",
            f"/v4/spreadsheets/{quote(spreadsheet_id, safe='')}/values/{quote(range_name, safe='')}",
            token=token,
        )
        rows = values.get("values") if isinstance(values.get("values"), list) else []
        return {
            "spreadsheet_id": spreadsheet_id,
            "range": values.get("range") or range_name,
            "major_dimension": values.get("majorDimension"),
            "values": rows,
            "summary": {
                "type": "google_sheets_values",
                "spreadsheet_id": spreadsheet_id,
                "range": values.get("range") or range_name,
                "row_count": len(rows),
            },
        }

    def _meet_conference_records_list(self, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        page_size = _bounded_int(payload.get("page_size"), default=10, minimum=1, maximum=100)
        params: dict[str, Any] = {"pageSize": page_size}
        if payload.get("page_token"):
            params["pageToken"] = str(payload["page_token"])
        if payload.get("filter"):
            params["filter"] = str(payload["filter"])
        records = _google_api_json(
            "GET",
            "https://meet.googleapis.com",
            "/v2/conferenceRecords",
            token=token,
            params=params,
        )
        conference_records = (
            records.get("conferenceRecords")
            if isinstance(records.get("conferenceRecords"), list)
            else []
        )
        return {
            "conference_records": conference_records,
            "next_page_token": records.get("nextPageToken"),
            "summary": {
                "type": "google_meet_conference_records",
                "record_count": len(conference_records),
                "has_next_page": bool(records.get("nextPageToken")),
            },
        }

    def _meet_conference_record_get(self, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        record_name = str(
            payload.get("name") or payload.get("conference_record") or payload.get("conference_record_id") or ""
        ).strip()
        if not record_name:
            raise ToolExecutionError(
                "Google Meet conference record get requires name, conference_record, or conference_record_id."
            )
        if not record_name.startswith("conferenceRecords/"):
            record_name = f"conferenceRecords/{record_name}"
        record = _google_api_json(
            "GET",
            "https://meet.googleapis.com",
            f"/v2/{quote(record_name, safe='/')}",
            token=token,
        )
        return {
            "conference_record": record,
            "summary": {
                "type": "google_meet_conference_record",
                "name": record.get("name") or record_name,
                "start_time": record.get("startTime"),
                "end_time": record.get("endTime"),
            },
        }


class RoutedItemCreateToolAdapter:
    key = "routed.item.create"

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if context.dry_run:
            return {"dry_run": True, "tool": self.key, "payload": payload}
        raw_items = payload.get("items")
        items = raw_items if isinstance(raw_items, list) else [payload]
        created: list[RoutedItem] = []
        for raw_item in items[:20]:
            if not isinstance(raw_item, dict):
                continue
            route_type = _normalize_route_type(str(raw_item.get("route_type") or raw_item.get("type") or ""))
            if not route_type:
                raise ToolExecutionError("routed.item.create requires route_type.")
            title = str(raw_item.get("title") or raw_item.get("name") or "").strip()
            content = str(raw_item.get("content") or raw_item.get("description") or raw_item.get("summary") or "").strip()
            if not title:
                raise ToolExecutionError("routed.item.create requires title.")
            if not content:
                content = title
            metadata = raw_item.get("metadata") if isinstance(raw_item.get("metadata"), dict) else {}
            source_refs = _source_refs_from_payload(raw_item)
            item = RoutedItem(
                domain_id=context.domain.id,
                agent_id=context.agent.id,
                task_id=context.task.id,
                route_type=route_type,
                title=title[:240],
                content=content,
                priority=str(raw_item.get("priority") or "normal").strip() or "normal",
                status=str(raw_item.get("status") or "open").strip() or "open",
                source_refs=source_refs,
                metadata_={
                    **metadata,
                    "created_by_tool": self.key,
                    "agent_key": context.agent.key,
                    "domain_key": context.domain.key,
                },
            )
            context.session.add(item)
            context.session.flush()
            created.append(item)
        promoted = RoutedMemoryService(context.session).promote_items(created) if created else []
        context.session.commit()
        return {
            "created_count": len(created),
            "promoted_count": len(promoted),
            "items": [
                {
                    "id": str(item.id),
                    "route_type": item.route_type,
                    "title": item.title,
                    "status": item.status,
                    "metadata": item.metadata_,
                }
                for item in created
            ],
            "promotions": [
                {
                    "routed_item_id": str(result.routed_item_id),
                    "route_type": result.route_type,
                    "object_type": result.object_type,
                    "object_id": str(result.object_id),
                    "action": result.action,
                }
                for result in promoted
            ],
            "summary": {
                "type": "routed_item_create",
                "created_count": len(created),
                "promoted_count": len(promoted),
                "route_types": sorted({item.route_type for item in created}),
            },
        }


class WorkflowNotificationCreateToolAdapter:
    key = "workflow.notification.create"

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if context.dry_run:
            return {"dry_run": True, "tool": self.key, "payload": payload}
        title = str(payload.get("title") or "Email needs your attention").strip()
        message = str(payload.get("message") or payload.get("summary") or "").strip()
        if not message:
            raise ToolExecutionError("workflow.notification.create requires message or summary.")
        severity = str(payload.get("severity") or "warning").strip().lower()
        if severity not in {"info", "warning", "urgent"}:
            raise ToolExecutionError("Notification severity must be info, warning, or urgent.")
        source_key = str(payload.get("message_id") or payload.get("thread_id") or "").strip()
        run = self._workflow_run(context)
        existing = self._existing_notification(
            context,
            run=run,
            title=title,
            message=message,
            source_key=source_key,
        )
        if existing is not None:
            return self._result(existing, duplicate=True)

        metadata = {
            "source": self.key,
            "agent_key": context.agent.key,
            "domain_key": context.domain.key,
            "task_id": str(context.task.id),
            "source_message_id": str(payload.get("message_id") or "").strip() or None,
            "source_thread_id": str(payload.get("thread_id") or "").strip() or None,
            "subject": str(payload.get("subject") or "").strip() or None,
            "sender": str(payload.get("from") or payload.get("sender") or "").strip() or None,
            "reason": str(payload.get("reason") or "").strip() or None,
        }
        notification = WorkflowOutputService(context.session).create_notification(
            run,
            title=title,
            message=message,
            severity=severity,
            notification_type="email_attention",
            status="delivered",
            delivered_at=datetime.now(UTC),
            metadata=metadata,
        )
        if run is None:
            notification.conversation_id = context.task.conversation_id
            notification.domain_id = context.domain.id
            context.session.commit()
            context.session.refresh(notification)
        record_channel_message(
            context.session,
            sender="maestro",
            content=f"{notification.title}\n\n{notification.message}",
            metadata={
                "source": self.key,
                "event_type": "email_attention",
                "notification_id": str(notification.id),
                "workflow_run_id": str(run.id) if run is not None else None,
                "task_id": str(context.task.id),
            },
        )
        return self._result(notification, duplicate=False)

    def _workflow_run(self, context: ToolExecutionContext) -> WorkflowRun | None:
        parent_task_ids = [context.task.id]
        if context.task.parent_task_id is not None:
            parent_task_ids.insert(0, context.task.parent_task_id)
        return context.session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.parent_task_id.in_(parent_task_ids))
            .order_by(WorkflowRun.created_at.desc())
        )

    def _existing_notification(
        self,
        context: ToolExecutionContext,
        *,
        run: WorkflowRun | None,
        title: str,
        message: str,
        source_key: str,
    ) -> WorkflowNotification | None:
        query = select(WorkflowNotification).where(
            WorkflowNotification.notification_type == "email_attention"
        )
        if run is not None:
            query = query.where(WorkflowNotification.workflow_run_id == run.id)
        else:
            query = query.where(WorkflowNotification.domain_id == context.domain.id)
        for notification in context.session.scalars(
            query.order_by(WorkflowNotification.created_at.desc()).limit(20)
        ).all():
            metadata = notification.metadata_ or {}
            if source_key and source_key in {
                str(metadata.get("source_message_id") or ""),
                str(metadata.get("source_thread_id") or ""),
            }:
                return notification
            compact_title = " ".join(title.split())[:240]
            compact_message = " ".join(message.split())[:240]
            if notification.title == compact_title and notification.message == compact_message:
                return notification
        return None

    def _result(
        self,
        notification: WorkflowNotification,
        *,
        duplicate: bool,
    ) -> dict[str, Any]:
        return {
            "notification_id": str(notification.id),
            "duplicate": duplicate,
            "title": notification.title,
            "message": notification.message,
            "severity": notification.severity,
            "status": notification.status,
            "summary": {
                "type": "workflow_notification",
                "notification_id": str(notification.id),
                "notification_type": notification.notification_type,
                "duplicate": duplicate,
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
        branch_workflow = _bool_setting(payload, context.connection, "branch_workflow", default=True)
        if branch_workflow:
            full_prompt = (
                f"{full_prompt}\n\n"
                "Maestro branch workflow guardrails:\n"
                "- You are already running inside an isolated Maestro-managed worktree.\n"
                "- Edit files and run validation only.\n"
                "- Do not create branches, commit, push, open pull requests, merge, deploy, hot reload, "
                "or post GitHub comments; Maestro performs those steps after your run.\n"
                "- Return a concise final report with changed files, validation results, and follow-ups."
            )

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as output_file:
            output_path = output_file.name
        try:
            git_context = self._prepare_branch_workflow(context, payload, target_path) if branch_workflow else None
            execution_path = (
                Path(str(git_context["worktree_path"]))
                if git_context is not None
                else target_path
            )
            args = [
                codex_bin,
                "exec",
                "--json",
                "--cd",
                str(execution_path),
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
                cwd=str(execution_path),
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
                "target_path": str(execution_path),
                "source_repo_path": str(target_path),
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
            if git_context is not None:
                result.update(
                    self._complete_branch_workflow(
                        context,
                        payload,
                        execution_path,
                        git_context,
                        final_message=final_message,
                        changed_files=result["changed_files"],
                    )
                )
            return result
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(f"Codex task timed out after {timeout_seconds} seconds.") from exc
        finally:
            if "git_context" in locals() and git_context is not None:
                self._cleanup_worktree(target_path, git_context)
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

    def _prepare_branch_workflow(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
        target_path: Path,
    ) -> dict[str, Any]:
        if not (target_path / ".git").exists():
            raise ToolExecutionError("Codex branch workflow requires a Git repository target.")
        allow_dirty = _bool_setting(payload, context.connection, "allow_dirty", default=False)
        status = _run_local_text(["git", "status", "--porcelain"], cwd=target_path).strip()
        if status and not allow_dirty:
            raise ToolExecutionError(
                "Codex branch workflow requires a clean working tree. Commit, stash, or set allow_dirty intentionally."
            )
        original_branch = _run_local_text(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=target_path,
        ).strip()
        if not original_branch or original_branch == "HEAD":
            raise ToolExecutionError("Codex branch workflow requires a named starting branch.")
        base_branch = str(
            payload.get("base_branch")
            or _connection_config(context.connection).get("base_branch")
            or _connection_config(context.connection).get("default_base_branch")
            or "main"
        ).strip()
        branch_name = str(payload.get("branch_name") or "").strip()
        if not branch_name:
            branch_name = self._generated_branch_name(context, payload)
        remote_base = f"origin/{base_branch}"
        try:
            _run_local_text(["git", "fetch", "origin", base_branch], cwd=target_path, timeout=120)
            base_ref = remote_base
        except ToolExecutionError:
            base_ref = base_branch
        worktree_path = self._worktree_path(context, payload, target_path, branch_name)
        if worktree_path.exists():
            _run_local_text(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=target_path)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        _run_local_text(
            ["git", "worktree", "add", "-B", branch_name, str(worktree_path), base_ref],
            cwd=target_path,
            timeout=120,
        )
        return {
            "original_branch": original_branch,
            "base_branch": base_branch,
            "base_ref": base_ref,
            "branch_name": branch_name,
            "worktree_path": str(worktree_path),
            "allow_dirty": allow_dirty,
        }

    def _complete_branch_workflow(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
        target_path: Path,
        git_context: dict[str, Any],
        *,
        final_message: str,
        changed_files: list[str],
    ) -> dict[str, Any]:
        branch_name = str(git_context["branch_name"])
        base_branch = str(git_context["base_branch"])
        status = _run_local_text(["git", "status", "--porcelain"], cwd=target_path).strip()
        changed_paths = _git_changed_paths(target_path)
        commit_sha = None
        if status:
            _run_local_text(["git", "add", "-A"], cwd=target_path)
            commit_message = _codex_commit_message(payload)
            _run_local_text(["git", "commit", "-m", commit_message], cwd=target_path, timeout=120)
            commit_sha = _run_local_text(["git", "rev-parse", "HEAD"], cwd=target_path).strip()
        create_pr = _bool_setting(payload, context.connection, "create_pr", default=True)
        push_branch = _bool_setting(payload, context.connection, "push_branch", default=create_pr)
        pr_payload: dict[str, Any] | None = None
        if push_branch and commit_sha:
            _run_local_text(["git", "push", "-u", "origin", branch_name], cwd=target_path, timeout=180)
        if create_pr and commit_sha:
            pr_payload = self._create_or_get_pr(
                context,
                payload,
                target_path,
                branch_name=branch_name,
                base_branch=base_branch,
                final_message=final_message,
                changed_paths=changed_paths,
            )
        diff_summary = _run_local_text(
            ["git", "diff", "--stat", f"{base_branch}...{branch_name}"],
            cwd=target_path,
        ).strip()
        return {
            "branch_workflow": True,
            "branch": branch_name,
            "base_branch": base_branch,
            "commit_sha": commit_sha,
            "changed_files": changed_paths or changed_files,
            "diff_summary": diff_summary,
            "pr": pr_payload,
            "pr_url": pr_payload.get("url") if pr_payload else None,
            "pr_number": pr_payload.get("number") if pr_payload else None,
            "review_status": "pr_opened" if pr_payload else ("no_changes" if not commit_sha else "branch_pushed"),
        }

    def _create_or_get_pr(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
        target_path: Path,
        *,
        branch_name: str,
        base_branch: str,
        final_message: str,
        changed_paths: list[str],
    ) -> dict[str, Any]:
        github_connection = self._github_connection(context)
        repo = _optional_repo_from(github_connection, payload) or _optional_repo_from(context.connection, payload)
        title = str(payload.get("pr_title") or payload.get("task_title") or "").strip()
        if not title:
            title = _single_line_preview(str(payload.get("task") or payload.get("prompt") or "Maestro coding task"), max_chars=90)
        body = str(payload.get("pr_body") or "").strip()
        if not body:
            body = _codex_pr_body(
                title=title,
                final_message=final_message,
                changed_paths=changed_paths,
                issue_number=str(payload.get("issue_number") or payload.get("number") or "").strip(),
            )
        args = ["pr", "create", "--base", base_branch, "--head", branch_name, "--title", title, "--body", body]
        if repo:
            args.extend(["--repo", repo])
        env = _github_env(github_connection or context.connection)
        try:
            url = _run_gh_text(args, env=env).strip()
        except ToolExecutionError as exc:
            if "already exists" not in str(exc).lower():
                raise
            view_args = ["pr", "view", branch_name, "--json", "number,title,body,url,headRefName,baseRefName"]
            if repo:
                view_args.extend(["--repo", repo])
            existing = _run_gh_json(view_args, env=env)
            return _normalized_pr_payload(repo, existing, body)
        view_args = ["pr", "view", url, "--json", "number,title,body,url,headRefName,baseRefName"]
        if repo:
            view_args.extend(["--repo", repo])
        pr = _run_gh_json(view_args, env=env)
        return _normalized_pr_payload(repo, pr, body)

    def _cleanup_worktree(self, target_path: Path, git_context: dict[str, Any]) -> None:
        worktree_path = str(git_context.get("worktree_path") or "").strip()
        if worktree_path:
            try:
                _run_local_text(["git", "worktree", "remove", "--force", worktree_path], cwd=target_path)
                _run_local_text(["git", "worktree", "prune"], cwd=target_path)
            except ToolExecutionError:
                pass

    def _github_connection(self, context: ToolExecutionContext) -> ToolConnection | None:
        return context.session.scalar(
            select(ToolConnection).where(
                ToolConnection.domain_id == context.domain.id,
                ToolConnection.tool_key == "github",
                ToolConnection.is_active.is_(True),
            )
        )

    def _generated_branch_name(self, context: ToolExecutionContext, payload: dict[str, Any]) -> str:
        prefix = str(
            payload.get("branch_prefix")
            or _connection_config(context.connection).get("branch_prefix")
            or "maestro/codex"
        ).strip().strip("/")
        issue_number = str(payload.get("issue_number") or payload.get("number") or "").strip()
        title = str(payload.get("task_title") or payload.get("title") or payload.get("task") or payload.get("prompt") or "task")
        slug = _slug_text(title, max_chars=42)
        suffix = f"issue-{issue_number}-{slug}" if issue_number else slug
        return f"{prefix}/{suffix}-{str(context.task.id)[:8]}"

    def _worktree_path(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
        target_path: Path,
        branch_name: str,
    ) -> Path:
        configured_root = str(
            payload.get("worktree_root")
            or _connection_config(context.connection).get("worktree_root")
            or ""
        ).strip()
        root = Path(configured_root).expanduser() if configured_root else target_path.parent / ".maestro_worktrees"
        return (root / _slug_text(branch_name, max_chars=90)).resolve()


class LocalAppReloadAdapter:
    def __init__(self, key: str):
        self.key = key

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.key == "local.app.inspect":
            target_path = _local_target_path(context.connection, payload)
            return _runtime_git_health(
                target_path,
                expected_branch=_runtime_branch(context.connection, payload),
            )
        if self.key == "local.app.recover":
            return self._recover_runtime(context, payload)
        if self.key == "local.app.deploy_pr":
            return self._deploy_pr(context, payload)
        if self.key != "local.app.reload":
            raise ToolExecutionError(f"Unsupported local app tool: {self.key}")
        target_path = _local_target_path(context.connection, payload)
        pull_latest = _bool_setting(payload, context.connection, "pull_latest", default=True)
        expected_branch = _runtime_branch(context.connection, payload)
        health = _runtime_git_health(target_path, expected_branch=expected_branch)
        _require_reloadable_runtime(health)
        commands: list[list[str]] = []
        if pull_latest:
            if expected_branch:
                commands.append(["git", "checkout", expected_branch])
            commands.append(["git", "pull", "--ff-only"])
        commands.extend(_reload_commands(context.connection, payload))
        if not commands:
            raise ToolExecutionError("Reload tool has no configured commands to run.")
        results = []
        for command in commands:
            started_at = datetime.now(UTC).isoformat()
            output = _run_local_text(command, cwd=target_path, timeout=300)
            results.append(
                {
                    "command": command,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "output_tail": output[-2000:],
                }
            )
        return {
            "target_path": str(target_path),
            "pull_latest": pull_latest,
            "preflight": health,
            "commands": results,
            "write_status": "reloaded",
            "summary": {
                "type": "local_app_reload",
                "target_path": str(target_path),
                "command_count": len(results),
                "pull_latest": pull_latest,
            },
        }

    def _recover_runtime(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        target_path = _local_target_path(context.connection, payload)
        expected_branch = _runtime_branch(context.connection, payload)
        health = _runtime_git_health(target_path, expected_branch=expected_branch)
        if health["is_clean"]:
            return {
                "target_path": str(target_path),
                "write_status": "not_needed",
                "preflight": health,
                "summary": {
                    "type": "local_app_recovery",
                    "action": "not_needed",
                    "target_path": str(target_path),
                },
            }
        action = str(payload.get("action") or "stash").strip().lower()
        if action != "stash":
            raise ToolExecutionError(
                "Runtime recovery supports only action=stash. Maestro will never discard or commit "
                "unknown runtime changes automatically."
            )
        label = str(payload.get("message") or "Maestro runtime recovery").strip()
        output = _run_local_text(
            ["git", "stash", "push", "--include-untracked", "-m", label],
            cwd=target_path,
            timeout=120,
        )
        recovered = _runtime_git_health(target_path, expected_branch=expected_branch)
        return {
            "target_path": str(target_path),
            "action": action,
            "stash_output": output[-2000:],
            "preflight": health,
            "post_recovery": recovered,
            "write_status": "recovered",
            "summary": {
                "type": "local_app_recovery",
                "action": action,
                "target_path": str(target_path),
                "changed_file_count": len(health["changed_files"]),
            },
        }

    def _deploy_pr(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        target_path = _local_target_path(context.connection, payload)
        expected_branch = _runtime_branch(context.connection, payload)
        health = _runtime_git_health(target_path, expected_branch=expected_branch)
        _require_reloadable_runtime(health)
        github_connection = context.session.scalar(
            select(ToolConnection).where(
                ToolConnection.domain_id == context.domain.id,
                ToolConnection.tool_key == "github",
                ToolConnection.is_active.is_(True),
            )
        )
        if github_connection is None:
            raise ToolExecutionError(
                "Cannot deliver the PR because this domain has no active GitHub connection."
            )
        github_context = ToolExecutionContext(
            session=context.session,
            agent=context.agent,
            domain=context.domain,
            task=context.task,
            connection=github_connection,
            dry_run=context.dry_run,
        )
        merge_output = GitHubCliToolAdapter("github.pr.merge").execute(github_context, payload)
        try:
            reload_output = LocalAppReloadAdapter("local.app.reload").execute(context, payload)
        except ToolExecutionError as exc:
            raise ToolExecutionError(
                "The PR merged successfully, but the runtime could not reload. "
                f"Run local.app.inspect and resolve the reported runtime state: {exc}"
            ) from exc
        return {
            "target_path": str(target_path),
            "pr": merge_output,
            "reload": reload_output,
            "write_status": "merged_and_reloaded",
            "summary": {
                "type": "local_app_deploy_pr",
                "target_path": str(target_path),
                "pr_number": merge_output.get("pr_number"),
                "merged": True,
                "reloaded": True,
            },
        }


class LLMGatewayToolAdapter:
    def __init__(self, key: str = "llm.gateway"):
        self.key = key

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = str(
            payload.get("prompt")
            or payload.get("input")
            or payload.get("task")
            or payload.get("request")
            or payload.get("instruction")
            or ""
        ).strip()
        if not prompt:
            raise ToolExecutionError("llm.gateway requires a prompt payload.")
        instructions = str(
            payload.get("instructions")
            or (
                "You are a Maestro support model helping an authorized domain agent reason "
                "through a delegated task. Be concise, practical, and explicit about uncertainty."
            )
        )
        context_text = str(payload.get("context") or "").strip()
        input_text = prompt if not context_text else f"{prompt}\n\nContext:\n{context_text}"
        if context.dry_run:
            return {
                "dry_run": True,
                "tool": self.key,
                "prompt_preview": prompt[:500],
                "context_chars": len(context_text),
            }

        model = str(payload.get("model") or _agent_model_profile(context.agent) or "").strip()
        client = OpenAILLMClient(model=model if model and model != "default" else None)
        output_text = client.text_response(instructions=instructions, input_text=input_text)
        return {
            "summary": {
                "type": "llm_gateway_response",
                "provider": client.provider,
                "model": client.model,
                "output_chars": len(output_text),
            },
            "output_text": output_text,
        }


class WebSearchToolAdapter:
    def __init__(self, key: str = "web.search"):
        self.key = key

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(payload.get("query") or payload.get("prompt") or "").strip()
        if not query:
            raise ToolExecutionError("web.search requires a query payload.")
        instructions = str(
            payload.get("instructions")
            or (
                "You are a Maestro research tool. Search the web only as needed, synthesize "
                "current findings, cite sources from annotations, and clearly distinguish facts "
                "from inference."
            )
        )
        search_parameters = _web_search_parameters(payload)
        model = str(payload.get("model") or _agent_model_profile(context.agent) or "").strip()
        if context.dry_run:
            return {
                "dry_run": True,
                "tool": self.key,
                "query": query,
                "search_parameters": search_parameters,
            }
        client = OpenAILLMClient(model=model if model and model != "default" else None)
        result = client.web_search_response(
            instructions=instructions,
            input_text=query,
            search_parameters=search_parameters,
        )
        annotations = result.get("annotations") or []
        citations = _citations_from_annotations(annotations)
        output_text = str(result.get("output_text") or "")
        return {
            "summary": {
                "type": "web_search_response",
                "provider": client.provider,
                "model": client.model,
                "citation_count": len(citations),
                "output_chars": len(output_text),
            },
            "query": query,
            "search_parameters": search_parameters,
            "output_text": output_text,
            "citations": citations,
            "annotations": annotations,
            "usage": result.get("usage"),
        }


class MemoryContextBundleToolAdapter:
    key = "memory.context_bundle"

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        domain_key = str(payload.get("domain_key") or context.domain.key)
        domain = DomainRepository(context.session).get_by_key(domain_key)
        if domain is None:
            raise ToolExecutionError(f"Unknown memory domain: {domain_key}")
        query_text = str(
            payload.get("query_text")
            or payload.get("query")
            or payload.get("prompt")
            or context.task.objective
            or ""
        ).strip()
        memory_types = payload.get("memory_type", payload.get("memory_types"))
        if isinstance(memory_types, str):
            memory_type_set = {
                item.strip()
                for item in memory_types.split(",")
                if item.strip()
            }
        elif isinstance(memory_types, list):
            memory_type_set = {str(item).strip() for item in memory_types if str(item).strip()}
        else:
            memory_type_set = None
        capabilities = context.agent.capabilities if isinstance(context.agent.capabilities, dict) else {}
        try:
            bundle = MemoryRetrievalService(context.session).build_context_bundle(
                MemoryContextBundleRequest(
                    profile=str(payload.get("profile") or capabilities.get("memory_profile") or "agent_prompt"),  # type: ignore[arg-type]
                    audience=str(payload.get("audience") or "agent"),  # type: ignore[arg-type]
                    domain_id=domain.id,
                    agent_id=context.agent.id,
                    query_text=query_text or None,
                    memory_types=memory_type_set,
                    min_importance=_optional_float(payload.get("min_importance")),
                    use_semantic=_optional_bool(payload.get("use_semantic"), default=True),
                    max_items=_bounded_int(payload.get("max_items"), default=12, minimum=1, maximum=40),
                    max_chars=_bounded_int(payload.get("max_chars"), default=4000, minimum=200, maximum=12000),
                )
            )
        except MemoryRetrievalError as exc:
            raise ToolExecutionError(str(exc)) from exc
        return _memory_context_bundle_payload(bundle, domain_key=domain.key)


class ReportRetrievalToolAdapter:
    def __init__(self, key: str):
        self.key = key

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.key == "reports.get":
            return self._get_report(context, payload)
        return self._search_reports(context, payload)

    def _search_reports(self, context: ToolExecutionContext, payload: dict[str, Any]) -> dict[str, Any]:
        query_text = str(payload.get("query_text") or payload.get("query") or context.task.objective or "").strip()
        limit = _bounded_int(payload.get("limit") or payload.get("max_items"), default=8, minimum=1, maximum=25)
        include_global = _optional_bool(payload.get("include_global"), default=True)
        statement = select(Report).order_by(Report.created_at.desc()).limit(limit * 3)
        domain_filters = [Report.domain_id == context.domain.id]
        if include_global:
            domain_filters.append(Report.domain_id.is_(None))
        filters = [or_(*domain_filters)]
        if query_text:
            like = f"%{query_text}%"
            filters.append(
                or_(
                    Report.title.ilike(like),
                    Report.summary.ilike(like),
                    Report.body_markdown.ilike(like),
                )
            )
        reports = [
            report
            for report in context.session.scalars(statement.where(*filters)).all()
            if not _report_is_archived(report)
        ][:limit]
        return {
            "summary": {
                "type": "reports_search",
                "domain_key": context.domain.key,
                "query_text": query_text,
                "returned_count": len(reports),
            },
            "reports": [_compact_report_payload(report, include_body=False) for report in reports],
        }

    def _get_report(self, context: ToolExecutionContext, payload: dict[str, Any]) -> dict[str, Any]:
        report_id = str(payload.get("report_id") or payload.get("id") or "").strip()
        if not report_id:
            raise ToolExecutionError("reports.get requires report_id.")
        try:
            parsed_id = uuid.UUID(report_id)
        except ValueError as exc:
            raise ToolExecutionError("reports.get report_id must be a UUID.") from exc
        report = context.session.get(Report, parsed_id)
        if report is None:
            raise ToolExecutionError(f"Unknown report: {report_id}")
        if report.domain_id not in (None, context.domain.id):
            raise ToolExecutionError("This agent cannot access that report domain.")
        return {
            "summary": {
                "type": "reports_get",
                "domain_key": context.domain.key,
                "report_id": str(report.id),
                "title": report.title,
            },
            "report": _compact_report_payload(report, include_body=True),
        }


class StageInteractionArtifactToolAdapter:
    key = "artifact.stage_interaction"

    def execute(
        self,
        context: ToolExecutionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "summary": {
                "type": "artifact_stage_deferred",
                "domain_key": context.domain.key,
                "task_id": str(context.task.id),
            },
            "write_status": "deferred_to_runtime",
            "message": (
                "The final agent/workflow artifact will be staged by Maestro after the agent "
                "produces its report, so this request does not need separate approval."
            ),
            "requested_payload": _redact_payload(payload),
        }


def default_tool_adapters() -> dict[str, ToolAdapter]:
    adapters: dict[str, ToolAdapter] = {
        key: GitHubCliToolAdapter(key)
        for key in (
            "github.read",
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
            "github.pr.merge",
        )
    }
    adapters.update(
        {
            key: GmailApiToolAdapter(key)
            for key in (
                "gmail.message.search",
                "gmail.message.list_recent",
                "gmail.message.get",
                "gmail.thread.get",
                "gmail.draft.create",
                "gmail.message.modify",
            )
        }
    )
    adapters.update(
        {
            key: GoogleWorkspaceToolAdapter(key)
            for key in (
                "google.drive.file.get",
                "google.drive.file.export",
                "google.docs.get",
                "google.slides.get",
                "google.sheets.get",
                "google.sheets.values.get",
                "google.meet.conference_records.list",
                "google.meet.conference_records.get",
            )
        }
    )
    adapters["codex.task.run"] = CodexCliToolAdapter("codex.task.run")
    adapters["llm.gateway"] = LLMGatewayToolAdapter("llm.gateway")
    adapters["web.search"] = WebSearchToolAdapter("web.search")
    adapters["memory.context_bundle"] = MemoryContextBundleToolAdapter()
    adapters["routed.item.create"] = RoutedItemCreateToolAdapter()
    adapters["workflow.notification.create"] = WorkflowNotificationCreateToolAdapter()
    adapters["reports.search"] = ReportRetrievalToolAdapter("reports.search")
    adapters["reports.get"] = ReportRetrievalToolAdapter("reports.get")
    adapters["artifact.stage_interaction"] = StageInteractionArtifactToolAdapter()
    for key in (
        "local.app.inspect",
        "local.app.recover",
        "local.app.reload",
        "local.app.deploy_pr",
    ):
        adapters[key] = LocalAppReloadAdapter(key)
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
    labels_may_skip = _dedupe_strings(
        [*payload_labels, *label_policy["preferred"]]
    )
    required_missing = [] if label_policy["required"] else []
    uncertainty = [
        (
            "Label existence is verified at approval time with GitHub; optional missing "
            "labels will be skipped."
        ),
        "This write will not create repository labels; missing optional labels are reported only.",
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
    if body_preview:
        summary_lines.append(f"Body preview: {_single_line_preview(body_preview, max_chars=220)}")
    if labels_to_apply:
        summary_lines.append(f"Labels to apply if present: {', '.join(labels_to_apply)}")
    if labels_may_skip:
        summary_lines.append(f"Optional labels that may be skipped: {', '.join(labels_may_skip)}")
    if label_policy["required"]:
        summary_lines.append(f"Required labels: {', '.join(label_policy['required'])}")
    summary_lines.append("Labels proposed for creation: none.")
    summary_lines.append("Missing optional labels: skipped and reported at execution.")
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
        "labels_may_skip": labels_may_skip,
        "labels_missing_optional": labels_skipped,
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


def _connection_config(connection: ToolConnection | None) -> dict[str, Any]:
    return dict(connection.config or {}) if connection is not None else {}


def _bool_setting(
    payload: dict[str, Any],
    connection: ToolConnection | None,
    key: str,
    *,
    default: bool,
) -> bool:
    if key in payload:
        return _as_bool(payload.get(key), default=default)
    config = _connection_config(connection)
    if key in config:
        return _as_bool(config.get(key), default=default)
    return default


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def _github_issue_number_from_url(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def _github_check_summary(checks: Any) -> dict[str, Any]:
    counts = {"passed": 0, "failed": 0, "pending": 0, "skipped": 0, "unknown": 0}
    failed_checks: list[str] = []
    pending_checks: list[str] = []
    if not isinstance(checks, list):
        return {
            "check_status": "unknown",
            "check_counts": counts,
            "failed_checks": failed_checks,
            "pending_checks": pending_checks,
        }

    for check in checks:
        if not isinstance(check, dict):
            counts["unknown"] += 1
            continue
        raw_state = str(check.get("state") or check.get("bucket") or "").strip().lower()
        name = str(check.get("name") or check.get("workflow") or "").strip()
        if raw_state in {"pass", "passed", "success", "successful", "completed"}:
            counts["passed"] += 1
        elif raw_state in {"fail", "failed", "failure", "error", "cancelled", "timed_out"}:
            counts["failed"] += 1
            if name:
                failed_checks.append(name)
        elif raw_state in {"pending", "queued", "in_progress", "waiting", "requested"}:
            counts["pending"] += 1
            if name:
                pending_checks.append(name)
        elif raw_state in {"skipping", "skipped", "neutral"}:
            counts["skipped"] += 1
        else:
            counts["unknown"] += 1

    if counts["failed"]:
        status = "failed"
    elif counts["pending"]:
        status = "pending"
    elif counts["unknown"]:
        status = "unknown"
    elif checks:
        status = "passed"
    else:
        status = "none"
    return {
        "check_status": status,
        "check_counts": counts,
        "failed_checks": failed_checks,
        "pending_checks": pending_checks,
    }


def _preview_text(value: str, *, max_chars: int) -> str:
    stripped = value.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 3].rstrip() + "..."


def _single_line_preview(value: str, *, max_chars: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


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
    owner = str(payload.get("owner") or "").strip()
    if repo and owner and "/" not in repo:
        repo = f"{owner}/{repo}"
    if not repo and connection is not None:
        config = connection.config or {}
        repo = str(config.get("repo") or "").strip()
        owner = str(config.get("owner") or "").strip()
        if repo and owner and "/" not in repo:
            repo = f"{owner}/{repo}"
    if not repo:
        raise ToolExecutionError("GitHub tool requires a repo, e.g. Caliperti1/Maestro.")
    if "/" not in repo:
        raise ToolExecutionError("GitHub repo must be in owner/name form.")
    return repo


def _repo_parts(repo: str) -> tuple[str | None, str | None]:
    if "/" not in repo:
        return None, repo or None
    owner, name = repo.split("/", 1)
    return owner or None, name or None


def _slug_text(value: str, *, max_chars: int = 60) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    slug = "-".join(part for part in slug.split("-") if part)
    return (slug[:max_chars].strip("-") or "task")


def _run_local_text(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
        env=env or os.environ.copy(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise ToolExecutionError(f"{' '.join(args)} failed: {detail}")
    return completed.stdout


def _git_changed_paths(target_path: Path) -> list[str]:
    output = _run_local_text(["git", "status", "--porcelain"], cwd=target_path)
    paths: list[str] = []
    for line in output.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if path:
            paths.append(path)
    return paths


def _codex_commit_message(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("commit_message") or "").strip()
    if explicit:
        return explicit
    issue_number = str(payload.get("issue_number") or payload.get("number") or "").strip()
    title = str(payload.get("task_title") or payload.get("title") or "").strip()
    if issue_number and title:
        return f"Implement issue #{issue_number}: {_single_line_preview(title, max_chars=60)}"
    if issue_number:
        return f"Implement issue #{issue_number}"
    return f"Implement Maestro coding task: {_single_line_preview(str(payload.get('task') or payload.get('prompt') or 'Codex changes'), max_chars=60)}"


def _codex_pr_body(
    *,
    title: str,
    final_message: str,
    changed_paths: list[str],
    issue_number: str = "",
) -> str:
    changed = "\n".join(f"- `{path}`" for path in changed_paths) or "- No file changes detected."
    report = final_message.strip() or "Codex completed without a final message."
    closes = f"\n\nCloses #{issue_number}" if issue_number else ""
    return (
        "## Maestro Coding Agent Summary\n"
        f"{report}\n\n"
        "## Changed Files\n"
        f"{changed}\n\n"
        "## Review Notes\n"
        "- Review the diff before merge.\n"
        "- Merge and hot reload require explicit Chris approval."
        f"{closes}"
    )


def _normalized_pr_payload(
    repo: str | None,
    pr: Any,
    fallback_body: str,
) -> dict[str, Any]:
    payload = pr if isinstance(pr, dict) else {}
    url = str(payload.get("url") or "").strip()
    repo_owner, repo_name = _repo_parts(repo or "")
    return {
        "repo": repo,
        "owner": repo_owner,
        "name": repo_name,
        "repo_name": repo_name,
        "number": payload.get("number"),
        "pr_number": payload.get("number"),
        "url": url or None,
        "pr_url": url or None,
        "html_url": url or None,
        "title": payload.get("title"),
        "body": payload.get("body") or fallback_body,
        "head_ref": payload.get("headRefName"),
        "base_ref": payload.get("baseRefName"),
    }


def _local_target_path(connection: ToolConnection | None, payload: dict[str, Any]) -> Path:
    config = _connection_config(connection)
    raw_target = str(
        payload.get("target_path")
        or payload.get("target_directory")
        or payload.get("cwd")
        or config.get("default_cwd")
        or config.get("target_path")
        or os.getcwd()
    ).strip()
    target = Path(raw_target).expanduser().resolve()
    if not target.exists() or not target.is_dir():
        raise ToolExecutionError(f"Reload target path is not a directory: {target}")
    allowed_roots = _string_list(config.get("allowed_roots"))
    roots = [Path(root).expanduser().resolve() for root in allowed_roots] if allowed_roots else [target]
    if not any(target == root or root in target.parents for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ToolExecutionError(f"Reload target path must be inside an allowed root: {allowed}")
    return target


def _runtime_branch(connection: ToolConnection | None, payload: dict[str, Any]) -> str:
    return str(payload.get("branch") or _connection_config(connection).get("branch") or "main").strip()


def _runtime_git_health(target_path: Path, *, expected_branch: str) -> dict[str, Any]:
    if not (target_path / ".git").exists():
        raise ToolExecutionError(f"Runtime target is not a Git checkout: {target_path}")
    status_output = _run_local_text(["git", "status", "--porcelain=v1"], cwd=target_path)
    changed_files = []
    runtime_support_paths = {".venv", "frontend/node_modules"}
    for line in status_output.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if path in runtime_support_paths:
            continue
        changed_files.append({"status": line[:2], "path": path})
    branch = _run_local_text(["git", "branch", "--show-current"], cwd=target_path).strip()
    head = _run_local_text(["git", "rev-parse", "HEAD"], cwd=target_path).strip()
    upstream = ""
    ahead = behind = None
    try:
        upstream = _run_local_text(
            ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
            cwd=target_path,
        ).strip()
        counts = _run_local_text(
            ["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
            cwd=target_path,
        ).strip().split()
        if len(counts) == 2:
            ahead, behind = int(counts[0]), int(counts[1])
    except ToolExecutionError:
        upstream = None
    is_clean = not changed_files
    recovery_actions = []
    if not is_clean:
        recovery_actions.append("stash")
    if branch != expected_branch:
        recovery_actions.append("switch_to_expected_branch_after_review")
    return {
        "target_path": str(target_path),
        "expected_branch": expected_branch,
        "branch": branch or None,
        "head": head,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "is_clean": is_clean,
        "changed_files": changed_files,
        "recovery_actions": recovery_actions,
        "summary": {
            "type": "local_app_runtime_health",
            "target_path": str(target_path),
            "branch": branch or None,
            "expected_branch": expected_branch,
            "is_clean": is_clean,
            "changed_file_count": len(changed_files),
            "ahead": ahead,
            "behind": behind,
        },
    }


def _require_reloadable_runtime(health: dict[str, Any]) -> None:
    if not health.get("is_clean"):
        paths = ", ".join(str(item.get("path")) for item in health.get("changed_files", [])[:8])
        raise ToolExecutionError(
            "Runtime checkout has uncommitted changes and was not modified. "
            f"Changed files: {paths or 'unknown'}. Inspect it with local.app.inspect, then ask Chris "
            "to approve local.app.recover with action=stash before retrying deployment."
        )


def _reload_commands(connection: ToolConnection | None, payload: dict[str, Any]) -> list[list[str]]:
    config = _connection_config(connection)
    raw_commands = payload.get("commands") or payload.get("reload_commands") or config.get("reload_commands") or []
    commands: list[list[str]] = []
    for raw in raw_commands if isinstance(raw_commands, list) else [raw_commands]:
        if isinstance(raw, str):
            command = shlex.split(raw)
        elif isinstance(raw, list):
            command = [str(part) for part in raw if str(part).strip()]
        else:
            continue
        if command:
            commands.append(command)
    return commands



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


def _github_read_search_terms(text: str) -> list[str]:
    lowered = text.lower()
    phrase_terms = [
        ("tool registry", "tool registry"),
        ("tool manifest", "tool manifest"),
        ("tool access", "tool access"),
        ("agent authorization", "agent authorization"),
        ("permission", "permission"),
        ("credential", "credential"),
        ("oauth", "oauth"),
        ("gmail", "gmail"),
        ("google", "google"),
        ("github", "github"),
        ("artifact", "artifact"),
        ("provenance", "provenance"),
        ("scheduler", "scheduler"),
        ("queue", "scheduler queue"),
        ("workflow", "workflow"),
        ("memory", "memory retrieval"),
        ("prompt", "prompt aggregation"),
        ("frontend", "frontend"),
        ("ui", "frontend ui"),
        ("api", "api route"),
        ("database", "database model"),
        ("model", "database model"),
        ("test", "test"),
    ]
    terms: list[str] = []
    for marker, term in phrase_terms:
        if marker in lowered and term not in terms:
            terms.append(term)
    if terms:
        return terms[:8]

    stopwords = {
        "about",
        "after",
        "agent",
        "agents",
        "architecture",
        "before",
        "code",
        "codebase",
        "current",
        "find",
        "for",
        "from",
        "how",
        "inspect",
        "latest",
        "maestro",
        "need",
        "needs",
        "read",
        "repo",
        "repository",
        "system",
        "that",
        "the",
        "this",
        "with",
    }
    words = [
        word
        for word in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", lowered)
        if word not in stopwords
    ]
    for word in words:
        if word not in terms:
            terms.append(word)
        if len(terms) >= 8:
            break
    return terms or ["README", "docs", "app", "tests"]


def _provider_key(tool_key: str) -> str:
    if tool_key.startswith("github."):
        return "github"
    if tool_key.startswith("gmail."):
        return "google"
    if tool_key.startswith("google."):
        return "google"
    if tool_key.startswith("codex."):
        return "codex"
    if tool_key.startswith("local.app."):
        return "local.app.reload"
    return tool_key


def _normalize_route_type(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    aliases = {
        "todo": "task",
        "to_do": "task",
        "organization": "entity",
        "org": "entity",
        "decision": "decision_log",
        "idea": "think_tank",
        "rfi": "human_input",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {
        "task",
        "human_input",
        "event",
        "contact",
        "entity",
        "think_tank",
        "decision_log",
        "project",
        "integration_note",
        "ignore",
    }
    return normalized if normalized in allowed else ""


def _source_refs_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    source_refs = payload.get("source_refs")
    if isinstance(source_refs, list):
        cleaned = [item for item in source_refs if isinstance(item, dict)]
        if cleaned:
            return cleaned
    refs: list[dict[str, Any]] = []
    gmail_ref = {
        key: payload.get(key)
        for key in ("message_id", "thread_id", "subject", "from", "date")
        if payload.get(key)
    }
    if gmail_ref:
        refs.append({"type": "gmail_message", **gmail_ref})
    report_id = str(payload.get("report_id") or "").strip()
    if report_id:
        refs.append({"type": "report", "report_id": report_id})
    artifact_id = str(payload.get("artifact_id") or "").strip()
    if artifact_id:
        refs.append({"type": "artifact", "artifact_id": artifact_id})
    return refs


def _memory_context_bundle_payload(
    bundle: MemoryContextBundle,
    *,
    domain_key: str,
) -> dict[str, Any]:
    request = bundle.request
    return {
        "summary": {
            "type": "memory_context_bundle",
            "domain_key": domain_key,
            "query_text": request.query_text,
            "included_count": bundle.included_count,
            "semantic_status": bundle.semantic_status,
        },
        "profile": request.profile,
        "audience": request.audience,
        "domain_key": domain_key,
        "agent_id": str(request.agent_id) if request.agent_id else None,
        "query_text": request.query_text,
        "memory_type": sorted(request.memory_types or []),
        "min_importance": request.min_importance,
        "use_semantic": request.use_semantic,
        "semantic_status": bundle.semantic_status,
        "max_items": request.max_items,
        "max_chars": bundle.max_chars,
        "used_chars": bundle.used_chars,
        "total_visible": bundle.total_visible,
        "filtered_count": bundle.filtered_count,
        "retrieved_count": bundle.retrieved_count,
        "included_count": bundle.included_count,
        "dropped_count": bundle.dropped_count,
        "retrieval_query": {
            "mode": bundle.retrieval_query.mode,
            "limit": bundle.retrieval_query.limit,
            "include_agent_memory": bundle.retrieval_query.include_agent_memory,
            "include_session_memory": bundle.retrieval_query.include_session_memory,
            "include_links": bundle.retrieval_query.include_links,
        },
        "sections": [_memory_context_section_payload(section) for section in bundle.sections],
        "rendered_text": bundle.rendered_text,
    }


def _compact_report_payload(report: Report, *, include_body: bool) -> dict[str, Any]:
    payload = {
        "id": str(report.id),
        "task_id": str(report.task_id) if report.task_id else None,
        "domain_id": str(report.domain_id) if report.domain_id else None,
        "agent_id": str(report.agent_id) if report.agent_id else None,
        "title": report.title,
        "report_type": report.report_type,
        "summary": report.summary,
        "archived": _report_is_archived(report),
        "created_at": report.created_at.isoformat(),
        "updated_at": report.updated_at.isoformat(),
    }
    if include_body:
        payload["body_markdown"] = report.body_markdown
    else:
        payload["body_preview"] = " ".join(report.body_markdown.split())[:800]
    return payload


def _report_is_archived(report: Report) -> bool:
    return bool((report.structured_data or {}).get("archived"))


def _memory_context_section_payload(section: MemoryContextSection) -> dict[str, Any]:
    return {
        "key": section.key,
        "label": section.label,
        "used_chars": section.used_chars,
        "memories": [_memory_context_snippet_payload(snippet) for snippet in section.snippets],
    }


def _memory_context_snippet_payload(snippet: MemoryContextSnippet) -> dict[str, Any]:
    memory = snippet.memory
    return {
        "id": str(memory.id),
        "title": memory.title,
        "scope": memory.scope,
        "memory_type": memory.memory_type,
        "importance": memory.importance,
        "excerpt": snippet.excerpt,
        "score": snippet.score,
        "query_relevance": snippet.query_relevance,
        "semantic_similarity": snippet.semantic_similarity,
        "score_reasons": snippet.score_reasons,
        "provenance": {
            "source_refs": snippet.provenance.source_refs,
            "seed_package": snippet.provenance.seed_package,
            "artifact": snippet.provenance.artifact,
            "processed_path": snippet.provenance.processed_path,
        },
        "links": [
            {
                "relation_type": link.relation_type,
                "direction": link.direction,
                "memory": {
                    "id": str(link.memory.id),
                    "title": link.memory.title,
                    "scope": link.memory.scope,
                    "memory_type": link.memory.memory_type,
                    "importance": link.memory.importance,
                },
                "metadata": link.metadata,
            }
            for link in snippet.links
        ],
    }


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _gmail_access_token(connection: ToolConnection | None) -> str:
    config = _connection_config(connection)
    refresh_token = _secret_config_value(
        config,
        "refresh_token",
        env_keys=("refresh_token_env", "env_refresh_token_name"),
    )
    if refresh_token:
        client_id = _secret_config_value(
            config,
            "client_id",
            env_keys=("client_id_env", "env_client_id_name"),
        )
        client_secret = _secret_config_value(
            config,
            "client_secret",
            env_keys=("client_secret_env", "env_client_secret_name"),
        )
        if not client_id:
            raise ToolExecutionError("Gmail refresh-token OAuth requires client_id or client_id_env.")
        if not client_secret:
            raise ToolExecutionError("Gmail refresh-token OAuth requires client_secret or client_secret_env.")
        token_payload = _google_oauth_refresh_access_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        access_token = str(token_payload.get("access_token") or "").strip()
        if not access_token:
            raise ToolExecutionError("Google OAuth token refresh did not return an access token.")
        return access_token

    env_names = [
        str(config.get("access_token_env") or "").strip(),
        str(config.get("env_token_name") or "").strip(),
        str(config.get("token_env_name") or "").strip(),
    ]
    for env_name in env_names:
        if not env_name:
            continue
        token = os.environ.get(env_name) or _dotenv_value(env_name)
        if token:
            return token
        raise ToolExecutionError(f"Gmail access token env var is not set: {env_name}")
    token = str(config.get("access_token") or config.get("token") or "").strip()
    if token:
        return token
    raise ToolExecutionError(
        "Gmail tools require refresh-token OAuth credentials. Configure client_id_env, "
        "client_secret_env, and refresh_token_env on the domain Gmail connection."
    )


def _secret_config_value(
    config: dict[str, Any],
    key: str,
    *,
    env_keys: tuple[str, ...],
) -> str | None:
    for env_key in env_keys:
        env_name = str(config.get(env_key) or "").strip()
        if not env_name:
            continue
        value = os.environ.get(env_name) or _dotenv_value(env_name)
        if value:
            return value
        raise ToolExecutionError(f"Gmail OAuth env var is not set: {env_name}")
    value = str(config.get(key) or "").strip()
    return value or None


def _google_oauth_refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    data = urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    request = Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ToolExecutionError(f"Google OAuth refresh failed: {exc.code} {detail}") from exc
    except URLError as exc:
        raise ToolExecutionError(f"Google OAuth refresh failed: {exc.reason}") from exc
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise ToolExecutionError("Google OAuth refresh returned an unexpected response.")
    return parsed


def _gmail_user_id(connection: ToolConnection | None, payload: dict[str, Any]) -> str:
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        user_id = str(_connection_config(connection).get("user_id") or "me").strip()
    return user_id or "me"


def _gmail_api_json(
    method: str,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    query = ""
    if params:
        query_items: list[tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, list):
                query_items.extend((key, str(item)) for item in value)
            else:
                query_items.append((key, str(value)))
        query = f"?{urlencode(query_items)}" if query_items else ""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"https://gmail.googleapis.com{path}{query}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ToolExecutionError(f"Gmail API {method} {path} failed: {exc.code} {detail}") from exc
    except URLError as exc:
        raise ToolExecutionError(f"Gmail API {method} {path} failed: {exc.reason}") from exc
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ToolExecutionError("Gmail API returned an unexpected response.")
    return parsed


def _google_api_json(
    method: str,
    base_url: str,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    raw = _google_api_raw(
        method,
        base_url,
        path,
        token=token,
        params=params,
        body=body,
        timeout=timeout,
    )
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ToolExecutionError("Google API returned an unexpected response.")
    return parsed


def _google_api_text(
    method: str,
    base_url: str,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> str:
    return _google_api_raw(
        method,
        base_url,
        path,
        token=token,
        params=params,
        body=body,
        timeout=timeout,
    )


def _google_api_raw(
    method: str,
    base_url: str,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> str:
    query = ""
    if params:
        query_items: list[tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, list):
                query_items.extend((key, str(item)) for item in value)
            else:
                query_items.append((key, str(value)))
        query = f"?{urlencode(query_items)}" if query_items else ""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{base_url}{path}{query}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ToolExecutionError(f"Google API {method} {path} failed: {exc.code} {detail}") from exc
    except URLError as exc:
        raise ToolExecutionError(f"Google API {method} {path} failed: {exc.reason}") from exc


def _google_file_id(payload: dict[str, Any]) -> str:
    raw = str(
        payload.get("file_id")
        or payload.get("document_id")
        or payload.get("presentation_id")
        or payload.get("spreadsheet_id")
        or payload.get("id")
        or ""
    ).strip()
    if not raw:
        raw = str(payload.get("url") or payload.get("web_view_link") or "").strip()
    file_id = _google_file_id_from_url(raw) if raw.startswith("http") else raw
    if not file_id:
        raise ToolExecutionError("Google Workspace tool requires file_id, document_id, id, or a Google file URL.")
    return file_id


def _google_file_id_from_url(value: str) -> str | None:
    patterns = [
        r"/document/d/([^/?#]+)",
        r"/spreadsheets/d/([^/?#]+)",
        r"/presentation/d/([^/?#]+)",
        r"/file/d/([^/?#]+)",
        r"[?&]id=([^&#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return None


def _google_doc_text(document: dict[str, Any]) -> str:
    chunks: list[str] = []
    body = document.get("body") if isinstance(document.get("body"), dict) else {}
    for item in body.get("content") or []:
        paragraph = item.get("paragraph") if isinstance(item, dict) else None
        if not isinstance(paragraph, dict):
            continue
        for element in paragraph.get("elements") or []:
            text_run = element.get("textRun") if isinstance(element, dict) else None
            if isinstance(text_run, dict):
                content = str(text_run.get("content") or "")
                if content:
                    chunks.append(content)
    return "".join(chunks).strip()


def _google_slides_text(presentation: dict[str, Any]) -> str:
    slides = presentation.get("slides") if isinstance(presentation.get("slides"), list) else []
    chunks: list[str] = []
    for index, slide in enumerate(slides, start=1):
        slide_chunks: list[str] = []
        if not isinstance(slide, dict):
            continue
        for element in slide.get("pageElements") or []:
            if not isinstance(element, dict):
                continue
            text_content = element.get("shape", {}).get("text") if isinstance(element.get("shape"), dict) else None
            if not isinstance(text_content, dict):
                continue
            for item in text_content.get("textElements") or []:
                text_run = item.get("textRun") if isinstance(item, dict) else None
                if isinstance(text_run, dict):
                    content = str(text_run.get("content") or "").strip()
                    if content:
                        slide_chunks.append(content)
        if slide_chunks:
            chunks.append(f"Slide {index}: " + " ".join(slide_chunks))
    return "\n".join(chunks).strip()


def _gmail_message_payload(message: dict[str, Any], *, max_body_chars: int) -> dict[str, Any]:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    headers = _gmail_headers(payload)
    body = _gmail_body_text(payload) if max_body_chars > 0 else ""
    google_links = _google_workspace_links(body)
    return {
        "message_id": message.get("id"),
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "label_ids": message.get("labelIds") or [],
        "snippet": message.get("snippet"),
        "history_id": message.get("historyId"),
        "internal_date": message.get("internalDate"),
        "subject": headers.get("subject"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "date": headers.get("date"),
        "body_text": body[:max_body_chars] if max_body_chars > 0 else "",
        "body_truncated": len(body) > max_body_chars if max_body_chars > 0 else False,
        "google_workspace_links": google_links,
        "meeting_notes": [
            link for link in google_links if _looks_like_meeting_notes_link(link)
        ],
        "headers": headers,
    }


def _gmail_headers(payload: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in payload.get("headers") or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip().lower()
        value = str(header.get("value") or "").strip()
        if name:
            headers[name] = value
    return headers


def _gmail_body_text(payload: dict[str, Any]) -> str:
    mime_type = str(payload.get("mimeType") or "")
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    data = str(body.get("data") or "")
    if data and mime_type in {"text/plain", "text/html", ""}:
        decoded = _base64url_decode(data)
        if mime_type == "text/html":
            return _strip_html(decoded)
        return decoded
    parts = payload.get("parts") if isinstance(payload.get("parts"), list) else []
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("mimeType") or "")
        text = _gmail_body_text(part)
        if not text:
            continue
        if part_type == "text/html":
            html_parts.append(text)
        else:
            plain_parts.append(text)
    if plain_parts:
        return "\n\n".join(plain_parts)
    return "\n\n".join(html_parts)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _strip_html(value: str) -> str:
    text = re.sub(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        lambda match: f"{_strip_html(match.group(2))} ({html.unescape(match.group(1))})",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    output = []
    in_tag = False
    for char in text:
        if char == "<":
            in_tag = True
            continue
        if char == ">":
            in_tag = False
            continue
        if not in_tag:
            output.append(char)
    return html.unescape(" ".join("".join(output).split()))


def _google_workspace_links(text: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for match in re.finditer(r"https?://(?:(?:docs|drive)\.google\.com|meet\.google\.com)/[^\s<>)\"']+", text):
        url = match.group(0).rstrip(".,;]")
        file_id = _google_file_id_from_url(url)
        kind = _google_link_kind(url)
        context_start = max(0, match.start() - 120)
        context_end = min(len(text), match.end() + 120)
        item = {
            "url": url,
            "file_id": file_id or "",
            "kind": kind,
            "context": text[context_start:context_end],
        }
        if item not in links:
            links.append(item)
    return links


def _google_link_kind(url: str) -> str:
    if "meet.google.com" in url:
        return "meet"
    if "/document/" in url:
        return "document"
    if "/spreadsheets/" in url:
        return "spreadsheet"
    if "/presentation/" in url:
        return "presentation"
    if "drive.google.com" in url:
        return "drive_file"
    return "google_workspace"


def _looks_like_meeting_notes_link(link: dict[str, str]) -> bool:
    haystack = f"{link.get('url', '')} {link.get('context', '')}".lower()
    return link.get("kind") == "document" and any(
        token in haystack for token in ("meeting", "notes", "minutes", "summary")
    )


def _gmail_rfc822_message(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str],
    bcc: list[str],
    from_address: str,
    reply_to_message_id: str,
) -> str:
    headers = []
    if from_address:
        headers.append(("From", from_address))
    headers.extend(
        [
            ("To", ", ".join(to)),
            ("Subject", subject),
            ("Content-Type", 'text/plain; charset="UTF-8"'),
            ("MIME-Version", "1.0"),
        ]
    )
    if cc:
        headers.append(("Cc", ", ".join(cc)))
    if bcc:
        headers.append(("Bcc", ", ".join(bcc)))
    if reply_to_message_id:
        headers.append(("In-Reply-To", reply_to_message_id))
        headers.append(("References", reply_to_message_id))
    header_text = "\r\n".join(f"{key}: {value}" for key, value in headers)
    return f"{header_text}\r\n\r\n{body}"


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
    timeout: int = 30,
) -> Any:
    output = _run_gh_text(args, env=env, allowed_exit_codes=allowed_exit_codes, timeout=timeout)
    if not output.strip():
        return None
    return json.loads(output)


def _run_gh_text(
    args: list[str],
    *,
    env: dict[str, str],
    allowed_exit_codes: set[int] | None = None,
    timeout: int = 30,
) -> str:
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
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


def _required_int(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    *,
    label: str,
    minimum: int = 1,
    maximum: int = 1_000_000,
) -> int:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError(f"{label} must be an integer.") from exc
        if parsed < minimum or parsed > maximum:
            raise ToolExecutionError(f"{label} must be between {minimum} and {maximum}.")
        return parsed
    choices = "`, `".join(keys)
    raise ToolExecutionError(f"GitHub tool requires `{choices}`.")


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if any(token in lowered for token in ("secret", "token", "api_key", "apikey", "password")):
            redacted[key] = "********" if value else ""
        else:
            redacted[key] = value
    return redacted


def _web_search_parameters(payload: dict[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    engine = str(payload.get("engine") or "").strip()
    if engine:
        parameters["engine"] = engine
    for key in ("max_results", "max_total_results", "max_characters"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        parameters[key] = _bounded_int(value, default=5, minimum=1, maximum=100_000)
    context_size = str(payload.get("search_context_size") or "").strip()
    if context_size in {"low", "medium", "high"}:
        parameters["search_context_size"] = context_size
    allowed_domains = _string_list(payload.get("allowed_domains"))
    excluded_domains = _string_list(payload.get("excluded_domains"))
    if allowed_domains:
        parameters["allowed_domains"] = allowed_domains
    if excluded_domains:
        parameters["excluded_domains"] = excluded_domains
    return parameters


def _agent_model_profile(agent: Agent) -> str | None:
    capabilities = agent.capabilities or {}
    model_profile = capabilities.get("model_profile") if isinstance(capabilities, dict) else None
    return str(model_profile).strip() if model_profile else None


def _citations_from_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for annotation in annotations:
        citation = annotation.get("url_citation") if isinstance(annotation, dict) else None
        if not isinstance(citation, dict):
            continue
        citations.append(
            {
                "url": citation.get("url"),
                "title": citation.get("title"),
                "content": citation.get("content"),
            }
        )
    return citations


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
