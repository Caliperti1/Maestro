import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid
from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(50), default="owner", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Domain(TimestampMixin, Base):
    __tablename__ = "domains"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    agents: Mapped[list["Agent"]] = relationship(back_populates="domain")
    memory_items: Mapped[list["MemoryItem"]] = relationship(back_populates="domain")


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    tool_permissions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    domain: Mapped[Domain] = relationship(back_populates="agents")


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(240))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_type: Mapped[str] = mapped_column(String(40), nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("agents.id", ondelete="SET NULL"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(40), default="normal", nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), default="chat", nullable=False)
    workflow_key: Mapped[str | None] = mapped_column(String(120))
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Report(TimestampMixin, Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    report_type: Mapped[str] = mapped_column(String(80), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    structured_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MemoryProposal(TimestampMixin, Base):
    __tablename__ = "memory_proposals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tasks.id", ondelete="SET NULL"))
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="SET NULL")
    )
    scope: Mapped[str] = mapped_column(String(40), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    impact_level: Mapped[str] = mapped_column(String(40), default="low", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="proposed", nullable=False, index=True)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryItem(TimestampMixin, Base):
    __tablename__ = "memory_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    created_from_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("memory_proposals.id", ondelete="SET NULL")
    )
    scope: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    impact_level: Mapped[str] = mapped_column(String(40), default="low", nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    domain: Mapped[Domain | None] = relationship(back_populates="memory_items")


class MemoryLink(Base):
    __tablename__ = "memory_links"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(80), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemoryEmbedding(TimestampMixin, Base):
    __tablename__ = "memory_embeddings"
    __table_args__ = (
        UniqueConstraint("memory_item_id", "provider", "model", name="uq_memory_embeddings_model"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    memory_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    dimensions: Mapped[int] = mapped_column(nullable=False)
    source_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)


class ToolConnection(TimestampMixin, Base):
    __tablename__ = "tool_connections"
    __table_args__ = (UniqueConstraint("domain_id", "tool_key", name="uq_tool_connections_domain_tool"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_key: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(80), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RuntimeSetting(TimestampMixin, Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class RoutedItem(TimestampMixin, Base):
    __tablename__ = "routed_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tasks.id", ondelete="SET NULL"))
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="SET NULL")
    )
    seed_package_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("seed_packages.id", ondelete="SET NULL")
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    route_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(40), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="open", nullable=False, index=True)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    tool_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tool_connections.id", ondelete="SET NULL")
    )
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), default="running", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SeedPackage(TimestampMixin, Base):
    __tablename__ = "seed_packages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="uploaded", nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="SET NULL"), index=True
    )
    seed_package_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("seed_packages.id", ondelete="SET NULL")
    )
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(160))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ScheduledRun(TimestampMixin, Base):
    __tablename__ = "scheduled_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    workflow_key: Mapped[str | None] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    cadence: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowDefinition(TimestampMixin, Base):
    __tablename__ = "workflow_definitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    trigger_type: Mapped[str] = mapped_column(String(80), default="manual", nullable=False)
    trigger_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    workflow_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[str] = mapped_column(String(40), default="normal", nullable=False)
    fairness_group: Mapped[str | None] = mapped_column(String(120), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WorkflowRun(TimestampMixin, Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_definitions.id", ondelete="SET NULL"), index=True
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(80), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(40), default="normal", nullable=False)
    fairness_group: Mapped[str | None] = mapped_column(String(120), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowQueueItem(TimestampMixin, Base):
    __tablename__ = "workflow_queue_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    child_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("domains.id", ondelete="SET NULL"), index=True
    )
    external_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(40), default="normal", nullable=False)
    stage_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_keys: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    resource_locks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    fairness_group: Mapped[str | None] = mapped_column(String(120), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SchedulerResourceLock(TimestampMixin, Base):
    __tablename__ = "scheduler_resource_locks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    resource_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    lock_scope: Mapped[str] = mapped_column(String(80), default="exclusive", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="held", nullable=False, index=True)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    queue_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_queue_items.id", ondelete="CASCADE"), index=True
    )
    owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)


class SchedulerEvent(Base):
    __tablename__ = "scheduler_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    queue_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_queue_items.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
