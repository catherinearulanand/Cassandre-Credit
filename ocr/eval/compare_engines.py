"""Small side-by-side of OCR engines on the same synthetic pages.

    python -m ocr.eval.compare_engines --pages-per-level 2 --levels clean mild moderate

This is a smoke-scale comparison (a handful of pages), not the release harness: PaddleOCR on this
workstation's CPU takes roughly a minute per page, so the full 240-page harness is impractical here.
The same pages go to every engine, through the same extraction, validation and straight-through rule,
so the numbers are comparable with each other and carry wide uncertainty. Use the full harness
(``python -m ocr.eval.harness --engine paddle``) on a machine with a GPU or a fast CPU build.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platform_core.clock import FixedClock
from testing.synthetic.financials import generate_history
from testing.synthetic.obligors import make_obligor

from ocr.eval.metrics import wilson
from ocr.pipeline.engine import OcrEngine
from ocr.pipeline.extract import extract_statement
from ocr.pipeline.route import hard_blocked
from ocr.pipeline.validate import validate_statement
from ocr.synthetic.render import RenderedPage, render_statement

ROOT = Path(__file__).resolve().parents[2]
STP_CONFIDENCE = 0.97


def make_pages(levels: list[str], per_level: int, seed: int) -> list[tuple[str, RenderedPage]]:
    rng = random.Random(seed)
    pages: list[tuple[str, RenderedPage]] = []
    for i in range(per_level * len(levels)):
        ob = make_obligor(rng, i + 1, "healthy")
        h = generate_history(rng, ob.obligor.obligor_id, "healthy")
        level = levels[i % len(levels)]
        qi = 6 + i % 5
        stmt = "BS" if i % 2 == 0 else "PL"
        pages.append(
            (
                level,
                render_statement(rng, h.quarters[qi], h.quarters[qi - 1], stmt, level, layout="A"),
            )
        )
    return pages


def run_engine(engine: OcrEngine, pages: list[tuple[str, RenderedPage]]) -> dict[str, Any]:
    per: dict[str, dict[str, int]] = {}
    secs = 0.0
    for level, page in pages:
        t = time.perf_counter()
        words = engine.recognise(page.image)
        secs += time.perf_counter() - t
        w, h = page.image.size
        ext = extract_statement(words, w, h, "DOC")
        results = validate_statement(ext)
        d = per.setdefault(
            level, dict(figures=0, located=0, correct=0, stp=0, stp_wrong=0, pages=0)
        )
        d["pages"] += 1
        for g in page.fields:
            d["figures"] += 1
            f = ext.fields.get(g.code)
            if f is None:
                continue
            d["located"] += 1
            d["correct"] += f.value_paise == g.value_paise
            if not (hard_blocked(ext, f, results) or f.ocr_conf < STP_CONFIDENCE):
                d["stp"] += 1
                d["stp_wrong"] += f.value_paise != g.value_paise
    tot = {
        k: sum(v[k] for v in per.values())
        for k in ("figures", "located", "correct", "stp", "stp_wrong", "pages")
    }
    lo, hi = wilson(tot["stp"] - tot["stp_wrong"], tot["stp"])[1:] if tot["stp"] else (None, None)
    return {
        "per_level": per,
        "overall": {**tot, "stp_precision_ci": [lo, hi]},
        "seconds_per_page": round(secs / max(1, len(pages)), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages-per-level", type=int, default=2)
    ap.add_argument("--levels", nargs="+", default=["clean", "mild", "moderate"])
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "ocr_engine_comparison.json")
    a = ap.parse_args()
    pages = make_pages(a.levels, a.pages_per_level, a.seed)
    from ocr.pipeline.engine import RapidOcrEngine
    from ocr.pipeline.paddle_engine import make_engine

    clock = FixedClock(datetime(2026, 10, 21, tzinfo=UTC))
    paddle = make_engine(clock, lambda e: None)
    report: dict[str, Any] = {
        "pages": len(pages),
        "levels": a.levels,
        "seed": a.seed,
        "engines": {},
    }
    for name, eng in (
        ("rapidocr-onnx", RapidOcrEngine()),
        (paddle.name + "/" + paddle.version, paddle),
    ):
        report["engines"][name] = run_engine(eng, pages)
        print(name, json.dumps(report["engines"][name]["overall"]), flush=True)
    if hasattr(paddle, "close"):
        paddle.close()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
