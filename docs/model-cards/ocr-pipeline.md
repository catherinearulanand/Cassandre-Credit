# Model card: statement OCR pipeline

**Status:** development. Release gate **failing** on coverage. Not independently validated.
**Owner:** credit administration · **Tier:** 2 · **Inventory id:** `ocr-pipeline`

## Intended use
Turn financial statements into spread lines, each with page and box lineage. The text recogniser is
**outsourced** (ADR 0004). Everything after it (structure, units, accounting-identity checks, routing,
calibration) is ours and runs on any vendor's word boxes.

## Not for
Straight-through use on a page whose unit, columns or identities cannot be confirmed; for those it
routes to a reviewer. Not for non-statement documents or non-Latin scripts.

## Measured (development engine, synthetic pages, 111 held-out pages, 2,505 figures)
See `docs/OCR_ACCURACY.md` and `reports/ocr_accuracy.json`. In short: 0 of 804 straight-through figures
wrong (95% upper bound 0.48%), coverage 32%, release gate **failed** on clean-tier accuracy and
straight-through rate because layout B is read at 30%. Consistent forgeries are flagged 71% of the time but
pinpointed 4%; swapped siblings pass straight through 15% of the time and column shifts 40% (n=20 each).

## Vendor onboarding checklist (the harness is the bake-off)
1. Word-level boxes and confidences in the reply. 2. India processing, zero retention, sub-processors
disclosed (`VendorPolicy`). 3. Refit the calibrator on that vendor's output. 4. Run the harness on consented
real scans and on the synthetic corpus; compare precision, coverage per tier, tamper residual risk, cost per page.
5. Independent validation sign-off on the thresholds (ADR 0003).

## Limitations
Synthetic pages only. Stamps, handwriting, folds, photographs, multi-page tables and Indic scripts are untested.

## Controls
Vendor outage or malformed reply raises a typed error and the page goes to manual spreading. Kill switch:
route all pages to manual spreading. Calls are logged by image hash without content.
