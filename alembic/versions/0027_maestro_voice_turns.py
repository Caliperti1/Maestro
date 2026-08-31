"""Add durable Maestro Voice client-turn idempotency.

Revision ID: 0027_maestro_voice_turns
Revises: 0026_calendar_event_work_links
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_maestro_voice_turns"
down_revision: str | None = "0026_calendar_event_work_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("client_turn_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_messages_client_turn_id",
        "messages",
        ["client_turn_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_messages_client_turn_id",
        "messages",
        ["client_turn_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_messages_client_turn_id", "messages", type_="unique")
    op.drop_index("ix_messages_client_turn_id", table_name="messages")
    op.drop_column("messages", "client_turn_id")
