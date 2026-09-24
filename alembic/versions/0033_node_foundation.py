"""Add the durable execution-node registry and job queue.

Revision ID: 0033_node_foundation
Revises: 0032_merge_mobile_codex_heads
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0033_node_foundation"
down_revision: str | None = "0032_merge_mobile_codex_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "execution_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("platform", sa.String(length=80), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("credential_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), server_default="offline", nullable=False),
        sa.Column("client_version", sa.String(length=80), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credential_issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), server_default="{}", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("status", "last_seen_at", "last_heartbeat_at"):
        op.create_index(op.f(f"ix_execution_nodes_{column}"), "execution_nodes", [column])

    op.create_table(
        "node_capabilities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("capability_key", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=40), server_default="ready", nullable=False),
        sa.Column("details", postgresql.JSON(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "capability_key", name="uq_node_capabilities_node_key"),
    )
    for column in ("node_id", "capability_key", "status"):
        op.create_index(op.f(f"ix_node_capabilities_{column}"), "node_capabilities", [column])

    op.create_table(
        "node_enrollment_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_hint", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("allowed_capabilities", postgresql.JSON(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), server_default="{}", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    for column in ("expires_at", "used_at", "revoked_at", "created_by_user_id"):
        op.create_index(op.f(f"ix_node_enrollment_tokens_{column}"), "node_enrollment_tokens", [column])

    op.create_table(
        "node_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("capability_key", sa.String(length=160), nullable=False),
        sa.Column("job_type", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), server_default="waiting_for_node", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=True),
        sa.Column("input_envelope", postgresql.JSON(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("output_envelope", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    for column in (
        "node_id", "task_id", "capability_key", "job_type", "status", "priority",
        "available_at", "lease_expires_at",
    ):
        op.create_index(op.f(f"ix_node_jobs_{column}"), "node_jobs", [column])
    op.create_index(
        "ix_node_jobs_claim",
        "node_jobs",
        ["node_id", "status", "available_at", "priority"],
    )

    op.create_table(
        "node_job_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("from_status", sa.String(length=40), nullable=True),
        sa.Column("to_status", sa.String(length=40), nullable=True),
        sa.Column("payload", postgresql.JSON(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["node_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("job_id", "node_id", "event_type", "created_at"):
        op.create_index(op.f(f"ix_node_job_events_{column}"), "node_job_events", [column])


def downgrade() -> None:
    op.drop_table("node_job_events")
    op.drop_index("ix_node_jobs_claim", table_name="node_jobs")
    op.drop_table("node_jobs")
    op.drop_table("node_enrollment_tokens")
    op.drop_table("node_capabilities")
    op.drop_table("execution_nodes")
