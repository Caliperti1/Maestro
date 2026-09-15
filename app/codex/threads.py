"""Repository-scoped persistent Codex Steward and Worker threads."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from app.codex.app_server import CodexAppServerClient, CodexTurnResult
from app.db.models import RepositoryProfile

CodexThreadRole = Literal["steward", "worker"]


@dataclass(frozen=True)
class CodexThreadState:
    role: CodexThreadRole
    name: str
    session_id: str | None
    status: str
    last_used_at: str | None
    cwd: str | None


class CodexProjectThreadService:
    def __init__(
        self,
        session: Session,
        *,
        client: CodexAppServerClient | None = None,
    ):
        self.session = session
        self.client = client or CodexAppServerClient()

    def states(self, profile: RepositoryProfile) -> list[CodexThreadState]:
        metadata = self._metadata(profile)
        return [
            CodexThreadState(
                role=role,
                name=self.thread_name(profile, role),
                session_id=self.session_id(profile, role),
                status=str((metadata.get(role) or {}).get("status") or "not_initialized"),
                last_used_at=(metadata.get(role) or {}).get("last_used_at"),
                cwd=(metadata.get(role) or {}).get("cwd") or profile.local_path,
            )
            for role in ("steward", "worker")
        ]

    def payload(self, profile: RepositoryProfile) -> list[dict[str, object]]:
        return [asdict(state) for state in self.states(profile)]

    def run_turn(
        self,
        profile: RepositoryProfile,
        *,
        role: CodexThreadRole,
        cwd: str | Path,
        prompt: str,
        model: str | None,
        effort: str | None,
        sandbox: str,
        timeout_seconds: int,
    ) -> CodexTurnResult:
        if role not in {"steward", "worker"}:
            raise ValueError(f"Unsupported Codex project-thread role: {role}")
        result = self.client.run_turn(
            thread_id=self.session_id(profile, role),
            thread_name=self.thread_name(profile, role),
            cwd=cwd,
            prompt=prompt,
            model=model,
            effort=effort,
            sandbox=sandbox,  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,
        )
        self._set_session_id(profile, role, result.thread_id)
        metadata = self._metadata(profile)
        role_metadata = dict(metadata.get(role) or {})
        role_metadata.update(
            {
                "name": self.thread_name(profile, role),
                "status": "ready",
                "last_used_at": datetime.now(UTC).isoformat(),
                "cwd": str(Path(cwd).expanduser().resolve()),
                "model": model,
            }
        )
        if result.replaced_thread_id:
            history = list(role_metadata.get("replaced_session_ids") or [])
            if result.replaced_thread_id not in history:
                history.append(result.replaced_thread_id)
            role_metadata["replaced_session_ids"] = history[-10:]
        metadata[role] = role_metadata
        profile.metadata_ = {**(profile.metadata_ or {}), "codex_threads": metadata}
        self.session.commit()
        self.session.refresh(profile)
        return result

    def initialize(
        self,
        profile: RepositoryProfile,
        *,
        roles: tuple[CodexThreadRole, ...] = ("steward", "worker"),
        model: str = "gpt-5.6-luna",
    ) -> list[dict[str, object]]:
        if not profile.local_path:
            raise ValueError("Repository needs a local checkout before Codex threads can be initialized.")
        root = Path(profile.local_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Repository checkout does not exist: {root}")
        for role in roles:
            if self.session_id(profile, role):
                continue
            self.run_turn(
                profile,
                role=role,
                cwd=root,
                prompt=self._initial_prompt(profile, role),
                model=model,
                effort="low",
                sandbox="read-only",
                timeout_seconds=300,
            )
        return self.payload(profile)

    @staticmethod
    def thread_name(profile: RepositoryProfile, role: CodexThreadRole) -> str:
        return f"{profile.display_name} Maestro {role.title()}"

    @staticmethod
    def session_id(profile: RepositoryProfile, role: CodexThreadRole) -> str | None:
        return (
            profile.codex_steward_session_id
            if role == "steward"
            else profile.codex_worker_session_id
        )

    @staticmethod
    def _set_session_id(
        profile: RepositoryProfile,
        role: CodexThreadRole,
        session_id: str,
    ) -> None:
        if role == "steward":
            profile.codex_steward_session_id = session_id
        else:
            profile.codex_worker_session_id = session_id

    @staticmethod
    def _metadata(profile: RepositoryProfile) -> dict[str, dict[str, object]]:
        value = (profile.metadata_ or {}).get("codex_threads")
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _initial_prompt(profile: RepositoryProfile, role: CodexThreadRole) -> str:
        if role == "steward":
            return (
                f"You are the persistent Maestro project steward for {profile.display_name} "
                f"({profile.external_repo}). Maintain architectural and product-state continuity. "
                "Treat the repository, its instructions, current documentation, and current git state "
                "as authoritative. Do not edit files in this initialization turn. Confirm readiness "
                "in one short sentence."
            )
        return (
            f"You are the persistent Maestro coding worker for {profile.display_name} "
            f"({profile.external_repo}). Future turns will provide one scoped coding task and an "
            "isolated worktree. Re-read current repository state on every turn, keep unrelated work "
            "separate, and report changes and validation clearly. Do not edit files in this "
            "initialization turn. Confirm readiness in one short sentence."
        )
