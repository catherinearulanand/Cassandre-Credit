"""Regression tests for the Phase 2 hostile review (P2-01 .. P2-16) and the rewritten core tests."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from authz import MakerChecker, Principal, ResourceScope, Role
from case_service.domain import CaseService
from entity_resolution.domain import EntityIndex, Query
from ews_service.domain import EwsService, MlScore
from monitoring_calendar.domain import MonitoringCalendar
from perfection_service.domain import PerfectionService
from platform_core.clock import FixedClock
from platform_core.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationFailure,
)
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
    r._today = clk.today
    return r


@pytest.fixture(scope="module")
def cts():  # type: ignore[no-untyped-def]
    rng = random.Random(11)
    ob = make_obligor(rng, 1, "healthy")
    h = generate_history(rng, ob.obligor.obligor_id, "healthy")
    return make_sanction(rng, ob, h, "template").cts


def _events() -> tuple[list[tuple[str, Any]], Any]:
    seen: list[tuple[str, Any]] = []
    return seen, lambda t, p: seen.append((t, p))


def _cases(clk: FixedClock) -> CaseService:
    return CaseService(clk, MakerChecker(clk, Signer(LocalDevKeyProvider())))


def _doc(**kw: Any) -> dict[str, Any]:
    return dict(document_ref="DOC-1", by="officer-1", **kw)


def _acct(**kw: Any) -> AccountSnapshot:
    base: dict[str, Any] = dict(
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


def _obl(i: int, **kw: Any):  # type: ignore[no-untyped-def]
    b = make_obligor(random.Random(i), i, "healthy").obligor
    return b.model_copy(update=kw) if kw else b


# ----------------------------------------------------------------------- calendar ------
def test_chase_climbs_every_step_and_documents_must_match(clk, reg, cts) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    cal = MonitoringCalendar(clk, reg, emit)
    cal.build(cts, date(2026, 12, 31))
    target = min(
        (o for o in cal.obligations.values() if o.due_date >= date(2026, 5, 1)),
        key=lambda o: o.due_date,
    )
    clk.set(datetime.combine(target.due_date + timedelta(days=1), datetime.min.time(), UTC))
    cal.sweep()
    assert target.status == "overdue" and target.chase_step == 1
    n = len(seen)
    cal.sweep()
    assert len(seen) == n  # same day: nothing new
    clk.advance(days=19)  # 20 days overdue: straight to the top, but every step is recorded
    cal.sweep()
    actions = [
        p.chase_action
        for t, p in seen
        if t == "obligation.overdue" and p.obligation_id == target.obligation_id
    ]
    assert actions == ["portal_message", "email", "rm_task", "raise_signal"]
    kw: dict[str, Any] = dict(
        obligor_id=target.obligor_id, doc_type=target.doc_type, period_end=target.period_end
    )
    for bad in (
        {"obligor_id": "OBL-OTHER"},
        {"doc_type": "audited_financials" if target.doc_type == "qis" else "qis"},
        {"period_end": date(1999, 1, 31)},
    ):
        with pytest.raises(ValidationFailure):
            cal.receive(target.obligation_id, "D", **{**kw, **bad})
    cal.receive(target.obligation_id, "DOC-1", **kw)
    cal.receive(target.obligation_id, "DOC-1", **kw)  # idempotent
    with pytest.raises(ConflictError):
        cal.receive(target.obligation_id, "DOC-2", **kw)


def test_P2_04_05_amendment_updates_rows_in_place(clk, reg, cts) -> None:  # type: ignore[no-untyped-def]
    cal = MonitoringCalendar(clk, reg)
    cal.build(cts, date(2026, 12, 31))
    total = len(cal.obligations)
    certs = [o for o in cal.obligations.values() if o.doc_type.startswith("compliance_certificate")]
    qis_future = next(
        o for o in cal.obligations.values() if o.doc_type == "qis" and o.due_date > clk.today()
    )
    qis_future.status, qis_future.chase_step = "overdue", 3
    done = next(
        o for o in cal.obligations.values() if o.doc_type == "qis" and o.due_date < clk.today()
    )
    cal.receive(
        done.obligation_id,
        "DOC-OK",
        obligor_id=done.obligor_id,
        doc_type="qis",
        period_end=done.period_end,
    )
    # same CTS id, covenants removed: certificate rows are waived, nothing is duplicated
    out = cal.reconcile_amendment(cts.model_copy(update={"covenants": []}), date(2026, 12, 31))
    open_certs = [o for o in certs if o.due_date <= date(2026, 12, 31)]
    assert (
        out["added"] == 0
        and out["waived"] == len([o for o in open_certs if o.status == "waived"]) > 0
    )
    assert all(o.status in ("waived", "received") for o in open_certs)
    assert done.status == "received" and done.received_document_id == "DOC-OK"
    # a new CTS id: received stays received, overdue keeps its chase history, no duplicates
    renamed = cts.model_copy(
        update={
            "cts_id": "CTS-NEW",
            "version": 2,
            "supersedes_version": 1,
            "amendment_sanction_ref": "AM-1",
        }
    )
    out2 = cal.reconcile_amendment(renamed, date(2026, 12, 31))
    assert out2["added"] == 0 and done.status == "received" and qis_future.chase_step == 3
    assert qis_future.cts_id == "CTS-NEW" and len(cal.obligations) == total
    assert cal.open_overdue(qis_future.obligor_id)  # still visible as overdue


# --------------------------------------------------------------------------- SMA ------
def test_sma_buckets_and_single_event_per_change(clk, reg) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    svc = SmaService(clk, reg, emit)
    assert svc.classify(_acct()).bucket == "STANDARD"
    assert svc.classify(_acct(dpd=45)).bucket == "SMA-1"
    svc.classify(_acct(dpd=45))
    assert [e[0] for e in seen].count("sma.changed") == 1
    assert svc.classify(_acct(dpd=95, npa_since=clk.today())).bucket == "NPA"
    assert [h["to"] for h in svc.history("ACC-1") if h["kind"] == "bucket"] == ["SMA-1", "NPA"]


def test_P2_07_npa_age_comes_from_the_clock_and_unknown_age_is_refused(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = SmaService(clk, reg)
    with pytest.raises(ValidationFailure):
        svc.classify(_acct(dpd=500))  # an NPA with no known start date is never guessed
    first = svc.classify(_acct(dpd=120, npa_since=clk.today() - timedelta(days=800), secured=False))
    assert first.irac_class not in (None, "SUBSTANDARD")  # two-year-old NPA is not "new"
    clk.advance(days=400)
    later = svc.classify(_acct(dpd=120, secured=False))  # age keeps growing without the caller
    assert float(later.provision_pct or 0) >= float(first.provision_pct or 0)
    with pytest.raises(ValidationFailure):
        svc.classify(_acct(account_id="A9", dpd=120, npa_since=clk.today() + timedelta(days=1)))


def test_npa_does_not_upgrade_on_partial_payment_or_contradictory_flags(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = SmaService(clk, reg)
    assert svc.classify(_acct(dpd=120, npa_since=clk.today())).bucket == "NPA"
    assert svc.classify(_acct(dpd=10, arrears_cleared=False)).bucket == "NPA"
    with pytest.raises(ValidationFailure):  # P2-08: "cleared" while days past due remain
        _acct(dpd=80, arrears_cleared=True)
    assert svc.classify(_acct(dpd=0, arrears_cleared=True)).bucket == "STANDARD"


def test_sma_revolving_uses_days_over_limit(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = SmaService(clk, reg)
    c = svc.classify(
        _acct(
            revolving=True,
            days_over_limit=100,
            secured=False,
            npa_since=clk.today() - timedelta(days=10),
        )
    )
    assert c.bucket == "NPA" and c.irac_class is not None and c.pack_unverified


def test_borrower_view_contagion_and_crilc(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = SmaService(clk, reg)
    svc.classify(_acct(account_id="A1", dpd=100, npa_since=clk.today()))
    svc.classify(_acct(account_id="A2", dpd=0))
    v = svc.borrower_view("OBL-1")
    assert v["worst_bucket"] == "NPA" and v["contagion_review"] == ["A2"]
    assert svc.crilc_reportable("OBL-1", 10**12)["reportable"]
    assert not svc.crilc_reportable("OBL-1", 1)["reportable"]
    with pytest.raises(NotFoundError):  # P2-15: unknown is not "clear"
        svc.crilc_reportable("OBL-UNKNOWN", 10**12)


# --------------------------------------------------------------------------- RFA ------
def test_P2_01_show_cause_cannot_be_backdated_or_anonymous(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = RfaService(clk, reg, _cases(clk))
    f = svc.trigger("OBL-1", SCOPE, indicators=["a"])
    with pytest.raises(ValidationFailure):
        svc.record_show_cause(f.rfa_id, **_doc(issued_on=date(2020, 1, 1)))
    with pytest.raises(ValidationFailure):
        svc.record_show_cause(f.rfa_id, document_ref=" ", by="x")
    with pytest.raises(ValidationFailure):
        svc.record_show_cause(f.rfa_id, **_doc(issued_on=clk.today() + timedelta(days=1)))
    assert not svc.committee_ready(f.rfa_id)[0]


def test_fraud_classification_needs_natural_justice_and_files_can_be_closed(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = RfaService(clk, reg, _cases(clk))
    f = svc.trigger("OBL-1", SCOPE, indicators=["EWS02_overdrawn_days"])
    assert svc.is_flagged("OBL-1")
    kw: dict[str, Any] = dict(
        proposed_by="agent:ews",
        proposer_kind="agent",
        on_behalf_of="mo1",
        classification="fraud",
        rationale="r",
    )
    with pytest.raises(ConflictError):
        svc.propose_classification(f.rfa_id, **kw)
    svc.record_show_cause(f.rfa_id, **_doc())
    with pytest.raises(ConflictError):
        svc.propose_classification(f.rfa_id, **kw)  # window still open
    clk.advance(days=22)
    assert svc.propose_classification(f.rfa_id, **kw)
    with pytest.raises(ConflictError):
        svc.propose_classification(f.rfa_id, **kw)
    with pytest.raises(ConflictError):  # P2-02: new evidence while a decision is pending
        svc.trigger("OBL-1", SCOPE, indicators=["brand_new_allegation"])
    svc.close_file(f.rfa_id, reason="committee rejected", by="committee-1")
    assert not svc.is_flagged("OBL-1")
    f2 = svc.trigger("OBL-1", SCOPE, indicators=["brand_new_allegation"])
    assert f2.rfa_id != f.rfa_id and f2.show_cause_issued_on is None


def test_P2_02_new_evidence_resets_the_notice(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = RfaService(clk, reg, _cases(clk))
    f = svc.trigger("OBL-1", SCOPE, indicators=["a"])
    svc.record_show_cause(f.rfa_id, **_doc())
    clk.advance(days=30)
    assert svc.committee_ready(f.rfa_id)[0]
    svc.trigger("OBL-1", SCOPE, indicators=["a", "b"])  # "b" was never put to the borrower
    assert f.show_cause_issued_on is None and not svc.committee_ready(f.rfa_id)[0]
    with pytest.raises(ValidationFailure):
        svc.trigger("OBL-2", SCOPE, indicators=[""])


def test_P2_03_response_needs_a_document_and_dates_are_validated(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = RfaService(clk, reg, _cases(clk))
    f = svc.trigger("OBL-1", SCOPE, indicators=["x"])
    with pytest.raises(ConflictError):
        svc.record_response(f.rfa_id, **_doc())
    svc.record_show_cause(f.rfa_id, **_doc())
    with pytest.raises(ValidationFailure):
        svc.record_response(f.rfa_id, document_ref="", by="x")
    clk.advance(days=3)
    svc.record_response(f.rfa_id, **_doc())
    assert svc.committee_ready(f.rfa_id)[0]
    with pytest.raises(ValidationFailure):
        svc.trigger("OBL-9", SCOPE, indicators=[], ews_score=10)
    clk.advance(days=200)
    assert svc.overdue_files() == [f]


def test_rfa_classification_needs_the_committee_permission(clk, reg) -> None:  # type: ignore[no-untyped-def]
    cases = _cases(clk)
    svc = RfaService(clk, reg, cases)
    f = svc.trigger("OBL-1", SCOPE, indicators=["x"])
    svc.record_show_cause(f.rfa_id, **_doc())
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
    with pytest.raises(AuthorizationError):
        cases.decide(f.case_id, pid, checker=officer, approve=True, reason="ok")


# --------------------------------------------------------------------------- EWS ------
def test_ews_fuses_rules_and_model_with_hysteresis(clk, reg) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    svc = EwsService(clk, reg, emit)
    s = svc.update("OBL-1", {"cheque_returns_90d": 4, "overdrawn_days_90d": 20})
    assert s.level == "critical" and s.score == 70 and "EWS01_cheque_returns" in s.fired
    assert s.data_gap and any(t == "signal.raised" for t, _ in seen)
    n = len(seen)
    svc.update("OBL-1", {"cheque_returns_90d": 4, "overdrawn_days_90d": 20})
    assert len(seen) == n
    s2 = svc.update("OBL-1", {"cheque_returns_90d": 4, "overdrawn_days_90d": 2})
    assert s2.score == 35 and s2.level == "watch"  # 35 is below the 38 needed to hold "elevated"
    s3 = svc.update("OBL-2", {}, MlScore(90.0, 0.2, ({"feature": "overdrawn_days"},), "ews-lgbm-1"))
    assert s3.level == "critical" and s3.score == 90


def test_P2_06_data_blackout_is_a_visible_alert(clk, reg) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    svc = EwsService(clk, reg, emit)
    s = svc.update("OBL-DARK", {})
    assert s.level == "none" and s.data_gap
    assert any(t == "signal.raised" and p.signal_type == "ews_data_gap" for t, p in seen)
    q = svc.queue(10)
    assert q["shown"] == ["OBL-DARK"] and q["data_gap_obligors"] == ["OBL-DARK"]
    with pytest.raises(ValidationFailure):
        svc.update("X", {}, MlScore(float("nan"), 0.1, (), "m"))


# --------------------------------------------------------------------- perfection -----
def test_perfection_overdue_once_and_late_cures_are_recorded(clk, reg) -> None:  # type: ignore[no-untyped-def]
    seen, emit = _events()
    svc = PerfectionService(clk, reg, emit)
    svc.require("COL-1", "CERSAI", clk.today() - timedelta(days=40))
    svc.require("COL-1", "CHG-1", clk.today() - timedelta(days=40))
    assert len(svc.sweep()) == 2 and svc.sweep() == []
    n = len(seen)
    svc.complete("COL-1", "CERSAI", clk.today())
    assert svc.status("COL-1", "CERSAI")["status"] == "LATE"
    assert len(seen) == n + 1  # P2-13: the late cure leaves a record
    with pytest.raises(ConflictError):
        svc.require("COL-1", "CERSAI", clk.today() - timedelta(days=1))
    with pytest.raises(ValidationFailure):
        svc.require("COL-2", "NeSL", clk.today())


# -------------------------------------------------------------------------- entity -----
def test_P2_09_10_11_entity_index_is_atomic_and_catches_contradictions() -> None:
    idx = EntityIndex()
    a, b = _obl(1), _obl(2)
    idx.add(a)
    with pytest.raises(ValueError):
        idx.add(b.model_copy(update={"gstins": a.gstins}))
    assert idx.resolve(Query(pan=b.pan)).status == "none"  # no ghost identifiers left behind
    with pytest.raises(ValueError):
        idx.add(b.model_copy(update={"pan": "XXXX"}))
    with pytest.raises(ValueError):
        idx.add(b.model_copy(update={"gstins": [a.gstins[0]]}))
    idx.add(b)
    assert idx.resolve(Query(pan=a.pan, cin=_obl(7).cin)).status == "conflict"
    sister = _obl(3, group_id="G1", legal_name="India Industries Limited")
    base = _obl(4, group_id="G1", promoter_names=["Ramesh Kumar Iyer", "राम कुमार"])
    idx.add(sister)
    idx.add(base)
    assert idx.is_related_payee(base.obligor_id, "India Industries Limited")
    assert idx.is_related_payee(base.obligor_id, "R K Iyer")
    assert idx.is_related_payee(base.obligor_id, "राम कुमार")
    assert not idx.is_related_payee(base.obligor_id, "Unrelated Traders")
