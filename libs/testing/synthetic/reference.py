"""Independent reference calculator ("manual recalculation" for the Phase 1 gate).

Deliberately written WITHOUT the DSL, parser or chart-of-accounts definitions: each ratio is
hand-coded from raw statement lines, the way a credit analyst would build it in a workbook.
If the engine and this module agree, the DSL, definitions, TTM windowing, sign handling and
rounding are all corroborated by an independent implementation.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from testing.synthetic.financials import FinancialHistory


def _v(h: FinancialHistory, qi: int, code: str) -> Decimal:
    # units of 0.01 lakh -> rupees
    return Decimal(h.quarters[qi].lines[code]) * 1000


def _ttm(h: FinancialHistory, qi: int, fn) -> Decimal | None:  # type: ignore[no-untyped-def]
    if qi < 3:
        return None
    return sum((fn(q) for q in range(qi - 3, qi + 1)), Decimal(0))


def reference_ratio(key: str, h: FinancialHistory, qi: int) -> Decimal | None:
    """Returns the ratio rounded half-even to 4 dp, or None if not computable / insufficient data."""
    with localcontext() as ctx:
        ctx.prec = 50

        def ebitda(q: int) -> Decimal:
            return (
                _v(h, q, "pbt")
                + _v(h, q, "finance_costs")
                + _v(h, q, "depreciation_amortisation")
                - _v(h, q, "other_income_non_operating")
            )

        def total_debt(q: int) -> Decimal:
            return (
                _v(h, q, "lt_borrowings")
                + _v(h, q, "st_borrowings")
                + _v(h, q, "current_maturities_ltd")
                + _v(h, q, "lease_liabilities")
            )

        def tnw(q: int) -> Decimal:
            return (
                _v(h, q, "share_capital")
                + _v(h, q, "other_equity")
                - _v(h, q, "intangible_assets")
                - _v(h, q, "deferred_tax_asset")
            )

        num: Decimal | None
        den: Decimal | None
        if key == "net_debt_ebitda":
            num = (
                total_debt(qi) - _v(h, qi, "cash_and_equivalents") - _v(h, qi, "liquid_investments")
            )
            den = _ttm(h, qi, ebitda)
        elif key == "dscr":
            num = _ttm(
                h,
                qi,
                lambda q: (
                    _v(h, q, "pat")
                    + _v(h, q, "depreciation_amortisation")
                    + _v(h, q, "finance_costs_lt")
                ),
            )
            den = _ttm(
                h, qi, lambda q: _v(h, q, "principal_repayment_lt") + _v(h, q, "finance_costs_lt")
            )
        elif key == "iscr":
            num = _ttm(h, qi, ebitda)
            den = _ttm(h, qi, lambda q: _v(h, q, "finance_costs"))
        elif key == "current_ratio":
            num, den = _v(h, qi, "current_assets"), _v(h, qi, "current_liabilities")
        elif key == "tol_tnw":
            num = _v(h, qi, "non_current_liabilities") + _v(h, qi, "current_liabilities")
            den = tnw(qi)
        elif key == "debt_equity":
            num = total_debt(qi)
            den = _v(h, qi, "share_capital") + _v(h, qi, "other_equity")
        else:
            raise KeyError(key)
        if num is None or den is None or den == 0:
            return None
        return (num / den).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
