"""Pure covenant-testing logic. No I/O, no wall clock.

Result semantics (§8.7):
- WAIVED: an approved waiver exists for this covenant and period.
- NOT_TESTED_MISSING_DATA: an input line or a TTM period is missing.
- NOT_COMPUTABLE: mathematically undefined (e.g. zero denominator).
- COMPLIED / BREACHED: value compared with the threshold at 4 dp (internal precision).
- IN_CURE: breached, but the agreement grants a cure period that has not expired.
- An equity cure (where allowed and cures remain) is applied to the numerator of the
  top-level ratio: subtracted for leverage covenants ("<="), added for coverage (">=").
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from domain.cts import CovenantDef, CreditTermSheet
from domain.entities import CovenantTestRecord, Spread, TestResult
from rules_engine.dsl import (
    DslError,
    Evaluator,
    MissingData,
    NotComputable,
    Resolved,
    compare,
    parse,
)

Q4 = Decimal("0.0001")


class SpreadSeriesResolver:
    """Resolves chart-of-accounts codes over a period-ordered list of spreads.

    offset 0 is the test period; offset k is k periods earlier. Spreads must be contiguous
    and of one periodicity/basis, or the resolver refuses (no silent mixing).
    """

    def __init__(self, spreads: list[Spread], test_index: int) -> None:
        self.spreads = spreads
        self.idx = test_index
        months = {s.period_months for s in spreads}
        bases = {s.basis for s in spreads}
        if len(months) != 1 or len(bases) != 1:
            raise DslError("spread series mixes periodicities or bases")
        self.period_months = months.pop()

    def ttm_periods(self) -> int:
        return 12 // self.period_months

    def resolve(self, name: str, offset: int) -> Resolved:
        i = self.idx - offset
        if i < 0:
            raise MissingData(name, offset)
        s = self.spreads[i]
        line = s.lines.get(name)
        if line is None:
            raise MissingData(name, offset)
        lineage = [f"spread:{s.spread_id}:line:{name}"]
        lineage += [f"doc:{c.document_id}:p{c.page}" for c in line.citations]
        return Resolved(Decimal(line.amount_paise) / 100, tuple(lineage))


@dataclass(frozen=True)
class Waiver:
    covenant_id: str
    period_end: date
    approval_id: str


@dataclass(frozen=True)
class EquityCure:
    covenant_id: str
    period_end: date
    amount_rupees: Decimal
    approval_id: str


@dataclass
class TestContext:
    __test__ = False  # not a pytest class

    waivers: list[Waiver] = field(default_factory=list)
    equity_cures: list[EquityCure] = field(default_factory=list)
    equity_cures_used: dict[str, int] = field(default_factory=dict)
    today: date | None = None  # for cure-period expiry


def _contiguous(spreads: list[Spread]) -> bool:
    for a, b in itertools.pairwise(spreads):
        gap = (b.period_end.year - a.period_end.year) * 12 + (
            b.period_end.month - a.period_end.month
        )
        if gap != a.period_months:
            return False
    return True


def _split_ratio(formula: str) -> tuple[str, str] | None:
    """If the formula is ``safe_div(a, b)`` at top level, return the source of a and b."""
    tree = parse(formula)
    if tree.data == "call" and str(tree.children[0]) == "safe_div":
        depth, start = 0, formula.index("(") + 1
        for i, ch in enumerate(formula[start:], start):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                return formula[start:i].strip(), formula[i + 1 : formula.rindex(")")].strip()
    return None


def evaluate_covenant(
    cov: CovenantDef,
    cts: CreditTermSheet,
    spreads: list[Spread],
    period_end: date,
    *,
    tested_at: datetime,
    test_id: str,
    ctx: TestContext | None = None,
) -> CovenantTestRecord:
    ctx = ctx or TestContext()
    base = dict(
        test_id=test_id,
        covenant_id=cov.covenant_id,
        cts_id=cts.cts_id,
        cts_version=cts.version,
        obligor_id=cts.obligor_id,
        period_end=period_end,
        threshold=cov.threshold,
        operator=cov.operator,
        tested_at=tested_at,
    )
    if any(w.covenant_id == cov.covenant_id and w.period_end == period_end for w in ctx.waivers):
        w = next(
            w
            for w in ctx.waivers
            if w.covenant_id == cov.covenant_id and w.period_end == period_end
        )
        return CovenantTestRecord(
            **base, value=None, result=TestResult.WAIVED, notes=[f"waived under {w.approval_id}"]
        )
    if (
        cov.klass != "financial"
        or cov.formula is None
        or cov.operator is None
        or cov.threshold is None
    ):
        return CovenantTestRecord(
            **base,
            value=None,
            result=TestResult.DEFERRED,
            notes=["non-financial covenant: tested by obligation workflow"],
        )
    if cov.first_test_date is not None and period_end < cov.first_test_date:
        return CovenantTestRecord(
            **base,
            value=None,
            result=TestResult.DEFERRED,
            notes=[f"before the first test date {cov.first_test_date}"],
        )
    chosen: dict[date, Spread] = {}
    for s in spreads:  # a restated period: prefer the audited spread, else the latest supplied
        if s.basis != cov.test_basis:
            continue
        cur = chosen.get(s.period_end)
        if cur is None or s.audited or not cur.audited:
            chosen[s.period_end] = s
    series = [chosen[k] for k in sorted(chosen)]
    idx = next((i for i, s in enumerate(series) if s.period_end == period_end), None)
    if idx is None:
        return CovenantTestRecord(
            **base,
            value=None,
            result=TestResult.NOT_TESTED_MISSING_DATA,
            notes=[f"no {cov.test_basis} spread for {period_end}"],
        )
    if not _contiguous(series[: idx + 1]):
        return CovenantTestRecord(
            **base,
            value=None,
            result=TestResult.NOT_TESTED_MISSING_DATA,
            notes=["spread series has a gap; TTM would be wrong"],
        )
    resolver = SpreadSeriesResolver(series[: idx + 1], idx)
    try:
        ev = Evaluator(cts.definitions, resolver)
        res = ev.evaluate(cov.formula)
    except MissingData as exc:
        return CovenantTestRecord(
            **base, value=None, result=TestResult.NOT_TESTED_MISSING_DATA, notes=[str(exc)]
        )
    except NotComputable as exc:
        return CovenantTestRecord(
            **base, value=None, result=TestResult.NOT_COMPUTABLE, notes=[str(exc)]
        )
    cure_applied: str | None = None
    value = res.value
    exact = res.exact if res.exact is not None else res.value
    notes: list[str] = []
    passed = compare(exact, cov.operator, cov.threshold)
    parts = _split_ratio(cov.formula)
    if parts is not None:
        den0 = Evaluator(cts.definitions, resolver).evaluate(parts[1]).value
        if den0 < 0:
            # A ratio over a negative base (negative EBITDA, negative net worth) is not a
            # meaningful pass: a leverage covenant would read "-0.004x <= 3.5x" as compliant.
            passed = False
            notes.append(
                f"denominator is negative ({den0}): ratio not meaningful; treated as a breach"
            )
    if not passed and cov.equity_cure_allowed:
        cure = next(
            (
                c
                for c in ctx.equity_cures
                if c.covenant_id == cov.covenant_id and c.period_end == period_end
            ),
            None,
        )
        used = ctx.equity_cures_used.get(cov.covenant_id, 0)
        if cure and cure.amount_rupees > 0 and used < cov.max_equity_cures and parts:
            num = Evaluator(cts.definitions, resolver).evaluate(parts[0]).exact
            den = Evaluator(cts.definitions, resolver).evaluate(parts[1]).exact
            assert num is not None and den is not None
            # a leverage cure reduces the numerator, but never below zero
            adj = (
                max(Decimal(0), num - cure.amount_rupees)
                if cov.operator in ("<=", "<")
                else num + cure.amount_rupees
            )
            if den > 0:
                cured_exact = adj / den
                notes.append(
                    f"equity cure ₹{cure.amount_rupees} applied ({cure.approval_id}); "
                    f"pre-cure value {value}"
                )
                cure_applied = cure.approval_id
                value = cured_exact.quantize(Q4, rounding=ROUND_HALF_EVEN)
                passed = compare(cured_exact, cov.operator, cov.threshold)
    result = TestResult.COMPLIED if passed else TestResult.BREACHED
    if not passed and (cov.cure_period_days or cov.grace_days):
        deadline = period_end + timedelta(days=cov.grace_days + cov.cure_period_days)
        # No clock supplied means no cure can be shown to be running: fail closed, stay BREACHED.
        if ctx.today is not None and ctx.today <= deadline:
            result = TestResult.IN_CURE
            notes.append(f"grace/cure period until {deadline.isoformat()}")
    headroom = None
    if cov.threshold != 0:
        h = (cov.threshold - value) if cov.operator in ("<=", "<") else (value - cov.threshold)
        headroom = (h / abs(cov.threshold) * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
    return CovenantTestRecord(
        **base,
        value=value,
        result=result,
        headroom_pct=headroom,
        inputs={k: str(v) for k, v in res.inputs.items()},
        lineage=res.lineage,
        notes=notes,
        equity_cure_applied=cure_applied,
    )


def due_test_dates(cov: CovenantDef, until: date) -> list[date]:
    """Quarter/half/year ends from first_test_date up to ``until`` (Indian FY calendar)."""
    if cov.first_test_date is None or cov.test_frequency == "event":
        return []
    step = {"monthly": 1, "quarterly": 3, "half_yearly": 6, "annual": 12}[cov.test_frequency]
    out, d = [], cov.first_test_date
    while d <= until:
        out.append(d)
        m = d.month + step
        y = d.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        # month end
        nxt = date(y + (m // 12), m % 12 + 1, 1) - timedelta(days=1)
        d = nxt
    return out
