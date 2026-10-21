"""AI governance: the inventory is complete, honest and consistent with the code."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INV = yaml.safe_load((ROOT / "governance" / "ai_inventory.yaml").read_text(encoding="utf-8"))
ENTRIES = {e["id"]: e for e in INV["entries"]}
ROUTES = yaml.safe_load(
    (ROOT / "services" / "model_gateway" / "config" / "routes.yaml").read_text(encoding="utf-8")
)


def test_every_agent_policy_has_an_inventory_entry_with_the_same_kill_switch() -> None:
    for d in sorted(
        p
        for p in (ROOT / "agents").iterdir()
        if (p / "agent.py").exists() or (p / "compiler.py").exists()
    ):
        assert (d / "policy.yaml").exists(), f"{d.name} has no policy.yaml"
    for pol in (ROOT / "agents").glob("*/policy.yaml"):
        spec = yaml.safe_load(pol.read_text(encoding="utf-8"))
        entry = ENTRIES.get(spec["agent_id"])
        assert entry is not None, f"{spec['agent_id']} is not in the AI inventory"
        assert entry["policy"] == pol.relative_to(ROOT).as_posix()
        assert entry["kill_switch"] == spec["kill_switch"]
        assert entry["risk_tier"] == spec["risk_tier"] and entry["owner"] == spec["owner"]
        assert set(entry["llm_use_cases"]) == set(spec["llm_use_cases"])
        assert entry["checkpoint_role"] == spec["human_checkpoint"]["role"]
        assert entry["data_class"] in spec["data_scope"]["data_classes"]
        assert "propose_kinds" in spec and isinstance(spec["propose_kinds"], list)


def test_every_gateway_use_case_is_inventoried_and_every_inventoried_one_is_routed() -> None:
    routed = set(ROUTES["use_cases"])
    listed = {u for e in INV["entries"] for u in e.get("llm_use_cases", [])}
    assert routed == listed, routed ^ listed


def test_cards_and_evidence_exist_and_nothing_is_claimed_validated() -> None:
    for e in INV["entries"]:
        assert (ROOT / e["card"]).exists(), e["id"]
        if "evidence" in e:
            assert (ROOT / e["evidence"]).exists(), e["id"]
        assert e["validation_status"] in {"not_validated", "validated", "validated_with_conditions"}
        if e["validation_status"] != "not_validated":
            report = ROOT / "docs" / "validation" / f"{e['id']}.md"
            assert report.exists(), f"{e['id']} claims validation without a report"
            body = report.read_text(encoding="utf-8")
            for field in ("Validator:", "Date:", "Evidence-SHA256:", "Scope:", "Conditions:"):
                assert field in body, f"{e['id']} validation report lacks {field}"
            assert "evidence" in e, f"{e['id']} claims validation without an evidence key"
        assert e["human_checkpoint"] and e["kill_switch"]


def test_agents_cannot_hold_write_or_money_tools() -> None:
    forbidden = ("limit.", "finacle.", "payments.", "case.decide", "cts.publish", "borrower.send")
    for pol in (ROOT / "agents").glob("*/policy.yaml"):
        spec = yaml.safe_load(pol.read_text(encoding="utf-8"))
        assert not [t for t in spec["tools_allowed"] if t.startswith(forbidden)], pol
        assert spec["human_checkpoint"], pol


def test_model_cards_state_status_and_limits() -> None:
    for card in (ROOT / "docs" / "model-cards").glob("*.md"):
        text = card.read_text(encoding="utf-8").lower()
        status = next((ln for ln in text.splitlines() if ln.startswith("**status:**")), "")
        assert "not independently validated" in status or "not validated" in status, card.name
        assert "limitation" in text or "what is not measured" in text, card.name
