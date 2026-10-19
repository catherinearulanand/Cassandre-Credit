"""Phase 0 gate: Finacle round-trip.

Limit tree published in the platform appears in the simulator via the adapter; a
utilisation change in the simulator arrives via CDC and updates the platform projection;
reconciliation reports zero drift; an out-of-band change is caught as drift.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from finacle_adapter.adapter import (
    CdcIngestor,
    CircuitBreaker,
    CircuitOpen,
    CommandOutbox,
    Dispatcher,
    FinacleUnavailable,
    HttpFinacleClient,
    Reconciler,
)
from finacle_adapter.ports import FinacleCommand
from finacle_sim.app import SimState, create_app
from platform_core.errors import AuthorizationError

SCENARIO = """
customers:
  - { cif: CIF0001, name: Saffron Synthetic Textiles Pvt Ltd }
accounts:
  - { account_id: CC-0001, cif: CIF0001, facility_id: FAC-CC, revolving: true, sanctioned_paise: 0, balance_paise: 0 }
  - { account_id: TL-0001, cif: CIF0001, facility_id: FAC-TL, revolving: false, sanctioned_paise: 0, balance_paise: 0 }
steps:
  - { day: 3, account_id: TL-0001, set: { dpd: 12 } }
  - { day: 5, account_id: TL-0001, set: { dpd: 35 } }
"""

TREE = {
    "OBL-NODE": {"sanctioned_paise": 75_00_00_000_00, "parent_id": None},
    "LIM-CC": {
        "sanctioned_paise": 25_00_00_000_00,
        "parent_id": "OBL-NODE",
        "account_id": "CC-0001",
    },
    "LIM-TL": {
        "sanctioned_paise": 50_00_00_000_00,
        "parent_id": "OBL-NODE",
        "account_id": "TL-0001",
    },
}


@pytest.fixture
def env():  # type: ignore[no-untyped-def]
    st = SimState()
    st.load_scenario(SCENARIO)
    tc = TestClient(create_app(st))
    client = HttpFinacleClient(tc, sleep=lambda s: None)
    return st, tc, client


def _publish_tree(client: HttpFinacleClient) -> tuple[CommandOutbox, list]:  # type: ignore[type-arg]
    ob = CommandOutbox()
    acks: list = []  # type: ignore[type-arg]
    for node_id, body in TREE.items():
        ob.submit(
            FinacleCommand(f"CMD-upsert-{node_id}-v1", "upsert_limit", node_id, body, "corr-1")
        )
    Dispatcher(ob, client, on_ack=lambda r, c: acks.append(r)).run_once()
    return ob, acks


def test_round_trip_gate(env) -> None:  # type: ignore[no-untyped-def]
    st, tc, client = env
    ob, acks = _publish_tree(client)
    assert all(a.status == "accepted" for a in acks) and len(acks) == 3
    assert st.limits["LIM-CC"]["sanctioned_paise"] == TREE["LIM-CC"]["sanctioned_paise"]
    assert st.accounts["CC-0001"]["sanctioned_paise"] == TREE["LIM-CC"]["sanctioned_paise"]

    ingest = CdcIngestor(client)
    ingest.poll()
    tc.post("/sim/utilisation/CC-0001", params={"balance_paise": 20_00_00_000_00})
    assert ingest.poll() >= 1
    assert ingest.accounts["CC-0001"]["balance_paise"] == 20_00_00_000_00

    tc.post("/sim/advance", params={"days": 5})
    ingest.poll()
    assert ingest.accounts["TL-0001"]["dpd"] == 35

    rep = Reconciler(client, clock=None).reconcile(  # type: ignore[arg-type]
        {
            k: {"sanctioned_paise": v["sanctioned_paise"], "status": "active"}
            for k, v in TREE.items()
        }
    )
    assert rep.clean and rep.checked == 3

    tc.post("/sim/tamper/limit/LIM-TL", params={"sanctioned_paise": 60_00_00_000_00})
    rep2 = Reconciler(client, clock=None).reconcile(  # type: ignore[arg-type]
        {k: {"sanctioned_paise": v["sanctioned_paise"]} for k, v in TREE.items()}
    )
    assert not rep2.clean and rep2.drifts[0].entity == "LIM-TL"


def test_replayed_command_is_not_double_applied(env) -> None:  # type: ignore[no-untyped-def]
    st, tc, client = env
    _publish_tree(client)
    writes = st.write_count
    ob = CommandOutbox()
    ob.submit(
        FinacleCommand("CMD-upsert-LIM-CC-v1", "upsert_limit", "LIM-CC", TREE["LIM-CC"], "corr-1")
    )
    res = Dispatcher(ob, client).run_once()
    assert res[0].status == "duplicate" and st.write_count == writes


def test_idempotency_key_reuse_with_different_body_rejected(env) -> None:  # type: ignore[no-untyped-def]
    _, tc, _ = env
    h = {"Idempotency-Key": "K1"}
    assert tc.put("/v1/limits/X", json={"sanctioned_paise": 1}, headers=h).status_code == 200
    assert tc.put("/v1/limits/X", json={"sanctioned_paise": 2}, headers=h).status_code == 409


def test_transient_failures_are_retried(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client = env
    st.faults.fail_next = 2
    _, acks = _publish_tree(client)
    assert acks[0].status == "accepted" and acks[0].attempts == 3


def test_outage_leaves_commands_pending_then_recovers(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client = env
    st.faults.outage = True
    ob = CommandOutbox()
    ob.submit(FinacleCommand("CMD-dp-1", "set_dp", "CC-0001", {"dp_paise": 5}, "c"))
    res = Dispatcher(ob, client).run_once()
    assert res[0].status == "failed" and ob.status("CMD-dp-1") == "retry"
    st.faults.outage = False
    client.breaker = CircuitBreaker()
    res = Dispatcher(ob, client).run_once()
    assert res[0].status == "accepted" and st.accounts["CC-0001"]["dp_paise"] == 5


def test_circuit_breaker_opens() -> None:
    t = [0.0]
    cb = CircuitBreaker(failure_threshold=2, reset_after_s=10, clock_fn=lambda: t[0])
    cb.failure()
    cb.failure()
    with pytest.raises(CircuitOpen):
        cb.before()
    t[0] = 11
    cb.before()  # half-open


def test_limit_status_change_requires_approval() -> None:
    ob = CommandOutbox()
    with pytest.raises(AuthorizationError):
        ob.submit(
            FinacleCommand("CMD-frz", "set_limit_status", "LIM-CC", {"status": "frozen"}, "c")
        )
    ob.submit(
        FinacleCommand(
            "CMD-frz", "set_limit_status", "LIM-CC", {"status": "frozen"}, "c", approval_id="APR-1"
        )
    )


def test_disbursement_requires_gate_decision(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client = env
    _publish_tree(client)
    ob = CommandOutbox()
    ob.submit(
        FinacleCommand(
            "CMD-d1",
            "disburse",
            "-",
            {"request_id": "DRW-1", "account_id": "CC-0001", "amount_paise": 100},
            "c",
        )
    )
    res = Dispatcher(ob, client).run_once()
    assert res[0].status == "rejected"
    assert st.accounts["CC-0001"]["balance_paise"] == 0


def test_unavailable_raises_after_retries(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client = env
    st.faults.outage = True
    with pytest.raises((FinacleUnavailable, CircuitOpen)):
        client.get("/v1/limits")
