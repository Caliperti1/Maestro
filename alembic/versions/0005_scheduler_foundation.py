"""add scheduler foundation

Revision ID: 0005_scheduler_foundation
Revises: 0004_routed_items
Create Date: 2026-07-01 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_scheduler_foundation"
down_revision: str | None = "0004_routed_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("trigger_type", sa.String(length=80), nullable=False),
        sa.Column("trigger_config", sa.JSON(), nullable=False),
        sa.Column("workflow_spec", sa.JSON(), nullable=False),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column("fairness_group", sa.String(length=120), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_workflow_definitions_domain_id", "workflow_definitions", ["domain_id"])
    op.create_index("ix_workflow_definitions_fairness_group", "workflow_definitions", ["fairness_group"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_definition_id", sa.Uuid(), nullable=True),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column("fairness_group", sa.String(length=120), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_definition_id"], ["workflow_definitions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_workflow_runs_conversation_id", "workflow_runs", ["conversation_id"])
    op.create_index("ix_workflow_runs_domain_id", "workflow_runs", ["domain_id"])
    op.create_index("ix_workflow_runs_fairness_group", "workflow_runs", ["fairness_group"])
    op.create_index("ix_workflow_runs_parent_task_id", "workflow_runs", ["parent_task_id"])
    op.create_index("ix_workflow_runs_scheduled_for", "workflow_runs", ["scheduled_for"])
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.create_index("ix_workflow_runs_workflow_definition_id", "workflow_runs", ["workflow_definition_id"])

    op.create_table(
        "workflow_queue_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("child_task_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("external_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column("stage_index", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("dependency_keys", sa.JSON(), nullable=False),
        sa.Column("resource_locks", sa.JSON(), nullable=False),
        sa.Column("fairness_group", sa.String(length=120), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["child_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_queue_items_agent_id", "workflow_queue_items", ["agent_id"])
    op.create_index("ix_workflow_queue_items_child_task_id", "workflow_queue_items", ["child_task_id"])
    op.create_index("ix_workflow_queue_items_domain_id", "workflow_queue_items", ["domain_id"])
    op.create_index("ix_workflow_queue_items_external_key", "workflow_queue_items", ["external_key"])
    op.create_index("ix_workflow_queue_items_fairness_group", "workflow_queue_items", ["fairness_group"])
    op.create_index("ix_workflow_queue_items_lease_expires_at", "workflow_queue_items", ["lease_expires_at"])
    op.create_index("ix_workflow_queue_items_parent_task_id", "workflow_queue_items", ["parent_task_id"])
    op.create_index("ix_workflow_queue_items_stage_index", "workflow_queue_items", ["stage_index"])
    op.create_index("ix_workflow_queue_items_status", "workflow_queue_items", ["status"])
    op.create_index("ix_workflow_queue_items_workflow_run_id", "workflow_queue_items", ["workflow_run_id"])

    op.create_table(
        "scheduler_resource_locks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resource_key", sa.String(length=200), nullable=False),
        sa.Column("lock_scope", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("queue_item_id", sa.Uuid(), nullable=True),
        sa.Column("owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["queue_item_id"], ["workflow_queue_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduler_resource_locks_lease_expires_at", "scheduler_resource_locks", ["lease_expires_at"])
    op.create_index("ix_scheduler_resource_locks_queue_item_id", "scheduler_resource_locks", ["queue_item_id"])
    op.create_index("ix_scheduler_resource_locks_resource_key", "scheduler_resource_locks", ["resource_key"])
    op.create_index("ix_scheduler_resource_locks_status", "scheduler_resource_locks", ["status"])
    op.create_index("ix_scheduler_resource_locks_workflow_run_id", "scheduler_resource_locks", ["workflow_run_id"])

    op.create_table(
        "scheduler_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("queue_item_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["queue_item_id"], ["workflow_queue_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduler_events_event_type", "scheduler_events", ["event_type"])
    op.create_index("ix_scheduler_events_queue_item_id", "scheduler_events", ["queue_item_id"])
    op.create_index("ix_scheduler_events_workflow_run_id", "scheduler_events", ["workflow_run_id"])


def downgrade() -> None:
    op.drop_index("ix_scheduler_events_workflow_run_id", table_name="scheduler_events")
    op.drop_index("ix_scheduler_events_queue_item_id", table_name="scheduler_events")
    op.drop_index("ix_scheduler_events_event_type", table_name="scheduler_events")
    op.drop_table("scheduler_events")
    op.drop_index("ix_scheduler_resource_locks_workflow_run_id", table_name="scheduler_resource_locks")
    op.drop_index("ix_scheduler_resource_locks_status", table_name="scheduler_resource_locks")
    op.drop_index("ix_scheduler_resource_locks_resource_key", table_name="scheduler_resource_locks")
    op.drop_index("ix_scheduler_resource_locks_queue_item_id", table_name="scheduler_resource_locks")
    op.drop_index("ix_scheduler_resource_locks_lease_expires_at", table_name="scheduler_resource_locks")
    op.drop_table("scheduler_resource_locks")
    op.drop_index("ix_workflow_queue_items_workflow_run_id", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_status", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_stage_index", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_parent_task_id", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_lease_expires_at", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_fairness_group", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_external_key", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_domain_id", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_child_task_id", table_name="workflow_queue_items")
    op.drop_index("ix_workflow_queue_items_agent_id", table_name="workflow_queue_items")
    op.drop_table("workflow_queue_items")
    op.drop_index("ix_workflow_runs_workflow_definition_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_status", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_scheduled_for", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_parent_task_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_fairness_group", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_domain_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_conversation_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index("ix_workflow_definitions_fairness_group", table_name="workflow_definitions")
    op.drop_index("ix_workflow_definitions_domain_id", table_name="workflow_definitions")
    op.drop_table("workflow_definitions")
