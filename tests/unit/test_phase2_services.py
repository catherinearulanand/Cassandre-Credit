"""Phase 2 workflow services: calendar, classification, entity resolution, RFA, EWS, perfection."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest
from authz import MakerChecker, Permission, Principal, ResourceScope, Role
from case_service.domain import CaseService
from entity_resolution.domain import EntityIndex, Query, normalise_name
from ews_service.domain import EwsService, MlScore
from monitoring_calendar.domain import MonitoringCalendar
from perfection_service.domain import PerfectionService
from platform_core.clock import FixedClock
from platform_core.errors import ConflictError, ValidationFailure
from rfa_service.domain import RfaService
from rules_engine import RuleRegistry, default_registry
from security.crypto import LocalDevKeyProvider, Signer
from sma_service.domain import AccountSnapshot, SmaService
from testing.synthetic.cts_gen import make_sanction
from testing.synthetic.financials import generate_history
from testing.synthetic.obligors import make_obligor

SCOPE = ResourceScope("WEST", "MIDCORP", "OBL-1")


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(datetime(2026, 10, 21, 9, 0, tzinfo=UTC))


@pytest.fixture
def reg(clk: FixedClock) -> RuleRegistry:
    r = default_registry()
    r._today = clk.today  # rules read "today" from the same clock as the service
    return r


def _events() -> tuple[list[tuple[str, object]], object]:
    seen: list[tuple[str, object]] = []
    return seen, lambda t, p: seen.append((t, p))


@pytest.fixture(scope="module")
def cts():  # type: ignore[no-untyped-def]
    rng = random.Random(11)
    ob = make_obligor(rng, 1, "healthy")
    h = generate_history(rng, ob.obligor.obligor_id, "healthy")
    return make_sanction(rng, ob, h, "template").cts


# --------------------------------------------------------------- monitoring calendar ------
def test_calendar_builds_idempotently_from_cts(clk, reg, cts) -> None:  # type: ignore[no-untyped-def]
    cal = MonitoringCalendar(clk, reg)
    first = cal.build(cts, date(2026, 12, 31))
    assert first and {o.doc_type.split(":")[0] for o in first} >= {"qis", "stock_statement"}
    assert cal.build(cts, date(2026, 12, 31)) == []  # no duplicates on rebuild
    ids = [o.obligation_id for o in first]
    assert len(ids) == len(set(ids))


def test_calendar_overdue_chase_escalates_once_per_step(clk, reg, cts) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    cal = MonitoringCalendar(clk, reg, emit)  # type: ignore[arg-type]
    cal.build(cts, date(2026, 12, 31))
    target = min(
        (o for o in cal.obligations.values() if o.due_date >= date(2026, 5, 1)),
        key=lambda o: o.due_date,
    )
    clk.set(datetime.combine(target.due_date + timedelta(days=1), datetime.min.time(), UTC))
    cal.sweep()
    assert target.status == "overdue" and target.chase_step == 1
    n = len([e for e in seen if e[0] == "obligation.overdue"])
    cal.sweep()  # same day: nothing new
    assert len([e for e in seen if e[0] == "obligation.overdue"]) == n
    clk.advance(days=7)
    cal.sweep()
    assert target.chase_step >= 3  # rm_task reached
    cal.receive(target.obligation_id, "DOC-1")
    assert target.status == "received"
    cal.receive(target.obligation_id, "DOC-1")  # idempotent
    with pytest.raises(ConflictError):
        cal.receive(target.obligation_id, "DOC-2")


# ------------------------------------------------------------------------- SMA ----------
def _acct(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        account_id="ACC-1",
        obligor_id="OBL-1",
        revolving=False,
        dpd=0,
        days_over_limit=0,
        outstanding_paise=10_000_00,
        secured=True,
    )
    base.update(kw)
    return AccountSnapshot(**base)


def test_sma_buckets_and_single_event_per_change(clk, reg) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    svc = SmaService(clk, reg, emit)  # type: ignore[arg-type]
    assert svc.classify(_acct()).bucket == "STANDARD"
    assert svc.classify(_acct(dpd=45)).bucket == "SMA-1"
    svc.classify(_acct(dpd=45))  # same bucket again: no new event
    assert [e[0] for e in seen].count("sma.changed") == 1
    assert svc.classify(_acct(dpd=95)).bucket == "NPA"
    assert [h["to"] for h in svc.history("ACC-1") if h["kind"] == "bucket"] == ["SMA-1", "NPA"]


def test_npa_does_not_upgrade_on_partial_payment(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = SmaService(clk, reg)
    assert svc.classify(_acct(dpd=120, months_as_npa=2)).bucket == "NPA"
    c = svc.classify(_acct(dpd=10, arrears_cleared=False, months_as_npa=2))
    assert c.bucket == "NPA"
    assert svc.classify(_acct(dpd=0, arrears_cleared=True)).bucket == "STANDARD"


def test_sma_revolving_uses_days_over_limit_and_irac_provision(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = SmaService(clk, reg)
    c = svc.classify(_acct(revolving=True, days_over_limit=100, secured=False))
    assert c.bucket == "NPA" and c.irac_class is not None and c.provision_pct is not None
    assert c.pack_unverified  # shipped packs are unverified and say so


def test_sma_rejects_bad_snapshots_and_flags_ecl_not_in_force(clk, reg) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationFailure):
        _acct(dpd=-1)
    with pytest.raises(ValidationFailure):
        _acct(dpd=True)
    c = SmaService(clk, reg).classify(_acct(dpd=5))
    assert c.stage is None  # ECL directions start 1 Apr 2027
    late = FixedClock(datetime(2027, 5, 1, tzinfo=UTC))
    reg2 = default_registry()
    reg2._today = late.today
    c2 = SmaService(late, reg2).classify(_acct(dpd=45))
    assert c2.stage == 2


def test_borrower_view_flags_contagion_and_crilc(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = SmaService(clk, reg)
    svc.classify(_acct(account_id="A1", dpd=100))
    svc.classify(_acct(account_id="A2", dpd=0))
    v = svc.borrower_view("OBL-1")
    assert v["worst_bucket"] == "NPA" and v["contagion_review"] == ["A2"]
    big = svc.crilc_reportable("OBL-1", 10**12)
    small = svc.crilc_reportable("OBL-1", 1)
    assert big["reportable"] and not small["reportable"]


# ------------------------------------------------------------- entity resolution -------
def _obl(i: int, **kw):  # type: ignore[no-untyped-def]
    b = make_obligor(random.Random(i), i, "healthy").obligor
    return b.model_copy(update=kw) if kw else b


def test_identifier_match_is_certain_and_name_match_needs_a_human() -> None:
    idx = EntityIndex()
    a, b = _obl(1), _obl(2)
    idx.add(a)
    idx.add(b)
    r = idx.resolve(Query(pan=a.pan))
    assert r.status == "certain" and r.matches[0].obligor_id == a.obligor_id
    r2 = idx.resolve(Query(name=a.legal_name.upper().replace("PVT LTD", "PRIVATE LIMITED")))
    assert r2.status == "candidates" and r2.needs_human
    assert not r2.matches[0].certain


def test_conflicting_or_malformed_identifiers_are_surfaced() -> None:
    idx = EntityIndex()
    a, b = _obl(1), _obl(2)
    idx.add(a)
    idx.add(b)
    assert idx.resolve(Query(pan=a.pan, cin=b.cin)).status == "conflict"
    assert idx.resolve(Query(pan="NOTAPAN")).status == "invalid_identifier"
    assert idx.resolve(Query(pan=a.pan, gstin=b.gstins[0])).status == "conflict"
    assert idx.resolve(Query(name="Completely Unrelated Holdings")).status == "none"
    with pytest.raises(ValueError):
        idx.add(_obl(3, pan=a.pan))  # one PAN, two obligors


def test_related_party_payee_screening() -> None:
    idx = EntityIndex()
    a = _obl(1, group_id="G1", promoter_names=["Ramesh Kumar Iyer"])
    sister = _obl(2, group_id="G1")
    idx.add(a)
    idx.add(sister)
    assert idx.is_related_payee(a.obligor_id, sister.legal_name.upper())
    assert idx.is_related_payee(a.obligor_id, "Iyer Ramesh Kumar")
    assert not idx.is_related_payee(a.obligor_id, "Unrelated Traders")
    assert normalise_name("ACME Pvt. Ltd.") == normalise_name("Acme Private Limited")


# ------------------------------------------------------------------------- RFA ----------
def _cases(clk):  # type: ignore[no-untyped-def]
    keys = LocalDevKeyProvider()
    return CaseService(clk, MakerChecker(clk, Signer(keys)))


def test_fraud_classification_cannot_be_proposed_before_natural_justice(clk, reg) -> None:  # type: ignore[no-untyped-def]
    cases = _cases(clk)
    svc = RfaService(clk, reg, cases)
    f = svc.trigger("OBL-1", SCOPE, indicators=["EWS02_overdrawn_days"])
    assert svc.trigger("OBL-1", SCOPE, indicators=["EWS05_lc_devolvement"]) is f  # one open file
    assert len(f.triggers) == 2
    kw = dict(
        proposed_by="agent:ews",
        proposer_kind="agent",
        on_behalf_of="mo1",
        classification="fraud",
        rationale="r",
    )
    with pytest.raises(ConflictError):
        svc.propose_classification(f.rfa_id, **kw)  # type: ignore[arg-type]
    svc.record_show_cause(f.rfa_id)
    with pytest.raises(ConflictError):
        svc.propose_classification(f.rfa_id, **kw)  # type: ignore[arg-type]  # window still open
    clk.advance(days=22)
    pid = svc.propose_classification(f.rfa_id, **kw)  # type: ignore[arg-type]
    assert pid
    with pytest.raises(ConflictError):
        svc.propose_classification(f.rfa_id, **kw)  # type: ignore[arg-type]


def test_rfa_response_inside_window_unlocks_committee_and_dates_are_validated(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = RfaService(clk, reg, _cases(clk))
    f = svc.trigger("OBL-1", SCOPE, indicators=["x"])
    with pytest.raises(ConflictError):
        svc.record_response(f.rfa_id)  # no notice yet
    with pytest.raises(ValidationFailure):
        svc.record_show_cause(f.rfa_id, clk.today() + timedelta(days=1))
    svc.record_show_cause(f.rfa_id)
    clk.advance(days=3)
    svc.record_response(f.rfa_id)
    assert svc.committee_ready(f.rfa_id)[0]
    with pytest.raises(ValidationFailure):
        svc.trigger("OBL-9", SCOPE, indicators=[], ews_score=10)
    clk.advance(days=200)
    assert svc.overdue_files() == [f]


def test_rfa_proposal_requires_committee_permission(clk, reg) -> None:  # type: ignore[no-untyped-def]
    cases = _cases(clk)
    svc = RfaService(clk, reg, cases)
    f = svc.trigger("OBL-1", SCOPE, indicators=["x"])
    svc.record_show_cause(f.rfa_id)
    clk.advance(days=22)
    pid = svc.propose_classification(
        f.rfa_id,
        proposed_by="agent:ews",
        proposer_kind="agent",
        on_behalf_of="mo1",
        classification="fraud",
        rationale="r",
    )
    officer = Principal(id="mo1", kind="human", roles=frozenset({Role.MONITORING_OFFICER}))
    from platform_core.errors import AuthorizationError

    with pytest.raises(AuthorizationError):  # a monitoring officer is not the fraud committee
        cases.decide(f.case_id, pid, checker=officer, approve=True, reason="ok")
    assert Permission.RFA_CLASSIFY not in officer.permissions()


# ------------------------------------------------------------------------- EWS ----------
def test_ews_fuses_rules_and_model_with_hysteresis_and_data_gap(clk, reg) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    svc = EwsService(clk, reg, emit)  # type: ignore[arg-type]
    s = svc.update("OBL-1", {"cheque_returns_90d": 4, "overdrawn_days_90d": 20})
    assert s.level == "critical" and s.score == 70
    assert "EWS01_cheque_returns" in s.fired
    assert s.data_gap  # most indicators had no data: stated, not hidden
    assert any(t == "signal.raised" for t, _ in seen)
    n = len(seen)
    svc.update("OBL-1", {"cheque_returns_90d": 4, "overdrawn_days_90d": 20})
    assert len(seen) == n  # unchanged state: no new events
    # improvement of one band does not clear the alert while the score is above the clear level
    s2 = svc.update("OBL-1", {"cheque_returns_90d": 4, "overdrawn_days_90d": 2})
    assert s2.level in ("elevated", "critical", "watch")
    ml = MlScore(90.0, 0.2, ({"feature": "overdrawn_days"},), "ews-lgbm-1")
    s3 = svc.update("OBL-2", {}, ml)
    assert s3.level == "critical" and s3.score == 90


def test_ews_queue_reports_what_was_cut(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = EwsService(clk, reg)
    for i, sc in enumerate([95, 60, 50, 30]):
        svc.update(f"O{i}", {}, MlScore(float(sc), 0.1, (), "m"))
    q = svc.queue(2)
    assert q["shown"] == ["O0", "O1"] and q["not_shown"] == 2 and q["total_alerts"] == 4
    with pytest.raises(ValidationFailure):
        svc.queue(0)


# ------------------------------------------------------------------- perfection --------
def test_perfection_overdue_announced_once_per_status(clk, reg) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    svc = PerfectionService(clk, reg, emit)  # type: ignore[arg-type]
    svc.require("COL-1", "CERSAI", clk.today() - timedelta(days=40))
    svc.require("COL-1", "CHG-1", clk.today() - timedelta(days=40))
    assert len(svc.sweep()) == 2
    assert svc.sweep() == []
    svc.complete("COL-1", "CERSAI", clk.today())
    assert svc.status("COL-1", "CERSAI")["status"] in ("LATE", "ON_TIME")
    with pytest.raises(ValidationFailure):
        svc.require("COL-2", "NeSL", clk.today())
