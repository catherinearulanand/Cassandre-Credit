"""Synchronous drawdown gate.

Every new disbursement passes ten checks in a fixed order, each recorded with its result.
Decision: APPROVE, REJECT(reasons) or REFER(reasons) to a human. Any dependency failure or
timeout is REJECT(gate_unavailable): the gate fails CLOSED for new money. Repayments are never
gated (a borrower must always be able to repay), so ``decide`` refuses them explicitly.

Checks read a pre-assembled ``GateContext`` (online feature store + service caches), never
slow systems of record. All amounts are int paise.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

from domain.limit_tree import LimitTree

Decision = Literal["APPROVE", "REJECT", "REFER"]
CHECKS = (
    "sanction_validity",
    "conditions_precedent",
    "conditions_subsequent",
    "limit_availability",
    "drawing_power",
    "covenant_status",
    "fraud_flag",
    "sma_npa_status",
    "amendment_in_flight",
    "end_use",
)


@dataclass(frozen=True)
class CovenantStatus:
    covenant_id: str
    result: str  # BREACHED | IN_CURE | COMPLIED | ...
    consequences: tuple[str, ...] = ()


@dataclass
class GateContext:
    cts_valid_until: date
    facility_limit_node: str
    open_cps: list[str]  # condition ids still open that block this drawdown
    overdue_stopping_cs: list[str]
    tree: LimitTree
    dp_paise: int | None  # None for non-working-capital facilities
    utilised_paise: int
    covenants: list[CovenantStatus]
    rfa_or_fraud_flag: bool
    sma_bucket: str  # STANDARD | SMA-0 | SMA-1 | SMA-2 | NPA
    amendment_in_flight: bool
    approved_payees: set[str] = field(default_factory=set)
    related_party_payees: set[str] = field(default_factory=set)
    einvoice_required: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    # Which facility this snapshot describes and when it was taken. A snapshot that does not name
    # the requested facility, or is stale, is never trusted: the gate fails closed.
    facility_id: str | None = None
    as_of: date | None = None


@dataclass(frozen=True)
class DrawdownRequest:
    request_id: str
    facility_id: str
    amount_paise: int
    payee_id: str | None = None
    invoice_irn_valid: bool | None = None
    kind: Literal["drawdown", "repayment"] = "drawdown"


@dataclass(frozen=True)
class CheckResult:
    name: str
    outcome: Literal["pass", "fail", "refer"]
    detail: str


@dataclass(frozen=True)
class GateDecision:
    request_id: str
    decision: Decision
    reasons: tuple[str, ...]
    checks: tuple[CheckResult, ...]
    latency_ms: float
    context_provenance: dict[str, Any] = field(default_factory=dict)


class ContextProvider(Protocol):
    def load(self, facility_id: str) -> GateContext: ...


class RepaymentNotGated(Exception):
    """Repayments must never be blocked, so they are not accepted by the gate at all."""


SMA_BUCKETS = {"STANDARD", "SMA-0", "SMA-1", "SMA-2", "NPA"}
COVENANT_RESULTS = {
    "COMPLIED",
    "BREACHED",
    "WAIVED",
    "DEFERRED",
    "IN_CURE",
    "NOT_TESTED_MISSING_DATA",
    "NOT_COMPUTABLE",
}
STOPPING = {"stop_drawdown", "freeze", "event_of_default"}  # consequence kinds that halt new money
MAX_CONTEXT_AGE_DAYS = 1


def _stops(consequences: tuple[str, ...]) -> bool:
    return any(c.split(":", 1)[0].strip().lower() in STOPPING for c in consequences)


def run_checks(req: DrawdownRequest, ctx: GateContext, today: date) -> list[CheckResult]:
    """Ten ordered checks. Anything unrecognised fails: the gate never assumes good news."""
    out: list[CheckResult] = []

    def add(name: str, ok: bool, detail: str, *, refer: bool = False) -> None:
        out.append(CheckResult(name, "pass" if ok else ("refer" if refer else "fail"), detail))

    if (
        isinstance(req.amount_paise, bool)
        or not isinstance(req.amount_paise, int)
        or req.amount_paise <= 0
    ):
        raise ValueError("amount must be a positive integer number of paise")
    if ctx.facility_id != req.facility_id:
        raise ValueError("context does not describe the requested facility")
    if ctx.as_of is None or (today - ctx.as_of).days > MAX_CONTEXT_AGE_DAYS or ctx.as_of > today:
        raise ValueError("context snapshot is missing, future-dated or stale")

    add("sanction_validity", today <= ctx.cts_valid_until, f"valid until {ctx.cts_valid_until}")
    add(
        "conditions_precedent",
        not ctx.open_cps,
        f"open: {ctx.open_cps}" if ctx.open_cps else "all satisfied",
    )
    add(
        "conditions_subsequent",
        not ctx.overdue_stopping_cs,
        f"overdue stopping: {ctx.overdue_stopping_cs}"
        if ctx.overdue_stopping_cs
        else "none overdue",
    )
    avail = ctx.tree.check(ctx.facility_limit_node, req.amount_paise)
    add(
        "limit_availability",
        avail.ok,
        f"shortfalls {avail.shortfalls}" if not avail.ok else f"fits path {avail.path}",
    )
    if ctx.dp_paise is None:
        add("drawing_power", True, "not a working-capital facility")
    else:
        headroom = ctx.dp_paise - ctx.utilised_paise
        add(
            "drawing_power",
            req.amount_paise <= headroom,
            f"DP headroom {headroom}, requested {req.amount_paise}",
        )

    unknown = [c.covenant_id for c in ctx.covenants if c.result not in COVENANT_RESULTS]
    blocking = [
        c.covenant_id for c in ctx.covenants if c.result == "BREACHED" and _stops(c.consequences)
    ]
    untested = [
        c.covenant_id
        for c in ctx.covenants
        if c.result in ("NOT_TESTED_MISSING_DATA", "NOT_COMPUTABLE") and _stops(c.consequences)
    ]
    if unknown:
        add("covenant_status", False, f"unrecognised covenant results: {unknown}")
    elif blocking:
        add("covenant_status", False, f"stopping breaches: {blocking}")
    elif untested:
        add("covenant_status", False, f"stopping covenants not testable: {untested}", refer=True)
    else:
        add("covenant_status", True, "no stopping breach")

    add(
        "fraud_flag",
        ctx.rfa_or_fraud_flag is False,
        "RFA/fraud flag set or unknown" if ctx.rfa_or_fraud_flag is not False else "clear",
    )
    if ctx.sma_bucket not in SMA_BUCKETS:
        add("sma_npa_status", False, f"unrecognised SMA/NPA status {ctx.sma_bucket!r}")
    elif ctx.sma_bucket in ("SMA-2", "NPA"):
        add("sma_npa_status", False, ctx.sma_bucket)
    elif ctx.sma_bucket == "SMA-1":
        add("sma_npa_status", False, "SMA-1: policy requires human sign-off", refer=True)
    else:
        add("sma_npa_status", True, ctx.sma_bucket)
    add(
        "amendment_in_flight",
        ctx.amendment_in_flight is False,
        "sanction amendment in flight or unknown"
        if ctx.amendment_in_flight is not False
        else "none",
        refer=True,
    )

    problems, refers = [], []
    if not ctx.approved_payees:
        problems.append("no approved payee list on record")
    if req.payee_id is None:
        problems.append("no payee")
    elif req.payee_id in ctx.related_party_payees:
        refers.append("related-party payee")
    elif ctx.approved_payees and req.payee_id not in ctx.approved_payees:
        problems.append("payee not on approved list")
    if ctx.einvoice_required and req.invoice_irn_valid is not True:
        problems.append("e-invoice IRN not validated")
    if problems:
        add("end_use", False, "; ".join(problems))
    elif refers:
        add("end_use", False, "; ".join(refers), refer=True)
    else:
        add("end_use", True, "payee and invoice acceptable")
    return out


_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gate-load")


class DrawdownGate:
    def __init__(
        self,
        provider: ContextProvider,
        today_fn: Callable[[], date],
        clock_ms: Callable[[], float] = lambda: time.perf_counter() * 1000,
        deadline_ms: float = 250,
    ) -> None:
        self.provider = provider
        self.today_fn = today_fn
        self.clock_ms = clock_ms
        self.deadline_ms = deadline_ms

    def decide(self, req: DrawdownRequest) -> GateDecision:
        if req.kind != "drawdown":
            raise RepaymentNotGated("repayments never pass through the drawdown gate")
        t0 = self.clock_ms()
        try:
            # the deadline is enforced while waiting, so a hung dependency cannot hold the gate open
            ctx = _POOL.submit(self.provider.load, req.facility_id).result(
                timeout=self.deadline_ms / 1000
            )
            checks = run_checks(req, ctx, self.today_fn())
            elapsed = self.clock_ms() - t0
            if elapsed > self.deadline_ms:
                return self._closed(req, t0, f"gate_timeout:{elapsed:.0f}ms")
        except FutureTimeout:
            return self._closed(req, t0, "gate_timeout:context_load")
        except Exception as exc:  # noqa: BLE001 - ANY failure must fail closed
            return self._closed(req, t0, f"gate_unavailable:{type(exc).__name__}")
        fails = [c for c in checks if c.outcome == "fail"]
        refers = [c for c in checks if c.outcome == "refer"]
        if fails:
            decision: Decision = "REJECT"
            reasons = tuple(f"{c.name}: {c.detail}" for c in fails + refers)
        elif refers:
            decision, reasons = "REFER", tuple(f"{c.name}: {c.detail}" for c in refers)
        else:
            decision, reasons = "APPROVE", ()
        # last line of defence: APPROVE requires all ten named checks present and passing
        if decision == "APPROVE" and (
            [c.name for c in checks] != list(CHECKS) or any(c.outcome != "pass" for c in checks)
        ):
            return self._closed(req, t0, "gate_internal_inconsistency")
        return GateDecision(
            req.request_id,
            decision,
            reasons,
            tuple(checks),
            self.clock_ms() - t0,
            dict(ctx.provenance),
        )

    def _closed(self, req: DrawdownRequest, t0: float, why: str) -> GateDecision:
        checks = tuple(CheckResult(n, "fail", "not evaluated") for n in CHECKS)
        return GateDecision(req.request_id, "REJECT", (why,), checks, self.clock_ms() - t0)
