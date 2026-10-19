"""OCR accuracy harness (Build Guide §9.5).

    python -m ocr.eval.harness [--per-level 24] [--seed 2026] [--gate] [--update-baseline]

Pipeline under test: render -> degrade -> OCR engine -> structure -> extract -> validate ->
calibrate -> route. Ground truth is exact (rendered from known values), so every number is a
measurement, not an estimate from human labels. Calibration is fitted on half the documents
and every reported metric is computed on the other half.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from testing.synthetic.financials import QuarterFinancials, generate_history
from testing.synthetic.obligors import make_obligor

from ocr.eval.metrics import cer, wilson
from ocr.pipeline import PIPELINE_VERSION
from ocr.pipeline.engine import OcrEngine, RapidOcrEngine, Word
from ocr.pipeline.extract import StatementExtraction, extract_statement
from ocr.pipeline.route import Calibrator, ece, features, hard_blocked, route
from ocr.pipeline.validate import IdentityResult, validate_statement
from ocr.synthetic.render import LEVELS, RenderedPage, render_statement

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports" / "_cache"
REPORT = ROOT / "reports" / "ocr_accuracy.json"
BASELINE = Path(__file__).with_name("baseline.json")
THRESHOLDS = Path(__file__).with_name("thresholds.yaml")


@dataclass
class Doc:
    doc_id: str
    level: str
    statement: str
    page: RenderedPage
    truth: QuarterFinancials
    words: list[Word] = field(default_factory=list)
    ext: StatementExtraction | None = None
    results: list[IdentityResult] = field(default_factory=list)
    split: str = "test"
    tamper: str | None = None  # None | inconsistent | consistent
    tampered_codes: list[str] = field(default_factory=list)
    ocr_seconds: float = 0.0
    before: dict[str, int] = field(default_factory=dict)


def _ocr_cached(engine: OcrEngine, doc: Doc) -> list[Word]:
    CACHE.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(doc.page.image.tobytes()).hexdigest()[:20]
    path = CACHE / f"{engine.name}-{engine.version}-{h}.json"
    if path.exists():
        return [Word(**w) for w in json.loads(path.read_text(encoding="utf-8"))]
    t0 = time.perf_counter()
    words = engine.recognise(doc.page.image)
    doc.ocr_seconds = time.perf_counter() - t0
    path.write_text(json.dumps([w.__dict__ for w in words]), encoding="utf-8")
    return words


def build_corpus(seed: int, per_level: int) -> list[Doc]:
    rng = random.Random(seed)
    pool = []
    for i in range(14):
        ob = make_obligor(rng, i)
        pool.append(generate_history(rng, ob.obligor.obligor_id, ob.trajectory, ob.shock_quarter))
    docs: list[Doc] = []
    for level in LEVELS:
        for k in range(per_level):
            h = rng.choice(pool)
            qi = rng.randint(4, len(h.quarters) - 1)
            stmt = "PL" if k % 2 == 0 else "BS"
            q, prior = h.quarters[qi], h.quarters[qi - 1]
            page = render_statement(rng, q, prior, stmt, level)
            docs.append(Doc(f"{level}-{k:03d}", level, stmt, page, q))
    seen: dict[tuple[str, str], int] = {}
    for d in docs:  # alternate within each (level, statement) cell so both splits see every kind
        n = seen.get((d.level, d.statement), 0)
        seen[(d.level, d.statement)] = n + 1
        d.split = "calibration" if n % 2 == 0 else "test"
    return docs


# ------------------------------------------------------------------ tamper corpus --------
def _edit(
    q: QuarterFinancials, rng: random.Random, consistent: bool
) -> tuple[QuarterFinancials, list[str]]:
    lines = dict(q.lines)
    if consistent:
        # forge other income upward and flow the change through the P&L so every identity still holds
        d = max(500, lines["other_income"] // 2)
        for code in ("other_income", "total_income", "pbt", "pat"):
            lines[code] += d
        return QuarterFinancials(q.period_end, lines), [
            "other_income",
            "total_income",
            "pbt",
            "pat",
        ]
    code = rng.choice(
        [
            "trade_payables",
            "other_current_liabilities",
            "inventories",
            "trade_receivables",
            "employee_benefits",
            "other_expenses",
            "cost_of_materials",
        ]
    )
    lines[code] = int(lines[code] * rng.uniform(0.55, 0.9))
    return QuarterFinancials(q.period_end, lines), [code]


def build_tamper_corpus(seed: int, n_inconsistent: int, n_consistent: int) -> list[Doc]:
    rng = random.Random(seed + 7)
    pool = []
    for i in range(10):
        ob = make_obligor(rng, 100 + i)
        pool.append(generate_history(rng, ob.obligor.obligor_id, ob.trajectory, ob.shock_quarter))
    docs = []
    for kind, n in (("inconsistent", n_inconsistent), ("consistent", n_consistent)):
        for k in range(n):
            h = rng.choice(pool)
            qi = rng.randint(4, len(h.quarters) - 1)
            q = h.quarters[qi]
            use_bs = kind == "inconsistent" and k % 2 == 1
            stmt = "BS" if use_bs else "PL"
            cands = (
                ["trade_payables", "other_current_liabilities", "inventories", "trade_receivables"]
                if use_bs
                else ["employee_benefits", "other_expenses", "cost_of_materials"]
            )
            q2, codes = _edit(q, rng, consistent=(kind == "consistent"))
            if kind == "inconsistent":
                code = rng.choice(cands)
                lines = dict(q.lines)
                lines[code] = int(lines[code] * rng.uniform(0.55, 0.9))
                q2, codes = QuarterFinancials(q.period_end, lines), [code]
            page = render_statement(rng, q2, h.quarters[qi - 1], stmt, "mild")
            docs.append(
                Doc(
                    f"tamper-{kind}-{k:03d}",
                    "mild",
                    stmt,
                    page,
                    q,
                    tamper=kind,
                    tampered_codes=codes,
                )
            )
    return docs


# --------------------------------------------------------------------- run pipeline ------
def run_pipeline(engine: OcrEngine, docs: list[Doc]) -> None:
    for d in docs:
        d.words = _ocr_cached(engine, d)
        w, h = d.page.image.size
        d.ext = extract_statement(d.words, w, h, d.doc_id)
        d.before = {c: f.value_paise for c, f in d.ext.fields.items()}
        d.results = validate_statement(d.ext)


@dataclass
class FieldRec:
    doc: Doc
    code: str
    found: bool
    correct_before: bool
    correct_after: bool
    cer_val: float
    feats: list[float]
    blocked: bool
    ocr_conf: float
    p: float = 0.0
    routed: str = "needs_review"


def field_records(docs: list[Doc]) -> list[FieldRec]:
    recs: list[FieldRec] = []
    for d in docs:
        assert d.ext is not None
        for g in d.page.fields:
            e = d.ext.fields.get(g.code)
            if e is None:
                recs.append(FieldRec(d, g.code, False, False, False, 1.0, [0.0] * 7, True, 0.0))
                continue
            truth = g.value_paise
            recs.append(
                FieldRec(
                    d,
                    g.code,
                    True,
                    d.before.get(g.code) == truth,
                    e.value_paise == truth,
                    cer(e.raw_text, g.text_current),
                    features(d.ext, d.results, e),
                    hard_blocked(d.ext, e, d.results),
                    e.ocr_conf,
                )
            )
    return recs


def _arrays(recs: list[FieldRec]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    found = [r for r in recs if r.found]
    x = np.array([r.feats for r in found], dtype=float)
    y = np.array([r.correct_after for r in found], dtype=float)
    blocked = np.array([r.blocked for r in found], dtype=bool)
    return x, y, blocked


def _stats(n_ok: int, n: int) -> dict[str, float]:
    p, lo, hi = wilson(n_ok, n)
    return {"value": p, "ci_low": lo, "ci_high": hi, "n": n}


def evaluate(
    engine: OcrEngine, per_level: int, seed: int, target_precision: float = 0.995
) -> dict[str, Any]:
    docs = build_corpus(seed, per_level)
    run_pipeline(engine, docs)
    recs = field_records(docs)
    cal_recs = [r for r in recs if r.doc.split == "calibration"]
    test_recs = [r for r in recs if r.doc.split == "test"]

    cal = Calibrator(target_precision=target_precision)
    xc, yc, bc = _arrays(cal_recs)
    cal.fit(xc, yc)
    cal.choose_threshold(xc, yc, bc)
    xt, yt, _bt = _arrays(test_recs)
    pt = cal.predict(xt)
    raw_conf = xt[:, 0]
    found_test = [r for r in test_recs if r.found]
    for r, p in zip(found_test, pt, strict=True):
        r.p = float(p)
        r.routed = route(r.doc.ext, r.doc.ext.fields[r.code], r.p, cal, r.doc.results)  # type: ignore[union-attr,arg-type]

    out: dict[str, Any] = {
        "pipeline_version": PIPELINE_VERSION,
        "engine": f"{engine.name}=={engine.version}",
        "seed": seed,
        "corpus": {
            "documents": len(docs),
            "per_level": per_level,
            "levels": list(LEVELS),
            "calibration_docs": sum(d.split == "calibration" for d in docs),
            "test_docs": sum(d.split == "test" for d in docs),
            "test_fields": len(test_recs),
        },
        "calibrator": {
            "features": [
                "ocr_conf",
                "label_score",
                "id_holds",
                "id_none",
                "id_violated",
                "repaired",
                "autocorr",
            ],
            "threshold": cal.threshold,
            "target_precision": target_precision,
            "fitted_on_fields": cal.fitted_on,
        },
    }

    def block(rs: list[FieldRec]) -> dict[str, Any]:
        n = len(rs)
        found = [r for r in rs if r.found]
        auto = [r for r in found if r.routed == "auto"]
        wrong_auto = [r for r in auto if not r.correct_after]
        res = {
            "fields": n,
            "recall_found": _stats(len(found), n),
            "accuracy_raw": _stats(sum(r.correct_before for r in rs), n),
            "accuracy_after_validation": _stats(sum(r.correct_after for r in rs), n),
            "stp_rate": _stats(len(auto), n),
            "stp_precision": _stats(len(auto) - len(wrong_auto), len(auto)),
            "review_rate": _stats(n - len(auto), n),
            # delivered error: errors that reach the ledger if reviewers fix everything flagged
            "delivered_accuracy_assuming_perfect_review": _stats(n - len(wrong_auto), n),
            "delivered_accuracy_assuming_99pct_review": _stats(
                n
                - len(wrong_auto)
                - round(0.01 * sum(1 for r in rs if r not in auto and not r.correct_after)),
                n,
            ),
            "mean_cer_amount": float(np.mean([r.cer_val for r in rs])) if rs else float("nan"),
        }
        return res

    out["overall"] = block(test_recs)
    out["by_level"] = {lv: block([r for r in test_recs if r.doc.level == lv]) for lv in LEVELS}
    out["by_statement"] = {
        s: block([r for r in test_recs if r.doc.statement == s]) for s in ("PL", "BS")
    }

    # ---- calibration quality, and the value of arithmetic validation (ablation) -----------
    ece_raw = ece(raw_conf, yt)
    ece_cal = ece(pt, yt)
    base_auto = raw_conf >= 0.90
    out["calibration"] = {"ece_raw_ocr_confidence": ece_raw, "ece_calibrated": ece_cal}
    out["ablation_confidence_only_routing"] = {
        "rule": "auto-accept when raw OCR confidence >= 0.90, no arithmetic validation",
        "stp_rate": _stats(int(base_auto.sum()), len(yt)),
        "stp_precision": _stats(int(((yt == 1) & base_auto).sum()), int(base_auto.sum())),
        "delivered_accuracy_assuming_perfect_review": _stats(
            len(yt) - int(((yt == 0) & base_auto).sum()), len(yt)
        ),
    }

    # ---- document level -------------------------------------------------------------------
    td = [d for d in docs if d.split == "test"]
    unit_ok = sum(
        d.ext is not None
        and d.ext.unit is not None
        and d.ext.unit.name.lower()[:3]
        == {"lakhs": "lak", "thousands": "tho", "rupees": "rup"}[d.page.unit_name][:3]
        for d in td
    )
    full_ok = 0
    stp_docs = 0
    for d in td:
        rs = [r for r in test_recs if r.doc is d]
        full_ok += all(r.correct_after for r in rs)
        stp_docs += all(r.routed == "auto" and r.correct_after for r in rs)
    supported = [d for d in td if d.level != "severe"]
    unit_supported = sum(
        d.ext is not None
        and d.ext.unit is not None
        and d.ext.unit.name[:3]
        == {"lakhs": "LAK", "thousands": "THO", "rupees": "RUP"}[d.page.unit_name]
        for d in supported
    )
    severe = [d for d in td if d.level == "severe"]
    severe_flagged = sum(
        d.ext is not None and any(f.startswith("rescan_recommended") for f in d.ext.doc_flags)
        for d in severe
    )
    out["documents"] = {
        "unit_detected_supported_tiers": _stats(unit_supported, len(supported)),
        "severe_pages_flagged_for_rescan": _stats(severe_flagged, len(severe)),
        "unit_detected_correctly": _stats(unit_ok, len(td)),
        "all_fields_correct": _stats(full_ok, len(td)),
        "fully_straight_through_and_correct": _stats(stp_docs, len(td)),
        "mean_ocr_seconds_per_page": float(
            np.mean([d.ocr_seconds for d in docs if d.ocr_seconds]) or 0.0
        ),
    }

    # ---- error taxonomy on the test set ---------------------------------------------------------
    tax: dict[str, int] = {
        "missing_row": 0,
        "wrong_value_flagged_for_review": 0,
        "wrong_value_auto": 0,
    }
    for r in test_recs:
        if not r.found:
            tax["missing_row"] += 1
        elif not r.correct_after:
            tax["wrong_value_auto" if r.routed == "auto" else "wrong_value_flagged_for_review"] += 1
    out["error_taxonomy_test"] = tax
    out["_docs"] = docs
    return out


def tamper_eval(engine: OcrEngine, seed: int, n_inc: int, n_con: int) -> dict[str, Any]:
    docs = build_tamper_corpus(seed, n_inc, n_con)
    run_pipeline(engine, docs)
    res: dict[str, Any] = {}
    for kind in ("inconsistent", "consistent"):
        ds = [d for d in docs if d.tamper == kind]
        flagged = 0
        located = 0
        for d in ds:
            assert d.ext is not None
            doc_flag = (
                any(f.validations for f in d.ext.fields.values())
                or bool(d.ext.doc_flags)
                or any(r.status == "violated" for r in d.results)
            )
            flagged += doc_flag
            located += any(
                d.ext.fields.get(c) is not None and d.ext.fields[c].validations
                for c in d.tampered_codes
            )
        res[kind] = {
            "documents": len(ds),
            "flagged_for_review": _stats(flagged, len(ds)),
            "tampered_cell_pinpointed": _stats(located, len(ds)),
        }
    return res


def latency_benchmark(engine: OcrEngine, docs: list[Doc], per_level: int = 3) -> dict[str, Any]:
    """Wall-clock seconds per page, measured on fresh calls (the result cache would hide it)."""
    out: dict[str, Any] = {"engine_hardware": "CPU (ONNX Runtime), single process"}
    allt: list[float] = []
    for lv in LEVELS:
        ts = []
        for d in [x for x in docs if x.level == lv][:per_level]:
            t0 = time.perf_counter()
            engine.recognise(d.page.image)
            ts.append(time.perf_counter() - t0)
        out[lv] = float(np.mean(ts))
        allt += ts
    out["mean_seconds_per_page"] = float(np.mean(allt))
    out["p95_seconds_per_page"] = float(np.quantile(allt, 0.95))
    return out


def false_flag_rate(docs: list[Doc], recs_correct_docs_only: bool = True) -> dict[str, Any]:
    """Of documents whose every field was actually read correctly, how many did validation flag?
    That is the review cost the tamper control imposes on genuine documents."""
    n = flagged = 0
    for d in docs:
        if d.split != "test" or d.ext is None:
            continue
        ok = all(
            d.ext.fields.get(g.code) is not None
            and d.ext.fields[g.code].value_paise == g.value_paise
            for g in d.page.fields
        )
        if not ok:
            continue
        n += 1
        flagged += any(f.validations for f in d.ext.fields.values()) or bool(d.ext.doc_flags)
    return _stats(flagged, n)


# ---------------------------------------------------------------------------- gate ----------
def gate(report: dict[str, Any]) -> list[str]:
    spec = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8"))
    failures: list[str] = []

    def get(path: str) -> float:
        node: Any = report
        for part in path.split("."):
            node = node[part]
        return float(node["value"] if isinstance(node, dict) and "value" in node else node)

    for path, minimum in spec["minimums"].items():
        v = get(path)
        if v < minimum:
            failures.append(f"{path} = {v:.4f} < minimum {minimum}")
    for path, maximum in spec.get("maximums", {}).items():
        v = get(path)
        if v > maximum:
            failures.append(f"{path} = {v:.4f} > maximum {maximum}")
    if BASELINE.exists():
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        tol = float(spec["regression_tolerance"])
        for path in spec["regression_watch"]:
            v, b = get(path), float(_pick(base, path))
            if v < b - tol:
                failures.append(f"{path} regressed: {v:.4f} vs champion {b:.4f} (tolerance {tol})")
    return failures


def _pick(d: dict[str, Any], path: str) -> float:
    node: Any = d
    for part in path.split("."):
        node = node[part]
    return float(node["value"] if isinstance(node, dict) and "value" in node else node)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-level", type=int, default=24)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--tamper-inconsistent", type=int, default=30)
    ap.add_argument("--tamper-consistent", type=int, default=16)
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args(argv)
    engine = RapidOcrEngine()
    t0 = time.perf_counter()
    report = evaluate(engine, args.per_level, args.seed)
    docs = report.pop("_docs")
    report["documents"]["genuine_documents_flagged_by_validation"] = false_flag_rate(docs)
    report["latency"] = latency_benchmark(engine, docs)
    report["tamper_detection"] = tamper_eval(
        engine, args.seed, args.tamper_inconsistent, args.tamper_consistent
    )
    report["wall_seconds"] = round(time.perf_counter() - t0, 1)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    o = report["overall"]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(
        f"engine {report['engine']}  pipeline {PIPELINE_VERSION}  threshold {report['calibrator']['threshold']:.3f}"
    )
    for k in (
        "accuracy_raw",
        "accuracy_after_validation",
        "stp_rate",
        "stp_precision",
        "delivered_accuracy_assuming_perfect_review",
    ):
        s = o[k]
        print(
            f"  {k:46s} {100 * s['value']:.2f}%  [{100 * s['ci_low']:.2f}, {100 * s['ci_high']:.2f}]  n={s['n']}"
        )
    if args.update_baseline:
        BASELINE.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
        print("baseline updated")
    if args.gate:
        failures = gate(report)
        for f in failures:
            print("GATE FAIL:", f)
        print("GATE", "FAILED" if failures else "PASSED")
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
