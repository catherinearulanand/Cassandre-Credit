"""Finacle for the demonstration: the real adapter talking to the Finacle SIMULATOR.

Nothing here reaches a real Finacle. The simulator (``simulators/finacle_sim``) is an in-process
FastAPI app that mimics Finacle's behaviour (idempotency keys, CDC feed, faults). The adapter, the
outbox, the signed approvals and the signed gate decisions are the production code paths.
"""

from __future__ import annotations

from typing import Any, Literal

from authz import MakerChecker, Permission, Principal, Role
from drawdown_gate.domain import DrawdownRequest, GateDecision
from drawdown_gate.signing import sign_decision, verify_decision
from fastapi.testclient import TestClient
from finacle_adapter.adapter import (
    CdcIngestor,
    CommandOutbox,
    Dispatcher,
    HttpFinacleClient,
    command_subject,
)
from finacle_adapter.ports import FinacleCommand
from finacle_sim.app import SimState, create_app
from platform_core.clock import Clock
from security.crypto import LocalDevKeyProvider, Signer

MONEY_KEYS = ("amount_paise", "dp_paise", "sanctioned_paise")


class FinacleDemo:
    def __init__(self, clock: Clock, scenario_yaml: str) -> None:
        self.state = SimState()
        self.state.load_scenario(scenario_yaml)
        self.http = TestClient(create_app(self.state))
        self.client = HttpFinacleClient(self.http, sleep=lambda s: None)
        self.signer = Signer(LocalDevKeyProvider())
        self.mc = MakerChecker(clock, self.signer)
        self.approver = Principal(
            id="sa-demo",
            kind="human",
            roles=frozenset({Role.SANCTIONING_AUTHORITY}),
            delegated_power_paise=10**15,
        )
        self.outbox = CommandOutbox(verifier=self._verify, gate_verifier=self._gate_verify)
        self.cdc = CdcIngestor(self.client)
        self.acks: list[Any] = []
        self._n = 0

    # ---- approvals: a human (simulated) signs each command; the adapter re-verifies it ---------
    def _approve(self, cmd: FinacleCommand) -> FinacleCommand:
        amount = next((int(cmd.body[k]) for k in MONEY_KEYS if k in cmd.body), 0)
        rec = self.mc.approve(
            subject=cmd.command_id,
            subject_payload=command_subject(cmd),
            action=cmd.command_type,
            maker_id="platform",
            checker=self.approver,
            required=Permission.LIMIT_CHANGE,
            reason="approved in demonstration",
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

    def _verify(self, cmd: FinacleCommand) -> bool:
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
        except Exception:  # noqa: BLE001 - any failure means "not approved"
            return False

    def _gate_verify(self, cmd: FinacleCommand) -> bool:
        gd = cmd.body.get("gate_decision")
        return isinstance(gd, dict) and verify_decision(
            gd,
            self.signer,
            request_id=cmd.body["request_id"],
            amount_paise=cmd.body["amount_paise"],
        )

    def _send(
        self, ctype: Literal["upsert_limit", "disburse"], target: str, body: dict[str, Any]
    ) -> list[Any]:
        self._n += 1
        cmd = self._approve(FinacleCommand(f"CMD-demo-{self._n}", ctype, target, body, "corr-demo"))
        self.outbox.submit(cmd)
        acks = Dispatcher(
            self.outbox, self.client, on_ack=lambda r, c: self.acks.append(r)
        ).run_once()
        return list(acks)

    def publish_limit(self, node_id: str, sanctioned_paise: int, account_id: str) -> str:
        acks = self._send(
            "upsert_limit",
            node_id,
            {"sanctioned_paise": sanctioned_paise, "account_id": account_id},
        )
        return str(acks[0].status)

    def disburse(
        self, decision: GateDecision, req: DrawdownRequest, account_id: str
    ) -> tuple[str, str | None]:
        """Only a signed gate APPROVE can reach the adapter; the adapter re-verifies it."""
        signed = sign_decision(decision, req, self.signer)
        body = {
            "request_id": req.request_id,
            "account_id": account_id,
            "amount_paise": req.amount_paise,
            "gate_decision": signed,
        }
        acks = self._send("disburse", account_id, body)
        return str(acks[0].status), getattr(acks[0], "finacle_ref", None)

    def balance(self, account_id: str) -> int:
        self.cdc.poll()
        return int(self.cdc.accounts[account_id]["balance_paise"])


def scenario(cif: str, name: str, cc_id: str, tl_id: str) -> str:
    return f"""
customers:
  - {{ cif: {cif}, name: {name} }}
accounts:
  - {{ account_id: {cc_id}, cif: {cif}, facility_id: FAC-CC, revolving: true, sanctioned_paise: 0, balance_paise: 0 }}
  - {{ account_id: {tl_id}, cif: {cif}, facility_id: FAC-TL, revolving: false, sanctioned_paise: 0, balance_paise: 0 }}
steps: []
"""
