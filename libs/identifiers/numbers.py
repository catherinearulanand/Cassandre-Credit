"""Parse amounts as printed in Indian financial documents.

Handles Indian digit grouping (1,23,45,678.90), western grouping, parentheses negatives,
trailing minus, Dr/Cr suffixes, inline units ("12.5 crore", "₹ 5 lakh") and statement unit
headers ("₹ in lakhs", "Rs. in crores", "(₹ '000)"). Everything is Decimal; output paise is int.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import Enum


class UnitScale(Enum):
    """Multiplier from the printed figure to rupees."""

    RUPEES = Decimal(1)
    THOUSANDS = Decimal(1_000)
    LAKHS = Decimal(100_000)
    MILLIONS = Decimal(1_000_000)
    CRORES = Decimal(10_000_000)


_UNIT_HEADER_PATTERNS: list[tuple[re.Pattern[str], UnitScale]] = [
    (re.compile(r"(in\s*)?(crores?|cr\.?)\b", re.I), UnitScale.CRORES),
    (re.compile(r"(in\s*)?(lakhs?|lacs?|lakh)\b", re.I), UnitScale.LAKHS),
    (re.compile(r"(in\s*)?millions?\b|\bmn\b", re.I), UnitScale.MILLIONS),
    (
        re.compile(r"'\s*000|in\s*'?\s*000|\(?\s*(in\s*)?thousands?\s*\)?|\(000\)", re.I),
        UnitScale.THOUSANDS,
    ),
    # "(Amount in INR)" / "(in rupees)" with no scale word means the figures are in rupees. OCR
    # often drops or misreads the rupee sign ("(Amount in )", "(Amount in 3)"), so match the words.
    (
        re.compile(r"amount\s*in(?![a-z]{2})|in\s*(rupees|inr)\b|\(\s*(rs\.?|inr)\s*\)", re.I),
        UnitScale.RUPEES,
    ),
]

_INLINE_UNIT = re.compile(r"(crores?|cr\.?|lakhs?|lacs?|millions?|mn|thousands?|k)\s*$", re.I)
_CURRENCY = re.compile(r"(₹|rs\.?|inr|rupees)", re.I)
_DASHES = {"-", "–", "—", "nil", "NIL", "Nil", ""}


@dataclass(frozen=True)
class ParsedAmount:
    value: Decimal  # in the printed unit
    negative: bool
    inline_unit: UnitScale | None
    raw: str


def detect_unit(header_text: str) -> UnitScale | None:
    """Detect a statement's unit header. Returns None when no unit header is present."""
    # typographic apostrophes and primes all mean the thousands tick in "(₹ ’000)"
    text = unicodedata.normalize("NFKC", header_text)  # full-width brackets/digits -> ASCII
    text = re.sub(r"[`\u2018\u2019\u02bc\u2032\u00b4]", "'", text)
    for pat, unit in _UNIT_HEADER_PATTERNS:
        if pat.search(text):
            return unit
    return None


def _inline_scale(token: str) -> UnitScale:
    t = token.lower().rstrip(".")
    if t.startswith(("crore", "cr")):
        return UnitScale.CRORES
    if t.startswith(("lakh", "lac")):
        return UnitScale.LAKHS
    if t.startswith(("million", "mn")):
        return UnitScale.MILLIONS
    return UnitScale.THOUSANDS


def parse_amount(text: str, *, cr_means: str | None = None) -> ParsedAmount | None:
    """Parse one printed amount. Returns None if the token is not a number.

    Dashes and "nil" parse to zero (financial statements use them for nil balances).
    ``cr_means`` resolves the "Cr" ambiguity: ``"crore"`` for sanction letters and
    statements, ``"credit"`` for bank-statement balances (where "Dr" means debit/negative).
    Left unset, a bare trailing "Cr" is ambiguous and the token is rejected (returns None):
    reading a credit balance as crore overstates it by a factor of ten million.
    """
    if cr_means not in (None, "crore", "credit"):
        raise ValueError("cr_means must be 'crore' or 'credit'")
    raw = text
    s = text.strip().replace(" ", " ")
    if s in _DASHES:
        return ParsedAmount(Decimal(0), False, None, raw)
    negative = False
    s = _CURRENCY.sub("", s).strip()
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    upper = s.upper()
    if re.search(r"\d\s*DR\.?$", upper):
        negative, s = True, re.sub(r"\s*[Dd][Rr]\.?$", "", s).strip()
    elif cr_means == "credit" and re.search(r"\d\s*CR\.?$", upper):
        s = re.sub(r"\s*[Cc][Rr]\.?$", "", s).strip()
    if cr_means is None and re.search(r"\d\s*cr\.?$", s, re.I):
        return None
    inline: UnitScale | None = None
    m = _INLINE_UNIT.search(s)
    if m and re.search(r"\d", s[: m.start()]):
        inline = _inline_scale(m.group(1))
        s = s[: m.start()].strip()
    if s.endswith("-") and len(s) > 1:
        negative, s = True, s[:-1].strip()
    if s.startswith("-") or s.startswith("−"):
        negative, s = True, s[1:].strip()
    s = s.replace(" ", "")
    # Commas must follow Indian (1,23,45,678) or western (12,345,678) grouping exactly;
    # anything else (European "1.234,56", "1,000,00", "12,34,5") is rejected, not guessed.
    if "," in s:
        whole = s.split(".", 1)[0]
        indian = re.fullmatch(r"\d{1,2}(,\d{2})*,\d{3}", whole)
        western = re.fullmatch(r"\d{1,3}(,\d{3})+", whole)
        if not (indian or western):
            return None
        if "." in s and s.rfind(",") > s.index("."):
            return None
        s = s.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:  # pragma: no cover - regex guards
        return None
    return ParsedAmount(value, negative, inline, raw)


def to_paise(parsed: ParsedAmount, statement_unit: UnitScale | None = None) -> int:
    """Convert to integer paise. An inline unit overrides the statement unit header."""
    scale = parsed.inline_unit or statement_unit or UnitScale.RUPEES
    rupees = parsed.value * scale.value
    paise = (rupees * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return -int(paise) if parsed.negative else int(paise)
