"""Cases, proposals and decisions.

Agents and services *propose*; only a human with the required permission *decides*, through
maker-checker, producing a signed ApprovalRecord bound to the exact proposal payload. History
is append-only. SLA breaches are detected against an injected clock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from authz import ApprovalRecord, MakerChecker, Permission, Principal, ResourceScope
from domain.entities import CaseType
from platform_core.clock import Clock
from platform_core.errors import ConflictError, NotFoundError
from platform_core.ids import new_id

KIND_PERMISSION: dict[str, Permission] = {
    "consequence": Permission.CONSEQUENCE_APPROVE,
    "waiver": Permission.WAIVER_APPROVE,
    "letter": Permission.LETTER_SEND,
    "fraud_classification": Permission.RFA_CLASSIFY,
    "ots": Permission.OTS_APPROVE,
    "limit_change": Permission.LIMIT_CHANGE,
    "deviation_clearance": Permission.DOC_DEVIATION_CLEAR,
    "stage_change": Permission.CASE_DECIDE,
}


class CaseState(StrEnum):
    OPEN = "open"
    AWAITING_DECISION = "awaiting_decision"
    DECIDED = "decided"
    CLOSED = "closed"


@dataclass
class Proposal:
    proposal_id: str
    kind: str  # consequence | waiver | letter | fraud_classification | ots | stage_change ...
    payload: dict[str, Any]
    proposed_by: str
    proposer_kind: str  # human | agent | service
    required_permission: Permission
    on_behalf_of: str | None = None
    status: str = "pending"  # pending | approved | rejected
    approval: ApprovalRecord | None = None
    rejection_reason: str | None = None


@dataclass
class Case:
    case_id: str
    case_type: CaseType
    obligor_id: str
    scope: ResourceScope
    summary: str
    opened_at: datetime
    sla_due: datetime
    state: CaseState = CaseState.OPEN
    evidence: list[str] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    sla_breached: bool = False


class CaseService:
    def __init__(
        self,
        clock: Clock,
        maker_checker: MakerChecker,
        emit: Callable[[str, Case, dict[str, Any]], None] | None = None,
    ) -> None:
        self.clock = clock
        self.mc = maker_checker
        self.cases: dict[str, Case] = {}
        self._emit = emit or (lambda t, c, d: None)

    def open_case(
        self,
        case_type: CaseType,
        obligor_id: str,
        scope: ResourceScope,
        summary: str,
        *,
        sla_days: int,
        evidence: list[str] | None = None,
    ) -> Case:
        now = self.clock.now()
        case = Case(
            new_id("CASE"),
            case_type,
            obligor_id,
            scope,
            summary,
            now,
            now + timedelta(days=sla_days),
            evidence=list(evidence or []),
        )
        case.history.append({"at": now.isoformat(), "event": "opened", "summary": summary})
        self.cases[case.case_id] = case
        self._emit(
            "case.opened",
            case,
            {"case_id": case.case_id, "case_type": case_type.value, "summary": summary},
        )
        return case

    def get(self, case_id: str) -> Case:
        try:
            return self.cases[case_id]
        except KeyError as exc:
            raise NotFoundError(f"case {case_id}") from exc

    def propose(
        self,
        case_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
        proposed_by: str,
        proposer_kind: str,
        required_permission: Permission | None = None,
        on_behalf_of: str | None = None,
    ) -> Proposal:
        # The right needed to decide is a property of the KIND of proposal, not the proposer's choice.
        mapped = KIND_PERMISSION.get(kind)
        if mapped is None:
            raise ConflictError(f"unknown proposal kind {kind!r}")
        if required_permission is not None and required_permission != mapped:
            raise ConflictError("proposer cannot choose a different approval permission")
        required_permission = mapped
        case = self.get(case_id)
        if case.state in (CaseState.CLOSED,):
            raise ConflictError("case closed")
        if proposer_kind == "agent" and not on_behalf_of:
            raise ConflictError("an agent proposal must name the human it acts for")
        p = Proposal(
            new_id("REC"),
            kind,
            dict(payload),
            proposed_by,
            proposer_kind,
            required_permission,
            on_behalf_of,
        )
        case.proposals.append(p)
        case.state = CaseState.AWAITING_DECISION
        case.history.append(
            {
                "at": self.clock.now().isoformat(),
                "event": "proposal",
                "proposal_id": p.proposal_id,
                "kind": kind,
                "by": proposed_by,
            }
        )
        return p

    def decide(
        self, case_id: str, proposal_id: str, *, checker: Principal, approve: bool, reason: str
    ) -> Proposal:
        case = self.get(case_id)
        p = next((x for x in case.proposals if x.proposal_id == proposal_id), None)
        if p is None:
            raise NotFoundError(f"proposal {proposal_id}")
        if p.status != "pending":
            raise ConflictError(f"proposal already {p.status}")
        if approve:
            p.approval = self.mc.approve(
                subject=f"{case_id}:{proposal_id}",
                subject_payload=p.payload,
                action=p.kind,
                maker_id=p.proposed_by,
                checker=checker,
                required=p.required_permission,
                reason=reason,
                scope=case.scope,
                maker_on_behalf_of=p.on_behalf_of,
            )
            p.status = "approved"
        else:
            # rejection also needs authority, so a random user cannot block a proposal
            from authz import authorize

            authorize(checker, p.required_permission, case.scope)
            if not reason.strip():
                raise ConflictError("rejection requires a reason")
            p.status, p.rejection_reason = "rejected", reason
        case.history.append(
            {
                "at": self.clock.now().isoformat(),
                "event": "decision",
                "proposal_id": proposal_id,
                "approved": approve,
                "by": checker.id,
            }
        )
        if all(x.status != "pending" for x in case.proposals):
            case.state = CaseState.DECIDED
        self._emit(
            "case.decided",
            case,
            {
                "case_id": case_id,
                "case_type": case.case_type.value,
                "decision": "approved" if approve else "rejected",
                "approver": checker.id,
                "approval_id": p.approval.approval_id if p.approval else "",
            },
        )
        return p

    def close(self, case_id: str, *, by: Principal, reason: str) -> None:
        case = self.get(case_id)
        if any(p.status == "pending" for p in case.proposals):
            raise ConflictError("cannot close a case with pending proposals")
        case.state = CaseState.CLOSED
        case.history.append(
            {"at": self.clock.now().isoformat(), "event": "closed", "by": by.id, "reason": reason}
        )

    def check_sla(self) -> list[Case]:
        now = self.clock.now()
        breached = []
        for c in self.cases.values():
            if (
                c.state not in (CaseState.DECIDED, CaseState.CLOSED)
                and now > c.sla_due
                and not c.sla_breached
            ):
                c.sla_breached = True
                c.history.append({"at": now.isoformat(), "event": "sla_breached"})
                breached.append(c)
        return breached
