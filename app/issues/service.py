"""Canonical product issue capture, reconciliation, retrieval, and editing."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    Domain,
    ProductIssue,
    ProductIssueRelation,
    ProductProject,
    RepositoryProfile,
)
from app.llm.client import LLMClientError, OpenAILLMClient

logger = logging.getLogger(__name__)

ISSUE_STATUSES = {
    "ready", "active", "blocked", "review", "completed", "cancelled", "superseded",
}
ISSUE_TYPES = {"idea", "bug", "feature", "story", "chore", "research", "architecture"}
RELATION_TYPES = {
    "duplicate_of", "related_to", "blocks", "blocked_by", "conflicts_with",
    "supersedes", "implemented_by",
}


@dataclass(frozen=True)
class IssueCaptureResult:
    status: str
    issue: ProductIssue | None
    message: str
    clarification: str | None = None
    matched_issue_id: str | None = None
    confidence: float = 0.0


@dataclass(frozen=True)
class IssueMatchDecision:
    action: str
    candidate_id: str | None = None
    relation_type: str | None = None
    rationale: str = ""
    confidence: float = 0.0


class IssueMatcher(Protocol):
    def resolve(
        self,
        *,
        proposed: dict[str, Any],
        candidates: list[ProductIssue],
    ) -> IssueMatchDecision: ...


class LLMIssueMatcher:
    """Adjudicates only plausible candidates; deterministic identity checks run first."""

    def __init__(self, client: OpenAILLMClient | None = None):
        self.client = client or OpenAILLMClient()

    def resolve(
        self,
        *,
        proposed: dict[str, Any],
        candidates: list[ProductIssue],
    ) -> IssueMatchDecision:
        if not candidates:
            return IssueMatchDecision("create", confidence=1.0)
        payload = self.client.structured_response(
            instructions=(
                "You reconcile product issues. Decide whether the proposed issue is the same work "
                "as one candidate, a distinct but related issue, a contradiction/superseding change, "
                "or new work. Never merge merely because two issues share a broad product area. "
                "Use merge only when one canonical issue can preserve the intent and acceptance criteria "
                "of both. Return candidate_id only from the provided candidates."
            ),
            input_text=json.dumps(
                {
                    "proposed": proposed,
                    "candidates": [issue_payload(item, compact=True) for item in candidates],
                },
                default=str,
            ),
            schema_name="product_issue_match",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "merge", "relate", "conflict", "supersede"],
                    },
                    "candidate_id": {"type": ["string", "null"]},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["action", "candidate_id", "rationale", "confidence"],
            },
        )
        action = str(payload.get("action") or "create")
        relation = {
            "relate": "related_to",
            "conflict": "conflicts_with",
            "supersede": "supersedes",
        }.get(action)
        return IssueMatchDecision(
            action=action,
            candidate_id=str(payload.get("candidate_id") or "") or None,
            relation_type=relation,
            rationale=str(payload.get("rationale") or ""),
            confidence=float(payload.get("confidence") or 0),
        )


class ProductIssueService:
    def __init__(self, session: Session, *, matcher: IssueMatcher | None = None):
        self.session = session
        self.matcher = matcher

    def capture(
        self,
        *,
        domain_key: str | None,
        project_key: str | None,
        title: str | None,
        problem: str = "",
        desired_outcome: str = "",
        acceptance_criteria: list[str] | None = None,
        notes: str = "",
        repository_key: str | None = None,
        issue_type: str = "feature",
        priority: str = "normal",
        source_refs: list[dict[str, Any]] | None = None,
        provenance: dict[str, Any] | None = None,
        agent_task: bool = False,
        use_semantic_match: bool = True,
    ) -> IssueCaptureResult:
        clarification = self._clarification(
            domain_key=domain_key,
            project_key=project_key,
            title=title,
            problem=problem,
            desired_outcome=desired_outcome,
            notes=notes,
        )
        if clarification:
            return IssueCaptureResult("needs_clarification", None, clarification, clarification)

        domain = self._domain(str(domain_key))
        project = self._project(domain.id, str(project_key))
        repository = self._repository(repository_key, project.id)
        normalized = normalize_issue_title(str(title))
        proposed = {
            "title": str(title).strip(),
            "problem": problem.strip(),
            "desired_outcome": desired_outcome.strip(),
            "acceptance_criteria": clean_list(acceptance_criteria or []),
            "notes": notes.strip(),
            "issue_type": issue_type if issue_type in ISSUE_TYPES else "feature",
            "project_key": project.key,
            "repository_key": repository.key if repository else None,
        }
        candidates = self.find_candidates(
            project_id=project.id,
            repository_id=repository.id if repository else None,
            text=" ".join(filter(None, (str(title), problem, desired_outcome, notes))),
        )
        exact = next((item for item in candidates if item.normalized_title == normalized), None)
        if exact is not None:
            self._merge_into(exact, proposed, source_refs or [], provenance or {}, "Exact normalized title match")
            self.session.commit()
            return IssueCaptureResult(
                "merged", exact, f"Updated existing issue '{exact.title}'.", matched_issue_id=str(exact.id), confidence=1.0
            )

        decision = self._decide(proposed, candidates, use_semantic_match=use_semantic_match)
        matched = next(
            (item for item in candidates if str(item.id) == decision.candidate_id),
            None,
        )
        if decision.action == "merge" and matched is not None and decision.confidence >= 0.78:
            self._merge_into(matched, proposed, source_refs or [], provenance or {}, decision.rationale)
            self.session.commit()
            return IssueCaptureResult(
                "merged", matched, f"Reconciled this with '{matched.title}'.",
                matched_issue_id=str(matched.id), confidence=decision.confidence,
            )

        issue = ProductIssue(
            domain_id=domain.id,
            project_id=project.id,
            repository_id=repository.id if repository else None,
            issue_type=proposed["issue_type"],
            title=proposed["title"],
            normalized_title=normalized,
            problem=proposed["problem"],
            desired_outcome=proposed["desired_outcome"],
            acceptance_criteria=proposed["acceptance_criteria"],
            notes=proposed["notes"],
            priority=priority,
            status="ready",
            agent_task=agent_task,
            agent_task_status="pending" if agent_task else "not_agent",
            sync_status="pending_push" if repository else "local",
            source_refs=source_refs or [],
            provenance=provenance or {"source_system": "maestro_chat", "captured_at": datetime.now(UTC).isoformat()},
            metadata_={"capture_reconciliation": {"action": decision.action, "rationale": decision.rationale, "confidence": decision.confidence}},
        )
        self.session.add(issue)
        self.session.flush()
        if matched is not None and decision.relation_type in RELATION_TYPES:
            self.session.add(ProductIssueRelation(
                source_issue_id=issue.id,
                target_issue_id=matched.id,
                relation_type=decision.relation_type,
                rationale=decision.rationale,
                confidence=decision.confidence,
                provenance=provenance or {},
            ))
            if decision.action == "supersede" and decision.confidence >= 0.85:
                matched.status = "superseded"
        self.session.commit()
        self.session.refresh(issue)
        return IssueCaptureResult("created", issue, f"Created issue '{issue.title}'.", confidence=decision.confidence)

    def find_candidates(
        self,
        *,
        project_id: uuid.UUID,
        repository_id: uuid.UUID | None,
        text: str,
        limit: int = 8,
    ) -> list[ProductIssue]:
        items = self.session.scalars(
            select(ProductIssue)
            .where(
                ProductIssue.project_id == project_id,
                ProductIssue.status.notin_(["cancelled"]),
            )
            .order_by(ProductIssue.updated_at.desc())
        ).all()
        terms = set(tokens(text))
        scored: list[tuple[float, ProductIssue]] = []
        for item in items:
            item_terms = set(tokens(f"{item.title} {item.problem} {item.desired_outcome} {item.notes}"))
            overlap = len(terms & item_terms) / max(1, len(terms | item_terms))
            repo_bonus = 0.08 if repository_id and item.repository_id == repository_id else 0.0
            if overlap >= 0.1 or item.normalized_title == normalize_issue_title(text):
                scored.append((overlap + repo_bonus, item))
        return [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]

    def search(
        self,
        *,
        query: str = "",
        domain_key: str | None = None,
        project_key: str | None = None,
        repository_key: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ProductIssue]:
        statement = select(ProductIssue).order_by(ProductIssue.updated_at.desc())
        if domain_key:
            statement = statement.join(Domain, Domain.id == ProductIssue.domain_id).where(Domain.key == domain_key)
        if project_key:
            statement = statement.join(ProductProject, ProductProject.id == ProductIssue.project_id).where(ProductProject.key == project_key)
        if repository_key:
            statement = statement.join(RepositoryProfile, RepositoryProfile.id == ProductIssue.repository_id).where(RepositoryProfile.key == repository_key)
        if status:
            statement = statement.where(ProductIssue.status == status)
        if query.strip():
            term = f"%{query.strip()}%"
            statement = statement.where(or_(ProductIssue.title.ilike(term), ProductIssue.problem.ilike(term), ProductIssue.desired_outcome.ilike(term), ProductIssue.notes.ilike(term)))
        return list(self.session.scalars(statement.limit(max(1, min(limit, 250)))).all())

    def update(self, issue_id: uuid.UUID, updates: dict[str, Any]) -> ProductIssue:
        issue = self.session.get(ProductIssue, issue_id)
        if issue is None:
            raise ValueError("Product issue not found.")
        allowed = {
            "title", "problem", "desired_outcome", "acceptance_criteria", "notes",
            "issue_type", "priority", "estimated_minutes", "status", "assignee_type",
            "assignee_ref", "agent_task", "repository_id",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unsupported issue fields: {', '.join(sorted(unknown))}.")
        for key, value in updates.items():
            if key == "status" and value not in ISSUE_STATUSES:
                raise ValueError(f"Unsupported issue status: {value}.")
            if key == "issue_type" and value not in ISSUE_TYPES:
                raise ValueError(f"Unsupported issue type: {value}.")
            if key == "acceptance_criteria":
                value = clean_list(value if isinstance(value, list) else [])
            setattr(issue, key, value)
        if "title" in updates:
            issue.normalized_title = normalize_issue_title(str(issue.title))
        if "agent_task" in updates:
            issue.agent_task_status = "pending" if issue.agent_task else "not_agent"
        if issue.external_number is not None:
            issue.sync_status = "pending_push"
        self.session.commit()
        self.session.refresh(issue)
        return issue

    def relate(self, source_id: uuid.UUID, target_id: uuid.UUID, relation_type: str, *, rationale: str = "", confidence: float = 1.0) -> ProductIssueRelation:
        if relation_type not in RELATION_TYPES:
            raise ValueError(f"Unsupported issue relation: {relation_type}.")
        if source_id == target_id:
            raise ValueError("An issue cannot relate to itself.")
        existing = self.session.scalar(select(ProductIssueRelation).where(ProductIssueRelation.source_issue_id == source_id, ProductIssueRelation.target_issue_id == target_id, ProductIssueRelation.relation_type == relation_type))
        if existing:
            existing.rationale = rationale or existing.rationale
            existing.confidence = max(existing.confidence, confidence)
            relation = existing
        else:
            relation = ProductIssueRelation(source_issue_id=source_id, target_issue_id=target_id, relation_type=relation_type, rationale=rationale, confidence=confidence, provenance={"source_system": "maestro"})
            self.session.add(relation)
        self.session.commit()
        self.session.refresh(relation)
        return relation

    def _decide(self, proposed: dict[str, Any], candidates: list[ProductIssue], *, use_semantic_match: bool) -> IssueMatchDecision:
        if not candidates:
            return IssueMatchDecision("create", confidence=1.0)
        best = candidates[0]
        score = token_similarity(proposed["title"], best.title)
        if score >= 0.82:
            return IssueMatchDecision("merge", str(best.id), rationale="Strong title and scope overlap.", confidence=score)
        if use_semantic_match:
            matcher = self.matcher or LLMIssueMatcher()
            try:
                return matcher.resolve(proposed=proposed, candidates=candidates)
            except (LLMClientError, OSError, ValueError, TypeError) as exc:
                logger.info("Issue semantic reconciliation unavailable; using local fallback: %s", exc)
        if score >= 0.42:
            return IssueMatchDecision("relate", str(best.id), "related_to", "Meaningful lexical overlap; preserved as separate work.", score)
        return IssueMatchDecision("create", confidence=max(0.5, 1 - score))

    def _merge_into(self, issue: ProductIssue, proposed: dict[str, Any], source_refs: list[dict[str, Any]], provenance: dict[str, Any], rationale: str) -> None:
        issue.problem = merge_text(issue.problem, proposed["problem"])
        issue.desired_outcome = merge_text(issue.desired_outcome, proposed["desired_outcome"])
        issue.notes = merge_text(issue.notes, proposed["notes"])
        issue.acceptance_criteria = clean_list([*(issue.acceptance_criteria or []), *proposed["acceptance_criteria"]])
        issue.source_refs = [*(issue.source_refs or []), *source_refs]
        submissions = list((issue.metadata_ or {}).get("merged_submissions") or [])
        submissions.append({"captured_at": datetime.now(UTC).isoformat(), "proposed": proposed, "provenance": provenance, "rationale": rationale})
        issue.metadata_ = {**(issue.metadata_ or {}), "merged_submissions": submissions[-20:]}
        if issue.external_number is not None:
            issue.sync_status = "pending_push"

    def _clarification(self, **values: Any) -> str | None:
        missing: list[str] = []
        if not str(values.get("domain_key") or "").strip():
            missing.append("which domain owns it")
        if not str(values.get("project_key") or "").strip():
            missing.append("which project it belongs to")
        if not str(values.get("title") or "").strip():
            missing.append("a short issue title")
        if not any(str(values.get(key) or "").strip() for key in ("problem", "desired_outcome", "notes")):
            missing.append("the problem or desired behavior")
        if not missing:
            return None
        return "Before I save this as a canonical issue, please clarify " + ", ".join(missing) + "."

    def _domain(self, key: str) -> Domain:
        domain = self.session.scalar(select(Domain).where(Domain.key == key.strip().lower()))
        if domain is None:
            raise ValueError(f"Unknown domain: {key}.")
        return domain

    def _project(self, domain_id: uuid.UUID, key: str) -> ProductProject:
        project = self.session.scalar(select(ProductProject).where(ProductProject.domain_id == domain_id, ProductProject.key == slug(key)))
        if project is None:
            project = ProductProject(domain_id=domain_id, key=slug(key), name=display_name(key), provenance={"source_system": "maestro_capture"})
            self.session.add(project)
            self.session.flush()
        return project

    def _repository(self, key: str | None, project_id: uuid.UUID) -> RepositoryProfile | None:
        if not key:
            return None
        repository = self.session.scalar(select(RepositoryProfile).where(RepositoryProfile.project_id == project_id, or_(RepositoryProfile.key == key, RepositoryProfile.external_repo == key)))
        if repository is None:
            raise ValueError(f"Unknown repository: {key}.")
        return repository


def issue_payload(issue: ProductIssue, *, compact: bool = False) -> dict[str, Any]:
    payload = {
        "id": str(issue.id), "domain_id": str(issue.domain_id), "project_id": str(issue.project_id),
        "repository_id": str(issue.repository_id) if issue.repository_id else None,
        "issue_type": issue.issue_type, "title": issue.title, "problem": issue.problem,
        "desired_outcome": issue.desired_outcome, "acceptance_criteria": issue.acceptance_criteria,
        "notes": issue.notes, "priority": issue.priority, "estimated_minutes": issue.estimated_minutes,
        "status": issue.status, "assignee_type": issue.assignee_type, "assignee_ref": issue.assignee_ref,
        "agent_task": issue.agent_task, "agent_task_status": issue.agent_task_status,
        "external_provider": issue.external_provider, "external_repo": issue.external_repo,
        "external_number": issue.external_number, "external_url": issue.external_url,
        "external_state": issue.external_state, "sync_status": issue.sync_status,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
    }
    if compact:
        return {key: payload[key] for key in ("id", "title", "problem", "desired_outcome", "acceptance_criteria", "status", "external_number")}
    payload.update({"source_refs": issue.source_refs, "provenance": issue.provenance, "metadata": issue.metadata_})
    return payload


def normalize_issue_title(value: str) -> str:
    return " ".join(tokens(value))


def tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def token_similarity(left: str, right: str) -> float:
    a, b = set(tokens(left)), set(tokens(right))
    return len(a & b) / max(1, len(a | b))


def clean_list(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def merge_text(current: str, incoming: str) -> str:
    current, incoming = current.strip(), incoming.strip()
    if not incoming or incoming.lower() in current.lower():
        return current
    if not current:
        return incoming
    return f"{current}\n\n{incoming}"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def display_name(value: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_\s]+", value.strip()) if part)
