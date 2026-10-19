# Independent audit log

Each phase is reviewed by a separate reviewer with a defined background, who may only read, run
and probe: they cannot edit the repository. Findings count only if they were reproduced. Fixes come
with a regression test that reproduces the reviewer's probe.

## Phase 0 — Foundations

**Reviewer:** Principal Engineer and Head of Platform Security Architecture. Background: wholesale-banking
platform engineering at a global bank (idempotent core-banking integration, entitlements) and data-lineage
platforms at an investment bank; Indian IT-governance and cyber readiness lead.
**Verdict at gate:** FAIL. All five automated gates were green, but the Finacle round-trip held only on the
happy path.

| ID | Severity | Defect | Status |
|---|---|---|---|
| F1 | Critical | A later command for the same target could apply before an earlier failed one, then be overwritten by it on retry (stale drawing power) | Fixed: dispatch is ordered per target. `test_F1_*` |
| F2 | Critical | A retry after a lost response was reported as rejected although the money had moved | Fixed: idempotency lookup first; ambiguous 4xx on a retried key becomes `needs_reconciliation`. `test_F2_*` |
| F3 | Critical | Disbursement, limit and DP commands needed no verified approval; negative amounts accepted | Fixed: approval verified against the exact body; amounts validated. `test_F3_*` |
| F4 | High | Approval records could be forged field by field and still verify | Fixed: whole record signed. `test_F4_*` |
| F5 | High | An agent could be maker while its own human was checker; spelling variants bypassed the check | Fixed. `test_F5_*` |
| F6 | High | Agent scope not narrowed to its human; `kind` was a free string; delegated power ignored | Fixed. `test_F6_*` |
| F7 | High | Frontier calls without redaction when config said so; instruction text never redacted | Fixed: unconditional. `test_F7_*` |
| F8 | High | Caller-declared data class was trusted | **Open** (provenance from the document service) |
| F9 | High | Redaction and log scrubber missed many PII spellings | Fixed. `test_F9_*` |
| F10 | High | A future-dated rule pack applied today | Fixed. `test_F10_*` |
| F11 | High | Typographic thousands marks read as rupees (1000x understatement) | Fixed. `test_F11_*` |
| F12 | High | Evaluators defaulted missing facts to "safe" values | Fixed: fail closed. `test_F12_*` |
| F13 | Medium | Unverified-rule override unaudited; verified pack without verifier accepted | Fixed. `test_F13_*` |
| F14 | Medium | Outbox relay stuck forever after a crash-before-mark | Fixed. `test_F14_*` |
| F15 | Medium | Failed calls not charged; restore could crash after a call; one rate slot per up to four calls | Fixed (output-token cap still advisory). `test_F15_*` |
| F16 | Medium | Anyone could re-enable a killed model | Fixed: authorised, human only. |
| F17 | Medium | `</data>` escape; constrained content allowed free text | Fixed. `test_F17_*` |
| F18 | Medium | DSL: exponential validation, raw errors, state leaking between evaluations | Fixed. `test_F18_*` |
| F19 | Medium | Contract check did not recurse into nested definitions | **Open** |
| F20 | Medium | Malformed digit grouping and bare "Cr" parsed silently | Fixed. `test_F20_*` |
| F21 | Medium | 408/429 terminal; empty body crashed; unbounded retries; resubmission with new body ignored | Fixed. |
| F22 | Medium | Hashing accepted floats and timezone/scale variants inconsistently | Fixed. `test_F22_*` |
| F23 | Low | Reconciliation covers limits and DP only | **Open** |
| F24 | Low | Recorded time could go backwards | Fixed. |
| F25 | Low | Audit detail aliasing, validators accepting a trailing newline, float/bool money | Fixed except domain-model identifier validation (**open**) |

After the fixes the suite grew from 135 to 221 tests with all gates green.
