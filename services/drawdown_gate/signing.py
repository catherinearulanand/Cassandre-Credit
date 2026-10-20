"""Signed gate decisions. A disbursement may only be released against a decision that this gate
signed for exactly this request, facility, amount and payee, and that says APPROVE."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from platform_core.canonical import content_hash
from security.crypto import Signature, Signer

from drawdown_gate.domain import DrawdownRequest, GateDecision


def _body(req: DrawdownRequest, decision: str, checks: list[str]) -> dict[str, Any]:
    return {
        "request_id": req.request_id,
        "facility_id": req.facility_id,
        "amount_paise": req.amount_paise,
        "payee_id": req.payee_id,
        "decision": decision,
        "checks": checks,
    }


def sign_decision(decision: GateDecision, req: DrawdownRequest, signer: Signer) -> dict[str, Any]:
    body = _body(req, decision.decision, [f"{c.name}:{c.outcome}" for c in decision.checks])
    sig = signer.sign_hash(content_hash(body))
    return {**body, "signature": asdict(sig)}


def verify_decision(
    d: dict[str, Any],
    signer: Signer,
    *,
    request_id: str,
    amount_paise: int,
    facility_id: str | None = None,
) -> bool:
    """True only for an authentic APPROVE for this request and amount."""
    try:
        sig = Signature(**d["signature"])
        body = {k: v for k, v in d.items() if k != "signature"}
        signer.verify(sig, content_hash(body))
    except Exception:  # noqa: BLE001 - any failure to verify means "not approved"
        return False
    return (
        body.get("decision") == "APPROVE"
        and body.get("request_id") == request_id
        and body.get("amount_paise") == amount_paise
        and (facility_id is None or body.get("facility_id") == facility_id)
        and len(body.get("checks", [])) == 10
    )
