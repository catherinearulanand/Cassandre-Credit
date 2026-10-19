"""Field extraction (stages 7-8 for statements): label -> chart-of-accounts code, unit, lineage.

Deterministic first: fuzzy label matching against chart-of-accounts aliases, unit header
detection, amount parsing to paise, and a Citation (page, bbox) for every value. An LLM is
only needed for labels this mapping cannot resolve confidently (see docs/LLM_DECISION.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from domain.coa import LINES
from domain.lineage import Citation
from identifiers.numbers import UnitScale, detect_unit, to_paise
from rapidfuzz import fuzz

from ocr.pipeline.engine import Word
from ocr.pipeline.structure import TableRow, build_table

_NORM = re.compile(r"[^a-z0-9]+")


def norm(label: str) -> str:
    return _NORM.sub("", label.lower())


_ALIASES: dict[str, list[tuple[str, str]]] = {"PL": [], "BS": []}
for _ln in LINES:
    if _ln.statement in ("PL", "BS"):
        for _a in (_ln.label, *_ln.aliases):
            _ALIASES[_ln.statement].append((norm(_a), _ln.code))
# Statement-specific labels printed by real companies that differ from the CoA wording
for _extra, _code, _st in [
    ("Shareholders' Funds", "total_equity", "BS"),
    ("Shareholders Funds", "total_equity", "BS"),
    ("Profit for the year", "pat", "PL"),
    ("Net Sales", "revenue_from_operations", "PL"),
]:
    _ALIASES[_st].append((norm(_extra), _code))

LABEL_ACCEPT = 0.86


@dataclass
class ExtField:
    code: str
    value_paise: int
    raw_text: str
    label_text: str
    label_score: float
    ocr_conf: float
    citation: Citation
    row_y: float
    repaired_chars: bool = False
    validations: list[str] = field(default_factory=list)
    auto_corrected_from: int | None = None
    prior_paise: int | None = None
    prior_raw: str | None = None


@dataclass
class StatementExtraction:
    statement: str
    unit: UnitScale | None
    unit_text: str | None
    fields: dict[str, ExtField]
    unmapped_rows: list[tuple[str, float]]
    duplicates: list[str]
    doc_flags: list[str] = field(default_factory=list)


def _norm_box(w: Word, page_w: float, page_h: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = w.orig or (w.x0, w.y0, w.x1, w.y1)
    return (
        max(0.0, x0 / page_w),
        max(0.0, y0 / page_h),
        min(1.0, x1 / page_w),
        min(1.0, y1 / page_h),
    )


# Schedule III statements carry a known set of lines; a page that yields far fewer is unreadable
# or incomplete and should be re-scanned, not reviewed field by field.
EXPECTED_LINES = {
    st: sum(1 for ln in LINES if ln.statement == st and ln.code != "other_income_non_operating")
    for st in ("PL", "BS")
}
MIN_YIELD = 0.6


def map_label(label: str, statement: str) -> tuple[str | None, float]:
    n = norm(label)
    if len(n) < 3:
        return None, 0.0
    best_code, best = None, 0.0
    for alias, code in _ALIASES[statement]:
        s = fuzz.ratio(n, alias) / 100
        if s > best:
            best_code, best = code, s
    return (best_code if best >= LABEL_ACCEPT else None), best


def detect_statement(words: list[Word], table: list[TableRow] | None = None) -> str:
    """Statement type from the row labels first (robust to a garbled title), then the title."""
    if table:
        score = {"PL": 0.0, "BS": 0.0}
        for t in table:
            if t.label.strip() and t.columns.get(0) is not None:
                for st in score:
                    score[st] += map_label(t.label, st)[1] >= LABEL_ACCEPT
        if abs(score["PL"] - score["BS"]) >= 2:
            return max(score, key=lambda k: score[k])
    text = " ".join(w.text.lower() for w in words[:40]).replace(" ", "")
    if "balancesheet" in text:
        return "BS"
    return "PL"


def extract_statement(
    words: list[Word], page_w: float, page_h: float, document_id: str, page: int = 1
) -> StatementExtraction:
    header = " ".join(w.text for w in sorted(words, key=lambda w: (w.y0, w.x0))[:14])
    unit = detect_unit(header)
    unit_text = header if unit else None
    table: list[TableRow] = build_table(words, page_w)
    statement = detect_statement(words, table)
    fields: dict[str, ExtField] = {}
    unmapped: list[tuple[str, float]] = []
    dups: list[str] = []
    for t in table:
        cur = t.columns.get(0)
        if cur is None or not t.label.strip():
            continue
        code, score = map_label(t.label, statement)
        if code is None:
            unmapped.append((t.label, score))
            continue
        pw = t.columns.get(1)
        scale = unit or UnitScale.RUPEES
        ef = ExtField(
            code=code,
            value_paise=to_paise(cur.parsed, scale),
            raw_text=cur.text,
            label_text=t.label,
            label_score=score,
            ocr_conf=cur.word.conf,
            citation=Citation(
                document_id=document_id,
                page=page,
                bbox=_norm_box(cur.word, page_w, page_h),
                text_span=cur.text[:120],
                confidence=max(0.0, min(1.0, cur.word.conf)),
            ),
            row_y=t.y,
            repaired_chars=cur.repaired,
            prior_paise=to_paise(pw.parsed, scale) if pw else None,
            prior_raw=pw.text if pw else None,
        )
        if code in fields:
            dups.append(code)
            if score <= fields[code].label_score:
                continue
        fields[code] = ef
    flags = []
    if unit is None:
        flags.append("unit_header_not_found")
    expected = 15 if statement == "PL" else 30
    if len(fields) < MIN_YIELD * expected:
        flags.append(f"rescan_recommended:{len(fields)}_of_{expected}_lines")
    return StatementExtraction(statement, unit, unit_text, fields, unmapped, dups, flags)
