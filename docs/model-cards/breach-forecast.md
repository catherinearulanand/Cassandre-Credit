# Model card: covenant breach forecast (M6)

**Status:** development. Not independently validated. **Show no skill claim beyond persistence.**
**Owner:** credit risk · **Tier:** 2 · **Inventory id:** `breach-forecast-m6` · **Code:** `ml/ews_model.py`

## Intended use
Advisory estimate of the chance a covenant is breached at the next test date, so a relationship manager
can open a conversation early. It never decides compliance: the covenant engine does that, exactly.

## Results (synthetic book; 306 test rows, 79 positives, only 7 that were not already in breach)
| Measure | Value |
|---|---|
| AUC overall | 0.997 |
| AUC on rows not already in breach (7 positives) | 1.0, but this equals the headroom-only AUC |
| Baseline: persistence | 0.954 |
| Baseline: count of fired rules | 0.959 |

## What this means
Headroom to the threshold explains essentially all of the forecast, and "already in breach" explains
most of the rest. **The model has not shown skill beyond a simple headroom rule.** The high AUC is a
property of the synthetic generator, where breaches follow headroom closely. Do not quote it as evidence of
forecasting ability.

## Limitations
Only 7 new-breach positives. Score drift between train and test is large (score PSI 0.39, max feature PSI 2.5,
action "investigate"). Synthetic data only.

## Controls
Advisory only. Kill switch: disable forecast. Before any use: back-test on real covenant history, and show
lift over a headroom rule on new breaches with an adequate sample.
