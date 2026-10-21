"""Periodic credit review: when it is due, and the fact pack the review memo is written from.

The pack is the only source of numbers for a memo. Every fact has a key, a value rendered as text
and the reference it came from. A memo (human or agent drafted) may quote a number only if it is in
the pack; ``agents.runtime.verify_numbers`` enforces that. The pack also lists what is *missing*, so a
gap is visible in the memo instead of being smoothed over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from platform_core.canonical import content_hash
from platform_core.clock import Clock
from platform_core.errors import ValidationFailure
from rules_engine import RuleRegistry


@dataclass(frozen=True)
class Fact:
    key: str
    value: str
    source: str  # e.g. "covenant_test:CT-123", "spread:SPR-9:line:pbt"


@dataclass(frozen=True)
class ReviewPack:
    obligor_id: str
    as_of: date
    facts: tuple[Fact, ...]
    missing: tuple[str, ...]
    pack_hash: str = ""

    def values(self) -> dict[str, str]:
        return {f.key: f.value for f in self.facts}


@dataclass
class ReviewSchedule:
    obligor_id: str
    rating: str
    last_review_on: date
    watch_list: bool = False
    history: list[date] = field(default_factory=list)


REQUIRED_SECTIONS = (
    "rating",
    "sma_bucket",
    "ews_level",
    "latest_dscr",
    "latest_leverage",
    "open_breaches",
    "overdue_obligations",
    "utilisation_pct",
)


_NUM = re.compile(r"-?\d{1,15}(\.\d{1,6})?")
_SHAPES: dict[str, re.Pattern[str]] = {
    "rating": re.compile(r"CR[1-6]"),
    "sma_bucket": re.compile(r"STANDARD|SMA-[012]|NPA"),
    "ews_level": re.compile(r"none|watch|elevated|critical"),
    "latest_dscr": _NUM,
    "latest_leverage": _NUM,
    "open_breaches": re.compile(r"\d{1,4}"),
    "overdue_obligations": re.compile(r"\d{1,4}"),
    "utilisation_pct": _NUM,
}
_SOURCE = re.compile(r"[a-z_]{2,30}:[A-Za-z0-9._-]{1,60}(:[A-Za-z0-9._-]{1,60})*")


class ReviewService:
    def __init__(self, clock: Clock, registry: RuleRegistry) -> None:
        self.clock, self.rules = clock, registry
        self.schedules: dict[str, ReviewSchedule] = {}

    def register(self, s: ReviewSchedule) -> None:
        self.schedules[s.obligor_id] = s

    def due(self, obligor_id: str) -> dict[str, Any]:
        s = self.schedules[obligor_id]
        d = self.rules.evaluate(
            "policy.review_calendar",
            {
                "rating": s.rating,
                "last_review_on": s.last_review_on,
                "today": self.clock.today(),
                "watch_list": s.watch_list,
            },
        )
        return {**d.result, "rule_version": d.rule_version}

    def overdue_reviews(self) -> list[str]:
        return sorted(o for o in self.schedules if self.due(o)["overdue"])

    def complete(self, obligor_id: str, on: date) -> None:
        s = self.schedules[obligor_id]
        if on < s.last_review_on or on > self.clock.today():
            raise ValidationFailure("review date outside the schedule")
        s.history.append(s.last_review_on)
        s.last_review_on = on

    def build_pack(self, obligor_id: str, inputs: dict[str, tuple[str, str]]) -> ReviewPack:
        """``inputs`` maps an allow-listed fact key to (value text, source reference). Each key has
        a strict value shape, so free text (and any instruction inside it) cannot enter the pack.
        Required sections that are absent are listed as missing."""
        facts = []
        for k, v in sorted(inputs.items()):
            shape = _SHAPES.get(k)
            if shape is None:
                raise ValidationFailure(f"fact {k!r} is not an allowed pack field", code="bad_fact")
            if (
                not isinstance(v, tuple)
                or len(v) != 2
                or not all(isinstance(x, str) and x for x in v)
                or not shape.fullmatch(v[0])
                or not _SOURCE.fullmatch(v[1])
            ):
                raise ValidationFailure(
                    f"fact {k!r} has an invalid value or source", code="bad_fact"
                )
            facts.append(Fact(k, v[0], v[1]))
        missing = tuple(s for s in REQUIRED_SECTIONS if s not in inputs)
        h = content_hash(
            {
                "o": obligor_id,
                "d": self.clock.today().isoformat(),
                "f": [[f.key, f.value, f.source] for f in facts],
            }
        )
        return ReviewPack(obligor_id, self.clock.today(), tuple(facts), missing, h)
