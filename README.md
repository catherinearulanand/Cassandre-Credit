# Corporate Credit Lifecycle Platform

Post-sanction credit monitoring around Finacle: sanction to closure (RBI stages 3 to 8). Build brief:
`docs/BUILD_GUIDE.md`. Commands run through `python -m tools.cli <target>` (Windows has no `make`).

```
uv venv --python 3.12 .venv && uv pip install --python .venv -e ".[dev]"
python -m tools.cli all                 # lint, types, contracts, rule packs, tests
python -m tools.cli test-golden         # OCR accuracy gate (cached OCR makes reruns fast)
python -m sim.run_simulation --n 300    # end-to-end simulation against independent oracles
python -m tools.pricing_model           # cost-to-serve and price corridor (placeholder inputs)
```

## Status

| Area | State | Evidence |
|---|---|---|
| Foundations: money, time, ids, bitemporal, hashing, events, outbox, contracts | Built | `tests/unit`, `tests/architecture` |
| Finacle simulator and adapter (ordered idempotent commands, verified approvals, reconciliation) | Built, audited | `tests/integration/test_finacle_roundtrip.py`, `docs/AUDIT_LOG.md` |
| Security: field encryption, signing, audit chain, redaction, injection screen, authz, maker-checker | Built, audited | `docs/SECURITY_MODEL.md` |
| Model gateway (routing, data classes, redaction, budgets, kill switch) | Built; providers are adapters, no live model attached | `tests/unit/test_model_gateway.py` |
| Rules engine and 18 rule packs | Built; **every pack is `unverified`** and must be checked against RBI text before UAT | `rules/CHANGELOG.md` |
| Covenant DSL and test engine | Built | 13,540 simulated tests agree with an independent calculator |
| CTS, limit tree, drawing power, drawdown gate, cases | Built | simulation report, property tests |
| Sanction Compiler | Grammar path built; **LLM path for negotiated agreements not built or measured** | `docs/LLM_DECISION.md` |
| OCR pipeline for financial statements | Built and gated on synthetic pages | `reports/ocr_accuracy.json` |
| EWS and breach forecast models | Built; validated out of time on **synthetic** data only | `reports/simulation_report.json` |
| Postgres, Kafka, Neo4j, Temporal, Feast adapters; UI; real Finacle | **Not built** (ports and in-memory adapters exist, ADR 0002) | |
| Monitoring calendar, SMA service, RFA, documentation agent, review, recovery, closure workflows | **Rules and clocks exist; services and workflows not built** | |

## What the numbers do and do not prove

All accuracy figures come from synthetic data with exact ground truth. They show that the pipeline logic is
correct and that its controls hold under controlled damage. They do not show performance on real bank scans,
real sanction letters or real defaults. Repeat every report on consented real samples before any claim to a
customer or regulator.

## Layout

`libs/` shared code · `services/` domain services (hexagonal) · `ocr/` pipeline, synthetic renderer, evaluation ·
`ml/` models · `agents/` scoped agents · `rules/` YAML rule packs · `contracts/` event and CTS schemas ·
`simulators/` Finacle · `sim/` end-to-end simulation · `tests/` unit, integration, architecture · `docs/`
