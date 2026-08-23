"""Remove the obsolete Think Tank routed store.

Revision ID: 0025_remove_think_tank
Revises: 0024_product_issue_intelligence
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_remove_think_tank"
down_revision: str | None = "0024_product_issue_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM routed_object_change_log WHERE object_type IN ('idea', 'routed_note')")
    op.execute("DELETE FROM routed_object_links WHERE object_type IN ('idea', 'routed_note')")
    op.execute("DELETE FROM routed_items WHERE route_type = 'think_tank'")
    op.drop_table("ideas")


def downgrade() -> None:
    op.create_table(
        "ideas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ideas_domain_id", "ideas", ["domain_id"])
    op.create_index("ix_ideas_status", "ideas", ["status"])
    op.create_index("ix_ideas_title", "ideas", ["title"])
