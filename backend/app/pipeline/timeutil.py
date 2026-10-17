"""Dates as the sources give them, turned into aware UTC datetimes."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

IST = timezone(timedelta(hours=5, minutes=30))

_HAS_TIME = re.compile(r"\d{1,2}:\d{2}|\d\s*(am|pm)\b", re.I)


def as_utc(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_iso(value: object, zone: str | None = None) -> datetime | None:
    """An ISO-8601 time as UTC. A value with no offset is read in `zone`
    (an IANA name), else as UTC. None when absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(zone) or timezone.utc)
    return parsed.astimezone(timezone.utc)


def _zone(name: str | None):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def parse_loose(value: object, zone: str | None = "Asia/Kolkata") -> tuple[datetime | None, bool]:
    """(UTC time, day only?) from whatever a model or a page wrote:
    "2026-10-14T18:30", "2026-10-14", "14 Oct 2026, 6 pm".

    A time with no zone is read in `zone` (India by default). A day with no
    time is stored as midnight IST and flagged, so it is shown as a day,
    never as a made-up hour.
    """
    if not isinstance(value, str) or not value.strip():
        return None, False
    text = value.strip()[:80]
    has_time = bool(_HAS_TIME.search(text))
    parsed = parse_iso(text, zone)
    if parsed is None:
        try:
            import dateparser
            parsed = dateparser.parse(text, settings={
                "TIMEZONE": zone or "UTC", "RETURN_AS_TIMEZONE_AWARE": True})
        except Exception:  # noqa: BLE001 - a bad date must not fail the run
            parsed = None
        if parsed is None:
            return None, False
    if not has_time:
        day = parsed.astimezone(_zone(zone) or IST).date() if parsed.tzinfo else parsed.date()
        return datetime.combine(day, time.min, IST).astimezone(timezone.utc), True
    return as_utc(parsed), False


def ist_day(value: datetime) -> date:
    return as_utc(value).astimezone(IST).date()


def day_bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    """[since, until) in UTC for inclusive Indian days."""
    since = datetime.combine(start, time.min, IST).astimezone(timezone.utc) if start else None
    until = (datetime.combine(end + timedelta(days=1), time.min, IST).astimezone(timezone.utc)
             if end else None)
    return since, until
