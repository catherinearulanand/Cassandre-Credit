"""Prefixed ULIDs (``OBL-01J...``). Sortable by creation time, globally unique."""

from __future__ import annotations

import re

from ulid import ULID

PREFIXES = frozenset(
    {
        "OBL",
        "GRP",
        "CTS",
        "FAC",
        "LIM",
        "COL",
        "COV",
        "CND",
        "DOC",
        "CASE",
        "SIG",
        "EVT",
        "SPR",
        "OBG",
        "DRW",
        "APR",
        "RUN",
        "CMD",
        "TST",
        "LTR",
        "RFA",
        "REC",
        "AUD",
        "LLM",
    }
)
_ID_RE = re.compile(r"^(?P<prefix>[A-Z]{2,5})-(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})$")


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    return f"{prefix}-{ULID()}"


def new_event_id() -> str:
    return str(ULID())


def parse_id(value: str) -> tuple[str, str]:
    m = _ID_RE.match(value)
    if not m:
        raise ValueError(f"malformed id {value!r}")
    return m.group("prefix"), m.group("ulid")
