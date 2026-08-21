"""Add nonblocking calendar context windows.

Revision ID: 0022_calendar_context_windows
Revises: 0021_memory_hygiene
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_calendar_context_windows"
down_revision: str | None = "0021_memory_hygiene"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_events",
        sa.Column("item_kind", sa.String(length=40), server_default="event", nullable=False),
    )
    op.add_column(
        "calendar_events",
        sa.Column("context_type", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column("scheduling_effect", sa.String(length=40), server_default="hard", nullable=False),
    )
    op.add_column(
        "calendar_events",
        sa.Column("blocks_time", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_index("ix_calendar_events_item_kind", "calendar_events", ["item_kind"])
    op.create_index("ix_calendar_events_context_type", "calendar_events", ["context_type"])
    op.create_index(
        "ix_calendar_events_scheduling_effect", "calendar_events", ["scheduling_effect"]
    )
    op.create_index("ix_calendar_events_blocks_time", "calendar_events", ["blocks_time"])


def downgrade() -> None:
    op.drop_index("ix_calendar_events_blocks_time", table_name="calendar_events")
    op.drop_index("ix_calendar_events_scheduling_effect", table_name="calendar_events")
    op.drop_index("ix_calendar_events_context_type", table_name="calendar_events")
    op.drop_index("ix_calendar_events_item_kind", table_name="calendar_events")
    op.drop_column("calendar_events", "blocks_time")
    op.drop_column("calendar_events", "scheduling_effect")
    op.drop_column("calendar_events", "context_type")
    op.drop_column("calendar_events", "item_kind")
