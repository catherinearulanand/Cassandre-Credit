"""Injectable clock. Domain code never calls ``datetime.now()``; it receives a Clock."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> date: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def today(self) -> date:
        return self.now().date()


class FixedClock:
    """Deterministic, advanceable clock for tests and simulations."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def advance(self, *, days: int = 0, seconds: float = 0) -> None:
        self._now = self._now + timedelta(days=days, seconds=seconds)

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("timezone-aware datetime required")
        self._now = when
