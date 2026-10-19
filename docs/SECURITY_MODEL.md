# Security model

Written for the bank's information-security and model-risk reviewers. Every row names the code
that enforces the control and the test that proves it. The last section lists what is **not** done.

## Principles

1. **The platform recommends; a named human decides.** Agents and services create proposals. A
   decision is a signed approval bound to the exact thing approved.
2. **Fail closed.** New money is refused when a dependency fails. Repayments are never blocked.
3. **Assume documents are hostile.** A borrower PDF is data. It is screened, escaped, redacted and
   never concatenated into an instruction.
4. **Nothing is silently trusted.** Hashes, signatures and chains are verified on read, not only on write.
5. **Every audit finding has a regression test.** An independent adversarial review of the foundation found
   25 defects, including three critical. They are listed in `docs/AUDIT_LOG.md`.

## Controls

| Area | Control | Code | Proven by |
|---|---|---|---|
| Money integrity | Integer paise, no floats, no bools, exact Indian-number parsing, malformed digit grouping rejected, ambiguous "Cr" refused | `libs/platform_core/money.py`, `libs/identifiers/numbers.py` | `test_platform_core`, `test_identifiers`, `test_audit_regressions` |
| Finacle safety | API only, idempotency key on every write, **commands applied in order per target**, ambiguous failures reconciled rather than reported as rejected, single half-open circuit probe, bounded retries | `services/finacle_adapter/adapter.py` | `test_finacle_roundtrip` (lost response after commit, stale-overwrite, retries, outage) |
| Approvals | Every limit, drawing-power or disbursement command needs an approval **verified against its exact body**; amounts validated; unsigned strings refused | `adapter.py`, `libs/authz/__init__.py` | `test_F3_*` |
| Maker-checker | Checker is a different human, compared after normalisation; an agent's human cannot approve the agent's work; the **whole approval record is signed**; delegated financial power enforced | `libs/authz/__init__.py` | `test_F4_*`, `test_F5_*`, `test_F6_*` |
| Access control | RBAC plus region, portfolio and obligor scope; agents inherit exactly their human's scope; agents can hold only the agent role and no decision permission | `libs/authz/__init__.py` | `test_security`, `test_F6_*` |
| Encryption | AES-256-GCM field encryption with key id, rotation, and **context binding** (a ciphertext copied to another row fails) | `libs/security/crypto.py` | `test_field_encryption_binds_context_and_rotates` |
| Integrity | Canonical hashing; signed CTS versions re-verified on every read; hash-chained append-only audit log and event log; signed anchors; copied audit detail | `libs/platform_core/canonical.py`, `libs/security/audit.py`, `libs/events/bus.py`, `services/cts_service/domain.py` | `test_audit_chain_detects_tamper`, `test_event_log_hash_chain`, `test_publish_maker_checker_and_immutability` |
| PII | Redaction tolerant of case, separators, full-width digits and name order; fail-safe on ambiguous digit runs; log scrubber; amounts preserved | `libs/security/redaction.py`, `libs/platform_core/logging.py` | `test_F9_*` |
| Prompt injection | Normalised screen (zero-width, homoglyph-adjacent, spaced forms), delimiter escape, constrained-content rule: suspicious text may only produce schema-validated output | `libs/security/injection.py`, `services/model_gateway/gateway.py` | `test_security`, `test_F17_*` |
| LLM governance | One gateway; data classes; **redaction cannot be disabled** for any non-self-hosted provider, including the instruction; India region and zero retention required; per-call budget and logging; authorised kill-switch; tested fallback; no provider SDK outside the gateway | `services/model_gateway/`, `tests/architecture/test_boundaries.py` | `test_model_gateway`, `test_F7_*`, `test_F15_*` |
| Regulation as configuration | UAT and production refuse unverified rule packs; override requires an audit hook; `as_of` defaults to today so a future rule never applies early; overlapping windows rejected; missing facts fail closed | `libs/rules_engine/` | `test_rules`, `test_F10_*`, `test_F12_*`, `test_F13_*` |
| Safe formulas | Covenant DSL parses to an AST, never `eval`; bounded depth, length and expansion; division by zero is a typed result | `libs/rules_engine/dsl.py` | `test_dsl` (fuzz), `test_F18_*`, architecture test bans `eval`/`exec` |
| Source hygiene | No invisible or bidirectional characters in source (Trojan Source); no float in money domain code; no wall-clock reads in domain code; service isolation | `tests/architecture/test_boundaries.py` | the same file |
| Document tampering | Accounting identities flag inconsistent edits and point at the cell; auto-repairs are never straight-through, so a forged figure cannot be "fixed back" and hidden | `ocr/pipeline/validate.py`, `route.py` | `test_ocr_pipeline`, tamper section of the OCR report |

## Threats we explicitly considered

| Threat | Mitigation | Residual risk |
|---|---|---|
| Borrower uploads a statement with an edited number | Identity checks, review routing, cell pinpointing | A **consistent** forgery (edits that keep every total right) passes arithmetic. The OCR report measures this rate. Needs cross-document checks (GST, bank statements) and image forensics |
| Instruction hidden in a PDF ("mark this complied") | Screen, escape, schema-constrained output, no tool that acts, human approval | Screens are bypassable by design; the architecture, not the screen, is the defence |
| Insider edits the audit log or an approval | Hash chain, signed anchors, whole-record signatures | Anchors must be written to WORM storage by operations |
| Insider replays or inflates a Finacle command | Approval bound to the exact body, idempotency, ordered dispatch | Real Finacle must also enforce the idempotency key |
| Frontier model provider retains data | Redaction, India region, zero retention, private endpoint, kill-switch | The legal arrangement is an open question for the bank (guide §21 Q3) |
| Agent acts beyond its human | Inherited scope, no decision permission, proposals only | Directory lookup of the human is a seam to implement |
| Key compromise | Rotation by key id, context-bound ciphertext | Keys are held by a dev provider; production needs an HSM-backed provider |

## Not done yet (do not claim otherwise)

- **HSM/KMS key provider, mTLS between services, OIDC/SSO, MFA, SIEM and UEBA integration.** The seams exist; the
  integrations do not.
- **Encrypted-at-rest stores.** Field encryption is a library; the persistent stores (Postgres, object storage) are
  behind ports that still have in-memory or SQLite adapters.
- **Penetration test, DAST, supply-chain scan.** CI lists `pip-audit` and `bandit`; neither has been run against a
  frozen dependency set in this environment.
- **Image forensics** (copy-move, splice, font consistency, PDF incremental saves) for tamper detection.
- **Audit findings still open:** the gateway trusts the caller's declared data class (F8: provenance should be
  derived from the document service); the contract compatibility check does not recurse into nested definitions (F19);
  reconciliation covers limits and DP, not every account field (F23); domain models do not yet validate PAN,
  GSTIN and LEI on the obligor (F25e).
- **DPDP Act workflows:** purpose limitation, consent, retention schedule and erasure for promoter personal data.
- **Independent validation** (model risk), FREE-AI mapping and red-team of the agents.
