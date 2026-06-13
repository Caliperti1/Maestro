import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import MemoryItem, MemoryProposal

MemoryScope = Literal["global", "maestro_session", "domain", "agent"]
ImpactLevel = Literal["low", "medium", "high", "very_high"]
WriteOutcome = Literal["written", "auto_approved", "pending_user_approval"]

AUTO_WRITE_IMPACTS: set[str] = {"low"}
AUTO_APPROVE_IMPACTS: set[str] = {"medium", "high"}
VALID_IMPACTS: set[str] = AUTO_WRITE_IMPACTS | AUTO_APPROVE_IMPACTS | {"very_high"}
VALID_SCOPES: set[str] = {"global", "maestro_session", "domain", "agent"}


@dataclass(frozen=True)
class MemoryCandidate:
    scope: MemoryScope
    memory_type: str
    title: str
    content: str
    domain_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    report_id: uuid.UUID | None = None
    rationale: str | None = None
    impact_level: ImpactLevel = "low"
    importance: float = 0.5
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryWriteResult:
    outcome: WriteOutcome
    memory_item: MemoryItem | None = None
    proposal: MemoryProposal | None = None


@dataclass(frozen=True)
class MemoryContext:
    memories: Sequence[MemoryItem]

    def by_scope(self, scope: MemoryScope) -> list[MemoryItem]:
        return [memory for memory in self.memories if memory.scope == scope]


class MemoryAccessError(ValueError):
    pass


class MemoryService:
    def __init__(self, session: Session):
        self.session = session

    def write_candidate(self, candidate: MemoryCandidate) -> MemoryWriteResult:
        self._validate_candidate(candidate)

        if candidate.impact_level in AUTO_WRITE_IMPACTS:
            memory_item = self._create_memory_item(candidate)
            self.session.commit()
            self.session.refresh(memory_item)
            return MemoryWriteResult(outcome="written", memory_item=memory_item)

        proposal_status = "approved"
        if candidate.impact_level not in AUTO_APPROVE_IMPACTS:
            proposal_status = "pending_user_approval"
        proposal = self._create_proposal(candidate, proposal_status)

        if candidate.impact_level == "very_high":
            self.session.commit()
            self.session.refresh(proposal)
            return MemoryWriteResult(outcome="pending_user_approval", proposal=proposal)

        memory_item = self._create_memory_item(candidate, proposal=proposal)
        proposal.reviewed_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(proposal)
        self.session.refresh(memory_item)
        return MemoryWriteResult(
            outcome="auto_approved",
            memory_item=memory_item,
            proposal=proposal,
        )

    def propose_memory(self, candidate: MemoryCandidate) -> MemoryProposal:
        self._validate_candidate(candidate)
        proposal = self._create_proposal(candidate, "proposed")
        self.session.commit()
        self.session.refresh(proposal)
        return proposal

    def approve_proposal(self, proposal_id: uuid.UUID) -> MemoryItem:
        proposal = self._get_proposal_or_raise(proposal_id)
        if proposal.status in {"approved", "applied"}:
            existing_memory = self.session.scalar(
                select(MemoryItem).where(MemoryItem.created_from_proposal_id == proposal.id)
            )
            if existing_memory is not None:
                return existing_memory

        if proposal.status == "rejected":
            raise MemoryAccessError("Rejected memory proposals cannot be approved.")

        candidate = self._candidate_from_proposal(proposal)
        memory_item = self._create_memory_item(candidate, proposal=proposal)
        proposal.status = "approved"
        proposal.reviewed_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(memory_item)
        self.session.refresh(proposal)
        return memory_item

    def reject_proposal(
        self,
        proposal_id: uuid.UUID,
        *,
        reason: str | None = None,
    ) -> MemoryProposal:
        proposal = self._get_proposal_or_raise(proposal_id)
        if proposal.status == "approved":
            raise MemoryAccessError("Approved memory proposals cannot be rejected.")

        metadata = dict(proposal.metadata_ or {})
        if reason:
            metadata["rejection_reason"] = reason
        proposal.metadata_ = metadata
        proposal.status = "rejected"
        proposal.reviewed_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(proposal)
        return proposal

    def list_proposals(
        self,
        *,
        status: str | None = None,
        impact_level: str | None = None,
        domain_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> Sequence[MemoryProposal]:
        query = select(MemoryProposal)
        if status is not None:
            query = query.where(MemoryProposal.status == status)
        if impact_level is not None:
            query = query.where(MemoryProposal.impact_level == impact_level)
        if domain_id is not None:
            query = query.where(MemoryProposal.domain_id == domain_id)
        if agent_id is not None:
            query = query.where(MemoryProposal.agent_id == agent_id)
        return self.session.scalars(
            query.order_by(MemoryProposal.created_at.desc()).limit(limit)
        ).all()

    def retrieve_for_agent(
        self,
        *,
        domain_id: uuid.UUID,
        agent_id: uuid.UUID | None = None,
        min_importance: float | None = None,
        memory_types: set[str] | None = None,
        limit: int = 100,
    ) -> MemoryContext:
        query = self._base_active_memory_query(
            min_importance=min_importance,
            memory_types=memory_types,
        )
        visibility = [
            MemoryItem.scope == "global",
            and_(MemoryItem.scope == "domain", MemoryItem.domain_id == domain_id),
        ]
        if agent_id is not None:
            visibility.append(
                and_(
                    MemoryItem.scope == "agent",
                    MemoryItem.domain_id == domain_id,
                    MemoryItem.agent_id == agent_id,
                )
            )
        query = query.where(or_(*visibility))
        return MemoryContext(self._ordered_memories(query, limit))

    def retrieve_for_maestro(
        self,
        *,
        domain_id: uuid.UUID | None = None,
        include_agent_memory: bool = False,
        include_session_memory: bool = True,
        min_importance: float | None = None,
        memory_types: set[str] | None = None,
        limit: int = 100,
    ) -> MemoryContext:
        query = self._base_active_memory_query(
            min_importance=min_importance,
            memory_types=memory_types,
        )
        scopes = ["global", "domain"]
        if include_session_memory:
            scopes.append("maestro_session")
        if include_agent_memory:
            scopes.append("agent")

        query = query.where(MemoryItem.scope.in_(scopes))
        if domain_id is not None:
            query = query.where(
                or_(
                    MemoryItem.scope.in_(["global", "maestro_session"]),
                    MemoryItem.domain_id == domain_id,
                )
            )
        return MemoryContext(self._ordered_memories(query, limit))

    def _base_active_memory_query(
        self,
        *,
        min_importance: float | None,
        memory_types: set[str] | None,
    ):
        now = datetime.now(UTC)
        query = select(MemoryItem).where(
            or_(MemoryItem.valid_from.is_(None), MemoryItem.valid_from <= now),
            or_(MemoryItem.valid_until.is_(None), MemoryItem.valid_until > now),
        )
        if min_importance is not None:
            query = query.where(MemoryItem.importance >= min_importance)
        if memory_types:
            query = query.where(MemoryItem.memory_type.in_(memory_types))
        return query

    def _ordered_memories(self, query, limit: int) -> Sequence[MemoryItem]:
        return self.session.scalars(
            query.order_by(MemoryItem.importance.desc(), MemoryItem.created_at.desc()).limit(limit)
        ).all()

    def _create_proposal(self, candidate: MemoryCandidate, status: str) -> MemoryProposal:
        proposal = MemoryProposal(
            domain_id=candidate.domain_id,
            agent_id=candidate.agent_id,
            task_id=candidate.task_id,
            report_id=candidate.report_id,
            scope=candidate.scope,
            memory_type=candidate.memory_type,
            title=candidate.title,
            content=candidate.content,
            rationale=candidate.rationale,
            impact_level=candidate.impact_level,
            status=status,
            source_refs=candidate.source_refs,
            metadata_=candidate.metadata,
        )
        self.session.add(proposal)
        self.session.flush()
        return proposal

    def _create_memory_item(
        self,
        candidate: MemoryCandidate,
        *,
        proposal: MemoryProposal | None = None,
    ) -> MemoryItem:
        metadata = dict(candidate.metadata)
        if candidate.source_refs:
            metadata.setdefault("source_refs", candidate.source_refs)

        memory_item = MemoryItem(
            domain_id=candidate.domain_id,
            agent_id=candidate.agent_id,
            created_from_proposal_id=proposal.id if proposal is not None else None,
            scope=candidate.scope,
            memory_type=candidate.memory_type,
            title=candidate.title,
            content=candidate.content,
            metadata_=metadata,
            importance=candidate.importance,
            impact_level=candidate.impact_level,
        )
        self.session.add(memory_item)
        self.session.flush()
        return memory_item

    def _candidate_from_proposal(self, proposal: MemoryProposal) -> MemoryCandidate:
        return MemoryCandidate(
            domain_id=proposal.domain_id,
            agent_id=proposal.agent_id,
            task_id=proposal.task_id,
            report_id=proposal.report_id,
            scope=proposal.scope,  # type: ignore[arg-type]
            memory_type=proposal.memory_type,
            title=proposal.title,
            content=proposal.content,
            rationale=proposal.rationale,
            impact_level=proposal.impact_level,  # type: ignore[arg-type]
            source_refs=proposal.source_refs,
            metadata=proposal.metadata_,
        )

    def _get_proposal_or_raise(self, proposal_id: uuid.UUID) -> MemoryProposal:
        proposal = self.session.get(MemoryProposal, proposal_id)
        if proposal is None:
            raise MemoryAccessError(f"Memory proposal {proposal_id} was not found.")
        return proposal

    def _validate_candidate(self, candidate: MemoryCandidate) -> None:
        if candidate.scope not in VALID_SCOPES:
            raise MemoryAccessError(f"Unsupported memory scope: {candidate.scope}")
        if candidate.impact_level not in VALID_IMPACTS:
            raise MemoryAccessError(f"Unsupported impact level: {candidate.impact_level}")
        if not candidate.title.strip():
            raise MemoryAccessError("Memory title is required.")
        if not candidate.content.strip():
            raise MemoryAccessError("Memory content is required.")
        if candidate.scope == "global" and (
            candidate.domain_id is not None or candidate.agent_id is not None
        ):
            raise MemoryAccessError("Global memory cannot be tied to a domain or agent.")
        if candidate.scope == "maestro_session" and candidate.agent_id is not None:
            raise MemoryAccessError("Maestro session memory cannot be tied to an agent.")
        if candidate.scope == "domain" and candidate.domain_id is None:
            raise MemoryAccessError("Domain memory requires a domain_id.")
        if candidate.scope == "agent" and (
            candidate.domain_id is None or candidate.agent_id is None
        ):
            raise MemoryAccessError("Agent memory requires both domain_id and agent_id.")
