"""Bitemporal store: every fact has valid time (business) and recorded time (system).

Answers "as at business date D, as known on system time S". Corrections never overwrite:
they close the recorded interval of the old belief and open a new one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Generic, TypeVar

from platform_core.clock import Clock

T = TypeVar("T")

DATE_MAX = date(9999, 12, 31)
TS_MAX = datetime.max.replace(tzinfo=None)


@dataclass(frozen=True)
class BitemporalRow(Generic[T]):
    key: str
    value: T
    valid_from: date
    valid_to: date  # exclusive
    recorded_from: datetime
    recorded_to: datetime | None  # None = current belief
    version: int
    cause_event_id: str | None

    def believed_at(self, known_at: datetime) -> bool:
        return self.recorded_from <= known_at and (
            self.recorded_to is None or known_at < self.recorded_to
        )

    def valid_on(self, as_of: date) -> bool:
        return self.valid_from <= as_of < self.valid_to


class BitemporalStore(Generic[T]):
    """In-memory bitemporal table. The Postgres adapter implements the same semantics."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._rows: dict[str, list[BitemporalRow[T]]] = {}
        self._last_recorded: datetime | None = None

    def assert_fact(
        self,
        key: str,
        value: T,
        valid_from: date,
        valid_to: date = DATE_MAX,
        cause_event_id: str | None = None,
    ) -> BitemporalRow[T]:
        """Record that ``value`` holds over [valid_from, valid_to), as of now (system time).

        Overlapping current beliefs are closed in system time and re-asserted for the
        non-overlapping remainder so history stays answerable.
        """
        if valid_to <= valid_from:
            raise ValueError("valid_to must be after valid_from")
        # Recorded time never goes backwards, even if the system clock is stepped back.
        now = (
            max(self._clock.now(), self._last_recorded)
            if self._last_recorded
            else self._clock.now()
        )
        self._last_recorded = now
        rows = self._rows.setdefault(key, [])
        version = max((r.version for r in rows), default=0) + 1
        new_rows: list[BitemporalRow[T]] = []
        for i, r in enumerate(rows):
            if r.recorded_to is not None:
                continue
            overlaps = r.valid_from < valid_to and valid_from < r.valid_to
            if not overlaps:
                continue
            rows[i] = replace(r, recorded_to=now)
            if r.valid_from < valid_from:
                new_rows.append(
                    replace(r, valid_to=valid_from, recorded_from=now, recorded_to=None)
                )
            if valid_to < r.valid_to:
                new_rows.append(
                    replace(r, valid_from=valid_to, recorded_from=now, recorded_to=None)
                )
        row = BitemporalRow(
            key=key,
            value=value,
            valid_from=valid_from,
            valid_to=valid_to,
            recorded_from=now,
            recorded_to=None,
            version=version,
            cause_event_id=cause_event_id,
        )
        rows.extend(new_rows)
        rows.append(row)
        return row

    def get(self, key: str, as_of: date, known_at: datetime | None = None) -> T | None:
        if known_at is not None and known_at.tzinfo is None:
            raise ValueError("known_at must be timezone-aware")
        known = known_at or max(self._clock.now(), self._last_recorded or self._clock.now())
        candidates = [
            r for r in self._rows.get(key, []) if r.valid_on(as_of) and r.believed_at(known)
        ]
        if not candidates:
            return None
        if len(candidates) > 1:  # pragma: no cover - invariant guard
            raise RuntimeError(f"bitemporal invariant violated for {key} at {as_of}/{known}")
        return candidates[0].value

    def history(self, key: str) -> list[BitemporalRow[T]]:
        return sorted(self._rows.get(key, []), key=lambda r: (r.recorded_from, r.valid_from))
