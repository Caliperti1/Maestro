"""Add workflow output run logs and notifications.

Revision ID: 0009_workflow_outputs
Revises: 0008_contact_aliases
Create Date: 2026-07-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0009_workflow_outputs"
down_revision: str | None = "0008_contact_aliases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_run_log_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_definition_id", sa.Uuid(), nullable=True),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("run_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_work", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("report_ids", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("routed_item_ids", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("artifact_ids", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("notification_ids", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_definition_id"], ["workflow_definitions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_run_id", name="uq_workflow_run_log_entries_run"),
    )
    op.create_index(op.f("ix_workflow_run_log_entries_conversation_id"), "workflow_run_log_entries", ["conversation_id"])
    op.create_index(op.f("ix_workflow_run_log_entries_domain_id"), "workflow_run_log_entries", ["domain_id"])
    op.create_index(op.f("ix_workflow_run_log_entries_parent_task_id"), "workflow_run_log_entries", ["parent_task_id"])
    op.create_index(op.f("ix_workflow_run_log_entries_run_completed_at"), "workflow_run_log_entries", ["run_completed_at"])
    op.create_index(op.f("ix_workflow_run_log_entries_status"), "workflow_run_log_entries", ["status"])
    op.create_index(op.f("ix_workflow_run_log_entries_workflow_definition_id"), "workflow_run_log_entries", ["workflow_definition_id"])
    op.create_index(op.f("ix_workflow_run_log_entries_workflow_run_id"), "workflow_run_log_entries", ["workflow_run_id"])

    op.create_table(
        "workflow_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("severity", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("notification_type", sa.String(length=80), nullable=False),
        sa.Column("target", sa.String(length=80), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workflow_notifications_conversation_id"), "workflow_notifications", ["conversation_id"])
    op.create_index(op.f("ix_workflow_notifications_domain_id"), "workflow_notifications", ["domain_id"])
    op.create_index(op.f("ix_workflow_notifications_severity"), "workflow_notifications", ["severity"])
    op.create_index(op.f("ix_workflow_notifications_status"), "workflow_notifications", ["status"])
    op.create_index(op.f("ix_workflow_notifications_workflow_run_id"), "workflow_notifications", ["workflow_run_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_workflow_notifications_workflow_run_id"), table_name="workflow_notifications")
    op.drop_index(op.f("ix_workflow_notifications_status"), table_name="workflow_notifications")
    op.drop_index(op.f("ix_workflow_notifications_severity"), table_name="workflow_notifications")
    op.drop_index(op.f("ix_workflow_notifications_domain_id"), table_name="workflow_notifications")
    op.drop_index(op.f("ix_workflow_notifications_conversation_id"), table_name="workflow_notifications")
    op.drop_table("workflow_notifications")

    op.drop_index(op.f("ix_workflow_run_log_entries_workflow_run_id"), table_name="workflow_run_log_entries")
    op.drop_index(op.f("ix_workflow_run_log_entries_workflow_definition_id"), table_name="workflow_run_log_entries")
    op.drop_index(op.f("ix_workflow_run_log_entries_status"), table_name="workflow_run_log_entries")
    op.drop_index(op.f("ix_workflow_run_log_entries_run_completed_at"), table_name="workflow_run_log_entries")
    op.drop_index(op.f("ix_workflow_run_log_entries_parent_task_id"), table_name="workflow_run_log_entries")
    op.drop_index(op.f("ix_workflow_run_log_entries_domain_id"), table_name="workflow_run_log_entries")
    op.drop_index(op.f("ix_workflow_run_log_entries_conversation_id"), table_name="workflow_run_log_entries")
    op.drop_table("workflow_run_log_entries")
