from __future__ import annotations

import random

import numpy as np
import pytest
from identifiers.numbers import UnitScale
from testing.synthetic.financials import generate_history
from testing.synthetic.obligors import make_obligor

from ocr.eval.metrics import cer, wilson
from ocr.pipeline.engine import Word
from ocr.pipeline.extract import extract_statement, map_label
from ocr.pipeline.route import Calibrator, ece, hard_blocked
from ocr.pipeline.structure import build_table, group_rows, to_cell
from ocr.pipeline.validate import evaluate_identities, validate_statement
from ocr.synthetic.render import render_statement

W, H = 1240, 900


def _w(text: str, x0: float, y: float, x1: float | None = None, conf: float = 0.99) -> Word:
    x1 = x1 if x1 is not None else x0 + 10 * len(text)
    return Word(text, x0, y, x1, y + 24, conf)


def _page(
    rows: list[tuple[str, str, str]], unit: str = "(in lakhs)", title: str = "Balance Sheet"
) -> list[Word]:
    words = [
        _w(title, 60, 70),
        _w(unit, 60, 105),
        _w("Current", 820, 105),
        _w("Previous", 1010, 105),
    ]
    y = 165
    for label, cur, pri in rows:
        words.append(_w(label, 60, y))
        words.append(_w(cur, 960 - 10 * len(cur), y, 960))
        words.append(_w(pri, 1150 - 10 * len(pri), y, 1150))
        y += 36
    return words


BS_ROWS = [
    ("Equity Share Capital", "100.00", "90.00"),
    ("Other Equity", "400.00", "380.00"),
    ("Total Equity", "500.00", "470.00"),
]


def test_rows_columns_and_labels() -> None:
    t = [r for r in build_table(_page(BS_ROWS), W) if r.columns]
    assert [r.label for r in t] == [r[0] for r in BS_ROWS]
    assert [r.columns[0].text for r in t] == ["100.00", "400.00", "500.00"]
    assert [r.columns[1].text for r in t] == ["90.00", "380.00", "470.00"]


def test_skewed_baselines_still_group_into_rows() -> None:
    words = [
        _w("Other Equity", 60, 200),
        _w("400.00", 900, 207),
        _w("Total Equity", 60, 240),
        _w("500.00", 900, 233),
    ]
    assert len(group_rows(words)) == 2


def test_digit_confusion_repair_and_negatives() -> None:
    c = to_cell(_w("1,2O4.5O", 0, 0))
    assert c is not None and c.text == "1,204.50" and c.repaired
    n = to_cell(_w("(1,234.00)", 0, 0))
    assert n is not None and n.parsed.negative
    assert to_cell(_w("Other Equity", 0, 0)) is None


@pytest.mark.parametrize(
    "label,code",
    [
        ("Shareholders' Funds", "total_equity"),
        ("Net Sales", "revenue_from_operations"),
        ("Sundry Creditors", "trade_payables"),
        ("Profit after tax", "pat"),
    ],
)
def test_label_mapping(label: str, code: str) -> None:
    pl_or_bs = "PL" if code in ("revenue_from_operations", "pat") else "BS"
    assert map_label(label, pl_or_bs)[0] == code
    assert map_label("Zzzzz Qqqq", "BS")[0] is None


def test_unit_missing_forces_review_and_is_never_assumed_rupees() -> None:
    ext = extract_statement(_page(BS_ROWS, unit="(Statement)"), W, H, "DOC-1")
    assert ext.unit is None and "unit_header_not_found" in ext.doc_flags
    results = validate_statement(ext)
    assert all(hard_blocked(ext, f) for f in ext.fields.values())
    assert evaluate_identities(ext)  # identities still evaluated for diagnostics
    assert results


def test_identity_holds_means_no_validation_flags() -> None:
    ext = extract_statement(_page(BS_ROWS), W, H, "DOC-1")
    assert ext.unit == UnitScale.LAKHS
    validate_statement(ext)
    assert all(not f.validations for f in ext.fields.values())


def test_ambiguous_single_digit_error_is_flagged_not_guessed() -> None:
    rows = [
        ("Equity Share Capital", "100.00", "90.00"),
        ("Other Equity", "400.00", "380.00"),
        ("Total Equity", "590.00", "470.00"),
    ]  # 500.00 misread; three cells could each explain it
    ext = extract_statement(_page(rows), W, H, "DOC-1")
    results = validate_statement(ext)
    assert any(r.status == "violated" for r in results)
    assert all(f.auto_corrected_from is None for f in ext.fields.values()), (
        "must not guess between candidates"
    )
    assert all(hard_blocked(ext, f) for f in ext.fields.values())


def test_unique_repair_is_suggested_but_never_straight_through() -> None:
    rows = [
        ("Equity Share Capital", "100.00", "90.00"),
        ("Other Equity", "400.00", "380.00"),
        ("Total Equity", "5.00", "470.00"),
    ]  # dropped digits: only the total can explain it
    ext = extract_statement(_page(rows), W, H, "DOC-1")
    validate_statement(ext)
    f = ext.fields["total_equity"]
    assert f.auto_corrected_from is not None
    assert f.value_paise == 500 * 100_000 * 100  # 500.00 lakh = 5,00,00,000 rupees in paise
    assert f.validations and hard_blocked(ext, f), "repairs go to a human, never straight through"


def test_gross_error_is_flagged_not_silently_repaired() -> None:
    rows = [
        ("Equity Share Capital", "100.00", "90.00"),
        ("Other Equity", "400.00", "380.00"),
        ("Total Equity", "987.00", "470.00"),
    ]
    ext = extract_statement(_page(rows), W, H, "DOC-1")
    results = validate_statement(ext)
    assert any(r.status == "violated" for r in results)
    assert all(f.auto_corrected_from is None for f in ext.fields.values())
    assert all(
        hard_blocked(ext, f)
        for f in ext.fields.values()
        if f.code in ("total_equity", "share_capital", "other_equity")
    )


def test_wilson_and_cer() -> None:
    p, lo, hi = wilson(100, 100)
    assert p == 1.0 and 0.95 < lo < 1.0 and hi == pytest.approx(1.0)
    assert wilson(0, 0)[0] != wilson(0, 0)[0]  # NaN for empty
    assert cer("1,234.50", "1,234.50") == 0 and cer("1,234.5", "1,234.50") == pytest.approx(1 / 8)


def test_calibrator_and_ece() -> None:
    rng = np.random.default_rng(0)
    conf = rng.uniform(0.5, 1.0, 4000)
    correct = (rng.uniform(0, 1, 4000) < conf**3).astype(float)  # over-confident raw scores
    x = np.column_stack(
        [
            conf,
            np.ones(4000),
            np.zeros(4000),
            np.ones(4000),
            np.zeros(4000),
            np.zeros(4000),
            np.zeros(4000),
        ]
    )
    cal = Calibrator()
    cal.fit(x[:2000], correct[:2000])
    assert ece(cal.predict(x[2000:]), correct[2000:]) < ece(conf[2000:], correct[2000:])
    t = cal.choose_threshold(x[:2000], correct[:2000], np.zeros(2000, dtype=bool))
    sel = cal.predict(x[2000:]) >= t
    assert sel.sum() == 0 or correct[2000:][sel].mean() > correct[2000:].mean()


@pytest.mark.slow
def test_renderer_ground_truth_is_exact_and_levels_differ() -> None:
    rng = random.Random(1)
    ob = make_obligor(rng, 1, "healthy")
    h = generate_history(rng, ob.obligor.obligor_id, "healthy")
    clean = render_statement(random.Random(9), h.quarters[6], h.quarters[5], "BS", "clean")
    severe = render_statement(random.Random(9), h.quarters[6], h.quarters[5], "BS", "severe")
    assert len(clean.fields) == 30 and {f.code for f in clean.fields} >= {
        "total_assets",
        "share_capital",
    }
    assert all(f.value_paise == h.quarters[6].lines[f.code] * 100_000 for f in clean.fields)
    a, b = (
        np.asarray(clean.image.convert("L"), dtype=float),
        np.asarray(severe.image.convert("L"), dtype=float),
    )
    assert np.abs(a - b).mean() > 5


def test_word_level_engine_output_finds_the_unit_and_binds_columns() -> None:
    """Engines that return one box per token ("(in", "lakhs)", "30", "June", "2025") must work as well
    as engines that return a box per line: the unit sits past the 14th token and the year in the
    title must not be mistaken for the first value row."""
    words = [
        _w("Saffron", 60, 40), _w("Synthetic", 140, 40), _w("Textiles", 240, 40), _w("Pvt", 330, 40),
        _w("Ltd", 380, 40), _w("Balance", 60, 70), _w("Sheet", 150, 70), _w("for", 220, 70),
        _w("the", 260, 70), _w("quarter", 300, 70), _w("ended", 400, 70), _w("30", 480, 70),
        _w("June", 520, 70), _w("2025", 580, 70), _w("(in", 60, 105), _w("lakhs)", 110, 105),
        _w("Current", 820, 105), _w("Previous", 1010, 105),
    ]  # fmt: skip
    y = 165
    for label, cur, pri in BS_ROWS:
        words += [
            _w(label, 60, y),
            _w(cur, 960 - 10 * len(cur), y, 960),
            _w(pri, 1150 - 10 * len(pri), y, 1150),
        ]
        y += 36
    ext = extract_statement(words, W, H, "DOC-1")
    assert ext.unit == UnitScale.LAKHS and "column_binding_uncertain" not in ext.doc_flags
    validate_statement(ext)
    assert ext.fields["total_equity"].value_paise == 500 * 100_000 * 100
    assert not ext.fields["total_equity"].validations
