"""Pure limit and drawing-power logic. All amounts are int paise; margins are Decimal.

Drawing power (standard Indian working-capital practice, per-asset margins):

    eligible_stock = stock - obsolete/slow-moving - stock-audit adjustment
                     - (unpaid creditors, if the CTS requires creditor deduction)
    eligible_bd    = book debts aged <= cut-off days - related-party debtors
    DP = min(sanctioned limit, max(0, eligible_stock x (1 - m_stock) + eligible_bd x (1 - m_bd)))

Invariants (property-tested): 0 <= DP <= sanctioned; DP is monotonic non-decreasing in
collateral value; DP is monotonic non-increasing in margin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from platform_core.errors import ValidationFailure


@dataclass(frozen=True)
class StockStatement:
    period_end: str
    raw_material: int
    wip: int
    finished_goods: int
    obsolete_stock: int = 0
    stock_audit_adjustment: int = 0
    unpaid_creditors: int = 0
    receivables_by_age: dict[int, int] = field(
        default_factory=dict
    )  # bucket upper bound days -> paise
    related_party_receivables: int = 0

    def total_stock(self) -> int:
        return self.raw_material + self.wip + self.finished_goods


@dataclass(frozen=True)
class DpResult:
    dp_paise: int
    eligible_stock: int
    eligible_book_debts: int
    stock_component: int
    bd_component: int
    capped_by_limit: bool
    inputs: dict[str, str]


def _apply_margin(amount: int, margin: Decimal) -> int:
    if not (Decimal(0) <= margin <= Decimal(1)):
        raise ValidationFailure("margin must be between 0 and 1")
    # floor: the bank never rounds drawing power up in the borrower's favour
    return int((Decimal(amount) * (Decimal(1) - margin)).to_integral_value(rounding=ROUND_FLOOR))


def compute_dp(
    sanctioned_paise: int,
    st: StockStatement,
    *,
    stock_margin: Decimal,
    bd_margin: Decimal,
    age_cutoff_days: int,
    deduct_creditors: bool = True,
) -> DpResult:
    if sanctioned_paise < 0:
        raise ValidationFailure("sanctioned limit cannot be negative")
    stock = st.total_stock() - st.obsolete_stock - st.stock_audit_adjustment
    if deduct_creditors:
        stock -= st.unpaid_creditors
    eligible_stock = max(0, stock)
    bd = sum(v for bucket, v in st.receivables_by_age.items() if bucket <= age_cutoff_days)
    eligible_bd = max(0, bd - st.related_party_receivables)
    s_comp = _apply_margin(eligible_stock, stock_margin)
    b_comp = _apply_margin(eligible_bd, bd_margin)
    raw = max(0, s_comp + b_comp)
    dp = min(sanctioned_paise, raw)
    return DpResult(
        dp,
        eligible_stock,
        eligible_bd,
        s_comp,
        b_comp,
        raw > sanctioned_paise,
        {
            "stock_margin": str(stock_margin),
            "bd_margin": str(bd_margin),
            "age_cutoff_days": str(age_cutoff_days),
            "deduct_creditors": str(deduct_creditors),
            "period_end": st.period_end,
        },
    )


from domain.limit_tree import AvailabilityResult, LimitState, LimitTree  # noqa: E402,F401  (re-export)
