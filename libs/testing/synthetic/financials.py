"""Synthetic quarterly Schedule III financials with exact accounting identities.

All amounts are integers in units of 0.01 lakh (= ₹1,000 = 1,00,000 paise), so every figure
prints exactly in lakhs (2 dp), thousands (0 dp) or rupees, and printed values equal ground
truth. Totals are computed from rounded components, so BS and P&L identities hold exactly.

Trajectories:
- healthy: steady growth, stable margins
- deteriorating: from a random quarter, margins compress, receivable days stretch, WC fully drawn
- shock: a one-quarter revenue collapse (e.g. plant outage / key customer loss) then partial recovery
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date

UNIT_PAISE = 100_000  # 0.01 lakh in paise

QUARTER_ENDS = [
    date(2023, 6, 30),
    date(2023, 9, 30),
    date(2023, 12, 31),
    date(2024, 3, 31),
    date(2024, 6, 30),
    date(2024, 9, 30),
    date(2024, 12, 31),
    date(2025, 3, 31),
    date(2025, 6, 30),
    date(2025, 9, 30),
    date(2025, 12, 31),
    date(2026, 3, 31),
    date(2026, 6, 30),
    date(2026, 9, 30),
]

PL_LINES = [
    "revenue_from_operations",
    "other_income",
    "total_income",
    "cost_of_materials",
    "change_in_inventories",
    "employee_benefits",
    "finance_costs",
    "depreciation_amortisation",
    "other_expenses",
    "total_expenses",
    "pbt",
    "current_tax",
    "deferred_tax",
    "tax_expense",
    "pat",
]
BS_LINES = [
    "share_capital",
    "other_equity",
    "total_equity",
    "lt_borrowings",
    "lease_liabilities",
    "deferred_tax_liability",
    "lt_provisions",
    "other_non_current_liabilities",
    "non_current_liabilities",
    "st_borrowings",
    "current_maturities_ltd",
    "trade_payables",
    "other_current_liabilities",
    "st_provisions",
    "current_liabilities",
    "total_equity_and_liabilities",
    "net_fixed_assets",
    "cwip",
    "intangible_assets",
    "lt_investments",
    "deferred_tax_asset",
    "other_non_current_assets",
    "non_current_assets",
    "inventories",
    "trade_receivables",
    "cash_and_equivalents",
    "liquid_investments",
    "other_current_assets",
    "current_assets",
    "total_assets",
]
MEMO_LINES = [
    "principal_repayment_lt",
    "finance_costs_lt",
    "other_income_non_operating",
    "lt_secured_debt",
]


@dataclass
class QuarterFinancials:
    period_end: date
    lines: dict[str, int]  # code -> units of 0.01 lakh

    def paise(self, code: str) -> int:
        return self.lines[code] * UNIT_PAISE


@dataclass
class FinancialHistory:
    obligor_id: str
    quarters: list[QuarterFinancials] = field(default_factory=list)
    cc_limit_units: int = 0
    tl_amount_units: int = 0
    tl_tenor_quarters: int = 28


def _r(x: float) -> int:
    return round(x)


def generate_history(
    rng: random.Random, obligor_id: str, trajectory: str, shock_quarter: int | None = None
) -> FinancialHistory:
    annual_rev = rng.uniform(5_000, 150_000) * 100  # lakhs -> units (0.01 lakh)
    q_rev = annual_rev / 4
    margin = rng.uniform(0.11, 0.20)
    growth = rng.uniform(0.005, 0.03)
    rate_q = rng.uniform(0.085, 0.11) / 4
    tl = annual_rev * rng.uniform(0.25, 0.55)
    tenor_q = rng.choice([20, 24, 28, 32])
    principal_q = tl / tenor_q
    cc_limit = annual_rev * rng.uniform(0.12, 0.22)
    nfa = annual_rev * rng.uniform(0.45, 0.8)
    share_capital = _r(annual_rev * rng.uniform(0.03, 0.08))
    other_equity = annual_rev * rng.uniform(0.18, 0.35)
    deterioration_start = rng.randint(5, 9) if trajectory == "deteriorating" else None
    recv_days, inv_days, pay_days = rng.uniform(60, 80), rng.uniform(70, 100), rng.uniform(45, 65)
    util = rng.uniform(0.55, 0.85)
    tl_out = tl
    hist = FinancialHistory(
        obligor_id, cc_limit_units=_r(cc_limit), tl_amount_units=_r(tl), tl_tenor_quarters=tenor_q
    )
    prev_inv = None
    for qi, qend in enumerate(QUARTER_ENDS):
        seasonal = 1.0 + 0.06 * [0.0, -0.5, 0.3, 0.8][qi % 4]
        q_rev *= 1 + growth
        rev_mult, m = 1.0, margin
        if deterioration_start is not None and qi >= deterioration_start:
            k = qi - deterioration_start + 1
            m = max(0.01, margin - 0.018 * k)
            rev_mult = max(0.6, 1 - 0.035 * k)
            recv_days += 6
            util = min(1.0, util + 0.06)
        if shock_quarter is not None:
            if qi == shock_quarter:
                rev_mult, m = 0.55, -0.04
                util = 1.0
            elif qi > shock_quarter:
                rev_mult, m = 0.8, max(0.02, margin - 0.06)
        rev = _r(q_rev * seasonal * rev_mult)
        other_income = _r(rev * rng.uniform(0.004, 0.012))
        dep = _r(nfa * 0.022)
        cc_used = cc_limit * util
        fin_lt = _r(tl_out * rate_q)
        fin = fin_lt + _r(cc_used * rate_q * 1.05)
        ebitda = _r(rev * m)
        pbt = ebitda - fin - dep + other_income
        total_income = rev + other_income
        total_exp = total_income - pbt
        com = _r(rev * rng.uniform(0.55, 0.62))
        emp = _r(rev * rng.uniform(0.07, 0.11))
        inv_now = _r(com * 4 * inv_days / 365)
        chg_inv = 0 if prev_inv is None else _r((prev_inv - inv_now) * 0.15)
        other_exp = total_exp - (com + chg_inv + emp + fin + dep)
        if other_exp < 0:  # keep a sane expense split; push the gap into materials
            com += other_exp - _r(rev * 0.02)
            other_exp = _r(rev * 0.02)
        total_exp = com + chg_inv + emp + fin + dep + other_exp
        pbt = total_income - total_exp
        cur_tax = _r(max(pbt, 0) * 0.22)
        def_tax = _r(max(pbt, 0) * 0.03) if pbt > 0 else _r(pbt * 0.02)
        tax = cur_tax + def_tax
        pat = pbt - tax
        # ---- balance sheet ---------------------------------------------------------
        principal_paid = min(principal_q, tl_out)
        tl_out = max(0.0, tl_out - principal_paid)
        cmltd = _r(min(tl_out, principal_q * 4))
        lt_borr = _r(tl_out) - cmltd
        capex = nfa * 0.02
        nfa = nfa + capex - dep
        other_equity += pat
        st_borr = _r(cc_used)
        payables = _r(com * 4 * pay_days / 365)
        recv = _r(rev * 4 * recv_days / 365)
        lines = {
            "revenue_from_operations": rev,
            "other_income": other_income,
            "total_income": total_income,
            "cost_of_materials": com,
            "change_in_inventories": chg_inv,
            "employee_benefits": emp,
            "finance_costs": fin,
            "depreciation_amortisation": dep,
            "other_expenses": other_exp,
            "total_expenses": total_exp,
            "pbt": pbt,
            "current_tax": cur_tax,
            "deferred_tax": def_tax,
            "tax_expense": tax,
            "pat": pat,
            "share_capital": share_capital,
            "other_equity": _r(other_equity),
            "lt_borrowings": lt_borr,
            "lease_liabilities": _r(annual_rev * 0.01),
            "deferred_tax_liability": _r(annual_rev * 0.008),
            "lt_provisions": _r(annual_rev * 0.006),
            "other_non_current_liabilities": _r(annual_rev * 0.004),
            "st_borrowings": st_borr,
            "current_maturities_ltd": cmltd,
            "trade_payables": payables,
            "other_current_liabilities": _r(rev * 0.12),
            "st_provisions": _r(rev * 0.02),
            "net_fixed_assets": _r(nfa),
            "cwip": _r(annual_rev * 0.015),
            "intangible_assets": _r(annual_rev * 0.006),
            "lt_investments": _r(annual_rev * 0.02),
            "deferred_tax_asset": _r(annual_rev * 0.002),
            "other_non_current_assets": _r(annual_rev * 0.01),
            "inventories": inv_now,
            "trade_receivables": recv,
            "liquid_investments": _r(annual_rev * 0.01),
            "other_current_assets": _r(rev * 0.08),
            "principal_repayment_lt": _r(principal_paid),
            "finance_costs_lt": fin_lt,
            "other_income_non_operating": other_income,
            "lt_secured_debt": _r(tl_out),
        }
        _close_balance_sheet(lines, rev)
        prev_inv = inv_now
        hist.quarters.append(QuarterFinancials(qend, lines))
    return hist


def _close_balance_sheet(x: dict[str, int], rev: int) -> None:
    x["total_equity"] = x["share_capital"] + x["other_equity"]
    x["non_current_liabilities"] = sum(
        x[k]
        for k in (
            "lt_borrowings",
            "lease_liabilities",
            "deferred_tax_liability",
            "lt_provisions",
            "other_non_current_liabilities",
        )
    )
    x["non_current_assets"] = sum(
        x[k]
        for k in (
            "net_fixed_assets",
            "cwip",
            "intangible_assets",
            "lt_investments",
            "deferred_tax_asset",
            "other_non_current_assets",
        )
    )
    for _ in range(3):
        x["current_liabilities"] = sum(
            x[k]
            for k in (
                "st_borrowings",
                "current_maturities_ltd",
                "trade_payables",
                "other_current_liabilities",
                "st_provisions",
            )
        )
        x["total_equity_and_liabilities"] = (
            x["total_equity"] + x["non_current_liabilities"] + x["current_liabilities"]
        )
        ca_ex_cash = sum(
            x[k]
            for k in (
                "inventories",
                "trade_receivables",
                "liquid_investments",
                "other_current_assets",
            )
        )
        cash = x["total_equity_and_liabilities"] - x["non_current_assets"] - ca_ex_cash
        min_cash = max(1, rev // 50)
        if cash >= min_cash:
            x["cash_and_equivalents"] = cash
            break
        # equity eroded: the gap is funded by other current liabilities (creditors stretch)
        x["other_current_liabilities"] += min_cash - cash
    x["current_assets"] = sum(
        x[k]
        for k in (
            "inventories",
            "trade_receivables",
            "cash_and_equivalents",
            "liquid_investments",
            "other_current_assets",
        )
    )
    x["total_assets"] = x["non_current_assets"] + x["current_assets"]
    assert x["total_assets"] == x["total_equity_and_liabilities"], "BS must balance"
