from __future__ import annotations

import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from authz import MakerChecker, Permission, Principal, ResourceScope, Role
from case_service.domain import CaseService, CaseState
from covenant_engine.domain import (
    EquityCure,
    TestContext,
    Waiver,
    due_test_dates,
    evaluate_covenant,
)
from cts_service.domain import CtsService, diff
from domain.entities import CaseType, Spread, SpreadLine, TestResult
from hypothesis import given
from hypothesis import strategies as st
from limit_service.domain import LimitState, LimitTree, StockStatement, compute_dp
from platform_core.errors import (
    AuthorizationError,
    ConflictError,
    IntegrityError,
    ValidationFailure,
)
from security.crypto import Signer
from testing.synthetic.cts_gen import make_sanction
from testing.synthetic.financials import UNIT_PAISE, generate_history
from testing.synthetic.obligors import make_obligor
from testing.synthetic.reference import reference_ratio

from agents.sanction_compiler.compiler import assemble_cts, grammar_extract


def _spreads(hist, obligor_id="OBL-1"):  # type: ignore[no-untyped-def]
    out = []
    for i, q in enumerate(hist.quarters):
        lines = {c: SpreadLine(code=c, amount_paise=v * UNIT_PAISE) for c, v in q.lines.items()}
        out.append(
            Spread(
                spread_id=f"SPR-{i}",
                obligor_id=obligor_id,
                period_end=q.period_end,
                period_months=3,
                basis="standalone",
                audited=False,
                lines=lines,
            )
        )
    return out


@pytest.fixture(scope="module")
def world():  # type: ignore[no-untyped-def]
    rng = random.Random(5)
    ob = make_obligor(rng, 1, "healthy")
    h = generate_history(rng, ob.obligor.obligor_id, "healthy")
    pkg = make_sanction(rng, ob, h, "template")
    return ob, h, pkg, _spreads(h, ob.obligor.obligor_id)


def test_history_identities_hold(world) -> None:  # type: ignore[no-untyped-def]
    _, h, _, _ = world
    for q in h.quarters:
        x = q.lines
        assert x["total_assets"] == x["total_equity_and_liabilities"]
        assert x["total_income"] - x["total_expenses"] == x["pbt"]
        assert x["pbt"] - x["tax_expense"] == x["pat"]


def test_engine_matches_independent_reference_on_every_covenant(world) -> None:  # type: ignore[no-untyped-def]
    _, h, pkg, spreads = world
    keys = {
        "Net Debt / EBITDA": "net_debt_ebitda",
        "DSCR": "dscr",
        "Interest Service Coverage Ratio": "iscr",
        "Current Ratio": "current_ratio",
        "TOL / TNW": "tol_tnw",
        "Debt / Equity": "debt_equity",
    }
    checked = 0
    for cov in pkg.cts.covenants:
        for qi in range(4, len(spreads)):
            rec = evaluate_covenant(
                cov,
                pkg.cts,
                spreads,
                spreads[qi].period_end,
                tested_at=datetime(2026, 10, 19, tzinfo=UTC),
                test_id="TST-1",
            )
            ref = reference_ratio(keys[cov.name], h, qi)
            assert ref is not None and rec.value == ref, (cov.name, qi, rec.value, ref)
            assert rec.lineage, "every result must carry lineage"
            checked += 1
    assert checked >= 20


def test_missing_history_is_not_tested(world) -> None:  # type: ignore[no-untyped-def]
    _, _, pkg, spreads = world
    dscr = next(c for c in pkg.cts.covenants if c.name == "DSCR")
    rec = evaluate_covenant(
        dscr,
        pkg.cts,
        spreads[:3],
        spreads[2].period_end,
        tested_at=datetime(2026, 10, 19, tzinfo=UTC),
        test_id="TST-2",
    )
    assert rec.result is TestResult.NOT_TESTED_MISSING_DATA


def test_gap_in_series_refuses(world) -> None:  # type: ignore[no-untyped-def]
    _, _, pkg, spreads = world
    gapped = spreads[:5] + spreads[6:9]
    dscr = next(c for c in pkg.cts.covenants if c.name == "DSCR")
    rec = evaluate_covenant(
        dscr,
        pkg.cts,
        gapped,
        gapped[-1].period_end,
        tested_at=datetime(2026, 10, 19, tzinfo=UTC),
        test_id="TST-3",
    )
    assert rec.result is TestResult.NOT_TESTED_MISSING_DATA and "gap" in rec.notes[0]


def _breaching(world):  # type: ignore[no-untyped-def]
    _, _, pkg, spreads = world
    cov = next(c for c in pkg.cts.covenants if c.name == "DSCR").model_copy(
        update={"threshold": Decimal("99"), "cure_period_days": 0, "equity_cure_allowed": False}
    )
    return cov, pkg.cts, spreads


def test_breach_waiver_cure_and_equity(world) -> None:  # type: ignore[no-untyped-def]
    cov, cts, spreads = _breaching(world)
    pe = spreads[6].period_end
    now = datetime(2026, 10, 19, tzinfo=UTC)
    assert (
        evaluate_covenant(cov, cts, spreads, pe, tested_at=now, test_id="T").result
        is TestResult.BREACHED
    )
    w = TestContext(waivers=[Waiver(cov.covenant_id, pe, "APR-9")])
    assert (
        evaluate_covenant(cov, cts, spreads, pe, tested_at=now, test_id="T", ctx=w).result
        is TestResult.WAIVED
    )
    cured = cov.model_copy(update={"cure_period_days": 30})
    inside = TestContext(today=date(pe.year, pe.month, 1))
    assert (
        evaluate_covenant(cured, cts, spreads, pe, tested_at=now, test_id="T", ctx=inside).result
        is TestResult.IN_CURE
    )
    after = TestContext(today=date(2030, 1, 1))
    assert (
        evaluate_covenant(cured, cts, spreads, pe, tested_at=now, test_id="T", ctx=after).result
        is TestResult.BREACHED
    )
    # equity cure on a leverage covenant
    nd = next(c for c in cts.covenants if c.name == "Net Debt / EBITDA")
    base = evaluate_covenant(nd, cts, spreads, pe, tested_at=now, test_id="T")
    tight = nd.model_copy(
        update={
            "threshold": base.value - Decimal("0.5"),
            "equity_cure_allowed": True,
            "max_equity_cures": 1,
            "cure_period_days": 0,
        }
    )
    assert (
        evaluate_covenant(tight, cts, spreads, pe, tested_at=now, test_id="T").result
        is TestResult.BREACHED
    )
    ctx = TestContext(equity_cures=[EquityCure(tight.covenant_id, pe, Decimal(10) ** 12, "APR-1")])
    r = evaluate_covenant(tight, cts, spreads, pe, tested_at=now, test_id="T", ctx=ctx)
    assert r.result is TestResult.COMPLIED and any("equity cure" in n for n in r.notes)
    r2 = evaluate_covenant(tight, cts, spreads, pe, tested_at=now, test_id="T", ctx=ctx)
    assert r2.result is TestResult.BREACHED  # only one cure allowed


def test_zero_denominator_not_computable(world) -> None:  # type: ignore[no-untyped-def]
    _, _, pkg, spreads = world
    z = [
        s.model_copy(
            update={
                "lines": {
                    **s.lines,
                    "principal_repayment_lt": SpreadLine(code="x", amount_paise=0),
                    "finance_costs_lt": SpreadLine(code="y", amount_paise=0),
                }
            }
        )
        for s in spreads
    ]
    dscr = next(c for c in pkg.cts.covenants if c.name == "DSCR")
    rec = evaluate_covenant(
        dscr, pkg.cts, z, z[6].period_end, tested_at=datetime(2026, 10, 19, tzinfo=UTC), test_id="T"
    )
    assert rec.result is TestResult.NOT_COMPUTABLE


def test_deterministic_rerun(world) -> None:  # type: ignore[no-untyped-def]
    _, _, pkg, spreads = world
    now = datetime(2026, 10, 19, tzinfo=UTC)
    a = [
        evaluate_covenant(c, pkg.cts, spreads, spreads[8].period_end, tested_at=now, test_id="T")
        for c in pkg.cts.covenants
    ]
    b = [
        evaluate_covenant(c, pkg.cts, spreads, spreads[8].period_end, tested_at=now, test_id="T")
        for c in pkg.cts.covenants
    ]
    assert a == b


def test_due_dates(world) -> None:  # type: ignore[no-untyped-def]
    _, _, pkg, _ = world
    ds = due_test_dates(pkg.cts.covenants[0], date(2025, 6, 30))
    assert ds == [
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
    ]


# ----------------------------------------------------------------- DP & limits ----------
def _st(**kw):  # type: ignore[no-untyped-def]
    d = dict(
        period_end="2026-09-30",
        raw_material=10_00_000_00,
        wip=5_00_000_00,
        finished_goods=15_00_000_00,
        receivables_by_age={
            30: 8_00_000_00,
            60: 6_00_000_00,
            90: 4_00_000_00,
            120: 3_00_000_00,
            180: 2_00_000_00,
        },
    )
    d.update(kw)
    return StockStatement(**d)


def test_dp_hand_computed() -> None:
    # stock 30L, BD <=90d = 18L; margin 25% => 22.5L + 13.5L = 36L; cap 40L
    r = compute_dp(
        40_00_000_00,
        _st(),
        stock_margin=Decimal("0.25"),
        bd_margin=Decimal("0.25"),
        age_cutoff_days=90,
        deduct_creditors=False,
    )
    assert r.dp_paise == 36_00_000_00 and not r.capped_by_limit
    r30 = compute_dp(
        40_00_000_00,
        _st(),
        stock_margin=Decimal("0.30"),
        bd_margin=Decimal("0.30"),
        age_cutoff_days=90,
        deduct_creditors=False,
    )
    assert r30.dp_paise == 33_60_000_00  # 21L + 12.6L
    capped = compute_dp(
        30_00_000_00,
        _st(),
        stock_margin=Decimal("0.25"),
        bd_margin=Decimal("0.25"),
        age_cutoff_days=90,
        deduct_creditors=False,
    )
    assert capped.dp_paise == 30_00_000_00 and capped.capped_by_limit


def test_dp_exclusions() -> None:
    r = compute_dp(
        10**12,
        _st(
            obsolete_stock=2_00_000_00,
            unpaid_creditors=3_00_000_00,
            related_party_receivables=1_00_000_00,
        ),
        stock_margin=Decimal("0.25"),
        bd_margin=Decimal("0.25"),
        age_cutoff_days=90,
    )
    assert r.eligible_stock == 25_00_000_00 and r.eligible_book_debts == 17_00_000_00
    r120 = compute_dp(
        10**12,
        _st(),
        stock_margin=Decimal("0"),
        bd_margin=Decimal("0"),
        age_cutoff_days=120,
        deduct_creditors=False,
    )
    assert r120.eligible_book_debts == 21_00_000_00


amounts = st.integers(0, 10**13)


@given(
    amounts,
    amounts,
    amounts,
    amounts,
    st.integers(0, 10**13),
    st.sampled_from([Decimal("0"), Decimal("0.25"), Decimal("0.4")]),
)
def test_dp_invariants(rm: int, fg: int, bd: int, limit: int, extra: int, margin: Decimal) -> None:
    s = StockStatement("p", rm, 0, fg, receivables_by_age={90: bd})
    dp = compute_dp(
        limit, s, stock_margin=margin, bd_margin=margin, age_cutoff_days=90, deduct_creditors=False
    ).dp_paise
    assert 0 <= dp <= limit
    bigger = StockStatement("p", rm + extra, 0, fg, receivables_by_age={90: bd + extra})
    assert (
        compute_dp(
            limit,
            bigger,
            stock_margin=margin,
            bd_margin=margin,
            age_cutoff_days=90,
            deduct_creditors=False,
        ).dp_paise
        >= dp
    )
    harsher = compute_dp(
        limit,
        s,
        stock_margin=margin + Decimal("0.05"),
        bd_margin=margin + Decimal("0.05"),
        age_cutoff_days=90,
        deduct_creditors=False,
    ).dp_paise
    assert harsher <= dp


def test_limit_tree_availability_and_consequences() -> None:
    t = LimitTree()
    t.add(LimitState("OBL", None, 100))
    t.add(
        LimitState(
            "CC", "OBL", 60, utilised=50, dp=55, facility_id="F1", interchangeable_with=["WCDL"]
        )
    )
    t.add(LimitState("WCDL", "OBL", 40, utilised=10))
    assert t.check("CC", 5).ok
    assert not t.check("CC", 6).ok or t.check("CC", 6).used_interchange == ["WCDL"]
    t.nodes["OBL"].utilised = 98
    res = t.check("CC", 5)
    assert not res.ok and "OBL" in res.shortfalls
    t.nodes["OBL"].utilised = 0
    t.apply_consequence("CC", "stop_drawdown")
    assert not t.check("CC", 1).ok
    with pytest.raises(ValidationFailure):
        t.apply_consequence("CC", "teleport")
    with pytest.raises(ValidationFailure):
        t.check("CC", 0)


# ------------------------------------------------------------------- CTS service ------
def _human(pid, *roles):  # type: ignore[no-untyped-def]
    return Principal(id=pid, kind="human", roles=frozenset(roles))


@pytest.fixture
def cts_env(clock, keys, rules, world):  # type: ignore[no-untyped-def]
    signer = Signer(keys)
    svc = CtsService(
        clock, rules, signer, MakerChecker(clock, signer), tier1_capital_rupees=Decimal(10) ** 12
    )
    return svc, world


def test_publish_maker_checker_and_immutability(cts_env) -> None:  # type: ignore[no-untyped-def]
    svc, (ob, _, pkg, _) = cts_env
    checker = _human("cad1", Role.CAD_CHECKER)
    with pytest.raises(AuthorizationError):  # drafter cannot publish own work
        svc.publish(
            pkg.cts,
            drafted_by="cad1",
            checker=checker,
            reason="ok",
            rating=ob.obligor.internal_rating,
        )
    agent = Principal(
        id="agent:sc", kind="agent", roles=frozenset({Role.AGENT}), on_behalf_of="cad0"
    )
    with pytest.raises(AuthorizationError):
        svc.publish(
            pkg.cts,
            drafted_by="cad0",
            checker=agent,
            reason="ok",
            rating=ob.obligor.internal_rating,
        )
    pub = svc.publish(
        pkg.cts,
        drafted_by="agent:sc",
        checker=checker,
        reason="verified vs sanction",
        rating=ob.obligor.internal_rating,
    )
    assert pub.cts.content_hash == pub.cts.compute_hash()
    with pytest.raises(ConflictError):
        svc.publish(
            pkg.cts,
            drafted_by="agent:sc",
            checker=checker,
            reason="again",
            rating=ob.obligor.internal_rating,
        )
    svc._store[pkg.cts.cts_id][0] = type(pub)(
        pub.cts.model_copy(update={"sanction_ref": "FORGED"}),
        pub.signature,
        pub.approval,
        pub.published_at,
        pub.drafted_by,
    )
    with pytest.raises(IntegrityError):
        svc.get(pkg.cts.cts_id)


def test_delegated_power_violation_blocks(cts_env) -> None:  # type: ignore[no-untyped-def]
    svc, (ob, _, pkg, _) = cts_env
    weak = pkg.cts.model_copy(update={"sanctioning_authority": "branch_credit_committee"})
    with pytest.raises(ValidationFailure, match="delegated power"):
        svc.publish(
            weak,
            drafted_by="agent:sc",
            checker=_human("c", Role.CAD_CHECKER),
            reason="r",
            rating=ob.obligor.internal_rating,
        )


def test_policy_violation_blocks(cts_env) -> None:  # type: ignore[no-untyped-def]
    svc, (ob, _, pkg, _) = cts_env
    bad_fac = pkg.cts.facilities[1].model_copy(update={"tenor_months": 36})
    bad = pkg.cts.model_copy(update={"facilities": [pkg.cts.facilities[0], bad_fac]})
    with pytest.raises(ValidationFailure, match="tenor"):
        svc.publish(
            bad,
            drafted_by="agent:sc",
            checker=_human("c", Role.CAD_CHECKER),
            reason="r",
            rating=ob.obligor.internal_rating,
        )


def test_amendment_versions_and_diff(cts_env) -> None:  # type: ignore[no-untyped-def]
    svc, (ob, _, pkg, _) = cts_env
    checker = _human("cad1", Role.CAD_CHECKER)
    svc.publish(
        pkg.cts,
        drafted_by="agent:sc",
        checker=checker,
        reason="r",
        rating=ob.obligor.internal_rating,
    )
    cov = pkg.cts.covenants[0].model_copy(
        update={"threshold": pkg.cts.covenants[0].threshold + Decimal("0.25")}
    )
    v2 = pkg.cts.model_copy(
        update={
            "version": 2,
            "supersedes_version": 1,
            "amendment_sanction_ref": "AMD/1",
            "covenants": [cov, *pkg.cts.covenants[1:]],
        }
    )
    pub, changes = svc.amend(
        pkg.cts.cts_id,
        v2,
        drafted_by="agent:sc",
        checker=checker,
        reason="waiver reset",
        rating=ob.obligor.internal_rating,
    )
    assert svc.versions(pkg.cts.cts_id) == [1, 2] and pub.cts.version == 2
    assert [c["path"] for c in changes] == [f"covenants[{cov.covenant_id}].threshold"]
    assert svc.get(pkg.cts.cts_id, 1).cts.covenants[0].threshold == pkg.cts.covenants[0].threshold
    with pytest.raises(ValidationFailure):
        svc.amend(
            pkg.cts.cts_id,
            v2.model_copy(update={"version": 3, "supersedes_version": 2}),
            drafted_by="a",
            checker=checker,
            reason="r",
            rating=ob.obligor.internal_rating,
        )
    assert diff(pkg.cts, pkg.cts) == []


def test_cts_requires_lineage() -> None:
    from domain.cts import Facility, FacilityType
    from platform_core.money import Money

    with pytest.raises(ValueError):
        Facility(
            facility_id="F",
            facility_type=FacilityType.TERM_LOAN,
            amount=Money(amount_paise=1),
            tenor_months=12,
            pricing_spread_bps=100,
        )


# --------------------------------------------------------------------- cases ----------
def test_case_decisions_need_authority_and_distinct_checker(clock, keys) -> None:  # type: ignore[no-untyped-def]
    svc = CaseService(clock, MakerChecker(clock, Signer(keys)))
    scope = ResourceScope("WEST", "MIDCORP", "OBL-1")
    case = svc.open_case(CaseType.COVENANT_BREACH, "OBL-1", scope, "DSCR breach", sla_days=5)
    p = svc.propose(
        case.case_id,
        kind="consequence",
        payload={"c": "cap"},
        proposed_by="agent:cov",
        proposer_kind="agent",
        required_permission=Permission.CONSEQUENCE_APPROVE,
        on_behalf_of="mo-requester",
    )
    rm = _human("rm1", Role.RM)
    with pytest.raises(AuthorizationError):
        svc.decide(case.case_id, p.proposal_id, checker=rm, approve=True, reason="ok")
    with pytest.raises(AuthorizationError):
        svc.decide(case.case_id, p.proposal_id, checker=rm, approve=False, reason="no")
    officer = Principal(
        id="mo1",
        kind="human",
        roles=frozenset({Role.MONITORING_OFFICER}),
        regions=frozenset({"WEST"}),
    )
    svc.decide(
        case.case_id, p.proposal_id, checker=officer, approve=True, reason="breach confirmed"
    )
    assert p.approval is not None and case.state is CaseState.DECIDED
    with pytest.raises(ConflictError):
        svc.decide(case.case_id, p.proposal_id, checker=officer, approve=True, reason="again")
    clock.advance(days=6)
    assert svc.check_sla() == []  # decided cases do not breach


def test_sla_breach(clock, keys) -> None:  # type: ignore[no-untyped-def]
    svc = CaseService(clock, MakerChecker(clock, Signer(keys)))
    c = svc.open_case(CaseType.EWS, "OBL-1", ResourceScope("W", "M", "OBL-1"), "x", sla_days=2)
    clock.advance(days=3)
    assert svc.check_sla() == [c] and svc.check_sla() == []


# ------------------------------------------------------------ sanction compiler --------
def test_grammar_compiler_matches_ground_truth_on_template_letters() -> None:
    rng = random.Random(21)
    for i in range(25):
        ob = make_obligor(rng, i)
        h = generate_history(rng, ob.obligor.obligor_id, ob.trajectory, ob.shock_quarter)
        pkg = make_sanction(rng, ob, h, "template")
        ex = grammar_extract(pkg.letter_lines)
        assert ex.unresolved == [], ex.unresolved
        draft, review = assemble_cts(
            ex,
            lines=pkg.letter_lines,
            document_id=pkg.document_id,
            obligor_id=ob.obligor.obligor_id,
            authority_power_paise=pkg.cts.sanctioning_authority_power.amount_paise,
        )
        assert draft is not None
        truth = pkg.cts
        assert [f.amount for f in draft.facilities] == [f.amount for f in truth.facilities]
        assert [
            (c.threshold, c.operator, c.cure_period_days, c.consequence) for c in draft.covenants
        ] == [(c.threshold, c.operator, c.cure_period_days, c.consequence) for c in truth.covenants]
        assert all(c.citations for c in draft.covenants + draft.facilities)


def test_compiler_never_invents_values() -> None:
    ex = grammar_extract(["SANCTION LETTER  Ref: X1", "garbled text without terms"])
    draft, review = assemble_cts(
        ex, lines=["a", "b"], document_id="D", obligor_id="OBL-000001", authority_power_paise=1
    )
    assert draft is None and any(r.startswith("missing:") for r in review)
