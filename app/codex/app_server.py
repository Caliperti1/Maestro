"""Small JSON-RPC client for Codex's local app-server protocol.

Threads created through app-server are persisted in the same local Codex history that the desktop
app reads. Keeping this boundary isolated makes the coding adapter independent of protocol details.
"""

from __future__ import annotations

import json
import selectors
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal

CodexSandbox = Literal["read-only", "workspace-write", "danger-full-access"]


class CodexAppServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexTurnResult:
    thread_id: str
    final_message: str
    status: str
    event_counts: dict[str, int]
    replaced_thread_id: str | None = None


class CodexAppServerClient:
    """Create or resume one Codex-visible thread and execute a turn."""

    def __init__(self, *, codex_bin: str = "codex"):
        self.codex_bin = codex_bin

    def run_turn(
        self,
        *,
        thread_id: str | None,
        thread_name: str,
        cwd: str | Path,
        prompt: str,
        model: str | None = None,
        effort: str | None = None,
        sandbox: CodexSandbox = "workspace-write",
        timeout_seconds: int = 900,
    ) -> CodexTurnResult:
        codex_bin = shutil.which(self.codex_bin) or (
            self.codex_bin if Path(self.codex_bin).expanduser().exists() else None
        )
        if codex_bin is None:
            raise CodexAppServerError("Codex CLI is not installed or not on PATH.")
        root = Path(cwd).expanduser().resolve()
        if not root.is_dir():
            raise CodexAppServerError(f"Codex thread working directory does not exist: {root}")

        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                [str(codex_bin), "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,
            )
            deadline = monotonic() + timeout_seconds
            next_id = 1
            counts: Counter[str] = Counter()
            messages: list[str] = []
            pending_messages: list[dict[str, Any]] = []
            replaced_thread_id: str | None = None
            active_thread_id = thread_id
            selector = selectors.DefaultSelector()
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)

            def send(method: str, params: dict[str, Any], *, notification: bool = False) -> int | None:
                nonlocal next_id
                request_id = None if notification else next_id
                if request_id is not None:
                    next_id += 1
                message: dict[str, Any] = {"method": method, "params": params}
                if request_id is not None:
                    message["id"] = request_id
                assert process.stdin is not None
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
                return request_id

            def read_message() -> dict[str, Any]:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise CodexAppServerError(
                        f"Codex app-server turn timed out after {timeout_seconds} seconds."
                    )
                ready = selector.select(timeout=remaining)
                if not ready:
                    raise CodexAppServerError(
                        f"Codex app-server turn timed out after {timeout_seconds} seconds."
                    )
                line = process.stdout.readline()
                if not line:
                    raise CodexAppServerError(
                        self._stderr(stderr_file)
                        or f"Codex app-server exited with status {process.poll()}."
                    )
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CodexAppServerError(f"Codex app-server returned invalid JSON: {line[:240]}") from exc
                method = str(message.get("method") or "")
                if method:
                    counts[method] += 1
                return message

            def await_response(request_id: int | None) -> dict[str, Any]:
                if request_id is None:
                    raise CodexAppServerError("A request ID is required for a response.")
                while True:
                    message = read_message()
                    if message.get("id") == request_id:
                        return message
                    pending_messages.append(message)

            try:
                response = await_response(
                    send(
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "maestro",
                                "title": "Maestro Codex Thread Manager",
                                "version": "1.0.0",
                            },
                            "capabilities": {"experimentalApi": True},
                        },
                    )
                )
                self._raise_response_error(response, "initialize")
                send("initialized", {}, notification=True)

                if active_thread_id:
                    resume = await_response(
                        send(
                            "thread/resume",
                            {
                                "threadId": active_thread_id,
                                "cwd": str(root),
                                "model": model,
                                "sandbox": sandbox,
                                "approvalPolicy": "never",
                                "excludeTurns": True,
                            },
                        )
                    )
                    if resume.get("error"):
                        replaced_thread_id = active_thread_id
                        active_thread_id = None

                if not active_thread_id:
                    start = await_response(
                        send(
                            "thread/start",
                            {
                                "cwd": str(root),
                                "model": model,
                                "sandbox": sandbox,
                                "approvalPolicy": "never",
                                "threadSource": "maestro",
                            },
                        )
                    )
                    self._raise_response_error(start, "thread/start")
                    active_thread_id = str(start["result"]["thread"]["id"])

                named = await_response(
                    send(
                        "thread/name/set",
                        {"threadId": active_thread_id, "name": thread_name},
                    )
                )
                self._raise_response_error(named, "thread/name/set")

                turn_params: dict[str, Any] = {
                    "threadId": active_thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "cwd": str(root),
                    "approvalPolicy": "never",
                    "sandboxPolicy": self._sandbox_policy(sandbox),
                    "summary": "none",
                    "turnTrigger": "maestro",
                }
                if model:
                    turn_params["model"] = model
                if effort:
                    turn_params["effort"] = effort
                turn = await_response(send("turn/start", turn_params))
                self._raise_response_error(turn, "turn/start")

                status = "running"
                turn_started = False
                while True:
                    message = pending_messages.pop(0) if pending_messages else read_message()
                    method = str(message.get("method") or "")
                    if method == "turn/started":
                        turn_started = True
                    elif method == "item/completed":
                        item = (message.get("params") or {}).get("item") or {}
                        if item.get("type") in {"agentMessage", "agent_message"}:
                            text = str(item.get("text") or item.get("message") or "").strip()
                            if text:
                                messages.append(text)
                    elif method == "turn/completed":
                        turn_payload = (message.get("params") or {}).get("turn") or {}
                        status_value = turn_payload.get("status")
                        status = (
                            str(status_value.get("type") or "unknown")
                            if isinstance(status_value, dict)
                            else str(status_value or "unknown")
                        )
                        error = turn_payload.get("error")
                        if status not in {"complete", "completed"}:
                            raise CodexAppServerError(
                                self._error_text(error)
                                or f"Codex turn ended with status {status}."
                            )
                        break
                    elif method == "thread/status/changed" and turn_started:
                        thread_status = (message.get("params") or {}).get("status")
                        status_type = (
                            str(thread_status.get("type") or "")
                            if isinstance(thread_status, dict)
                            else str(thread_status or "")
                        )
                        # Resumed app-server threads currently return to idle without emitting
                        # turn/completed. The completed agent item is persisted before this event.
                        if status_type == "idle" and messages:
                            status = "completed"
                            break
                        if status_type == "systemError":
                            raise CodexAppServerError("Codex thread entered a system error state.")
                    elif message.get("id") is not None and method:
                        raise CodexAppServerError(
                            f"Codex requested unsupported interactive input through {method}."
                        )

                return CodexTurnResult(
                    thread_id=active_thread_id,
                    final_message=messages[-1] if messages else "",
                    status=status,
                    event_counts=dict(counts),
                    replaced_thread_id=replaced_thread_id,
                )
            finally:
                selector.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    @staticmethod
    def _sandbox_policy(sandbox: CodexSandbox) -> dict[str, Any]:
        if sandbox == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if sandbox == "read-only":
            return {"type": "readOnly", "networkAccess": False}
        return {
            "type": "workspaceWrite",
            "writableRoots": [],
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }

    @staticmethod
    def _raise_response_error(response: dict[str, Any], method: str) -> None:
        if response.get("error"):
            raise CodexAppServerError(
                CodexAppServerClient._error_text(response["error"])
                or f"Codex app-server {method} failed."
            )

    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "").strip()
        return str(error or "").strip()

    @staticmethod
    def _stderr(stderr_file: Any) -> str:
        try:
            stderr_file.seek(0)
            return str(stderr_file.read() or "").strip()[-4000:]
        except OSError:
            return ""
