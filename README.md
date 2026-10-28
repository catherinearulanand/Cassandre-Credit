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
| Model gateway (routing, data classes, redaction, budgets, kill switch) | Built; Groq free-tier prototype adapter added for synthetic covenant extraction, awaiting the user's key | `tests/unit/test_model_gateway.py`, `tests/unit/test_groq_prototype.py` |
| Rules engine and 18 rule packs | Built; **every pack is `unverified`** and must be checked against RBI text before UAT | `rules/CHANGELOG.md` |
| Covenant DSL and test engine | Built | 13,540 simulated tests agree with an independent calculator |
| CTS, limit tree, drawing power, drawdown gate, cases | Built | simulation report, property tests |
| Sanction Compiler | Grammar path built; synthetic Groq covenant-proposal bridge built; **real negotiated-agreement path still requires a self-hosted model and measurement** | `docs/LLM_DECISION.md` |
| OCR pipeline for financial statements | Built; **v3 release gate currently FAILS on coverage** (zero wrong figures auto-accepted, but layout B is read at 30%). See `docs/OCR_ACCURACY.md` | `reports/ocr_accuracy.json` |
| EWS and breach forecast models | Built; validated out of time on **synthetic** data only | `reports/simulation_report.json` |
| Postgres, Kafka, Neo4j, Temporal, Feast adapters; UI; real Finacle | **Not built** (ports and in-memory adapters exist, ADR 0002) | |
| Monitoring calendar, SMA/ECL, entity resolution, RFA, EWS service, perfection, stressed assets, recovery, closure, review | Built as domain services with ports; reviewed twice by independent red-teams (`docs/AUDIT_LOG.md`, round 3). **No persistence, API or UI** | `tests/unit/test_phase2_*`, `test_phase3_*` |
| Agents (covenant, EWS analyst, review memo, recovery) and hardened runtime | Built; propose-only, number-bound drafts, template fallback. **LLM drafting not measured** | `docs/model-cards/agents.md` |
| Outsourced OCR port | Built (ADR 0004); needs a vendor adapter, per-vendor calibration and a bake-off on real scans | `ocr/pipeline/vendor.py` |
| PaddleOCR and the demonstration story | PaddleOCR local worker **connected**; hosted API adapter awaiting a key; four-borrower replay with every step labelled real or simulated | `docs/SIMULATION_BRIEF.md`, `python -m sim.demo_story` |
| AI inventory, model cards, validation pack | Built; **nothing is validated** | `governance/`, `docs/VALIDATION_PACK.md` |

## What the numbers do and do not prove

All accuracy figures come from synthetic data with exact ground truth. They show that the pipeline logic is
correct and that its controls hold under controlled damage. They do not show performance on real bank scans,
real sanction letters or real defaults. Need to test on real data before going to any customer or regulator. 

## Layout

`libs/` shared code · `services/` domain services (hexagonal) · `ocr/` pipeline, synthetic renderer, evaluation ·
`ml/` models · `agents/` scoped agents · `rules/` YAML rule packs · `contracts/` event and CTS schemas ·
`simulators/` Finacle · `sim/` end-to-end simulation · `tests/` unit, integration, architecture · `docs/`
