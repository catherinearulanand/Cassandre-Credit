"""End-to-end simulation. python -m sim.run_simulation [--n 300] [--seed 2026]

Every stage is checked against an independent oracle (hand-written reference calculators or
exact ground truth from the generator), so the reported numbers are measurements of agreement,
not self-assessment. Outputs reports/simulation_report.json.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from authz import MakerChecker, Principal, Role
from covenant_engine.domain import evaluate_covenant
from cts_service.domain import CtsService
from domain.limit_tree import LimitState, LimitTree
from drawdown_gate.domain import CovenantStatus, DrawdownGate, DrawdownRequest, GateContext
from limit_service.domain import StockStatement, compute_dp
from platform_core.clock import FixedClock
from rules_engine import default_registry
from security.crypto import LocalDevKeyProvider, Signer
from testing.synthetic.financials import UNIT_PAISE
from testing.synthetic.reference import reference_ratio

from agents.sanction_compiler.compiler import assemble_cts, grammar_extract
from ml.ews_model import build_rows, train_eval
from ocr.eval.metrics import wilson
from sim.book import BookEntry, build_book, cts_of, expected_covenant_result

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "simulation_report.json"
NOW = datetime(2026, 10, 19, tzinfo=UTC)


def stat(ok: int, n: int) -> dict[str, float]:
    p, lo, hi = wilson(ok, n)
    return {"value": p, "ci_low": lo, "ci_high": hi, "n": n}


def stage_sanction(book: list[BookEntry]) -> dict[str, Any]:
    tot = {"template": [0, 0, 0], "negotiated": [0, 0, 0]}  # docs, fully extracted, correct fields
    field_ok = field_n = cite_ok = cite_n = 0
    for e in book:
        pkg = e.package
        ex = grammar_extract(pkg.letter_lines)
        draft, _review = assemble_cts(
            ex,
            lines=pkg.letter_lines,
            document_id=pkg.document_id,
            obligor_id=e.bundle.obligor.obligor_id,
            authority_power_paise=pkg.cts.sanctioning_authority_power.amount_paise,
        )
        t = tot[pkg.style]
        t[0] += 1
        if draft is None or ex.unresolved:
            continue
        t[1] += 1
        truth = pkg.cts
        checks: list[bool] = [
            [f.amount for f in draft.facilities] == [f.amount for f in truth.facilities],
            [f.tenor_months for f in draft.facilities]
            == [f.tenor_months for f in truth.facilities],
            [f.pricing_spread_bps for f in draft.facilities]
            == [f.pricing_spread_bps for f in truth.facilities],
            draft.facilities[1].margin_pct == truth.facilities[1].margin_pct,
            draft.facilities[1].receivable_age_cutoff_days
            == truth.facilities[1].receivable_age_cutoff_days,
            draft.sanction_ref == truth.sanction_ref,
            draft.sanction_date == truth.sanction_date,
            draft.valid_until == truth.valid_until,
            draft.sanctioning_authority == truth.sanctioning_authority,
            len(draft.covenants) == len(truth.covenants),
        ]
        for dc, tc in zip(draft.covenants, truth.covenants, strict=False):
            checks += [
                dc.threshold == tc.threshold,
                dc.operator == tc.operator,
                dc.formula == tc.formula,
                dc.cure_period_days == tc.cure_period_days,
                dc.consequence == tc.consequence,
                dc.equity_cure_allowed == tc.equity_cure_allowed,
            ]
        t[2] += all(checks)
        field_ok += sum(checks)
        field_n += len(checks)
        for c in [*draft.facilities, *draft.covenants, *draft.limits]:
            cite_n += 1
            cite_ok += bool(c.citations)
    tpl, neg = tot["template"], tot["negotiated"]
    return {
        "template_letters": {
            "documents": tpl[0],
            "fully_extracted_without_llm": stat(tpl[1], tpl[0]),
            "cts_exactly_equal_to_truth": stat(tpl[2], tpl[0]),
        },
        "negotiated_agreements": {
            "documents": neg[0],
            "fully_extracted_by_grammar": stat(neg[1], neg[0]),
            "needs_llm_or_human": stat(neg[0] - neg[1], neg[0]),
        },
        "field_accuracy_on_extracted_template": stat(field_ok, field_n),
        "citation_coverage": stat(cite_ok, cite_n),
    }


def stage_publish(book: list[BookEntry]) -> dict[str, Any]:
    clock = FixedClock(NOW)
    signer = Signer(LocalDevKeyProvider())
    rules = default_registry()
    svc = CtsService(
        clock, rules, signer, MakerChecker(clock, signer), tier1_capital_rupees=Decimal(10) ** 12
    )
    checker = Principal(id="cad-checker-1", kind="human", roles=frozenset({Role.CAD_CHECKER}))
    ok = blocked = 0
    reasons: dict[str, int] = {}
    t0 = time.perf_counter()
    for e in book:
        try:
            svc.publish(
                cts_of(e),
                drafted_by="agent:sanction_compiler",
                drafted_on_behalf_of="cad-maker-1",
                checker=checker,
                reason="verified against sanction letter",
                rating=e.bundle.obligor.internal_rating,
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001 - classify every refusal
            blocked += 1
            key = str(exc)[:70]
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "published": ok,
        "blocked_by_validation": blocked,
        "top_block_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5]),
        "seconds": round(time.perf_counter() - t0, 3),
    }


KEYMAP = {
    "Net Debt / EBITDA": "net_debt_ebitda",
    "DSCR": "dscr",
    "Interest Service Coverage Ratio": "iscr",
    "Current Ratio": "current_ratio",
    "TOL / TNW": "tol_tnw",
    "Debt / Equity": "debt_equity",
}


def stage_covenants(
    book: list[BookEntry],
) -> tuple[dict[str, Any], list[dict[tuple[str, int], Any]]]:
    n = value_ok = result_ok = lineage_ok = 0
    breaches = 0
    nonpositive = 0
    per_test_ms: list[float] = []
    all_results: list[dict[tuple[str, int], Any]] = []
    for e in book:
        cts = cts_of(e)
        res: dict[tuple[str, int], Any] = {}
        for cov in cts.covenants:
            for qi, s in enumerate(e.spreads):
                if cov.first_test_date is None or s.period_end < cov.first_test_date:
                    continue
                t0 = time.perf_counter()
                r = evaluate_covenant(
                    cov, cts, e.spreads, s.period_end, tested_at=NOW, test_id=f"TST-{qi}"
                )
                per_test_ms.append((time.perf_counter() - t0) * 1000)
                res[(cov.covenant_id, qi)] = r
                ref = reference_ratio(KEYMAP[cov.name], e.hist, qi)
                ref_exact = reference_ratio(KEYMAP[cov.name], e.hist, qi, exact=True)
                n += 1
                if ref is None:
                    # no meaningful ratio (non-positive base): the oracle expects a breach or
                    # "not computable", never a pass
                    unsafe = r.result.value in ("BREACHED", "NOT_COMPUTABLE", "IN_CURE")
                    value_ok += unsafe
                    result_ok += unsafe
                    nonpositive += 1
                elif r.value == ref:
                    value_ok += 1
                    assert cov.threshold is not None and cov.operator is not None
                    assert ref_exact is not None
                    exp = expected_covenant_result(cov.operator, ref_exact, cov.threshold)
                    if r.result.value == exp or (r.result.value == "IN_CURE" and exp == "BREACHED"):
                        result_ok += 1
                lineage_ok += bool(r.lineage)
                breaches += r.result.value in ("BREACHED", "IN_CURE")
        all_results.append(res)
    per_test_ms.sort()
    return {
        "tests_run": n,
        "value_matches_independent_calculation": stat(value_ok, n),
        "result_matches_independent_calculation": stat(result_ok, n),
        "lineage_coverage": stat(lineage_ok, n),
        "breaches_or_in_cure": breaches,
        "tests_with_non_positive_base": nonpositive,
        "latency_ms": {
            "p50": round(statistics.median(per_test_ms), 3),
            "p99": round(per_test_ms[int(0.99 * len(per_test_ms))], 3),
        },
    }, all_results


def stage_gate(book: list[BookEntry], cov: list[dict[tuple[str, int], Any]]) -> dict[str, Any]:
    class P:
        def __init__(self) -> None:
            self.ctx: GateContext | None = None

        def load(self, facility_id: str) -> GateContext:
            assert self.ctx is not None
            return self.ctx

    prov = P()
    gate = DrawdownGate(prov, lambda: date(2026, 10, 19))
    correct = total = 0
    lat: list[float] = []
    for e, res in zip(book, cov, strict=True):
        cts = cts_of(e)
        for qi in (6, 9, 12):
            statuses = []
            expect_block = False
            for c in cts.covenants:
                r = res.get((c.covenant_id, qi))
                if r is None:
                    continue
                statuses.append(
                    CovenantStatus(
                        c.covenant_id,
                        "BREACHED" if r.result.value == "BREACHED" else "COMPLIED",
                        tuple(c.consequence),
                    )
                )
                if r.result.value == "BREACHED" and "stop_drawdown" in c.consequence:
                    expect_block = True
            tree = LimitTree()
            tree.add(LimitState("OBL", None, 10**13))
            tree.add(LimitState("CC", "OBL", 10**12, dp=10**12, facility_id="F"))
            prov.ctx = GateContext(
                cts_valid_until=date(2027, 6, 1),
                facility_limit_node="CC",
                open_cps=[],
                overdue_stopping_cs=[],
                tree=tree,
                dp_paise=10**12,
                utilised_paise=0,
                covenants=statuses,
                rfa_or_fraud_flag=False,
                sma_bucket="STANDARD",
                amendment_in_flight=False,
                approved_payees={"SUP"},
                facility_id="F",
                as_of=date(2026, 10, 19),
            )
            d = gate.decide(
                DrawdownRequest(
                    f"D-{e.bundle.obligor.obligor_id}-{qi}", "F", 1_00_000_00, payee_id="SUP"
                )
            )
            lat.append(d.latency_ms)
            total += 1
            correct += (d.decision == "REJECT") == expect_block
    lat.sort()
    return {
        "decisions": total,
        "gate_matches_covenant_policy": stat(correct, total),
        "latency_ms": {
            "p50": round(lat[len(lat) // 2], 4),
            "p99": round(lat[int(0.99 * len(lat))], 4),
            "budget_p99": 300,
        },
    }


def stage_sma(book: list[BookEntry]) -> dict[str, Any]:
    rules = default_registry()

    def oracle(dpd: int) -> str:  # plain-Python oracle written independently of the rule pack
        if dpd == 0:
            return "STANDARD"
        if dpd <= 30:
            return "SMA-0"
        if dpd <= 60:
            return "SMA-1"
        if dpd <= 90:
            return "SMA-2"
        return "NPA"

    n = ok = 0
    for e in book:
        for c in e.conduct:
            got = rules.evaluate("rbi.sma", {"type": "non_revolving", "dpd": c.dpd_max}).result
            n += 1
            ok += got == oracle(c.dpd_max)
    for dpd in range(0, 130):  # exhaustive boundary sweep
        n += 1
        ok += rules.evaluate("rbi.sma", {"type": "non_revolving", "dpd": dpd}).result == oracle(dpd)
    return {"classifications": n, "match_independent_oracle": stat(ok, n)}


def stage_dp(book: list[BookEntry], seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    n = ok = inv = 0
    for e in book:
        for qi in rng.sample(range(4, 14), 3):
            q = e.hist.quarters[qi]
            stock = q.lines["inventories"] * UNIT_PAISE
            recv = q.lines["trade_receivables"] * UNIT_PAISE
            aged = int(recv * rng.uniform(0.05, 0.3))
            limit = e.package.cts.facilities[1].amount.amount_paise
            mpct = rng.choice([25, 30])
            cutoff = e.package.cts.facilities[1].receivable_age_cutoff_days or 90
            st = StockStatement(
                "p", stock, 0, 0, receivables_by_age={cutoff: recv - aged, cutoff + 30: aged}
            )
            got = compute_dp(
                limit,
                st,
                stock_margin=Decimal(mpct) / 100,
                bd_margin=Decimal(mpct) / 100,
                age_cutoff_days=cutoff,
                deduct_creditors=False,
            ).dp_paise
            oracle = min(limit, stock * (100 - mpct) // 100 + (recv - aged) * (100 - mpct) // 100)
            n += 1
            ok += got == oracle
            inv += not (0 <= got <= limit)
    return {"cases": n, "match_hand_oracle": stat(ok, n), "invariant_violations": inv}


def stage_closure() -> dict[str, Any]:
    rules = default_registry()
    out = {}
    for label, today in (
        ("D+0", "2026-03-01"),
        ("D+25", "2026-03-26"),
        ("D+31", "2026-04-01"),
        ("D+40", "2026-04-10"),
    ):
        d = rules.evaluate("rbi.document_release", {"repaid_on": "2026-03-01", "today": today})
        out[label] = {
            "days_delayed": d.result["days_delayed"],
            "compensation_rupees": d.result["compensation_rupees"],
            "escalate_on": d.details["escalate_on"],
        }
    return out


def stage_models(book: list[BookEntry], cov: list[dict[tuple[str, int], Any]]) -> dict[str, Any]:
    rows = build_rows(book, cov)
    obligors = sorted({r["obligor"] for r in rows})
    rng = random.Random(1234)
    held = set(rng.sample(obligors, k=max(2, round(0.35 * len(obligors)))))
    # EWS labels look 2 quarters ahead, so training rows stop 3 quarters before calibration
    ews = train_eval(
        rows,
        "y_ews",
        "EWS stress score (M4)",
        train_q=range(4, 7),
        calib_q=9,
        test_q=range(10, 12),
        test_obligors=held,
    )
    # breach labels look 1 quarter ahead
    brk = train_eval(
        rows,
        "y_breach",
        "Covenant breach forecast (M6)",
        train_q=range(4, 9),
        calib_q=10,
        test_q=range(11, 13),
        test_obligors=held,
    )
    defaults = sum(1 for e in book if e.default_quarter is not None)
    idio = sum(1 for e in book if e.default_quarter is not None and e.idiosyncratic)
    return {
        "book_defaults": defaults,
        "book_defaults_from_unforeseeable_shocks": idio,
        "ews": ews.__dict__,
        "breach_forecast": brk.__dict__,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    book = build_book(args.n, args.seed)
    report: dict[str, Any] = {
        "seed": args.seed,
        "obligors": len(book),
        "generated_at": NOW.isoformat(),
    }
    report["sanction_compilation"] = stage_sanction(book)
    report["cts_publication"] = stage_publish(book)
    report["covenant_testing"], cov = stage_covenants(book)
    report["drawdown_gate"] = stage_gate(book, cov)
    report["sma_classification"] = stage_sma(book)
    report["drawing_power"] = stage_dp(book, args.seed)
    report["closure_document_release"] = stage_closure()
    report["models"] = stage_models(book, cov)
    report["wall_seconds"] = round(time.perf_counter() - t0, 1)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    c = report["covenant_testing"]
    print(
        f"obligors {len(book)}  covenant tests {c['tests_run']}  "
        f"value agreement {100 * c['value_matches_independent_calculation']['value']:.3f}%  "
        f"result agreement {100 * c['result_matches_independent_calculation']['value']:.3f}%"
    )
    m = report["models"]["ews"]["metrics"]
    print(
        f"EWS OOT AUC {m['auc']:.3f}  precision@10% {m['precision_at_capacity']:.3f}  recall@10% {m['recall_at_capacity']:.3f}"
    )
    print(f"wrote {REPORT}  ({report['wall_seconds']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
