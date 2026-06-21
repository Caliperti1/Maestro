"""add memory embeddings

Revision ID: 0002_memory_embeddings
Revises: 0001_initial_maestro_schema
Create Date: 2026-06-20
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision = "0002_memory_embeddings"
down_revision = "0001_initial_maestro_schema"
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "memory_embeddings",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column(
            "memory_item_id",
            UUID,
            sa.ForeignKey("memory_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("source_text_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_memory_embeddings_memory_item_id", "memory_embeddings", ["memory_item_id"])
    op.create_unique_constraint(
        "uq_memory_embeddings_model",
        "memory_embeddings",
        ["memory_item_id", "provider", "model"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_memory_embeddings_model", "memory_embeddings", type_="unique")
    op.drop_index("ix_memory_embeddings_memory_item_id", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")
