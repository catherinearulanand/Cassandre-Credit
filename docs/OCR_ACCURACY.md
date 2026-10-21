# How we show OCR accuracy, and what the numbers say today

Written for the bank's credit-risk, model-risk and operations reviewers.

**Source of every figure below:** `reports/ocr_accuracy.json`, produced by `python -m ocr.eval.harness`
(engine rapidocr-onnx 1.4.4, pipeline 0.3.0, seed fixed, 240 synthetic pages, 111 held-out test pages,
2,505 test fields). Dashboard: `reports/ocr_dashboard.html`. **All of it is synthetic.**

> **Re-measured on 21 Oct with bundled fonts (supersedes the figures below).** The first run rendered pages
> with Windows fonts, which do not exist on a Linux build machine, so CI could not reproduce it. Fonts are now
> bundled (`ocr/synthetic/fonts`), pages are identical everywhere, and the run was repeated. Same conclusion,
> slightly different numbers: 2,505 test figures; **0 of 836 straight-through figures wrong** (95% bound 0.46%);
> overall accuracy 48.9%, straight-through rate 33.4%. By tier (correct / straight-through): clean 60.8% / 42.1%,
> mild 72.2% / 50.7%, moderate 59.8% / 41.6%, severe 5.9% / 1.4% (rescan requested). By layout: A 74.8% correct,
> B 24.5%. The release gate still **fails** on the same five coverage checks. Tamper: one-figure edits flagged 97.5%
> and pinpointed 55%; consistent forgeries flagged 70.8%, pinpointed 0%. Swapped siblings pass straight through
> 15% of the time, column shifts 40%. Tables below are from the earlier Windows-font run and are kept for history.
> `ocr/eval/baseline.json` holds this run, and is the regression reference, not an accepted champion.

## Verdict first

| Question | Answer |
|---|---|
| Does a wrong figure reach the ledger unreviewed? | None did in 804 straight-through figures. The 95% upper bound on that error rate is 0.48%. Page level: 100% of straight-through pages were clean, lower bound 91.6%. |
| Does the pipeline meet its own release gate? | **No. The gate FAILED** on coverage (below). It is not green and we have not loosened it. |
| Why | Layout B (notes column, labels worded independently of the extractor's alias table) is read at 30% accuracy. 1,148 of the misses are "row not recognised", and none are wrong values accepted. The system leaves them to a person instead of guessing. |

Gate failures: page-level clean lower bound 0.916 (needs 0.93); clean tier STP 42.4% (needs 80%);
clean accuracy 65.1% (needs 99%); mild 75.4% (needs 99%); moderate 61.6% (needs 80%).

## The metric stack we show (and why each one is there)

1. **Per scan-quality tier** (clean, mild, moderate, severe): located, correct, straight-through, reviewer
   queue. A single blended accuracy hides the tier where the product is unusable.
2. **Straight-through precision, with an interval.** The number a credit officer cares about is "of what
   you did not show me, how much was wrong". Reported as a Wilson bound, plus a page-clustered bootstrap
   (figures on one page share scan damage) and a page-level bound.
3. **Coverage next to precision.** Precision of 100% with a straight-through rate of 32% is honest and
   not impressive. Always shown together.
4. **Calibration.** Raw engine confidence ECE 0.054; calibrated score ECE 0.029.
5. **Ablation against a confidence-only rule** tuned to the same precision target on the calibration half.
   On this corpus it accepts nothing (the engine is confidently wrong too often), which is the argument for
   arithmetic corroboration rather than trusting confidence.
6. **Error taxonomy.** Test split: 1,148 missing rows, 97 wrong values flagged for review, 0 wrong values accepted.
7. **Adversarial and residual risk, reported, not hidden.**
   - One figure edited: flagged 95% of the time, correct cell pinpointed 50%.
   - Edits that keep every total consistent: flagged 71% (mostly by side effects), cell pinpointed 4%.
     Arithmetic cannot see a consistent forgery; this needs cross-document checks and image forensics.
   - Identity-preserving swap: 15% would pass straight through. Column shift: 40% would pass
     (small samples, n=20 each; wide intervals).
8. **Latency.** 7.2 s per page mean, 10.8 s p95, one CPU process.

## Per tier (test half)

| Tier | Located | Correct | Straight-through | Wrong among straight-through |
|---|---|---|---|---|
| Clean | 66.7% | 65.1% | 42.4% | 0 of 318 |
| Mild | 75.9% | 75.4% | 53.9% | 0 |
| Moderate | 63.3% | 61.6% | 34.2% | 0 |
| Severe | 13.3% | 2.2% | 0% (rescan requested) | n/a |

Layout A alone: 71.5% correct, 64.8% straight-through. Layout B: 30.3% correct, 1.3% straight-through.

## What happened to the gate, honestly

The previous corpus (one layout) passed all gates. We then hardened it after a model-risk review
(second layout, independent wording, notes column, identity-preserving tests, obligor-disjoint split).
The pipeline did worse, which is the point of a harder corpus. We did **not** fit aliases to the
Layout B wording to turn the gate green: that would make Layout B a development set and the number
meaningless. The proper next steps, in order:

1. Replace the alias table with a label classifier trained on public Indian annual-report line items,
   not on the generator's wording.
2. Add a held-out Layout C that nobody has looked at, and gate on that.
3. Run the same harness on consented real scans. That is the only evidence a customer should accept.

`ocr/eval/baseline.json` currently holds this failing run, because `--update-baseline` ran. It should be
reset when the gate passes again; do not treat it as an accepted champion.

## What synthetic data cannot show

Real scans vary more than one renderer and six fonts; stamps, handwriting, folded or photographed pages,
multi-page tables, non-Latin scripts and non-statement documents are not covered. Thresholds were set
before the first run but amended twice afterwards (ADR 0003); a model-risk sign-off is pending.
