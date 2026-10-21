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


_AMOUNT_TOKEN = re.compile(r"\(?-?(?:\d[\d,]*\.\d{1,2}|\d{1,3}(?:,\d{2,3})+)\)?")


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
CONTINUITY_MAX = 5


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
    ordered = sorted(words, key=lambda w: (w.y0, w.x0))
    # The header is everything above the first row that carries a formatted amount (a number with a
    # decimal point or digit-group commas; a bare year or day such as "2025" does not count). Engines
    # that return one box per token need many more than 14 tokens to reach the unit note; engines that
    # return one box per line need fewer. Falls back to the first 14 tokens when no amount is found.
    first_amount_y = min(
        (w.y0 for w in ordered if _AMOUNT_TOKEN.fullmatch(w.text.strip())), default=None
    )
    above = [w for w in ordered if first_amount_y is not None and w.y0 < first_amount_y - 1]
    header = " ".join(w.text for w in (above[:80] if above else ordered[:14]))
    unit = detect_unit(header)
    unit_text = header if unit else None
    table: list[TableRow] = build_table(words, page_w)
    statement = detect_statement(words, table)
    structural_flags = list(getattr(table, "flags", []))
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
    flags = list(structural_flags)
    if unit is None:
        flags.append("unit_header_not_found")
    # Unit sanity. Statements in lakhs/crores print two decimals; statements in thousands or whole
    # rupees usually print none. A header that contradicts the number format is the signature of a
    # 100x-1000x scale error, so such a page goes to a human (conservative by design).
    if unit is not None and len(fields) >= 5:
        two_dp = sum(bool(re.search(r"\.\d{2}$", f.raw_text)) for f in fields.values()) / len(
            fields
        )
        if unit in (UnitScale.LAKHS, UnitScale.CRORES) and two_dp < 0.5:
            flags.append("unit_format_conflict:large_unit_header_but_whole_numbers")
        if unit in (UnitScale.THOUSANDS, UnitScale.RUPEES, UnitScale.MILLIONS) and two_dp > 0.2:
            flags.append("unit_format_conflict:small_unit_header_but_decimals")
    # Continuity: a value that jumps beyond 5x against the prior-period column on the same row is
    # more likely a mis-paired row or a swapped value than a real quarter. And a figure with no
    # prior-period value to compare with cannot be corroborated this way.
    for ef in fields.values():
        if ef.prior_paise is None:
            ef.validations.append("no_prior_period")
        elif ef.value_paise and ef.prior_paise:
            ratio = abs(ef.value_paise) / abs(ef.prior_paise)
            if ratio > CONTINUITY_MAX or ratio < 1 / CONTINUITY_MAX:
                ef.validations.append("continuity_break")
    expected = 15 if statement == "PL" else 30
    if len(fields) < MIN_YIELD * expected:
        flags.append(f"rescan_recommended:{len(fields)}_of_{expected}_lines")
    return StatementExtraction(statement, unit, unit_text, fields, unmapped, dups, flags)
