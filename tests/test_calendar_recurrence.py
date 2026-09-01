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


def test_monthly_recurrence_supports_last_day_of_each_month() -> None:
    values = {
        "start_at": datetime(2026, 9, 30, 21, 0, tzinfo=UTC),
        "recurrence_rule": "FREQ=MONTHLY;BYMONTHDAY=-1",
        "timezone_name": "America/New_York",
    }

    assert event_occurs_on(target_date=date(2026, 9, 30), **values)
    assert event_occurs_on(target_date=date(2026, 10, 31), **values)
    assert event_occurs_on(target_date=date(2026, 11, 30), **values)
    assert event_occurs_on(target_date=date(2027, 2, 28), **values)
    assert not event_occurs_on(target_date=date(2026, 10, 30), **values)


def test_query_calendar_date_understands_next_weekday() -> None:
    assert query_calendar_date(
        "What's on my schedule next Monday?",
        now=datetime(2026, 8, 20, 16, 0, tzinfo=UTC),
    ) == date(2026, 8, 24)


def test_query_calendar_date_understands_written_month_date() -> None:
    assert query_calendar_date(
        "Collaborative Autonomy Standup on August 31, 2026 at 11:00 AM Eastern",
        now=datetime(2026, 8, 20, 16, 0, tzinfo=UTC),
    ) == date(2026, 8, 31)
