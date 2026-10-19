"""Calibrated confidence and routing (stage 10).

Raw OCR confidence is not a probability of being right. A small logistic model over
independent signals (OCR confidence, label-match score, arithmetic-identity state, character
repair, auto-correction) is fitted on a labelled calibration set and evaluated on a disjoint
test set (expected calibration error before and after). The straight-through threshold is the
lowest one whose precision on the calibration set meets the target; fields below it, with a
failed validation, a repaired value or a missing unit go to the human queue with the image crop.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
from sklearn.linear_model import LogisticRegression

from ocr.pipeline.extract import ExtField, StatementExtraction
from ocr.pipeline.validate import IdentityResult, evaluate_identities, identity_state

FEATURES = ["ocr_conf", "label_score", "id_holds", "id_none", "id_violated", "repaired", "autocorr"]


def features(ext: StatementExtraction, results: list[IdentityResult], ef: ExtField) -> list[float]:
    st = identity_state(ext, results, ef.code)
    return [
        ef.ocr_conf,
        ef.label_score,
        float(st == 1),
        float(st == 0),
        float(st == 2),
        float(ef.repaired_chars),
        float(ef.auto_corrected_from is not None),
    ]


def ece(probs: np.ndarray, correct: np.ndarray, bins: int = 10) -> float:
    if len(probs) == 0:
        return 0.0
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in itertools.pairwise(edges):
        m = (probs >= lo) & ((probs < hi) if hi < 1 else (probs <= hi))
        if m.any():
            total += m.mean() * abs(correct[m].mean() - probs[m].mean())
    return float(total)


@dataclass
class Calibrator:
    model: LogisticRegression | None = None
    threshold: float = 0.99
    target_precision: float = 0.995
    fitted_on: int = 0
    notes: list[str] = field(default_factory=list)

    def fit(self, x: np.ndarray, y: np.ndarray) -> None:
        self.fitted_on = len(y)
        if len(set(y.tolist())) < 2:
            self.notes.append("calibration set has one class; falling back to raw confidence")
            return
        self.model = LogisticRegression(C=2.0, max_iter=1000, class_weight=None)
        self.model.fit(x, y)

    def predict(self, x: np.ndarray) -> np.ndarray:
        if self.model is None:
            return x[:, 0]
        return self.model.predict_proba(x)[:, 1]

    def choose_threshold(
        self, x: np.ndarray, y: np.ndarray, hard_block: np.ndarray, min_support: int = 30
    ) -> float:
        """Lowest threshold t such that, among fields with p>=t that are not hard-blocked,
        precision >= target (with enough support). Higher thresholds trade STP for safety."""
        p = self.predict(x)
        best = 1.0
        for t in np.unique(np.round(p, 4))[::-1]:
            sel = (p >= t) & ~hard_block
            if sel.sum() < min_support:
                continue
            if y[sel].mean() >= self.target_precision:
                best = float(t)
            else:
                break
        self.threshold = best
        return best


LABEL_FLOOR = 0.90


def hard_blocked(
    ext: StatementExtraction, ef: ExtField, results: list[IdentityResult] | None = None
) -> bool:
    """Conditions that send a field to a human no matter how confident the model is.

    Straight-through requires *independent arithmetic corroboration*: the field must sit in at
    least one accounting identity that holds and in none that is violated. A value that no
    identity confirms (because the page is incomplete, or the line is not covered) is reviewed,
    however high its OCR confidence: confidence says the characters are crisp, not that this row
    was paired with the right number.
    """
    if results is None:
        results = evaluate_identities(ext)
    return (
        ext.unit is None
        or any(f.startswith("rescan_recommended") for f in ext.doc_flags)
        or bool(ef.validations)
        or ef.auto_corrected_from is not None
        or bool(ext.duplicates and ef.code in ext.duplicates)
        or ef.label_score < LABEL_FLOOR
        or identity_state(ext, results, ef.code) != 1
    )


def route(
    ext: StatementExtraction,
    ef: ExtField,
    p: float,
    cal: Calibrator,
    results: list[IdentityResult] | None = None,
) -> str:
    blocked = hard_blocked(ext, ef, results)
    return "needs_review" if (blocked or p < cal.threshold) else "auto"
