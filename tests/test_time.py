from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.time import home_isoformat, home_timezone
from app.memory.routed_retrieval import _parse_optional_datetime
from app.memory.routed_service import _datetime_from_metadata


def test_home_timezone_defaults_to_eastern_time(monkeypatch) -> None:
    monkeypatch.delenv("HOME_TIMEZONE", raising=False)
    get_settings.cache_clear()
    home_timezone.cache_clear()

    assert get_settings().home_timezone == "America/New_York"
    assert home_timezone().key == "America/New_York"


def test_home_isoformat_renders_in_home_timezone(monkeypatch) -> None:
    monkeypatch.setenv("HOME_TIMEZONE", "America/New_York")
    get_settings.cache_clear()
    home_timezone.cache_clear()

    rendered = home_isoformat(datetime(2026, 7, 1, 12, 0, tzinfo=UTC))

    assert rendered == "2026-07-01T08:00:00-04:00"


def test_timezone_less_routed_event_times_default_to_eastern(monkeypatch) -> None:
    monkeypatch.setenv("HOME_TIMEZONE", "America/New_York")
    get_settings.cache_clear()
    home_timezone.cache_clear()

    extracted = _datetime_from_metadata({"start_at": "2026-07-21T08:00:00"}, "start_at")
    edited = _parse_optional_datetime("2026-01-21T08:00:00")

    assert extracted is not None
    assert extracted.isoformat() == "2026-07-21T08:00:00-04:00"
    assert edited is not None
    assert edited.isoformat() == "2026-01-21T08:00:00-05:00"
