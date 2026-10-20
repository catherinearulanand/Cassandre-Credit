from __future__ import annotations

import time
from datetime import date

import pytest
from domain.limit_tree import LimitState, LimitTree
from drawdown_gate.domain import (
    CHECKS,
    CovenantStatus,
    DrawdownGate,
    DrawdownRequest,
    GateContext,
    RepaymentNotGated,
)
from hypothesis import given
from hypothesis import strategies as st

TODAY = date(2026, 10, 19)


def _ctx(**kw) -> GateContext:  # type: ignore[no-untyped-def]
    tree = LimitTree()
    tree.add(LimitState("OBL", None, 1_000))
    tree.add(LimitState("CC", "OBL", 600, utilised=100, dp=500, facility_id="F"))
    base = dict(
        cts_valid_until=date(2027, 6, 1),
        facility_limit_node="CC",
        open_cps=[],
        overdue_stopping_cs=[],
        tree=tree,
        dp_paise=500,
        utilised_paise=100,
        covenants=[],
        rfa_or_fraud_flag=False,
        sma_bucket="STANDARD",
        amendment_in_flight=False,
        approved_payees={"SUP-1"},
        related_party_payees={"SUP-RP"},
        facility_id="F",
        as_of=TODAY,
    )
    base.update(kw)
    return GateContext(**base)  # type: ignore[arg-type]


class Static:
    def __init__(self, ctx: GateContext | Exception) -> None:
        self.ctx = ctx

    def load(self, facility_id: str) -> GateContext:
        if isinstance(self.ctx, Exception):
            raise self.ctx
        return self.ctx


def _gate(ctx, **kw):  # type: ignore[no-untyped-def]
    return DrawdownGate(Static(ctx), lambda: TODAY, **kw)


REQ = DrawdownRequest("DRW-1", "F", 100, payee_id="SUP-1")


def test_approve_records_all_ten_checks_in_order() -> None:
    d = _gate(_ctx()).decide(REQ)
    assert d.decision == "APPROVE" and d.reasons == ()
    assert [c.name for c in d.checks] == list(CHECKS) and all(c.outcome == "pass" for c in d.checks)


@pytest.mark.parametrize(
    "override,check,expected",
    [
        ({"cts_valid_until": date(2026, 10, 18)}, "sanction_validity", "REJECT"),
        ({"open_cps": ["CP-1"]}, "conditions_precedent", "REJECT"),
        ({"overdue_stopping_cs": ["CS-1"]}, "conditions_subsequent", "REJECT"),
        ({"dp_paise": 150}, "drawing_power", "REJECT"),
        (
            {
                "covenants": [
                    CovenantStatus("COV-1", "BREACHED", ("reprice:+50bps", "stop_drawdown"))
                ]
            },
            "covenant_status",
            "REJECT",
        ),
        ({"rfa_or_fraud_flag": True}, "fraud_flag", "REJECT"),
        ({"sma_bucket": "SMA-2"}, "sma_npa_status", "REJECT"),
        ({"sma_bucket": "NPA"}, "sma_npa_status", "REJECT"),
        ({"sma_bucket": "SMA-1"}, "sma_npa_status", "REFER"),
        ({"amendment_in_flight": True}, "amendment_in_flight", "REFER"),
    ],
)
def test_each_check_can_block(override, check, expected) -> None:  # type: ignore[no-untyped-def]
    d = _gate(_ctx(**override)).decide(REQ)
    assert d.decision == expected
    assert next(c for c in d.checks if c.name == check).outcome != "pass"


def test_breach_without_stop_consequence_does_not_block() -> None:
    cov = [CovenantStatus("COV-1", "BREACHED", ("reprice:+50bps",))]
    assert _gate(_ctx(covenants=cov)).decide(REQ).decision == "APPROVE"


def test_limit_availability_walks_to_the_root() -> None:
    ctx = _ctx()
    ctx.tree.nodes["OBL"].utilised = 950  # obligor-level headroom is only 50
    d = _gate(ctx).decide(REQ)
    assert d.decision == "REJECT" and "OBL" in " ".join(d.reasons)


def test_end_use_rules() -> None:
    assert (
        _gate(_ctx()).decide(DrawdownRequest("D", "F", 100, payee_id="SUP-RP")).decision == "REFER"
    )
    assert (
        _gate(_ctx()).decide(DrawdownRequest("D", "F", 100, payee_id="STRANGER")).decision
        == "REJECT"
    )
    assert _gate(_ctx()).decide(DrawdownRequest("D", "F", 100, payee_id=None)).decision == "REJECT"
    needs = _ctx(einvoice_required=True)
    assert _gate(needs).decide(REQ).decision == "REJECT"
    assert (
        _gate(needs)
        .decide(DrawdownRequest("D", "F", 100, payee_id="SUP-1", invoice_irn_valid=True))
        .decision
        == "APPROVE"
    )


def test_reject_beats_refer() -> None:
    d = _gate(_ctx(sma_bucket="SMA-1", open_cps=["CP-1"])).decide(REQ)
    assert d.decision == "REJECT" and len(d.reasons) == 2


@pytest.mark.parametrize(
    "failure",
    [ConnectionError("feature store down"), TimeoutError(), KeyError("x"), RuntimeError()],
)
def test_fails_closed_on_any_dependency_failure(failure: Exception) -> None:
    d = _gate(failure).decide(REQ)
    assert d.decision == "REJECT" and d.reasons[0].startswith(("gate_unavailable", "gate_timeout"))
    assert len(d.checks) == 10


def test_fails_closed_on_timeout() -> None:
    t = iter(range(0, 10_000, 400))
    g = DrawdownGate(
        Static(_ctx()), lambda: TODAY, clock_ms=lambda: float(next(t)), deadline_ms=250
    )
    d = g.decide(REQ)
    assert d.decision == "REJECT" and d.reasons[0].startswith("gate_timeout")


def test_repayments_are_never_gated() -> None:
    with pytest.raises(RepaymentNotGated):
        _gate(_ctx(rfa_or_fraud_flag=True)).decide(DrawdownRequest("R", "F", 100, kind="repayment"))


@given(st.integers(1, 2_000), st.integers(0, 1_000), st.booleans())
def test_never_approves_with_a_failed_blocking_check(amount: int, dp: int, open_cp: bool) -> None:
    ctx = _ctx(dp_paise=dp, open_cps=["CP"] if open_cp else [])
    d = _gate(ctx).decide(DrawdownRequest("D", "F", amount, payee_id="SUP-1"))
    if d.decision == "APPROVE":
        assert (
            not open_cp and amount <= dp - 100 and amount <= 500
        )  # DP headroom and path both hold
        assert all(c.outcome == "pass" for c in d.checks)


def test_latency_p99_under_300ms_at_load() -> None:
    gate = _gate(_ctx())
    lat = []
    for i in range(3_000):
        t0 = time.perf_counter()
        gate.decide(DrawdownRequest(f"D{i}", "F", 1 + i % 50, payee_id="SUP-1"))
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    assert lat[int(0.99 * len(lat))] < 300
    assert lat[int(0.99 * len(lat))] < 25, "pure-logic path should be far below the budget"
