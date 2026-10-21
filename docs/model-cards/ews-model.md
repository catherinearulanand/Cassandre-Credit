# Model card: EWS stress score (M4)

**Status:** development. Not independently validated. Measured on a synthetic book only.
**Owner:** credit risk · **Tier:** 2 · **Inventory id:** `ews-lgbm-m4` · **Code:** `ml/ews_model.py`

## Intended use
Rank obligors by near-term stress so analysts look at the right ones first. Advisory. It never
changes a limit, a classification or a drawdown. The rules-based EWS (`ews.indicators`) keeps running
without it, and `ews.alerting` fuses both with hysteresis.

## Not for
Fraud determination, provisioning, pricing, or any automatic action.

## Data and method
Gradient-boosted trees (LightGBM) on conduct, spread-derived ratios and covenant headroom, with
SHAP reasons and Platt scaling fitted on a separate quarter. Training, calibration and test are
separated by time, with an embargo, and the test set contains obligors never seen in training.

## Results (synthetic book, 600 obligors, seed in report; 325 test rows, 39 positives)
| Measure | Value |
|---|---|
| AUC | 0.838 (95% CI 0.734 to 0.918, obligor-clustered bootstrap) |
| Precision at 10% alert capacity | 56% (CI 39% to 72%) |
| Recall at 10% alert capacity | 46% (CI 32% to 61%) |
| Calibration error (ECE) | 0.032 after scaling, 0.048 before |
| Baseline: count of fired rules | AUC 0.803 |
| Baseline: current DPD only | AUC 0.631 |
| Baseline: persistence | AUC 0.627 |

The lift over the rules-only baseline is **modest (0.84 vs 0.80)** and its interval overlaps the baseline.
We do not claim the model is necessary; we claim it is no worse and gives ranked, explained alerts.

## Known limitations
- Defaults in the synthetic world include shocks no input can predict (19 of 212); real books will differ.
- The generator and the model share assumptions, so these figures are an upper bound on real performance.
- Score distribution drift between train and test is very high (score PSI 1.03, max feature PSI 3.17, action "investigate"); on a real
  book this needs a monitoring decision before use.
- Small test sample: wide intervals.

## Controls
Human reviews every alert. Reason codes accompany every score. Kill switch: disable the score, rules continue.
Promotion needs `model:promote` permission and an independent validation report (not yet produced).

## Before production
Back-test on the bank's own history (guide section 21 Q6), fix the alert-capacity target (Q7), independent
validation, and a monitoring plan for drift, calibration and fairness across segments.
