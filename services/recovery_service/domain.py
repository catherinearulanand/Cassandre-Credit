"""Recovery: one-time settlements, wilful-defaulter process and penal-charge checks.

The service prepares and checks; it never settles, classifies or charges. An OTS proposal states
the recovery ratio and whether the offer is below the realisable security value so the approver
sees the trade-off in numbers. Wilful-defaulter designation can be proposed only after the
show-cause and review-committee steps are complete (``rbi.wilful_defaulter``). Penal charges are
checked against ``rbi.penal_charges`` before they are applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from domain.ports import CasePort
from platform_core.clock import Clock
from platform_core.errors import ConflictError, ValidationFailure
from rules_engine import RuleRegistry


def _paise(name: str, v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValidationFailure(f"{name} must be a non-negative integer of paise")
    return v


@dataclass(frozen=True)
class OtsAnalysis:
    outstanding_paise: int
    offer_paise: int
    security_realisable_paise: int
    recovery_ratio: Decimal  # offer / outstanding, rounded down to 4 places
    below_security_value: bool
    haircut_paise: int


class RecoveryService:
    def __init__(self, clock: Clock, registry: RuleRegistry, cases: CasePort) -> None:
        self.clock, self.rules, self.cases = clock, registry, cases

    @staticmethod
    def analyse_ots(
        outstanding_paise: int, offer_paise: int, security_realisable_paise: int
    ) -> OtsAnalysis:
        out = _paise("outstanding", outstanding_paise)
        offer = _paise("offer", offer_paise)
        sec = _paise("security value", security_realisable_paise)
        if out == 0:
            raise ValidationFailure("nothing outstanding to settle")
        if offer > out:
            raise ValidationFailure("an OTS offer above the outstanding is not a settlement")
        ratio = (Decimal(offer) / Decimal(out)).quantize(Decimal("0.0001"), rounding=ROUND_FLOOR)
        return OtsAnalysis(out, offer, sec, ratio, offer < sec, out - offer)

    def propose_ots(
        self,
        case_id: str,
        *,
        analysis: OtsAnalysis,
        terms: str,
        proposed_by: str,
        proposer_kind: str,
        on_behalf_of: str | None,
    ) -> str:
        prop = self.cases.propose(
            case_id,
            kind="ots",
            payload={
                "outstanding_paise": analysis.outstanding_paise,
                "offer_paise": analysis.offer_paise,
                "security_realisable_paise": analysis.security_realisable_paise,
                "recovery_ratio": str(analysis.recovery_ratio),
                "below_security_value": analysis.below_security_value,
                "haircut_paise": analysis.haircut_paise,
                "terms": terms,
            },
            proposed_by=proposed_by,
            proposer_kind=proposer_kind,
            on_behalf_of=on_behalf_of,
        )
        return str(prop.proposal_id)

    def wilful_status(
        self,
        outstanding_paise: int,
        *,
        show_cause_issued_on: Any = None,
        response_received_on: Any = None,
        review_committee_confirmed: bool = False,
    ) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "outstanding_rupees": str(Decimal(_paise("outstanding", outstanding_paise)) / 100),
            "today": self.clock.today(),
            "review_committee_confirmed": review_committee_confirmed,
        }
        if show_cause_issued_on:
            facts["show_cause_issued_on"] = show_cause_issued_on
        if response_received_on:
            facts["response_received_on"] = response_received_on
        d = self.rules.evaluate("rbi.wilful_defaulter", facts)
        return {"status": d.result, "explanation": d.explanation, "rule_version": d.rule_version}

    def propose_wilful_designation(
        self,
        case_id: str,
        *,
        status: dict[str, Any],
        rationale: str,
        proposed_by: str,
        proposer_kind: str,
        on_behalf_of: str | None,
    ) -> str:
        if status.get("status") != "MAY_CLASSIFY":
            raise ConflictError(
                f"cannot propose designation: {status.get('explanation')}", code="wilful_not_ready"
            )
        prop = self.cases.propose(
            case_id,
            kind="wilful_defaulter_classification",
            payload={"rationale": rationale, "rule_version": status["rule_version"]},
            proposed_by=proposed_by,
            proposer_kind=proposer_kind,
            on_behalf_of=on_behalf_of,
        )
        return str(prop.proposal_id)

    def check_penal_charge(
        self, *, kind: str, capitalise: bool, disclosed_in_agreement: bool
    ) -> dict[str, Any]:
        d = self.rules.evaluate(
            "rbi.penal_charges",
            {
                "kind": kind,
                "capitalise": capitalise,
                "disclosed_in_agreement": disclosed_in_agreement,
            },
        )
        return {
            "allowed": d.result == "ALLOWED",
            "reasons": d.details.get("reasons", []),
            "rule_version": d.rule_version,
        }
