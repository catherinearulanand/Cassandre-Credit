"""Event catalogue (Section 7.5): one payload model per event type and major version.

JSON Schemas are exported to ``contracts/events/<type>.v<N>.json`` and diffed in CI by
``events.compat``. Payload models forbid unknown fields on *produce* so producers cannot
drift silently; consumers read with ``extra='ignore'`` semantics (tolerant reader).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SanctionApproved(Payload):
    proposal_id: str
    approver: str
    document_ids: list[str]


class CtsDrafted(Payload):
    cts_id: str
    version: int
    low_confidence_fields: list[str]
    drafted_by: str


class CtsPublished(Payload):
    cts_id: str
    version: int
    content_hash: str
    signature_key_id: str
    approver: str
    approval_id: str


class CtsAmended(Payload):
    cts_id: str
    old_version: int
    new_version: int
    change_set: list[dict[str, Any]]
    amendment_sanction_ref: str


class LimitChanged(Payload):
    node_id: str
    old_sanctioned_paise: int
    new_sanctioned_paise: int
    reason: str
    cause_event_id: str | None = None
    status: Literal["active", "frozen", "capped", "closed"] = "active"


class DpChanged(Payload):
    facility_id: str
    old_dp_paise: int
    new_dp_paise: int
    inputs: dict[str, str]


class CollateralRevalued(Payload):
    collateral_id: str
    value_paise: int
    method: str
    valuer: str


class PerfectionOverdue(Payload):
    collateral_id: str
    item: Literal["CERSAI", "CHG-1", "NeSL", "insurance_bank_clause", "title_search", "valuation"]
    due_date: str
    days_overdue: int
    rule_id: str
    rule_version: str


class DocumentReceived(Payload):
    document_id: str
    sha256: str
    source: str
    claimed_type: str | None = None
    size_bytes: int


class DocumentExtracted(Payload):
    document_id: str
    doc_type: str
    fields: dict[str, Any]
    confidence: float
    review_status: str
    pipeline_version: str
    model_versions: dict[str, str]


class SpreadCompleted(Payload):
    spread_id: str
    period_end: str
    period_months: int
    basis: str
    line_count: int
    exceptions_signed_off_by: str | None = None


class ObligationDue(Payload):
    obligation_id: str
    doc_type: str
    due_date: str


class ObligationOverdue(Payload):
    obligation_id: str
    doc_type: str
    due_date: str
    days_overdue: int
    chase_action: str


class CovenantTested(Payload):
    test_id: str
    covenant_id: str
    period_end: str
    value: str | None
    threshold: str | None
    operator: str | None
    result: str
    inputs: dict[str, str]


class CovenantBreached(CovenantTested):
    consequences: list[str]
    case_id: str


class DrawdownRequested(Payload):
    request_id: str
    facility_id: str
    amount_paise: int
    purpose: str | None = None


class DrawdownGated(Payload):
    request_id: str
    facility_id: str
    amount_paise: int
    decision: Literal["APPROVE", "REJECT", "REFER"]
    reasons: list[str]
    checks: list[dict[str, Any]]
    latency_ms: float


class SignalRaised(Payload):
    signal_id: str
    signal_type: str
    severity: str
    score: float
    explanation: str
    evidence: list[str]


class EwsScoreChanged(Payload):
    score: float
    horizon_months: int
    pd_equivalent: float
    reasons: list[dict[str, Any]]
    model_version: str


class SmaChanged(Payload):
    account_id: str
    old_bucket: str
    new_bucket: str
    dpd: int
    days_over_limit: int
    rule_version: str


class RfaOpened(Payload):
    case_id: str
    triggering_indicators: list[str]


class StageChanged(Payload):
    old_stage: int
    new_stage: int
    reasons: list[str]


class CaseOpened(Payload):
    case_id: str
    case_type: str
    summary: str


class CaseDecided(Payload):
    case_id: str
    case_type: str
    decision: str
    approver: str
    approval_id: str


class AgentRunCompleted(Payload):
    run_id: str
    agent_id: str
    human_principal: str
    tool_calls: list[dict[str, Any]]
    output_ref: str
    denied_calls: int = 0


class AccountClosed(Payload):
    facility_ids: list[str]
    zero_dues_proof: str
    release_actions: list[str]


class FinacleAck(Payload):
    command_id: str
    command_type: str
    status: Literal["accepted", "rejected", "duplicate"]
    finacle_ref: str | None = None


class LlmCallLogged(Payload):
    call_id: str
    use_case: str
    model: str
    provider: str
    data_class: str
    redacted: bool
    tokens_in: int
    tokens_out: int
    latency_ms: float
    outcome: str


CATALOGUE: dict[tuple[str, int], type[Payload]] = {
    ("sanction.approved", 1): SanctionApproved,
    ("cts.drafted", 1): CtsDrafted,
    ("cts.published", 1): CtsPublished,
    ("cts.amended", 1): CtsAmended,
    ("limit.changed", 1): LimitChanged,
    ("dp.changed", 1): DpChanged,
    ("collateral.revalued", 1): CollateralRevalued,
    ("perfection.overdue", 1): PerfectionOverdue,
    ("document.received", 1): DocumentReceived,
    ("document.extracted", 1): DocumentExtracted,
    ("spread.completed", 1): SpreadCompleted,
    ("obligation.due", 1): ObligationDue,
    ("obligation.overdue", 1): ObligationOverdue,
    ("covenant.tested", 1): CovenantTested,
    ("covenant.breached", 1): CovenantBreached,
    ("drawdown.requested", 1): DrawdownRequested,
    ("drawdown.gated", 1): DrawdownGated,
    ("signal.raised", 1): SignalRaised,
    ("ews.score.changed", 1): EwsScoreChanged,
    ("sma.changed", 1): SmaChanged,
    ("rfa.opened", 1): RfaOpened,
    ("stage.changed", 1): StageChanged,
    ("case.opened", 1): CaseOpened,
    ("case.decided", 1): CaseDecided,
    ("agent.run.completed", 1): AgentRunCompleted,
    ("account.closed", 1): AccountClosed,
    ("finacle.ack", 1): FinacleAck,
    ("llm.call.logged", 1): LlmCallLogged,
}


class UnknownEventType(ValueError):
    pass


def validate_payload(event_type: str, version: int, data: dict[str, Any]) -> dict[str, Any]:
    model = CATALOGUE.get((event_type, version))
    if model is None:
        raise UnknownEventType(f"{event_type} v{version} is not in the catalogue")
    return model.model_validate(data).model_dump(mode="json")
