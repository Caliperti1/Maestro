"""Add contact aliases.

Revision ID: 0008_contact_aliases
Revises: 0007_routed_memory_objects
Create Date: 2026-07-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0008_contact_aliases"
down_revision: str | None = "0007_routed_memory_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.String(length=240), nullable=False),
        sa.Column("normalized_alias", sa.String(length=260), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False, server_default="system"),
        sa.Column("source_refs", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_alias", name="uq_contact_aliases_normalized_alias"),
    )
    op.create_index(op.f("ix_contact_aliases_contact_id"), "contact_aliases", ["contact_id"], unique=False)
    op.create_index(op.f("ix_contact_aliases_normalized_alias"), "contact_aliases", ["normalized_alias"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_contact_aliases_normalized_alias"), table_name="contact_aliases")
    op.drop_index(op.f("ix_contact_aliases_contact_id"), table_name="contact_aliases")
    op.drop_table("contact_aliases")
