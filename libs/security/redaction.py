"""PII redaction with reversible tokens for LLM calls leaving the self-hosted zone.

Detects PAN, Aadhaar, GSTIN, bank account numbers, IFSC, phone, email, UPI addresses, passport,
DIN and the named individuals supplied by the caller (promoters, guarantors, directors).

Design rules, all driven by audit findings:
- Normalise first (NFKC; zero-width and other format characters removed) and match
  case-insensitively: ``abcpk1234d`` and a PAN with a zero-width character inside are both PANs.
- Separators are tolerated inside numbers (space, dot, hyphen, brackets).
- Fail safe: a digit run that looks like an Aadhaar but fails Verhoeff is still redacted as an
  account number. Over-redaction costs a little model context; under-redaction is a leak.
- Amounts are not account numbers. A bare digit run is left alone when the text just before it
  says it is an amount ("amount_paise": 1000000000, "limit 500000000", rupee sign). Callers should
  introduce account numbers with a label ("A/c No.") so they are caught either way.
- Names match token by token, in either order, across spaces, commas and newlines.
Placeholders are typed (``<PAN_1>``) so the model can still reason about structure.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_SEP = r"[\s.\-]{0,2}"
_AMOUNT_CONTEXT = re.compile(
    r"(amount|paise|rupees|inr|rs\.?|₹|value|limit|sanction\w*|balance|exposure|crore|lakh|"
    r"drawing\s+power|dp|outstanding|\"\s*:)\s*[:=]?\s*$",
    re.I,
)
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GSTIN", re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b", re.I)),
    ("PAN", re.compile(r"\b[A-Z]{3}[ABCFGHLJPT][A-Z][\s\-]?[0-9]{4}[\s\-]?[A-Z]\b", re.I)),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", re.I)),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("UPI", re.compile(r"\b[\w.\-]{3,}@[A-Za-z]{3,}\b")),
    ("PASSPORT", re.compile(r"\b[A-PR-WY][1-9]\d{6}\b", re.I)),
    ("DIN", re.compile(r"\bDIN[\s:.\-]*\d{8}\b", re.I)),
    ("PHONE", re.compile(r"(?<![\w])(?:\+?91[\s.\-]?)?\(?[6-9](?:[\s.\-()]{0,2}\d){9}(?!\d)")),
    ("ACCOUNT", re.compile(r"\b[A-Z]{1,3}\d{8,16}\b", re.I)),
    # 9-18 digits with optional separators; commas excluded so "1,23,45,678" amounts survive
    ("ACCOUNT_BARE", re.compile(r"(?<![\d,.])\d(?:" + _SEP + r"\d){8,17}(?![\d,]|\.\d)")),
]
# "A/c No. 12345678", "account number: 98765432": any labelled number is an account number
_LABELLED = re.compile(
    r"(\b(?:a/c|acct?|account)(?:\s*(?:no|number|#)\.?)?\s*[:\-]?\s*)(\d{6,18})\b", re.I
)


def _name_pattern(name: str) -> re.Pattern[str] | None:
    tokens = [t for t in re.split(r"[\s,.]+", unicodedata.normalize("NFKC", name)) if t]
    if not tokens:
        return None
    sep = r"[\s,.]*"
    forward = sep.join(re.escape(t) for t in tokens)
    reverse = sep.join(re.escape(t) for t in reversed(tokens))
    alt = forward if forward == reverse else f"{forward}|{reverse}"
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.I)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


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
    text = _clean(text)
    result = RedactionResult(text=text)
    reverse: dict[str, str] = {}

    def placeholder(kind: str, original: str) -> str:
        kind = "ACCOUNT" if kind == "ACCOUNT_BARE" else kind
        key = f"{kind}|{re.sub(r'[\s.\-()]', '', original).casefold()}"
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
    out = _LABELLED.sub(lambda m: m.group(1) + placeholder("ACCOUNT", m.group(2)), out)

    for kind, pat in _PATTERNS:

        def _sub(m: re.Match[str], kind: str = kind) -> str:
            if kind == "ACCOUNT_BARE" and _AMOUNT_CONTEXT.search(
                m.string[max(0, m.start() - 28) : m.start()]
            ):
                return m.group(0)  # an amount, not an account number
            return placeholder(kind, m.group(0))

        out = pat.sub(_sub, out)
    result.text = out
    return result
