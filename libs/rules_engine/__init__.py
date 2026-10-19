"""Rules engine: loads versioned YAML rule packs and evaluates them with recorded provenance.

Regulation as configuration: *parameters* (day counts, thresholds, percentages) live in
``rules/**.yaml``; *logic* lives in evaluator functions registered per rule id. Every
Decision records the rule id and version, so a replay as at a past date uses the version
that was in force then.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from platform_core.clock import SystemClock
from platform_core.config import PlatformSettings
from platform_core.errors import DomainError, ValidationFailure
from pydantic import BaseModel, Field, model_validator


class RuleSource(BaseModel):
    title: str
    date: dt.date | None = None
    url: str | None = None
    source_note: str


class RuleTest(BaseModel):
    input: dict[str, Any]
    expect: Any
    name: str | None = None


class RulePack(BaseModel):
    id: str
    version: str
    status: Literal["unverified", "verified", "superseded"]
    effective_from: date
    effective_to: date | None = None
    source: RuleSource
    verified_by: str | None = None
    verified_on: date | None = None
    rules: dict[str, Any]
    tests: list[RuleTest] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> RulePack:
        if self.status == "verified" and not (self.verified_by and self.verified_on):
            raise ValueError("a verified pack needs verified_by and verified_on")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self

    def covers(self, d: date) -> bool:
        return self.effective_from <= d and (self.effective_to is None or d < self.effective_to)


@dataclass(frozen=True)
class Decision:
    result: Any
    explanation: str
    rule_id: str
    rule_version: str
    details: dict[str, Any] = field(default_factory=dict)


Evaluator = Callable[[dict[str, Any], dict[str, Any]], tuple[Any, str, dict[str, Any]]]
_EVALUATORS: dict[str, Evaluator] = {}


def evaluator(rule_id: str) -> Callable[[Evaluator], Evaluator]:
    def deco(fn: Evaluator) -> Evaluator:
        _EVALUATORS[rule_id] = fn
        return fn

    return deco


class RuleLoadError(DomainError):
    code = "rule_load_error"


class RuleRegistry:
    def __init__(
        self,
        settings: PlatformSettings | None = None,
        on_override: Callable[[str, str], None] | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._settings = settings or PlatformSettings()
        self._packs: dict[str, list[RulePack]] = {}
        self._on_override = on_override
        self._today: Callable[[], date] = today or (lambda: SystemClock().today())

    # ---- loading --------------------------------------------------------------------
    def load_dir(self, root: Path) -> RuleRegistry:
        for path in sorted(root.rglob("*.yaml")):
            with path.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if not isinstance(data, dict) or "id" not in data:
                continue
            self.add(RulePack.model_validate(data))
        return self

    def add(self, pack: RulePack) -> None:
        if pack.status == "unverified" and self._settings.strict:
            if not self._settings.allow_unverified_rules:
                raise RuleLoadError(
                    f"{self._settings.profile} refuses unverified rule pack "
                    f"{pack.id}@{pack.version}",
                    code="unverified_rule_in_strict_profile",
                )
            if self._on_override is None:
                raise RuleLoadError(
                    "unverified-rule override requires an audit hook", code="override_unaudited"
                )
            self._on_override(pack.id, pack.version)
        if pack.id not in _EVALUATORS:
            raise RuleLoadError(f"no evaluator registered for {pack.id}")
        versions = self._packs.setdefault(pack.id, [])
        if any(p.version == pack.version for p in versions):
            raise RuleLoadError(f"duplicate {pack.id}@{pack.version}")
        for other in versions:
            end_a = pack.effective_to or date.max
            end_b = other.effective_to or date.max
            if pack.effective_from < end_b and other.effective_from < end_a:
                raise RuleLoadError(
                    f"{pack.id}@{pack.version} overlaps {other.version}: set effective_to on the old one",
                    code="rule_window_overlap",
                )
        versions.append(pack)
        versions.sort(key=lambda p: p.effective_from)

    def ids(self) -> list[str]:
        return sorted(self._packs)

    def pack(
        self, rule_id: str, *, as_of: date | None = None, version: str | None = None
    ) -> RulePack:
        versions = self._packs.get(rule_id)
        if not versions:
            raise RuleLoadError(f"rule pack {rule_id} not loaded", code="rule_missing")
        if version is not None:
            for p in versions:
                if p.version == version:
                    return p
            raise RuleLoadError(f"{rule_id}@{version} not loaded", code="rule_version_missing")
        # A pack in the future is never applied "today"; a replay passes an explicit as_of.
        when = as_of if as_of is not None else self._today()
        in_force = [
            p
            for p in versions
            if p.covers(when) and (p.status != "superseded" or as_of is not None)
        ]
        if not in_force:
            raise RuleLoadError(
                f"no version of {rule_id} in force at {as_of}", code="rule_not_in_force"
            )
        return in_force[-1]

    def params(self, rule_id: str, *, as_of: date | None = None) -> dict[str, Any]:
        return self.pack(rule_id, as_of=as_of).rules

    # ---- evaluation -----------------------------------------------------------------
    def evaluate(
        self,
        rule_id: str,
        facts: dict[str, Any],
        *,
        as_of: date | None = None,
        version: str | None = None,
    ) -> Decision:
        pack = self.pack(rule_id, as_of=as_of, version=version)
        try:
            result, explanation, details = _EVALUATORS[rule_id](pack.rules, facts)
        except KeyError as exc:
            raise ValidationFailure(
                f"{rule_id}: required fact {exc} is missing", code="rule_fact_missing"
            ) from exc
        return Decision(result, explanation, pack.id, pack.version, details)

    def run_embedded_tests(self) -> list[str]:
        """Run every pack's embedded tests. Returns failure messages (empty = pass)."""
        failures: list[str] = []
        for rid, versions in self._packs.items():
            for p in versions:
                for i, t in enumerate(p.tests):
                    try:
                        got, _, _ = _EVALUATORS[rid](p.rules, t.input)
                    except Exception as exc:  # noqa: BLE001 - report every failure
                        failures.append(f"{rid}@{p.version} test {t.name or i}: raised {exc!r}")
                        continue
                    if _normalise(got) != _normalise(t.expect):
                        failures.append(
                            f"{rid}@{p.version} test {t.name or i}: "
                            f"expected {t.expect!r} got {got!r}"
                        )
        return failures


def _normalise(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _normalise(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_normalise(x) for x in v]
    if isinstance(v, date):
        return v.isoformat()
    return str(v) if not isinstance(v, (str, bool, type(None))) else v


RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"


def default_registry(
    settings: PlatformSettings | None = None,
    on_override: Callable[[str, str], None] | None = None,
) -> RuleRegistry:
    from rules_engine import evaluators  # noqa: F401  (registers evaluators)

    return RuleRegistry(settings, on_override).load_dir(RULES_ROOT)
