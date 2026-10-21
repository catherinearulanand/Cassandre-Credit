"""Phase 3 workflow services: stressed assets, recovery, closure and review."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from authz import MakerChecker, Principal, ResourceScope, Role
from case_service.domain import CaseService
from closure_service.domain import RELEASE_ACTIONS, ClosureFacts, ClosureService
from platform_core.clock import FixedClock
from platform_core.errors import ConflictError, IntegrityError, ValidationFailure
from recovery_service.domain import RecoveryService
from review_service.domain import REQUIRED_SECTIONS, ReviewSchedule, ReviewService
from rules_engine import RuleRegistry, default_registry
from security.crypto import LocalDevKeyProvider, Signer
from stressed_assets_service.domain import StressedAssetService

SCOPE = ResourceScope("WEST", "MIDCORP", "OBL-1")


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(datetime(2026, 10, 21, 9, 0, tzinfo=UTC))


@pytest.fixture
def reg(clk: FixedClock) -> RuleRegistry:
    r = default_registry()
    r._today = clk.today
    return r


@pytest.fixture
def mc(clk: FixedClock) -> MakerChecker:
    return MakerChecker(clk, Signer(LocalDevKeyProvider()))


@pytest.fixture
def cases(clk: FixedClock, mc: MakerChecker) -> CaseService:
    return CaseService(clk, mc)


def _head(*roles: Role) -> Principal:
    return Principal(id="decider-1", kind="human", roles=frozenset(roles))


# ----------------------------------------------------------------- stressed assets ------
def test_stressed_clock_and_provisioning_follow_the_pack(clk, reg, cases, mc) -> None:  # type: ignore[no-untyped-def]
    svc = StressedAssetService(clk, reg, cases, mc)
    f = svc.open_file("OBL-1", SCOPE, clk.today() - timedelta(days=400), 5_000_000_00)
    st = svc.status(f.file_id)
    assert st["additional_provision_pct"] == "35" and st["deadline_missed"]
    assert svc.deadline_misses() == [f.file_id]
    assert st["pack_unverified"]
    with pytest.raises(ConflictError):
        svc.open_file("OBL-1", SCOPE, clk.today() - timedelta(days=5), 1_00)  # one open file each


def test_plan_needs_a_signed_human_approval_for_that_proposal(clk, reg, cases, mc) -> None:  # type: ignore[no-untyped-def]
    svc = StressedAssetService(clk, reg, cases, mc)
    f = svc.open_file("OBL-1", SCOPE, clk.today() - timedelta(days=100), 1_000_00)
    other = svc.open_file("OBL-2", SCOPE, clk.today() - timedelta(days=100), 1_000_00)
    pid = svc.propose_plan(
        f.file_id,
        plan_ref="RP-1",
        summary="s",
        proposed_by="agent:x",
        proposer_kind="agent",
        on_behalf_of="mo1",
    )
    with pytest.raises(ConflictError):
        svc.record_implementation(other.file_id, None)  # type: ignore[arg-type]  # no plan proposed
    authority = Principal(
        id="sa1",
        kind="human",
        roles=frozenset({Role.SANCTIONING_AUTHORITY}),
        delegated_power_paise=10**12,
    )
    p = cases.decide(f.case_id, pid, checker=authority, approve=True, reason="plan viable")
    assert p.approval is not None
    svc.record_implementation(f.file_id, p.approval)
    st = svc.status(f.file_id)
    assert (
        st["plan_implemented"]
        and not st["deadline_missed"]
        and st["additional_provision_pct"] == "0"
    )
    # an approval for another proposal cannot implement this file
    pid2 = svc.propose_plan(
        other.file_id,
        plan_ref="RP-2",
        summary="s2",
        proposed_by="agent:x",
        proposer_kind="agent",
        on_behalf_of="mo1",
    )
    p2 = cases.decide(other.case_id, pid2, checker=authority, approve=True, reason="ok")
    assert p2.approval is not None
    with pytest.raises((ConflictError, IntegrityError)):
        svc.record_implementation(f.file_id, p2.approval)


def test_stress_inputs_are_validated(clk, reg, cases, mc) -> None:  # type: ignore[no-untyped-def]
    svc = StressedAssetService(clk, reg, cases, mc)
    with pytest.raises(ValidationFailure):
        svc.open_file("O", SCOPE, clk.today() + timedelta(days=1), 100)
    with pytest.raises(ValidationFailure):
        svc.open_file("O", SCOPE, clk.today(), 0)
    with pytest.raises(ValidationFailure):
        svc.open_file("O", SCOPE, clk.today(), True)


# ---------------------------------------------------------------------- recovery -------
def test_ots_analysis_is_exact_and_flags_offers_below_security(clk, reg, cases) -> None:  # type: ignore[no-untyped-def]
    a = RecoveryService.analyse_ots(1_000_000_00, 400_000_00, 600_000_00)
    assert (
        str(a.recovery_ratio) == "0.4000"
        and a.below_security_value
        and a.haircut_paise == 600_000_00
    )
    b = RecoveryService.analyse_ots(3, 1, 0)
    assert str(b.recovery_ratio) == "0.3333"  # rounded down, never up in the borrower's favour
    for bad in ((0, 0, 0), (100, 101, 0), (100, -1, 0), (100, 1, True)):
        with pytest.raises(ValidationFailure):
            RecoveryService.analyse_ots(*bad)  # type: ignore[arg-type]


def test_ots_proposal_needs_recovery_head(clk, reg, cases) -> None:  # type: ignore[no-untyped-def]
    svc = RecoveryService(clk, reg, cases)
    case = cases.open_case(
        __import__("domain.entities", fromlist=["CaseType"]).CaseType.RECOVERY,
        "OBL-1",
        SCOPE,
        "ots",
        sla_days=10,
    )
    a = svc.analyse_ots(100_00, 50_00, 40_00)
    pid = svc.propose_ots(
        case.case_id,
        analysis=a,
        terms="t",
        proposed_by="agent:r",
        proposer_kind="agent",
        on_behalf_of="rh0",
    )
    from platform_core.errors import AuthorizationError

    with pytest.raises(AuthorizationError):
        cases.decide(
            case.case_id, pid, checker=_head(Role.MONITORING_OFFICER), approve=True, reason="x"
        )
    with pytest.raises(AuthorizationError):  # money approval must state the amount
        cases.decide(case.case_id, pid, checker=_head(Role.RECOVERY_HEAD), approve=True, reason="x")


def test_wilful_designation_only_after_every_step(clk, reg, cases) -> None:  # type: ignore[no-untyped-def]
    svc = RecoveryService(clk, reg, cases)
    big = 9_000_000_00
    assert svc.wilful_status(100_00)["status"] == "NOT_ELIGIBLE"
    assert svc.wilful_status(big)["status"] == "SHOW_CAUSE_REQUIRED"
    st = svc.wilful_status(
        big, show_cause_issued_on=clk.today() - timedelta(days=30), review_committee_confirmed=False
    )
    assert st["status"] == "REVIEW_COMMITTEE_REQUIRED"
    case = cases.open_case(
        __import__("domain.entities", fromlist=["CaseType"]).CaseType.RECOVERY,
        "OBL-1",
        SCOPE,
        "w",
        sla_days=5,
    )
    with pytest.raises(ConflictError):
        svc.propose_wilful_designation(
            case.case_id,
            status=st,
            rationale="r",
            proposed_by="a",
            proposer_kind="agent",
            on_behalf_of="h",
        )
    ok = svc.wilful_status(
        big,
        show_cause_issued_on=clk.today() - timedelta(days=30),
        review_committee_confirmed=True,
    )
    assert ok["status"] == "MAY_CLASSIFY"
    assert svc.propose_wilful_designation(
        case.case_id,
        status=ok,
        rationale="r",
        proposed_by="agent:r",
        proposer_kind="agent",
        on_behalf_of="h",
    )


def test_penal_charge_check(clk, reg, cases) -> None:  # type: ignore[no-untyped-def]
    svc = RecoveryService(clk, reg, cases)
    assert svc.check_penal_charge(
        kind="penal_charge", capitalise=False, disclosed_in_agreement=True
    )["allowed"]
    bad = svc.check_penal_charge(
        kind="penal_interest", capitalise=True, disclosed_in_agreement=False
    )
    assert not bad["allowed"] and len(bad["reasons"]) == 3


# ----------------------------------------------------------------------- closure -------
def _facts(clk, **kw):  # type: ignore[no-untyped-def]
    base = dict(
        obligor_id="OBL-1",
        facility_ids=("F1", "F2"),
        outstanding_paise={"F1": 0, "F2": 0},
        contingent_paise={"F1": 0, "F2": 0},
        pending_finacle_commands=0,
        open_drawdown_requests=0,
        repaid_on=clk.today() - timedelta(days=10),
    )
    base.update(kw)
    return ClosureFacts(**base)


def test_closure_blockers_treat_unknown_as_not_zero(clk) -> None:  # type: ignore[no-untyped-def]
    assert ClosureService.blockers(_facts(clk)) == []
    assert ClosureService.blockers(_facts(clk, outstanding_paise={"F1": 0}))  # F2 unknown
    assert ClosureService.blockers(_facts(clk, contingent_paise={"F1": 5, "F2": 0}))
    assert ClosureService.blockers(_facts(clk, pending_finacle_commands=1))
    assert ClosureService.blockers(_facts(clk, open_drawdown_requests=1))
    assert ClosureService.blockers(_facts(clk, facility_ids=()))
    assert ClosureService.blockers(_facts(clk, outstanding_paise={"F1": True, "F2": 0}))


def test_closure_end_to_end_with_signed_approval_and_release_clock(clk, reg, cases, mc) -> None:  # type: ignore[no-untyped-def]
    from domain.entities import CaseType

    svc = ClosureService(clk, reg, cases, mc)
    case = cases.open_case(CaseType.CLOSURE, "OBL-1", SCOPE, "close", sla_days=30)
    with pytest.raises(ConflictError):
        svc.propose_closure(
            case.case_id,
            _facts(clk, outstanding_paise={"F1": 1, "F2": 0}),
            proposed_by="agent:c",
            proposer_kind="agent",
            on_behalf_of="mo",
        )
    cf = svc.propose_closure(
        case.case_id, _facts(clk), proposed_by="agent:c", proposer_kind="agent", on_behalf_of="mo"
    )
    st = svc.release_status(case.case_id)
    assert st["days_delayed"] == 0 and len(st["missing_actions"]) == len(RELEASE_ACTIONS)
    approver = Principal(id="sa1", kind="human", roles=frozenset({Role.SANCTIONING_AUTHORITY}))
    assert cf.proposal_id is not None
    p = cases.decide(
        case.case_id, cf.proposal_id, checker=approver, approve=True, reason="dues nil"
    )
    assert p.approval is not None
    for a in RELEASE_ACTIONS[:-1]:
        svc.record_action(case.case_id, a, f"EV-{a}")
    with pytest.raises(ConflictError):
        svc.complete(case.case_id, approval=p.approval)  # one release action outstanding
    svc.record_action(case.case_id, RELEASE_ACTIONS[-1], "EV-last")
    ev = svc.complete(case.case_id, approval=p.approval)
    assert ev.facility_ids == ["F1", "F2"] and ev.zero_dues_proof == cf.proof
    # delay compensation after the window, and only when attributable to the lender
    clk.advance(days=40)
    cf.released_on = None
    late = svc.release_status(case.case_id)
    assert late["days_delayed"] == 20 and late["compensation_rupees"] == "100000"
    assert (
        svc.release_status(case.case_id, attributable_to_lender=False)["compensation_rupees"] == "0"
    )
    with pytest.raises(ValidationFailure):
        svc.record_action(case.case_id, "burn_documents", "x")


# ------------------------------------------------------------------------ review -------
def test_review_due_dates_and_watch_list_tighten_cadence(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = ReviewService(clk, reg)
    svc.register(ReviewSchedule("OBL-1", "CR2", date(2026, 1, 31)))
    svc.register(ReviewSchedule("OBL-2", "CR5", date(2026, 1, 31)))
    svc.register(ReviewSchedule("OBL-3", "CR1", date(2026, 6, 30), watch_list=True))
    assert svc.due("OBL-1")["next_review_due"] == "2027-01-31" and not svc.due("OBL-1")["overdue"]
    assert svc.overdue_reviews() == ["OBL-2", "OBL-3"]
    svc.complete("OBL-2", date(2026, 10, 1))
    assert "OBL-2" not in svc.overdue_reviews()
    with pytest.raises(ValidationFailure):
        svc.complete("OBL-2", clk.today() + timedelta(days=1))


def test_review_pack_lists_missing_sections_and_hashes_content(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = ReviewService(clk, reg)
    inputs = {"rating": ("CR3", "rating:2026Q3"), "latest_dscr": ("1.42", "covenant_test:CT-1")}
    pack = svc.build_pack("OBL-1", inputs)
    assert set(pack.missing) == set(REQUIRED_SECTIONS) - set(inputs)
    assert pack.pack_hash == svc.build_pack("OBL-1", dict(inputs)).pack_hash
    other = svc.build_pack("OBL-1", {**inputs, "latest_dscr": ("1.43", "covenant_test:CT-1")})
    assert other.pack_hash != pack.pack_hash
    with pytest.raises(ValidationFailure):
        svc.build_pack("OBL-1", {"rating": ("CR3", "")})
