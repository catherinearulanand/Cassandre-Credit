"""Security perfection tracking: CERSAI and charge filings after a charge is created.

The statutory windows come from ``rbi.cersai`` and ``statute.companies_act_chg1``. The service
tracks each required registration, reports its status as of today and returns the overdue ones so
a case can be opened. Unknown item types are rejected rather than ignored.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from events.catalogue import Payload, PerfectionOverdue
from platform_core.clock import Clock
from platform_core.errors import ConflictError, NotFoundError, ValidationFailure
from rules_engine import RuleRegistry

Emit = Callable[[str, Payload], None]
_RULE = {"CERSAI": "rbi.cersai", "CHG-1": "statute.companies_act_chg1"}
_OVERDUE = {"OVERDUE", "OVERDUE_IN_ADDITIONAL_WINDOW"}


@dataclass
class PerfectionItem:
    collateral_id: str
    item: str
    created_on: date
    done_on: date | None = None
    last_reported_status: str = ""


class PerfectionService:
    def __init__(self, clock: Clock, registry: RuleRegistry, emit: Emit | None = None) -> None:
        self.clock, self.rules = clock, registry
        self._emit: Emit = emit or (lambda t, p: None)
        self.items: dict[tuple[str, str], PerfectionItem] = {}

    def require(self, collateral_id: str, item: str, created_on: date) -> PerfectionItem:
        if item not in _RULE:
            raise ValidationFailure(f"no rule pack tracks {item!r}", code="unknown_perfection_item")
        if created_on > self.clock.today():
            raise ValidationFailure("charge creation date is in the future")
        key = (collateral_id, item)
        if key in self.items:
            return self.items[key]
        self.items[key] = PerfectionItem(collateral_id, item, created_on)
        return self.items[key]

    def complete(self, collateral_id: str, item: str, done_on: date) -> None:
        try:
            it = self.items[(collateral_id, item)]
        except KeyError as exc:
            raise NotFoundError(f"{item} for {collateral_id}") from exc
        if done_on < it.created_on or done_on > self.clock.today():
            raise ValidationFailure("completion date outside the item's life")
        if it.done_on is not None:
            raise ConflictError("already completed")
        it.done_on = done_on

    def status(self, collateral_id: str, item: str) -> dict[str, Any]:
        it = self.items[(collateral_id, item)]
        facts: dict[str, Any] = {"created_on": it.created_on, "today": self.clock.today()}
        if it.done_on:
            facts["registered_on" if item == "CERSAI" else "filed_on"] = it.done_on
        d = self.rules.evaluate(_RULE[item], facts)
        return {
            "status": d.result,
            "due_date": d.details["due_date"],
            "days_late": d.details["days_late"],
            "rule_id": d.rule_id,
            "rule_version": d.rule_version,
        }

    def sweep(self) -> list[PerfectionItem]:
        """Overdue items, announced once per status change."""
        out: list[PerfectionItem] = []
        for it in self.items.values():
            if it.done_on is not None:
                continue
            st = self.status(it.collateral_id, it.item)
            if st["status"] in _OVERDUE and st["status"] != it.last_reported_status:
                it.last_reported_status = st["status"]
                self._emit(
                    "perfection.overdue",
                    PerfectionOverdue(
                        collateral_id=it.collateral_id,
                        item=it.item,
                        due_date=st["due_date"],
                        days_overdue=int(st["days_late"]),
                        rule_id=st["rule_id"],
                        rule_version=st["rule_version"],
                    ),
                )
                out.append(it)
        return out
