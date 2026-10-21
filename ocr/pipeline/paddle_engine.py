"""PaddleOCR behind the vendor-engine port, in two deployment modes.

* **Local worker** (``PaddleWorkerTransport``): the open-source PP-OCR models run in an isolated
  interpreter (``.venv-paddle``) that this process talks to over a pipe. Images never leave the machine,
  so this mode satisfies the India-residency and zero-retention terms and is the one to use with real
  borrower documents. It is *really connected* when the isolated environment exists.
* **Hosted API** (``PaddleCloudTransport``): the PaddleOCR service reached with an API key you supply.
  The key is read from the environment and is never logged or stored. The service is operated outside
  India, so ``VendorPolicy`` allows it for **synthetic data only** (``synthetic_data_only=True``). The
  request and response shapes below follow the PaddleX serving convention and have **not** been verified
  against a live endpoint: until a key is available, any shape that does not match is rejected as a
  contract violation and the page goes to manual spreading.

Both feed the same validator (``VendorOcrEngine``), so the downstream identity checks, routing and
calibration are unchanged.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import struct
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from platform_core.clock import Clock

from ocr.pipeline.vendor import AuditHook, VendorOcrEngine, VendorPolicy

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("paddle_worker.py")
DEFAULT_PADDLE_PYTHON = ROOT / ".venv-paddle" / "Scripts" / "python.exe"

LOCAL_POLICY = VendorPolicy(
    vendor_id="paddleocr-local",
    region="IN",
    zero_retention=True,
    contract_ref="SELF-HOSTED-OPEN-SOURCE",  # no third party processes the image
    confidence_scale=1.0,
    timeout_s=120.0,
    sub_processors_disclosed=True,
)


def paddle_python() -> Path:
    return Path(os.environ.get("PADDLE_PYTHON", str(DEFAULT_PADDLE_PYTHON)))


def local_available() -> bool:
    return paddle_python().exists()


class PaddleWorkerTransport:
    """Keeps one PaddleOCR worker process alive and serves one image at a time."""

    def __init__(self, python: Path | None = None, startup_timeout_s: float = 600.0) -> None:
        self._python = python or paddle_python()
        self._startup = startup_timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._lock = threading.Lock()
        self.engine_version = "paddleocr-unknown"

    def _start(self) -> None:
        env = dict(
            os.environ, PYTHONIOENCODING="utf-8", PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True"
        )
        self._proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and script, no shell
            [str(self._python), "-P", str(WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self._lines = queue.Queue()
        proc = self._proc

        def pump() -> None:
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, b""):
                self._lines.put(line)
            self._lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        ready = self._read(self._startup)
        self.engine_version = str(ready.get("engine_version", self.engine_version))

    def _read(self, timeout_s: float) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=timeout_s)
        except queue.Empty as exc:
            self.close()
            raise TimeoutError("PaddleOCR worker timed out") from exc
        if line is None:
            self.close()
            raise ConnectionError("PaddleOCR worker exited")
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("worker reply is not an object")
        return data

    def post(self, payload: bytes, *, timeout_s: float) -> dict[str, Any]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(struct.pack(">I", len(payload)) + payload)
            self._proc.stdin.flush()
            reply = self._read(timeout_s)
        if "error" in reply:
            raise RuntimeError(f"worker error: {reply['error']}")
        return reply

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()


def parse_cloud_result(body: Any) -> dict[str, Any]:
    """Map a PaddleX-style OCR response to ``{"words": [...]}``. Unknown shapes give ``words=None``
    so the validator rejects them; nothing is guessed."""
    try:
        results = body["result"]["ocrResults"]
        words: list[dict[str, Any]] = []
        for page in results:
            pr = page["prunedResult"]
            polys = pr.get("rec_polys") or pr.get("dt_polys")
            for text, score, poly in zip(pr["rec_texts"], pr["rec_scores"], polys, strict=True):
                words.append({"text": text, "box": poly, "confidence": score})
        return {"words": words}
    except (KeyError, TypeError, ValueError, IndexError):
        return {"words": None}


class PaddleCloudTransport:
    """Hosted PaddleOCR API. Configuration comes from the environment; the token is never logged.

    ``PADDLEOCR_API_URL``     full URL of the OCR endpoint (from your account's API page)
    ``PADDLEOCR_API_TOKEN``   the key you were issued
    ``PADDLEOCR_AUTH_SCHEME`` header scheme, default ``token`` (sent as ``Authorization: token <key>``)
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        scheme: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url or os.environ.get("PADDLEOCR_API_URL", "")
        self._token = token or os.environ.get("PADDLEOCR_API_TOKEN", "")
        self._scheme = scheme or os.environ.get("PADDLEOCR_AUTH_SCHEME", "token")
        if not self._url.startswith("https://"):
            raise ValueError("PADDLEOCR_API_URL must be an https URL")
        if not self._token:
            raise ValueError("PADDLEOCR_API_TOKEN is not set")
        self._client = client or httpx.Client()

    def post(self, payload: bytes, *, timeout_s: float) -> dict[str, Any]:
        resp = self._client.post(
            self._url,
            headers={"Authorization": f"{self._scheme} {self._token}"},
            json={"file": base64.b64encode(payload).decode("ascii"), "fileType": 1},
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return parse_cloud_result(resp.json())


def cloud_available() -> bool:
    return bool(os.environ.get("PADDLEOCR_API_URL")) and bool(os.environ.get("PADDLEOCR_API_TOKEN"))


def local_engine(clock: Clock, audit: AuditHook) -> VendorOcrEngine:
    t = PaddleWorkerTransport()
    eng = VendorOcrEngine(t, LOCAL_POLICY, clock, audit, version="paddleocr-local")
    eng.close = t.close  # type: ignore[attr-defined]
    return eng


def cloud_engine(clock: Clock, audit: AuditHook) -> VendorOcrEngine:
    policy = VendorPolicy(
        vendor_id="paddleocr-cloud",
        region="CN",  # operated outside India: synthetic data only
        zero_retention=True,
        contract_ref="DEMO-SYNTHETIC-ONLY",
        confidence_scale=1.0,
        sub_processors_disclosed=True,
        synthetic_data_only=True,
    )
    return VendorOcrEngine(PaddleCloudTransport(), policy, clock, audit, version="paddleocr-cloud")


def make_engine(clock: Clock, audit: AuditHook, prefer: str = "local") -> VendorOcrEngine:
    """local worker if installed, else hosted API if a key is configured, else raise."""
    order: list[tuple[str, Callable[[Clock, AuditHook], VendorOcrEngine], Callable[[], bool]]] = [
        ("local", local_engine, local_available),
        ("cloud", cloud_engine, cloud_available),
    ]
    order.sort(key=lambda x: x[0] != prefer)
    for _, factory, ok in order:
        if ok():
            return factory(clock, audit)
    raise RuntimeError("no PaddleOCR backend: install .venv-paddle or set PADDLEOCR_API_URL/TOKEN")


__all__ = [
    "LOCAL_POLICY", "PaddleCloudTransport", "PaddleWorkerTransport", "cloud_available",
    "local_available", "make_engine", "parse_cloud_result",
]  # fmt: skip
