"""Remove todo-style completion state from calendar events.

Revision ID: 0017_calendar_event_lifecycle
Revises: 0016_perti_calendar_intelligence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_calendar_event_lifecycle"
down_revision: str | None = "0016_perti_calendar_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM contact_interactions
            WHERE calendar_event_id IN (
                SELECT id
                FROM calendar_events
                WHERE status IN ('done', 'completed')
                  AND start_at > now()
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE calendar_events
            SET status = 'scheduled', updated_at = now()
            WHERE status IN ('done', 'completed')
            """
        )
    )


def downgrade() -> None:
    # Completion was ambiguous user-interface state, so it cannot be reconstructed safely.
    pass
