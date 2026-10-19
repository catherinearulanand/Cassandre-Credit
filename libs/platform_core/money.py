"""Money as integer paise. Floats are never accepted.

Why: a single binary-float rounding error in drawing power or a covenant ratio is a
regulatory finding. Amounts are exact integers; ratios are ``Decimal`` with an explicit
rounding policy.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

PAISE_PER_RUPEE = 100
LAKH = 100_000
CRORE = 10_000_000


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount_paise: int
    currency: str = "INR"

    @field_validator("amount_paise", mode="before")
    @classmethod
    def _no_float(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise TypeError("Money does not accept float; use int paise or Decimal rupees")
        return v

    # ---- construction -------------------------------------------------------------
    @classmethod
    def zero(cls, currency: str = "INR") -> Money:
        return cls(amount_paise=0, currency=currency)

    @classmethod
    def from_rupees(cls, rupees: Decimal | int | str, currency: str = "INR") -> Money:
        if isinstance(rupees, float):
            raise TypeError("float rupees are not accepted")
        d = Decimal(rupees) * PAISE_PER_RUPEE
        return cls(
            amount_paise=int(d.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)), currency=currency
        )

    @classmethod
    def from_crore(cls, crore: Decimal | int | str) -> Money:
        return cls.from_rupees(Decimal(crore) * CRORE)

    @classmethod
    def from_lakh(cls, lakh: Decimal | int | str) -> Money:
        return cls.from_rupees(Decimal(lakh) * LAKH)

    # ---- arithmetic ---------------------------------------------------------------
    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(amount_paise=self.amount_paise + other.amount_paise, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(amount_paise=self.amount_paise - other.amount_paise, currency=self.currency)

    def __neg__(self) -> Money:
        return Money(amount_paise=-self.amount_paise, currency=self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_paise < other.amount_paise

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_paise <= other.amount_paise

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_paise > other.amount_paise

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_paise >= other.amount_paise

    def scale(self, factor: Decimal) -> Money:
        """Multiply by a Decimal factor (e.g. ``1 - margin``), rounding half-even to paise."""
        if isinstance(factor, float):
            raise TypeError("float factor not accepted")
        v = (Decimal(self.amount_paise) * factor).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        return Money(amount_paise=int(v), currency=self.currency)

    @property
    def rupees(self) -> Decimal:
        return Decimal(self.amount_paise) / PAISE_PER_RUPEE

    def min(self, other: Money) -> Money:
        return self if self <= other else other

    def max(self, other: Money) -> Money:
        return self if self >= other else other

    def format_indian(self) -> str:
        """Render with Indian digit grouping: ₹1,23,45,678.90."""
        return f"₹{format_indian_number(self.rupees)}"


def format_indian_number(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    q = abs(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    whole, frac = f"{q:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"{sign}{whole}.{frac}"
