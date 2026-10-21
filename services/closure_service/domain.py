"""Closure and release of security documents.

Closure is allowed only with proof of zero dues on every facility, no pending Finacle command, no
open drawdown and no outstanding contingent exposure (LC, BG). The proof is a hash over exactly the
facts checked, so an auditor can re-derive it. The release clock and any compensation come from
``rbi.document_release``; a delay not attributable to the lender must say why. Closing is a human
decision through the case port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Any

from authz import ApprovalRecord, MakerChecker, Permission
from domain.ports import CasePort
from events.catalogue import AccountClosed
from platform_core.canonical import content_hash
from platform_core.clock import Clock
from platform_core.errors import ConflictError, ValidationFailure
from rules_engine import RuleRegistry

RELEASE_ACTIONS = (
    "release_original_documents",
    "file_charge_satisfaction",
    "update_cersai_satisfaction",
    "remove_lien_marking",
    "remove_insurance_bank_clause",
    "return_post_dated_instruments",
)


@dataclass(frozen=True)
class ClosureFacts:
    obligor_id: str
    facility_ids: tuple[str, ...]
    outstanding_paise: dict[str, int]  # per facility, incl. accrued interest and charges
    contingent_paise: dict[str, int]  # outstanding LC / BG per facility
    pending_finacle_commands: int
    open_drawdown_requests: int
    repaid_on: date

    def __post_init__(self) -> None:
        ids = set(self.facility_ids)
        if len(ids) != len(self.facility_ids):
            raise ValidationFailure("duplicate facility ids", code="bad_closure_facts")
        for name in ("outstanding_paise", "contingent_paise"):
            table = getattr(self, name)
            if set(table) != ids:  # dues on a facility that was not named must not be ignorable
                raise ValidationFailure(
                    f"{name} must have exactly the named facilities", code="bad_closure_facts"
                )
            object.__setattr__(self, name, MappingProxyType(dict(table)))


@dataclass
class ClosureFile:
    case_id: str
    facts: ClosureFacts
    proof: str
    actions_done: dict[str, str] = field(default_factory=dict)
    proposal_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    released_on: date | None = None
    completed: bool = False
    action_dates: dict[str, date] = field(default_factory=dict)
    delay_reason: str | None = None


class ClosureService:
    def __init__(
        self, clock: Clock, registry: RuleRegistry, cases: CasePort, maker_checker: MakerChecker
    ) -> None:
        self.clock, self.rules, self.cases, self.mc = clock, registry, cases, maker_checker
        self.files: dict[str, ClosureFile] = {}

    @staticmethod
    def blockers(facts: ClosureFacts) -> list[str]:
        out: list[str] = []
        for fid in facts.facility_ids:
            for label, table in (
                ("outstanding", facts.outstanding_paise),
                ("contingent", facts.contingent_paise),
            ):
                v = table.get(fid)
                if v is None or isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    out.append(f"{fid}: {label} amount unknown")  # unknown is never zero
                elif v > 0:
                    out.append(f"{fid}: {label} {v} paise remains")
        if facts.pending_finacle_commands:
            out.append(f"{facts.pending_finacle_commands} Finacle command(s) pending")
        if facts.open_drawdown_requests:
            out.append(f"{facts.open_drawdown_requests} drawdown request(s) open")
        if not facts.facility_ids:
            out.append("no facilities named")
        return out

    def propose_closure(
        self,
        case_id: str,
        facts: ClosureFacts,
        *,
        proposed_by: str,
        proposer_kind: str,
        on_behalf_of: str | None,
    ) -> ClosureFile:
        if case_id in self.files:
            raise ConflictError("a closure proposal already exists for this case")
        blockers = self.blockers(facts)
        if blockers:
            raise ConflictError("closure blocked: " + "; ".join(blockers), code="closure_blocked")
        if facts.repaid_on > self.clock.today():
            raise ValidationFailure("repayment date is in the future")
        proof = self._proof(facts)
        payload = {
            "facility_ids": sorted(facts.facility_ids),
            "zero_dues_proof": proof,
            "repaid_on": facts.repaid_on.isoformat(),
        }
        prop = self.cases.propose(
            case_id,
            kind="closure",
            payload=payload,
            proposed_by=proposed_by,
            proposer_kind=proposer_kind,
            on_behalf_of=on_behalf_of,
        )
        cf = ClosureFile(case_id, facts, proof, proposal_id=str(prop.proposal_id), payload=payload)
        self.files[case_id] = cf
        return cf

    @staticmethod
    def _proof(facts: ClosureFacts) -> str:
        return content_hash(
            {
                "obligor": facts.obligor_id,
                "facilities": sorted(facts.facility_ids),
                "outstanding": dict(sorted(facts.outstanding_paise.items())),
                "contingent": dict(sorted(facts.contingent_paise.items())),
                "repaid_on": facts.repaid_on.isoformat(),
            }
        )

    def record_action(
        self, case_id: str, action: str, evidence_ref: str, done_on: date | None = None
    ) -> None:
        if action not in RELEASE_ACTIONS:
            raise ValidationFailure(f"unknown release action {action!r}")
        if not evidence_ref.strip():
            raise ValidationFailure("evidence reference required")
        when = done_on or self.clock.today()
        cf = self.files[case_id]
        if when < cf.facts.repaid_on or when > self.clock.today():
            raise ValidationFailure("release date must be between repayment and today")
        cf.actions_done[action] = evidence_ref
        cf.action_dates[action] = when

    def release_status(
        self,
        case_id: str,
        *,
        attributable_to_lender: bool = True,
        reason: str | None = None,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        cf = self.files[case_id]
        if not attributable_to_lender:
            if not (reason and reason.strip() and evidence_ref and evidence_ref.strip()):
                raise ValidationFailure(
                    "a delay not attributable to the lender needs a reason and evidence",
                    code="exemption_unsupported",
                )
            cf.delay_reason = f"{reason} [{evidence_ref}]"
        facts: dict[str, Any] = {
            "repaid_on": cf.facts.repaid_on,
            "today": self.clock.today(),
            "delay_attributable_to_lender": attributable_to_lender,
        }
        if cf.released_on:
            facts["released_on"] = cf.released_on
        d = self.rules.evaluate("rbi.document_release", facts)
        return {
            **d.result,
            "escalate_on": d.details["escalate_on"],
            "missing_actions": [a for a in RELEASE_ACTIONS if a not in cf.actions_done],
            "rule_version": d.rule_version,
        }

    def complete(
        self, case_id: str, *, approval: ApprovalRecord, current_facts: ClosureFacts
    ) -> AccountClosed:
        """After the closure approval and every release action, with facts read again now.
        Returns the event to publish. Runs once."""
        cf = self.files[case_id]
        if cf.completed:
            raise ConflictError("closure already completed", code="already_completed")
        stale = self.blockers(current_facts)
        if stale or self._proof(current_facts) != cf.proof:
            raise ConflictError(
                "dues or facts changed since the closure was approved: "
                + ("; ".join(stale) or "proof differs"),
                code="closure_facts_changed",
            )
        missing = [a for a in RELEASE_ACTIONS if a not in cf.actions_done]
        if missing:
            raise ConflictError(
                "release actions outstanding: " + ", ".join(missing), code="release_incomplete"
            )
        # The approval must be the signed human decision on exactly this closure proposal.
        self.mc.verify(approval, cf.payload, required=Permission.CASE_DECIDE)
        if not approval.subject.endswith(f":{cf.proposal_id}") or approval.action != "closure":
            raise ConflictError("approval is for a different proposal", code="approval_mismatch")
        cf.released_on = max(cf.action_dates.values())
        cf.completed = True
        return AccountClosed(
            facility_ids=sorted(cf.facts.facility_ids),
            zero_dues_proof=cf.proof,
            release_actions=sorted(cf.actions_done),
        )
