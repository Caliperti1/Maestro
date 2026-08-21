from datetime import UTC, date, datetime

import pytest

from app.memory.calendar_recurrence import (
    event_occurs_on,
    normalize_recurrence_rule,
    query_calendar_date,
)


def test_normalize_recurrence_repairs_end_of_year_to_start_year() -> None:
    result = normalize_recurrence_rule(
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20251231T235959Z",
        start_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        source_text="Run every Monday through Thursday until the end of the year.",
    )

    assert result == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20261231T235959Z"


def test_normalize_recurrence_rejects_expired_rule_without_safe_repair() -> None:
    with pytest.raises(ValueError, match="ends before its first occurrence"):
        normalize_recurrence_rule(
            "FREQ=WEEKLY;BYDAY=MO;UNTIL=20251231T235959Z",
            start_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        )


def test_recurring_event_occurs_on_configured_weekdays() -> None:
    values = {
        "start_at": datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20261231T235959Z",
        "timezone_name": "America/New_York",
    }

    assert event_occurs_on(target_date=date(2026, 8, 31), **values)
    assert not event_occurs_on(target_date=date(2026, 8, 30), **values)


def test_query_calendar_date_understands_next_weekday() -> None:
    assert query_calendar_date(
        "What's on my schedule next Monday?",
        now=datetime(2026, 8, 20, 16, 0, tzinfo=UTC),
    ) == date(2026, 8, 24)
