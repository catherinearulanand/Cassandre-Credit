"""Red-flagged accounts and fraud classification: natural justice enforced in code.

The platform can *trigger* an RFA file from early-warning evidence and *prepare* a classification
proposal. It cannot classify. The proposal is refused until a show-cause notice was issued and the
response window has elapsed or a response was received (``rbi.rfa``); the decision itself is a
human committee approval through the case service. Each step is dated by the injected clock, and
a file past its resolution deadline is reported.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from authz import ResourceScope
from domain.entities import CaseType
from domain.ports import CasePort
from events.catalogue import Payload, RfaOpened
from platform_core.clock import Clock
from platform_core.errors import ConflictError, NotFoundError, ValidationFailure
from platform_core.ids import new_id
from rules_engine import RuleRegistry

Emit = Callable[[str, Payload], None]


@dataclass
class RfaFile:
    rfa_id: str
    obligor_id: str
    case_id: str
    opened_on: date
    triggers: list[str]
    scope: ResourceScope
    show_cause_issued_on: date | None = None
    response_received_on: date | None = None
    classification_proposal_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class RfaService:
    def __init__(
        self, clock: Clock, registry: RuleRegistry, cases: CasePort, emit: Emit | None = None
    ) -> None:
        self.clock, self.rules, self.cases = clock, registry, cases
        self._emit: Emit = emit or (lambda t, p: None)
        self.files: dict[str, RfaFile] = {}
        self._open_by_obligor: dict[str, str] = {}

    def trigger(
        self,
        obligor_id: str,
        scope: ResourceScope,
        *,
        indicators: list[str],
        ews_score: int | None = None,
    ) -> RfaFile:
        """Open (or return the existing) RFA file when evidence crosses the policy threshold."""
        existing = self._open_by_obligor.get(obligor_id)
        if existing is not None:
            f = self.files[existing]
            for i in indicators:
                if i not in f.triggers:
                    f.triggers.append(i)
            return f
        threshold = int(self.rules.params("rbi.rfa")["ews_score_rfa_threshold"])
        if not indicators and (ews_score is None or ews_score < threshold):
            raise ValidationFailure("no evidence to open an RFA file", code="rfa_no_trigger")
        case = self.cases.open_case(
            CaseType.RFA,
            obligor_id,
            scope,
            f"RFA: {', '.join(indicators) or 'EWS score ' + str(ews_score)}",
            sla_days=7,
            evidence=list(indicators),
        )
        f = RfaFile(
            new_id("RFA"), obligor_id, case.case_id, self.clock.today(), list(indicators), scope
        )
        f.events.append({"on": f.opened_on.isoformat(), "event": "opened"})
        self.files[f.rfa_id] = f
        self._open_by_obligor[obligor_id] = f.rfa_id
        self._emit(
            "rfa.opened", RfaOpened(case_id=case.case_id, triggering_indicators=list(indicators))
        )
        return f

    def _get(self, rfa_id: str) -> RfaFile:
        try:
            return self.files[rfa_id]
        except KeyError as exc:
            raise NotFoundError(f"rfa {rfa_id}") from exc

    def record_show_cause(self, rfa_id: str, issued_on: date | None = None) -> RfaFile:
        f = self._get(rfa_id)
        if f.show_cause_issued_on is not None:
            raise ConflictError("show-cause already recorded")
        when = issued_on or self.clock.today()
        if when > self.clock.today():
            raise ValidationFailure("show-cause cannot be dated in the future")
        f.show_cause_issued_on = when
        f.events.append({"on": when.isoformat(), "event": "show_cause_issued"})
        return f

    def record_response(self, rfa_id: str, received_on: date | None = None) -> RfaFile:
        f = self._get(rfa_id)
        if f.show_cause_issued_on is None:
            raise ConflictError("no show-cause notice to respond to")
        when = received_on or self.clock.today()
        if when < f.show_cause_issued_on or when > self.clock.today():
            raise ValidationFailure("response date outside the notice period")
        f.response_received_on = when
        f.events.append({"on": when.isoformat(), "event": "response_received"})
        return f

    def committee_ready(self, rfa_id: str) -> tuple[bool, str]:
        f = self._get(rfa_id)
        facts: dict[str, Any] = {"today": self.clock.today()}
        if f.show_cause_issued_on:
            facts["show_cause_issued_on"] = f.show_cause_issued_on
        if f.response_received_on:
            facts["response_received_on"] = f.response_received_on
        d = self.rules.evaluate("rbi.rfa", facts)
        return bool(d.result), d.explanation

    def propose_classification(
        self,
        rfa_id: str,
        *,
        proposed_by: str,
        proposer_kind: str,
        on_behalf_of: str | None,
        classification: str,
        rationale: str,
    ) -> str:
        """Prepare the committee proposal. Refused until natural justice is complete."""
        f = self._get(rfa_id)
        ok, why = self.committee_ready(rfa_id)
        if not ok:
            raise ConflictError(f"cannot propose fraud classification: {why}", code="rfa_not_ready")
        if classification not in ("fraud", "not_fraud"):
            raise ValidationFailure("classification must be fraud or not_fraud")
        if f.classification_proposal_id is not None:
            raise ConflictError("a classification proposal already exists")
        prop = self.cases.propose(
            f.case_id,
            kind="fraud_classification",
            payload={
                "rfa_id": rfa_id,
                "obligor_id": f.obligor_id,
                "classification": classification,
                "rationale": rationale,
                "show_cause_issued_on": f.show_cause_issued_on.isoformat()
                if f.show_cause_issued_on
                else "",
                "response_received_on": f.response_received_on.isoformat()
                if f.response_received_on
                else "",
            },
            proposed_by=proposed_by,
            proposer_kind=proposer_kind,
            on_behalf_of=on_behalf_of,
        )
        f.classification_proposal_id = str(prop.proposal_id)
        f.events.append({"on": self.clock.today().isoformat(), "event": "classification_proposed"})
        return f.classification_proposal_id

    def overdue_files(self) -> list[RfaFile]:
        days = int(self.rules.params("rbi.rfa")["rfa_resolution_days"])
        today = self.clock.today()
        return [
            f
            for f in self.files.values()
            if f.classification_proposal_id is None and today > f.opened_on + timedelta(days=days)
        ]
