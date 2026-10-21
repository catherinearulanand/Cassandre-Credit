# ADR 0004: OCR is outsourced behind the engine interface

**Status:** accepted · 21 Oct 2026

## Context
The bank will buy text recognition from a vendor rather than operate it. Everything downstream of
recognition (structure, unit detection, identity checks, routing, spreading, covenants) is ours.

## Decisions
- `VendorOcrEngine` implements `OcrEngine`. The vendor transport is a port; no vendor SDK is imported.
- **Word-level boxes are a procurement requirement.** A reply without them is rejected, because
  lineage (page, bbox) and the accounting checks depend on them. Field-only APIs are not acceptable.
- Contract terms are code: India processing, zero retention, contract reference, disclosed
  sub-processors. The engine refuses to start otherwise.
- Calls are logged without the image: vendor, contract, image hash, size, outcome, time.
- Outage, timeout or a malformed reply raises a typed error; the page goes to manual spreading and
  nothing is guessed. This is the same fail-closed stance as the drawdown gate.
- **Calibration is per vendor.** The confidence calibrator is refit on that vendor's output, and the
  harness runs unchanged as the vendor bake-off (precision, coverage per tier, tamper residual risk, cost).

## Consequences
- The layout B coverage failure (see `docs/OCR_ACCURACY.md`) is in our label matching, which sits after
  the engine, so it still has to be fixed in-house.
- Borrower financials leave the bank. The outsourcing agreement, data-protection impact assessment and
  regulator notification are the bank's to complete. `VendorPolicy.check` only enforces that they are recorded.
- Vendor accuracy claims are not accepted. Only the harness on consented real scans counts.
