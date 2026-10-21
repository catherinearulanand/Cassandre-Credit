"""PaddleOCR adapters: hosted-API mapping and safety, local worker (skipped if not installed)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from PIL import Image, ImageDraw
from platform_core.clock import FixedClock

from ocr.pipeline.paddle_engine import (
    PaddleCloudTransport,
    cloud_engine,
    local_available,
    local_engine,
    make_engine,
    parse_cloud_result,
)
from ocr.pipeline.vendor import (
    VendorContractViolation,
    VendorPolicy,
    VendorPolicyViolation,
    VendorUnavailable,
)

CLOCK = FixedClock(datetime(2026, 10, 21, tzinfo=UTC))
GOOD = {
    "result": {
        "ocrResults": [
            {
                "prunedResult": {
                    "rec_texts": ["Total", "500.00"],
                    "rec_scores": [0.99, 0.98],
                    "rec_polys": [
                        [[0, 0], [40, 0], [40, 10], [0, 10]],
                        [[60, 0], [100, 0], [100, 10], [60, 10]],
                    ],
                }
            }
        ]
    }
}
IMG = Image.new("RGB", (200, 50), "white")


def _transport(handler: Any, token: str = "sekret-token") -> PaddleCloudTransport:
    return PaddleCloudTransport(
        url="https://ocr.example.test/ocr",
        token=token,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_cloud_result_mapping_and_unknown_shapes() -> None:
    assert [w["text"] for w in parse_cloud_result(GOOD)["words"]] == ["Total", "500.00"]
    for bad in (
        {},
        {"result": {}},
        {"result": {"ocrResults": [{"prunedResult": {"rec_texts": ["a"], "rec_scores": []}}]}},
        None,
        "x",
    ):
        assert parse_cloud_result(bad)["words"] is None


def test_cloud_engine_reads_through_the_validator_and_sends_the_token_only_as_a_header() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.content.decode()
        return httpx.Response(200, json=GOOD)

    import os

    os.environ["PADDLEOCR_API_URL"], os.environ["PADDLEOCR_API_TOKEN"] = (
        "https://ocr.example.test/ocr",
        "sekret-token",
    )
    try:
        eng = cloud_engine(CLOCK, lambda e: None)
    finally:
        del os.environ["PADDLEOCR_API_URL"], os.environ["PADDLEOCR_API_TOKEN"]
    eng._t = _transport(handler)  # type: ignore[attr-defined]  # swap the network for a mock
    words = eng.recognise(IMG)
    assert [w.text for w in words] == ["Total", "500.00"]
    assert seen["auth"] == "token sekret-token" and "sekret" not in seen["body"]


def test_unknown_response_shape_is_a_contract_violation_not_a_guess() -> None:
    from ocr.pipeline.vendor import VendorOcrEngine

    policy = VendorPolicy(
        "paddleocr-cloud",
        "CN",
        True,
        "DEMO",
        sub_processors_disclosed=True,
        synthetic_data_only=True,
    )
    e = VendorOcrEngine(
        _transport(lambda r: httpx.Response(200, json={"surprise": 1})),
        policy,
        CLOCK,
        lambda x: None,
    )
    with pytest.raises(VendorContractViolation):
        e.recognise(IMG)


def test_http_errors_become_unavailable_and_the_token_never_appears_in_messages() -> None:
    from ocr.pipeline.vendor import VendorOcrEngine

    policy = VendorPolicy(
        "paddleocr-cloud",
        "CN",
        True,
        "DEMO",
        sub_processors_disclosed=True,
        synthetic_data_only=True,
    )
    e = VendorOcrEngine(
        _transport(lambda r: httpx.Response(401, json={"error": "bad key sekret-token"})),
        policy,
        CLOCK,
        lambda x: None,
    )
    with pytest.raises(VendorUnavailable) as exc:
        e.recognise(IMG)
    assert "sekret" not in str(exc.value) and exc.value.__cause__ is None


def test_a_non_india_service_is_allowed_only_for_synthetic_data() -> None:
    base: dict[str, Any] = dict(
        vendor_id="x",
        region="CN",
        zero_retention=True,
        contract_ref="C",
        sub_processors_disclosed=True,
    )
    with pytest.raises(VendorPolicyViolation):
        VendorPolicy(**base).check()
    VendorPolicy(**base, synthetic_data_only=True).check()


def test_transport_configuration_is_validated() -> None:
    with pytest.raises(ValueError):
        PaddleCloudTransport(url="http://insecure.example/ocr", token="t")
    with pytest.raises(ValueError):
        PaddleCloudTransport(url="https://ok.example/ocr", token="")


def test_make_engine_fails_clearly_when_nothing_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADDLE_PYTHON", "Z:/does/not/exist/python.exe")
    monkeypatch.delenv("PADDLEOCR_API_URL", raising=False)
    monkeypatch.delenv("PADDLEOCR_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        make_engine(CLOCK, lambda e: None)


@pytest.mark.slow
@pytest.mark.skipif(not local_available(), reason="isolated .venv-paddle not installed")
def test_local_paddleocr_reads_separate_cells() -> None:
    img = Image.new("RGB", (900, 160), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 60), "Total Equity", fill="black")
    d.text((500, 60), "500.00", fill="black")
    log: list[dict[str, Any]] = []
    eng = local_engine(CLOCK, log.append)
    try:
        words = eng.recognise(img)
    finally:
        eng.close()  # type: ignore[attr-defined]
    assert [w.text for w in words] == ["Total", "Equity", "500.00"]
    assert log[0]["outcome"] == "ok" and log[0]["region"] == "IN" and not log[0]["synthetic_only"]
