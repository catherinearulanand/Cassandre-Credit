| Area | Component | Status | What is behind it | Evidence |
|---|---|---|---|---|
| OCR | PaddleOCR (local worker) | **CONNECTED** | PP-OCR models in an isolated interpreter (paddleocr-local) | read ['Total', 'Equity', '500.00'] |
| OCR | PaddleOCR hosted API | **AWAITING KEY** | adapter written (synthetic data only: the service is outside India); response shape unverified until a key is used | no key in the environment |
| Core banking | Finacle | **SIMULATED** | simulators/finacle_sim (in-process); the adapter in front of it is the production code | simulator answered HTTP 200 |
| AI | LLM providers (self-hosted and frontier) | **NOT BUILT** | gateway, redaction, budgets and kill switch are real; no provider is attached, so drafting uses deterministic templates | 7 use cases routed; 0 providers connected |
| Platform | Rules engine, 20 rule packs | **CONNECTED** | production code; pack contents are unverified against RBI text | rules-test passes |
| Platform | Covenant engine, CTS, limits, drawing power, drawdown gate | **CONNECTED** | production code on synthetic input | agrees with independent calculators |
| Platform | SMA/ECL, RFA, EWS, calendar, stressed assets, recovery, closure, review | **CONNECTED** | production domain services on synthetic input | tests/unit, tests/integration |
| Platform | Agents (covenant, EWS, review memo, recovery) | **CONNECTED** | real runtime and guards; drafts come from templates because no LLM is attached | tests/unit/test_agents.py |
| Platform | Model gateway | **CONNECTED** | real policy engine with scripted providers in tests | tests/unit/test_model_gateway.py |
| ML | EWS and breach models | **CONNECTED** | LightGBM trained and evaluated on the synthetic book | reports/simulation_report.json |
| Data stores | Postgres / object storage | **SIMULATED** | in-memory and SQLite adapters behind ports | ADR 0002 |
| Data stores | Kafka / event bus | **SIMULATED** | in-process hash-chained event log with idempotent consumers | libs/events/bus.py |
| Data stores | Temporal, Neo4j, Feast | **NOT BUILT** | ports only | ADR 0002 |
| Security | Keys and signing | **SIMULATED** | LocalDevKeyProvider (software keys); HSM/KMS provider not built | libs/security/crypto.py |
| Security | SSO / OIDC / MFA, mTLS, SIEM | **NOT BUILT** | principals are constructed in code | docs/SECURITY_MODEL.md |
| People | Credit officers, committees, CAD checker, recovery head | **SIMULATED** | scripted principals, labelled '(simulated human)' in every timeline | sim/demo_story.py |
| External data | GST, Account Aggregator, bureaus, MCA/CERSAI, ratings, news | **SIMULATED** | 7 of 14 early-warning indicators have synthetic feeds; the rest are reported as not evaluable | libs/testing/synthetic, sim/book.py |
| External data | Borrower portal and document uploads | **SIMULATED** | documents are rendered by the synthetic generator | ocr/synthetic/render.py |
| UI / API | Web UI, REST API | **NOT BUILT** | domain services only | README |
