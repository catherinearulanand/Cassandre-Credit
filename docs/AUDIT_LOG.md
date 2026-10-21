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

## Round 2 — Re-verification of Phase 0 fixes

**Reviewer:** the same Principal Engineer, asked to attack the fixes rather than trust them.
**Verdict:** FAIL again. Of the original findings, F4, F10, F11, F14, F16, F18, F21 and F22 held; the rest were
only partly fixed, and the fixes introduced new defects.

| ID | Severity | Defect | Status |
|---|---|---|---|
| N1 | Critical | One approval could be replayed under new command ids, moving money once per replay | Fixed: approval bound to the command id, single use, one disbursement per request id |
| N2 | Critical | Dead or ambiguous commands silently released later commands; no way to resolve them; an open circuit burned attempts | Fixed: they block their account until a human resolves them; open circuit is not an attempt |
| N3 | Critical | A committed disbursement could be reported rejected | Fixed: intent is recorded before sending; any 4xx after an earlier try is `needs_reconciliation` |
| N4 | High | The drawdown gate was not enforced on disbursement | Fixed at the adapter: a gate-signed APPROVE for this request, facility and amount is required. Wiring the live gate to the live adapter is application work still to do |
| N5 | High | Look-alike identities defeated maker-checker | Fixed: plain-ASCII identities only; agent makers must name their human |
| N6 | High | Delegated power only checked when an amount was passed | Fixed for limit and OTS approvals; the amount is still supplied by the caller rather than derived from the payload (partly open) |
| N7 | High | Agent rights not capped by their human; services could hold decision roles | Fixed |
| N8 | High | Redaction skipped when `person_names` was None | Fixed |
| N9 | Medium | Redaction corrupted amounts and still missed some PII | Amounts fixed. Open: dates of birth, non-Latin names not supplied by the caller |
| N10 | High | Evaluators still defaulted missing facts | Fixed for IRAC, RFA, Prudential, CRILC, credit policy, monitoring; EWS reports unevaluated indicators |
| N11 | Medium | "12 34" parsed as 1234; leading-zero groups accepted | Fixed |
| N12 | Medium | Rule params mutable; decisions did not say the pack was unverified | Fixed |
| N13 | Medium | Block-delimiter disguises; a trivial schema counted as constrained | Fixed. The injection screen remains a screen, not a guarantee |
| N14 | High | Gate approved on unknown SMA or covenant values, empty payee list, stale or wrong-facility context; hung dependency held it open | Fixed |
| N15 | Medium | Command bodies not validated against targets | Fixed (allow-lists, status list); covenant records still lightly validated |
| N16 | Medium | Proposer chose the approval permission | Fixed |
| N17-N20 | Low | Listener errors stopped the queue; approvals checked only at submit; `parse_id` newline; audit time could go backwards | Fixed |

## Round 2 — Phase 1 review (document AI, covenants, CTS, models)

**Reviewer:** Head of Model Risk Validation and Document-AI Evaluation (statistician; validated wholesale credit
models and document extraction at a global investment bank and at a large Indian bank).
**Verdict:** FAIL for the Phase 1 gate. The evidence shows the plumbing is deterministic and self-consistent on a
synthetic world built from the same assumptions as the code. It does not show real-scan accuracy, covenant
correctness on stressed obligors or non-trivial forecasting skill. Several headline numbers were circular.

| ID | Sev | Defect | Status |
|---|---|---|---|
| F1 | High | A negative denominator gave a negative ratio that passed every maximum covenant | Fixed: treated as a breach; the independent oracle now encodes the same policy |
| F2 | High | Columns bound by position, ignoring the Current/Previous headers | Fixed: bound by headers; swapped, single or unlabelled columns go to review |
| F3 | High | Identity-preserving errors (swapped or shifted siblings) pass the arithmetic check | Reduced by a continuity check against the prior column. **Residual risk measured, not eliminated** (see OCR report, identity-preserving section) |
| F4 | High | Unit scale never cross-checked | Partly fixed: a header that contradicts the number format goes to review. No magnitude check against sanction size yet |
| F5 | High | Breach forecast AUC mostly persistence; stress generator shared model inputs | Reported honestly: persistence baseline and new-breach-only AUC added. The forecast shows no demonstrated skill beyond "already in breach" |
| F6 | Medium | "Unforeseeable" shocks leaked into conduct features | Fixed: shocks now affect the outcome only |
| F7 | Medium | Out-of-time split leaked; calibration on ~215 rows; row bootstrap | Fixed: obligor-disjoint, embargo, Platt scaling, obligor-cluster bootstrap. Feature drift (PSI) is flagged "investigate", not hidden |
| F8 | Medium | OCR split shared quarters; field-level CIs too tight | Fixed: split by obligor; page-clustered and page-level bounds |
| F9 | Medium | "Delivered accuracy assuming perfect review" tautological | Removed from the gate |
| F10 | Medium | Straw-man confidence baseline | Fixed: tuned on the calibration half to the same target |
| F11 | Medium | Threshold amended after results without an ADR; vacuous regression baseline | ADR 0003 written; **reviewer sign-off still pending** |
| F12 | Medium | Single template; labels drawn from the extractor's own aliases | Partly fixed: second layout, Notes column, independently worded labels. Still synthetic; no stamps, handwriting, multi-page tables or real scans |
| F13 | Medium | Compliance judged at 4 decimal places | Fixed: exact comparison |
| F14 | Medium | Equity cure mutated its input; no cap | Fixed: pure evaluation, capped, caller records the consumed cure |
| F15 | Medium | No clock meant IN_CURE; grace ignored without a cure period | Fixed |
| F16 | Medium | First test date and restated periods not handled | Fixed. Waiver validity (period has data, approval live) still open |
| F17 | Medium | Group Large Exposures check inert | Fixed: unknown group exposure blocks publication |
| F18 | Medium | Amendment could change CTS id and obligor | Fixed |
| F19 | Medium | Conflicting letter lines: last one won | Fixed: flagged unresolved |
| F20 | Medium | Compiler "100% of fields" partly circular (formulas and cures from a library) | **Open.** Formulas, cure and equity-cure terms are not yet extracted from the text; the 100% figure covers only terms present in the template |
| F21 | Medium | "0 of 79" presented as proof an LLM is needed | Reworded: it shows grammar brittleness, not LLM need |
| F22 | Medium | Covenant and gate "100%" prove little | Partly addressed (oracle policy, exactness). It remains a regression test on a shared formula library; independent semantics for `prior`, `avg`, half-yearly and consolidated tests are not built |
| F23 | Medium | Docs claimed things not in the repository | Fixed: security principle 5 and pricing claims corrected |
| F24 | Low | NaN written into JSON | Fixed: strict JSON |

## Round 3 — Phase 2 review (monitoring, classification, RFA, EWS, entity resolution, vendor OCR)

**Reviewer:** Head of Credit Monitoring and Regulatory Reporting Systems (SMA/NPA, CRILC, fraud-risk governance; former internal-audit lead on RBI inspections).
**Verdict:** FAIL at the gate. Two Critical and six High findings, all reproduced. All are fixed with a regression test in `tests/unit/test_phase2_regressions.py` unless marked.

| ID | Sev | Defect | Status |
|---|---|---|---|
| P2-01 | Critical | A show-cause could be backdated, so the 21-day window read as elapsed on day 0 | Fixed: dated between file opening and today; document reference and issuer required |
| P2-02 | Critical | New allegations rode an old notice; a decided file could never be reopened | Fixed: new evidence resets the notice; a pending decision blocks new evidence; `close_file` lets a fresh file open |
| P2-03 | High | A same-day placeholder response unlocked the committee | Fixed: document reference and recorder required |
| P2-04 | High | Amendments with the same CTS id changed nothing (ids hashed the CTS id) | Fixed: ids are obligor + type + period; due dates updated in place; removed open items waived |
| P2-05 | High | A new CTS id duplicated received items and hid overdue ones | Fixed: rows carry over status, document and chase history |
| P2-06 | High | A data blackout read as "clear" and was invisible in the queue | Fixed: a data-gap signal and queue entry |
| P2-07 | High | NPA age defaulted to 0 months, so provisioning could be understated | Fixed: age derived from `npa_since` and the clock; unknown age is refused |
| P2-08 | High | A contradictory "arrears cleared" flag upgraded an NPA | Fixed: refused while days past due remain. **Open policy question:** no probation after a cure |
| P2-09 | Medium | A failed `EntityIndex.add` left ghost identifiers that crashed `resolve` | Fixed: validate, then write; re-add replaces |
| P2-10 | Medium | A contradictory CIN/LEI/PAN still resolved as "certain" | Fixed: conflict |
| P2-11 | Medium | Related-party screening missed suffix-only, non-Latin and initial-only names | Fixed. Look-alike false candidates remain by design: a human confirms |
| P2-12 | Medium | Chase skipped steps; any document satisfied any obligation | Fixed: each step is recorded; obligor, type and period must match |
| P2-13 | Medium | A late but cured perfection left no record; a second `require` was ignored | Fixed |
| P2-14 | Medium | Vendor OCR: overflow escaped unaudited; loose coercion; empty page accepted; unbounded policy numbers | Fixed |
| P2-15 | Low | NaN model score, unknown obligor reported "not reportable", blank indicators | Fixed |
| P2-16 | Medium | The lifecycle test hand-wrote the gate's context and several asserts were vacuous | Fixed: the gate now reads the SMA, RFA and case services |

## Round 3 — Phase 3 review (agents, runtime, workflow services, governance)

**Reviewer:** Head of AI Risk and Agentic Systems Security (LLM red-teamer).
**Verdict:** FAIL at the gate. Two Critical, eight High. Fixed with regression tests in `tests/unit/test_agents.py`, `test_phase3_regressions.py` and `test_model_gateway.py` unless marked.

| ID | Sev | Defect | Status |
|---|---|---|---|
| F1 | Critical | Facts never reached the model, and the side channel (`context`) skipped the injection screen and redaction | Fixed: facts are screened data blocks; the gateway drops `context` for any non-local provider |
| F2 | Critical | `verify_numbers` passed `250bps`, `₹5crore`, `-35%`, ranges, ids, number words, scaled and re-united values | Fixed: units are part of the match, nothing is exempt, number words must appear in a fact |
| F3 | High | No semantic guard: a letter could waive the covenant | Fixed: concession screen and required wording. Pattern checks, not proof |
| F4 | High | An agent could propose any kind regardless of its policy | Fixed: `propose_kinds` |
| F5 | High | Scope was fixed per run, not per resource named in a call | Fixed in the runtime via a resolver. **Open:** `CaseService.propose` itself does no scope check |
| F6 | High | Identity stamping depended on the tool's label; undeclared kwargs accepted | Fixed |
| F7 | High | Closure ignored dues on unnamed facilities, never re-checked, was repeatable | Fixed (repayment date is still caller-asserted; **open**: source it from the ledger port) |
| F8 | High | OTS and limit approvals could never succeed (amount never passed) | Fixed: amount taken from the proposal; delegated power now exercised |
| F9 | High | Wilful designation accepted a forged status dict and weak date checks | Fixed: status derived from evidence, evidence in the payload, strict booleans and dates |
| F10 | Medium | A late or backdated plan erased the deadline breach | Fixed |
| F11 | Medium | Free text could enter the review pack and launder numbers | Fixed: allow-listed, typed fields |
| F12 | Medium | Failed calls not audited or budgeted; crashed runs not finished | Fixed |
| F13 | Medium | Compensation exemption had no reason or evidence | Fixed |
| F14 | Low | Vacuous or circular tests | Fixed |
| F15 | Medium | Governance test could be satisfied without a real validation | Strengthened: report fields, role and data-class cross-checks. **Open:** reports are not cryptographically signed |
