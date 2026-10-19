from __future__ import annotations

from decimal import Decimal

import pytest
from domain.coa import CODES
from hypothesis import given, settings
from hypothesis import strategies as st
from rules_engine.dsl import (
    DslError,
    Evaluator,
    MissingData,
    NotComputable,
    Resolved,
    compare,
    parse,
    validate,
)


class TableResolver:
    """Periods: offset 0 = latest quarter."""

    def __init__(self, data: dict[str, list[str]], ttm: int = 4) -> None:
        self.data = data
        self.ttm = ttm

    def resolve(self, name: str, offset: int) -> Resolved:
        vals = self.data.get(name)
        if vals is None or offset >= len(vals) or vals[offset] is None:
            raise MissingData(name, offset)
        return Resolved(Decimal(vals[offset]), (f"line:{name}@{offset}",))

    def ttm_periods(self) -> int:
        return self.ttm


DEFS = {
    "ebitda": "pbt + finance_costs + depreciation_amortisation - other_income",
    "total_debt": "lt_borrowings + st_borrowings + current_maturities_ltd + lease_liabilities",
    "net_debt": "total_debt - cash_and_equivalents - liquid_investments",
}


def _resolver() -> TableResolver:
    return TableResolver(
        {
            "pbt": ["100", "90", "80", "70", "60"],
            "finance_costs": ["20", "20", "20", "20", "20"],
            "depreciation_amortisation": ["10", "10", "10", "10", "10"],
            "other_income": ["5", "5", "5", "5", "5"],
            "lt_borrowings": ["900"],
            "st_borrowings": ["300"],
            "current_maturities_ltd": ["100"],
            "lease_liabilities": ["0"],
            "cash_and_equivalents": ["150"],
            "liquid_investments": ["50"],
        }
    )


def test_net_debt_to_ttm_ebitda() -> None:
    r = Evaluator(DEFS, _resolver()).evaluate("safe_div(net_debt, ttm(ebitda))")
    # net debt = 1300 - 200 = 1100; ttm ebitda = (125+115+105+95) = 440
    assert r.value == Decimal("2.5000")
    assert "line:pbt@3" in r.lineage
    assert r.inputs["pbt@-3"] == Decimal(70)


def test_prior_and_functions() -> None:
    e = Evaluator(DEFS, _resolver())
    assert e.evaluate("prior(pbt, 2)").value == Decimal(80)
    assert Evaluator(DEFS, _resolver()).evaluate("avg(pbt, prior(pbt))").value == Decimal(95)
    assert Evaluator(DEFS, _resolver()).evaluate(
        "max(1, 2, 3) - min(4, 5) + abs(-2)"
    ).value == Decimal(1)
    assert Evaluator(DEFS, _resolver()).evaluate("coalesce(missing_line, pbt)").value == Decimal(
        100
    )
    assert Evaluator(DEFS, _resolver()).evaluate("-(2 * 3) / 4").value == Decimal("-1.5")


def test_division_by_zero_is_typed() -> None:
    with pytest.raises(NotComputable):
        Evaluator({}, _resolver()).evaluate("pbt / (finance_costs - 20)")
    with pytest.raises(NotComputable):
        Evaluator({}, _resolver()).evaluate("safe_div(pbt, 0)")


def test_missing_data_is_typed() -> None:
    with pytest.raises(MissingData):
        Evaluator({}, _resolver()).evaluate("ttm(lt_borrowings)")


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os')",
        "pbt.__class__",
        "exec(1)",
        "pbt +",
        "sum()",
        "prior(pbt, x)",
        "lambda: 1",
        "pbt; 1",
        "open('f')",
        "1 ** 2",
        "ttm(pbt, 2)",
        "a[0]",
    ],
)
def test_rejects_unsafe_or_malformed(bad: str) -> None:
    with pytest.raises((DslError, MissingData)):
        parse(bad)
        Evaluator({}, _resolver()).evaluate(bad)


def test_static_validation() -> None:
    validate("safe_div(net_debt, ttm(ebitda))", DEFS, CODES)
    with pytest.raises(DslError):
        validate("safe_div(net_debt, unknown_thing)", DEFS, CODES)
    with pytest.raises(DslError):
        validate("a", {"a": "b + 1", "b": "a"}, CODES)


def test_depth_limit() -> None:
    with pytest.raises(DslError):
        parse("abs(" * 60 + "1" + ")" * 60)


def test_compare() -> None:
    assert compare(Decimal("3.5"), "<=", Decimal("3.5"))
    assert not compare(Decimal("3.51"), "<=", Decimal("3.5"))


@settings(max_examples=200)
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz_0123456789+-*/(), .", max_size=60))
def test_fuzz_never_crashes_unexpectedly(src: str) -> None:
    try:
        Evaluator({}, TableResolver({})).evaluate(src)
    except (DslError, MissingData, NotComputable):
        pass


@given(st.integers(-(10**9), 10**9), st.integers(1, 10**9))
def test_deterministic(a: int, b: int) -> None:
    r = TableResolver({"pbt": [str(a)], "finance_costs": [str(b)]})
    v1 = Evaluator({}, r).evaluate("pbt / finance_costs").value
    v2 = Evaluator({}, r).evaluate("pbt / finance_costs").value
    assert v1 == v2
