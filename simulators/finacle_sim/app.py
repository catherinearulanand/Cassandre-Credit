"""Finacle simulator.

Mimics the Finacle capabilities the platform needs: customers, accounts (balance, DPD,
days over limit), limits (tree nodes, freeze/cap), drawing power, covenant records,
disbursement instructions, and a CDC feed. Behaviour is scripted from YAML scenarios
(DPD progression, utilisation moves) and fault profiles (error rate, latency, outages)
so tests can exercise retries, circuit breaking, reconciliation drift and fail-closed paths.

Idempotency: every write takes ``Idempotency-Key``. A repeated key with the same body
returns the original response; a repeated key with a different body returns 409.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


@dataclass
class Faults:
    error_rate: float = 0.0
    latency_ms: int = 0
    outage: bool = False
    fail_next: int = 0
    drop_after_commit: int = 0  # commit, then lose the response (ambiguous failure)
    seed: int = 7
    _rng: random.Random = field(default_factory=lambda: random.Random(7))

    def check(self) -> None:
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        if self.outage:
            raise HTTPException(503, "finacle unavailable (simulated outage)")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise HTTPException(503, "simulated transient failure")
        if self.error_rate and self._rng.random() < self.error_rate:
            raise HTTPException(503, "simulated random failure")


@dataclass
class SimState:
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    limits: dict[str, dict[str, Any]] = field(default_factory=dict)
    covenant_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    disbursements: dict[str, dict[str, Any]] = field(default_factory=dict)
    idem: dict[str, tuple[str, Any]] = field(default_factory=dict)
    cdc: list[dict[str, Any]] = field(default_factory=list)
    day: int = 0
    faults: Faults = field(default_factory=Faults)
    write_count: int = 0

    def emit(self, kind: str, entity_id: str, data: dict[str, Any]) -> None:
        self.cdc.append(
            {
                "seq": len(self.cdc) + 1,
                "kind": kind,
                "entity_id": entity_id,
                "day": self.day,
                "data": data,
            }
        )

    # ---- scenario scripting --------------------------------------------------------
    def load_scenario(self, text: str) -> None:
        sc = yaml.safe_load(text)
        for c in sc.get("customers", []):
            self.customers[c["cif"]] = c
        for a in sc.get("accounts", []):
            a.setdefault("balance_paise", 0)
            a.setdefault("dpd", 0)
            a.setdefault("days_over_limit", 0)
            a.setdefault("status", "active")
            a.setdefault("dp_paise", a.get("sanctioned_paise", 0))
            self.accounts[a["account_id"]] = a
        self._steps = sorted(sc.get("steps", []), key=lambda s: s["day"])
        f = sc.get("faults", {})
        self.faults = Faults(
            error_rate=f.get("error_rate", 0.0),
            latency_ms=f.get("latency_ms", 0),
            outage=f.get("outage", False),
            fail_next=f.get("fail_next", 0),
            drop_after_commit=f.get("drop_after_commit", 0),
            _rng=random.Random(f.get("seed", 7)),
        )

    def advance(self, days: int = 1) -> None:
        for _ in range(days):
            self.day += 1
            for step in getattr(self, "_steps", []):
                if step["day"] == self.day:
                    acct = self.accounts[step["account_id"]]
                    acct.update(step["set"])
                    self.emit("account.updated", step["account_id"], dict(acct))
            # day-end: revolving accounts over limit/DP accrue days_over_limit
            for aid, a in self.accounts.items():
                if a.get("revolving"):
                    drawable = min(a.get("sanctioned_paise", 0), a.get("dp_paise", 0))
                    over = a["balance_paise"] > drawable
                    new = a["days_over_limit"] + 1 if over else 0
                    if new != a["days_over_limit"]:
                        a["days_over_limit"] = new
                        self.emit("account.updated", aid, dict(a))


def _body_hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def create_app(state: SimState | None = None) -> FastAPI:
    st = state or SimState()
    app = FastAPI(title="Finacle simulator", version="0.1.0")
    app.state.sim = st

    def idempotent(key: str | None, body: Any, fn: Any, validate: Any = None) -> Any:
        # The idempotency lookup MUST come first: a retry of a committed command has to
        # return the original result even if the account state has since changed.
        if not key:
            raise HTTPException(400, "Idempotency-Key header required")
        st.faults.check()  # an outage blocks even idempotent lookups, as a real one does
        h = _body_hash(body)
        if key in st.idem:
            prev_h, prev_resp = st.idem[key]
            if prev_h != h:
                raise HTTPException(409, "Idempotency-Key reused with different body")
            return {**prev_resp, "duplicate": True}
        if validate is not None:
            err = validate()
            if err is not None:
                return err
        resp = fn()
        st.write_count += 1
        st.idem[key] = (h, resp)
        if st.faults.drop_after_commit > 0:
            st.faults.drop_after_commit -= 1
            st.faults.outage = True  # the link died together with the response
            raise HTTPException(503, "response lost after commit (simulated)")
        return resp

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/accounts/{account_id}")
    def get_account(account_id: str) -> dict[str, Any]:
        st.faults.check()
        if account_id not in st.accounts:
            raise HTTPException(404, "account not found")
        return st.accounts[account_id]

    @app.get("/v1/accounts")
    def list_accounts() -> list[dict[str, Any]]:
        st.faults.check()
        return list(st.accounts.values())

    @app.put("/v1/limits/{node_id}")
    async def upsert_limit(
        node_id: str, request: Request, idempotency_key: str | None = Header(default=None)
    ) -> Any:
        body = await request.json()

        def do() -> dict[str, Any]:
            node = {"node_id": node_id, "status": "active", **body}
            st.limits[node_id] = node
            if body.get("account_id") in st.accounts:
                st.accounts[body["account_id"]]["sanctioned_paise"] = body["sanctioned_paise"]
            st.emit("limit.upserted", node_id, node)
            return {"status": "accepted", "finacle_ref": f"FLM{len(st.limits):06d}"}

        return idempotent(idempotency_key, body, do)

    @app.get("/v1/limits")
    def list_limits() -> list[dict[str, Any]]:
        st.faults.check()
        return list(st.limits.values())

    @app.post("/v1/limits/{node_id}/status")
    async def limit_status(
        node_id: str, request: Request, idempotency_key: str | None = Header(default=None)
    ) -> Any:
        body = await request.json()
        if node_id not in st.limits:
            raise HTTPException(404, "limit not found")

        def do() -> dict[str, Any]:
            st.limits[node_id]["status"] = body["status"]
            aid = st.limits[node_id].get("account_id")
            if aid in st.accounts:
                st.accounts[aid]["status"] = body["status"]
            st.emit("limit.status", node_id, {"status": body["status"]})
            return {"status": "accepted", "finacle_ref": f"FST{st.write_count:06d}"}

        return idempotent(idempotency_key, body, do)

    @app.put("/v1/accounts/{account_id}/dp")
    async def set_dp(
        account_id: str, request: Request, idempotency_key: str | None = Header(default=None)
    ) -> Any:
        body = await request.json()
        if account_id not in st.accounts:
            raise HTTPException(404, "account not found")

        def do() -> dict[str, Any]:
            st.accounts[account_id]["dp_paise"] = body["dp_paise"]
            st.emit("account.updated", account_id, dict(st.accounts[account_id]))
            return {"status": "accepted", "finacle_ref": f"FDP{st.write_count:06d}"}

        return idempotent(idempotency_key, body, do)

    @app.post("/v1/covenant-records")
    async def covenant_record(
        request: Request, idempotency_key: str | None = Header(default=None)
    ) -> Any:
        body = await request.json()

        def do() -> dict[str, Any]:
            st.covenant_records[body["test_id"]] = body
            return {"status": "accepted", "finacle_ref": f"FCV{len(st.covenant_records):06d}"}

        return idempotent(idempotency_key, body, do)

    @app.post("/v1/disbursements")
    async def disburse(request: Request, idempotency_key: str | None = Header(default=None)) -> Any:
        body = await request.json()
        acct = st.accounts.get(body["account_id"])
        if acct is None:
            raise HTTPException(404, "account not found")

        def validate() -> Any:
            if acct["status"] != "active":
                return JSONResponse(
                    {"status": "rejected", "reason": f"account {acct['status']}"}, 422
                )
            if not body.get("gate_decision_id"):
                return JSONResponse(
                    {"status": "rejected", "reason": "no drawdown gate decision"}, 422
                )
            if not isinstance(body.get("amount_paise"), int) or body["amount_paise"] <= 0:
                return JSONResponse({"status": "rejected", "reason": "invalid amount"}, 422)
            return None

        def do() -> dict[str, Any]:
            acct["balance_paise"] += body["amount_paise"]
            st.disbursements[body["request_id"]] = body
            st.emit("account.updated", body["account_id"], dict(acct))
            return {"status": "accepted", "finacle_ref": f"FDB{len(st.disbursements):06d}"}

        return idempotent(idempotency_key, body, do, validate)

    @app.get("/v1/disbursements/{request_id}")
    def get_disbursement(request_id: str) -> dict[str, Any]:
        st.faults.check()
        if request_id not in st.disbursements:
            raise HTTPException(404, "disbursement not found")
        return st.disbursements[request_id]

    @app.get("/v1/cdc")
    def cdc(after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        st.faults.check()
        return [e for e in st.cdc if e["seq"] > after][:limit]

    # ---- simulator control (not part of the Finacle surface) -----------------------
    @app.post("/sim/advance")
    def sim_advance(days: int = 1) -> dict[str, int]:
        st.advance(days)
        return {"day": st.day}

    @app.post("/sim/utilisation/{account_id}")
    def sim_utilise(account_id: str, balance_paise: int) -> dict[str, Any]:
        st.accounts[account_id]["balance_paise"] = balance_paise
        st.emit("account.updated", account_id, dict(st.accounts[account_id]))
        return st.accounts[account_id]

    @app.post("/sim/tamper/limit/{node_id}")
    def sim_tamper(node_id: str, sanctioned_paise: int) -> dict[str, Any]:
        """Simulate an out-of-band change made directly in Finacle (reconciliation catches it)."""
        st.limits[node_id]["sanctioned_paise"] = sanctioned_paise
        return st.limits[node_id]

    return app
