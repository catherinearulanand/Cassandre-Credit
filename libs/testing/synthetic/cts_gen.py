"""Synthetic sanctions: ground-truth CTS + the sanction letter text it was compiled from.

Two letter styles:
- ``template``: the bank's standard sanction-letter format (most Indian banks use one).
  Deterministic grammar extraction should handle it.
- ``negotiated``: facility-agreement prose with defined terms, cross-references and
  paraphrase. This is where a schema-constrained LLM is needed (see docs/LLM_DECISION.md).

Citations in the ground-truth CTS point at the letter line (page 1, bbox by line index).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from domain.cts import (
    CollateralRequirement,
    Condition,
    CovenantDef,
    CreditTermSheet,
    Facility,
    FacilityType,
    LimitNode,
)
from domain.lineage import Citation
from platform_core.money import Money

from testing.synthetic.financials import UNIT_PAISE, FinancialHistory
from testing.synthetic.obligors import ObligorBundle
from testing.synthetic.reference import reference_ratio

LIB_DEFS = {
    "ebitda": "pbt + finance_costs + depreciation_amortisation - other_income_non_operating",
    "total_debt": "lt_borrowings + st_borrowings + current_maturities_ltd + lease_liabilities",
    "net_debt": "total_debt - cash_and_equivalents - liquid_investments",
    "tnw": "share_capital + other_equity - intangible_assets - deferred_tax_asset",
    "tol": "non_current_liabilities + current_liabilities",
}
LIB_COVENANTS = {
    "net_debt_ebitda": ("Net Debt / EBITDA", "safe_div(net_debt, ttm(ebitda))", "<="),
    "dscr": (
        "DSCR",
        "safe_div(ttm(pat + depreciation_amortisation + finance_costs_lt), "
        "ttm(principal_repayment_lt + finance_costs_lt))",
        ">=",
    ),
    "iscr": ("Interest Service Coverage Ratio", "safe_div(ttm(ebitda), ttm(finance_costs))", ">="),
    "current_ratio": ("Current Ratio", "safe_div(current_assets, current_liabilities)", ">="),
    "tol_tnw": ("TOL / TNW", "safe_div(tol, tnw)", "<="),
    "debt_equity": ("Debt / Equity", "safe_div(total_debt, share_capital + other_equity)", "<="),
}

AUTHORITIES = [
    ("branch_credit_committee", 25 * 10**7),
    ("zonal_credit_committee", 150 * 10**7),
    ("central_credit_committee", 750 * 10**7),
    ("board_credit_committee", 10**13),
]


@dataclass
class SanctionPackage:
    cts: CreditTermSheet
    letter_lines: list[str]
    style: str
    document_id: str
    field_lines: dict[str, int] = field(default_factory=dict)  # field path -> line index


def _cite(doc: str, idx: int, n: int, text: str) -> Citation:
    y0 = min(0.98, 0.04 + idx * (0.92 / max(n, 1)))
    return Citation(
        document_id=doc,
        page=1,
        bbox=(0.06, y0, 0.94, min(1.0, y0 + 0.02)),
        text_span=text[:120],
        confidence=1.0,
    )


def _fmt_crore(units: int, rng: random.Random, style: str) -> str:
    rupees = Decimal(units) * 1000
    crore = rupees / Decimal(10**7)
    choice = rng.randint(0, 2) if style == "negotiated" else rng.choice([0, 0, 1])
    if choice == 0:
        return f"Rs. {crore:.2f} crore"
    if choice == 1:
        return f"₹{(rupees / Decimal(10**5)):.2f} lakh"
    from platform_core.money import format_indian_number

    return f"INR {format_indian_number(rupees)}"


def _round_units(units: int, step_lakh: int = 50) -> int:
    step = step_lakh * 100
    return max(step, (units // step) * step)


def _threshold_for(key: str, start: Decimal | None, rng: random.Random) -> Decimal:
    q = Decimal("0.05")
    if start is None:
        base = {
            "net_debt_ebitda": Decimal("3.5"),
            "dscr": Decimal("1.25"),
            "iscr": Decimal("2.0"),
            "current_ratio": Decimal("1.1"),
            "tol_tnw": Decimal("3.0"),
            "debt_equity": Decimal("2.0"),
        }[key]
        return base
    if LIB_COVENANTS[key][2] == "<=":
        t = start * Decimal(str(rng.uniform(1.15, 1.45)))
    else:
        t = start * Decimal(str(rng.uniform(0.65, 0.85)))
    return (t / q).quantize(Decimal(1)) * q


def make_sanction(
    rng: random.Random,
    ob: ObligorBundle,
    hist: FinancialHistory,
    style: str = "template",
    sanction_date: date = date(2024, 6, 15),
) -> SanctionPackage:
    o = ob.obligor
    doc = f"DOC-SANC-{o.obligor_id}"
    tl_units = _round_units(hist.tl_amount_units)
    cc_units = _round_units(hist.cc_limit_units)
    total_units = tl_units + cc_units
    total_paise = total_units * UNIT_PAISE
    authority = next(a for a, power in AUTHORITIES if total_paise // 100 <= power)
    authority_power = dict(AUTHORITIES)[authority]
    margin = Decimal(rng.choice(["0.25", "0.25", "0.30"]))
    age_cutoff = rng.choice([90, 90, 120])
    # priced above the (illustrative) policy floor for the obligor's rating, as a real sanction would be
    floor = {"CR1": 50, "CR2": 90, "CR3": 140, "CR4": 200, "CR5": 275, "CR6": 375}.get(
        o.internal_rating, 300
    )
    spread_tl, spread_cc = floor + rng.choice([25, 50, 75, 100]), floor + rng.choice([10, 25, 50])
    tenor = hist.tl_tenor_quarters * 3
    # pick covenants: always DSCR + Net Debt/EBITDA, plus 2-3 more
    keys = ["net_debt_ebitda", "dscr", *rng.sample(["iscr", "current_ratio", "tol_tnw", "debt_equity"], k=rng.randint(2, 3))]
    q4 = hist.quarters[4]  # first test quarter (needs four quarters for TTM)
    lines: list[str] = []
    fl: dict[str, int] = {}

    def add(text: str, key: str | None = None) -> int:
        lines.append(text)
        if key:
            fl[key] = len(lines) - 1
        return len(lines) - 1

    sref = f"SL/{o.region[:2]}/{sanction_date.year}/{rng.randint(1000, 9999)}"
    add("SYNTHETIC DOCUMENT - FOR TESTING ONLY")
    if style == "template":
        add(f"SANCTION LETTER  Ref: {sref}", "sanction_ref")
        add(f"Date: {sanction_date.strftime('%d.%m.%Y')}", "sanction_date")
        add(f"Borrower: {o.legal_name} (CIF {o.cif})")
        add(f"Sanctioning Authority: {authority.replace('_', ' ').title()}", "authority")
        add(
            f"Facility 1: Term Loan of {_fmt_crore(tl_units, rng, style)} for a tenor of {tenor} months "
            f"at EBLR + {spread_tl} bps",
            "fac.TL",
        )
        add(
            f"Facility 2: Cash Credit of {_fmt_crore(cc_units, rng, style)} at EBLR + {spread_cc} bps, "
            f"margin {int(margin * 100)}% on stock and book debts, book debts older than {age_cutoff} days ineligible",
            "fac.CC",
        )
        add(
            "Financial Covenants (tested quarterly on standalone financials, first test on "
            f"{q4.period_end.strftime('%d.%m.%Y')}):",
            "first_test",
        )
    else:
        add(
            f'FACILITY AGREEMENT dated {sanction_date.strftime("%d %B %Y")} (the "Agreement") under sanction '
            f"reference {sref}",
            "sanction_ref",
        )
        add(
            f'between {o.legal_name} (the "Borrower") and the Bank. Approved by the '
            f"{authority.replace('_', ' ')}.",
            "authority",
        )
        lines.append(f"Effective Date: {sanction_date.isoformat()}")
        fl["sanction_date"] = len(lines) - 1
        add(
            f"Clause 2.1 The Bank agrees to make available a term loan facility in an aggregate amount of "
            f'{_fmt_crore(tl_units, rng, style)} (the "Term Facility"), repayable over {tenor} months, bearing '
            f"interest at the External Benchmark Lending Rate plus a margin of {spread_tl} basis points.",
            "fac.TL",
        )
        add(
            f"Clause 2.2 The Bank further agrees to make available a fund-based working capital facility by way of "
            f'cash credit not exceeding {_fmt_crore(cc_units, rng, style)} (the "WC Facility") at EBLR plus '
            f"{spread_cc} basis points, subject to Drawing Power computed after a margin of {int(margin * 100)} per cent "
            f"on eligible current assets; receivables outstanding beyond {age_cutoff} days shall be excluded.",
            "fac.CC",
        )
        add(
            "Clause 21 (Financial Covenants). The Borrower shall ensure that, on each Test Date commencing "
            f"{q4.period_end.strftime('%d %B %Y')}, by reference to its standalone Financial Statements:",
            "first_test",
        )
    covenants: list[CovenantDef] = []
    phrasing_t = {
        "<=": ["shall not exceed {t}x", "not more than {t}x", "maximum {t}x"],
        ">=": ["shall not be less than {t}x", "minimum {t}x", "not below {t}x"],
    }
    phrasing_n = {
        "net_debt_ebitda": "the ratio of Consolidated Net Borrowings to EBITDA for the Relevant Period {p}",
        "dscr": "the Debt Service Cover Ratio for each Relevant Period {p}",
        "iscr": "the ratio of EBITDA to Finance Charges (interest cover) {p}",
        "current_ratio": "Current Assets to Current Liabilities {p}",
        "tol_tnw": "Total Outside Liabilities to Tangible Net Worth {p}",
        "debt_equity": "the Leverage (Total Debt to Equity) {p}",
    }
    negotiated_phrase = {"<=": "is not greater than {t}:1", ">=": "is at least {t}:1"}
    for i, key in enumerate(keys, 1):
        name, formula, op = LIB_COVENANTS[key]
        start = reference_ratio(key, hist, 4)
        thr = _threshold_for(key, start, rng)
        cons = ["stop_drawdown"] if key in ("dscr", "net_debt_ebitda") else []
        cons = [f"reprice:+{rng.choice([25, 50, 75])}bps", *cons]
        cure = rng.choice([0, 30, 45])
        equity = key in ("dscr", "net_debt_ebitda") and rng.random() < 0.4
        if style == "template":
            ph = rng.choice(phrasing_t[op]).format(t=f"{thr:.2f}")
            extra = f"; cure period {cure} days" if cure else ""
            extra += "; equity cure permitted" if equity else ""
            cons_txt = " and ".join(
                "additional interest of " + c.split("+")[1].replace("bps", " bps")
                if c.startswith("reprice")
                else "stop further drawdowns"
                for c in cons
            )
            idx = add(
                f"{chr(96 + i)}) {name} {ph}{extra}. Consequence of breach: {cons_txt}.",
                f"cov.{key}",
            )
        else:
            ph = negotiated_phrase[op].format(t=f"{thr:.2f}")
            body = phrasing_n[key].format(p=ph)
            extra = (
                f", provided that the Borrower may remedy a breach within {cure} days of the Compliance "
                f"Certificate"
                if cure
                else ""
            )
            extra += (
                ", and the Sponsors may cure by subscribing for additional equity (Clause 21.4)"
                if equity
                else ""
            )
            idx = add(
                f"21.{i} {body}{extra}. Upon an Event of Default under this Clause 21.{i}, the Margin shall "
                f"increase by {cons[0].split('+')[1].replace('bps', '')} basis points"
                + (
                    " and the Bank shall have no obligation to permit any further Utilisation."
                    if "stop_drawdown" in cons
                    else "."
                ),
                f"cov.{key}",
            )
        covenants.append(
            CovenantDef(
                covenant_id=f"COV-{o.obligor_id[-6:]}-{i:02d}",
                klass="financial",
                name=name,
                formula=formula,
                operator=op,
                threshold=thr,
                test_frequency="quarterly",
                test_basis="standalone",
                first_test_date=q4.period_end,
                cure_period_days=cure,
                equity_cure_allowed=equity,
                max_equity_cures=2 if equity else 0,
                consequence=cons,
                applies_to_limit_ids=[f"LIM-{o.obligor_id[-6:]}-TL", f"LIM-{o.obligor_id[-6:]}-CC"],
                citations=[_cite(doc, idx, 30, lines[idx])],
            )
        )
    cp_idx = add(
        "Conditions precedent: creation of hypothecation charge on current assets and registration "
        "with CERSAI; board resolution authorising the borrowing.",
        "cp",
    )
    cs_idx = add(
        "Conditions subsequent: filing of Form CHG-1 with the Registrar of Companies within 30 days "
        "of charge creation.",
        "cs",
    )
    add(
        "Security: first charge on stock and book debts; first charge on plant and machinery; "
        "personal guarantee of the promoters."
    )
    add(
        f"This sanction is valid until {(sanction_date + timedelta(days=365)).strftime('%d.%m.%Y')} "
        "and subject to annual review.",
        "valid_until",
    )
    n = len(lines)
    sid = o.obligor_id[-6:]
    fac_tl = Facility(
        facility_id=f"FAC-{sid}-TL",
        facility_type=FacilityType.TERM_LOAN,
        amount=Money(amount_paise=tl_units * UNIT_PAISE),
        tenor_months=tenor,
        pricing_spread_bps=spread_tl,
        citations=[_cite(doc, fl["fac.TL"], n, lines[fl["fac.TL"]])],
    )
    fac_cc = Facility(
        facility_id=f"FAC-{sid}-CC",
        facility_type=FacilityType.CASH_CREDIT,
        amount=Money(amount_paise=cc_units * UNIT_PAISE),
        tenor_months=12,
        pricing_spread_bps=spread_cc,
        margin_pct=margin,
        receivable_age_cutoff_days=age_cutoff,
        citations=[_cite(doc, fl["fac.CC"], n, lines[fl["fac.CC"]])],
    )
    top = _cite(doc, 1, n, lines[1])
    limits = [
        LimitNode(
            node_id=f"LIM-{sid}-OBL",
            parent_id=None,
            node_type="obligor",
            sanctioned=Money(amount_paise=total_units * UNIT_PAISE),
            valid_from=sanction_date,
            valid_to=sanction_date + timedelta(days=365 * 8),
            citations=[top],
        ),
        LimitNode(
            node_id=f"LIM-{sid}-TL",
            parent_id=f"LIM-{sid}-OBL",
            node_type="facility",
            facility_id=fac_tl.facility_id,
            sanctioned=fac_tl.amount,
            valid_from=sanction_date,
            valid_to=sanction_date + timedelta(days=30 * tenor),
            citations=fac_tl.citations,
        ),
        LimitNode(
            node_id=f"LIM-{sid}-CC",
            parent_id=f"LIM-{sid}-OBL",
            node_type="facility",
            facility_id=fac_cc.facility_id,
            sanctioned=fac_cc.amount,
            valid_from=sanction_date,
            valid_to=sanction_date + timedelta(days=365),
            citations=fac_cc.citations,
        ),
    ]
    cps = [
        Condition(
            condition_id=f"CND-{sid}-CP1",
            kind="precedent",
            description="Hypothecation charge on current assets registered with CERSAI",
            evidence_doc_type="cersai_acknowledgement",
            blocks=["first_drawdown"],
            citations=[_cite(doc, cp_idx, n, lines[cp_idx])],
        ),
        Condition(
            condition_id=f"CND-{sid}-CP2",
            kind="precedent",
            description="Board resolution",
            evidence_doc_type="board_resolution",
            blocks=["first_drawdown"],
            citations=[_cite(doc, cp_idx, n, lines[cp_idx])],
        ),
        Condition(
            condition_id=f"CND-{sid}-CS1",
            kind="subsequent",
            description="Form CHG-1 filed with ROC",
            evidence_doc_type="chg1_acknowledgement",
            due_date=sanction_date + timedelta(days=30),
            blocks=["all_drawdowns"],
            stops_drawdown_when_overdue=True,
            citations=[_cite(doc, cs_idx, n, lines[cs_idx])],
        ),
    ]
    collateral = [
        CollateralRequirement(
            collateral_id=f"COL-{sid}-CA",
            collateral_type="stock",
            charge_type="hypothecation",
            rank="first",
            margin_pct=margin,
            secures_facility_ids=[fac_cc.facility_id],
            registrations_required=["CERSAI", "CHG-1", "insurance_bank_clause"],
            citations=[_cite(doc, n - 2, n, lines[n - 2])],
        ),
        CollateralRequirement(
            collateral_id=f"COL-{sid}-BD",
            collateral_type="book_debts",
            charge_type="hypothecation",
            rank="first",
            margin_pct=margin,
            secures_facility_ids=[fac_cc.facility_id],
            registrations_required=["CERSAI", "CHG-1"],
            citations=[_cite(doc, n - 2, n, lines[n - 2])],
        ),
        CollateralRequirement(
            collateral_id=f"COL-{sid}-PM",
            collateral_type="plant_machinery",
            charge_type="hypothecation",
            rank="first",
            margin_pct=Decimal("0.25"),
            secures_facility_ids=[fac_tl.facility_id],
            registrations_required=["CERSAI", "CHG-1", "valuation"],
            citations=[_cite(doc, n - 2, n, lines[n - 2])],
        ),
    ]
    cts = CreditTermSheet(
        cts_id=f"CTS-{sid}",
        version=1,
        obligor_id=o.obligor_id,
        sanction_ref=sref,
        sanctioning_authority=authority,
        sanctioning_authority_power=Money(amount_paise=authority_power * 100),
        sanction_date=sanction_date,
        valid_until=sanction_date + timedelta(days=365),
        review_due=sanction_date + timedelta(days=365),
        facilities=[fac_tl, fac_cc],
        limits=limits,
        collateral=collateral,
        covenants=covenants,
        definitions=dict(LIB_DEFS),
        conditions=cps,
    )
    return SanctionPackage(cts, lines, style, doc, fl)
