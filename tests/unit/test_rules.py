from __future__ import annotations

from datetime import date

import pytest
from platform_core.config import PlatformSettings, Profile
from rules_engine import RULES_ROOT, RuleLoadError, RuleRegistry


def test_all_embedded_rule_tests_pass(rules: RuleRegistry) -> None:
    assert rules.run_embedded_tests() == []


def test_expected_packs_loaded(rules: RuleRegistry) -> None:
    expected = {
        "rbi.sma",
        "rbi.irac",
        "rbi.crilc",
        "rbi.cersai",
        "statute.companies_act_chg1",
        "rbi.document_release",
        "rbi.rfa",
        "rbi.wilful_defaulter",
        "rbi.prudential_framework",
        "rbi.penal_charges",
        "rbi.large_exposures",
        "rbi.ecl",
        "policy.credit_policy",
        "policy.approval_matrix",
        "policy.covenant_consequences",
        "policy.monitoring",
        "ews.indicators",
    }
    assert expected <= set(rules.ids())


def test_every_pack_has_source_note_and_tests(rules: RuleRegistry) -> None:
    for rid in rules.ids():
        p = rules.pack(rid, as_of=date(2030, 1, 1)) if rid == "rbi.ecl" else rules.pack(rid)
        assert p.source.source_note.strip()
        assert p.tests, f"{rid} has no embedded tests"


def test_uat_refuses_unverified() -> None:
    from rules_engine import evaluators  # noqa: F401

    with pytest.raises(RuleLoadError) as ei:
        RuleRegistry(PlatformSettings(profile=Profile.UAT)).load_dir(RULES_ROOT)
    assert ei.value.code == "unverified_rule_in_strict_profile"


def test_uat_override_is_reported() -> None:
    from rules_engine import evaluators  # noqa: F401

    seen: list[tuple[str, str]] = []
    reg = RuleRegistry(
        PlatformSettings(profile=Profile.PROD, allow_unverified_rules=True),
        on_override=lambda i, v: seen.append((i, v)),
    )
    reg.load_dir(RULES_ROOT)
    assert ("rbi.sma", "2026.10.0") in seen


def test_decision_records_version(rules: RuleRegistry) -> None:
    d = rules.evaluate("rbi.sma", {"type": "non_revolving", "dpd": 45})
    assert d.result == "SMA-1" and d.rule_id == "rbi.sma" and d.rule_version == "2026.10.0"


def test_as_of_selects_version_in_force(rules: RuleRegistry) -> None:
    with pytest.raises(RuleLoadError):
        rules.evaluate("rbi.ecl", {"dpd": 0}, as_of=date(2026, 10, 19))  # not yet effective
    assert rules.evaluate("rbi.ecl", {"dpd": 45}, as_of=date(2027, 5, 1)).result == 2
