"""Monitoring calendar: what the borrower owes us, by when, and what we do when it is late.

Obligations are derived from the published CTS (covenants, conditions subsequent, working-capital
stock statements) with due dates taken from the ``policy.monitoring`` rule pack. An obligation's id
depends on the obligor, the document type and the period, *not* on the CTS version, so an amendment
updates the same row instead of duplicating it: received items stay received, overdue items keep
their chase history, a changed due date is applied, and open items the amendment removed are
waived with a history entry. A document satisfies an obligation only if it names the same obligor,
type and period. Chasing climbs one step at a time and records every step it crosses.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from domain.cts import CreditTermSheet
from domain.entities import Obligation
from domain.schedule import due_test_dates
from events.catalogue import ObligationDue, ObligationOverdue, Payload
from platform_core.canonical import content_hash
from platform_core.clock import Clock
from platform_core.errors import ConflictError, NotFoundError, ValidationFailure
from rules_engine import RuleRegistry

Emit = Callable[[str, Payload], None]
_CHASE_ORDER = ["none", "portal_message", "email", "rm_task", "raise_signal"]


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return _month_end(d.year + m // 12, m % 12 + 1)


def _obligation_id(obligor_id: str, doc_type: str, period_end: date | None) -> str:
    return "OBL-" + content_hash([obligor_id, doc_type, str(period_end)])[:16]


class MonitoringCalendar:
    def __init__(self, clock: Clock, registry: RuleRegistry, emit: Emit | None = None) -> None:
        self.clock, self.rules = clock, registry
        self._emit: Emit = emit or (lambda t, p: None)
        self.obligations: dict[str, Obligation] = {}
        self.history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ building ----------
    def _desired(self, cts: CreditTermSheet, until: date) -> list[tuple[str, date | None, date]]:
        p = self.rules.params("policy.monitoring")["due"]
        out: list[tuple[str, date | None, date]] = []
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
        for cond in cts.conditions:
            if cond.kind == "subsequent" and cond.due_date is not None and cond.due_date <= until:
                out.append(
                    (f"condition:{cond.condition_id}:{cond.evidence_doc_type}", None, cond.due_date)
                )
        return out

    def build(self, cts: CreditTermSheet, until: date) -> list[Obligation]:
        """Add every obligation falling due up to ``until``. Existing rows are left alone."""
        added: list[Obligation] = []
        for doc_type, period_end, due in self._desired(cts, until):
            oid = _obligation_id(cts.obligor_id, doc_type, period_end)
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
        """Apply an amended CTS to the existing calendar (same or new CTS id)."""
        now = self.clock.now().isoformat()
        wanted = {
            _obligation_id(new_cts.obligor_id, dt, pe): (dt, pe, due)
            for dt, pe, due in self._desired(new_cts, until)
        }
        added = len(self.build(new_cts, until))
        moved = waived = 0
        for oid, (_, _, due) in wanted.items():
            ob = self.obligations[oid]
            ob.cts_id = new_cts.cts_id
            if ob.status in ("open", "overdue") and ob.due_date != due:
                self.history.append(
                    {
                        "at": now,
                        "obligation": oid,
                        "event": "due_date_changed",
                        "from": ob.due_date.isoformat(),
                        "to": due.isoformat(),
                    }
                )
                ob.due_date = due
                moved += 1
        for oid, ob in self.obligations.items():
            if (
                ob.obligor_id == new_cts.obligor_id
                and oid not in wanted
                and ob.status in ("open", "overdue")
                and ob.due_date <= until
            ):
                ob.status = "waived"
                self.history.append({"at": now, "obligation": oid, "event": "removed_by_amendment"})
                waived += 1
        return {"added": added, "due_date_changed": moved, "waived": waived}

    # ------------------------------------------------------------------ lifecycle ---------
    def get(self, obligation_id: str) -> Obligation:
        try:
            return self.obligations[obligation_id]
        except KeyError as exc:
            raise NotFoundError(f"obligation {obligation_id}") from exc

    def receive(
        self,
        obligation_id: str,
        document_id: str,
        *,
        obligor_id: str,
        doc_type: str,
        period_end: date | None = None,
    ) -> Obligation:
        """Satisfy an obligation with a document that names the same obligor, type and period."""
        ob = self.get(obligation_id)
        if obligor_id != ob.obligor_id:
            raise ValidationFailure("document belongs to a different obligor", code="doc_mismatch")
        if doc_type != ob.doc_type and doc_type != ob.doc_type.split(":", 1)[0]:
            raise ValidationFailure("document type does not match", code="doc_mismatch")
        if ob.period_end is not None and period_end != ob.period_end:
            raise ValidationFailure("document covers a different period", code="doc_mismatch")
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
            key = ("due", o.obligation_id, o.due_date.isoformat())
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
        """Mark overdue items and climb the chase ladder, emitting an event for every step crossed.
        Returns obligations whose chase level rose on this sweep."""
        today = self.clock.today()
        changed: list[Obligation] = []
        for o in self.obligations.values():
            if o.status not in ("open", "overdue") or today <= o.due_date:
                continue
            days = (today - o.due_date).days
            decision = self.rules.evaluate("policy.monitoring", {"days_overdue": days})
            action = str(decision.result)
            target = _CHASE_ORDER.index(action) if action in _CHASE_ORDER else 0
            o.status = "overdue"
            if target > o.chase_step:
                for step in range(o.chase_step + 1, target + 1):
                    self._emit(
                        "obligation.overdue",
                        ObligationOverdue(
                            obligation_id=o.obligation_id,
                            doc_type=o.doc_type,
                            due_date=o.due_date.isoformat(),
                            days_overdue=days,
                            chase_action=_CHASE_ORDER[step],
                        ),
                    )
                o.chase_step = target
                changed.append(o)
        return changed

    def open_overdue(self, obligor_id: str) -> list[Obligation]:
        return [
            o
            for o in self.obligations.values()
            if o.obligor_id == obligor_id and o.status == "overdue"
        ]
