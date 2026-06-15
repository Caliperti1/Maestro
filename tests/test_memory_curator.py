import uuid

from sqlalchemy.orm import Session

from app.db.models import MemoryItem
from app.db.repositories import AgentRepository, DomainRepository, MemoryProposalRepository
from app.db.seed import seed_default_domains
from app.memory import MemoryCurator, StagedMemorySource


def _source_context(session: Session):
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("maestro-development")
    assert domain is not None
    agent = AgentRepository(session).create(
        domain_id=domain.id,
        key="deterministic-memory-curator-test-agent",
        name="Deterministic Memory Curator Test Agent",
        agent_type="memory_curator",
    )
    return domain, agent


def test_curator_extracts_marked_memory_candidates(session: Session) -> None:
    domain, agent = _source_context(session)
    source = StagedMemorySource(
        source_type="raw_note",
        source_id="note-1",
        domain_id=domain.id,
        agent_id=agent.id,
        title="Architecture scratchpad",
        uri="file://scratchpad.md",
        content="""
Unmarked text is ignored.
PREFERENCE: Phone review - Chris wants phone-accessible Maestro workflows early.
DECISION [global]: Product bet - Memory is the core Maestro product bet.
INSTRUCTION [agent]: Curator behavior - Preserve source refs on every candidate.
""",
    )

    candidates = MemoryCurator(session).extract_candidates(source)

    assert [candidate.memory_type for candidate in candidates] == [
        "preference",
        "decision",
        "standing_instruction",
    ]
    assert [candidate.scope for candidate in candidates] == ["domain", "global", "agent"]
    assert candidates[0].domain_id == domain.id
    assert candidates[1].domain_id is None
    assert candidates[2].agent_id == agent.id
    assert candidates[0].source_refs == [
        {"type": "raw_note", "line": 3, "id": "note-1", "uri": "file://scratchpad.md"}
    ]


def test_curator_processes_source_through_memory_service(session: Session) -> None:
    domain, agent = _source_context(session)
    report_id = uuid.uuid4()
    source = StagedMemorySource(
        source_type="report",
        source_id=report_id,
        domain_id=domain.id,
        agent_id=agent.id,
        report_id=report_id,
        title="Daily standup test report",
        content="""
MEMORY: Standup format - Daily standup should stay thin until domain agents mature.
DECISION: Queue concept - Very high impact memory must wait for approval.
VERY_HIGH: Approval policy - Let Maestro change external commitments without approval.
""",
    )

    batch = MemoryCurator(session).process_source(source)

    assert len(batch.candidates) == 3
    assert [result.outcome for result in batch.results] == [
        "written",
        "auto_approved",
        "pending_user_approval",
    ]
    assert batch.pending_approval_count == 1
    assert session.query(MemoryItem).count() == 2

    proposals = MemoryProposalRepository(session).list_by_status("pending_user_approval")
    assert len(proposals) == 1
    assert proposals[0].source_refs == [{"type": "report", "line": 4, "id": str(report_id)}]


def test_curator_approval_queue_can_approve_or_reject_pending_memory(
    session: Session,
) -> None:
    domain, _ = _source_context(session)
    curator = MemoryCurator(session)
    batch = curator.process_source(
        StagedMemorySource(
            source_type="raw_note",
            source_id="approval-note",
            domain_id=domain.id,
            content="""
VERY_HIGH: External authority - Allow Maestro to send emails without confirmation.
VERY_HIGH: Budget policy - Allow Maestro to approve spend above existing limits.
""",
        )
    )

    assert batch.pending_approval_count == 2
    pending = list(curator.list_pending_approvals(domain_id=domain.id))
    assert len(pending) == 2

    approved = curator.approve_pending_memory(pending[0].id)
    rejected = curator.reject_pending_memory(pending[1].id, reason="Too much autonomy.")

    assert approved.created_from_proposal_id == pending[0].id
    assert rejected.status == "rejected"
    assert rejected.metadata_["rejection_reason"] == "Too much autonomy."
    assert len(curator.list_pending_approvals(domain_id=domain.id)) == 0


def test_curator_can_process_multiple_staged_sources(session: Session) -> None:
    domain, agent = _source_context(session)
    curator = MemoryCurator(session)
    batches = curator.process_sources(
        [
            StagedMemorySource(
                source_type="message",
                source_id="message-1",
                domain_id=domain.id,
                agent_id=agent.id,
                content="FACT: Maestro uses deterministic extraction before LLM extraction.",
            ),
            StagedMemorySource(
                source_type="tool_call",
                source_id="tool-call-1",
                domain_id=domain.id,
                agent_id=agent.id,
                content="SUMMARY [session]: Current thread - User wants issue #16 next.",
            ),
        ]
    )

    assert [len(batch.candidates) for batch in batches] == [1, 1]
    assert session.query(MemoryItem).count() == 2
    scopes = {memory.scope for memory in session.query(MemoryItem).all()}
    assert scopes == {"domain", "maestro_session"}
