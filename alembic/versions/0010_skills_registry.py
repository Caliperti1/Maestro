"""Add skills registry.

Revision ID: 0010_skills_registry
Revises: 0009_workflow_outputs
Create Date: 2026-07-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_skills_registry"
down_revision = "0009_workflow_outputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "skill_permissions",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.create_table(
        "skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index(op.f("ix_skills_domain_id"), "skills", ["domain_id"])
    op.create_index(op.f("ix_skills_is_active"), "skills", ["is_active"])


def downgrade() -> None:
    op.drop_index(op.f("ix_skills_is_active"), table_name="skills")
    op.drop_index(op.f("ix_skills_domain_id"), table_name="skills")
    op.drop_table("skills")
    op.drop_column("agents", "skill_permissions")
