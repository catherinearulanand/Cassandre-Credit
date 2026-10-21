"""Monitoring calendar: what the borrower owes us, by when, and what we do when it is late.

Obligations are derived from the published CTS (covenants, conditions subsequent, working-capital
stock statements) with due dates taken from the ``policy.monitoring`` rule pack. Ids are
deterministic, so building the calendar twice (or after an amendment) never duplicates a row.
Chasing escalates one step at a time and emits an event only when the action changes.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from domain.cts import CreditTermSheet
from domain.entities import Obligation
from events.catalogue import ObligationDue, ObligationOverdue, Payload
from platform_core.canonical import content_hash
from platform_core.clock import Clock
from platform_core.errors import ConflictError, NotFoundError
from rules_engine import RuleRegistry

Emit = Callable[[str, Payload], None]
_CHASE_ORDER = ["none", "portal_message", "email", "rm_task", "raise_signal"]


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return _month_end(d.year + m // 12, m % 12 + 1)


def _obligation_id(cts_id: str, doc_type: str, period_end: date | None, key: str = "") -> str:
    return "OBL-" + content_hash([cts_id, doc_type, str(period_end), key])[:16]


class MonitoringCalendar:
    def __init__(self, clock: Clock, registry: RuleRegistry, emit: Emit | None = None) -> None:
        self.clock, self.rules = clock, registry
        self._emit: Emit = emit or (lambda t, p: None)
        self.obligations: dict[str, Obligation] = {}
        self.history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ building ----------
    def build(self, cts: CreditTermSheet, until: date) -> list[Obligation]:
        """Add every obligation falling due up to ``until``. Safe to call repeatedly."""
        p = self.rules.params("policy.monitoring")["due"]
        out: list[tuple[str, date | None, date]] = []
        # Compliance certificates and QIS follow each covenant test date.
        from domain.schedule import due_test_dates

        for cov in cts.covenants:
            for td in due_test_dates(cov, until):
                if cov.klass == "financial":
                    out.append(
                        (
                            f"compliance_certificate:{cov.covenant_id}",
                            td,
                            td + timedelta(days=int(p["compliance_certificate_days_after_test"])),
                        )
                    )
        # Quarterly information and annual accounts, from the sanction date.
        q = _month_end(cts.sanction_date.year, ((cts.sanction_date.month - 1) // 3 + 1) * 3)
        while q <= until:
            out.append(("qis", q, q + timedelta(days=int(p["qis_days_after_quarter"]))))
            q = _add_months(q, 3)
        fy = date(cts.sanction_date.year, 3, 31)
        while fy <= until:
            if fy >= cts.sanction_date:
                out.append(
                    (
                        "audited_financials",
                        fy,
                        fy + timedelta(days=int(p["audited_fs_days_after_fy"])),
                    )
                )
            fy = date(fy.year + 1, 3, 31)
        # Monthly stock statement for each working-capital facility.
        day = int(p["stock_statement_day_of_following_month"])
        for fac in cts.facilities:
            if not fac.facility_type.revolving:
                continue
            m = _month_end(cts.sanction_date.year, cts.sanction_date.month)
            while m <= until:
                nxt = m + timedelta(days=1)
                out.append(
                    (f"stock_statement:{fac.facility_id}", m, date(nxt.year, nxt.month, day))
                )
                m = _add_months(m, 1)
        # Conditions subsequent with a due date.
        for cond in cts.conditions:
            if cond.kind == "subsequent" and cond.due_date is not None and cond.due_date <= until:
                out.append(
                    (f"condition:{cond.condition_id}:{cond.evidence_doc_type}", None, cond.due_date)
                )

        added: list[Obligation] = []
        for doc_type, period_end, due in out:
            oid = _obligation_id(cts.cts_id, doc_type, period_end)
            if oid in self.obligations:
                continue
            ob = Obligation(
                obligation_id=oid,
                obligor_id=cts.obligor_id,
                cts_id=cts.cts_id,
                doc_type=doc_type,
                period_end=period_end,
                due_date=due,
            )
            self.obligations[oid] = ob
            added.append(ob)
        return added

    def reconcile_amendment(self, new_cts: CreditTermSheet, until: date) -> dict[str, int]:
        """After an amendment: add new obligations; open ones no longer in the CTS are waived
        with a history entry (never silently dropped, never deleted)."""
        added = self.build(new_cts, until)
        keep = {
            _obligation_id(new_cts.cts_id, o.doc_type, o.period_end)
            for o in self.obligations.values()
            if o.cts_id == new_cts.cts_id
        }
        waived = 0
        for o in self.obligations.values():
            if (
                o.obligor_id == new_cts.obligor_id
                and o.cts_id != new_cts.cts_id
                and o.status in ("open", "overdue")
            ):
                twin = _obligation_id(new_cts.cts_id, o.doc_type, o.period_end)
                if twin in keep:
                    o.status = "waived"
                    self.history.append(
                        {
                            "at": self.clock.now().isoformat(),
                            "obligation": o.obligation_id,
                            "event": "superseded_by_amendment",
                            "twin": twin,
                        }
                    )
                    waived += 1
        return {"added": len(added), "superseded": waived}

    # ------------------------------------------------------------------ lifecycle ---------
    def get(self, obligation_id: str) -> Obligation:
        try:
            return self.obligations[obligation_id]
        except KeyError as exc:
            raise NotFoundError(f"obligation {obligation_id}") from exc

    def receive(self, obligation_id: str, document_id: str) -> Obligation:
        ob = self.get(obligation_id)
        if ob.status == "received":
            if ob.received_document_id == document_id:
                return ob  # idempotent
            raise ConflictError("obligation already satisfied by a different document")
        if ob.status == "waived":
            raise ConflictError("obligation was waived")
        ob.status, ob.received_document_id = "received", document_id
        self.history.append(
            {
                "at": self.clock.now().isoformat(),
                "obligation": obligation_id,
                "event": "received",
                "document": document_id,
                "late": self.clock.today() > ob.due_date,
            }
        )
        return ob

    def due_soon(self, within_days: int = 7) -> list[Obligation]:
        today = self.clock.today()
        return [
            o
            for o in self.obligations.values()
            if o.status == "open" and today <= o.due_date <= today + timedelta(days=within_days)
        ]

    def announce_due(self, within_days: int = 7) -> int:
        n = 0
        for o in self.due_soon(within_days):
            key = ("due", o.obligation_id)
            if any(h.get("key") == key for h in self.history):
                continue
            self.history.append({"key": key, "at": self.clock.now().isoformat()})
            self._emit(
                "obligation.due",
                ObligationDue(
                    obligation_id=o.obligation_id,
                    doc_type=o.doc_type,
                    due_date=o.due_date.isoformat(),
                ),
            )
            n += 1
        return n

    def sweep(self) -> list[Obligation]:
        """Mark overdue items and advance the chase ladder. Returns obligations whose chase
        action changed on this sweep."""
        today = self.clock.today()
        changed: list[Obligation] = []
        for o in self.obligations.values():
            if o.status not in ("open", "overdue") or today <= o.due_date:
                continue
            days = (today - o.due_date).days
            decision = self.rules.evaluate("policy.monitoring", {"days_overdue": days})
            action = str(decision.result)
            step = _CHASE_ORDER.index(action) if action in _CHASE_ORDER else 0
            o.status = "overdue"
            if step > o.chase_step:
                o.chase_step = step
                changed.append(o)
                self._emit(
                    "obligation.overdue",
                    ObligationOverdue(
                        obligation_id=o.obligation_id,
                        doc_type=o.doc_type,
                        due_date=o.due_date.isoformat(),
                        days_overdue=days,
                        chase_action=action,
                    ),
                )
        return changed

    def open_overdue(self, obligor_id: str) -> list[Obligation]:
        return [
            o
            for o in self.obligations.values()
            if o.obligor_id == obligor_id and o.status == "overdue"
        ]
