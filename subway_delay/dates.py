from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def parse_manual_date(value: str | None) -> date | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("--date must be in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--date must be in YYYY-MM-DD format.") from exc


def resolve_capture_date(
    timezone_name: str,
    explicit_date: date | None = None,
    now: datetime | None = None,
) -> date:
    if explicit_date is not None:
        return explicit_date

    timezone = ZoneInfo(timezone_name)
    current = now.astimezone(timezone) if now is not None else datetime.now(timezone)
    weekday = current.weekday()

    if weekday == 0:
        delta_days = 3
    elif weekday == 5:
        delta_days = 1
    elif weekday == 6:
        delta_days = 2
    else:
        delta_days = 1

    return current.date() - timedelta(days=delta_days)
