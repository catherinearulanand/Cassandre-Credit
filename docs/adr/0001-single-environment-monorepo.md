# ADR 0001: One Python environment for the monorepo, boundaries enforced by tests

**Status:** accepted · 19 Oct 2026

## Context
Build Guide §6 asks for each service to be an independent package in a `uv` workspace, with
services never importing each other. With seventeen services, a per-service `pyproject.toml`
multiplies dependency resolution and slows the inner loop on developer laptops (Windows, no `make`).

## Decision
- One `pyproject.toml` and one virtual environment. Source roots `libs/`, `services/`,
  `simulators/` and the repo root are on the import path.
- The isolation rules are enforced by architecture tests in `tests/architecture/`:
  services do not import each other (only `finacle_adapter.ports` is shared), libs never import
  services, provider SDKs only inside `model_gateway`, no `eval`/`exec`, no float literals in
  money domain code, no wall-clock reads in domain code, no invisible/bidi characters.
- `tools/cli.py` mirrors the Makefile targets because Windows workstations have no `make`.

## Alternatives considered
- Full `uv` workspace with one package per service. Kept as the target for when services are
  deployed separately (Phase 1 Helm charts). The split is mechanical because the boundaries
  already hold.

## Consequences
- Faster builds now. Boundary violations fail CI instead of failing packaging.
- A dependency added for one service is visible to all; `pip-audit` covers the union.
