"""Covenant formula DSL (Section 7.6). Safe by construction: parsed to an AST, never eval'd.

    expr   := term (("+"|"-") term)*
    term   := factor (("*"|"/") factor)*
    factor := NUMBER | NAME | NAME "(" args ")" | "(" expr ")" | "-" factor

Functions: sum avg min max abs ttm prior coalesce safe_div.
- ``ttm(x)``     sums x over the trailing 12 months (4 quarters, 2 halves or 1 year).
- ``prior(x,n)`` evaluates x n periods earlier (n is an integer literal, default 1).
- ``coalesce(a,b,...)`` first argument that resolves (missing data is skipped).
- ``safe_div(a,b)`` and ``/``: division by zero is a typed NotComputable, never inf.

Names resolve to (a) agreement definitions (expanded recursively, cycles rejected) or
(b) chart-of-accounts codes via a resolver that returns value + lineage.
Arithmetic is Decimal with 28 significant digits; the final value is quantised per the
covenant's rounding policy (default ROUND_HALF_EVEN, 4 dp internal).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from functools import lru_cache
from typing import Protocol

from lark import Lark, Token, Tree
from lark.exceptions import LarkError

GRAMMAR = r"""
?start: expr
?expr: expr "+" term   -> add
     | expr "-" term   -> sub
     | term
?term: term "*" factor -> mul
     | term "/" factor -> div
     | factor
?factor: NUMBER        -> number
       | "-" factor    -> neg
       | NAME "(" [arglist] ")" -> call
       | NAME          -> var
       | "(" expr ")"
arglist: expr ("," expr)*
NAME: /[a-z_][a-z0-9_.]*/
NUMBER: /\d+(\.\d+)?/
%import common.WS
%ignore WS
"""

FUNCTIONS = frozenset({"sum", "avg", "min", "max", "abs", "ttm", "prior", "coalesce", "safe_div"})
MAX_FORMULA_LEN = 2_000
MAX_DEPTH = 40
MAX_EXPANSION = 2_000  # nodes evaluated, guards against definition blow-up

_parser = Lark(GRAMMAR, parser="lalr", maybe_placeholders=False)


class DslError(Exception):
    """Formula is malformed or references unknown names (a configuration error)."""


class NotComputable(Exception):
    """Mathematically undefined (e.g. division by zero). Maps to NOT_COMPUTABLE."""


class MissingData(Exception):
    """A required input is not available. Maps to NOT_TESTED_MISSING_DATA."""

    def __init__(self, name: str, offset: int) -> None:
        super().__init__(f"missing {name} at period offset {offset}")
        self.name = name
        self.offset = offset


@dataclass(frozen=True)
class Resolved:
    value: Decimal
    lineage: tuple[str, ...] = ()


class VariableResolver(Protocol):
    def resolve(self, name: str, offset: int) -> Resolved:
        """Value of chart-of-accounts code ``name`` for the period ``offset`` steps back.

        Raise MissingData when unavailable. Never return None.
        """

    def ttm_periods(self) -> int:
        """Number of periods that make up trailing twelve months (4, 2 or 1)."""


@dataclass
class EvalResult:
    value: Decimal  # rounded to the internal precision, for display and storage
    lineage: list[str] = field(default_factory=list)
    inputs: dict[str, Decimal] = field(default_factory=dict)
    exact: Decimal | None = None  # unrounded; covenant compliance is decided on this


@lru_cache(maxsize=4096)
def parse(formula: str) -> Tree[Token]:
    if len(formula) > MAX_FORMULA_LEN:
        raise DslError("formula too long")
    try:
        tree = _parser.parse(formula)
        _check(tree, 0)
    except LarkError as exc:
        raise DslError(f"cannot parse formula {formula!r}: {exc.__class__.__name__}") from exc
    except RecursionError as exc:
        raise DslError("formula too deeply nested") from exc
    return tree


def _check(node: Tree[Token] | Token, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise DslError("formula nesting too deep")
    if isinstance(node, Token):
        return
    if node.data == "call":
        name = str(node.children[0])
        if name not in FUNCTIONS:
            raise DslError(f"unknown function {name!r}")
        args = _args(node)
        if name == "prior":
            if not 1 <= len(args) <= 2:
                raise DslError("prior(x[, n]) takes 1 or 2 arguments")
            if len(args) == 2 and not (
                isinstance(args[1], Tree)
                and args[1].data == "number"
                and str(args[1].children[0]).isdigit()
            ):
                raise DslError("prior offset must be an integer literal")
        if name in ("ttm", "abs") and len(args) != 1:
            raise DslError(f"{name} takes exactly one argument")
        if name == "safe_div" and len(args) != 2:
            raise DslError("safe_div takes two arguments")
        if name in ("sum", "avg", "min", "max", "coalesce") and not args:
            raise DslError(f"{name} needs at least one argument")
    # Only function calls and negations add nesting; a flat sum of 100 terms is not "deep".
    nxt = depth + 1 if node.data in ("call", "neg") else depth
    for child in node.children:
        if child is not None:
            _check(child, nxt)


def _args(call: Tree[Token]) -> list[Tree[Token] | Token]:
    if len(call.children) < 2 or call.children[1] is None:
        return []
    arglist = call.children[1]
    assert isinstance(arglist, Tree)
    return list(arglist.children)


def referenced_names(formula: str) -> set[str]:
    names: set[str] = set()
    for sub in parse(formula).iter_subtrees():
        if sub.data == "var":
            names.add(str(sub.children[0]))
    return names


def validate(formula: str, definitions: dict[str, str], known_codes: frozenset[str]) -> None:
    """Static check: every name resolves to a definition or a known code; no cycles."""

    done: set[str] = set()  # memoised: shared sub-definitions are checked once, not per path

    def visit(f: str, stack: tuple[str, ...]) -> None:
        for name in referenced_names(f):
            if name in definitions:
                if name in stack:
                    raise DslError(f"definition cycle: {' -> '.join((*stack, name))}")
                if name in done:
                    continue
                visit(definitions[name], (*stack, name))
                done.add(name)
            elif name not in known_codes:
                raise DslError(f"unknown variable {name!r}")

    visit(formula, ())


class Evaluator:
    def __init__(self, definitions: dict[str, str], resolver: VariableResolver) -> None:
        self._defs = definitions
        self._resolver = resolver
        self._lineage: list[str] = []
        self._inputs: dict[str, Decimal] = {}
        self._steps = 0
        self._def_stack: list[str] = []

    def evaluate(self, formula: str, *, internal_dp: int = 4) -> EvalResult:
        tree = parse(formula)
        self._lineage, self._inputs, self._steps, self._def_stack = [], {}, 0, []
        q = Decimal(1).scaleb(-internal_dp)
        with localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
            try:
                exact = self._eval(tree, 0)
                value = exact.quantize(q, rounding=ROUND_HALF_EVEN)
            except InvalidOperation as exc:
                raise NotComputable("numeric overflow beyond supported precision") from exc
            except RecursionError as exc:
                raise DslError("formula too deeply nested") from exc
        return EvalResult(value, list(dict.fromkeys(self._lineage)), self._inputs, exact)

    # ---------------------------------------------------------------------------------
    def _eval(self, node: Tree[Token] | Token, offset: int) -> Decimal:
        self._steps += 1
        if self._steps > MAX_EXPANSION:
            raise DslError("formula expansion limit exceeded")
        if isinstance(node, Token):  # pragma: no cover - grammar wraps tokens
            raise DslError("unexpected token")
        kind = node.data
        if kind == "number":
            return Decimal(str(node.children[0]))
        if kind == "neg":
            return -self._eval(node.children[0], offset)
        if kind in ("add", "sub", "mul", "div"):
            a = self._eval(node.children[0], offset)
            b = self._eval(node.children[1], offset)
            if kind == "add":
                return a + b
            if kind == "sub":
                return a - b
            if kind == "mul":
                return a * b
            if b == 0:
                raise NotComputable("division by zero")
            return a / b
        if kind == "var":
            return self._var(str(node.children[0]), offset)
        if kind == "call":
            return self._call(node, offset)
        raise DslError(f"unknown node {kind}")  # pragma: no cover

    def _var(self, name: str, offset: int) -> Decimal:
        if name in self._defs:
            if name in self._def_stack:
                raise DslError(f"definition cycle at {name}")
            self._def_stack.append(name)
            try:
                return self._eval(parse(self._defs[name]), offset)
            finally:
                self._def_stack.pop()
        r = self._resolver.resolve(name, offset)
        key = name if offset == 0 else f"{name}@-{offset}"
        self._inputs[key] = r.value
        self._lineage.extend(r.lineage)
        return r.value

    def _call(self, node: Tree[Token], offset: int) -> Decimal:
        name = str(node.children[0])
        args = _args(node)
        if name == "ttm":
            n = self._resolver.ttm_periods()
            return sum((self._eval(args[0], offset + k) for k in range(n)), Decimal(0))
        if name == "prior":
            steps = (
                int(str(args[1].children[0])) if len(args) == 2 and isinstance(args[1], Tree) else 1
            )
            return self._eval(args[0], offset + steps)
        if name == "coalesce":
            for a in args:
                try:
                    return self._eval(a, offset)
                except MissingData:
                    continue
            raise MissingData("coalesce(...)", offset)
        values = [self._eval(a, offset) for a in args]
        if name == "sum":
            return sum(values, Decimal(0))
        if name == "avg":
            return sum(values, Decimal(0)) / len(values)
        if name == "min":
            return min(values)
        if name == "max":
            return max(values)
        if name == "abs":
            return abs(values[0])
        if name == "safe_div":
            if values[1] == 0:
                raise NotComputable("safe_div denominator is zero")
            return values[0] / values[1]
        raise DslError(f"unknown function {name}")  # pragma: no cover


def compare(value: Decimal, operator: str, threshold: Decimal) -> bool:
    match operator:
        case ">=":
            return value >= threshold
        case "<=":
            return value <= threshold
        case ">":
            return value > threshold
        case "<":
            return value < threshold
        case "==":
            return value == threshold
    raise DslError(f"unknown operator {operator}")
