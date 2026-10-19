"""Metric helpers: Wilson intervals and string error rates."""

from __future__ import annotations

import math

from rapidfuzz.distance import Levenshtein


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Point estimate and 95% Wilson score interval. Safe for proportions near 0 or 1."""
    if n == 0:
        return (math.nan, math.nan, math.nan)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (p, max(0.0, centre - half), min(1.0, centre + half))


def cer(pred: str, truth: str) -> float:
    if not truth:
        return 0.0 if not pred else 1.0
    return min(1.0, Levenshtein.distance(pred, truth) / len(truth))


def pct(x: float, nd: int = 2) -> str:
    return "n/a" if math.isnan(x) else f"{100 * x:.{nd}f}%"


def interval(successes: int, n: int, nd: int = 2) -> str:
    p, lo, hi = wilson(successes, n)
    return f"{pct(p, nd)} [{pct(lo, nd)}, {pct(hi, nd)}] (n={n})"
