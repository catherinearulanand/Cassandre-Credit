"""P2-14: hostile or buggy vendor replies are rejected, audited and never crash the call."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image
from platform_core.clock import FixedClock

from ocr.pipeline.vendor import (
    VendorContractViolation,
    VendorOcrEngine,
    VendorPolicy,
    VendorPolicyViolation,
    VendorUnavailable,
)

POLICY = VendorPolicy(
    "v", "IN", True, "CTR-1", confidence_scale=100.0, sub_processors_disclosed=True
)
IMG = Image.new("RGB", (1240, 900), "white")


class T:
    def __init__(self, reply: Any = None, fail: Exception | None = None) -> None:
        self.reply, self.fail = reply, fail

    def post(self, payload: bytes, *, timeout_s: float) -> dict[str, Any]:
        if self.fail:
            raise self.fail
        return self.reply  # type: ignore[no-any-return]


def _eng(t: T) -> tuple[VendorOcrEngine, list[dict[str, Any]]]:
    log: list[dict[str, Any]] = []
    return VendorOcrEngine(
        t, POLICY, FixedClock(datetime(2026, 10, 21, tzinfo=UTC)), log.append
    ), log


def _w(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"text": "x", "box": [0, 0, 5, 5], "confidence": 90}
    base.update(kw)
    return base


@pytest.mark.parametrize(
    "word",
    [
        _w(confidence=10**400),
        _w(box=[10**400, 0, 1, 1]),
        _w(box="1234"),
        _w(confidence=True),
        _w(confidence="0.5"),
        _w(box=[0, 0, 5]),
        _w(text="y" * 5000),
        _w(box=[[0, 0], [1]]),
    ],
)
def test_hostile_words_raise_a_contract_violation_and_are_audited(word: dict[str, Any]) -> None:
    eng, log = _eng(T({"words": [word]}))
    with pytest.raises(VendorContractViolation):
        eng.recognise(IMG)
    assert log[-1]["outcome"] == "contract_violation"


def test_an_empty_page_is_unreadable_not_blank() -> None:
    eng, log = _eng(T({"words": []}))
    with pytest.raises(VendorContractViolation):
        eng.recognise(IMG)
    assert log[-1]["outcome"] == "contract_violation"


def test_control_and_bidi_characters_are_stripped_from_text() -> None:
    eng, _ = _eng(T({"words": [_w(text="Net" + chr(0x202E) + " worth" + chr(0))]}))
    assert eng.recognise(IMG)[0].text == "Net worth"


def test_transport_text_does_not_leak_through_the_exception_chain() -> None:
    eng, _ = _eng(T(fail=RuntimeError("secret body of the document")))
    with pytest.raises(VendorUnavailable) as e:
        eng.recognise(IMG)
    assert e.value.__cause__ is None and "secret" not in str(e.value)


@pytest.mark.parametrize(
    "change",
    [{"timeout_s": 0}, {"timeout_s": float("inf")}, {"max_words": -1}, {"max_image_bytes": 10**12}],
)
def test_numeric_policy_fields_are_bounded(change: dict[str, Any]) -> None:
    with pytest.raises(VendorPolicyViolation):
        _eng(T({"words": []}))[0].__class__(
            T(),
            replace(POLICY, **change),
            FixedClock(datetime(2026, 10, 21, tzinfo=UTC)),
            lambda e: None,
        )
