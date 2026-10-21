"""End-to-end demonstration on synthetic borrowers: what the platform does, and what is real.

    python -m sim.demo_story --engine paddle --out reports/demo_story.json

Every step in the timeline is tagged with how it is connected:

* ``REAL``       production code running as it would in the bank (rules, covenant engine, gate, adapter...)
* ``REAL+SYNTH`` production code running on synthetic input (e.g. PaddleOCR reading a synthetic page)
* ``SIMULATED``  a stand-in for something that is not connected (Finacle, human approvers, data feeds)
* ``NOT BUILT``  named so the gap is visible

The people who approve things are played by scripted principals and are labelled ``(simulated human)``.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from authz import MakerChecker, Principal, ResourceScope, Role
from case_service.domain import CaseService
from closure_service.domain import RELEASE_ACTIONS, ClosureFacts, ClosureService
from covenant_engine.domain import evaluate_covenant
from cts_service.domain import CtsService
from domain.entities import CaseType, SignalSeverity
from domain.limit_tree import LimitState, LimitTree
from drawdown_gate.domain import CovenantStatus, DrawdownGate, DrawdownRequest, GateContext
from ews_service.domain import EwsService
from monitoring_calendar.domain import MonitoringCalendar
from platform_core.clock import FixedClock
from recovery_service.domain import RecoveryService
from rfa_service.domain import RfaService
from rules_engine import default_registry
from security.crypto import LocalDevKeyProvider, Signer
from sma_service.domain import AccountSnapshot, SmaService
from stressed_assets_service.domain import StressedAssetService
from testing.synthetic.financials import QUARTER_ENDS, QuarterFinancials

from agents.covenant_agent import agent as covenant_agent
from agents.ews_analyst import agent as ews_analyst
from agents.recovery_agent import agent as recovery_agent
from agents.runtime import AgentRun, AgentSpec, ToolSpec
from agents.sanction_compiler.compiler import assemble_cts, grammar_extract
from ocr.pipeline.extract import extract_statement
from ocr.pipeline.route import hard_blocked
from ocr.pipeline.validate import validate_statement
from ocr.synthetic.render import render_statement
from sim.book import BookEntry, build_book, cts_of, expected_covenant_result
from sim.finacle_demo import FinacleDemo, scenario

ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = date(2026, 10, 21)
STP_CONFIDENCE = 0.97  # uncalibrated demo threshold; the identity requirement does the real work


@dataclass
class Step:
    when: str
    component: str
    mode: str
    text: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Journey:
    obligor: str
    archetype: str
    steps: list[Step] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def add(self, when: date, component: str, mode: str, text: str, **data: Any) -> None:
        self.steps.append(Step(when.isoformat(), component, mode, text, data))


class World:
    """Shared services for one journey, all on one fixed clock."""

    def __init__(self, e: BookEntry, start: date) -> None:
        self.e = e
        self.oid = e.bundle.obligor.obligor_id
        self.clock = FixedClock(datetime.combine(start, datetime.min.time(), UTC))
        self.events: Counter[str] = Counter()
        self.rules = default_registry()
        self.rules._today = lambda: RUN_DATE
        signer = Signer(LocalDevKeyProvider())
        self.mc = MakerChecker(self.clock, signer)
        self.signer = signer
        self.cases = CaseService(self.clock, self.mc)
        self.scope = ResourceScope(e.bundle.obligor.region, e.bundle.obligor.portfolio, self.oid)

        def emit(t: str, p: Any) -> None:
            self.events[t] += 1

        self.emit = emit
        self.sma = SmaService(self.clock, self.rules, emit)
        self.ews = EwsService(self.clock, self.rules, emit)
        self.rfa = RfaService(self.clock, self.rules, self.cases, emit)
        self.calendar = MonitoringCalendar(self.clock, self.rules, emit)
        self.stress = StressedAssetService(self.clock, self.rules, self.cases, self.mc)
        self.recovery = RecoveryService(self.clock, self.rules, self.cases)
        self.closure = ClosureService(self.clock, self.rules, self.cases, self.mc)
        reg = e.bundle.obligor.region
        self.mo_agent_principal = Principal(
            id="mo-priya", kind="human", roles=frozenset({Role.MONITORING_OFFICER}),
            regions=frozenset({reg}),
        )  # fmt: skip
        self.mo_decider = Principal(
            id="mo-arjun", kind="human", roles=frozenset({Role.MONITORING_OFFICER}),
            regions=frozenset({reg}),
        )  # fmt: skip
        self.recovery_head = Principal(
            id="rh-meera", kind="human", roles=frozenset({Role.RECOVERY_HEAD}),
            delegated_power_paise=10**13,
        )  # fmt: skip
        self.sanctioning = Principal(
            id="sa-ravi", kind="human", roles=frozenset({Role.SANCTIONING_AUTHORITY}),
            delegated_power_paise=10**14,
        )  # fmt: skip


def _run_agent(w: World, agent_id: str, tools: dict[str, ToolSpec]) -> AgentRun:
    spec = AgentSpec.load(ROOT / "agents" / agent_id / "policy.yaml")
    return AgentRun(spec, w.mo_agent_principal, lambda name, kw: w.scope, tools, emit=w.emit)


def _propose_tool(w: World) -> ToolSpec:
    return ToolSpec("case.propose", w.cases.propose, "propose")


# ---------------------------------------------------------------------------- stages -----
def stage_sanction(j: Journey, w: World) -> CreditTermSheetLike:
    e = w.e
    pkg = e.package
    ex = grammar_extract(pkg.letter_lines)
    draft, _review = assemble_cts(
        ex,
        lines=pkg.letter_lines,
        document_id=pkg.document_id,
        obligor_id=w.oid,
        authority_power_paise=pkg.cts.sanctioning_authority_power.amount_paise,
    )
    if draft is None or ex.unresolved:
        j.add(
            pkg.cts.sanction_date, "Sanction Compiler", "REAL",
            f"Negotiated agreement: grammar could not read it ({len(ex.unresolved)} unresolved fields). "
            "Routed to a CAD maker.",
            unresolved=len(ex.unresolved),
        )  # fmt: skip
        j.add(
            pkg.cts.sanction_date, "LLM clause reader", "NOT BUILT",
            "A self-hosted model would read the agreement here; no model is connected, so a human "
            "types the terms (simulated human) and the CTS is taken from the generator's truth.",
        )  # fmt: skip
        return pkg.cts
    exact = (
        [f.amount for f in draft.facilities] == [f.amount for f in pkg.cts.facilities]
        and len(draft.covenants) == len(pkg.cts.covenants)
        and all(
            a.threshold == b.threshold and a.formula == b.formula
            for a, b in zip(draft.covenants, pkg.cts.covenants, strict=True)
        )
    )
    j.add(
        pkg.cts.sanction_date, "Sanction Compiler", "REAL",
        f"Read the standard sanction letter: {len(draft.facilities)} facilities, "
        f"{len(draft.covenants)} covenants, every field cited to a line. Matches the letter: {exact}.",
        cited_elements=sum(bool(c.citations) for c in [*draft.facilities, *draft.covenants]),
    )  # fmt: skip
    return pkg.cts


CreditTermSheetLike = Any


def stage_publish(j: Journey, w: World, cts: CreditTermSheetLike) -> None:
    svc = CtsService(w.clock, w.rules, w.signer, w.mc, tier1_capital_rupees=Decimal(10) ** 12)
    checker = Principal(id="cad-checker-sunita", kind="human", roles=frozenset({Role.CAD_CHECKER}))
    svc.publish(
        cts, drafted_by="agent:sanction_compiler", drafted_on_behalf_of="cad-maker-vikram",
        checker=checker, reason="verified against the sanction letter",
        rating=w.e.bundle.obligor.internal_rating,
    )  # fmt: skip
    j.add(
        cts.sanction_date, "CTS service", "REAL",
        "Published the Credit Term Sheet: validated against credit policy and large-exposure limits, "
        "signed, hash-chained. Maker is the agent's human; checker is a different person "
        "(simulated human: Sunita, CAD checker).",
        version=cts.version,
    )  # fmt: skip
    w.clock.set(datetime.combine(cts.sanction_date, datetime.min.time(), UTC))
    added = w.calendar.build(cts, RUN_DATE)
    j.add(
        cts.sanction_date, "Monitoring calendar", "REAL",
        f"Derived {len(added)} obligations from the CTS (stock statements, QIS, audited accounts, "
        "compliance certificates). Due dates come from the monitoring rule pack.",
        obligations=len(added),
    )  # fmt: skip


def stage_finacle(j: Journey, w: World, cts: CreditTermSheetLike) -> FinacleDemo:
    cif = w.e.bundle.obligor.cif
    cc, tl = f"CC-{w.oid[-6:]}", f"TL-{w.oid[-6:]}"
    fin = FinacleDemo(w.clock, scenario(cif, "Synthetic Borrower", cc, tl))
    total = cts.total_sanctioned().amount_paise
    cc_amt = next(f.amount.amount_paise for f in cts.facilities if f.facility_type.revolving)
    st1 = fin.publish_limit("OBL-NODE", total, cc)
    st2 = fin.publish_limit("LIM-CC", cc_amt, cc)
    j.add(
        cts.sanction_date, "Finacle adapter", "REAL adapter + SIMULATED Finacle",
        f"Pushed the limit tree to Finacle through the adapter (signed approvals, idempotent, ordered): "
        f"{st1}, {st2}. The receiving end is the Finacle simulator, not a bank system.",
        cc_limit_paise=cc_amt,
    )  # fmt: skip
    j.summary["cc_account"], j.summary["cc_limit_paise"] = cc, cc_amt
    return fin


class GateCtx:
    """What the gate reads, built from the live services (not hand-written)."""

    def __init__(self, w: World, cc_limit: int, account: str) -> None:
        self.w, self.cc_limit, self.account = w, cc_limit, account
        self.covenant_breached: list[str] = []

    def load(self, facility_id: str) -> GateContext:
        w = self.w
        tree = LimitTree()
        tree.add(LimitState("OBL", None, self.cc_limit * 2))
        tree.add(
            LimitState(
                "CC", "OBL", self.cc_limit, dp=int(self.cc_limit * 0.8), facility_id=self.account
            )
        )
        stops = tuple(
            p.payload["consequence"]
            for c in w.cases.cases.values()
            if c.obligor_id == w.oid
            for p in c.proposals
            if p.kind == "consequence" and p.status == "approved"
        )
        covs = [CovenantStatus("DECIDED-PRECAUTION", "BREACHED", stops)] if stops else []
        for cid in self.covenant_breached:
            covs.append(CovenantStatus(cid, "BREACHED", ()))
        return GateContext(
            cts_valid_until=date(2027, 6, 1), facility_limit_node="CC", open_cps=[],
            overdue_stopping_cs=[], tree=tree, dp_paise=int(self.cc_limit * 0.8), utilised_paise=0,
            covenants=covs, rfa_or_fraud_flag=w.rfa.is_flagged(w.oid),
            sma_bucket=w.sma.borrower_view(w.oid)["worst_bucket"] if w.sma._by_obligor.get(w.oid) else "STANDARD",
            amendment_in_flight=False, approved_payees={"SUP-1"}, facility_id=self.account,
            as_of=w.clock.today(),
        )  # fmt: skip


def try_drawdown(j: Journey, w: World, fin: FinacleDemo, gctx: GateCtx, label: str) -> str:
    gate = DrawdownGate(gctx, w.clock.today)
    amount = max(1_00, gctx.cc_limit // 20)
    req = DrawdownRequest(
        f"DRW-{w.oid[-4:]}-{len(w.events)}-{w.clock.today()}",
        gctx.account,
        amount,
        payee_id="SUP-1",
    )
    d = gate.decide(req)
    j.add(
        w.clock.today(), "Drawdown gate", "REAL",
        f"{label}: {d.decision}" + (f" ({'; '.join(d.reasons)})" if d.reasons else " (all ten checks passed)"),
        latency_ms=round(d.latency_ms, 3), decision=d.decision,
    )  # fmt: skip
    if d.decision == "APPROVE":
        status, ref = fin.disburse(d, req, gctx.account)
        j.add(
            w.clock.today(), "Finacle adapter", "REAL adapter + SIMULATED Finacle",
            f"Disbursed {amount:,} paise: {status}. Balance now {fin.balance(gctx.account):,} paise.",
            status=status, finacle_ref=ref,
        )  # fmt: skip
    return d.decision


def stage_ocr(
    j: Journey, w: World, engine: Any, qi: int, seed: int, level: str = "mild"
) -> dict[str, Any]:
    e = w.e
    rng = random.Random(seed + qi)
    tot = auto_ok = auto_bad = review = 0
    for stmt in ("BS", "PL"):
        page = render_statement(
            rng,
            e.hist.quarters[qi],
            e.hist.quarters[qi - 1] if qi else None,
            stmt,
            level,
            layout="A",
        )
        words = engine.recognise(page.image)
        pw, ph = page.image.size
        ext = extract_statement(words, pw, ph, f"DOC-{w.oid[-4:]}-{stmt}-{qi}")
        results = validate_statement(ext)
        for g in page.fields:
            tot += 1
            f = ext.fields.get(g.code)
            if f is None or hard_blocked(ext, f, results) or f.ocr_conf < STP_CONFIDENCE:
                review += 1
            elif f.value_paise == g.value_paise:
                auto_ok += 1
            else:
                auto_bad += 1
    out = {
        "figures": tot,
        "straight_through_correct": auto_ok,
        "straight_through_wrong": auto_bad,
        "to_reviewer": review,
    }
    j.add(
        QUARTER_ENDS[qi], "OCR + extraction + validation", "REAL+SYNTH",
        f"{engine.name} read the quarter's balance sheet and P&L (synthetic pages, '{level}' scan): "
        f"{auto_ok}/{tot} figures passed straight through correct, {auto_bad} wrong passed through, "
        f"{review} sent to a reviewer. Review is played by a simulated human who confirms from the truth.",
        **out,
    )  # fmt: skip
    return out


def stage_tamper(j: Journey, w: World, engine: Any, qi: int, seed: int) -> None:
    e, rng = w.e, random.Random(seed + 99)
    q = e.hist.quarters[qi]
    code = "trade_receivables"
    forged = dict(q.lines)
    forged[code] = int(forged[code] * 0.7)
    page = render_statement(
        rng,
        QuarterFinancials(q.period_end, forged),
        e.hist.quarters[qi - 1],
        "BS",
        "mild",
        layout="A",
    )
    words = engine.recognise(page.image)
    pw, ph = page.image.size
    ext = extract_statement(words, pw, ph, "DOC-FORGED")
    results = validate_statement(ext)
    violated = [r for r in results if r.status == "violated"]
    flagged = bool(violated) or any(f.validations for f in ext.fields.values())
    j.add(
        QUARTER_ENDS[qi], "Tamper check", "REAL+SYNTH",
        "A borrower edits one receivables figure on the balance sheet. The accounting identities "
        f"{'caught it and held the page for review' if flagged else 'did NOT catch it'}. "
        "(A forgery that also adjusts every dependent total would pass arithmetic: see the OCR report.)",
        flagged=flagged, identities_violated=len(violated),
    )  # fmt: skip


def conduct_metrics(e: BookEntry, qi: int, open_breaches: int) -> dict[str, Any]:
    c = e.conduct[qi]
    return {
        "cheque_returns_90d": c.cheque_returns,
        "overdrawn_days_90d": c.overdrawn_days,
        "utilisation_avg_90d": f"{c.util_avg:.2f}",
        "stock_statement_days_late": c.stock_stmt_late_days,
        "interest_delay_days": c.interest_delay_days,
        "gst_returns_missed_6m": c.gst_gap,
        "covenant_breaches_open": open_breaches,
    }


def covenant_results(w: World, cts: CreditTermSheetLike) -> dict[tuple[str, int], Any]:
    out: dict[tuple[str, int], Any] = {}
    for cov in cts.covenants:
        for qi, s in enumerate(w.e.spreads):
            if cov.first_test_date is not None and s.period_end >= cov.first_test_date:
                out[(cov.covenant_id, qi)] = evaluate_covenant(
                    cov, cts, w.e.spreads, s.period_end,
                    tested_at=datetime.combine(s.period_end, datetime.min.time(), UTC),
                    test_id=f"TST-{cov.covenant_id[-4:]}-{qi}",
                )  # fmt: skip
    return out


def stage_covenants(
    j: Journey, w: World, cts: CreditTermSheetLike, upto: int
) -> tuple[dict[tuple[str, int], Any], int | None]:
    res = covenant_results(w, cts)
    agree = n = 0
    first_breach: int | None = None
    for (cid, qi), r in res.items():
        if qi > upto:
            continue
        cov = next(c for c in cts.covenants if c.covenant_id == cid)
        n += 1
        if r.value is not None and cov.threshold is not None and cov.operator:
            agree += (
                expected_covenant_result(cov.operator, r.value, cov.threshold) == r.result.value
            )
        if r.result.value == "BREACHED" and (first_breach is None or qi < first_breach):
            first_breach = qi
    j.add(
        QUARTER_ENDS[upto], "Covenant engine", "REAL",
        f"Tested {n} covenant instances to {QUARTER_ENDS[upto]}; {agree}/{n} agree with an independent "
        f"calculator. First breach: {QUARTER_ENDS[first_breach] if first_breach is not None else 'none'}.",
        tests=n, agree=agree,
    )  # fmt: skip
    return res, first_breach


def stage_breach_response(
    j: Journey,
    w: World,
    cts: CreditTermSheetLike,
    res: dict[tuple[str, int], Any],
    qi: int,
    gctx: GateCtx,
) -> None:
    w.clock.set(datetime.combine(QUARTER_ENDS[qi] + timedelta(days=30), datetime.min.time(), UTC))
    breached = [(cid, r) for (cid, q), r in res.items() if q == qi and r.result.value == "BREACHED"]
    if not breached:
        return
    cid, rec = breached[0]
    cov = next(c for c in cts.covenants if c.covenant_id == cid)
    case = w.cases.open_case(
        CaseType.COVENANT_BREACH, w.oid, w.scope, f"{cov.name} breach", sla_days=5
    )
    j.add(w.clock.today(), "Case service", "REAL", f"Opened breach case for '{cov.name}'.")
    tools = {
        "covenant.read_result": ToolSpec(
            "covenant.read_result",
            lambda test_id: {
                "result": rec.result.value,
                "value": str(rec.value),
                "threshold": str(rec.threshold),
                "operator": rec.operator,
                "period_end": rec.period_end.isoformat(),
            },
            "read",
        ),
        "rules.consequence_mode": ToolSpec(
            "rules.consequence_mode",
            lambda consequence: str(
                w.rules.evaluate(
                    "policy.covenant_consequences", {"consequence": consequence}
                ).result
            ),
            "compute",
        ),
        "case.propose": _propose_tool(w),
    }
    run = _run_agent(w, "covenant_agent", tools)
    out = covenant_agent.run(
        run, test_id=rec.test_id, case_id=case.case_id, covenant_name=cov.name,
        consequences=list(cov.consequence), llm=None,
    )  # fmt: skip
    d = out.drafts["reservation_of_rights"]
    j.add(
        w.clock.today(), "Covenant Agent", "REAL (templates; LLM not connected)",
        f"Proposed {len(out.proposals) - 1} consequence(s) and drafted a reservation-of-rights letter "
        f"(source: {d.source}). It cannot apply, send or waive anything.",
        proposals=len(out.proposals), letter_excerpt=d.text[:160], tool_calls=len(run.calls),
    )  # fmt: skip
    for p in case.proposals:
        if p.kind == "consequence":
            try:
                w.cases.decide(
                    case.case_id,
                    p.proposal_id,
                    checker=w.mo_agent_principal,
                    approve=True,
                    reason="x",
                )
            except Exception as exc:  # noqa: BLE001 - this refusal is the point
                j.add(
                    w.clock.today(),
                    "Maker-checker",
                    "REAL",
                    f"The agent's own human cannot approve its proposal: {getattr(exc, 'code', type(exc).__name__)}.",
                )
                break
    for p in case.proposals:
        approve = p.kind == "consequence"
        if p.status == "pending":
            w.cases.decide(
                case.case_id,
                p.proposal_id,
                checker=w.mo_decider,
                approve=approve,
                reason="breach confirmed; apply the policy consequence",
            )
    j.add(
        w.clock.today(), "Credit monitoring officer", "SIMULATED human",
        "A second officer (Arjun) approved the consequences with a signed, payload-bound approval.",
    )  # fmt: skip
    gctx.covenant_breached = []  # consequences now flow through approved proposals
    try_drawdown(j, w, fin_of(j), gctx, "Drawdown request after the breach")


_FIN: dict[str, FinacleDemo] = {}


def fin_of(j: Journey) -> FinacleDemo:
    return _FIN[j.obligor]


def stage_stress(
    j: Journey, w: World, cts: CreditTermSheetLike, fin: FinacleDemo, gctx: GateCtx
) -> None:
    e = w.e
    dq = e.default_quarter if e.default_quarter is not None else len(QUARTER_ENDS) - 1
    start = max(4, dq - 3)
    cutoff = QUARTER_ENDS[start]
    delivered = 0
    for o in w.calendar.obligations.values():
        if o.due_date < cutoff:
            w.calendar.receive(
                o.obligation_id,
                "DOC-SIM",
                obligor_id=o.obligor_id,
                doc_type=o.doc_type,
                period_end=o.period_end,
            )
            delivered += 1
    j.add(
        cutoff, "Borrower portal", "SIMULATED",
        f"The borrower delivered {delivered} documents on time up to {cutoff}; nothing arrives afterwards.",
        delivered=delivered,
    )  # fmt: skip
    npa_since: date | None = None
    acct = AccountSnapshot  # alias for brevity
    acc_id = f"TL-{w.oid[-6:]}"
    for qi in range(start, dq + 1):
        w.clock.set(datetime.combine(QUARTER_ENDS[qi], datetime.min.time(), UTC))
        c = e.conduct[qi]
        st = w.ews.update(w.oid, conduct_metrics(e, qi, 0))
        dpd = c.dpd_max
        if dpd > 90 and npa_since is None:
            npa_since = QUARTER_ENDS[qi]
        cls = w.sma.classify(
            acct(acc_id, w.oid, False, dpd, 0, 50_00_00_000_00, True, npa_since=npa_since)
        )
        j.add(
            QUARTER_ENDS[qi], "EWS + SMA service", "REAL",
            f"Conduct: utilisation {c.util_avg:.0%}, {c.overdrawn_days} overdrawn days, {c.cheque_returns} returned cheques, "
            f"DPD {dpd}. EWS score {st.score} ({st.level}); classification {cls.bucket}.",
            ews_score=st.score, ews_level=st.level, bucket=cls.bucket, data_gap=st.data_gap,
        )  # fmt: skip
    st = w.ews.state[w.oid]
    last_dpd = e.conduct[dq].dpd_max
    case = w.cases.open_case(CaseType.EWS, w.oid, w.scope, "early-warning alert", sla_days=3)
    tools = {
        "ews.read_state": ToolSpec(
            "ews.read_state",
            lambda obligor_id: {
                "score": st.score,
                "level": st.level,
                "fired": list(st.fired),
                "not_evaluated": list(st.not_evaluated),
                "data_gap": st.data_gap,
            },
            "read",
        ),
        "case.propose": _propose_tool(w),
    }
    run = _run_agent(w, "ews_analyst", tools)
    out = ews_analyst.run(run, obligor_id=w.oid, case_id=case.case_id)
    j.add(
        w.clock.today(), "EWS Analyst", "REAL (template; LLM not connected)",
        f"Note: {out.drafts['note'].text[:200] if 'note' in out.drafts else 'no active alert'} "
        f"Proposals filed: {len(out.proposals)}.",
        proposals=len(out.proposals),
    )  # fmt: skip
    for p in case.proposals:
        w.cases.decide(
            case.case_id,
            p.proposal_id,
            checker=w.mo_decider,
            approve=True,
            reason="alert accepted; stop new drawdowns",
        )
    try_drawdown(j, w, fin, gctx, "Drawdown request during stress")
    bucket = w.sma.borrower_view(w.oid)["worst_bucket"]
    if bucket in ("SMA-2", "NPA"):
        f = w.rfa.trigger(
            w.oid, w.scope, indicators=list(st.fired)[:3] or ["EWS_critical"], ews_score=st.score
        )
        j.add(
            w.clock.today(),
            "RFA service",
            "REAL",
            f"Red-flag file opened from EWS evidence; the gate now sees the flag: {w.rfa.is_flagged(w.oid)}.",
        )
        try:
            w.rfa.propose_classification(
                f.rfa_id,
                proposed_by="agent:ews_analyst",
                proposer_kind="agent",
                on_behalf_of="mo-priya",
                classification="fraud",
                rationale="x",
            )
        except Exception as exc:  # noqa: BLE001
            j.add(
                w.clock.today(),
                "RFA service",
                "REAL",
                f"A fraud-classification proposal is refused before natural justice: {getattr(exc, 'code', type(exc).__name__)}.",
            )
        w.rfa.record_show_cause(f.rfa_id, document_ref="SCN-001", by="rfa-committee-secretary")
        w.clock.advance(days=22)
        ready, why = w.rfa.committee_ready(f.rfa_id)
        j.add(
            w.clock.today(),
            "RFA service",
            "REAL",
            f"Show-cause issued; after the response window: committee ready = {ready} ({why}).",
        )
        w.rfa.close_file(
            f.rfa_id,
            reason="committee found no fraud; account stays under watch (simulated decision)",
            by="rfa-committee",
        )
        j.add(
            w.clock.today(),
            "RFA committee",
            "SIMULATED human",
            "Committee closes the file with no fraud finding; the gate flag clears.",
        )
        gctx.covenant_breached = []
    # no payment for a further 60 days: the account ages into NPA
    w.clock.set(datetime.combine(QUARTER_ENDS[dq] + timedelta(days=60), datetime.min.time(), UTC))
    aged_dpd = last_dpd + 60
    if aged_dpd > 90 and npa_since is None:
        npa_since = QUARTER_ENDS[dq] + timedelta(days=max(1, 91 - last_dpd))
    cls = w.sma.classify(
        acct(acc_id, w.oid, False, aged_dpd, 0, 50_00_00_000_00, True, npa_since=npa_since)
    )
    j.add(
        w.clock.today(),
        "SMA service",
        "REAL",
        f"No payment for 60 more days (DPD {aged_dpd}): classification {cls.bucket}; IRAC class {cls.irac_class}, provision {cls.provision_pct}% (rule pack is unverified).",
        bucket=cls.bucket,
    )
    bucket = cls.bucket
    # stressed asset clock + recovery
    if bucket == "NPA" and npa_since is not None:
        sf = w.stress.open_file(w.oid, w.scope, npa_since, 50_00_00_000_00)
        s = w.stress.status(sf.file_id)
        j.add(
            w.clock.today(),
            "Stressed-asset service",
            "REAL",
            f"Resolution clock started: review ends {s['review_period_end']}, plan due {s['resolution_deadline']}.",
            **{k: s[k] for k in ("review_period_end", "resolution_deadline")},
        )
        case_r = w.cases.open_case(CaseType.RECOVERY, w.oid, w.scope, "OTS evaluation", sla_days=10)
        tools2 = {
            "recovery.analyse_ots": ToolSpec(
                "recovery.analyse_ots",
                lambda outstanding_paise, offer_paise, security_realisable_paise: _ots_dict(
                    RecoveryService.analyse_ots(
                        outstanding_paise, offer_paise, security_realisable_paise
                    )
                ),
                "compute",
            ),
            "case.propose": _propose_tool(w),
        }
        run2 = _run_agent(w, "recovery_agent", tools2)
        out2 = recovery_agent.run(
            run2, case_id=case_r.case_id, outstanding_paise=50_00_00_000_00,
            offer_paise=32_00_00_000_00, security_realisable_paise=36_00_00_000_00, terms="lump sum in 45 days",
        )  # fmt: skip
        j.add(
            w.clock.today(),
            "Recovery Agent",
            "REAL (template; LLM not connected)",
            out2.drafts["summary"].text,
            proposals=len(out2.proposals),
        )
        w.cases.decide(
            case_r.case_id,
            case_r.proposals[0].proposal_id,
            checker=w.recovery_head,
            approve=True,
            reason="recovery ratio acceptable to the committee",
        )
        j.add(
            w.clock.today(),
            "Recovery head",
            "SIMULATED human",
            "Approved the settlement at delegated power; the approval records the amount.",
        )


def _ots_dict(a: Any) -> dict[str, Any]:
    return {
        "outstanding_paise": a.outstanding_paise, "offer_paise": a.offer_paise,
        "security_realisable_paise": a.security_realisable_paise,
        "recovery_ratio": str(a.recovery_ratio), "below_security_value": a.below_security_value,
        "haircut_paise": a.haircut_paise,
    }  # fmt: skip


def stage_closure(j: Journey, w: World, fin: FinacleDemo, cts: CreditTermSheetLike) -> None:
    w.clock.set(datetime.combine(RUN_DATE, datetime.min.time(), UTC))
    ids = tuple(f.facility_id for f in cts.facilities)
    z = {i: 0 for i in ids}
    facts = ClosureFacts(w.oid, ids, z, dict(z), 0, 0, RUN_DATE - timedelta(days=12))
    case = w.cases.open_case(CaseType.CLOSURE, w.oid, w.scope, "loan repaid in full", sla_days=30)
    cf = w.closure.propose_closure(
        case.case_id,
        facts,
        proposed_by="agent:closure",
        proposer_kind="agent",
        on_behalf_of="mo-priya",
    )
    assert cf.proposal_id
    p = w.cases.decide(
        case.case_id,
        cf.proposal_id,
        checker=w.sanctioning,
        approve=True,
        reason="zero dues confirmed",
    )
    j.add(
        w.clock.today(),
        "Closure service",
        "REAL",
        f"Zero-dues proof {cf.proof[:12]}… over {len(ids)} facilities approved by a sanctioning authority (simulated human).",
    )
    for a in RELEASE_ACTIONS:
        w.closure.record_action(case.case_id, a, f"EV-{a}", done_on=RUN_DATE - timedelta(days=2))
    assert p.approval is not None
    ev = w.closure.complete(case.case_id, approval=p.approval, current_facts=facts)
    st = w.closure.release_status(case.case_id)
    j.add(
        w.clock.today(),
        "Document release",
        "REAL",
        f"All {len(RELEASE_ACTIONS)} release actions done in time: days delayed {st['days_delayed']}, compensation ₹{st['compensation_rupees']}. AccountClosed event for {len(ev.facility_ids)} facilities.",
    )


# ------------------------------------------------------------------------------- run -----
def pick(book: list[BookEntry]) -> dict[str, BookEntry]:
    out: dict[str, BookEntry] = {}
    for e in book:
        cts = cts_of(e)
        template = e.package.style == "template"
        res = {}
        for cov in cts.covenants:
            for qi, s in enumerate(e.spreads):
                if cov.first_test_date is not None and s.period_end >= cov.first_test_date:
                    r = evaluate_covenant(
                        cov,
                        cts,
                        e.spreads,
                        s.period_end,
                        tested_at=datetime(2026, 10, 1, tzinfo=UTC),
                        test_id="T",
                    )
                    res[(cov.covenant_id, qi)] = r.result.value
        breaches = [qi for (_, qi), v in res.items() if v == "BREACHED"]
        if template and e.default_quarter is None and not breaches and "steady" not in out:
            out["steady"] = e
        elif (
            template
            and e.default_quarter is None
            and breaches
            and "breach" not in out
            and min(breaches) >= 7
        ):
            out["breach"] = e
        elif (
            template
            and e.default_quarter is not None
            and not e.idiosyncratic
            and e.default_quarter >= 8
            and "default" not in out
        ):
            out["default"] = e
        elif not template and "negotiated" not in out:
            out["negotiated"] = e
        if len(out) == 4:
            break
    return out


def journey(kind: str, e: BookEntry, engine: Any, seed: int) -> Journey:
    j = Journey(e.bundle.obligor.obligor_id, kind)
    w = World(e, e.package.cts.sanction_date)
    _FIN[j.obligor] = None  # type: ignore[assignment]
    cts = stage_sanction(j, w)
    stage_publish(j, w, cts)
    fin = stage_finacle(j, w, cts)
    _FIN[j.obligor] = fin
    gctx = GateCtx(w, j.summary["cc_limit_paise"], j.summary["cc_account"])
    if kind == "negotiated":
        j.summary["note"] = (
            "Stops at the sanction stage: the model that would read negotiated agreements is not connected."
        )
        return j
    # statements through the real OCR engine
    qi_ocr = 8
    if engine is not None:
        j.summary["ocr"] = stage_ocr(j, w, engine, qi_ocr, seed)
        if kind == "steady":
            stage_tamper(j, w, engine, qi_ocr, seed)
    full = covenant_results(w, cts)
    first_b = min((qi for (_, qi), r in full.items() if r.result.value == "BREACHED"), default=None)
    upto = (
        len(QUARTER_ENDS) - 1
        if kind == "steady"
        else (first_b if kind == "breach" and first_b is not None else (e.default_quarter or 12))
    )
    res, first = stage_covenants(j, w, cts, upto)
    if kind == "steady":
        w.clock.set(
            datetime.combine(QUARTER_ENDS[upto] + timedelta(days=5), datetime.min.time(), UTC)
        )
        try_drawdown(j, w, fin, gctx, "Drawdown request")
        stage_closure(j, w, fin, cts)
    elif kind == "breach" and first is not None:
        w.clock.set(
            datetime.combine(QUARTER_ENDS[first - 1] + timedelta(days=20), datetime.min.time(), UTC)
        )
        try_drawdown(j, w, fin, gctx, "Drawdown request in the quarter before the breach")
        stage_breach_response(j, w, cts, res, first, gctx)
    elif kind == "default":
        stage_stress(j, w, cts, fin, gctx)
        cal_overdue = len(w.calendar.sweep())
        j.add(
            w.clock.today(),
            "Monitoring calendar",
            "REAL",
            f"Chase sweep: {cal_overdue} overdue obligations moved up the ladder.",
        )
    j.steps.sort(key=lambda s: s.when)
    j.summary["events_emitted"] = dict(w.events)
    j.summary["finacle_writes"] = fin.state.write_count
    return j


def make_engine_for(kind: str) -> Any:
    if kind == "none":
        return None
    if kind == "rapid":
        from ocr.pipeline.engine import RapidOcrEngine

        return RapidOcrEngine()
    from ocr.pipeline.paddle_engine import make_engine

    return make_engine(FixedClock(datetime(2026, 10, 21, tzinfo=UTC)), lambda entry: None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["paddle", "rapid", "none"], default="paddle")
    ap.add_argument("--pool", type=int, default=150)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "demo_story.json")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    book = build_book(args.pool, args.seed)
    chosen = pick(book)
    engine = make_engine_for(args.engine)
    journeys = [journey(k, e, engine, args.seed) for k, e in chosen.items()]
    if engine is not None and hasattr(engine, "close"):
        engine.close()
    report = {
        "engine": getattr(engine, "name", "none"),
        "run_date": RUN_DATE.isoformat(),
        "seed": args.seed,
        "journeys": [asdict(j) for j in journeys],
        "wall_seconds": round(time.perf_counter() - t0, 1),
        "severity_scale": [s.value for s in SignalSeverity],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for j in journeys:
        print(f"\n== {j.archetype} ({j.obligor}) ==")
        for s in j.steps:
            print(f"  {s.when}  [{s.mode:>38}] {s.component}: {s.text[:150]}")
    print(f"\nwrote {args.out}  ({report['wall_seconds']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
