"""Early-warning service: rules first, model second, humans last.

Fired indicators (``ews.indicators``) and the model score are fused by ``ews.alerting`` into one
score and level, with hysteresis so alerts do not flap. A signal always carries its reasons and
the indicators that could not be evaluated (missing data is shown, never read as "clear"). Alert
volume is capped by analyst capacity: the queue ranks by score and reports what was left out, so
overload is visible instead of silently dropping the tail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from domain.entities import Signal, SignalSeverity
from events.catalogue import EwsScoreChanged, Payload, SignalRaised
from platform_core.clock import Clock
from platform_core.errors import ValidationFailure
from platform_core.ids import new_id
from rules_engine import RuleRegistry

Emit = Callable[[str, Payload], None]
_SEVERITY = {
    "watch": SignalSeverity.LOW,
    "elevated": SignalSeverity.MEDIUM,
    "critical": SignalSeverity.HIGH,
}


@dataclass(frozen=True)
class MlScore:
    score: float  # 0..100, calibrated risk expressed on the alert scale
    pd_equivalent: float
    reasons: tuple[dict[str, Any], ...]  # e.g. SHAP top features
    model_version: str


@dataclass
class EwsState:
    obligor_id: str
    score: int = 0
    level: str = "none"
    data_gap: bool = False
    fired: tuple[str, ...] = ()
    not_evaluated: tuple[str, ...] = ()
    history: list[dict[str, Any]] = field(default_factory=list)


class EwsService:
    def __init__(
        self,
        clock: Clock,
        registry: RuleRegistry,
        emit: Emit | None = None,
        *,
        min_score_change: int = 5,
    ) -> None:
        self.clock, self.rules = clock, registry
        self._emit: Emit = emit or (lambda t, p: None)
        self.min_change = min_score_change
        self.state: dict[str, EwsState] = {}

    def update(
        self,
        obligor_id: str,
        metrics: dict[str, Any],
        ml: MlScore | None = None,
        *,
        horizon_months: int = 12,
    ) -> EwsState:
        ind = self.rules.evaluate("ews.indicators", {"metrics": metrics})
        packs = self.rules.params("ews.indicators")["indicators"]
        sev_by_id = {i["id"]: i["severity"] for i in packs}
        fired = [str(i) for i in ind.result]
        missing = [str(i) for i in ind.details["not_evaluated"]]
        prior = self.state.get(obligor_id) or EwsState(obligor_id)
        facts: dict[str, Any] = {
            "hit_severities": [sev_by_id[i] for i in fired],
            "total_indicators": len(packs),
            "unevaluated": len(missing),
            "previous_level": prior.level,
        }
        if ml is not None:
            facts["ml_score"] = ml.score
        alert = self.rules.evaluate("ews.alerting", facts)
        res = alert.result
        new = EwsState(
            obligor_id,
            int(res["score"]),
            str(res["level"]),
            bool(res["data_gap"]),
            tuple(fired),
            tuple(missing),
            prior.history,
        )
        moved = abs(new.score - prior.score) >= self.min_change or new.level != prior.level
        if moved:
            new.history.append(
                {"at": self.clock.now().isoformat(), "score": new.score, "level": new.level}
            )
            self._emit(
                "ews.score.changed",
                EwsScoreChanged(
                    score=new.score,
                    horizon_months=horizon_months,
                    pd_equivalent=ml.pd_equivalent if ml else 0,
                    reasons=list(ml.reasons) if ml else [{"indicator": i} for i in fired],
                    model_version=ml.model_version if ml else "rules-only",
                ),
            )
        if new.level in _SEVERITY and (new.level != prior.level or new.data_gap != prior.data_gap):
            sig = self._signal(new, ml)
            self._emit(
                "signal.raised",
                SignalRaised(
                    signal_id=sig.signal_id,
                    signal_type=sig.signal_type,
                    severity=sig.severity.value,
                    score=sig.score,
                    explanation=sig.explanation,
                    evidence=sig.evidence,
                ),
            )
        self.state[obligor_id] = new
        return new

    def _signal(self, s: EwsState, ml: MlScore | None) -> Signal:
        parts = [f"{len(s.fired)} indicator(s) fired: {', '.join(s.fired) or 'none'}"]
        if ml is not None:
            top = ", ".join(str(r.get("feature", r)) for r in ml.reasons[:3])
            parts.append(f"model score {ml.score:.0f} ({ml.model_version}); drivers: {top}")
        if s.not_evaluated:
            parts.append(f"{len(s.not_evaluated)} indicator(s) could not be evaluated")
        if s.data_gap:
            parts.append("DATA GAP: too many indicators missing to treat the score as complete")
        return Signal(
            signal_id=new_id("SIG"),
            obligor_id=s.obligor_id,
            source="ews_service",
            signal_type="ews_data_gap" if s.data_gap else "ews_level",
            severity=_SEVERITY[s.level],
            score=s.score,
            raised_at=self.clock.now(),
            valid_at=self.clock.today(),
            explanation="; ".join(parts),
            evidence=list(s.fired),
            rule_id="ews.alerting",
            rule_version=self.rules.pack("ews.alerting").version,
        )

    def queue(self, capacity: int) -> dict[str, Any]:
        """Top alerts for analysts, highest score first, and an explicit count of what was cut."""
        if capacity < 1:
            raise ValidationFailure("capacity must be at least 1")
        live = sorted(
            (s for s in self.state.values() if s.level != "none"),
            key=lambda s: (-s.score, s.obligor_id),
        )
        return {
            "shown": [s.obligor_id for s in live[:capacity]],
            "not_shown": len(live) - min(len(live), capacity),
            "total_alerts": len(live),
        }
