"""Text-recognition engines behind one interface (Build Guide §9.2 stage 5).

Engines are swappable by configuration: the in-house/open-source engine here, a commercial
India-hosted engine as fallback and benchmark, a handwriting or Indic-script model for those
document families. Every engine returns word-level boxes with confidences so lineage
(page, bbox) is available regardless of vendor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float  # pixels
    conf: float
    # original (pre-deskew) box, kept so citations point at the real pixels
    orig: tuple[float, float, float, float] | None = None

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


class OcrEngine(Protocol):
    name: str
    version: str

    def recognise(self, image: Image.Image) -> list[Word]: ...


class RapidOcrEngine:
    """PP-OCR family models (detection + recognition) served through ONNX Runtime on CPU."""

    name = "rapidocr-onnx"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()
        try:
            import importlib.metadata as md

            self.version = md.version("rapidocr-onnxruntime")
        except Exception:  # noqa: BLE001 - version is informational
            self.version = "unknown"

    def recognise(self, image: Image.Image) -> list[Word]:
        result, _ = self._ocr(np.asarray(image.convert("RGB")))
        words: list[Word] = []
        for box, text, conf in result or []:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            words.append(Word(str(text), min(xs), min(ys), max(xs), max(ys), float(conf)))
        return words
