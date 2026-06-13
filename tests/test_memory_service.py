from sqlalchemy.orm import Session

from app.db.models import MemoryItem
from app.db.repositories import AgentRepository, DomainRepository
from app.db.seed import seed_default_domains
from app.memory import MemoryAccessError, MemoryCandidate, MemoryService


def _domain_and_agents(session: Session):
    seed_default_domains(session)
    domain_repo = DomainRepository(session)
    praxis = domain_repo.get_by_key("praxis")
    ophi = domain_repo.get_by_key("ophi")
    assert praxis is not None
    assert ophi is not None

    agent_repo = AgentRepository(session)
    praxis_agent = agent_repo.create(
        domain_id=praxis.id,
        key="praxis-memory-test-agent",
        name="Praxis Memory Test Agent",
        agent_type="domain_agent",
    )
    ophi_agent = agent_repo.create(
        domain_id=ophi.id,
        key="ophi-memory-test-agent",
        name="Ophi Memory Test Agent",
        agent_type="domain_agent",
    )
    return praxis, ophi, praxis_agent, ophi_agent


def test_low_impact_candidate_writes_canonical_memory_directly(session: Session) -> None:
    praxis, _, _, _ = _domain_and_agents(session)
    service = MemoryService(session)

    result = service.write_candidate(
        MemoryCandidate(
            domain_id=praxis.id,
            scope="domain",
            memory_type="preference",
            title="Standup preference",
            content="Chris wants thin daily standup output first.",
            impact_level="low",
            source_refs=[{"type": "message", "id": "m-1"}],
        )
    )

    assert result.outcome == "written"
    assert result.memory_item is not None
    assert result.proposal is None
    assert result.memory_item.scope == "domain"
    assert result.memory_item.metadata_["source_refs"] == [{"type": "message", "id": "m-1"}]


def test_medium_and_high_impact_candidates_are_auto_approved_with_audit(
    session: Session,
) -> None:
    praxis, _, _, _ = _domain_and_agents(session)
    service = MemoryService(session)

    result = service.write_candidate(
        MemoryCandidate(
            domain_id=praxis.id,
            scope="domain",
            memory_type="decision",
            title="Use Postgres",
            content="Maestro uses Postgres as the local memory database.",
            rationale="Memory and provenance are core concerns.",
            impact_level="high",
            importance=0.9,
        )
    )

    assert result.outcome == "auto_approved"
    assert result.proposal is not None
    assert result.proposal.status == "approved"
    assert result.proposal.reviewed_at is not None
    assert result.memory_item is not None
    assert result.memory_item.created_from_proposal_id == result.proposal.id
    assert result.memory_item.importance == 0.9


def test_very_high_impact_candidate_waits_for_user_approval(session: Session) -> None:
    praxis, _, _, _ = _domain_and_agents(session)
    service = MemoryService(session)

    result = service.write_candidate(
        MemoryCandidate(
            domain_id=praxis.id,
            scope="domain",
            memory_type="standing_instruction",
            title="Change approval policy",
            content="Allow autonomous high-impact external writes.",
            impact_level="very_high",
        )
    )

    assert result.outcome == "pending_user_approval"
    assert result.memory_item is None
    assert result.proposal is not None
    assert result.proposal.status == "pending_user_approval"
    assert session.query(MemoryItem).count() == 0

    memory_item = service.approve_proposal(result.proposal.id)

    assert memory_item.created_from_proposal_id == result.proposal.id
    assert memory_item.title == "Change approval policy"


def test_rejected_proposal_remains_auditable_and_does_not_write_memory(session: Session) -> None:
    praxis, _, _, _ = _domain_and_agents(session)
    service = MemoryService(session)
    proposal = service.propose_memory(
        MemoryCandidate(
            domain_id=praxis.id,
            scope="domain",
            memory_type="fact",
            title="Unverified fact",
            content="This source may not be reliable.",
            impact_level="medium",
        )
    )

    rejected = service.reject_proposal(proposal.id, reason="Source was not reliable.")

    assert rejected.status == "rejected"
    assert rejected.reviewed_at is not None
    assert rejected.metadata_["rejection_reason"] == "Source was not reliable."
    assert session.query(MemoryItem).count() == 0


def test_agent_retrieval_is_limited_to_global_own_domain_and_own_agent_memory(
    session: Session,
) -> None:
    praxis, ophi, praxis_agent, ophi_agent = _domain_and_agents(session)
    service = MemoryService(session)

    global_memory = service.write_candidate(
        MemoryCandidate(
            scope="global",
            memory_type="preference",
            title="Global preference",
            content="Prefer concise reports.",
            impact_level="low",
            importance=0.4,
        )
    ).memory_item
    praxis_domain_memory = service.write_candidate(
        MemoryCandidate(
            domain_id=praxis.id,
            scope="domain",
            memory_type="decision",
            title="Praxis domain decision",
            content="Praxis has its own operating priorities.",
            impact_level="low",
            importance=0.8,
        )
    ).memory_item
    praxis_agent_memory = service.write_candidate(
        MemoryCandidate(
            domain_id=praxis.id,
            agent_id=praxis_agent.id,
            scope="agent",
            memory_type="standing_instruction",
            title="Praxis agent instruction",
            content="Praxis agent should inspect GitHub first.",
            impact_level="low",
            importance=0.9,
        )
    ).memory_item
    service.write_candidate(
        MemoryCandidate(
            domain_id=ophi.id,
            agent_id=ophi_agent.id,
            scope="agent",
            memory_type="standing_instruction",
            title="Ophi agent instruction",
            content="Ophi agent should inspect product notes first.",
            impact_level="low",
        )
    )

    context = service.retrieve_for_agent(domain_id=praxis.id, agent_id=praxis_agent.id)

    assert [memory.id for memory in context.memories] == [
        praxis_agent_memory.id,
        praxis_domain_memory.id,
        global_memory.id,
    ]
    assert context.by_scope("agent") == [praxis_agent_memory]
    assert context.by_scope("domain") == [praxis_domain_memory]
    assert context.by_scope("global") == [global_memory]


def test_maestro_retrieval_can_span_domains_and_filter_importance(session: Session) -> None:
    praxis, ophi, _, _ = _domain_and_agents(session)
    service = MemoryService(session)
    service.write_candidate(
        MemoryCandidate(
            domain_id=praxis.id,
            scope="domain",
            memory_type="decision",
            title="Low importance Praxis item",
            content="This should be filtered out.",
            impact_level="low",
            importance=0.2,
        )
    )
    ophi_memory = service.write_candidate(
        MemoryCandidate(
            domain_id=ophi.id,
            scope="domain",
            memory_type="decision",
            title="Important Ophi item",
            content="This should be visible to Maestro.",
            impact_level="low",
            importance=0.85,
        )
    ).memory_item
    session_memory = service.write_candidate(
        MemoryCandidate(
            scope="maestro_session",
            memory_type="summary",
            title="Session summary",
            content="User is focused on memory architecture.",
            impact_level="low",
            importance=0.75,
        )
    ).memory_item

    context = service.retrieve_for_maestro(min_importance=0.7)

    assert [memory.id for memory in context.memories] == [ophi_memory.id, session_memory.id]


def test_invalid_scope_shape_is_rejected(session: Session) -> None:
    service = MemoryService(session)

    try:
        service.write_candidate(
            MemoryCandidate(
                scope="agent",
                memory_type="fact",
                title="Invalid agent memory",
                content="Agent memory needs an agent and domain.",
                impact_level="low",
            )
        )
    except MemoryAccessError as exc:
        assert "requires both domain_id and agent_id" in str(exc)
    else:
        raise AssertionError("Expected invalid agent memory to be rejected.")
