"""Add contact hydration jobs and organization intelligence.

Revision ID: 0015_contact_hydration
Revises: 0014_contact_intelligence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0015_contact_hydration"
down_revision: str | None = "0014_contact_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.String(length=240), nullable=False),
        sa.Column("normalized_alias", sa.String(length=260), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_alias", name="uq_organization_aliases_normalized_alias"),
    )
    op.create_index(op.f("ix_organization_aliases_entity_id"), "organization_aliases", ["entity_id"])
    op.create_index(
        op.f("ix_organization_aliases_normalized_alias"),
        "organization_aliases",
        ["normalized_alias"],
    )
    op.create_table(
        "organization_embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("source_text_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id", "provider", "model", name="uq_organization_embeddings_model"),
    )
    op.create_index(op.f("ix_organization_embeddings_entity_id"), "organization_embeddings", ["entity_id"])
    op.create_table(
        "contact_hydration_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("page_token", sa.Text(), nullable=True),
        sa.Column("page_size", sa.Integer(), nullable=False),
        sa.Column("max_messages", sa.Integer(), nullable=False),
        sa.Column("max_contacts", sa.Integer(), nullable=False),
        sa.Column("messages_scanned", sa.Integer(), nullable=False),
        sa.Column("candidates_found", sa.Integer(), nullable=False),
        sa.Column("promoted_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False),
        sa.Column("local_model_profile", sa.String(length=240), nullable=False),
        sa.Column("cloud_model_profile", sa.String(length=240), nullable=False),
        sa.Column("max_cloud_calls", sa.Integer(), nullable=False),
        sa.Column("cloud_calls", sa.Integer(), nullable=False),
        sa.Column("enable_enrichment", sa.Boolean(), nullable=False),
        sa.Column("enable_cloud_fallback", sa.Boolean(), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("domain_id", "task_id", "mode", "status", "lease_owner", "lease_expires_at"):
        op.create_index(op.f(f"ix_contact_hydration_jobs_{column}"), "contact_hydration_jobs", [column])
    op.create_table(
        "contact_hydration_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_type", sa.String(length=40), nullable=False),
        sa.Column("identity_key", sa.String(length=360), nullable=False),
        sa.Column("display_name", sa.String(length=240), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("existing_object_id", sa.Uuid(), nullable=True),
        sa.Column("promoted_object_id", sa.Uuid(), nullable=True),
        sa.Column("routed_item_id", sa.Uuid(), nullable=True),
        sa.Column("proposed_data", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["contact_hydration_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["routed_item_id"], ["routed_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "candidate_type",
            "identity_key",
            name="uq_contact_hydration_candidate_identity",
        ),
    )
    for column in (
        "job_id",
        "candidate_type",
        "identity_key",
        "display_name",
        "action",
        "status",
        "existing_object_id",
        "promoted_object_id",
        "routed_item_id",
    ):
        op.create_index(
            op.f(f"ix_contact_hydration_candidates_{column}"),
            "contact_hydration_candidates",
            [column],
        )


def downgrade() -> None:
    op.drop_table("contact_hydration_candidates")
    op.drop_table("contact_hydration_jobs")
    op.drop_table("organization_embeddings")
    op.drop_table("organization_aliases")
