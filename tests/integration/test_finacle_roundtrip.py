"""Phase 0 gate: Finacle round-trip, plus regression tests for every audited failure mode.

Limit tree published in the platform appears in the simulator via the adapter; a utilisation
change in the simulator arrives via CDC and updates the platform projection; reconciliation
reports zero drift; an out-of-band change is caught as drift. Under retries, lost responses,
outages, replays and forged approvals the adapter never double-applies, never reorders, never
reports money as un-moved, and never releases money without a signed gate APPROVE.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from authz import MakerChecker, Permission, Principal, Role
from drawdown_gate.domain import CHECKS, CheckResult, DrawdownRequest, GateDecision
from drawdown_gate.signing import sign_decision, verify_decision
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
    command_subject,
)
from finacle_adapter.ports import FinacleCommand
from finacle_sim.app import SimState, create_app
from platform_core.clock import FixedClock
from platform_core.errors import AuthorizationError, ConflictError, ValidationFailure
from security.crypto import LocalDevKeyProvider, Signer

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
    "OBL-NODE": {"sanctioned_paise": 75_00_00_000_00},
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
MONEY_KEYS = ("amount_paise", "dp_paise", "sanctioned_paise")


class Approvals:
    """Issues signed approvals bound to the exact command, and signed gate decisions."""

    def __init__(self) -> None:
        clock = FixedClock(datetime(2026, 10, 19, tzinfo=UTC))
        self.signer = Signer(LocalDevKeyProvider())
        self.mc = MakerChecker(clock, self.signer)
        self.checker = Principal(
            id="sa1",
            kind="human",
            roles=frozenset({Role.SANCTIONING_AUTHORITY}),
            delegated_power_paise=10**15,
        )

    def approve(self, cmd: FinacleCommand) -> FinacleCommand:
        amount = next((int(cmd.body[k]) for k in MONEY_KEYS if k in cmd.body), 0)
        rec = self.mc.approve(
            subject=cmd.command_id,
            subject_payload=command_subject(cmd),
            action=cmd.command_type,
            maker_id="platform",
            checker=self.checker,
            required=Permission.LIMIT_CHANGE,
            reason="approved for test",
            amount_paise=amount,
        )
        return FinacleCommand(
            cmd.command_id,
            cmd.command_type,
            cmd.target,
            cmd.body,
            cmd.correlation_id,
            rec.approval_id,
        )

    def gate_decision(
        self, request_id: str, account: str, amount: int, decision: str = "APPROVE"
    ) -> dict[str, Any]:
        req = DrawdownRequest(request_id, account, amount, payee_id="SUP-1")
        checks = tuple(CheckResult(n, "pass", "ok") for n in CHECKS)
        d = GateDecision(request_id, decision, (), checks, 0.1)  # type: ignore[arg-type]
        return sign_decision(d, req, self.signer)

    def verify(self, cmd: FinacleCommand) -> bool:
        stripped = FinacleCommand(
            cmd.command_id,
            cmd.command_type,
            cmd.target,
            {k: v for k, v in cmd.body.items() if k != "gate_decision_id"},
            cmd.correlation_id,
            cmd.approval_id,
        )
        try:
            self.mc.verify_id(
                cmd.approval_id or "", command_subject(stripped), required=Permission.LIMIT_CHANGE
            )
            return True
        except Exception:  # noqa: BLE001 - any verification failure means "not approved"
            return False

    def gate_verify(self, cmd: FinacleCommand) -> bool:
        gd = cmd.body.get("gate_decision")
        return bool(gd) and verify_decision(
            gd,
            self.signer,
            request_id=cmd.body["request_id"],
            amount_paise=cmd.body["amount_paise"],
        )

    def outbox(self) -> CommandOutbox:
        return CommandOutbox(verifier=self.verify, gate_verifier=self.gate_verify)


@pytest.fixture
def env():  # type: ignore[no-untyped-def]
    st = SimState()
    st.load_scenario(SCENARIO)
    tc = TestClient(create_app(st))
    client = HttpFinacleClient(tc, sleep=lambda s: None)
    ap = Approvals()
    return st, tc, client, ap, ap.outbox()


def _cmd(ap: Approvals, ctype: str, target: str, body: dict[str, Any], cid: str) -> FinacleCommand:
    return ap.approve(FinacleCommand(cid, ctype, target, body, "corr-1"))


def _disburse(
    ap: Approvals, cid: str, request_id: str, amount: int, account: str = "CC-0001"
) -> FinacleCommand:
    body = {
        "request_id": request_id,
        "account_id": account,
        "amount_paise": amount,
        "gate_decision": ap.gate_decision(request_id, account, amount),
    }
    return _cmd(ap, "disburse", account, body, cid)


def _publish_tree(ap, ob, client):  # type: ignore[no-untyped-def]
    acks = []
    for node_id, body in TREE.items():
        ob.submit(_cmd(ap, "upsert_limit", node_id, body, f"CMD-upsert-{node_id}-v1"))
    Dispatcher(ob, client, on_ack=lambda r, c: acks.append(r)).run_once()
    return acks


def test_round_trip_gate(env) -> None:  # type: ignore[no-untyped-def]
    st, tc, client, ap, ob = env
    acks = _publish_tree(ap, ob, client)
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

    expected = {k: {"sanctioned_paise": v["sanctioned_paise"]} for k, v in TREE.items()}
    rep = Reconciler(client, clock=None).reconcile(expected)  # type: ignore[arg-type]
    assert rep.clean and rep.checked == 3

    tc.post("/sim/tamper/limit/LIM-TL", params={"sanctioned_paise": 60_00_00_000_00})
    rep2 = Reconciler(client, clock=None).reconcile(expected)  # type: ignore[arg-type]
    assert not rep2.clean and rep2.drifts[0].entity == "LIM-TL"


def test_replayed_command_is_not_double_applied(env) -> None:  # type: ignore[no-untyped-def]
    st, tc, client, ap, ob = env
    original = _cmd(ap, "upsert_limit", "LIM-CC", TREE["LIM-CC"], "CMD-upsert-LIM-CC-v1")
    ob.submit(original)
    Dispatcher(ob, client).run_once()
    writes = st.write_count
    ob2 = ap.outbox()
    ob2.submit(original)  # crash recovery re-sends the identical signed command
    res = Dispatcher(ob2, client).run_once()
    assert res[0].status == "duplicate" and st.write_count == writes


def test_idempotency_key_reuse_with_different_body_rejected(env) -> None:  # type: ignore[no-untyped-def]
    _, tc, *_ = env
    h = {"Idempotency-Key": "K1"}
    assert tc.put("/v1/limits/X", json={"sanctioned_paise": 1}, headers=h).status_code == 200
    assert tc.put("/v1/limits/X", json={"sanctioned_paise": 2}, headers=h).status_code == 409


def test_transient_failures_are_retried(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, ap, ob = env
    st.faults.fail_next = 2
    acks = _publish_tree(ap, ob, client)
    assert acks[0].status == "accepted" and acks[0].attempts == 3


def test_outage_leaves_commands_pending_then_recovers(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, ap, ob = env
    st.faults.outage = True
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 5}, "CMD-dp-1"))
    res = Dispatcher(ob, client).run_once()
    assert res[0].status == "failed" and ob.status("CMD-dp-1") == "retry"
    st.faults.outage = False
    client.breaker = CircuitBreaker()
    res = Dispatcher(ob, client).run_once()
    assert res[0].status == "accepted" and st.accounts["CC-0001"]["dp_paise"] == 5


def test_circuit_breaker_opens_and_admits_one_probe() -> None:
    t = [0.0]
    cb = CircuitBreaker(failure_threshold=2, reset_after_s=10, clock_fn=lambda: t[0])
    cb.failure()
    cb.failure()
    with pytest.raises(CircuitOpen):
        cb.before()
    t[0] = 11
    cb.before()  # the single half-open probe
    with pytest.raises(CircuitOpen):
        cb.before()  # a concurrent caller is still refused
    cb.failure()  # probe failed: re-open
    with pytest.raises(CircuitOpen):
        cb.before()
    t[0] = 25
    cb.before()
    cb.success()
    cb.before()  # closed again


# ---------------------------------------------------------------- audit regressions ------
def test_F1_stale_command_cannot_overwrite_newer_one(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, ap, ob = env
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 800}, "CMD-1"))
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 500}, "CMD-2"))
    st.faults.fail_next = 5  # CMD-1 exhausts its retries
    Dispatcher(ob, client).run_once()
    assert st.accounts["CC-0001"]["dp_paise"] == 0, "CMD-2 must not jump the queue"
    st.faults.fail_next = 0
    client.breaker = CircuitBreaker()
    Dispatcher(ob, client).run_once()
    assert st.accounts["CC-0001"]["dp_paise"] == 500, "older DP must never win"


def test_F2_lost_response_after_commit_is_not_reported_as_rejected(env) -> None:  # type: ignore[no-untyped-def]
    st, tc, client, ap, ob = env
    _publish_tree(ap, ob, client)
    st.faults.drop_after_commit = 1
    ob.submit(_disburse(ap, "CMD-d1", "DRW-1", 700))
    r1 = Dispatcher(ob, client).run_once()
    assert r1[0].status == "failed" and st.accounts["CC-0001"]["balance_paise"] == 700
    # the account is now frozen; the retry must still resolve to the original success
    st.faults.outage = False
    tc.post(
        "/v1/limits/LIM-CC/status", json={"status": "frozen"}, headers={"Idempotency-Key": "frz"}
    )
    client.breaker = CircuitBreaker()
    r2 = Dispatcher(ob, client).run_once()
    assert r2[0].status == "duplicate", r2
    assert st.accounts["CC-0001"]["balance_paise"] == 700, "never disbursed twice"


def test_N3_rejection_after_an_earlier_try_is_ambiguous_not_rejected(env) -> None:  # type: ignore[no-untyped-def]
    st, tc, client, ap, ob = env
    _publish_tree(ap, ob, client)
    st.faults.drop_after_commit = 1
    ob.submit(_disburse(ap, "CMD-d1", "DRW-1", 700))
    Dispatcher(ob, client).run_once()  # committed, response lost, outage
    st.faults.outage = False
    st.idem.clear()  # Finacle forgot the key (e.g. retention window): now a 4xx is ambiguous
    tc.post(
        "/v1/limits/LIM-CC/status", json={"status": "frozen"}, headers={"Idempotency-Key": "frz"}
    )
    client.breaker = CircuitBreaker()
    res = Dispatcher(ob, client).run_once()
    assert res[0].status == "needs_reconciliation", res
    # and it BLOCKS later commands on the same account until a human resolves it
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 9}, "CMD-dp-later"))
    assert Dispatcher(ob, client).run_once() == []
    assert st.accounts["CC-0001"]["dp_paise"] == 0
    human = Principal(id="sa1", kind="human", roles=frozenset({Role.SANCTIONING_AUTHORITY}))
    ob.resolve(human, "CMD-d1", "applied", "Finacle shows balance +700 for DRW-1")
    res2 = Dispatcher(ob, client).run_once()
    assert res2[0].status == "accepted" and st.accounts["CC-0001"]["dp_paise"] == 9


def test_N1_one_approval_cannot_be_replayed_under_new_command_ids(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, ap, ob = env
    _publish_tree(ap, ob, client)
    original = _disburse(ap, "D-1", "DRW-1", 100)
    ob.submit(original)
    for i in range(3):
        replay = FinacleCommand(
            f"D-replay-{i}",
            "disburse",
            original.target,
            original.body,
            original.correlation_id,
            original.approval_id,
        )
        with pytest.raises(AuthorizationError):
            ob.submit(replay)
    Dispatcher(ob, client).run_once()
    assert st.accounts["CC-0001"]["balance_paise"] == 100


def test_N1_same_disbursement_request_cannot_be_approved_twice(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, ap, ob = env
    ob.submit(_disburse(ap, "D-1", "DRW-1", 100))
    with pytest.raises(ConflictError):
        ob.submit(_disburse(ap, "D-2", "DRW-1", 100))  # fresh approval, same request id


def test_N4_disbursement_needs_a_signed_gate_approve_for_this_request(env) -> None:  # type: ignore[no-untyped-def]
    _, _, _, ap, ob = env
    body = {"request_id": "DRW-1", "account_id": "CC-0001", "amount_paise": 100}
    with pytest.raises(AuthorizationError):  # no gate decision at all
        ob.submit(_cmd(ap, "disburse", "CC-0001", body, "D-1"))
    rejected = {**body, "gate_decision": ap.gate_decision("DRW-1", "CC-0001", 100, "REJECT")}
    with pytest.raises(AuthorizationError):  # gate said REJECT
        ob.submit(_cmd(ap, "disburse", "CC-0001", rejected, "D-2"))
    other_amount = {**body, "gate_decision": ap.gate_decision("DRW-1", "CC-0001", 50)}
    with pytest.raises(AuthorizationError):  # approved a different amount
        ob.submit(_cmd(ap, "disburse", "CC-0001", other_amount, "D-3"))
    tampered = ap.gate_decision("DRW-1", "CC-0001", 100)
    tampered["request_id"] = "DRW-OTHER"
    with pytest.raises(AuthorizationError):  # altered after signing
        ob.submit(_cmd(ap, "disburse", "CC-0001", {**body, "gate_decision": tampered}, "D-4"))


def test_N2_dead_commands_block_their_account_and_circuit_open_is_not_an_attempt(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, ap, ob = env
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 800}, "CMD-1"))
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 9}, "CMD-2"))
    ob.conn.execute("UPDATE commands SET attempts=10 WHERE command_id='CMD-1'")
    acks: list[str] = []
    res = Dispatcher(ob, client, on_ack=lambda r, c: acks.append(r.status)).run_once()
    assert res[0].status == "dead" and acks == ["dead"], "dead commands raise an alert"
    assert Dispatcher(ob, client).run_once() == []
    assert st.accounts["CC-0001"]["dp_paise"] == 0, "CMD-2 must not overtake the dead CMD-1"
    # an open circuit does not burn attempts
    st.faults.outage = True
    ob2 = ap.outbox()
    ob2.submit(_cmd(ap, "set_dp", "TL-0001", {"dp_paise": 1}, "CMD-3"))
    d = Dispatcher(ob2, client)
    for _ in range(12):
        d.run_once()
    assert ob2.attempts("CMD-3") < 10 and ob2.status("CMD-3") != "dead"


def test_F3_every_money_or_limit_command_needs_a_verified_approval() -> None:
    ap = Approvals()
    ob = ap.outbox()
    bare = FinacleCommand("C1", "upsert_limit", "LIM-X", {"sanctioned_paise": 10**9}, "c")
    with pytest.raises(AuthorizationError):
        ob.submit(bare)
    good = ap.approve(FinacleCommand("C3", "set_dp", "CC-0001", {"dp_paise": 100}, "c"))
    forged = FinacleCommand("C3", "set_dp", "CC-0001", {"dp_paise": 10**12}, "c", good.approval_id)
    with pytest.raises(AuthorizationError):  # same approval, larger body
        ob.submit(forged)
    ob.submit(good)
    with pytest.raises(AuthorizationError):  # no verifier configured: refuse, do not trust strings
        CommandOutbox().submit(good)


@pytest.mark.parametrize(
    "ctype,key,val",
    [
        ("disburse", "amount_paise", -5 * 10**13),
        ("disburse", "amount_paise", 0),
        ("disburse", "amount_paise", 10**30),
        ("set_dp", "dp_paise", -1),
        ("upsert_limit", "sanctioned_paise", True),
        ("disburse", "amount_paise", 1.5),
    ],
)
def test_F3_bad_amounts_rejected(ctype: str, key: str, val: object) -> None:
    ap = Approvals()
    ob = ap.outbox()
    body: dict[str, Any] = {"request_id": "R", "account_id": "A", key: val}
    if ctype != "disburse":
        body = {key: val}
    with pytest.raises((ValidationFailure, AuthorizationError)):
        ob.submit(FinacleCommand("C", ctype, "A", body, "c", approval_id="x"))  # type: ignore[arg-type]


def test_N15_unexpected_fields_and_unknown_statuses_rejected() -> None:
    ap = Approvals()
    ob = ap.outbox()
    with pytest.raises(ValidationFailure):
        ob.submit(
            ap.approve(
                FinacleCommand(
                    "C", "set_dp", "CC-0001", {"dp_paise": 1, "account_id": "TL-0001"}, "c"
                )
            )
        )
    with pytest.raises(ValidationFailure):
        ob.submit(
            ap.approve(
                FinacleCommand("C2", "set_limit_status", "LIM-CC", {"status": "explode"}, "c")
            )
        )
    with pytest.raises(ValidationFailure):
        ob.submit(
            ap.approve(
                FinacleCommand(
                    "C3",
                    "disburse",
                    "TL-0001",
                    {
                        "request_id": "R",
                        "account_id": "CC-0001",
                        "amount_paise": 5,
                        "gate_decision": ap.gate_decision("R", "CC-0001", 5),
                    },
                    "c",
                )
            )
        )


def test_F21_retryable_statuses_empty_bodies_and_resubmission(env) -> None:  # type: ignore[no-untyped-def]
    st, tc, client, ap, ob = env
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 5}, "CMD-x"))
    with pytest.raises(ConflictError):
        ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 6}, "CMD-x"))

    class Flaky:
        def __init__(self) -> None:
            self.n = 0

        def request(self, method, path, json=None, headers=None):  # type: ignore[no-untyped-def]
            self.n += 1
            if self.n < 3:
                return httpx.Response(429, json={"detail": "slow down"})
            return httpx.Response(204)

    fc = HttpFinacleClient(Flaky(), sleep=lambda s: None)  # type: ignore[arg-type]
    status, body, attempts = fc.request("PUT", "/x", json_body={}, idempotency_key="k")
    assert (status, attempts) == (204, 3)
    ob2 = ap.outbox()
    ob2.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 7}, "CMD-empty"))
    res = Dispatcher(ob2, fc).run_once()  # 204 / empty body must not crash the dispatcher
    assert res[0].status in ("accepted", "failed")


def test_N17_a_failing_listener_does_not_stop_the_queue(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, ap, ob = env
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 1}, "CMD-a"))
    ob.submit(_cmd(ap, "set_dp", "TL-0001", {"dp_paise": 2}, "CMD-b"))

    def boom(*_: Any) -> None:
        raise RuntimeError("listener bug")

    res = Dispatcher(ob, client, on_ack=boom).run_once()
    assert [r.status for r in res] == ["accepted", "accepted"]


def test_N18_approval_is_rechecked_at_dispatch(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, ap, ob = env
    ob.submit(_cmd(ap, "set_dp", "CC-0001", {"dp_paise": 5}, "CMD-a"))
    ap.mc.records.clear()  # approval revoked/lost between submit and dispatch
    res = Dispatcher(ob, client).run_once()
    assert res[0].status == "rejected" and st.accounts["CC-0001"]["dp_paise"] == 0


def test_unavailable_raises_after_retries(env) -> None:  # type: ignore[no-untyped-def]
    st, _, client, *_ = env
    st.faults.outage = True
    with pytest.raises((FinacleUnavailable, CircuitOpen)):
        client.get("/v1/limits")
