"""Outsourced OCR: contract terms, reply validation, fail-closed behaviour, pipeline hand-off."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image
from platform_core.clock import FixedClock

from ocr.pipeline.engine import Word
from ocr.pipeline.extract import extract_statement
from ocr.pipeline.validate import validate_statement
from ocr.pipeline.vendor import (
    VendorContractViolation,
    VendorOcrEngine,
    VendorPolicy,
    VendorPolicyViolation,
    VendorUnavailable,
)

W, H = 1240, 900
POLICY = VendorPolicy(
    vendor_id="acme-ocr",
    region="IN",
    zero_retention=True,
    contract_ref="OUTSRC-2026-014",
    confidence_scale=100.0,
    sub_processors_disclosed=True,
)


def _page_words() -> list[Word]:
    def w(text: str, x0: float, y: float, x1: float | None = None) -> Word:
        return Word(text, x0, y, x1 if x1 is not None else x0 + 10 * len(text), y + 24, 0.99)

    rows = [
        ("Equity Share Capital", "100.00", "90.00"),
        ("Other Equity", "400.00", "380.00"),
        ("Total Equity", "500.00", "470.00"),
    ]
    out = [
        w("Balance Sheet", 60, 70),
        w("(in lakhs)", 60, 105),
        w("Current", 820, 105),
        w("Previous", 1010, 105),
    ]
    y = 165
    for label, cur, pri in rows:
        out += [
            w(label, 60, y),
            w(cur, 960 - 10 * len(cur), y, 960),
            w(pri, 1150 - 10 * len(pri), y, 1150),
        ]
        y += 36
    return out


class FakeTransport:
    """Behaves like a vendor API: polygons, confidences on a 0-100 scale."""

    def __init__(
        self, words: list[Word] | None = None, reply: Any = None, fail: Exception | None = None
    ) -> None:
        self.words, self.reply, self.fail, self.calls = words, reply, fail, 0

    def post(self, payload: bytes, *, timeout_s: float) -> dict[str, Any]:
        self.calls += 1
        assert payload[:4] == b"\x89PNG" and timeout_s > 0
        if self.fail:
            raise self.fail
        if self.reply is not None:
            return self.reply  # type: ignore[no-any-return]
        return {
            "words": [
                {
                    "text": w.text,
                    "box": [[w.x0, w.y0], [w.x1, w.y0], [w.x1, w.y1], [w.x0, w.y1]],
                    "confidence": w.conf * 100,
                }
                for w in self.words or []
            ]
        }


def _engine(transport: FakeTransport, policy: VendorPolicy = POLICY):  # type: ignore[no-untyped-def]
    log: list[dict[str, Any]] = []
    clock = FixedClock(datetime(2026, 10, 21, tzinfo=UTC))
    return VendorOcrEngine(transport, policy, clock, log.append), log


IMG = Image.new("RGB", (W, H), "white")


def test_vendor_words_flow_through_the_unchanged_pipeline() -> None:
    eng, log = _engine(FakeTransport(_page_words()))
    words = eng.recognise(IMG)
    assert eng.name == "vendor:acme-ocr" and 0.0 <= words[0].conf <= 1.0
    ext = extract_statement(words, W, H, "DOC-1")
    validate_statement(ext)
    assert ext.fields["total_equity"].value_paise == 500 * 100_000 * 100
    assert log[0]["outcome"] == "ok" and "image_sha256" in log[0]


def test_audit_log_never_contains_the_image_or_text() -> None:
    eng, log = _engine(FakeTransport(_page_words()))
    eng.recognise(IMG)
    blob = repr(log)
    assert "Equity" not in blob and "PNG" not in blob
    assert set(log[0]) >= {"at", "vendor", "contract", "image_sha256", "bytes", "outcome", "words"}


@pytest.mark.parametrize(
    "change",
    [
        {"region": "US"},
        {"zero_retention": False},
        {"contract_ref": "  "},
        {"sub_processors_disclosed": False},
        {"confidence_scale": 10.0},
    ],
)
def test_policy_violations_stop_the_engine_before_any_call(change: dict[str, Any]) -> None:
    t = FakeTransport(_page_words())
    with pytest.raises(VendorPolicyViolation):
        _engine(t, replace(POLICY, **change))
    assert t.calls == 0


def test_outage_fails_closed_and_is_logged() -> None:
    eng, log = _engine(FakeTransport(fail=TimeoutError("slow")))
    with pytest.raises(VendorUnavailable):
        eng.recognise(IMG)
    assert log[0]["outcome"] == "unavailable"


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"words": "nope"},
        {"words": [{"text": "x", "confidence": 90}]},  # no box
        {"words": [{"text": "x", "box": [0, 0, 5, 5]}]},  # no confidence
        {"words": [{"text": "x", "box": [0, 0, 5, 5], "confidence": 150}]},  # > 100
        {"words": [{"text": "x", "box": [0, 0, 5, 5], "confidence": float("nan")}]},
        {"words": [{"text": "x", "box": [10, 10, 5, 5], "confidence": 90}]},  # inverted
        {"words": [{"text": "x", "box": [0, 0, 5000, 5], "confidence": 90}]},  # off page
        {"words": [{"text": 5, "box": [0, 0, 5, 5], "confidence": 90}]},
        {"words": [{"text": "x", "box": "garbage", "confidence": 90}]},
        ["not", "a", "dict"],
    ],
)
def test_malformed_replies_are_rejected_not_repaired(reply: Any) -> None:
    eng, log = _engine(FakeTransport(reply=reply))
    with pytest.raises(VendorContractViolation):
        eng.recognise(IMG)
    assert log[0]["outcome"] == "contract_violation"


def test_blank_words_are_dropped_and_dict_boxes_accepted() -> None:
    reply = {
        "words": [
            {"text": "  ", "box": [0, 0, 5, 5], "confidence": 90},
            {"text": "Net worth", "box": {"x0": 1, "y0": 2, "x1": 50, "y1": 20}, "confidence": 91},
        ]
    }
    eng, _ = _engine(FakeTransport(reply=reply))
    words = eng.recognise(IMG)
    assert [w.text for w in words] == ["Net worth"] and words[0].conf == pytest.approx(0.91)


def test_oversized_images_are_not_sent() -> None:
    t = FakeTransport(_page_words())
    eng, _ = _engine(t, replace(POLICY, max_image_bytes=100))
    with pytest.raises(VendorPolicyViolation):
        eng.recognise(IMG)
    assert t.calls == 0
