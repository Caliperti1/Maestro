"""Consolidate Perti and add calendar/organization intelligence.

Revision ID: 0016_perti_calendar_intelligence
Revises: 0015_contact_hydration
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_perti_calendar_intelligence"
down_revision: str | None = "0015_contact_hydration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_TABLES = (
    "agents",
    "calendar_events",
    "contact_domain_notes",
    "contact_hydration_jobs",
    "contact_interactions",
    "contact_organization_affiliations",
    "contact_relationships",
    "conversations",
    "decision_records",
    "entity_domain_notes",
    "ideas",
    "memory_items",
    "memory_proposals",
    "reports",
    "routed_items",
    "scheduled_runs",
    "seed_packages",
    "skills",
    "tasks",
    "todos",
    "tool_connections",
    "workflow_definitions",
    "workflow_notifications",
    "workflow_queue_items",
    "workflow_run_log_entries",
    "workflow_runs",
)


def upgrade() -> None:
    connection = op.get_bind()
    perti_id = connection.execute(
        sa.text("SELECT id FROM domains WHERE key = 'ophi'")
    ).scalar_one_or_none()
    irad_id = connection.execute(
        sa.text("SELECT id FROM domains WHERE key = 'personal-irad-projects'")
    ).scalar_one_or_none()
    if perti_id is not None and irad_id is not None:
        _merge_tool_connections(connection, perti_id=perti_id, irad_id=irad_id)
        for table in DOMAIN_TABLES:
            connection.execute(
                sa.text(f"UPDATE {table} SET domain_id = :perti_id WHERE domain_id = :irad_id"),
                {"perti_id": perti_id, "irad_id": irad_id},
            )
        connection.execute(sa.text("DELETE FROM domains WHERE id = :irad_id"), {"irad_id": irad_id})
    if perti_id is not None:
        connection.execute(
            sa.text(
                """
                UPDATE domains
                SET key = 'perti-laboratories',
                    name = 'Perti Laboratories',
                    description = 'Perti Laboratories software products, applied research, market development, and technical operations.'
                WHERE id = :perti_id
                """
            ),
            {"perti_id": perti_id},
        )

    op.add_column(
        "calendar_events",
        sa.Column("timezone", sa.String(length=80), server_default="America/New_York", nullable=False),
    )
    op.add_column(
        "calendar_events",
        sa.Column("all_day", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("calendar_events", sa.Column("recurrence_rule", sa.Text(), nullable=True))
    op.add_column("calendar_events", sa.Column("conferencing_url", sa.String(length=640), nullable=True))
    op.add_column("calendar_events", sa.Column("organizer_name", sa.String(length=240), nullable=True))
    op.add_column("calendar_events", sa.Column("organizer_email", sa.String(length=320), nullable=True))
    op.add_column("calendar_events", sa.Column("external_provider", sa.String(length=80), nullable=True))
    op.add_column("calendar_events", sa.Column("external_calendar_id", sa.String(length=320), nullable=True))
    op.add_column("calendar_events", sa.Column("external_event_id", sa.String(length=320), nullable=True))
    op.add_column("calendar_events", sa.Column("external_etag", sa.String(length=320), nullable=True))
    op.add_column(
        "calendar_events",
        sa.Column("sync_status", sa.String(length=40), server_default="local", nullable=False),
    )
    op.add_column("calendar_events", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))
    for column in ("external_provider", "external_event_id", "sync_status"):
        op.create_index(op.f(f"ix_calendar_events_{column}"), "calendar_events", [column])

    op.create_table(
        "calendar_event_attendees",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("normalized_identity", sa.String(length=360), nullable=False),
        sa.Column("attendee_type", sa.String(length=40), server_default="required", nullable=False),
        sa.Column("response_status", sa.String(length=40), server_default="needs_action", nullable=False),
        sa.Column("is_organizer", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_user", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["event_id"], ["calendar_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "normalized_identity", name="uq_calendar_attendee_identity"),
    )
    for column in ("event_id", "contact_id", "email", "normalized_identity"):
        op.create_index(op.f(f"ix_calendar_event_attendees_{column}"), "calendar_event_attendees", [column])

    op.create_table(
        "calendar_event_organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=80), server_default="related", nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["calendar_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "entity_id", "role", name="uq_calendar_event_organization"),
    )
    op.create_index(op.f("ix_calendar_event_organizations_event_id"), "calendar_event_organizations", ["event_id"])
    op.create_index(op.f("ix_calendar_event_organizations_entity_id"), "calendar_event_organizations", ["entity_id"])

    op.create_table(
        "organization_identifiers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("identifier_type", sa.String(length=80), nullable=False),
        sa.Column("value", sa.String(length=640), nullable=False),
        sa.Column("normalized_value", sa.String(length=640), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identifier_type", "normalized_value", name="uq_organization_identifier"),
    )
    for column in ("entity_id", "identifier_type", "normalized_value"):
        op.create_index(op.f(f"ix_organization_identifiers_{column}"), "organization_identifiers", [column])

    op.create_table(
        "organization_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("related_entity_id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("relationship_type", sa.String(length=80), server_default="associated_with", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0.8", nullable=False),
        sa.Column("status", sa.String(length=40), server_default="active", nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entity_id",
            "related_entity_id",
            "domain_id",
            "relationship_type",
            name="uq_organization_relationship_context",
        ),
    )
    for column in ("entity_id", "related_entity_id", "domain_id", "status"):
        op.create_index(op.f(f"ix_organization_relationships_{column}"), "organization_relationships", [column])

    op.add_column("contact_interactions", sa.Column("calendar_event_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_contact_interactions_calendar_event_id",
        "contact_interactions",
        "calendar_events",
        ["calendar_event_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_contact_interactions_calendar_event_id"),
        "contact_interactions",
        ["calendar_event_id"],
    )
    op.create_unique_constraint(
        "uq_contact_calendar_interaction",
        "contact_interactions",
        ["contact_id", "calendar_event_id"],
    )


def _merge_tool_connections(
    connection: sa.engine.Connection,
    *,
    perti_id: uuid.UUID,
    irad_id: uuid.UUID,
) -> None:
    """Preserve tool history and configuration when both domains use the same tool."""
    parameters = {"perti_id": perti_id, "irad_id": irad_id}
    connection.execute(
        sa.text(
            """
            UPDATE tool_calls AS calls
            SET tool_connection_id = target.id
            FROM tool_connections AS source
            JOIN tool_connections AS target
              ON target.domain_id = :perti_id
             AND target.tool_key = source.tool_key
            WHERE source.domain_id = :irad_id
              AND calls.tool_connection_id = source.id
            """
        ),
        parameters,
    )
    connection.execute(
        sa.text(
            """
            UPDATE tool_connections AS target
            SET config = source.config || target.config,
                is_active = source.is_active OR target.is_active,
                updated_at = now()
            FROM tool_connections AS source
            WHERE target.domain_id = :perti_id
              AND source.domain_id = :irad_id
              AND target.tool_key = source.tool_key
            """
        ),
        parameters,
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM tool_connections AS source
            USING tool_connections AS target
            WHERE source.domain_id = :irad_id
              AND target.domain_id = :perti_id
              AND source.tool_key = target.tool_key
            """
        ),
        parameters,
    )


def downgrade() -> None:
    op.drop_constraint("uq_contact_calendar_interaction", "contact_interactions", type_="unique")
    op.drop_index(op.f("ix_contact_interactions_calendar_event_id"), table_name="contact_interactions")
    op.drop_constraint(
        "fk_contact_interactions_calendar_event_id",
        "contact_interactions",
        type_="foreignkey",
    )
    op.drop_column("contact_interactions", "calendar_event_id")
    op.drop_table("organization_relationships")
    op.drop_table("organization_identifiers")
    op.drop_table("calendar_event_organizations")
    op.drop_table("calendar_event_attendees")
    for column in ("sync_status", "external_event_id", "external_provider"):
        op.drop_index(op.f(f"ix_calendar_events_{column}"), table_name="calendar_events")
    for column in (
        "last_synced_at",
        "sync_status",
        "external_etag",
        "external_event_id",
        "external_calendar_id",
        "external_provider",
        "organizer_email",
        "organizer_name",
        "conferencing_url",
        "recurrence_rule",
        "all_day",
        "timezone",
    ):
        op.drop_column("calendar_events", column)

    connection = op.get_bind()
    perti_id = connection.execute(
        sa.text("SELECT id FROM domains WHERE key = 'perti-laboratories'")
    ).scalar_one_or_none()
    if perti_id is not None:
        connection.execute(
            sa.text(
                """
                UPDATE domains
                SET key = 'ophi',
                    name = 'Ophi',
                    description = 'Ophi software products, research, and technical operations.'
                WHERE id = :perti_id
                """
            ),
            {"perti_id": perti_id},
        )
    personal_irad_exists = connection.execute(
        sa.text("SELECT 1 FROM domains WHERE key = 'personal-irad-projects'")
    ).scalar_one_or_none()
    if personal_irad_exists is None:
        connection.execute(
            sa.text(
                """
                INSERT INTO domains (id, key, name, description, is_active, created_at, updated_at)
                VALUES (
                    :domain_id,
                    'personal-irad-projects',
                    'Personal IRAD Projects',
                    'Independent research, invention, and build projects.',
                    true,
                    now(),
                    now()
                )
                """
            ),
            {"domain_id": uuid.uuid4()},
        )
