"""Format and checksum validators.

- GSTIN: 15 chars; char 15 is a mod-36 check character over chars 1-14 (GSTN algorithm).
- LEI: ISO 17442, 20 chars; ISO 7064 MOD 97-10 (whole string as integer mod 97 == 1).
- Aadhaar: 12 digits, Verhoeff checksum. Used only to *detect* for redaction; the platform
  never stores Aadhaar numbers.
"""

from __future__ import annotations

import re

_ALNUM36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# PAN 4th character = holder type: C company, P person, H HUF, F firm, A AOP, T trust,
# B BOI, L local authority, J artificial juridical person, G government.
_PAN_RE = re.compile(r"^[A-Z]{3}[ABCFGHLJPT][A-Z][0-9]{4}[A-Z]$")
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{3}[ABCFGHLJPT][A-Z][0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CIN_RE = re.compile(
    r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}(PLC|PTC|GOI|SGC|FTC|GAP|GAT|NPL|ULL|ULT|OPC)[0-9]{6}$"
)
_LLPIN_RE = re.compile(r"^[A-Z]{3}-[0-9]{4}$")
_LEI_RE = re.compile(r"^[0-9A-Z]{18}[0-9]{2}$")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")

# GST state codes 01-38 plus 97 (other territory) and 99 (centre jurisdiction).
_GST_STATE_CODES = {f"{i:02d}" for i in range(1, 39)} | {"97", "99"}


def is_valid_pan(pan: str) -> bool:
    return bool(_PAN_RE.fullmatch(pan))


def gstin_check_char(first14: str) -> str:
    if len(first14) != 14:
        raise ValueError("GSTIN body must be 14 characters")
    total = 0
    for i, ch in enumerate(first14):
        value = _ALNUM36.index(ch)
        product = value * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _ALNUM36[(36 - total % 36) % 36]


def is_valid_gstin(gstin: str) -> bool:
    if not _GSTIN_RE.fullmatch(gstin):
        return False
    if gstin[:2] not in _GST_STATE_CODES:
        return False
    return gstin_check_char(gstin[:14]) == gstin[14]


def is_valid_cin(cin: str) -> bool:
    if not _CIN_RE.fullmatch(cin):
        return False
    year = int(cin[8:12])
    return 1850 <= year <= 2100


def is_valid_llpin(llpin: str) -> bool:
    return bool(_LLPIN_RE.fullmatch(llpin))


def _lei_numeric(s: str) -> int:
    return int("".join(str(_ALNUM36.index(c)) for c in s))


def lei_check_digits(first18: str) -> str:
    if len(first18) != 18:
        raise ValueError("LEI body must be 18 characters")
    return f"{98 - _lei_numeric(first18 + '00') % 97:02d}"


def is_valid_lei(lei: str) -> bool:
    return bool(_LEI_RE.fullmatch(lei)) and _lei_numeric(lei) % 97 == 1


def is_valid_ifsc(ifsc: str) -> bool:
    return bool(_IFSC_RE.fullmatch(ifsc))


def is_valid_udyam(udyam: str) -> bool:
    return bool(_UDYAM_RE.fullmatch(udyam))


# ---- Verhoeff (Aadhaar) -------------------------------------------------------------
_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def is_valid_aadhaar(value: str) -> bool:
    digits = value.replace(" ", "").replace("-", "")
    return (
        len(digits) == 12 and digits.isdigit() and digits[0] not in "01" and verhoeff_valid(digits)
    )
