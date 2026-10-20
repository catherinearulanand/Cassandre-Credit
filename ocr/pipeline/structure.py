"""Layout and table structure (stage 6) for statement pages: rows, columns, labels, amounts."""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field

import numpy as np
from identifiers.numbers import ParsedAmount, parse_amount
from rapidfuzz import fuzz

from ocr.pipeline.engine import Word

_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})
_AMOUNT_SHAPE = re.compile(r"^[\(\-]?[\d,.\sOolI|SB]+[\)\-]?$")


@dataclass
class Row:
    words: list[Word]
    y: float

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Cell:
    word: Word
    parsed: ParsedAmount
    text: str  # as read after confusion repair
    repaired: bool = False


@dataclass
class TableRow:
    label_words: list[Word]
    cells: list[Cell]
    y: float
    label: str = ""
    columns: dict[int, Cell] = field(default_factory=dict)  # 0=current, 1=previous


def estimate_skew_degrees(words: list[Word], max_deg: float = 6.0, step: float = 0.1) -> float:
    """Page skew from word-box centres: the angle at which rows line up best.

    Rotates the centres over a small angle range and keeps the angle whose vertical histogram is
    sharpest (sum of squared bin counts). Needs no image access, so it also corrects pages whose
    engine returned axis-aligned boxes on a rotated scan.
    """
    if len(words) < 8:
        return 0.0
    xs = np.array([w.xc for w in words])
    ys = np.array([w.yc for w in words])
    bin_w = max(4.0, statistics.median(w.h for w in words) / 2)
    best_a, best_s = 0.0, -1.0
    for a in np.arange(-max_deg, max_deg + step / 2, step):
        t = math.radians(float(a))
        y2 = ys * math.cos(t) - xs * math.sin(t)
        counts = np.histogram(y2, bins=np.arange(y2.min(), y2.max() + bin_w, bin_w))[0]
        score = float((counts.astype(float) ** 2).sum())
        if score > best_s + 1e-9:
            best_a, best_s = float(a), score
    return best_a


def deskew_words(words: list[Word], degrees: float) -> list[Word]:
    """Rotate box centres by ``degrees`` (keeping box size) so rows and columns are axis-aligned."""
    if abs(degrees) < 0.05:
        return words
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    out = []
    for w in words:
        x2 = w.xc * c + w.yc * s
        y2 = w.yc * c - w.xc * s
        hw, hh = (w.x1 - w.x0) / 2, (w.y1 - w.y0) / 2
        out.append(
            Word(
                w.text,
                x2 - hw,
                y2 - hh,
                x2 + hw,
                y2 + hh,
                w.conf,
                w.orig or (w.x0, w.y0, w.x1, w.y1),
            )
        )
    return out


# a splittable amount: comma-grouped (optionally with 2 dp) or a short 2-dp decimal. Bare digit runs
# and one-dp / three-dp decimals are never split: they are damaged numbers, not two columns.
_NUM = r"(?:(?:\d{1,2}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})+)(?:\.\d{2})?|\d{1,3}\.\d{2})"
_ONE = re.compile(_NUM)
_TWO_PLUS = re.compile(rf"(?:{_NUM}){{2,}}")


def _glued_parts(text: str) -> list[re.Match[str]] | None:
    """If ``text`` is two or more correctly grouped amounts glued together, return the parts.

    "2,27,77,70,00033,48,43,000" is "2,27,77,70,000" + "33,48,43,000". A single valid amount
    ("1,23,45,678.90") is never split; text that cannot be tiled exactly is left alone, so a
    damaged number is reviewed rather than guessed.
    """
    if _ONE.fullmatch(text) or not _TWO_PLUS.fullmatch(text):
        return None
    parts = list(_ONE.finditer(text))
    if len(parts) >= 2 and "".join(m.group(0) for m in parts) == text:
        return parts
    return None


def split_merged_numbers(words: list[Word], page_width: float = 0.0) -> list[Word]:
    """Split one recognised box that holds several amounts ("3,33,63,15,000 3,21,79,46,000")
    into one box per amount, apportioning the horizontal extent by character position. Engines
    merge adjacent columns when the gutter is narrow; without this the row loses a column."""
    out: list[Word] = []
    for w in words:
        spans_columns = page_width <= 0 or (w.x1 - w.x0) > 0.15 * page_width
        glued = _glued_parts(w.text.strip()) if spans_columns else None
        parts = glued if glued is not None else list(re.finditer(r"\S+", w.text))
        if len(parts) >= 2 and (
            glued is not None or all(_looks_numeric(m.group(0)) for m in parts)
        ):
            n = max(1, len(w.text))
            width = w.x1 - w.x0
            for m in parts:
                x0, x1 = w.x0 + width * m.start() / n, w.x0 + width * m.end() / n
                orig = None
                if w.orig is not None:
                    ow = w.orig[2] - w.orig[0]
                    orig = (
                        w.orig[0] + ow * m.start() / n,
                        w.orig[1],
                        w.orig[0] + ow * m.end() / n,
                        w.orig[3],
                    )
                out.append(Word(m.group(0), x0, w.y0, x1, w.y1, w.conf, orig))
        else:
            out.append(w)
    return out


def group_rows(words: list[Word]) -> list[Row]:
    """Cluster words into text rows by vertical centre (tolerant of skew via per-row mean)."""
    if not words:
        return []
    tol = 0.55 * statistics.median(w.h for w in words)
    rows: list[Row] = []
    for w in sorted(words, key=lambda w: w.yc):
        if rows and abs(w.yc - rows[-1].y) <= tol:
            rows[-1].words.append(w)
            rows[-1].y = sum(x.yc for x in rows[-1].words) / len(rows[-1].words)
        else:
            rows.append(Row([w], w.yc))
    for r in rows:
        r.words.sort(key=lambda w: w.x0)
    return rows


def _looks_numeric(text: str) -> bool:
    t = text.strip()
    if not t or not _AMOUNT_SHAPE.match(t):
        return False
    digits = sum(c.isdigit() for c in t)
    letters = sum(c.isalpha() for c in t)
    return digits >= 1 and digits >= letters


def to_cell(w: Word) -> Cell | None:
    if not _looks_numeric(w.text):
        if w.text.strip() in ("-", "–", "—"):
            return Cell(w, parse_amount("-") or ParsedAmount(0, False, None, w.text), w.text)  # type: ignore[arg-type]
        return None
    raw = w.text.strip()
    fixed = raw.translate(_CONFUSIONS) if any(c.isalpha() or c == "|" for c in raw) else raw
    if re.search(r"\d\s+\d", fixed):
        return None  # two figures in one box that could not be separated: do not join them
    fixed = re.sub(r"\s+", "", fixed)
    p = parse_amount(fixed)
    if p is None:
        return None
    return Cell(w, p, fixed, repaired=fixed != raw)


class TableList(list[TableRow]):
    """Table rows plus structural warnings about the page (column binding, single column)."""

    def __init__(self, rows: list[TableRow] | None = None) -> None:
        super().__init__(rows or [])
        self.flags: list[str] = []


def build_table(words: list[Word], page_width: float) -> TableList:
    words_deskewed = deskew_words(words, estimate_skew_degrees(words))
    rows = group_rows(split_merged_numbers(words_deskewed, page_width))
    table: list[TableRow] = []
    for r in rows:
        cells = [c for w in r.words if (c := to_cell(w)) is not None]
        # label = the words that are not amounts and are mostly letters; digit/symbol noise a
        # recogniser emits for a damaged cell ("000'06'06") must not contaminate the label
        label_words = [
            w
            for w in r.words
            if not any(c.word is w for c in cells)
            and sum(ch.isalpha() for ch in w.text) >= 0.5 * max(1, len(w.text.replace(" ", "")))
        ]
        table.append(TableRow(label_words, cells, r.y, " ".join(w.text for w in label_words)))
    # columns: cluster right edges of numeric cells; bind roles from the "Current"/"Previous" headers
    # (a swapped or single-column page must be detected, not assumed to be [current, previous])
    out = TableList(table)
    edges = sorted(c.word.x1 for t in table for c in t.cells)
    if not edges:
        return out
    gap = 0.06 * page_width
    clusters: list[list[float]] = [[edges[0]]]
    for e in edges[1:]:
        if e - clusters[-1][-1] > gap:
            clusters.append([])
        clusters[-1].append(e)
    big = [c for c in clusters if len(c) >= 3]
    ranked = sorted(big or clusters, key=len, reverse=True)
    centres_x1 = [sum(c) / len(c) for c in ranked]

    def cluster_of(c: Cell) -> int | None:
        k = min(range(len(centres_x1)), key=lambda i: abs(c.word.x1 - centres_x1[i]))
        return k if abs(c.word.x1 - centres_x1[k]) <= gap else None

    # mean cell-centre of each cluster, to compare with header word centres
    sums: dict[int, list[float]] = {}
    for t in table:
        for c in t.cells:
            k = cluster_of(c)
            if k is not None:
                sums.setdefault(k, []).append(c.word.xc)
    centre_xc = {k: sum(v) / len(v) for k, v in sums.items()}
    first_numeric_y = min(t.y for t in table if t.cells)
    heads: dict[str, float] = {}
    for w in words_deskewed:
        if w.yc >= first_numeric_y:
            continue
        txt = re.sub(r"[^a-z]", "", w.text.lower())
        if not 4 <= len(txt) <= 13:
            continue
        for role, name in (("current", "current"), ("previous", "previous")):
            if fuzz.ratio(txt, name) >= 70:
                heads[role] = w.xc
    role_cluster: dict[str, int] = {}
    for role, hx in heads.items():
        if centre_xc:
            k = min(centre_xc, key=lambda i: abs(centre_xc[i] - hx))
            if abs(centre_xc[k] - hx) <= 0.14 * page_width:
                role_cluster[role] = k
    if len(role_cluster) == 2 and role_cluster["current"] != role_cluster["previous"]:
        order = {role_cluster["current"]: 0, role_cluster["previous"]: 1}
    else:
        out.flags.append("column_binding_uncertain")
        positional = sorted(centre_xc, key=lambda i: centre_xc[i])[:2]
        order = {k: i for i, k in enumerate(positional)}
        if len(centre_xc) < 2:
            out.flags.append("single_numeric_column")
    for t in table:
        for c in t.cells:
            k = cluster_of(c)
            if k is not None and k in order:
                t.columns[order[k]] = c
    return out
