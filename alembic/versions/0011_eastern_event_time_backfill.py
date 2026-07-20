"""Interpret legacy timezone-less routed event times in Eastern Time.

Revision ID: 0011_eastern_event_time_backfill
Revises: 0010_skills_registry
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

revision = "0011_eastern_event_time_backfill"
down_revision = "0010_skills_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    events = sa.table(
        "calendar_events",
        sa.column("id", sa.Uuid()),
        sa.column("start_at", sa.DateTime(timezone=True)),
        sa.column("end_at", sa.DateTime(timezone=True)),
        sa.column("metadata", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(events.c.id, events.c.start_at, events.c.end_at, events.c.metadata)
    ).mappings()
    eastern = ZoneInfo("America/New_York")

    for row in rows:
        metadata = row["metadata"] if isinstance(row["metadata"], dict) else {}
        structured = metadata.get("structured_data")
        structured = structured if isinstance(structured, dict) else {}
        raw_start = structured.get("start_at")
        naive_start = _timezone_less_datetime(raw_start)
        stored_start = row["start_at"]
        if naive_start is None or stored_start is None:
            continue
        stored_utc = (
            stored_start.replace(tzinfo=UTC)
            if stored_start.tzinfo is None
            else stored_start.astimezone(UTC)
        )
        legacy_utc = naive_start.replace(tzinfo=UTC)
        if stored_utc != legacy_utc:
            continue

        corrected_start = naive_start.replace(tzinfo=eastern).astimezone(UTC)
        shift = corrected_start - stored_utc
        stored_end = row["end_at"]
        corrected_end = stored_end + shift if stored_end is not None else None
        connection.execute(
            events.update()
            .where(events.c.id == row["id"])
            .values(start_at=corrected_start, end_at=corrected_end)
        )


def downgrade() -> None:
    # The upgrade only repairs values matching the legacy UTC interpretation. Reversing it later
    # would be unable to distinguish corrected legacy values from intentional Eastern events.
    pass


def _timezone_less_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if "T" not in text and " " not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is None else None
