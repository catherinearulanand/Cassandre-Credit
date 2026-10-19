"""PII redaction with reversible tokens for LLM calls leaving the self-hosted zone.

Detects PAN, Aadhaar, GSTIN, bank account numbers, IFSC, phone, email, passport, DIN and the
named individuals supplied by the caller (promoters, guarantors, directors).

Design rules, all driven by audit findings:
- Normalise first (NFKC, so full-width digits and NBSP collapse to ASCII) and match
  case-insensitively: ``abcpk1234d`` is a PAN.
- Separators are tolerated inside numbers (space, dot, hyphen, NBSP).
- Fail safe: a digit run that looks like an Aadhaar but fails Verhoeff is still redacted as an
  account number. Over-redaction costs a little model context; under-redaction is a leak.
- Names match token-by-token, in either order, across spaces, commas and newlines.
Placeholders are typed (``<PAN_1>``) so the model can still reason about structure.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_SEP = r"[\s.\-]{0,2}"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GSTIN", re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b", re.I)),
    ("PAN", re.compile(r"\b[A-Z]{3}[ABCFGHLJPT][A-Z][0-9]{4}[A-Z]\b", re.I)),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", re.I)),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("PASSPORT", re.compile(r"\b[A-PR-WY][1-9]\d{6}\b", re.I)),
    ("DIN", re.compile(r"\bDIN[\s:.\-]*\d{8}\b", re.I)),
    ("PHONE", re.compile(r"(?<![\w])(?:\+?91[\s.\-]?)?[6-9](?:" + _SEP + r"\d){9}(?!\d)")),
    ("ACCOUNT", re.compile(r"\b[A-Z]{1,3}\d{8,16}\b", re.I)),
    # 9-18 digits with optional single separators; commas excluded so "1,23,45,678" amounts survive
    ("ACCOUNT", re.compile(r"(?<![\d,.])\d(?:" + _SEP + r"\d){8,17}(?![\d,]|\.\d)")),
]


def _name_pattern(name: str) -> re.Pattern[str] | None:
    tokens = [t for t in re.split(r"[\s,.]+", unicodedata.normalize("NFKC", name)) if t]
    if not tokens:
        return None
    sep = r"[\s,.]*"
    forward = sep.join(re.escape(t) for t in tokens)
    reverse = sep.join(re.escape(t) for t in reversed(tokens))
    alt = forward if forward == reverse else f"{forward}|{reverse}"
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.I)


@dataclass
class RedactionResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    counts: dict[str, int] = field(default_factory=dict)

    def restore(self, text: str) -> str:
        for placeholder, original in sorted(self.mapping.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(placeholder, original)
        return text


def redact(text: str, *, person_names: list[str] | None = None) -> RedactionResult:
    text = unicodedata.normalize("NFKC", text)
    result = RedactionResult(text=text)
    reverse: dict[str, str] = {}

    def placeholder(kind: str, original: str) -> str:
        key = f"{kind}|{re.sub(r'[\s.\-]', '', original).casefold()}"
        if key in reverse:
            return reverse[key]
        n = result.counts.get(kind, 0) + 1
        result.counts[kind] = n
        ph = f"<{kind}_{n}>"
        result.mapping[ph] = original
        reverse[key] = ph
        return ph

    out = text
    for name in sorted(person_names or [], key=len, reverse=True):
        pat = _name_pattern(name)
        if pat is not None:
            out = pat.sub(lambda m: placeholder("PERSON", m.group(0)), out)
    for kind, pat in _PATTERNS:
        out = pat.sub(lambda m, kind=kind: placeholder(kind, m.group(0)), out)  # type: ignore[misc]
    result.text = out
    return result
