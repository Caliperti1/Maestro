from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.db.repositories import (
    AgentRepository,
    ArtifactRepository,
    ConversationRepository,
    DomainRepository,
    MemoryItemRepository,
    MemoryLinkRepository,
    MemoryProposalRepository,
    MessageRepository,
    ReportRepository,
    ScheduledRunRepository,
    SeedPackageRepository,
    TaskRepository,
    ToolCallRepository,
    ToolConnectionRepository,
    UserRepository,
)
from app.db.seed import DEFAULT_DOMAINS, seed_default_domains


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        yield db


def test_default_domain_seed_is_idempotent(session: Session) -> None:
    first_seed = seed_default_domains(session)
    second_seed = seed_default_domains(session)

    domain_repo = DomainRepository(session)
    active_domains = domain_repo.list_active()

    assert len(first_seed) == len(DEFAULT_DOMAINS)
    assert len(second_seed) == len(DEFAULT_DOMAINS)
    assert len(active_domains) == len(DEFAULT_DOMAINS)
    assert domain_repo.get_by_key("maestro-development") is not None


def test_core_repositories_create_and_read(session: Session) -> None:
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("praxis")
    assert domain is not None

    user = UserRepository(session).create(email="chris@example.com", display_name="Chris")
    agent = AgentRepository(session).create(
        domain_id=domain.id,
        key="praxis-cto",
        name="Praxis CTO",
        agent_type="cto",
        capabilities={"reports": True},
        tool_permissions={"github": "read"},
    )
    conversation = ConversationRepository(session).create(
        user_id=user.id,
        domain_id=domain.id,
        agent_id=agent.id,
        title="Praxis repo status",
    )
    message = MessageRepository(session).create(
        conversation_id=conversation.id,
        sender_type="user",
        agent_id=agent.id,
        content="What is the repo status?",
    )
    task = TaskRepository(session).create(
        conversation_id=conversation.id,
        domain_id=domain.id,
        requested_by_user_id=user.id,
        assigned_agent_id=agent.id,
        objective="Summarize repo status",
    )
    report = ReportRepository(session).create(
        task_id=task.id,
        domain_id=domain.id,
        agent_id=agent.id,
        title="Repo status",
        report_type="status",
        body_markdown="No implementation yet.",
    )
    tool_connection = ToolConnectionRepository(session).create(
        domain_id=domain.id,
        tool_key="github",
        display_name="GitHub",
        auth_type="app",
    )
    ToolCallRepository(session).create(
        task_id=task.id,
        agent_id=agent.id,
        tool_connection_id=tool_connection.id,
        tool_name="issues.list",
        status="complete",
    )
    ArtifactRepository(session).create(
        task_id=task.id,
        report_id=report.id,
        artifact_type="markdown",
        name="repo-status.md",
        uri="artifact://repo-status.md",
    )

    assert UserRepository(session).get_by_email("chris@example.com") == user
    assert AgentRepository(session).get_by_key("praxis-cto") == agent
    assert ConversationRepository(session).get(conversation.id) == conversation
    assert MessageRepository(session).list_by_conversation(conversation.id) == [message]
    assert TaskRepository(session).list_by_status("queued") == [task]
    assert ReportRepository(session).list_by_task(task.id) == [report]
    assert ToolConnectionRepository(session).list_by_domain(domain.id) == [tool_connection]


def test_memory_layers_proposals_links_and_seed_packages(session: Session) -> None:
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("maestro-development")
    assert domain is not None

    agent = AgentRepository(session).create(
        domain_id=domain.id,
        key="memory-curator",
        name="Memory Curator",
        agent_type="memory_curator",
    )
    seed_package = SeedPackageRepository(session).create(
        domain_id=domain.id,
        name="Architecture notes",
        source_type="doc",
        metadata_={"path": "Maestro Design Thoughts.md"},
    )
    proposal = MemoryProposalRepository(session).create(
        domain_id=domain.id,
        agent_id=agent.id,
        scope="domain",
        memory_type="decision",
        title="Postgres first",
        content="Maestro uses Postgres from the beginning.",
        rationale="Persistence and provenance are core concerns.",
        impact_level="medium",
        source_refs=[{"type": "seed_package", "id": str(seed_package.id)}],
    )
    global_memory = MemoryItemRepository(session).create(
        scope="global",
        memory_type="preference",
        title="Phone-first feedback",
        content="Chris wants to test Maestro from his phone early.",
        importance=0.8,
        impact_level="medium",
    )
    domain_memory = MemoryItemRepository(session).create(
        domain_id=domain.id,
        created_from_proposal_id=proposal.id,
        scope="domain",
        memory_type="decision",
        title="Postgres first",
        content="Maestro uses Postgres from the beginning.",
    )
    agent_memory = MemoryItemRepository(session).create(
        domain_id=domain.id,
        agent_id=agent.id,
        scope="agent",
        memory_type="standing_instruction",
        title="Curator writes canonical memory",
        content="Only the Memory Curator writes canonical memory.",
    )
    memory_link = MemoryLinkRepository(session).create(
        source_memory_id=domain_memory.id,
        target_memory_id=global_memory.id,
        relation_type="supports",
    )
    ArtifactRepository(session).create(
        seed_package_id=seed_package.id,
        artifact_type="source_doc",
        name="Architecture notes",
        uri="file://Maestro Design Thoughts.md",
    )

    memory_repo = MemoryItemRepository(session)
    assert memory_repo.list_global_memory() == [global_memory]
    assert memory_repo.list_domain_memory(domain.id) == [domain_memory]
    assert memory_repo.list_agent_memory(agent.id) == [agent_memory]
    assert MemoryProposalRepository(session).list_by_status("proposed") == [proposal]
    assert MemoryLinkRepository(session).list_from_memory(domain_memory.id) == [memory_link]
    assert SeedPackageRepository(session).list_by_status("uploaded") == [seed_package]


def test_scheduled_run_repository(session: Session) -> None:
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("personal")
    assert domain is not None

    run = ScheduledRunRepository(session).create(
        domain_id=domain.id,
        workflow_key="daily-standup",
        name="Daily Standup",
        cadence="daily",
        config={"hour": 8},
    )

    assert ScheduledRunRepository(session).list_active() == [run]
