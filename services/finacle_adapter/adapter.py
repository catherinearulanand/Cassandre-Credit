"""Finacle adapter: HTTP client with resilience, command outbox, CDC ingestion, reconciliation.

Resilience (Section 13): per-call timeout, jittered exponential backoff, circuit breaker.
Commands are written to a durable outbox first; the dispatcher sends them with the command
id as Idempotency-Key, so a retry after an ambiguous failure can never double-apply.
Limit-changing commands without an approval id are refused at the adapter boundary.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from authz import Permission, Principal, authorize
from platform_core.clock import Clock
from platform_core.errors import AuthorizationError, ConflictError, ValidationFailure

from finacle_adapter.ports import CommandResult, Drift, FinacleCommand, ReconciliationReport

# Every command that changes limits, drawing power or money needs an approval bound to its exact
# body AND its command id (a signed ApprovalRecord). A disbursement additionally needs a signed
# drawdown-gate APPROVE for exactly this request.
APPROVAL_REQUIRED = {"upsert_limit", "set_limit_status", "set_dp", "disburse"}
MAX_ATTEMPTS = 10
MAX_AMOUNT_PAISE = 10**14  # sanity ceiling (Rs 1,000 crore per single command)
RETRYABLE_STATUS = {408, 425, 429}
LIMIT_STATUSES = {"active", "frozen", "capped", "closed"}
BLOCKING = ("dead", "needs_reconciliation")
ALLOWED_KEYS: dict[str, set[str]] = {
    "upsert_limit": {"sanctioned_paise", "parent_id", "account_id"},
    "set_limit_status": {"status"},
    "set_dp": {"dp_paise"},
    "disburse": {
        "request_id",
        "account_id",
        "amount_paise",
        "payee_id",
        "gate_decision",
        "gate_decision_id",
    },
}

log = logging.getLogger(__name__)
CommandVerifier = Callable[[FinacleCommand], bool]


def command_subject(cmd: FinacleCommand) -> dict[str, Any]:
    """What an approval is bound to: this command id, type, target and exact body.

    Binding the command id means one approval cannot be replayed under fresh command ids."""
    return {
        "command_id": cmd.command_id,
        "correlation_id": cmd.correlation_id,
        "command_type": cmd.command_type,
        "target": cmd.target,
        "body": cmd.body,
    }


def order_key(cmd: FinacleCommand) -> str:
    """Commands that can affect the same account are applied strictly in order."""
    if cmd.command_type == "disburse":
        return str(cmd.body.get("account_id", cmd.target))
    return cmd.target


def _validate_command(cmd: FinacleCommand) -> None:
    allowed = ALLOWED_KEYS.get(cmd.command_type)
    if allowed is not None:
        extra = set(cmd.body) - allowed
        if extra:
            raise ValidationFailure(f"unexpected fields {sorted(extra)}", code="bad_body")
    if cmd.command_type == "set_limit_status" and cmd.body.get("status") not in LIMIT_STATUSES:
        raise ValidationFailure("unknown limit status", code="bad_body")
    if cmd.command_type == "covenant_record" and not cmd.body.get("test_id"):
        raise ValidationFailure("covenant record needs a test_id", code="bad_body")
    if (
        cmd.command_type == "disburse"
        and cmd.body.get("account_id") != cmd.target
        and cmd.target != "-"
    ):
        raise ValidationFailure("disbursement target does not match its account", code="bad_body")
    key = {
        "disburse": "amount_paise",
        "set_dp": "dp_paise",
        "upsert_limit": "sanctioned_paise",
    }.get(cmd.command_type)
    if key is None:
        return
    v = cmd.body.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValidationFailure(f"{key} must be an integer number of paise", code="bad_amount")
    if v < 0 or (cmd.command_type == "disburse" and v == 0) or v > MAX_AMOUNT_PAISE:
        raise ValidationFailure(f"{key} out of range", code="bad_amount")


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
    probing: bool = False

    def before(self) -> None:
        if self.opened_at is not None:
            if self.probing or self.clock_fn() - self.opened_at < self.reset_after_s:
                raise CircuitOpen("finacle circuit open")
            self.probing = True  # half-open: exactly one probe call goes through

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.probing = False

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold or self.probing:
            self.opened_at = self.clock_fn()
            self.probing = False


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
                if r.status_code >= 500 or r.status_code in RETRYABLE_STATUS:
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
    """Durable command queue (SQLite here, Postgres in shared envs).

    States: pending -> inflight -> accepted | duplicate | rejected | retry | needs_reconciliation | dead.
    ``needs_reconciliation`` and ``dead`` BLOCK their order key until a human resolves them, so a
    later command can never overtake an earlier one whose outcome is unknown.
    """

    def __init__(
        self,
        path: str = ":memory:",
        *,
        verifier: CommandVerifier | None = None,
        gate_verifier: CommandVerifier | None = None,
        allow_unverified: bool = False,
    ) -> None:
        self.verifier = verifier
        self.gate_verifier = gate_verifier
        self.allow_unverified = allow_unverified
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS commands (command_id TEXT PRIMARY KEY, body TEXT NOT NULL, "
            "okey TEXT NOT NULL, dedupe TEXT UNIQUE, approval_id TEXT, "
            "status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
            "finacle_ref TEXT, detail TEXT)"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_use ON commands(approval_id) "
            "WHERE approval_id IS NOT NULL"
        )

    # ---- admission ---------------------------------------------------------------------
    def _check_approvals(self, cmd: FinacleCommand) -> None:
        if cmd.command_type not in APPROVAL_REQUIRED:
            return
        if not cmd.approval_id:
            raise AuthorizationError(
                f"{cmd.command_type} requires an approval record", code="approval_required"
            )
        for name, verifier in (("approval", self.verifier), ("gate decision", self.gate_verifier)):
            if name == "gate decision" and cmd.command_type != "disburse":
                continue
            if verifier is None:
                if not self.allow_unverified:
                    raise AuthorizationError(
                        f"no {name} verifier configured; refusing unverifiable {name}",
                        code="approval_unverifiable",
                    )
            elif not verifier(cmd):
                raise AuthorizationError(
                    f"{name} does not verify against this exact command", code="approval_invalid"
                )

    def submit(self, cmd: FinacleCommand) -> None:
        _validate_command(cmd)
        self._check_approvals(cmd)
        if cmd.command_type == "disburse":
            gd = cmd.body.get("gate_decision") or {}
            sig = str(gd.get("signature", {}).get("signature_b64", cmd.approval_id))
            cmd = replace(cmd, body={**cmd.body, "gate_decision_id": f"GD-{sig[:16]}"})
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
        row = self.conn.execute(
            "SELECT body FROM commands WHERE command_id=?", (cmd.command_id,)
        ).fetchone()
        if row is not None:
            if json.loads(row[0]) != json.loads(payload):
                raise ConflictError(f"command {cmd.command_id} resubmitted with a different body")
            return
        dedupe = f"disburse:{cmd.body['request_id']}" if cmd.command_type == "disburse" else None
        try:
            self.conn.execute(
                "INSERT INTO commands(command_id, body, okey, dedupe, approval_id) VALUES (?,?,?,?,?)",
                (cmd.command_id, payload, order_key(cmd), dedupe, cmd.approval_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "duplicate disbursement request or approval already used by another command"
            ) from exc

    # ---- queue -------------------------------------------------------------------------
    def pending(self) -> list[FinacleCommand]:
        rows = self.conn.execute(
            "SELECT body FROM commands WHERE status IN ('pending','retry','inflight') ORDER BY rowid"
        ).fetchall()
        return [FinacleCommand(**json.loads(r[0])) for r in rows]

    def blocked_keys(self) -> set[str]:
        rows = self.conn.execute(
            f"SELECT DISTINCT okey FROM commands WHERE status IN ({','.join('?' * len(BLOCKING))})",  # noqa: S608  # nosec B608 - only a count of "?" placeholders is interpolated
            BLOCKING,
        ).fetchall()
        return {r[0] for r in rows}

    def attempts(self, command_id: str) -> int:
        row = self.conn.execute(
            "SELECT attempts FROM commands WHERE command_id=?", (command_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def begin(self, command_id: str) -> None:
        """Record intent to send BEFORE sending, so a crash is treated as ambiguous."""
        self.conn.execute(
            "UPDATE commands SET status='inflight', attempts=attempts+1 WHERE command_id=?",
            (command_id,),
        )

    def unbegin(self, command_id: str) -> None:
        """The call never left the process (open circuit): it does not count as an attempt."""
        self.conn.execute(
            "UPDATE commands SET status='retry', attempts=MAX(0, attempts-1) WHERE command_id=?",
            (command_id,),
        )

    def mark(self, res: CommandResult) -> None:
        self.conn.execute(
            "UPDATE commands SET status=?, finacle_ref=?, detail=? WHERE command_id=?",
            (res.status, res.finacle_ref, res.detail, res.command_id),
        )

    def status(self, command_id: str) -> str:
        row = self.conn.execute(
            "SELECT status FROM commands WHERE command_id=?", (command_id,)
        ).fetchone()
        return str(row[0]) if row else "unknown"

    def resolve(self, principal: Principal, command_id: str, outcome: str, note: str) -> None:
        """A human settles a blocked command after checking Finacle.

        outcome: ``applied`` (Finacle shows it took effect), ``not_applied`` (it did not and must
        not be sent), or ``requeue`` (send it again; Finacle's idempotency key makes that safe).
        """
        authorize(principal, Permission.LIMIT_CHANGE)
        if principal.kind != "human" or not note.strip():
            raise AuthorizationError("resolution needs a human and a written note")
        if self.status(command_id) not in BLOCKING:
            raise ConflictError("only dead or needs_reconciliation commands can be resolved")
        new = {"applied": "accepted", "not_applied": "rejected", "requeue": "retry"}.get(outcome)
        if new is None:
            raise ValidationFailure("outcome must be applied, not_applied or requeue")
        attempts = "0" if outcome == "requeue" else "attempts"
        self.conn.execute(
            f"UPDATE commands SET status=?, detail=?, attempts={attempts} WHERE command_id=?",  # noqa: S608  # nosec B608 - attempts is the literal "0" or "attempts"
            (new, f"resolved by {principal.id}: {outcome}: {note}", command_id),
        )


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

    def _ack(self, res: CommandResult, cmd: FinacleCommand) -> None:
        if self.on_ack is None:
            return
        try:
            self.on_ack(res, cmd)
        except Exception:  # a failing listener must not stop the queue
            log.exception("on_ack listener failed for %s", cmd.command_id)

    def run_once(self) -> list[CommandResult]:
        results: list[CommandResult] = []
        blocked: set[str] = self.outbox.blocked_keys()
        for cmd in self.outbox.pending():
            key = order_key(cmd)
            if key in blocked:
                continue
            prior = self.outbox.attempts(cmd.command_id)
            if prior >= MAX_ATTEMPTS:
                res = CommandResult(cmd.command_id, "dead", detail="max attempts reached")
                self.outbox.mark(res)
                self._ack(res, cmd)
                results.append(res)
                blocked.add(key)
                continue
            # re-verify at dispatch: an approval revoked or tampered after submit must not run
            if cmd.command_type in APPROVAL_REQUIRED and self.outbox.verifier is not None:
                stripped = replace(
                    cmd, body={k: v for k, v in cmd.body.items() if k != "gate_decision_id"}
                )
                if not self.outbox.verifier(stripped):
                    res = CommandResult(
                        cmd.command_id, "rejected", detail="approval no longer valid"
                    )
                    self.outbox.mark(res)
                    self._ack(res, cmd)
                    results.append(res)
                    continue
            method, path = _ROUTES[cmd.command_type]
            body = dict(cmd.body)
            if cmd.approval_id:
                body["approval_id"] = cmd.approval_id
            self.outbox.begin(cmd.command_id)
            try:
                status, resp, attempts = self.client.request(
                    method,
                    path.format(target=cmd.target),
                    json_body=body,
                    idempotency_key=cmd.command_id,
                )
            except CircuitOpen as exc:
                self.outbox.unbegin(cmd.command_id)
                results.append(CommandResult(cmd.command_id, "failed", detail=str(exc)))
                blocked.add(key)
                continue
            except FinacleUnavailable as exc:
                self.outbox.conn.execute(
                    "UPDATE commands SET status='retry', detail=? WHERE command_id=?",
                    (str(exc), cmd.command_id),
                )
                results.append(CommandResult(cmd.command_id, "failed", detail=str(exc), attempts=1))
                blocked.add(key)
                continue
            resp = resp if isinstance(resp, dict) else {}
            if status < 400:
                st = "duplicate" if resp.get("duplicate") else "accepted"
                res = CommandResult(cmd.command_id, st, resp.get("finacle_ref"), attempts=attempts)  # type: ignore[arg-type]
            else:
                detail = str(resp.get("reason") or resp.get("detail") or status)
                # Only a first, single-try rejection is a real rejection. If this command was ever
                # sent before (even inside one request's retries) the first try may have committed.
                ambiguous = prior > 0 or attempts > 1
                res = CommandResult(
                    cmd.command_id,
                    "needs_reconciliation" if ambiguous else "rejected",
                    detail=detail,
                    attempts=attempts,
                )
                if ambiguous:
                    blocked.add(key)
            self.outbox.mark(res)
            self._ack(res, cmd)
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
