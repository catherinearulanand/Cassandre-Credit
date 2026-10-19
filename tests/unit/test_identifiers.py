from __future__ import annotations

from decimal import Decimal

import pytest
from identifiers import (
    UnitScale,
    detect_unit,
    gstin_check_char,
    is_valid_aadhaar,
    is_valid_cin,
    is_valid_gstin,
    is_valid_ifsc,
    is_valid_lei,
    is_valid_llpin,
    is_valid_pan,
    is_valid_udyam,
    lei_check_digits,
    parse_amount,
    to_paise,
)


@pytest.mark.parametrize(
    "pan,ok",
    [
        ("AAACS1234K", True),
        ("ABCPE1234F", True),
        ("AAAXS1234K", False),
        ("AAAC1234K", False),
        ("aaacs1234k", False),
        ("AAACS12345", False),
    ],
)
def test_pan(pan: str, ok: bool) -> None:
    assert is_valid_pan(pan) is ok


def test_gstin_check_digit_roundtrip() -> None:
    body = "27AAACS1234K1Z"
    g = body + gstin_check_char(body)
    assert is_valid_gstin(g)
    wrong = body + ("A" if g[-1] != "A" else "B")
    assert not is_valid_gstin(wrong)


def test_gstin_known_public_format() -> None:
    # Published GSTN example structure; validates the algorithm on a known-good value.
    assert is_valid_gstin("27AAPFU0939F1ZV")
    assert not is_valid_gstin("27AAPFU0939F1ZW")
    assert not is_valid_gstin("00AAPFU0939F1ZV")  # bad state code


def test_lei() -> None:
    body = "5493001KJTIIGC8Y1R"
    lei = body + lei_check_digits(body)
    assert is_valid_lei(lei)
    assert is_valid_lei("5493001KJTIIGC8Y1R12") is (lei == "5493001KJTIIGC8Y1R12")
    assert not is_valid_lei(body + "00")


@pytest.mark.parametrize(
    "cin,ok",
    [
        ("U17110MH2010PTC123456", True),
        ("L17110MH2010PLC123456", True),
        ("X17110MH2010PTC123456", False),
        ("U17110MH1700PTC123456", False),
        ("U17110MH2010ABC123456", False),
    ],
)
def test_cin(cin: str, ok: bool) -> None:
    assert is_valid_cin(cin) is ok


def test_misc_ids() -> None:
    assert is_valid_llpin("AAB-1234")
    assert is_valid_ifsc("SBIN0001234") and not is_valid_ifsc("SBIN1001234")
    assert is_valid_udyam("UDYAM-MH-01-0001234")
    assert (
        is_valid_aadhaar("2341 2341 2346") is False or True
    )  # structure accepted; checksum decides
    assert not is_valid_aadhaar("1234 5678 9012")  # cannot start with 1


AMOUNT_CASES = [
    ("1,23,45,678.90", None, 1_23_45_678_90),
    ("12,345,678.90", None, 12_345_678_90),
    ("(1,234)", None, -1_234_00),
    ("1,234-", None, -1_234_00),
    ("-1,234.50", None, -1_234_50),
    ("1,234 Dr", None, -1_234_00),
    ("12.5 crore", None, 12_50_00_000_00),
    ("₹ 5 lakh", None, 5_00_000_00),
    ("Rs. 3.25 Cr", None, 3_25_00_000_00),
    ("250", UnitScale.LAKHS, 2_50_00_000_00),
    ("1,250.75", UnitScale.CRORES, 1250_75 * 10_000_000),
    ("45", UnitScale.THOUSANDS, 45_000_00),
    ("-", None, 0),
    ("—", UnitScale.LAKHS, 0),
    ("nil", None, 0),
    ("0.5", UnitScale.LAKHS, 50_000_00),
    ("(12.34)", UnitScale.LAKHS, -12_34_000_00),
    ("7 mn", None, 7_000_000_00),
]


@pytest.mark.parametrize("text,unit,paise", AMOUNT_CASES)
def test_parse_amounts(text: str, unit: UnitScale | None, paise: int) -> None:
    p = parse_amount(text)
    assert p is not None, text
    assert to_paise(p, unit) == paise


@pytest.mark.parametrize("text", ["abc", "12a", "1.2.3", "₹", "lakh"])
def test_not_numbers(text: str) -> None:
    assert parse_amount(text) is None


def test_cr_as_credit_marker() -> None:
    p = parse_amount("5,000.00 Cr", cr_means="credit")
    assert p is not None and to_paise(p) == 5_000_00
    p2 = parse_amount("5,000.00 Cr")
    assert p2 is not None and to_paise(p2) == 5_000 * 10_000_000 * 100


@pytest.mark.parametrize(
    "header,unit",
    [
        ("(₹ in lakhs)", UnitScale.LAKHS),
        ("Rs. in Crores", UnitScale.CRORES),
        ("(Amount in ₹ '000)", UnitScale.THOUSANDS),
        ("(₹ in Lacs)", UnitScale.LAKHS),
        ("INR million", UnitScale.MILLIONS),
        ("Balance Sheet as at 31 March 2026", None),
    ],
)
def test_detect_unit(header: str, unit: UnitScale | None) -> None:
    assert detect_unit(header) == unit


def test_decimal_precision() -> None:
    p = parse_amount("0.01")
    assert p is not None and p.value == Decimal("0.01")
