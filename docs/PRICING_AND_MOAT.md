# Pricing and moat

**Status of this document.** The architecture and security claims are backed by code and tests in this
repository. The commercial parts are a framework with placeholder numbers. No competitor price, market
size or bank volume is asserted here, because none was supplied or verifiable. Replace the assumptions in
`tools/pricing_model.py` with your own before quoting anything.

## The three moats, stated so they can be checked

### 1. Architecture: control and lineage are built in, not added

| Claim | Evidence in the repository | Why a vendor cannot add it quickly |
|---|---|---|
| Every number traces to a page and box | `Citation` on every CTS field, spread line and OCR value; lineage carried into covenant results | Lineage has to be part of the data model; adding it later means changing that model |
| Any decision can be replayed as at a past date | Bitemporal store, hash-chained event log, versioned rule packs selected by effective date | Needs events, versioned rules and immutable CTS from day one |
| Regulation changes are releases, not code | 18 rule packs with embedded tests; UAT and production refuse unverified packs | Requires the engine to take every parameter from configuration |
| Finacle stays the book | API-only adapter, ordered idempotent commands, reconciliation | A competing core cannot sell "around Finacle" |
| Agents cannot act | Proposals only; inherited scope; signed human approval for any change | Needs agent identity, scoped access and approval records designed in from the start |

### 2. Security and AI governance as a product, not an appendix

Banks must satisfy RBI IT governance, outsourcing and FREE-AI expectations before any model touches a
decision. We sell the evidence along with the software. **Built today:** the model gateway call log, the hash-chained
audit log, signed approvals, the independent audit log (`docs/AUDIT_LOG.md`) and measured evaluation reports.
**Not built yet:** per-model cards, independent validation reports, an AI inventory table, FREE-AI mapping and
red-team results (`docs/model-cards/` is empty). The pack is priced as a module because it removes work
from the bank's own model-risk and infosec teams, but the unbuilt parts must exist before it is sold.

### 3. Pricing that tracks the bank's value

Per-seat and per-module pricing is common in enterprise software, and it can penalise adoption and hide cost. Here the cost drivers are
visible and the bank can verify them: documents processed, the share needing human review, obligors
monitored. Because the platform reports its own straight-through rate and error rate with confidence
intervals, an outcome-linked price is auditable by both sides.

## Proposed price structure (shape, not numbers)

| Line | Metric | Notes |
|---|---|---|
| Core platform | Per monitored obligor per year, banded by sanctioned exposure | CTS, limits, covenants, calendar, gate, cases |
| Document AI | Per page processed, with a **rebate when straight-through accuracy is below the agreed floor** | Shares risk; makes the accuracy report contractual |
| Intelligence | Per obligor per year add-on | EWS, breach forecast, SMA/CRILC marts |
| Agents | Per approved draft (letter, memo) with a monthly cap | LLM cost is budget-capped in the gateway, so the cap is real |
| **Security and AI-governance pack** | Flat annual fee plus per-model validation | Inventory, validation reports, audit replay, FREE-AI mapping, red-team |
| Deployment | One-time, plus a premium for fully on-premise inference | GPU nodes in the bank's zone |

## Corridor

The calculator prints, per obligor per year: cost to serve, a price floor at the target margin, the value the
bank receives net of the review work the platform adds, and a ceiling at the share of that value you choose
to capture. If the ceiling is below the floor the offer is not viable at those assumptions.

```
python -m tools.pricing_model --obligors 400
```

## What would weaken these moats

- **Accuracy on real scans.** All accuracy figures in this repository are measured on synthetic documents.
  The moat is only real once the same report is produced on the bank's own consented documents.
- **Finacle and incumbents closing the gap.** Their covenant register could gain testing. The defence is
  lineage, replay and governance, which are harder to retrofit, and speed on the data flywheel.
- **The data flywheel needs deployments.** Human corrections become training data only after go-live.
- **Security claims need the unfinished work in `docs/SECURITY_MODEL.md`** (HSM, SSO, SIEM, penetration test).
