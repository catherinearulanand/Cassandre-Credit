"""CTS validation, publication, amendment and diff.

Publication rules (§8.1):
- maker-checker: the checker is a human CAD checker and differs from the drafter (the
  drafter may be the Sanction Compiler agent);
- validation must pass: credit policy, delegated financial power, Large Exposures, DSL;
- content_hash over canonical JSON, signed; every read re-verifies hash and signature;
- published versions are immutable; amendments are version n+1 with an amendment
  sanction reference and an explicit change set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from authz import ApprovalRecord, MakerChecker, Permission, Principal
from domain.coa import CODES
from domain.cts import CreditTermSheet
from platform_core.clock import Clock
from platform_core.errors import ConflictError, IntegrityError, NotFoundError, ValidationFailure
from rules_engine import RuleRegistry
from rules_engine.dsl import DslError, validate
from security.crypto import Signature, Signer


@dataclass(frozen=True)
class PublishedCts:
    cts: CreditTermSheet
    signature: Signature
    approval: ApprovalRecord
    published_at: datetime
    drafted_by: str


def validate_draft(
    cts: CreditTermSheet,
    rules: RuleRegistry,
    *,
    tier1_capital_rupees: Decimal,
    rating: str,
    existing_group_exposure_rupees: Decimal | None = None,
) -> list[str]:
    problems: list[str] = []
    for f in cts.facilities:
        d = rules.evaluate(
            "policy.credit_policy",
            {
                "facility_type": f.facility_type.value,
                "tenor_months": f.tenor_months,
                "spread_bps": f.pricing_spread_bps,
                "rating": rating,
                "margin_pct": str(f.margin_pct) if f.margin_pct is not None else None,
            },
        )
        problems += [f"{f.facility_id}: {v}" for v in d.result]
    total_rupees = Decimal(cts.total_sanctioned().amount_paise) / 100
    dm = rules.evaluate(
        "policy.approval_matrix",
        {"authority": cts.sanctioning_authority, "total_exposure_rupees": str(total_rupees)},
    )
    if not dm.result:
        problems.append(f"delegated power: {dm.explanation}")
    le = rules.evaluate(
        "rbi.large_exposures",
        {
            "tier1_capital_rupees": str(tier1_capital_rupees),
            "exposure_rupees": str(total_rupees),
            "counterparty": "single",
        },
    )
    if not le.result:
        problems.append(f"large exposures: {le.explanation}")
    if cts.group_id and existing_group_exposure_rupees is None:
        problems.append("group exposure unknown: the Large Exposures group check cannot run")
    elif cts.group_id and existing_group_exposure_rupees is not None:
        lg = rules.evaluate(
            "rbi.large_exposures",
            {
                "tier1_capital_rupees": str(tier1_capital_rupees),
                "exposure_rupees": str(total_rupees + existing_group_exposure_rupees),
                "counterparty": "group",
            },
        )
        if not lg.result:
            problems.append(f"group large exposures: {lg.explanation}")
    for c in cts.covenants:
        if c.formula:
            try:
                validate(c.formula, cts.definitions, CODES)
            except DslError as exc:
                problems.append(f"{c.covenant_id}: {exc}")
    for name, formula in cts.definitions.items():
        try:
            validate(formula, {k: v for k, v in cts.definitions.items() if k != name}, CODES)
        except DslError as exc:
            problems.append(f"definition {name}: {exc}")
    return problems


def diff(old: CreditTermSheet, new: CreditTermSheet) -> list[dict[str, Any]]:
    """Field-level change set between two CTS versions (ignores version bookkeeping)."""
    skip = {"version", "supersedes_version", "amendment_sanction_ref", "content_hash"}
    a = old.model_dump(mode="json", exclude=skip)
    b = new.model_dump(mode="json", exclude=skip)
    out: list[dict[str, Any]] = []

    def keyed(items: list[Any]) -> dict[str, Any] | None:
        for k in ("covenant_id", "node_id", "condition_id", "collateral_id", "facility_id"):
            if items and all(isinstance(i, dict) and isinstance(i.get(k), str) for i in items):
                values = [i[k] for i in items]
                if len(set(values)) == len(values):
                    return {i[k]: i for i in items}
        return None

    def walk(x: Any, y: Any, path: str) -> None:
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                if k == "citations":
                    continue
                walk(x.get(k), y.get(k), f"{path}.{k}" if path else k)
        elif (
            isinstance(x, list)
            and isinstance(y, list)
            and (kx := keyed(x)) is not None
            and (ky := keyed(y)) is not None
        ):
            for k in sorted(set(kx) | set(ky)):
                walk(kx.get(k), ky.get(k), f"{path}[{k}]")
        elif x != y:
            out.append({"path": path, "old": x, "new": y})

    walk(a, b, "")
    return out


class CtsService:
    def __init__(
        self,
        clock: Clock,
        rules: RuleRegistry,
        signer: Signer,
        maker_checker: MakerChecker,
        *,
        tier1_capital_rupees: Decimal,
        emit: Callable[[str, CreditTermSheet, dict[str, Any]], None] | None = None,
        group_exposure: Callable[[str], Decimal | None] | None = None,
    ) -> None:
        # group_exposure(group_id) -> existing group exposure in rupees from the limit service, or
        # None if unknown. Unknown exposure blocks publication: the check fails closed.
        self._group_exposure = group_exposure or (lambda gid: None)
        self.clock = clock
        self.rules = rules
        self.signer = signer
        self.mc = maker_checker
        self.tier1 = tier1_capital_rupees
        self._store: dict[str, list[PublishedCts]] = {}
        self._emit = emit or (lambda t, c, d: None)

    def publish(
        self,
        draft: CreditTermSheet,
        *,
        drafted_by: str,
        checker: Principal,
        reason: str,
        drafted_on_behalf_of: str | None = None,
        rating: str,
    ) -> PublishedCts:
        if draft.version != 1:
            raise ValidationFailure("use amend() for versions after 1")
        if draft.cts_id in self._store:
            raise ConflictError(f"{draft.cts_id} already published")
        return self._commit(
            draft,
            drafted_by,
            checker,
            reason,
            "publish",
            rating=rating,
            on_behalf_of=drafted_on_behalf_of,
        )

    def amend(
        self,
        cts_id: str,
        new: CreditTermSheet,
        *,
        drafted_by: str,
        checker: Principal,
        drafted_on_behalf_of: str | None = None,
        reason: str,
        rating: str,
    ) -> tuple[PublishedCts, list[dict[str, Any]]]:
        cur = self.get(cts_id)
        if new.cts_id != cts_id or new.obligor_id != cur.cts.obligor_id:
            raise ConflictError("an amendment must keep the same CTS id and obligor")
        if new.version != cur.cts.version + 1 or new.supersedes_version != cur.cts.version:
            raise ConflictError("amendment must be exactly the next version")
        if not new.amendment_sanction_ref:
            raise ValidationFailure("amendment requires an amendment sanction reference")
        changes = diff(cur.cts, new)
        if not changes:
            raise ValidationFailure("amendment changes nothing")
        pub = self._commit(
            new,
            drafted_by,
            checker,
            reason,
            "amend",
            changes=changes,
            rating=rating,
            on_behalf_of=drafted_on_behalf_of,
        )
        return pub, changes

    def _commit(
        self,
        cts: CreditTermSheet,
        drafted_by: str,
        checker: Principal,
        reason: str,
        action: str,
        changes: list[dict[str, Any]] | None = None,
        *,
        rating: str,
        on_behalf_of: str | None = None,
    ) -> PublishedCts:
        group = self._group_exposure(cts.group_id) if cts.group_id else None
        problems = validate_draft(
            cts,
            self.rules,
            tier1_capital_rupees=self.tier1,
            rating=rating,
            existing_group_exposure_rupees=group,
        )
        if problems:
            raise ValidationFailure(
                "CTS failed validation: " + " | ".join(problems), problems=problems
            )
        h = cts.compute_hash()
        sealed = cts.model_copy(update={"content_hash": h})
        approval = self.mc.approve(
            subject=f"{cts.cts_id}:v{cts.version}",
            subject_payload={"content_hash": h},
            action=f"cts.{action}",
            maker_id=drafted_by,
            checker=checker,
            required=Permission.CTS_PUBLISH,
            reason=reason,
            maker_on_behalf_of=on_behalf_of,
        )
        pub = PublishedCts(sealed, self.signer.sign_hash(h), approval, self.clock.now(), drafted_by)
        self._store.setdefault(cts.cts_id, []).append(pub)
        if action == "publish":
            self._emit(
                "cts.published",
                sealed,
                {
                    "cts_id": cts.cts_id,
                    "version": cts.version,
                    "content_hash": h,
                    "signature_key_id": pub.signature.key_id,
                    "approver": checker.id,
                    "approval_id": approval.approval_id,
                },
            )
        else:
            self._emit(
                "cts.amended",
                sealed,
                {
                    "cts_id": cts.cts_id,
                    "old_version": cts.version - 1,
                    "new_version": cts.version,
                    "change_set": changes or [],
                    "amendment_sanction_ref": cts.amendment_sanction_ref,
                },
            )
        return pub

    def get(self, cts_id: str, version: int | None = None) -> PublishedCts:
        versions = self._store.get(cts_id)
        if not versions:
            raise NotFoundError(f"CTS {cts_id}")
        pub = (
            versions[-1]
            if version is None
            else next((p for p in versions if p.cts.version == version), None)
        )
        if pub is None:
            raise NotFoundError(f"CTS {cts_id} v{version}")
        h = pub.cts.compute_hash()
        if h != pub.cts.content_hash:
            raise IntegrityError(
                f"CTS {cts_id} v{pub.cts.version} content changed after publication"
            )
        self.signer.verify(pub.signature, h)
        return pub

    def versions(self, cts_id: str) -> list[int]:
        return [p.cts.version for p in self._store.get(cts_id, [])]
