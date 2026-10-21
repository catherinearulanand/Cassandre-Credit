"""Outsourced OCR behind the same ``OcrEngine`` interface (ADR 0004).

The vendor is treated as an untrusted, replaceable recogniser. What it may send us is checked
here, what we may send it is limited by contract terms recorded in ``VendorPolicy`` and enforced
before the first call, and every call is logged without the image. If the vendor cannot give
word-level boxes we refuse its output (lineage and the accounting checks need them) and the
document goes to manual spreading. Nothing here decides accuracy: the same validation, routing and
calibration run downstream, and a new vendor must be calibrated and benchmarked on the harness.
"""

from __future__ import annotations

import hashlib
import io
import math
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image
from platform_core.clock import Clock
from platform_core.errors import DomainError

from ocr.pipeline.engine import Word


class VendorUnavailable(DomainError):
    """Outage, timeout or transport error. The caller routes the page to manual spreading."""

    code = "ocr_vendor_unavailable"
    http_status = 503


class VendorContractViolation(DomainError):
    """The vendor reply is unusable (no boxes, impossible geometry, bad confidences)."""

    code = "ocr_vendor_contract_violation"
    http_status = 502


class VendorPolicyViolation(DomainError):
    code = "ocr_vendor_policy_violation"


class VendorTransport(Protocol):
    def post(self, payload: bytes, *, timeout_s: float) -> dict[str, Any]: ...


@dataclass(frozen=True)
class VendorPolicy:
    """Terms the bank holds the vendor to. Checked at construction, not per request."""

    vendor_id: str
    region: str  # where the image is processed
    zero_retention: bool
    contract_ref: str  # outsourcing agreement / DPA reference
    confidence_scale: float = 1.0  # 1.0 if the vendor reports 0..1, 100.0 if 0..100
    timeout_s: float = 20.0
    max_image_bytes: int = 15_000_000
    max_words: int = 20_000
    sub_processors_disclosed: bool = False
    # A non-India service may be used only for synthetic demonstration data. Real borrower data
    # never goes to it; the flag is recorded on every audit entry.
    synthetic_data_only: bool = False

    def check(self) -> None:
        problems = []
        if self.region != "IN" and not self.synthetic_data_only:
            problems.append("processing must be in India (or synthetic_data_only)")
        if not self.zero_retention:
            problems.append("zero retention required")
        if not self.contract_ref.strip():
            problems.append("outsourcing contract reference required")
        if not self.sub_processors_disclosed:
            problems.append("sub-processors must be disclosed")
        if self.confidence_scale not in (1.0, 100.0):
            problems.append("confidence_scale must be 1 or 100")
        if not (0 < self.timeout_s <= 120):
            problems.append("timeout must be between 0 and 120 seconds")
        if not (0 < self.max_words <= 100_000):
            problems.append("max_words must be between 1 and 100000")
        if not (0 < self.max_image_bytes <= 50_000_000):
            problems.append("max_image_bytes must be between 1 and 50 MB")
        if problems:
            raise VendorPolicyViolation("; ".join(problems))


AuditHook = Callable[[dict[str, Any]], None]


def _num(v: Any) -> float:
    """A real JSON number. Booleans, strings and absurd magnitudes are contract violations."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise VendorContractViolation("expected a number")
    try:
        f = float(v)
    except OverflowError as exc:
        raise VendorContractViolation("number out of range") from exc
    if not math.isfinite(f) or abs(f) > 1e9:
        raise VendorContractViolation("number out of range")
    return f


def _box(raw: Any) -> tuple[float, float, float, float]:
    """Accept [x0,y0,x1,y1], {x0..y1} or a polygon [[x,y],...]. Anything else is a violation."""
    try:
        if isinstance(raw, dict):
            return (_num(raw["x0"]), _num(raw["y0"]), _num(raw["x1"]), _num(raw["y1"]))
        if isinstance(raw, str) or not isinstance(raw, (list, tuple)) or not raw:
            raise VendorContractViolation("word box is missing or malformed")
        if isinstance(raw[0], (list, tuple)):
            xs = [_num(p[0]) for p in raw]
            ys = [_num(p[1]) for p in raw]
            return (min(xs), min(ys), max(xs), max(ys))
        if len(raw) != 4:
            raise VendorContractViolation("word box is missing or malformed")
        x0, y0, x1, y1 = (_num(v) for v in raw)
        return (x0, y0, x1, y1)
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise VendorContractViolation("word box is missing or malformed") from exc


class VendorOcrEngine:
    """Calls an outsourced OCR service and returns validated ``Word`` boxes."""

    def __init__(
        self,
        transport: VendorTransport,
        policy: VendorPolicy,
        clock: Clock,
        audit: AuditHook,
        *,
        version: str = "vendor-api-v1",
    ) -> None:
        policy.check()
        self._t, self._p, self._clock, self._audit = transport, policy, clock, audit
        self.name = f"vendor-{policy.vendor_id}"
        self.version = version

    def recognise(self, image: Image.Image) -> list[Word]:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        payload = buf.getvalue()
        if len(payload) > self._p.max_image_bytes:
            raise VendorPolicyViolation("image exceeds the contracted size")
        entry: dict[str, Any] = {
            "at": self._clock.now().isoformat(),
            "vendor": self._p.vendor_id,
            "region": self._p.region,
            "synthetic_only": self._p.synthetic_data_only,
            "contract": self._p.contract_ref,
            "image_sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "outcome": "pending",
        }
        try:
            reply = self._t.post(payload, timeout_s=self._p.timeout_s)
        except Exception as exc:  # noqa: BLE001 - any transport failure fails closed
            entry["outcome"] = "unavailable"
            self._audit(entry)
            raise VendorUnavailable(f"OCR vendor call failed: {type(exc).__name__}") from None
        try:
            words = self._parse(reply, image.width, image.height)
        except VendorContractViolation:
            entry["outcome"] = "contract_violation"
            self._audit(entry)
            raise
        except Exception as exc:
            entry["outcome"] = "contract_violation"
            self._audit(entry)
            raise VendorContractViolation("reply could not be parsed") from exc
        entry["outcome"] = "ok"
        entry["words"] = len(words)
        self._audit(entry)
        return words

    def _parse(self, reply: dict[str, Any], width: int, height: int) -> list[Word]:
        raw_words = reply.get("words") if isinstance(reply, dict) else None
        if not isinstance(raw_words, list):
            raise VendorContractViolation("reply has no word-level output")
        if len(raw_words) > self._p.max_words:
            raise VendorContractViolation("implausible number of words")
        if not raw_words:
            raise VendorContractViolation("empty reply for a page: treated as unreadable")
        slack = 0.02
        out: list[Word] = []
        for rw in raw_words:
            if not isinstance(rw, dict) or not isinstance(rw.get("text"), str):
                raise VendorContractViolation("word without text")
            text = "".join(
                c for c in rw["text"] if unicodedata.category(c) not in ("Cc", "Cf")
            ).strip()
            if len(rw["text"]) > 200:
                raise VendorContractViolation("implausibly long word")
            if not text:
                continue
            if "confidence" not in rw:
                raise VendorContractViolation("word without a numeric confidence")
            conf = _num(rw["confidence"]) / self._p.confidence_scale
            x0, y0, x1, y1 = _box(rw.get("box"))
            if not all(math.isfinite(v) for v in (x0, y0, x1, y1, conf)):
                raise VendorContractViolation("non-finite geometry or confidence")
            if not (0.0 <= conf <= 1.0):
                raise VendorContractViolation("confidence outside [0, 1] after scaling")
            if x1 <= x0 or y1 <= y0:
                raise VendorContractViolation("empty or inverted box")
            if (
                x0 < -slack * width
                or y0 < -slack * height
                or x1 > (1 + slack) * width
                or y1 > (1 + slack) * height
            ):
                raise VendorContractViolation("box lies outside the page")
            out.append(Word(text, x0, y0, x1, y1, conf))
        return out
