"""Synthetic book: obligors, financial paths, sanctions and quarterly account conduct.

Conduct and default are generated from a latent stress process with three deliberate
realism features, so downstream models cannot score unrealistically well:
- measurement noise on every conduct feature;
- ``idiosyncratic_default_share`` of defaults come from shocks no input anticipates
  (fraud, sudden customer loss) and are therefore unpredictable by construction;
- some stressed borrowers recover without default (false-alarm base rate).
Ground truth is exact, so model metrics are measurements on this world, not on a real book.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import numpy as np
from domain.cts import CreditTermSheet
from domain.entities import Spread, SpreadLine
from testing.synthetic.cts_gen import SanctionPackage, make_sanction
from testing.synthetic.financials import (
    QUARTER_ENDS,
    UNIT_PAISE,
    FinancialHistory,
    generate_history,
)
from testing.synthetic.obligors import ObligorBundle, make_obligor
from testing.synthetic.reference import reference_ratio

N_Q = len(QUARTER_ENDS)


@dataclass
class QuarterConduct:
    util_avg: float
    overdrawn_days: int
    cheque_returns: int
    interest_delay_days: int
    gst_gap: int
    stock_stmt_late_days: int
    dpd_max: int


@dataclass
class BookEntry:
    bundle: ObligorBundle
    hist: FinancialHistory
    package: SanctionPackage
    spreads: list[Spread]
    conduct: list[QuarterConduct] = field(default_factory=list)
    stress: list[float] = field(default_factory=list)
    default_quarter: int | None = None  # first quarter reaching SMA-2 (dpd_max >= 61)
    idiosyncratic: bool = False


def spreads_from_history(h: FinancialHistory, obligor_id: str) -> list[Spread]:
    out = []
    for i, q in enumerate(h.quarters):
        lines = {c: SpreadLine(code=c, amount_paise=v * UNIT_PAISE) for c, v in q.lines.items()}
        out.append(
            Spread(
                spread_id=f"SPR-{obligor_id[-6:]}-{i:02d}",
                obligor_id=obligor_id,
                period_end=q.period_end,
                period_months=3,
                basis="standalone",
                audited=False,
                lines=lines,
            )
        )
    return out


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + float(np.exp(-x)))


def simulate_conduct(
    rng: random.Random,
    h: FinancialHistory,
) -> tuple[list[QuarterConduct], list[float], int | None, bool]:
    nrng = np.random.default_rng(rng.randint(0, 2**31))
    stress: list[float] = []
    for qi in range(N_Q):
        dscr = reference_ratio("dscr", h, qi) if qi >= 3 else None
        iscr = reference_ratio("iscr", h, qi) if qi >= 3 else None
        cur = reference_ratio("current_ratio", h, qi)
        d = float(dscr) if dscr is not None else 1.6
        i = float(iscr) if iscr is not None else 3.0
        c = float(cur) if cur is not None else 1.3
        z = 2.2 * (1.25 - d) + 0.5 * (2.0 - i) + 1.2 * (1.05 - c) + nrng.normal(0, 0.45) - 0.6
        stress.append(_sigmoid(z))
    conduct: list[QuarterConduct] = []
    default_q: int | None = None
    idio = False
    # an unforeseeable shock hits a small share of otherwise healthy-looking borrowers
    shock_q = rng.randint(5, N_Q - 1) if rng.random() < 0.04 else None
    for qi in range(N_Q):
        s = stress[qi]
        if shock_q is not None and qi >= shock_q:
            s = max(s, 0.85)
        util = float(np.clip(0.55 + 0.4 * s + nrng.normal(0, 0.06), 0.2, 1.05))
        od = int(max(0, nrng.poisson(0.5 + 18 * s**2)))
        chq = int(nrng.poisson(0.1 + 4.5 * s**2))
        idl = int(max(0, nrng.poisson(1 + 30 * s**2)))
        gst = int(nrng.poisson(0.1 + 2.5 * s**2))
        stk = int(max(0, nrng.poisson(2 + 25 * s**2)))
        p_dpd = _sigmoid(7.0 * (s - 0.72))
        dpd = 0
        if nrng.random() < p_dpd:
            dpd = int(nrng.choice([20, 45, 65, 75, 95], p=[0.15, 0.2, 0.3, 0.2, 0.15]))
        if shock_q is not None and qi == shock_q + 1:
            dpd = max(dpd, 65)
        conduct.append(QuarterConduct(util, od, chq, idl, gst, stk, dpd))
        if default_q is None and dpd >= 61:
            default_q = qi
            idio = shock_q is not None and qi in (shock_q, shock_q + 1)
    return conduct, stress, default_q, idio


def build_book(n: int, seed: int, template_share: float = 0.7) -> list[BookEntry]:
    rng = random.Random(seed)
    book: list[BookEntry] = []
    for i in range(n):
        b = make_obligor(rng, i + 1)
        h = generate_history(rng, b.obligor.obligor_id, b.trajectory, b.shock_quarter)
        style = "template" if rng.random() < template_share else "negotiated"
        pkg = make_sanction(rng, b, h, style, sanction_date=date(2023, 6, 15))
        conduct, stress, dq, idio = simulate_conduct(rng, h)
        book.append(
            BookEntry(
                b, h, pkg, spreads_from_history(h, b.obligor.obligor_id), conduct, stress, dq, idio
            )
        )
    return book


def cts_of(e: BookEntry) -> CreditTermSheet:
    return e.package.cts


def expected_covenant_result(op: str, value: Decimal, threshold: Decimal) -> str:
    """Independent oracle for the comparison step (plain Python, no engine code)."""
    ok = {
        "<=": value <= threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        ">": value > threshold,
        "==": value == threshold,
    }[op]
    return "COMPLIED" if ok else "BREACHED"


def run_covenants(e: BookEntry, tested_at: datetime) -> dict[tuple[str, int], object]:
    """Engine results for every covenant at every quarter from the first test date."""
    from covenant_engine.domain import evaluate_covenant

    cts = cts_of(e)
    out: dict[tuple[str, int], object] = {}
    for cov in cts.covenants:
        for qi, s in enumerate(e.spreads):
            if cov.first_test_date is not None and s.period_end >= cov.first_test_date:
                out[(cov.covenant_id, qi)] = evaluate_covenant(
                    cov, cts, e.spreads, s.period_end, tested_at=tested_at, test_id=f"TST-{qi}"
                )
    return out
