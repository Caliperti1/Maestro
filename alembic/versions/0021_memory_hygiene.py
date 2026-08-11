"""Add durable memory hygiene audit runs.

Revision ID: 0021_memory_hygiene
Revises: 0020_federated_retrieval
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_memory_hygiene"
down_revision: str | None = "0020_federated_retrieval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_hygiene_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("embedding_backfilled_count", sa.Integer(), nullable=False),
        sa.Column("provenance_repaired_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_merged_count", sa.Integer(), nullable=False),
        sa.Column("proposal_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_hygiene_runs_status", "memory_hygiene_runs", ["status"])


def downgrade() -> None:
    op.drop_table("memory_hygiene_runs")
