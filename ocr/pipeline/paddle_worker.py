"""PaddleOCR worker. Runs inside the isolated ``.venv-paddle`` interpreter, not the project venv.

Protocol (binary, over stdin/stdout): the parent sends ``4-byte big-endian length`` + PNG bytes; the
worker replies with one JSON line ``{"words": [...], "engine_version": "..."}``. Everything the
libraries print goes to stderr so the protocol channel stays clean. No project code is imported here:
the worker depends only on paddleocr, numpy and Pillow.
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
from typing import Any


def main() -> int:
    out = os.fdopen(os.dup(1), "wb", buffering=0)  # the protocol channel
    os.dup2(2, 1)  # anything printed by libraries now goes to stderr
    sys.stdout = sys.stderr
    inp = sys.stdin.buffer

    import numpy as np
    import paddleocr
    from PIL import Image

    # "mobile" is ~2x faster on CPU; "server" uses the library default (larger, more accurate).
    profile = os.environ.get("PADDLE_OCR_PROFILE", "mobile")
    models = (
        {
            "text_detection_model_name": "PP-OCRv5_mobile_det",
            "text_recognition_model_name": "PP-OCRv5_mobile_rec",
        }
        if profile == "mobile"
        else {}
    )
    ocr = paddleocr.PaddleOCR(
        cpu_threads=os.cpu_count() or 4,
        **models,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,  # oneDNN path fails on CPU for the 3.x static models
    )
    version = f"paddleocr-{getattr(paddleocr, '__version__', 'unknown')}-{profile}"
    out.write((json.dumps({"ready": True, "engine_version": version}) + "\n").encode())

    while True:
        head = inp.read(4)
        if len(head) < 4:
            return 0
        (n,) = struct.unpack(">I", head)
        png = inp.read(n)
        try:
            img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
            res = ocr.predict(img, return_word_box=True)
            words = []
            for r in res:
                texts = r.get("rec_texts") or []
                scores = r.get("rec_scores") or []
                polys = r.get("rec_polys")
                if polys is None:
                    polys = r.get("dt_polys")
                if polys is None:
                    polys = []
                tw, tr = r.get("text_word"), r.get("text_word_region")
                for i, (t, sc, poly) in enumerate(zip(texts, scores, polys, strict=False)):
                    if tw and tr and i < len(tw) and i < len(tr) and len(tw[i]) == len(tr[i]):
                        # word-level cells. The library splits at punctuation ("6,114.41" becomes
                        # "6", ",", "114.41"), so tokens are re-joined until a real space.
                        group: list[tuple[str, Any]] = []
                        for wt, region in [*zip(tw[i], tr[i], strict=True), (" ", None)]:
                            if str(wt).strip():
                                group.append((str(wt), region))
                                continue
                            if group:
                                pts = [pt for _, rg in group for pt in rg]
                                x0 = min(float(x) for x, _ in pts)
                                x1 = max(float(x) for x, _ in pts)
                                y0 = min(float(y) for _, y in pts)
                                y1 = max(float(y) for _, y in pts)
                                words.append(
                                    {
                                        "text": "".join(t for t, _ in group),
                                        "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                                        "confidence": float(sc),
                                    }
                                )
                                group = []
                        continue
                    words.append(
                        {
                            "text": str(t),
                            "box": [[float(x), float(y)] for x, y in np.asarray(poly).tolist()],
                            "confidence": float(sc),
                        }
                    )
            reply = {"words": words, "engine_version": version}
        except Exception as exc:  # noqa: BLE001 - report to the parent, keep serving
            reply = {"error": type(exc).__name__}
        out.write((json.dumps(reply) + "\n").encode())


if __name__ == "__main__":
    raise SystemExit(main())
