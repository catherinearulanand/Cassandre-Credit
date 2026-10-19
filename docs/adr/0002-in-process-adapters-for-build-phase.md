# ADR 0002: In-process adapters behind ports during the build phase

**Status:** accepted · 19 Oct 2026

## Context
The guide's production stack is Postgres, Redpanda/Kafka, Neo4j, Temporal, Feast and MinIO
(§5.1). The build machine runs Windows without those services running, and the first goal
is to prove the domain logic, controls and accuracy on simulated data.

## Decision
Every infrastructure dependency sits behind a port. The build ships these adapters:

| Port | Build adapter | Production adapter (pending) |
|---|---|---|
| Event backbone | `events.bus.Broker` + `EventLog` (hash-chained) | Redpanda/Kafka producer/consumer |
| Transactional outbox | SQLite (`SqliteOutbox`, `CommandOutbox`) | Postgres, same schema |
| Bitemporal store | `platform_core.bitemporal.BitemporalStore` | Postgres tables with the §7.8 columns |
| Long-running clocks | deterministic state machines driven by `FixedClock` | Temporal workflows |
| Feature store | in-memory snapshot cache | Feast + Redis |
| Finacle | `simulators/finacle_sim` over HTTP (FastAPI TestClient) | real Finacle APIs |
| Key management | `LocalDevKeyProvider` | HSM/KMS `KeyProvider` |

`docker-compose.yml` defines the production-like infrastructure for integration work.

## Consequences
- Tests are fast and deterministic. The semantics (idempotency, ordering, at-least-once,
  hash chains, bitemporal reads) are pinned by tests that the production adapters must also pass.
- Production adapters are real work still to do; they are listed as open items in the README.
