"""PII redaction with reversible tokens for LLM calls leaving the self-hosted zone.

Detects PAN, Aadhaar (Verhoeff-checked), GSTIN, bank account numbers, IFSC, phone, email
and named individuals supplied by the caller (promoters, guarantors, directors from the
obligor record). Placeholders are typed (``<PAN_1>``) so the model can still reason about
structure. ``restore`` re-identifies placeholders in the response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from identifiers.validators import is_valid_aadhaar

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GSTIN", re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")),
    ("PAN", re.compile(r"\b[A-Z]{3}[ABCFGHLJPT][A-Z][0-9]{4}[A-Z]\b")),
    ("AADHAAR", re.compile(r"\b[2-9][0-9]{3}[\s-]?[0-9]{4}[\s-]?[0-9]{4}\b")),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)")),
    ("ACCOUNT", re.compile(r"(?<![\d,.])\d{9,18}(?![\d,.])")),
]


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
    result = RedactionResult(text=text)
    reverse: dict[str, str] = {}

    def placeholder(kind: str, original: str) -> str:
        if original in reverse:
            return reverse[original]
        n = result.counts.get(kind, 0) + 1
        result.counts[kind] = n
        ph = f"<{kind}_{n}>"
        result.mapping[ph] = original
        reverse[original] = ph
        return ph

    out = text
    for name in sorted(person_names or [], key=len, reverse=True):
        if name.strip():
            out = re.sub(
                re.escape(name), lambda m: placeholder("PERSON", m.group(0)), out, flags=re.I
            )
    for kind, pat in _PATTERNS:

        def _sub(m: re.Match[str], kind: str = kind) -> str:
            val = m.group(0)
            if kind == "AADHAAR" and not is_valid_aadhaar(val):
                return val
            return placeholder(kind, val)

        out = pat.sub(_sub, out)
    result.text = out
    return result
