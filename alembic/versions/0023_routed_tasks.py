"""Add scheduled and agent-executed routed tasks.

Revision ID: 0023_routed_tasks
Revises: 0022_calendar_context_windows
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_routed_tasks"
down_revision: str | None = "0022_calendar_context_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("todos", sa.Column("estimated_minutes", sa.Integer(), nullable=True))
    op.add_column(
        "todos", sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "todos", sa.Column("agent_task", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column(
        "todos",
        sa.Column(
            "agent_task_status", sa.String(length=40), server_default="not_agent", nullable=False
        ),
    )
    op.add_column("todos", sa.Column("workflow_task_id", sa.Uuid(), nullable=True))
    op.add_column("todos", sa.Column("workflow_run_id", sa.Uuid(), nullable=True))
    op.add_column(
        "todos", sa.Column("last_agent_task_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("todos", sa.Column("agent_task_error", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_todos_workflow_task_id",
        "todos",
        "tasks",
        ["workflow_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_todos_workflow_run_id",
        "todos",
        "workflow_runs",
        ["workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_todos_scheduled_start_at", "todos", ["scheduled_start_at"])
    op.create_index("ix_todos_agent_task", "todos", ["agent_task"])
    op.create_index("ix_todos_agent_task_status", "todos", ["agent_task_status"])
    op.create_index("ix_todos_workflow_task_id", "todos", ["workflow_task_id"])
    op.create_index("ix_todos_workflow_run_id", "todos", ["workflow_run_id"])

    op.add_column("calendar_events", sa.Column("todo_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_calendar_events_todo_id",
        "calendar_events",
        "todos",
        ["todo_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_calendar_events_todo_id", "calendar_events", ["todo_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_calendar_events_todo_id", table_name="calendar_events")
    op.drop_constraint("fk_calendar_events_todo_id", "calendar_events", type_="foreignkey")
    op.drop_column("calendar_events", "todo_id")

    op.drop_index("ix_todos_workflow_run_id", table_name="todos")
    op.drop_index("ix_todos_workflow_task_id", table_name="todos")
    op.drop_index("ix_todos_agent_task_status", table_name="todos")
    op.drop_index("ix_todos_agent_task", table_name="todos")
    op.drop_index("ix_todos_scheduled_start_at", table_name="todos")
    op.drop_constraint("fk_todos_workflow_run_id", "todos", type_="foreignkey")
    op.drop_constraint("fk_todos_workflow_task_id", "todos", type_="foreignkey")
    op.drop_column("todos", "agent_task_error")
    op.drop_column("todos", "last_agent_task_attempt_at")
    op.drop_column("todos", "workflow_run_id")
    op.drop_column("todos", "workflow_task_id")
    op.drop_column("todos", "agent_task_status")
    op.drop_column("todos", "agent_task")
    op.drop_column("todos", "scheduled_start_at")
    op.drop_column("todos", "estimated_minutes")
