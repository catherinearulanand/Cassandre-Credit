"""Finacle adapter: HTTP client with resilience, command outbox, CDC ingestion, reconciliation.

Resilience (Section 13): per-call timeout, jittered exponential backoff, circuit breaker.
Commands are written to a durable outbox first; the dispatcher sends them with the command
id as Idempotency-Key, so a retry after an ambiguous failure can never double-apply.
Limit-changing commands without an approval id are refused at the adapter boundary.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from platform_core.clock import Clock
from platform_core.errors import AuthorizationError

from finacle_adapter.ports import CommandResult, Drift, FinacleCommand, ReconciliationReport

APPROVAL_REQUIRED = {"set_limit_status"}  # freeze/cap/close: decision-driven


class CircuitOpen(Exception):
    pass


class FinacleUnavailable(Exception):
    pass


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_after_s: float = 30.0
    clock_fn: Callable[[], float] = time.monotonic
    failures: int = 0
    opened_at: float | None = None

    def before(self) -> None:
        if self.opened_at is not None:
            if self.clock_fn() - self.opened_at < self.reset_after_s:
                raise CircuitOpen("finacle circuit open")
            self.opened_at = None  # half-open: let one call through
            self.failures = self.failure_threshold - 1

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self.clock_fn()


@dataclass
class HttpFinacleClient:
    """Production clients are built with httpx.Client(base_url=..., timeout=httpx.Timeout(2.0))."""

    client: httpx.Client
    max_retries: int = 4
    base_backoff_s: float = 0.05
    sleep: Callable[[float], None] = time.sleep
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    rng: random.Random = field(default_factory=lambda: random.Random(11))

    def request(
        self, method: str, path: str, *, json_body: Any = None, idempotency_key: str | None = None
    ) -> tuple[int, Any, int]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        attempt = 0
        while True:
            attempt += 1
            self.breaker.before()
            try:
                r = self.client.request(method, path, json=json_body, headers=headers)
            except httpx.HTTPError as exc:
                self.breaker.failure()
                if attempt > self.max_retries:
                    raise FinacleUnavailable(str(exc)) from exc
            else:
                if r.status_code >= 500:
                    self.breaker.failure()
                    if attempt > self.max_retries:
                        raise FinacleUnavailable(f"HTTP {r.status_code}")
                else:
                    self.breaker.success()
                    return r.status_code, (r.json() if r.content else None), attempt
            # full jitter backoff
            self.sleep(self.rng.uniform(0, self.base_backoff_s * (2 ** (attempt - 1))))

    def get(self, path: str) -> Any:
        status, body, _ = self.request("GET", path)
        if status >= 400:
            raise FinacleUnavailable(f"GET {path} -> {status}")
        return body


_ROUTES: dict[str, tuple[str, str]] = {
    "upsert_limit": ("PUT", "/v1/limits/{target}"),
    "set_limit_status": ("POST", "/v1/limits/{target}/status"),
    "set_dp": ("PUT", "/v1/accounts/{target}/dp"),
    "covenant_record": ("POST", "/v1/covenant-records"),
    "disburse": ("POST", "/v1/disbursements"),
}


class CommandOutbox:
    """Durable command queue (SQLite here, Postgres in shared envs)."""

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS commands (command_id TEXT PRIMARY KEY, body TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
            "finacle_ref TEXT, detail TEXT)"
        )

    def submit(self, cmd: FinacleCommand) -> None:
        if cmd.command_type in APPROVAL_REQUIRED and not cmd.approval_id:
            raise AuthorizationError(
                f"{cmd.command_type} requires an approval record", code="approval_required"
            )
        payload = json.dumps(
            {
                "command_id": cmd.command_id,
                "command_type": cmd.command_type,
                "target": cmd.target,
                "body": cmd.body,
                "correlation_id": cmd.correlation_id,
                "approval_id": cmd.approval_id,
            }
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO commands(command_id, body) VALUES (?, ?)",
            (cmd.command_id, payload),
        )

    def pending(self) -> list[FinacleCommand]:
        rows = self.conn.execute(
            "SELECT body FROM commands WHERE status IN ('pending','retry') ORDER BY rowid"
        ).fetchall()
        return [FinacleCommand(**json.loads(r[0])) for r in rows]

    def mark(self, res: CommandResult) -> None:
        self.conn.execute(
            "UPDATE commands SET status=?, attempts=attempts+?, finacle_ref=?, detail=? "
            "WHERE command_id=?",
            (res.status, res.attempts, res.finacle_ref, res.detail, res.command_id),
        )

    def status(self, command_id: str) -> str:
        row = self.conn.execute(
            "SELECT status FROM commands WHERE command_id=?", (command_id,)
        ).fetchone()
        return str(row[0]) if row else "unknown"


class Dispatcher:
    def __init__(
        self,
        outbox: CommandOutbox,
        client: HttpFinacleClient,
        on_ack: Callable[[CommandResult, FinacleCommand], None] | None = None,
    ) -> None:
        self.outbox = outbox
        self.client = client
        self.on_ack = on_ack

    def run_once(self) -> list[CommandResult]:
        results = []
        for cmd in self.outbox.pending():
            method, path = _ROUTES[cmd.command_type]
            body = dict(cmd.body)
            if cmd.approval_id:
                body["approval_id"] = cmd.approval_id
            try:
                status, resp, attempts = self.client.request(
                    method,
                    path.format(target=cmd.target),
                    json_body=body,
                    idempotency_key=cmd.command_id,
                )
            except (FinacleUnavailable, CircuitOpen) as exc:
                res = CommandResult(cmd.command_id, "failed", detail=str(exc), attempts=1)
                self.outbox.conn.execute(
                    "UPDATE commands SET status='retry', attempts=attempts+1, detail=? "
                    "WHERE command_id=?",
                    (str(exc), cmd.command_id),
                )
                results.append(res)
                continue
            if status < 400:
                st = "duplicate" if resp.get("duplicate") else "accepted"
                res = CommandResult(cmd.command_id, st, resp.get("finacle_ref"), attempts=attempts)  # type: ignore[arg-type]
            else:
                detail = (
                    resp.get("reason") or resp.get("detail")
                    if isinstance(resp, dict)
                    else str(resp)
                )
                res = CommandResult(
                    cmd.command_id, "rejected", detail=str(detail), attempts=attempts
                )
            self.outbox.mark(res)
            if self.on_ack:
                self.on_ack(res, cmd)
            results.append(res)
        return results


class CdcIngestor:
    """Polls Finacle CDC and maintains the platform's read projection of accounts."""

    def __init__(self, client: HttpFinacleClient) -> None:
        self.client = client
        self.cursor = 0
        self.accounts: dict[str, dict[str, Any]] = {}
        self.handlers: list[Callable[[dict[str, Any]], None]] = []

    def poll(self) -> int:
        events = self.client.get(f"/v1/cdc?after={self.cursor}")
        for e in events:
            if e["kind"] == "account.updated":
                self.accounts[e["entity_id"]] = e["data"]
            for h in self.handlers:
                h(e)
            self.cursor = e["seq"]
        return len(events)


class Reconciler:
    """Nightly: compare platform intent with Finacle state. Drift opens a case upstream."""

    def __init__(self, client: HttpFinacleClient, clock: Clock) -> None:
        self.client = client
        self.clock = clock

    def reconcile(
        self, expected_limits: dict[str, dict[str, Any]], expected_dp: dict[str, int] | None = None
    ) -> ReconciliationReport:
        rep = ReconciliationReport()
        actual = {n["node_id"]: n for n in self.client.get("/v1/limits")}
        for node_id, exp in expected_limits.items():
            rep.checked += 1
            act = actual.get(node_id)
            if act is None:
                rep.drifts.append(Drift(node_id, "exists", True, False))
                continue
            for f in ("sanctioned_paise", "status"):
                if f in exp and exp[f] != act.get(f):
                    rep.drifts.append(Drift(node_id, f, exp[f], act.get(f)))
        for node_id in set(actual) - set(expected_limits):
            rep.drifts.append(Drift(node_id, "exists", False, True))
        for acct_id, dp in (expected_dp or {}).items():
            rep.checked += 1
            act_acct = self.client.get(f"/v1/accounts/{acct_id}")
            if act_acct.get("dp_paise") != dp:
                rep.drifts.append(Drift(acct_id, "dp_paise", dp, act_acct.get("dp_paise")))
        return rep
