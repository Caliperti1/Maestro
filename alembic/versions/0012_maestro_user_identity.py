"""Separate the Maestro user from CRM contacts.

Revision ID: 0012_maestro_user_identity
Revises: 0011_eastern_event_time_backfill
"""

from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0012_maestro_user_identity"
down_revision = "0011_eastern_event_time_backfill"
branch_labels = None
depends_on = None


USER_NAME = "Chris Aliperti"
USER_EMAIL = "chris.aliperti@praxis-defense.com"


def upgrade() -> None:
    connection = op.get_bind()
    contacts = sa.table(
        "contacts",
        sa.column("id", sa.Uuid()),
        sa.column("normalized_name", sa.String()),
        sa.column("email", sa.String()),
    )
    events = sa.table(
        "calendar_events",
        sa.column("id", sa.Uuid()),
        sa.column("attendees", sa.JSON()),
    )
    links = sa.table(
        "routed_object_links",
        sa.column("routed_item_id", sa.Uuid()),
        sa.column("object_type", sa.String()),
        sa.column("object_id", sa.Uuid()),
    )
    routed_items = sa.table(
        "routed_items",
        sa.column("id", sa.Uuid()),
        sa.column("status", sa.String()),
        sa.column("metadata", sa.JSON()),
    )

    owner_rows = connection.execute(
        sa.select(contacts.c.id).where(
            sa.or_(
                contacts.c.normalized_name == "chris aliperti",
                sa.func.lower(contacts.c.email) == USER_EMAIL,
            )
        )
    ).all()
    owner_ids = {row.id for row in owner_rows}

    for row in connection.execute(sa.select(events.c.id, events.c.attendees)).mappings():
        attendees = row["attendees"] if isinstance(row["attendees"], list) else []
        rewritten: list[dict[str, Any]] = []
        user_seen = False
        changed = False
        for attendee in attendees:
            if not isinstance(attendee, dict):
                continue
            attendee_name = str(attendee.get("name") or "").strip().lower()
            attendee_email = str(attendee.get("email") or "").strip().lower()
            attendee_contact_id = str(attendee.get("contact_id") or "")
            is_user = (
                attendee_contact_id in {str(owner_id) for owner_id in owner_ids}
                or attendee_name == USER_NAME.lower()
                or attendee_email == USER_EMAIL
            )
            if not is_user:
                rewritten.append(attendee)
                continue
            changed = True
            if user_seen:
                continue
            user_seen = True
            user_attendee = {
                **attendee,
                "name": USER_NAME,
                "email": USER_EMAIL,
                "is_user": True,
                "identity": "maestro_user",
            }
            user_attendee.pop("contact_id", None)
            rewritten.append(user_attendee)
        if changed:
            connection.execute(
                events.update().where(events.c.id == row["id"]).values(attendees=rewritten)
            )

    if not owner_ids:
        return

    routed_rows = connection.execute(
        sa.select(links.c.routed_item_id).where(
            links.c.object_type == "contact",
            links.c.object_id.in_(owner_ids),
        )
    ).all()
    for row in routed_rows:
        existing_metadata = connection.execute(
            sa.select(routed_items.c.metadata).where(routed_items.c.id == row.routed_item_id)
        ).scalar_one_or_none()
        metadata = existing_metadata if isinstance(existing_metadata, dict) else {}
        connection.execute(
            routed_items.update()
            .where(routed_items.c.id == row.routed_item_id)
            .values(
                status="ignored",
                metadata={
                    **metadata,
                    "identity_resolution": {
                        "identity": "maestro_user",
                        "action": "suppressed_self_contact",
                        "full_name": USER_NAME,
                        "migration": revision,
                    },
                },
            )
        )

    connection.execute(
        links.delete().where(
            links.c.object_type == "contact",
            links.c.object_id.in_(owner_ids),
        )
    )
    connection.execute(contacts.delete().where(contacts.c.id.in_(owner_ids)))


def downgrade() -> None:
    # Removed self-contact rows were derived data. Recreating them would reintroduce the bug and
    # could not faithfully reconstruct records removed by the upgrade.
    pass
