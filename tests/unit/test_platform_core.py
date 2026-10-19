from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from platform_core.bitemporal import BitemporalStore
from platform_core.canonical import canonical_json, content_hash
from platform_core.clock import FixedClock
from platform_core.ids import new_id, parse_id
from platform_core.money import Money, format_indian_number


def test_money_rejects_float() -> None:
    with pytest.raises(Exception):
        Money(amount_paise=1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money.from_rupees(1.5)  # type: ignore[arg-type]


def test_money_crore_lakh() -> None:
    assert Money.from_crore(1).amount_paise == 1_00_00_000_00
    assert Money.from_lakh("2.5").amount_paise == 2_50_000_00


def test_indian_grouping() -> None:
    assert format_indian_number(Decimal("12345678.9")) == "1,23,45,678.90"
    assert format_indian_number(Decimal("-1000")) == "-1,000.00"
    assert format_indian_number(Decimal("999")) == "999.00"


@given(
    st.integers(min_value=-(10**15), max_value=10**15),
    st.integers(min_value=-(10**15), max_value=10**15),
)
def test_money_add_sub_roundtrip(a: int, b: int) -> None:
    x, y = Money(amount_paise=a), Money(amount_paise=b)
    assert ((x + y) - y) == x


@given(st.integers(min_value=0, max_value=10**15))
def test_money_rupee_roundtrip_lossless(p: int) -> None:
    m = Money(amount_paise=p)
    assert Money.from_rupees(m.rupees) == m


def test_currency_mismatch() -> None:
    with pytest.raises(ValueError):
        Money(amount_paise=1) + Money(amount_paise=1, currency="USD")


def test_ids() -> None:
    i = new_id("CTS")
    assert parse_id(i)[0] == "CTS"
    with pytest.raises(ValueError):
        new_id("XXX")


def test_canonical_stable_and_float_free() -> None:
    a = {"b": Decimal("1.50"), "a": [1, 2], "d": date(2026, 1, 1)}
    b = {"d": date(2026, 1, 1), "a": [1, 2], "b": Decimal("1.50")}
    assert canonical_json(a) == canonical_json(b)
    assert content_hash(a) == content_hash(b)


def test_bitemporal_correction_and_as_known() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    store: BitemporalStore[int] = BitemporalStore(clock)
    store.assert_fact("dp", 100, date(2026, 1, 1))
    t1 = clock.now()
    clock.advance(days=10)
    # Correction: from 1 Feb the DP is 80 (learned on 11 Jan)
    store.assert_fact("dp", 80, date(2026, 2, 1))
    assert store.get("dp", date(2026, 1, 15)) == 100
    assert store.get("dp", date(2026, 2, 15)) == 80
    # As known on 1 Jan, Feb was still believed to be 100
    assert store.get("dp", date(2026, 2, 15), known_at=t1) == 100
    clock.advance(days=1)
    # Retroactive correction of January only
    store.assert_fact("dp", 90, date(2026, 1, 10), date(2026, 1, 20))
    assert store.get("dp", date(2026, 1, 5)) == 100
    assert store.get("dp", date(2026, 1, 15)) == 90
    assert store.get("dp", date(2026, 1, 25)) == 100
    assert store.get("dp", date(2026, 2, 5)) == 80


@given(
    st.lists(
        st.tuples(st.integers(0, 60), st.integers(1, 30), st.integers(0, 999)),
        min_size=1,
        max_size=12,
    )
)
def test_bitemporal_never_ambiguous(ops: list[tuple[int, int, int]]) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    store: BitemporalStore[int] = BitemporalStore(clock)
    base = date(2026, 1, 1).toordinal()
    for start, length, value in ops:
        clock.advance(seconds=1)
        store.assert_fact(
            "k", value, date.fromordinal(base + start), date.fromordinal(base + start + length)
        )
    for d in range(0, 95):
        store.get("k", date.fromordinal(base + d))  # raises if two current beliefs overlap
    last_start, last_len, last_val = ops[-1]
    assert store.get("k", date.fromordinal(base + last_start)) == last_val
