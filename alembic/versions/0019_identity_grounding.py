"""Add authoritative identity grounding graph.

Revision ID: 0019_identity_grounding
Revises: 0018_context_ingestion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_identity_grounding"
down_revision: str | None = "0018_context_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("node_type", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=240), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("is_authoritative", sa.Boolean(), nullable=False),
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
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_identity_nodes_key", "identity_nodes", ["key"])
    op.create_index("ix_identity_nodes_node_type", "identity_nodes", ["node_type"])
    op.create_index("ix_identity_nodes_domain_id", "identity_nodes", ["domain_id"])
    op.create_index("ix_identity_nodes_entity_id", "identity_nodes", ["entity_id"])
    op.create_index("ix_identity_nodes_is_authoritative", "identity_nodes", ["is_authoritative"])

    op.create_table(
        "identity_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=240), nullable=False),
        sa.Column("subject_node_id", sa.Uuid(), nullable=False),
        sa.Column("object_node_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["object_node_id"], ["identity_nodes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subject_node_id"], ["identity_nodes.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_identity_relationships_key", "identity_relationships", ["key"])
    op.create_index(
        "ix_identity_relationships_subject_node_id",
        "identity_relationships",
        ["subject_node_id"],
    )
    op.create_index(
        "ix_identity_relationships_object_node_id",
        "identity_relationships",
        ["object_node_id"],
    )
    op.create_index(
        "ix_identity_relationships_relationship_type",
        "identity_relationships",
        ["relationship_type"],
    )
    op.create_index(
        "ix_identity_relationships_is_current",
        "identity_relationships",
        ["is_current"],
    )
    _allow_sanitized_domain_egress()


def downgrade() -> None:
    op.drop_table("identity_relationships")
    op.drop_table("identity_nodes")


def _allow_sanitized_domain_egress() -> None:
    """Remove the superseded domain-wide local-only rule from existing sanitized evidence."""

    connection = op.get_bind()
    domains = sa.table(
        "domains",
        sa.column("id", sa.Uuid()),
        sa.column("key", sa.String()),
    )
    domain_ids = list(
        connection.execute(
            sa.select(domains.c.id).where(domains.c.key.in_(["usma", "l3"]))
        ).scalars()
    )
    if not domain_ids:
        return

    registrations = sa.table(
        "source_registrations",
        sa.column("id", sa.Uuid()),
        sa.column("domain_id", sa.Uuid()),
        sa.column("policy", sa.JSON()),
    )
    registration_ids = list(
        connection.execute(
            sa.select(registrations.c.id).where(registrations.c.domain_id.in_(domain_ids))
        ).scalars()
    )
    _update_json_rows(
        connection,
        registrations,
        registrations.c.policy,
        registrations.c.domain_id.in_(domain_ids),
    )

    ingestion_records = sa.table(
        "ingestion_records",
        sa.column("id", sa.Uuid()),
        sa.column("domain_id", sa.Uuid()),
        sa.column("source_registration_id", sa.Uuid()),
        sa.column("policy", sa.JSON()),
    )
    record_filter = ingestion_records.c.domain_id.in_(domain_ids)
    if registration_ids:
        record_filter = sa.or_(
            record_filter,
            ingestion_records.c.source_registration_id.in_(registration_ids),
        )
    _update_json_rows(
        connection,
        ingestion_records,
        ingestion_records.c.policy,
        record_filter,
    )

    for table_name, json_column_name in (
        ("memory_items", "metadata"),
        ("memory_proposals", "metadata"),
        ("memory_proposals", "source_refs"),
        ("routed_items", "metadata"),
        ("routed_items", "source_refs"),
        ("reports", "structured_data"),
        ("workflow_run_log_entries", "metadata"),
    ):
        table = sa.table(
            table_name,
            sa.column("id", sa.Uuid()),
            sa.column("domain_id", sa.Uuid()),
            sa.column(json_column_name, sa.JSON()),
        )
        _update_json_rows(
            connection,
            table,
            table.c[json_column_name],
            table.c.domain_id.in_(domain_ids),
        )


def _update_json_rows(connection, table, json_column, predicate) -> None:
    rows = connection.execute(sa.select(table.c.id, json_column).where(predicate)).all()
    for row in rows:
        payload = row._mapping[json_column.name]
        updated, changed = _replace_local_only(payload)
        if changed:
            connection.execute(
                sa.update(table).where(table.c.id == row._mapping["id"]).values(
                    {json_column.name: updated}
                )
            )


def _replace_local_only(value):
    if isinstance(value, dict):
        changed = False
        result = {}
        is_local_only_policy = value.get("egress_policy") == "local_only"
        for key, item in value.items():
            updated, item_changed = _replace_local_only(item)
            if key == "egress_policy" and item == "local_only":
                updated = "external_allowed"
                item_changed = True
            if key == "requires_human_review" and item is True and is_local_only_policy:
                updated = False
                item_changed = True
            result[key] = updated
            changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        changed = False
        result = []
        for item in value:
            updated, item_changed = _replace_local_only(item)
            result.append(updated)
            changed = changed or item_changed
        return result, changed
    return value, False
