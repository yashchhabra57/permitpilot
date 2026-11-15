"""Time helpers. All timestamps are naive UTC so SQLite and PostgreSQL behave the same."""

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
