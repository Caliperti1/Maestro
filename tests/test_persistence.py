from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

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
    WorkflowQueueItemRepository,
    WorkflowRunRepository,
)
from app.db.seed import DEFAULT_DOMAINS, seed_default_domains
from app.maestro.scheduler import SchedulerService


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


def test_scheduler_persists_workflow_run_queue_and_parallel_batches(session: Session) -> None:
    seed_default_domains(session)
    domain = DomainRepository(session).get_by_key("praxis")
    assert domain is not None
    task = TaskRepository(session).create(
        domain_id=domain.id,
        status="proposed",
        priority="high",
        source_type="maestro_chat",
        workflow_key="maestro.generic",
        objective="Prepare partner workflow.",
        input_payload={
            "plan_id": "plan-1",
            "scheduler": {
                "policy": "test",
                "queue_items": [
                    {
                        "id": "q1",
                        "stage_index": 1,
                        "position": 1,
                        "status": "pending",
                        "domain_key": "praxis",
                        "objective": "Research partner.",
                    },
                    {
                        "id": "q2",
                        "stage_index": 1,
                        "position": 2,
                        "status": "pending",
                        "domain_key": "ophi",
                        "objective": "Check product implications.",
                    },
                    {
                        "id": "q3",
                        "stage_index": 2,
                        "position": 1,
                        "status": "pending",
                        "domain_key": "praxis",
                        "objective": "Synthesize brief.",
                        "depends_on_work_item_ids": ["q1"],
                    },
                ],
            },
        },
    )

    run = SchedulerService(session).enqueue_maestro_plan(task)

    assert WorkflowRunRepository(session).get_by_parent_task(task.id) == run
    queue_items = WorkflowQueueItemRepository(session).list_by_run(run.id)
    assert [item.external_key for item in queue_items] == ["q1", "q2", "q3"]
    batches = SchedulerService(session).runnable_batches()
    assert len(batches) == 1
    assert {item["external_key"] for item in batches[0]["parallel_ready"]} == {"q1", "q2"}


def test_scheduler_claims_completes_and_unblocks_dependent_work(session: Session) -> None:
    seed_default_domains(session)
    definition = SchedulerService(session).upsert_definition(
        key="daily-standup",
        name="Daily Standup",
        trigger_type="manual",
        workflow_spec={
            "queue_items": [
                {
                    "id": "collect",
                    "stage_index": 1,
                    "objective": "Collect domain updates.",
                    "domain_key": "praxis",
                    "required_tools": ["github.pr.search"],
                },
                {
                    "id": "synthesize",
                    "stage_index": 2,
                    "objective": "Synthesize daily plan.",
                    "domain_key": "maestro-development",
                    "depends_on": ["collect"],
                },
            ]
        },
    )
    run = SchedulerService(session).enqueue_definition_run(definition)

    claimed = SchedulerService(session).claim_ready_items(owner="test-worker", limit=4)

    assert [item.external_key for item in claimed] == ["collect"]
    assert claimed[0].status == "running"
    assert claimed[0].lease_owner == "test-worker"
    SchedulerService(session).complete_queue_item(claimed[0].id, output_payload={"ok": True})
    batches = SchedulerService(session).runnable_batches()
    assert batches[0]["workflow_run_id"] == str(run.id)
    assert [item["external_key"] for item in batches[0]["parallel_ready"]] == ["synthesize"]


def test_scheduler_enqueues_due_recurring_definitions_once(session: Session) -> None:
    now = datetime.now(UTC)
    definition = SchedulerService(session).upsert_definition(
        key="morning-standup",
        name="Morning Standup",
        trigger_type="recurring",
        trigger_config={
            "next_run_at": (now - timedelta(minutes=1)).isoformat(),
            "interval_minutes": 60,
        },
        workflow_spec={
            "queue_items": [
                {"id": "standup", "objective": "Prepare standup.", "domain_key": "personal"}
            ]
        },
        fairness_group="personal",
    )

    runs = SchedulerService(session).enqueue_due_workflows(now=now)
    second_runs = SchedulerService(session).enqueue_due_workflows(now=now)

    assert len(runs) == 1
    assert second_runs == []
    session.refresh(definition)
    assert definition.trigger_config["last_enqueued_at"]
    assert definition.trigger_config["next_run_at"] != (now - timedelta(minutes=1)).isoformat()


def test_scheduler_enqueues_event_triggered_workflows_with_filters(session: Session) -> None:
    SchedulerService(session).upsert_definition(
        key="praxis-email-triage",
        name="Praxis Email Triage",
        trigger_type="event",
        trigger_config={
            "event_type": "gmail.message.received",
            "filters": {"domain_key": "praxis", "labels.primary": True},
        },
        workflow_spec={
            "queue_items": [
                {
                    "id": "triage",
                    "objective": "Triage the new Praxis email.",
                    "domain_key": "praxis",
                    "required_tools": ["gmail.message.get"],
                }
            ]
        },
        fairness_group="praxis",
    )

    ignored = SchedulerService(session).enqueue_event_workflows(
        event_type="gmail.message.received",
        event_id="msg-ignored",
        event_payload={"domain_key": "ophi", "labels": {"primary": True}},
    )
    runs = SchedulerService(session).enqueue_event_workflows(
        event_type="gmail.message.received",
        event_id="msg-1",
        event_payload={"domain_key": "praxis", "labels": {"primary": True}},
    )
    duplicate = SchedulerService(session).enqueue_event_workflows(
        event_type="gmail.message.received",
        event_id="msg-1",
        event_payload={"domain_key": "praxis", "labels": {"primary": True}},
    )

    assert ignored == []
    assert len(runs) == 1
    assert duplicate == runs
    assert runs[0].source_type == "event"
    assert runs[0].input_payload["event"]["event_id"] == "msg-1"


def test_scheduler_tick_enqueues_due_work_and_claims_ready_items(session: Session) -> None:
    now = datetime.now(UTC)
    SchedulerService(session).upsert_definition(
        key="before-eight-standup",
        name="Before 8 AM Standup",
        trigger_type="recurring",
        trigger_config={
            "next_run_at": (now - timedelta(minutes=5)).isoformat(),
            "interval_minutes": 1440,
        },
        workflow_spec={
            "queue_items": [
                {"id": "brief", "objective": "Build the daily brief.", "domain_key": "personal"}
            ]
        },
        fairness_group="personal",
    )

    result = SchedulerService(session).tick(owner="tick-test", claim_limit=2, now=now)

    assert len(result["enqueued"]) == 1
    assert len(result["claimed"]) == 1
    assert result["claimed"][0]["external_key"] == "brief"
    assert result["claimed"][0]["lease_owner"] == "tick-test"
