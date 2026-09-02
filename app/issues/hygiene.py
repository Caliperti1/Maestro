"""Conservative recurring reconciliation for canonical product issues."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ProductIssue, ProductIssueRelation, RepositoryProfile
from app.issues.service import LLMIssueMatcher, merge_text
from app.llm.client import LLMClientError


@dataclass(frozen=True)
class IssueHygieneResult:
    scanned: int
    semantic_checks: int
    merged: int
    related: int
    conflicts: int
    skipped: int


class ProductIssueHygieneService:
    """Merges only high-confidence duplicates and records all lesser relationships."""

    def __init__(self, session: Session):
        self.session = session

    def run(
        self,
        profile: RepositoryProfile,
        *,
        max_semantic_checks: int = 8,
    ) -> IssueHygieneResult:
        issues = list(self.session.scalars(select(ProductIssue).where(ProductIssue.repository_id == profile.id, ProductIssue.status.notin_(["cancelled", "superseded"]))).all())
        existing_relations = self.session.scalars(
            select(ProductIssueRelation).where(
                ProductIssueRelation.source_issue_id.in_([issue.id for issue in issues])
            )
        ).all()
        resolved_pairs = {
            frozenset((relation.source_issue_id, relation.target_issue_id))
            for relation in existing_relations
        }
        semantic_checks = 0
        merged = related = conflicts = skipped = 0
        for index, issue in enumerate(issues):
            if issue.status == "superseded":
                continue
            candidates = [
                candidate
                for candidate in issues[index + 1:]
                if candidate.status != "superseded"
                and frozenset((issue.id, candidate.id)) not in resolved_pairs
                and _plausible(issue, candidate)
            ]
            if not candidates:
                continue
            if semantic_checks >= max_semantic_checks:
                skipped += len(candidates)
                continue
            semantic_checks += 1
            try:
                matcher = LLMIssueMatcher()
                if hasattr(matcher, "session"):
                    matcher.session = self.session
                decision = matcher.resolve(
                    proposed={
                        "title": issue.title, "problem": issue.problem,
                        "desired_outcome": issue.desired_outcome,
                        "acceptance_criteria": issue.acceptance_criteria, "notes": issue.notes,
                    },
                    candidates=candidates,
                )
            except (LLMClientError, OSError, ValueError, TypeError):
                skipped += len(candidates)
                continue
            target = next((candidate for candidate in candidates if str(candidate.id) == decision.candidate_id), None)
            if target is None:
                continue
            if decision.action == "merge" and decision.confidence >= 0.9 and not (issue.external_number and target.external_number):
                keeper, duplicate = _keeper(issue, target)
                keeper.problem = merge_text(keeper.problem, duplicate.problem)
                keeper.desired_outcome = merge_text(keeper.desired_outcome, duplicate.desired_outcome)
                keeper.notes = merge_text(keeper.notes, duplicate.notes)
                keeper.acceptance_criteria = list(dict.fromkeys([*(keeper.acceptance_criteria or []), *(duplicate.acceptance_criteria or [])]))
                keeper.source_refs = [*(keeper.source_refs or []), *(duplicate.source_refs or [])]
                duplicate.status = "superseded"
                self._relate(duplicate, keeper, "duplicate_of", decision.rationale, decision.confidence)
                merged += 1
            elif decision.action in {"relate", "conflict", "supersede"} and decision.confidence >= 0.72:
                relation_type = decision.relation_type or "related_to"
                self._relate(issue, target, relation_type, decision.rationale, decision.confidence)
                resolved_pairs.add(frozenset((issue.id, target.id)))
                conflicts += int(relation_type == "conflicts_with")
                related += int(relation_type != "conflicts_with")
        self.session.commit()
        return IssueHygieneResult(
            len(issues), semantic_checks, merged, related, conflicts, skipped
        )

    def _relate(self, source: ProductIssue, target: ProductIssue, relation_type: str, rationale: str, confidence: float) -> None:
        existing = self.session.scalar(select(ProductIssueRelation).where(ProductIssueRelation.source_issue_id == source.id, ProductIssueRelation.target_issue_id == target.id, ProductIssueRelation.relation_type == relation_type))
        if existing is None:
            self.session.add(ProductIssueRelation(source_issue_id=source.id, target_issue_id=target.id, relation_type=relation_type, rationale=rationale, confidence=confidence, provenance={"source_system": "issue_hygiene"}))


def _plausible(left: ProductIssue, right: ProductIssue) -> bool:
    left_terms = set(left.normalized_title.split())
    right_terms = set(right.normalized_title.split())
    return len(left_terms & right_terms) / max(1, len(left_terms | right_terms)) >= 0.25


def _keeper(left: ProductIssue, right: ProductIssue) -> tuple[ProductIssue, ProductIssue]:
    if left.external_number and not right.external_number:
        return left, right
    if right.external_number and not left.external_number:
        return right, left
    return (left, right) if left.created_at <= right.created_at else (right, left)
