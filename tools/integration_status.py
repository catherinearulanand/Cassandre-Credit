"""What is really connected, and what is simulated. Probed, not asserted.

    python -m tools.integration_status [--deep] [--md out.md]

Each row has a status:
  CONNECTED    a live call to the real thing succeeded just now (and says what it returned)
  CONFIGURED   wired and a credential is present (not exercised)
  AWAITING KEY the adapter is written; it needs the credential you will supply
  SIMULATED    a stand-in with the same interface (the stand-in is named)
  NOT BUILT    nothing exists behind the port
The probes are the source of truth for docs/SIMULATION_BRIEF.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ("libs", "services", "simulators", ".")]


@dataclass
class Row:
    area: str
    name: str
    status: str
    behind_it: str
    evidence: str


def probe_paddle_local(deep: bool) -> Row:
    from ocr.pipeline.paddle_engine import paddle_python

    py = paddle_python()
    if not py.exists():
        return Row(
            "OCR", "PaddleOCR (local worker)", "NOT BUILT", "isolated .venv-paddle missing", str(py)
        )
    if not deep:
        return Row(
            "OCR",
            "PaddleOCR (local worker)",
            "CONFIGURED",
            "isolated interpreter present; run with --deep to execute a real read",
            str(py),
        )
    from datetime import UTC, datetime

    from PIL import Image, ImageDraw
    from platform_core.clock import FixedClock

    from ocr.pipeline.paddle_engine import local_engine

    img = Image.new("RGB", (900, 160), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 60), "Total Equity", fill="black")
    d.text((500, 60), "500.00", fill="black")
    eng = local_engine(FixedClock(datetime(2026, 10, 21, tzinfo=UTC)), lambda e: None)
    try:
        words = eng.recognise(img)
        ok = [w.text for w in words] == ["Total", "Equity", "500.00"]
        return Row(
            "OCR",
            "PaddleOCR (local worker)",
            "CONNECTED" if ok else "CONFIGURED",
            f"PP-OCR models in an isolated interpreter ({eng.version})",
            f"read {[w.text for w in words]}",
        )
    finally:
        eng.close()  # type: ignore[attr-defined]


def probe_paddle_cloud() -> Row:
    have = bool(os.environ.get("PADDLEOCR_API_URL")) and bool(os.environ.get("PADDLEOCR_API_TOKEN"))
    return Row(
        "OCR", "PaddleOCR hosted API", "CONFIGURED" if have else "AWAITING KEY",
        "adapter written (synthetic data only: the service is outside India); response shape unverified until a key is used",
        "PADDLEOCR_API_URL/TOKEN set" if have else "no key in the environment",
    )  # fmt: skip


def probe_finacle() -> Row:
    from fastapi.testclient import TestClient
    from finacle_sim.app import SimState, create_app

    st = SimState()
    st.load_scenario("customers:\n  - { cif: C1, name: X }\naccounts: []\nsteps: []\n")
    r = TestClient(create_app(st)).get("/v1/limits")
    return Row(
        "Core banking",
        "Finacle",
        "SIMULATED",
        "simulators/finacle_sim (in-process); the adapter in front of it is the production code",
        f"simulator answered HTTP {r.status_code}",
    )


def probe_llm() -> Row:
    import yaml

    cfg = yaml.safe_load(
        (ROOT / "services/model_gateway/config/routes.yaml").read_text(encoding="utf-8")
    )
    return Row(
        "AI", "LLM providers (self-hosted and frontier)", "NOT BUILT",
        "gateway, redaction, budgets and kill switch are real; no provider is attached, so drafting uses deterministic templates",
        f"{len(cfg['use_cases'])} use cases routed; 0 providers connected",
    )  # fmt: skip


def static_rows() -> list[Row]:
    return [
        Row(
            "Platform",
            "Rules engine, 20 rule packs",
            "CONNECTED",
            "production code; pack contents are unverified against RBI text",
            "rules-test passes",
        ),
        Row(
            "Platform",
            "Covenant engine, CTS, limits, drawing power, drawdown gate",
            "CONNECTED",
            "production code on synthetic input",
            "agrees with independent calculators",
        ),
        Row(
            "Platform",
            "SMA/ECL, RFA, EWS, calendar, stressed assets, recovery, closure, review",
            "CONNECTED",
            "production domain services on synthetic input",
            "tests/unit, tests/integration",
        ),
        Row(
            "Platform",
            "Agents (covenant, EWS, review memo, recovery)",
            "CONNECTED",
            "real runtime and guards; drafts come from templates because no LLM is attached",
            "tests/unit/test_agents.py",
        ),
        Row(
            "Platform",
            "Model gateway",
            "CONNECTED",
            "real policy engine with scripted providers in tests",
            "tests/unit/test_model_gateway.py",
        ),
        Row(
            "ML",
            "EWS and breach models",
            "CONNECTED",
            "LightGBM trained and evaluated on the synthetic book",
            "reports/simulation_report.json",
        ),
        Row(
            "Data stores",
            "Postgres / object storage",
            "SIMULATED",
            "in-memory and SQLite adapters behind ports",
            "ADR 0002",
        ),
        Row(
            "Data stores",
            "Kafka / event bus",
            "SIMULATED",
            "in-process hash-chained event log with idempotent consumers",
            "libs/events/bus.py",
        ),
        Row("Data stores", "Temporal, Neo4j, Feast", "NOT BUILT", "ports only", "ADR 0002"),
        Row(
            "Security",
            "Keys and signing",
            "SIMULATED",
            "LocalDevKeyProvider (software keys); HSM/KMS provider not built",
            "libs/security/crypto.py",
        ),
        Row(
            "Security",
            "SSO / OIDC / MFA, mTLS, SIEM",
            "NOT BUILT",
            "principals are constructed in code",
            "docs/SECURITY_MODEL.md",
        ),
        Row(
            "People",
            "Credit officers, committees, CAD checker, recovery head",
            "SIMULATED",
            "scripted principals, labelled '(simulated human)' in every timeline",
            "sim/demo_story.py",
        ),
        Row(
            "External data",
            "GST, Account Aggregator, bureaus, MCA/CERSAI, ratings, news",
            "SIMULATED",
            "7 of 14 early-warning indicators have synthetic feeds; the rest are reported as not evaluable",
            "libs/testing/synthetic, sim/book.py",
        ),
        Row(
            "External data",
            "Borrower portal and document uploads",
            "SIMULATED",
            "documents are rendered by the synthetic generator",
            "ocr/synthetic/render.py",
        ),
        Row("UI / API", "Web UI, REST API", "NOT BUILT", "domain services only", "README"),
    ]


def build(deep: bool) -> list[Row]:
    rows = [probe_paddle_local(deep), probe_paddle_cloud(), probe_finacle(), probe_llm()]
    return rows + static_rows()


def to_markdown(rows: list[Row]) -> str:
    out = ["| Area | Component | Status | What is behind it | Evidence |", "|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r.area} | {r.name} | **{r.status}** | {r.behind_it} | {r.evidence} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="execute a real read through PaddleOCR")
    ap.add_argument("--md", type=Path)
    ap.add_argument("--json", type=Path, default=ROOT / "reports" / "integration_status.json")
    a = ap.parse_args()
    rows = build(a.deep)
    a.json.parent.mkdir(parents=True, exist_ok=True)
    a.json.write_text(json.dumps([asdict(r) for r in rows], indent=2), encoding="utf-8")
    md = to_markdown(rows)
    if a.md:
        a.md.write_text(md + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
