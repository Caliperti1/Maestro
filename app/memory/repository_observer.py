"""Commit-aware local repository observer that produces evidence reports first."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Domain, SourceCheckpoint, SourceRegistration
from app.memory.context_gateway import ContextGatewayService, GatewayIngestResult, GatewayItem
from app.memory.ingestion import IngestionLedgerService, SourcePolicy


@dataclass(frozen=True)
class RepositoryObservationResult:
    status: str
    repository_key: str
    commit: str
    previous_commit: str | None
    mode: str
    changed_files: list[str]
    gateway: GatewayIngestResult | None


class RepositoryObserverService:
    def __init__(self, session: Session, *, gateway: ContextGatewayService | None = None):
        self.session = session
        self.gateway = gateway or ContextGatewayService(session)

    def register(self, *, key: str, path: str, domain: Domain, display_name: str | None = None) -> SourceRegistration:
        repository = Path(path).expanduser().resolve()
        if not (repository / ".git").exists():
            raise ValueError(f"Not a Git repository: {repository}")
        return IngestionLedgerService(self.session).ensure_registration(
            key=key,
            source_system="local_repository",
            display_name=display_name or repository.name,
            adapter_type="repository_observer",
            domain=domain,
            policy=SourcePolicy(sensitivity="business_confidential", trust_level="system_observed", transfer_method="local_repository_observer"),
            config={"path": str(repository)},
        )

    def observe(self, registration: SourceRegistration, *, force_full: bool = False) -> RepositoryObservationResult:
        if registration.adapter_type != "repository_observer":
            raise ValueError("Source is not a repository observer registration.")
        repository = Path(str((registration.config or {}).get("path") or "")).expanduser().resolve()
        domain = self.session.get(Domain, registration.domain_id) if registration.domain_id else None
        if domain is None:
            raise ValueError("Repository observer requires a domain.")
        head = _git(repository, "rev-parse", "HEAD").strip()
        checkpoint = self.session.scalar(select(SourceCheckpoint).where(SourceCheckpoint.source_registration_id == registration.id, SourceCheckpoint.cursor_key == "last_observed_commit"))
        previous = str((checkpoint.cursor_value or {}).get("commit") or "") if checkpoint else ""
        if previous == head and not force_full:
            return RepositoryObservationResult("unchanged", registration.key, head, previous, "incremental", [], None)
        mode = "full" if force_full or not previous else "incremental"
        changed = _git(repository, "ls-files").splitlines() if mode == "full" else _git(repository, "diff", "--name-only", f"{previous}..{head}").splitlines()
        report = self._report(repository, registration, head=head, previous=previous or None, mode=mode, changed=changed)
        gateway_result = self.gateway.ingest(
            GatewayItem(
                source_registration_key=registration.key,
                source_system="local_repository",
                external_id=str(repository),
                source_version=head,
                content_type="repository_state_report",
                domain_key=domain.key,
                title=f"{registration.display_name} Repository State Report",
                content=report,
                source_timestamp=datetime.now(UTC),
                policy=SourcePolicy(sensitivity="business_confidential", trust_level="system_observed", transfer_method="local_repository_observer"),
                metadata={"adapter_type": "repository_observer", "repository_path": str(repository), "commit": head, "previous_commit": previous or None, "mode": mode},
            ),
            domain=domain,
        )
        IngestionLedgerService(self.session).update_checkpoint(registration, cursor_key="last_observed_commit", cursor_value={"commit": head, "observed_at": datetime.now(UTC).isoformat()})
        return RepositoryObservationResult("staged", registration.key, head, previous or None, mode, changed, gateway_result)

    def _report(self, repository: Path, registration: SourceRegistration, *, head: str, previous: str | None, mode: str, changed: list[str]) -> str:
        readme = ""
        for candidate in ("README.md", "README.rst", "README"):
            path = repository / candidate
            if path.exists():
                readme = path.read_text(encoding="utf-8", errors="replace")[:8000]
                break
        commits = _git(repository, "log", "--oneline", "-n", "20" if mode == "full" else "10")
        relevant = [path for path in changed if path.startswith(("app/", "src/", "frontend/", "docs/", "tests/", "README", "pyproject", "package"))][:250]
        return "\n".join([
            f"# {registration.display_name} Repository State Report",
            "",
            f"Observed: {datetime.now(UTC).isoformat()}",
            f"Repository: {repository}",
            f"Commit: {head}",
            f"Previous commit: {previous or 'none (baseline)'}",
            f"Observation mode: {mode}",
            "",
            "## Changed or Baseline Files",
            *(f"- {path}" for path in relevant),
            "",
            "## Recent Commits",
            "```text",
            commits.strip(),
            "```",
            "",
            "## Repository Overview Evidence",
            readme or "No README was found.",
            "",
            "## Curator Instruction",
            "Treat this report as repository evidence. Update current product/architecture truth; preserve historical change in this report rather than as competing durable memories.",
        ])


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "Repository observation command failed.")
    return result.stdout
