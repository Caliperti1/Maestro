"""Retire the L3 domain and its runtime components.

Revision ID: 0030_retire_l3_domain
Revises: 0029_recurring_todos
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_retire_l3_domain"
down_revision: str | None = "0029_recurring_todos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    l3_id = connection.scalar(sa.text("SELECT id FROM domains WHERE key = 'l3'"))
    if l3_id is None:
        return

    connection.execute(
        sa.text("UPDATE workflow_definitions SET is_active = false WHERE domain_id = :domain_id"),
        {"domain_id": l3_id},
    )
    connection.execute(
        sa.text("UPDATE source_registrations SET is_active = false WHERE domain_id = :domain_id"),
        {"domain_id": l3_id},
    )
    connection.execute(
        sa.text("UPDATE agents SET is_active = false WHERE domain_id = :domain_id"),
        {"domain_id": l3_id},
    )
    connection.execute(
        sa.text("UPDATE domains SET is_active = false WHERE id = :domain_id"),
        {"domain_id": l3_id},
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE domains SET is_active = true WHERE key = 'l3'"))

