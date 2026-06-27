import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

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
        return self.session.scalar(
            select(ToolConnection).where(
                ToolConnection.domain_id == domain.id,
                ToolConnection.tool_key == tool_key,
                ToolConnection.is_active.is_(True),
            )
        )


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
        repo = _repo_from(context.connection, payload)
        if self.key == "github.repo.get":
            return self._repo_get(repo)
        if self.key == "github.issue.search":
            return self._issue_search(repo, payload)
        if self.key == "github.issue.get":
            return self._issue_get(repo, payload)
        raise ToolExecutionError(f"Unsupported GitHub tool: {self.key}")

    def _repo_get(self, repo: str) -> dict[str, Any]:
        return {
            "repo": repo,
            "result": _run_gh_json(
                [
                    "repo",
                    "view",
                    repo,
                    "--json",
                    "nameWithOwner,description,defaultBranchRef,url,isPrivate",
                ]
            ),
        }

    def _issue_search(self, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
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
            "issues": _run_gh_json(args),
        }

    def _issue_get(self, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
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
                ]
            ),
        }


def default_tool_adapters() -> dict[str, ToolAdapter]:
    return {
        key: GitHubCliToolAdapter(key)
        for key in ("github.repo.get", "github.issue.search", "github.issue.get")
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


def _run_gh_json(args: list[str]) -> Any:
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ToolExecutionError(
            (completed.stderr or completed.stdout or "GitHub CLI failed.").strip()
        )
    if not completed.stdout.strip():
        return None
    return json.loads(completed.stdout)


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
