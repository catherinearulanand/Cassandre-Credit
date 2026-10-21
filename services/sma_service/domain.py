"""Asset classification: SMA buckets, NPA class and provision, CRILC reportability, ECL stage.

Every number comes from a rule pack (``rbi.sma``, ``rbi.irac``, ``rbi.crilc``, ``rbi.ecl``). This
service adds state and the rules about *moving*: it records each change with its reason, emits
``sma.changed`` once per change, and never lets a bucket improve on a partial payment. Facts that
are missing or malformed raise: a stressed account is never silently classified STANDARD.

Two behaviours below are bank-policy assumptions that must be confirmed against the RBI text before
UAT (they are listed in ``rules/CHANGELOG.md``): an NPA account upgrades only when *all* arrears are
cleared, and one NPA account marks the borrower's other accounts as "NPA by contagion" for review.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from events.catalogue import Payload, SmaChanged, StageChanged
from platform_core.clock import Clock
from platform_core.errors import ValidationFailure
from rules_engine import RuleLoadError, RuleRegistry

Emit = Callable[[str, Payload], None]
_ORDER = {"STANDARD": 0, "SMA-0": 1, "SMA-1": 2, "SMA-2": 3, "NPA": 4}


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    obligor_id: str
    revolving: bool
    dpd: int
    days_over_limit: int
    outstanding_paise: int
    secured: bool
    arrears_cleared: bool = False  # every overdue amount (principal and interest) is paid
    months_as_npa: int = 0
    loss_identified: bool = False
    credit_impaired: bool = False
    watch_list: bool = False
    restructured: bool = False
    pd_origination: str | None = None
    pd_current: str | None = None
    ews_score: int | None = None

    def __post_init__(self) -> None:
        for name in ("dpd", "days_over_limit", "outstanding_paise", "months_as_npa"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValidationFailure(
                    f"{name} must be a non-negative integer", code="bad_snapshot"
                )
        if not self.account_id or not self.obligor_id:
            raise ValidationFailure("account and obligor ids are required", code="bad_snapshot")


@dataclass(frozen=True)
class Classification:
    account_id: str
    bucket: str
    irac_class: str | None
    provision_pct: str | None
    stage: int | None  # None while the ECL directions are not in force
    stage_reasons: tuple[str, ...]
    rule_versions: dict[str, str]
    pack_unverified: bool
    explanation: str
    contagion_from: str | None = None


@dataclass
class _State:
    bucket: str = "STANDARD"
    stage: int = 1
    history: list[dict[str, Any]] = field(default_factory=list)


class SmaService:
    def __init__(self, clock: Clock, registry: RuleRegistry, emit: Emit | None = None) -> None:
        self.clock, self.rules = clock, registry
        self._emit: Emit = emit or (lambda t, p: None)
        self._state: dict[str, _State] = {}
        self._latest: dict[str, Classification] = {}
        self._by_obligor: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ classify ----------
    def classify(self, a: AccountSnapshot) -> Classification:
        facts = {
            "type": "revolving" if a.revolving else "non_revolving",
            "dpd": a.dpd,
            "days_over_limit": a.days_over_limit,
        }
        sma = self.rules.evaluate("rbi.sma", facts)
        bucket = str(sma.result)
        prior = self._state.get(a.account_id)
        if (
            prior is not None
            and prior.bucket == "NPA"
            and bucket != "NPA"
            and not a.arrears_cleared
        ):
            bucket = "NPA"  # no upgrade on a partial payment
        irac_class: str | None = None
        provision: str | None = None
        versions = {"rbi.sma": sma.rule_version}
        unverified = sma.pack_status != "verified"
        if bucket == "NPA":
            ir = self.rules.evaluate(
                "rbi.irac",
                {
                    # NPA by sticky state may have a lower DPD now; classification months drive the class
                    "dpd": max(a.dpd, a.days_over_limit, 91),
                    "months_as_npa": a.months_as_npa,
                    "loss_identified": a.loss_identified,
                    "secured": a.secured,
                },
            )
            irac_class, provision = str(ir.result), str(ir.details["provision_pct"])
            versions["rbi.irac"] = ir.rule_version
            unverified = unverified or ir.pack_status != "verified"
        ecl_facts: dict[str, Any] = {"dpd": max(a.dpd, a.days_over_limit if a.revolving else 0)}
        for k in ("pd_origination", "pd_current", "ews_score"):
            if getattr(a, k) is not None:
                ecl_facts[k] = getattr(a, k)
        ecl_facts.update(
            credit_impaired=a.credit_impaired or bucket == "NPA",
            watch_list=a.watch_list,
            restructured=a.restructured,
        )
        stage: int | None
        try:
            stage_d = self.rules.evaluate("rbi.ecl", ecl_facts)
        except RuleLoadError as exc:
            if exc.code != "rule_not_in_force":
                raise
            stage, stage_reasons = None, ("ECL directions not yet in force",)  # before 1 Apr 2027
        else:
            versions["rbi.ecl"] = stage_d.rule_version
            unverified = unverified or stage_d.pack_status != "verified"
            stage = int(stage_d.result)
            stage_reasons = tuple(stage_d.details.get("reasons", [])) or (stage_d.explanation,)
        cls = Classification(
            a.account_id,
            bucket,
            irac_class,
            provision,
            stage,
            stage_reasons,
            versions,
            unverified,
            sma.explanation,
        )
        self._record(a, cls)
        return cls

    def _record(self, a: AccountSnapshot, cls: Classification) -> None:
        st = self._state.setdefault(a.account_id, _State())
        self._by_obligor.setdefault(a.obligor_id, set()).add(a.account_id)
        now = self.clock.now().isoformat()
        if cls.bucket != st.bucket:
            self._emit(
                "sma.changed",
                SmaChanged(
                    account_id=a.account_id,
                    old_bucket=st.bucket,
                    new_bucket=cls.bucket,
                    dpd=a.dpd,
                    days_over_limit=a.days_over_limit,
                    rule_version=cls.rule_versions["rbi.sma"],
                ),
            )
            st.history.append(
                {
                    "at": now,
                    "kind": "bucket",
                    "from": st.bucket,
                    "to": cls.bucket,
                    "why": cls.explanation,
                }
            )
            st.bucket = cls.bucket
        if cls.stage is not None and cls.stage != st.stage:
            self._emit(
                "stage.changed",
                StageChanged(
                    old_stage=st.stage, new_stage=cls.stage, reasons=list(cls.stage_reasons)
                ),
            )
            st.history.append({"at": now, "kind": "stage", "from": st.stage, "to": cls.stage})
            st.stage = cls.stage
        self._latest[a.account_id] = cls

    # ------------------------------------------------------------------ borrower view -----
    def borrower_view(self, obligor_id: str) -> dict[str, Any]:
        """Worst bucket across the borrower's accounts and the accounts that need review
        because another account of the same borrower is NPA (contagion, review not auto-class)."""
        ids = sorted(self._by_obligor.get(obligor_id, set()))
        latest = [self._latest[i] for i in ids]
        worst = max((c.bucket for c in latest), key=lambda b: _ORDER[b], default="STANDARD")
        contagion = (
            [c.account_id for c in latest if worst == "NPA" and c.bucket != "NPA"] if latest else []
        )
        return {"obligor_id": obligor_id, "worst_bucket": worst, "contagion_review": contagion}

    def crilc_reportable(self, obligor_id: str, aggregate_exposure_paise: int) -> dict[str, Any]:
        """Reportable if exposure meets the threshold and any account is SMA or worse."""
        view = self.borrower_view(obligor_id)
        d = self.rules.evaluate("rbi.crilc", {"aggregate_exposure_paise": aggregate_exposure_paise})
        stressed = _ORDER[view["worst_bucket"]] >= _ORDER["SMA-0"]
        return {
            "reportable": bool(d.result) and stressed,
            "above_threshold": bool(d.result),
            "worst_bucket": view["worst_bucket"],
            "rule_version": d.rule_version,
            "frequency": d.details["frequency"],
        }

    def history(self, account_id: str) -> list[dict[str, Any]]:
        st = self._state.get(account_id)
        return list(st.history) if st else []
