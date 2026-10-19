"""Limit tree: availability along the path to the root, fungibility and consequences."""

from __future__ import annotations

from dataclasses import dataclass, field

from platform_core.errors import ValidationFailure


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
