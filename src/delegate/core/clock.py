"""Time helpers. The service takes a ``Clock`` so tests can control lease expiry."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Fixed-width UTC format so string comparison in SQL is chronological."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def plus_minutes(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=minutes)
