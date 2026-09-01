"""Small, auditable helpers for recurring calendar validation and lookup."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.time import home_timezone

WEEKDAYS = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}
WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
MONTH_NAMES = {
    name: number
    for number, names in enumerate(
        (
            (),
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        )
    )
    for name in names
}


def recurrence_options(rule: str | None) -> dict[str, str]:
    if not rule:
        return {}
    return {
        key.strip().upper(): value.strip().upper()
        for part in rule.split(";")
        if "=" in part
        for key, value in [part.split("=", 1)]
        if key.strip() and value.strip()
    }


def normalize_recurrence_rule(
    rule: str,
    *,
    start_at: datetime,
    source_text: str = "",
) -> str:
    options = recurrence_options(rule)
    if options.get("FREQ") not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise ValueError("The recurring schedule needs a supported frequency.")
    until = recurrence_until(options.get("UNTIL"))
    if until is None or until >= start_at.astimezone(UTC):
        return _serialize(options)
    if re.search(
        r"\b(?:through|until|to)\s+(?:the\s+)?end\s+of\s+(?:the\s+)?year\b",
        source_text,
        re.IGNORECASE,
    ):
        options["UNTIL"] = f"{start_at.year}1231T235959Z"
        return _serialize(options)
    raise ValueError(
        "The recurring schedule ends before its first occurrence. What end date should I use?"
    )


def recurrence_until(value: str | None) -> datetime | None:
    if not value:
        return None
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
        return parsed
    raise ValueError("The recurring schedule has an invalid UNTIL date.")


def query_calendar_date(query: str, *, now: datetime | None = None) -> date | None:
    normalized = " ".join(query.lower().split())
    local_now = (now or datetime.now(UTC)).astimezone(home_timezone())
    today = local_now.date()
    if re.search(r"\btoday\b", normalized):
        return today
    if re.search(r"\btomorrow\b", normalized):
        return today + timedelta(days=1)
    weekday_match = re.search(
        r"\b(?:(next|this)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        normalized,
    )
    if weekday_match:
        target_weekday = WEEKDAY_NAMES[weekday_match.group(2)]
        delta = (target_weekday - today.weekday()) % 7
        if weekday_match.group(1) == "next" and delta == 0:
            delta = 7
        return today + timedelta(days=delta)
    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", normalized)
    if iso_match:
        try:
            return date(*(int(value) for value in iso_match.groups()))
        except ValueError:
            return None
    month_match = re.search(
        r"\b(" + "|".join(MONTH_NAMES) + r")\s+(\d{1,2})(?:,?\s+(20\d{2}))?\b",
        normalized,
    )
    if month_match:
        month = MONTH_NAMES[month_match.group(1)]
        day = int(month_match.group(2))
        year = int(month_match.group(3) or today.year)
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if month_match.group(3) is None and candidate < today - timedelta(days=7):
            try:
                candidate = date(year + 1, month, day)
            except ValueError:
                return None
        return candidate
    numeric_match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d{2}|\d{2}))?\b", normalized)
    if numeric_match:
        month, day = (int(value) for value in numeric_match.groups()[:2])
        year_text = numeric_match.group(3)
        year = int(year_text) if year_text else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def event_occurs_on(
    *,
    start_at: datetime | None,
    recurrence_rule: str | None,
    target_date: date,
    timezone_name: str | None,
) -> bool:
    if start_at is None:
        return False
    timezone = _timezone(timezone_name)
    local_start = start_at.astimezone(timezone)
    if not recurrence_rule:
        return local_start.date() == target_date
    options = recurrence_options(recurrence_rule)
    try:
        until = recurrence_until(options.get("UNTIL"))
    except ValueError:
        return False
    if target_date < local_start.date():
        return False
    if until is not None and target_date > until.astimezone(timezone).date():
        return False
    interval = _positive_int(options.get("INTERVAL"), default=1)
    elapsed_days = (target_date - local_start.date()).days
    frequency = options.get("FREQ")
    if frequency == "DAILY":
        return elapsed_days % interval == 0
    if frequency == "WEEKLY":
        weekdays = {
            WEEKDAYS[value] for value in options.get("BYDAY", "").split(",") if value in WEEKDAYS
        } or {local_start.weekday()}
        return target_date.weekday() in weekdays and (elapsed_days // 7) % interval == 0
    if frequency == "MONTHLY":
        months = (target_date.year - local_start.year) * 12 + target_date.month - local_start.month
        month_days = {
            int(value)
            for value in options.get("BYMONTHDAY", str(local_start.day)).split(",")
            if value.lstrip("-").isdigit()
        }
        return (
            months >= 0
            and months % interval == 0
            and _matches_month_day(target_date, month_days)
        )
    if frequency == "YEARLY":
        years = target_date.year - local_start.year
        return (
            years >= 0
            and years % interval == 0
            and (
                target_date.month,
                target_date.day,
            )
            == (local_start.month, local_start.day)
        )
    return False


def _serialize(options: dict[str, str]) -> str:
    preferred = ("FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL")
    keys = [key for key in preferred if key in options]
    keys.extend(sorted(set(options) - set(keys)))
    return ";".join(f"{key}={options[key]}" for key in keys)


def _positive_int(value: str | None, *, default: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _matches_month_day(target_date: date, month_days: set[int]) -> bool:
    days_in_month = monthrange(target_date.year, target_date.month)[1]
    resolved_days = {
        value if value > 0 else days_in_month + value + 1
        for value in month_days
        if value != 0
    }
    return target_date.day in resolved_days


def _timezone(name: str | None) -> ZoneInfo:
    if not name:
        return home_timezone()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return home_timezone()
