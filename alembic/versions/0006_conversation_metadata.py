"""add conversation metadata

Revision ID: 0006_conversation_metadata
Revises: 0005_scheduler_foundation
Create Date: 2026-07-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_conversation_metadata"
down_revision: str | None = "0005_scheduler_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.alter_column("conversations", "metadata", server_default=None)


def downgrade() -> None:
    op.drop_column("conversations", "metadata")
