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


# ---------------------------------------------------------------- limit tree ----------
@dataclass
class LimitState:
    node_id: str
    parent_id: str | None
    sanctioned: int
    utilised: int = 0
    status: str = "active"  # active | frozen | capped | closed
    cap: int | None = None
    dp: int | None = None  # revolving facility nodes only
    interchangeable_with: list[str] = field(default_factory=list)
    facility_id: str | None = None

    def ceiling(self) -> int:
        c = self.sanctioned
        if self.cap is not None:
            c = min(c, self.cap)
        if self.dp is not None:
            c = min(c, self.dp)
        return c

    def available(self) -> int:
        if self.status in ("frozen", "closed"):
            return 0
        return max(0, self.ceiling() - self.utilised)


@dataclass
class AvailabilityResult:
    ok: bool
    path: list[str]
    shortfalls: dict[str, int]
    used_interchange: list[str]


class LimitTree:
    def __init__(self) -> None:
        self.nodes: dict[str, LimitState] = {}

    def add(self, node: LimitState) -> None:
        if node.parent_id is not None and node.parent_id not in self.nodes:
            raise ValidationFailure(f"parent {node.parent_id} not in tree")
        self.nodes[node.node_id] = node

    def path_to_root(self, node_id: str) -> list[LimitState]:
        out, cur = [], self.nodes.get(node_id)
        seen: set[str] = set()
        while cur is not None:
            if cur.node_id in seen:
                raise ValidationFailure("cycle in limit tree")
            seen.add(cur.node_id)
            out.append(cur)
            cur = self.nodes.get(cur.parent_id) if cur.parent_id else None
        return out

    def check(self, node_id: str, amount: int) -> AvailabilityResult:
        """A drawdown must fit every node from the facility to the root.

        At the facility node, spare capacity in interchangeable (fungible) siblings may be
        borrowed; ancestors are never relaxed.
        """
        if amount <= 0:
            raise ValidationFailure("drawdown amount must be positive")
        if node_id not in self.nodes:
            raise ValidationFailure(f"unknown limit node {node_id}")
        path = self.path_to_root(node_id)
        shortfalls: dict[str, int] = {}
        used: list[str] = []
        for i, n in enumerate(path):
            avail = n.available()
            if i == 0 and avail < amount and n.status == "active":
                for sib_id in n.interchangeable_with:
                    sib = self.nodes.get(sib_id)
                    if sib and sib.status == "active" and avail < amount:
                        take = min(sib.available(), amount - avail)
                        if take > 0:
                            avail += take
                            used.append(sib_id)
            if avail < amount:
                shortfalls[n.node_id] = amount - avail
        return AvailabilityResult(not shortfalls, [n.node_id for n in path], shortfalls, used)

    def apply_consequence(self, node_id: str, consequence: str) -> LimitState:
        """Apply a covenant consequence to a node (the caller holds the approval/rule record)."""
        n = self.nodes[node_id]
        kind, _, arg = consequence.partition(":")
        if kind in ("stop_drawdown", "freeze"):
            n.status = "frozen"
        elif kind == "cap":
            n.status, n.cap = "capped", n.utilised if not arg else int(arg)
        elif kind in ("reprice", "event_of_default"):
            pass  # pricing and EoD act in Finacle / legal, not on availability
        else:
            raise ValidationFailure(f"unknown consequence {consequence}")
        return n
