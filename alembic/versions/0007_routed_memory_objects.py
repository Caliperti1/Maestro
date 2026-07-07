"""add routed memory object stores

Revision ID: 0007_routed_memory_objects
Revises: 0006_conversation_metadata
Create Date: 2026-07-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_routed_memory_objects"
down_revision: str | None = "0006_conversation_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location", sa.String(length=320), nullable=True),
        sa.Column("attendees", sa.JSON(), nullable=False),
        sa.Column("supporting_refs", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_calendar_events_domain_id", "calendar_events", ["domain_id"])
    op.create_index("ix_calendar_events_end_at", "calendar_events", ["end_at"])
    op.create_index("ix_calendar_events_start_at", "calendar_events", ["start_at"])
    op.create_index("ix_calendar_events_status", "calendar_events", ["status"])
    op.create_index("ix_calendar_events_title", "calendar_events", ["title"])

    op.create_table(
        "entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("normalized_name", sa.String(length=260), nullable=False),
        sa.Column("website", sa.String(length=320), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_name"),
    )
    op.create_index("ix_entities_name", "entities", ["name"])
    op.create_index("ix_entities_status", "entities", ["status"])

    op.create_table(
        "entity_domain_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("interaction_log", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id", "domain_id", name="uq_entity_domain_notes_entity_domain"),
    )
    op.create_index("ix_entity_domain_notes_domain_id", "entity_domain_notes", ["domain_id"])
    op.create_index("ix_entity_domain_notes_entity_id", "entity_domain_notes", ["entity_id"])

    op.create_table(
        "contacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("normalized_name", sa.String(length=260), nullable=False),
        sa.Column("phone", sa.String(length=80), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("linkedin", sa.String(length=320), nullable=True),
        sa.Column("organization_entity_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("origination", sa.Text(), nullable=True),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_event_ids", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["organization_entity_id"], ["entities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_contacts_last_contact_at", "contacts", ["last_contact_at"])
    op.create_index("ix_contacts_name", "contacts", ["name"])
    op.create_index("ix_contacts_normalized_name", "contacts", ["normalized_name"])
    op.create_index("ix_contacts_organization_entity_id", "contacts", ["organization_entity_id"])
    op.create_index("ix_contacts_status", "contacts", ["status"])

    op.create_table(
        "contact_domain_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("interaction_log", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contact_id", "domain_id", name="uq_contact_domain_notes_contact_domain"),
    )
    op.create_index("ix_contact_domain_notes_contact_id", "contact_domain_notes", ["contact_id"])
    op.create_index("ix_contact_domain_notes_domain_id", "contact_domain_notes", ["domain_id"])

    op.create_table(
        "contact_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("related_contact_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contact_relationships_contact_id", "contact_relationships", ["contact_id"])
    op.create_index("ix_contact_relationships_related_contact_id", "contact_relationships", ["related_contact_id"])

    op.create_table(
        "todos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("todo_type", sa.String(length=80), nullable=False),
        sa.Column("owner_type", sa.String(length=80), nullable=False),
        sa.Column("owner_ref", sa.String(length=240), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_todos_domain_id", "todos", ["domain_id"])
    op.create_index("ix_todos_due_at", "todos", ["due_at"])
    op.create_index("ix_todos_status", "todos", ["status"])
    op.create_index("ix_todos_title", "todos", ["title"])
    op.create_index("ix_todos_todo_type", "todos", ["todo_type"])

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
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ideas_domain_id", "ideas", ["domain_id"])
    op.create_index("ix_ideas_status", "ideas", ["status"])
    op.create_index("ix_ideas_title", "ideas", ["title"])

    op.create_table(
        "decision_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_decision_records_domain_id", "decision_records", ["domain_id"])
    op.create_index("ix_decision_records_status", "decision_records", ["status"])
    op.create_index("ix_decision_records_title", "decision_records", ["title"])

    op.create_table(
        "routed_object_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routed_item_id", sa.Uuid(), nullable=False),
        sa.Column("object_type", sa.String(length=80), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["routed_item_id"], ["routed_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("routed_item_id", "object_type", "object_id", name="uq_routed_object_link"),
    )
    op.create_index("ix_routed_object_links_object_id", "routed_object_links", ["object_id"])
    op.create_index("ix_routed_object_links_object_type", "routed_object_links", ["object_type"])
    op.create_index("ix_routed_object_links_routed_item_id", "routed_object_links", ["routed_item_id"])

    op.create_table(
        "routed_object_change_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("object_type", sa.String(length=80), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("routed_item_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["routed_item_id"], ["routed_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_routed_object_change_log_object_id", "routed_object_change_log", ["object_id"])
    op.create_index("ix_routed_object_change_log_object_type", "routed_object_change_log", ["object_type"])
    op.create_index("ix_routed_object_change_log_routed_item_id", "routed_object_change_log", ["routed_item_id"])


def downgrade() -> None:
    for table_name in (
        "routed_object_change_log",
        "routed_object_links",
        "decision_records",
        "ideas",
        "todos",
        "contact_relationships",
        "contact_domain_notes",
        "contacts",
        "entity_domain_notes",
        "entities",
        "calendar_events",
    ):
        op.drop_table(table_name)
