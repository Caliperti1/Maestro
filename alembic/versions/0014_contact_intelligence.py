"""Add first-class contact intelligence records.

Revision ID: 0014_contact_intelligence
Revises: 0013_llm_call_logs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0014_contact_intelligence"
down_revision: str | None = "0013_llm_call_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB


def upgrade() -> None:
    op.add_column("contact_relationships", sa.Column("domain_id", UUID, nullable=True))
    op.add_column(
        "contact_relationships",
        sa.Column("relationship_type", sa.String(length=80), nullable=False, server_default="associated_with"),
    )
    op.add_column(
        "contact_relationships",
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.7"),
    )
    op.add_column(
        "contact_relationships",
        sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
    )
    op.create_foreign_key(
        "fk_contact_relationships_domain_id",
        "contact_relationships",
        "domains",
        ["domain_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_contact_relationships_domain_id", "contact_relationships", ["domain_id"])
    op.create_index("ix_contact_relationships_status", "contact_relationships", ["status"])

    op.create_table(
        "contact_interactions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("contact_id", UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("routed_item_id", UUID, sa.ForeignKey("routed_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("interaction_type", sa.String(length=80), nullable=False, server_default="mention"),
        sa.Column("channel", sa.String(length=80), nullable=True),
        sa.Column("direction", sa.String(length=40), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_refs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("provenance", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("routed_item_id", name="uq_contact_interactions_routed_item_id"),
    )
    for column in ("contact_id", "domain_id", "routed_item_id", "interaction_type", "channel", "occurred_at"):
        op.create_index(f"ix_contact_interactions_{column}", "contact_interactions", [column])

    op.create_table(
        "contact_organization_affiliations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("contact_id", UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", UUID, sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("role", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("relationship_type", sa.String(length=80), nullable=False, server_default="works_at"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_refs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("provenance", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("contact_id", "entity_id", "domain_id", "role", name="uq_contact_affiliation_context"),
    )
    for column in ("contact_id", "entity_id", "domain_id", "status"):
        op.create_index(f"ix_contact_organization_affiliations_{column}", "contact_organization_affiliations", [column])

    op.execute(
        """
        INSERT INTO contact_organization_affiliations (
            id, contact_id, entity_id, domain_id, role, relationship_type, is_primary, status,
            source_refs, provenance, metadata, created_at, updated_at
        )
        SELECT DISTINCT ON (c.id, c.organization_entity_id, notes.domain_id)
            uuid_generate_v4(), c.id, c.organization_entity_id, notes.domain_id, '', 'works_at',
            true, 'active', c.source_refs, c.provenance,
            jsonb_build_object('backfilled_from', 'contacts.organization_entity_id'), now(), now()
        FROM contacts c
        LEFT JOIN contact_domain_notes notes ON notes.contact_id = c.id
        WHERE c.organization_entity_id IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO contact_interactions (
            id, contact_id, domain_id, routed_item_id, interaction_type, channel, direction,
            occurred_at, summary, source_refs, provenance, metadata, created_at, updated_at
        )
        SELECT
            uuid_generate_v4(), notes.contact_id, notes.domain_id, NULL, 'historical_mention',
            NULL, NULL,
            COALESCE(NULLIF(entry.value->>'created_at', '')::timestamptz, notes.created_at),
            COALESCE(NULLIF(entry.value->>'content', ''), NULLIF(entry.value->>'title', ''), notes.notes, 'Historical contact interaction'),
            COALESCE(entry.value->'source_refs', notes.source_refs::jsonb, '[]'::jsonb),
            jsonb_build_object('backfilled_from', 'contact_domain_notes.interaction_log'),
            entry.value, now(), now()
        FROM contact_domain_notes notes
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(notes.interaction_log::jsonb, '[]'::jsonb)) entry(value)
        """
    )

    op.create_table(
        "contact_embeddings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("contact_id", UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("source_text_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("contact_id", "provider", "model", name="uq_contact_embeddings_model"),
    )
    op.create_index("ix_contact_embeddings_contact_id", "contact_embeddings", ["contact_id"])


def downgrade() -> None:
    op.drop_table("contact_embeddings")
    op.drop_table("contact_organization_affiliations")
    op.drop_table("contact_interactions")
    op.drop_index("ix_contact_relationships_status", table_name="contact_relationships")
    op.drop_index("ix_contact_relationships_domain_id", table_name="contact_relationships")
    op.drop_constraint("fk_contact_relationships_domain_id", "contact_relationships", type_="foreignkey")
    for column in ("status", "confidence", "relationship_type", "domain_id"):
        op.drop_column("contact_relationships", column)
