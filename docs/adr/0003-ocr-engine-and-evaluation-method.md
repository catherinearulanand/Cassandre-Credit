# ADR 0003: OCR engine, evaluation method and release-gate amendments

**Status:** accepted · 20 Oct 2026 · reviewer sign-off pending (model risk, see Consequences)

## Context
The build guide asks for a layered OCR pipeline with measured accuracy (section 9) and a release gate
that fails CI on regression. We needed a recogniser that runs on a Windows workstation without a GPU,
and an evaluation that does not flatter the pipeline.

## Decisions

**Engine.** PP-OCR family models through ONNX Runtime (`rapidocr-onnxruntime`) behind the `OcrEngine`
interface. It is the in-house/open-source tier. A commercial India-hosted engine and a handwriting or
Indic-script model plug into the same interface later. Roughly 3.7 s per page on one CPU process.

**What the engine is not trusted for.** Confidence is not correctness. Every figure that goes straight
through must be corroborated by an accounting identity that holds, must not be a repaired value, must
sit in a page with a known unit, readable yield and bound columns, and must not jump against the
prior-period column. Repairs are suggestions for a reviewer, never straight-through.

**Evaluation.**
- Synthetic pages with exact ground truth, four degradation tiers, two layouts, 40 obligors, six fonts.
  Layout B has a Notes column and wording written independently of the extractor's alias table.
- Calibration and test are split **by obligor**, so no company's figures appear in both halves.
- Confidence intervals: field-level Wilson, page-clustered bootstrap, and a page-level Wilson bound on
  the share of straight-through pages with no wrong figure.
- The confidence-only baseline is tuned on the calibration half to the same precision target.
- Identity-preserving errors (swapped or shifted siblings) and consistent forgeries are measured and
  reported as known residual risk, not hidden.

## Release-gate amendments (history, because they were made after seeing results)
| Version | Change | Why |
|---|---|---|
| v1, 19 Oct | Fixed from guide section 9.4 before the first run | Pre-registration |
| v2, 19 Oct | Unit detection measured on supported tiers; severe tier gated on safe rejection | v1 asked a recogniser to read pages no recogniser reads. No supported-tier threshold was lowered |
| v3, 20 Oct | Dropped "delivered accuracy assuming perfect review"; added page-level bound, moderate-tier floor and layout-B coverage; regression tolerance 0.01 to 0.02 | The metric was tautological. The corpus became harder, so run-to-run variance is larger |

## Alternatives considered
- Tesseract: weaker on dense numeric tables in our probes.
- A vision-language model for everything: slower, costlier, and its confidence is harder to calibrate;
  kept as the structuring tier for documents the deterministic path cannot read.

## Consequences
- The champion baseline in `ocr/eval/baseline.json` is the first accepted run on the v3 corpus. It is
  updated only by an explicit `--update-baseline`, in a reviewed change.
- This ADR does not replace independent validation. A model-risk reviewer must sign the thresholds
  before the numbers are quoted outside the team.
- Everything here is measured on synthetic pages. The same harness runs unchanged on consented real
  scans, which is the next step and the only evidence that counts for a customer.

## Outcome of the v3 run (20 Oct)
The v3 corpus **failed** the v3 gate on coverage: clean-tier accuracy 65.1% against 99% required, because
layout B is read at 30%. Straight-through precision stayed at 100% (0 of 804 wrong). Thresholds were not
changed and aliases were not fitted to layout B. Remediation plan is in `docs/OCR_ACCURACY.md`. The champion
baseline file holds this failing run and must be reset once a passing run exists.
