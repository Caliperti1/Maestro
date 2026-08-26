"""Link calendar events to prerequisite, during-event, and follow-up work.

Revision ID: 0026_calendar_event_work_links
Revises: 0025_remove_think_tank
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_calendar_event_work_links"
down_revision: str | None = "0025_remove_think_tank"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_event_work_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("todo_id", sa.Uuid(), nullable=True),
        sa.Column("product_issue_id", sa.Uuid(), nullable=True),
        sa.Column("relationship_type", sa.String(length=40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(todo_id IS NOT NULL AND product_issue_id IS NULL) OR "
            "(todo_id IS NULL AND product_issue_id IS NOT NULL)",
            name="ck_calendar_event_work_link_one_target",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["calendar_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["todo_id"], ["todos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["product_issue_id"], ["product_issues.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "todo_id", name="uq_calendar_event_work_link_todo"),
        sa.UniqueConstraint(
            "event_id", "product_issue_id", name="uq_calendar_event_work_link_issue"
        ),
    )
    for column in ("event_id", "todo_id", "product_issue_id", "relationship_type"):
        op.create_index(
            f"ix_calendar_event_work_links_{column}",
            "calendar_event_work_links",
            [column],
        )


def downgrade() -> None:
    op.drop_table("calendar_event_work_links")
