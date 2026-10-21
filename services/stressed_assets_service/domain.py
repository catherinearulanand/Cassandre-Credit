"""Stressed-asset clocks: review period, resolution deadline and the provisioning that follows.

Dates and percentages come from ``rbi.prudential_framework``. The service owns the file's state
(first default, plan implemented or not), reports where each file stands today, and flags files
whose deadline passed without an implemented plan. Choosing a resolution plan is a human credit
decision, so the service only *prepares* the proposal through the case port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from authz import ApprovalRecord, MakerChecker, Permission, ResourceScope
from domain.entities import CaseType
from domain.ports import CasePort
from platform_core.clock import Clock
from platform_core.errors import ConflictError, NotFoundError, ValidationFailure
from platform_core.ids import new_id
from rules_engine import RuleRegistry


@dataclass
class StressFile:
    file_id: str
    obligor_id: str
    first_default_on: date
    exposure_paise: int
    scope: ResourceScope
    case_id: str
    plan_ref: str | None = None
    plan_implemented_on: date | None = None
    plan_payload: dict[str, Any] = field(default_factory=dict)
    plan_proposal_id: str | None = None
    log: list[dict[str, Any]] = field(default_factory=list)


class StressedAssetService:
    def __init__(
        self, clock: Clock, registry: RuleRegistry, cases: CasePort, maker_checker: MakerChecker
    ) -> None:
        self.clock, self.rules, self.cases, self.mc = clock, registry, cases, maker_checker
        self.files: dict[str, StressFile] = {}

    def open_file(
        self, obligor_id: str, scope: ResourceScope, first_default_on: date, exposure_paise: int
    ) -> StressFile:
        if first_default_on > self.clock.today():
            raise ValidationFailure("first default cannot be in the future")
        if isinstance(exposure_paise, bool) or exposure_paise <= 0:
            raise ValidationFailure("exposure must be a positive integer of paise")
        for f in self.files.values():
            if f.obligor_id == obligor_id and f.plan_implemented_on is None:
                raise ConflictError("an unresolved stressed-asset file already exists")
        case = self.cases.open_case(
            CaseType.RESTRUCTURING,
            obligor_id,
            scope,
            f"Stressed asset: first default {first_default_on}",
            sla_days=30,
        )
        f = StressFile(
            new_id("STR"), obligor_id, first_default_on, exposure_paise, scope, case.case_id
        )
        f.log.append({"on": self.clock.today().isoformat(), "event": "opened"})
        self.files[f.file_id] = f
        return f

    def _get(self, file_id: str) -> StressFile:
        try:
            return self.files[file_id]
        except KeyError as exc:
            raise NotFoundError(f"stressed-asset file {file_id}") from exc

    def status(self, file_id: str) -> dict[str, Any]:
        f = self._get(file_id)
        d = self.rules.evaluate(
            "rbi.prudential_framework",
            {
                "first_default_on": f.first_default_on,
                "today": self.clock.today(),
                "plan_implemented": f.plan_implemented_on is not None,
            },
        )
        res = d.result
        deadline = date.fromisoformat(res["resolution_deadline"])
        return {
            "file_id": file_id,
            "review_period_end": res["review_period_end"],
            "resolution_deadline": res["resolution_deadline"],
            "additional_provision_pct": res["additional_provision_pct"],
            "plan_implemented": f.plan_implemented_on is not None,
            "deadline_missed": f.plan_implemented_on is None and self.clock.today() > deadline,
            "rule_version": d.rule_version,
            "pack_unverified": d.pack_status != "verified",
        }

    def propose_plan(
        self,
        file_id: str,
        *,
        plan_ref: str,
        summary: str,
        proposed_by: str,
        proposer_kind: str,
        on_behalf_of: str | None,
    ) -> str:
        f = self._get(file_id)
        payload = {"file_id": file_id, "plan_ref": plan_ref, "summary": summary}
        prop = self.cases.propose(
            f.case_id,
            kind="stage_change",
            payload=payload,
            proposed_by=proposed_by,
            proposer_kind=proposer_kind,
            on_behalf_of=on_behalf_of,
        )
        f.plan_ref, f.plan_payload, f.plan_proposal_id = plan_ref, payload, str(prop.proposal_id)
        f.log.append(
            {"on": self.clock.today().isoformat(), "event": "plan_proposed", "plan": plan_ref}
        )
        return str(prop.proposal_id)

    def record_implementation(
        self, file_id: str, approval: ApprovalRecord, on: date | None = None
    ) -> None:
        """Only after the plan was approved (the approval id is the evidence) and implemented."""
        f = self._get(file_id)
        if f.plan_ref is None:
            raise ConflictError("no resolution plan was proposed")
        self.mc.verify(approval, f.plan_payload, required=Permission.CASE_DECIDE)
        if not approval.subject.endswith(f":{f.plan_proposal_id}"):
            raise ConflictError("approval is for a different proposal", code="approval_mismatch")
        when = on or self.clock.today()
        if when < f.first_default_on or when > self.clock.today():
            raise ValidationFailure("implementation date outside the file's life")
        f.plan_implemented_on = when
        f.log.append(
            {"on": when.isoformat(), "event": "implemented", "approval": approval.approval_id}
        )

    def deadline_misses(self) -> list[str]:
        return [fid for fid in self.files if self.status(fid)["deadline_missed"]]
