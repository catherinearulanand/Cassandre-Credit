"""Phase 2 workflow services: calendar, classification, entity resolution, RFA, EWS, perfection."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest
from authz import MakerChecker, ResourceScope
from case_service.domain import CaseService
from entity_resolution.domain import EntityIndex, Query, normalise_name
from ews_service.domain import EwsService, MlScore
from monitoring_calendar.domain import MonitoringCalendar
from platform_core.clock import FixedClock
from platform_core.errors import ValidationFailure
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


# ------------------------------------------------------------------------- EWS ----------


def test_ews_queue_reports_what_was_cut(clk, reg) -> None:  # type: ignore[no-untyped-def]
    svc = EwsService(clk, reg)
    for i, sc in enumerate([95, 60, 50, 30]):
        svc.update(f"O{i}", {}, MlScore(float(sc), 0.1, (), "m"))
    q = svc.queue(2)
    assert q["shown"] == ["O0", "O1"] and q["not_shown"] == 2 and q["total_alerts"] == 4
    with pytest.raises(ValidationFailure):
        svc.queue(0)


# ------------------------------------------------------------------- perfection --------
