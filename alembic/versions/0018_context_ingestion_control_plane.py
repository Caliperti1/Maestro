"""Add the context ingestion control plane and retire superseded memories.

Revision ID: 0018_context_ingestion
Revises: 0017_calendar_event_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_context_ingestion"
down_revision: str | None = "0017_calendar_event_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_registrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("source_system", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=240), nullable=False),
        sa.Column("adapter_type", sa.String(length=120), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_source_registrations_domain_id", "source_registrations", ["domain_id"])
    op.create_index(
        "ix_source_registrations_source_system",
        "source_registrations",
        ["source_system"],
    )
    op.create_index("ix_source_registrations_is_active", "source_registrations", ["is_active"])

    op.create_table(
        "source_checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_registration_id", sa.Uuid(), nullable=False),
        sa.Column("cursor_key", sa.String(length=160), nullable=False),
        sa.Column("cursor_value", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_registration_id"],
            ["source_registrations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_registration_id",
            "cursor_key",
            name="uq_source_checkpoints_registration_cursor",
        ),
    )
    op.create_index(
        "ix_source_checkpoints_source_registration_id",
        "source_checkpoints",
        ["source_registration_id"],
    )
    op.create_index("ix_source_checkpoints_status", "source_checkpoints", ["status"])

    op.create_table(
        "ingestion_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_registration_id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("seed_package_id", sa.Uuid(), nullable=True),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("external_id", sa.String(length=500), nullable=False),
        sa.Column("source_version", sa.String(length=160), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["seed_package_id"], ["seed_packages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_registration_id"],
            ["source_registrations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_registration_id",
            "external_id",
            "source_version",
            name="uq_ingestion_records_source_object_version",
        ),
    )
    op.create_index(
        "ix_ingestion_records_source_registration_id",
        "ingestion_records",
        ["source_registration_id"],
    )
    op.create_index("ix_ingestion_records_domain_id", "ingestion_records", ["domain_id"])
    op.create_index(
        "ix_ingestion_records_seed_package_id",
        "ingestion_records",
        ["seed_package_id"],
    )
    op.create_index("ix_ingestion_records_artifact_id", "ingestion_records", ["artifact_id"])
    op.create_index("ix_ingestion_records_content_hash", "ingestion_records", ["content_hash"])
    op.create_index("ix_ingestion_records_status", "ingestion_records", ["status"])

    # A supersedes edge means the target is historical evidence, not active truth.
    op.execute(
        sa.text(
            """
            UPDATE memory_items AS old_memory
            SET valid_until = supersession.first_superseded_at,
                updated_at = now()
            FROM (
                SELECT target_memory_id, MIN(created_at) AS first_superseded_at
                FROM memory_links
                WHERE relation_type = 'supersedes'
                GROUP BY target_memory_id
            ) AS supersession
            WHERE old_memory.id = supersession.target_memory_id
              AND old_memory.valid_until IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_records_status", table_name="ingestion_records")
    op.drop_index("ix_ingestion_records_content_hash", table_name="ingestion_records")
    op.drop_index("ix_ingestion_records_artifact_id", table_name="ingestion_records")
    op.drop_index("ix_ingestion_records_seed_package_id", table_name="ingestion_records")
    op.drop_index("ix_ingestion_records_domain_id", table_name="ingestion_records")
    op.drop_index("ix_ingestion_records_source_registration_id", table_name="ingestion_records")
    op.drop_table("ingestion_records")
    op.drop_index("ix_source_checkpoints_status", table_name="source_checkpoints")
    op.drop_index("ix_source_checkpoints_source_registration_id", table_name="source_checkpoints")
    op.drop_table("source_checkpoints")
    op.drop_index("ix_source_registrations_is_active", table_name="source_registrations")
    op.drop_index("ix_source_registrations_source_system", table_name="source_registrations")
    op.drop_index("ix_source_registrations_domain_id", table_name="source_registrations")
    op.drop_table("source_registrations")
