from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.core.config import get_settings


@lru_cache
def home_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().home_timezone)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_home_timezone(value: datetime) -> datetime:
    return ensure_aware_utc(value).astimezone(home_timezone())


def home_isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return to_home_timezone(value).isoformat()
