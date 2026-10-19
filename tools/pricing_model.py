"""Cost-to-serve and price-corridor calculator.

    python -m tools.pricing_model [--obligors 400] [--json inputs.json]

Every commercial input below is an ASSUMPTION placeholder, not market data. Replace them with
your own costs and the bank's own volumes. Platform behaviour inputs (review rate, seconds per
page, covenant tests per obligor) are read from the measured reports when they exist.

Corridor logic:
  floor   = cost to serve / (1 - target gross margin)
  ceiling = value delivered to the bank (analyst hours saved x loaded cost) x share you capture
A price outside the corridor is either unprofitable for you or unattractive for the bank.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Assumptions:
    # ---- volumes (per obligor per year) -- ASSUMPTIONS --------------------------------------
    quarterly_statements_per_obligor: int = 4
    pages_per_statement_pack: int = 12  # P&L + BS + notes extracts, per quarter
    other_documents_per_obligor: int = (
        18  # stock statements, certificates, insurance, valuations...
    )
    pages_per_other_document: int = 3
    covenants_per_obligor: int = 4
    # ---- unit costs -- ASSUMPTIONS (INR) -------------------------------------------------------
    ocr_cost_per_page: float = 0.40  # GPU inference amortised
    llm_cost_per_obligor_year: float = 650.0  # drafting + negotiated-agreement reads, budget-capped
    infra_cost_per_obligor_year: float = 900.0  # storage, events, graph, observability
    review_minutes_per_flagged_field: float = 0.5
    fields_per_page: float = 20.0
    reviewer_cost_per_hour: float = 450.0  # bank-side loaded cost; cost to the bank, not to us
    support_cost_per_obligor_year: float = 1_100.0
    # ---- value side -- ASSUMPTIONS ---------------------------------------------------------------
    manual_spreading_hours_per_statement: float = 3.0
    manual_covenant_tracking_hours_per_obligor_year: float = 12.0
    manual_chasing_hours_per_obligor_year: float = 10.0
    analyst_cost_per_hour: float = 900.0
    value_capture_share: float = 0.25  # fraction of bank savings you price at
    target_gross_margin: float = 0.70
    # ---- measured platform behaviour (overridden from reports when available) ------------------
    review_rate: float = 0.35
    notes: list[str] = field(default_factory=list)


def load_measured(a: Assumptions) -> Assumptions:
    p = ROOT / "reports" / "ocr_accuracy.json"
    if p.exists():
        r = json.loads(p.read_text(encoding="utf-8"))
        a.review_rate = float(r["overall"]["review_rate"]["value"])
        a.notes.append(
            f"review_rate={a.review_rate:.3f} read from reports/ocr_accuracy.json (synthetic corpus)"
        )
    else:
        a.notes.append("review_rate is a placeholder: run python -m ocr.eval.harness first")
    return a


def compute(a: Assumptions, obligors: int) -> dict[str, float]:
    pages = (
        a.quarterly_statements_per_obligor * a.pages_per_statement_pack
        + a.other_documents_per_obligor * a.pages_per_other_document
    )
    ocr = pages * a.ocr_cost_per_page
    cost = (
        ocr
        + a.llm_cost_per_obligor_year
        + a.infra_cost_per_obligor_year
        + a.support_cost_per_obligor_year
    )
    floor = cost / (1 - a.target_gross_margin)
    saved_hours = (
        a.quarterly_statements_per_obligor * a.manual_spreading_hours_per_statement
        + a.manual_covenant_tracking_hours_per_obligor_year
        + a.manual_chasing_hours_per_obligor_year
    )
    review_hours = (
        pages * a.fields_per_page * a.review_rate * a.review_minutes_per_flagged_field / 60
    )
    net_hours = max(0.0, saved_hours - review_hours)
    value = net_hours * a.analyst_cost_per_hour
    ceiling = value * a.value_capture_share
    return {
        "pages_per_obligor_year": pages,
        "cost_to_serve_per_obligor_year": round(cost, 0),
        "price_floor_per_obligor_year": round(floor, 0),
        "analyst_hours_saved_gross": round(saved_hours, 1),
        "analyst_review_hours_added": round(review_hours, 1),
        "analyst_hours_saved_net": round(net_hours, 1),
        "value_to_bank_per_obligor_year": round(value, 0),
        "price_ceiling_per_obligor_year": round(ceiling, 0),
        "corridor_is_viable": float(ceiling > floor),
        "book_cost_to_serve": round(cost * obligors, 0),
        "book_value_to_bank": round(value * obligors, 0),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obligors", type=int, default=400)
    ap.add_argument("--json", type=Path, help="override assumptions from a JSON file")
    args = ap.parse_args(argv)
    a = load_measured(Assumptions())
    if args.json:
        for k, v in json.loads(args.json.read_text(encoding="utf-8")).items():
            setattr(a, k, v)
    out = compute(a, args.obligors)
    print("ASSUMPTIONS ARE PLACEHOLDERS. Replace with your costs and the bank's volumes.\n")
    for k, v in out.items():
        print(f"{k:38s} {v:>14,.1f}")
    for n in a.notes:
        print("note:", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
