"""Add recurring todo series and occurrence linkage.

Revision ID: 0029_recurring_todos
Revises: 0028_attention_blockers_only
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_recurring_todos"
down_revision: str | None = "0028_attention_blockers_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recurring_todo_series",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("recurrence_rule", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(length=80), nullable=False),
        sa.Column("due_anchor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_anchor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_minutes", sa.Integer(), nullable=True),
        sa.Column("owner_type", sa.String(length=80), nullable=False),
        sa.Column("owner_ref", sa.String(length=240), nullable=True),
        sa.Column("agent_task", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("last_materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recurring_todo_series_domain_id", "recurring_todo_series", ["domain_id"])
    op.create_index("ix_recurring_todo_series_title", "recurring_todo_series", ["title"])
    op.create_index("ix_recurring_todo_series_due_anchor_at", "recurring_todo_series", ["due_anchor_at"])
    op.create_index(
        "ix_recurring_todo_series_scheduled_anchor_at",
        "recurring_todo_series",
        ["scheduled_anchor_at"],
    )
    op.create_index("ix_recurring_todo_series_agent_task", "recurring_todo_series", ["agent_task"])
    op.create_index("ix_recurring_todo_series_status", "recurring_todo_series", ["status"])

    op.add_column("todos", sa.Column("recurring_series_id", sa.Uuid(), nullable=True))
    op.add_column("todos", sa.Column("recurrence_original_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_todos_recurring_series_id",
        "todos",
        "recurring_todo_series",
        ["recurring_series_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_todos_recurring_series_id", "todos", ["recurring_series_id"])
    op.create_index("ix_todos_recurrence_original_at", "todos", ["recurrence_original_at"])
    op.create_unique_constraint(
        "uq_todos_recurring_series_occurrence",
        "todos",
        ["recurring_series_id", "recurrence_original_at"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_todos_recurring_series_occurrence", "todos", type_="unique")
    op.drop_index("ix_todos_recurrence_original_at", table_name="todos")
    op.drop_index("ix_todos_recurring_series_id", table_name="todos")
    op.drop_constraint("fk_todos_recurring_series_id", "todos", type_="foreignkey")
    op.drop_column("todos", "recurrence_original_at")
    op.drop_column("todos", "recurring_series_id")
    op.drop_index("ix_recurring_todo_series_status", table_name="recurring_todo_series")
    op.drop_index("ix_recurring_todo_series_agent_task", table_name="recurring_todo_series")
    op.drop_index("ix_recurring_todo_series_scheduled_anchor_at", table_name="recurring_todo_series")
    op.drop_index("ix_recurring_todo_series_due_anchor_at", table_name="recurring_todo_series")
    op.drop_index("ix_recurring_todo_series_title", table_name="recurring_todo_series")
    op.drop_index("ix_recurring_todo_series_domain_id", table_name="recurring_todo_series")
    op.drop_table("recurring_todo_series")
