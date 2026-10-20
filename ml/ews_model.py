"""EWS stress score (M4) and covenant breach forecast (M6).

Design (Build Guide §10): gradient boosting, point-in-time features, out-of-time validation
(train on older quarters, calibrate on the next, test on the newest), isotonic calibration,
SHAP reason codes, a rules-only baseline for honest lift, and a monitoring snapshot (PSI).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np
import shap
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from testing.synthetic.reference import reference_ratio

from sim.book import N_Q, BookEntry

FEATURES = [
    "dscr",
    "iscr",
    "current_ratio",
    "net_debt_ebitda",
    "pat_margin",
    "receivable_days",
    "dscr_delta",
    "iscr_delta",
    "min_headroom_pct",
    "headroom_delta",
    "open_breaches",
    "util_avg",
    "overdrawn_days",
    "cheque_returns",
    "interest_delay_days",
    "gst_gap",
    "stock_stmt_late_days",
    "dpd_max",
    "overdrawn_days_lag1",
    "cheque_returns_lag1",
    "util_trend",
]
HUMAN = {
    "dscr": "debt service cover is {v:.2f}x",
    "iscr": "interest cover is {v:.2f}x",
    "current_ratio": "current ratio is {v:.2f}",
    "net_debt_ebitda": "net debt / EBITDA is {v:.2f}x",
    "pat_margin": "net margin is {v:.1%}",
    "receivable_days": "receivable days are {v:.0f}",
    "dscr_delta": "DSCR moved {v:+.2f}x vs last quarter",
    "iscr_delta": "interest cover moved {v:+.2f}x vs last quarter",
    "min_headroom_pct": "tightest covenant headroom is {v:.1f}%",
    "headroom_delta": "headroom moved {v:+.1f} pts vs last quarter",
    "open_breaches": "{v:.0f} covenant(s) in breach",
    "util_avg": "average limit utilisation is {v:.0%}",
    "overdrawn_days": "{v:.0f} overdrawn days this quarter",
    "cheque_returns": "{v:.0f} cheque/ECS returns this quarter",
    "interest_delay_days": "interest serviced {v:.0f} days late",
    "gst_gap": "{v:.0f} GST filing gaps",
    "stock_stmt_late_days": "stock statement {v:.0f} days late",
    "dpd_max": "peak DPD {v:.0f}",
    "overdrawn_days_lag1": "{v:.0f} overdrawn days last quarter",
    "cheque_returns_lag1": "{v:.0f} returns last quarter",
    "util_trend": "utilisation trend {v:+.0%}",
}
# EWS indicator thresholds mirror rules/ews/indicators.yaml (the rule layer is the baseline)
RULE_BASELINE = [
    "cheque_returns>=3",
    "overdrawn_days>=15",
    "util_avg>=0.97",
    "stock_stmt_late_days>15",
    "interest_delay_days>30",
    "gst_gap>=2",
    "open_breaches>=1",
]


def _proba(model: Any, x: np.ndarray) -> np.ndarray:
    return np.asarray(model.predict_proba(x))[:, 1]


def _f(x: Any, default: float = 0.0) -> float:
    return default if x is None else float(x)


def build_rows(
    book: list[BookEntry], cov_results: list[dict[tuple[str, int], Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for e, res in zip(book, cov_results, strict=True):
        h = e.hist
        for qi in range(4, N_Q):
            if e.default_quarter is not None and qi > e.default_quarter:
                break  # after default the account is in recovery, not monitoring
            ratios = {
                k: reference_ratio(k, h, qi)
                for k in ("dscr", "iscr", "current_ratio", "net_debt_ebitda")
            }
            prev = {k: reference_ratio(k, h, qi - 1) for k in ("dscr", "iscr")} if qi >= 4 else {}
            q = h.quarters[qi].lines
            rev = max(1, q["revenue_from_operations"])
            heads, breaches = [], 0
            for cov in e.package.cts.covenants:
                r = res.get((cov.covenant_id, qi))
                if r is not None and r.headroom_pct is not None:
                    heads.append(float(r.headroom_pct))
                    breaches += r.result.value == "BREACHED"
            heads_prev = []
            for cov in e.package.cts.covenants:
                r = res.get((cov.covenant_id, qi - 1))
                if r is not None and r.headroom_pct is not None:
                    heads_prev.append(float(r.headroom_pct))
            mh = min(heads) if heads else 50.0
            c, c1 = e.conduct[qi], e.conduct[qi - 1]
            util_prev = e.conduct[qi - 2].util_avg if qi >= 2 else c1.util_avg
            row: dict[str, Any] = {
                "obligor": e.bundle.obligor.obligor_id,
                "q": qi,
                "dscr": _f(ratios["dscr"], 2.0),
                "iscr": _f(ratios["iscr"], 3.0),
                "current_ratio": _f(ratios["current_ratio"], 1.3),
                "net_debt_ebitda": _f(ratios["net_debt_ebitda"], 99.0),
                "pat_margin": q["pat"] / rev,
                "receivable_days": q["trade_receivables"] / rev * 90,
                "dscr_delta": _f(ratios["dscr"], 2.0) - _f(prev.get("dscr"), 2.0),
                "iscr_delta": _f(ratios["iscr"], 3.0) - _f(prev.get("iscr"), 3.0),
                "min_headroom_pct": mh,
                "headroom_delta": mh - (min(heads_prev) if heads_prev else mh),
                "open_breaches": float(breaches),
                "util_avg": c.util_avg,
                "overdrawn_days": c.overdrawn_days,
                "cheque_returns": c.cheque_returns,
                "interest_delay_days": c.interest_delay_days,
                "gst_gap": c.gst_gap,
                "stock_stmt_late_days": c.stock_stmt_late_days,
                "dpd_max": c.dpd_max,
                "overdrawn_days_lag1": c1.overdrawn_days,
                "cheque_returns_lag1": c1.cheque_returns,
                "util_trend": c.util_avg - util_prev,
            }
            nxt = [e.conduct[j].dpd_max for j in (qi + 1, qi + 2) if j < N_Q]
            row["y_ews"] = int(any(d >= 61 for d in nxt)) if len(nxt) == 2 else -1
            if qi + 1 < N_Q:
                nb = [res.get((cv.covenant_id, qi + 1)) for cv in e.package.cts.covenants]
                row["y_breach"] = int(
                    any(r is not None and r.result.value == "BREACHED" for r in nb)
                )
            else:
                row["y_breach"] = -1
            row["rule_hits"] = float(
                (c.cheque_returns >= 3)
                + (c.overdrawn_days >= 15)
                + (c.util_avg >= 0.97)
                + (c.stock_stmt_late_days > 15)
                + (c.interest_delay_days > 30)
                + (c.gst_gap >= 2)
                + (breaches >= 1)
            )
            row["idiosyncratic"] = e.idiosyncratic
            row["default_q"] = e.default_quarter if e.default_quarter is not None else -1
            rows.append(row)
    return rows


def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    tot = 0.0
    for lo, hi in itertools.pairwise(edges):
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        if m.any():
            tot += m.mean() * abs(y[m].mean() - p[m].mean())
    return float(tot)


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    qs = np.quantile(expected, np.linspace(0, 1, bins + 1))
    qs[0], qs[-1] = -np.inf, np.inf
    e = np.histogram(expected, qs)[0] / len(expected)
    a = np.histogram(actual, qs)[0] / len(actual)
    e, a = np.clip(e, 1e-4, None), np.clip(a, 1e-4, None)
    return float(np.sum((a - e) * np.log(a / e)))


@dataclass
class ModelReport:
    name: str
    target: str
    split: dict[str, Any]
    metrics: dict[str, Any]
    baselines: dict[str, Any]
    reasons_sample: list[dict[str, Any]] = field(default_factory=list)
    importance: list[tuple[str, float]] = field(default_factory=list)
    monitoring: dict[str, Any] = field(default_factory=dict)


def train_eval(
    rows: list[dict[str, Any]],
    target: str,
    name: str,
    *,
    train_q: range,
    calib_q: int,
    test_q: range,
    capacity: float = 0.10,
    seed: int = 11,
    test_obligors: set[str] | None = None,
) -> ModelReport:
    """Out-of-time AND (when ``test_obligors`` is given) obligor-disjoint evaluation.

    Train/calibration use only obligors outside ``test_obligors``; the test set is those held-out
    obligors in the newest quarters. The caller sets ``train_q`` so that label horizons of training
    rows end before the calibration quarter (an embargo), so no label overlaps calibration/test."""
    use = [r for r in rows if r[target] >= 0]
    held = test_obligors or set()

    def x(rs: list[dict[str, Any]]) -> np.ndarray:
        return np.array([[r[f] for f in FEATURES] for r in rs], dtype=float)

    def y(rs: list[dict[str, Any]]) -> np.ndarray:
        return np.array([r[target] for r in rs], dtype=int)

    tr = [r for r in use if r["q"] in train_q and r["obligor"] not in held]
    ca = [r for r in use if r["q"] == calib_q and r["obligor"] not in held]
    te = [r for r in use if r["q"] in test_q and (not held or r["obligor"] in held)]
    model = lgb.LGBMClassifier(
        n_estimators=250,
        learning_rate=0.04,
        num_leaves=15,
        min_child_samples=25,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_lambda=2.0,
        random_state=seed,
        verbose=-1,
    )
    model.fit(x(tr), y(tr))

    # Platt scaling on the logit: stable on a few hundred calibration rows, where isotonic
    # regression produces step functions that saturate at 0 and 1
    def logit(a: np.ndarray) -> np.ndarray:
        a = np.clip(a, 1e-4, 1 - 1e-4)
        return np.log(a / (1 - a)).reshape(-1, 1)

    platt = LogisticRegression(C=10.0)
    platt.fit(logit(_proba(model, x(ca))), y(ca))
    p_raw = _proba(model, x(te))
    p = platt.predict_proba(logit(p_raw))[:, 1]
    yt = y(te)
    k = max(1, round(capacity * len(te)))
    order = np.argsort(-p_raw)
    top = np.zeros(len(te), dtype=bool)
    top[order[:k]] = True
    base_rate = float(yt.mean())
    prec = float(yt[top].mean())
    rec = float(yt[top].sum() / max(1, yt.sum()))
    brng = np.random.default_rng(seed)
    boots = []
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(te):
        groups.setdefault(r["obligor"], []).append(i)
    glist = list(groups.values())
    for _ in range(400):  # cluster bootstrap: resample obligors, not rows
        pick = brng.integers(0, len(glist), len(glist))
        idx = np.array([i for g in pick for i in glist[g]])
        if yt[idx].min() != yt[idx].max():
            boots.append(roc_auc_score(yt[idx], p_raw[idx]))
    auc_ci = (
        [float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))]
        if boots
        else [float("nan")] * 2
    )
    from ocr.eval.metrics import wilson

    metrics = {
        "auc_ci95_bootstrap": auc_ci,
        "precision_at_capacity_ci95": list(wilson(int(yt[top].sum()), int(k))[1:]),
        "recall_at_capacity_ci95": list(wilson(int(yt[top].sum()), int(yt.sum()))[1:]),
        "auc": float(roc_auc_score(yt, p_raw)),
        "pr_auc": float(average_precision_score(yt, p_raw)),
        "brier_calibrated": float(brier_score_loss(yt, p)),
        "brier_uncalibrated": float(brier_score_loss(yt, p_raw)),
        "ece_calibrated": ece(p, yt),
        "ece_uncalibrated": ece(p_raw, yt),
        "base_rate": base_rate,
        "alert_capacity": capacity,
        "alerts": int(k),
        "precision_at_capacity": prec,
        "recall_at_capacity": rec,
        "lift_at_capacity": prec / base_rate if base_rate else float("nan"),
        "test_rows": len(te),
        "test_positives": int(yt.sum()),
    }
    # rules-only and naive baselines on the same test rows
    rule = np.array([r["rule_hits"] for r in te])
    dpd = np.array([r["dpd_max"] for r in te], dtype=float)
    persistence = (
        np.array([r["open_breaches"] for r in te], dtype=float)
        if target == "y_breach"
        else np.array([float(r["dpd_max"] >= 61) for r in te])
    )
    baselines = {
        "rules_only_indicator_count_auc": float(roc_auc_score(yt, rule)),
        "current_dpd_only_auc": float(roc_auc_score(yt, dpd)),
        "persistence_auc": float(roc_auc_score(yt, persistence)),
        "random_auc": 0.5,
    }
    if target == "y_breach":
        # how much of the skill is just "already in breach stays in breach"? Score only the
        # rows that are NOT in breach today: those positives are genuinely new breaches.
        fresh = np.array([r["open_breaches"] == 0 for r in te])
        metrics["rows_not_already_in_breach"] = int(fresh.sum())
        metrics["new_breach_positives"] = int(yt[fresh].sum())
        if fresh.sum() > 0 and yt[fresh].min() != yt[fresh].max():
            metrics["auc_on_new_breaches_only"] = float(roc_auc_score(yt[fresh], p_raw[fresh]))
            ps = np.array([r["min_headroom_pct"] for r in te])[fresh]
            metrics["headroom_only_auc_on_new_breaches"] = float(roc_auc_score(yt[fresh], -ps))
    # lead time (EWS only): of obligors that reach SMA-2 in the test window, flagged >= 1 quarter early
    if target == "y_ews":
        by_ob: dict[str, list[tuple[int, bool]]] = {}
        for r, t in zip(te, top, strict=True):
            by_ob.setdefault(r["obligor"], []).append((r["q"], bool(t)))
        defaulters = {
            r["obligor"]: r["default_q"] for r in te if r["y_ews"] == 1 and r["default_q"] >= 0
        }
        early = sum(any(flag and q < dq for q, flag in by_ob[o]) for o, dq in defaulters.items())
        metrics["defaulters_in_test_window"] = len(defaulters)
        metrics["flagged_at_least_one_quarter_before_sma2"] = early
        idio = sum(1 for r in te if r["y_ews"] == 1 and r["idiosyncratic"])
        metrics["positives_from_unforeseeable_shocks"] = idio
    explainer = shap.TreeExplainer(model)
    sv = explainer.shap_values(x(te))
    sv = sv[1] if isinstance(sv, list) else sv
    reasons = []
    for i in order[:5]:
        contrib = sorted(zip(FEATURES, sv[i], x(te)[i], strict=True), key=lambda t: -abs(t[1]))[:5]
        reasons.append(
            {
                "obligor": te[i]["obligor"],
                "quarter": te[i]["q"],
                "score": float(p[i]),
                "actual": int(yt[i]),
                "reasons": [
                    {"feature": f, "contribution": float(c), "human_text": HUMAN[f].format(v=v)}
                    for f, c, v in contrib
                ],
            }
        )
    imp = sorted(zip(FEATURES, np.abs(sv).mean(axis=0), strict=True), key=lambda t: -t[1])[:8]
    mon: dict[str, Any] = {
        "psi_score_train_vs_test": psi(_proba(model, x(tr)), p_raw),
        "max_feature_psi": max(psi(x(tr)[:, j], x(te)[:, j]) for j in range(len(FEATURES))),
    }
    mon["psi_action"] = (
        "investigate"
        if mon["psi_score_train_vs_test"] > 0.25 or mon["max_feature_psi"] > 0.25
        else "ok"
    )
    return ModelReport(
        name,
        target,
        {
            "train_quarters": [train_q.start, train_q.stop - 1],
            "calibration_quarter": calib_q,
            "test_quarters": [test_q.start, test_q.stop - 1],
            "train_rows": len(tr),
            "obligor_disjoint": bool(held),
            "test_obligors": len(held),
            "embargo": "train label horizons end before the calibration quarter",
        },
        metrics,
        baselines,
        reasons,
        [(f, float(v)) for f, v in imp],
        mon,
    )
