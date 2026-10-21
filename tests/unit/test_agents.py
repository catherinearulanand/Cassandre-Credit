"""Agent runtime and the four agents: bounded tools, human-bound proposals, numbers from tools."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from authz import MakerChecker, Permission, Principal, ResourceScope, Role
from case_service.domain import CaseService
from domain.entities import CaseType
from model_gateway.gateway import KillSwitchActive
from platform_core.clock import FixedClock
from platform_core.errors import ValidationFailure
from recovery_service.domain import RecoveryService
from security.crypto import LocalDevKeyProvider, Signer

from agents.covenant_agent import agent as covenant_agent
from agents.ews_analyst import agent as ews_analyst
from agents.recovery_agent import agent as recovery_agent
from agents.review_memo import agent as review_memo
from agents.runtime import (
    AgentBudgetExceeded,
    AgentDenied,
    AgentRun,
    AgentSpec,
    ToolSpec,
    draft_narrative,
    load_prompt,
    verify_numbers,
)

ROOT = Path(__file__).resolve().parents[2] / "agents"
SCOPE = ResourceScope("WEST", "MIDCORP", "OBL-1")


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(datetime(2026, 10, 21, 9, 0, tzinfo=UTC))


@pytest.fixture
def cases(clk: FixedClock) -> CaseService:
    return CaseService(clk, MakerChecker(clk, Signer(LocalDevKeyProvider())))


@pytest.fixture
def human() -> Principal:
    return Principal(
        id="mo1",
        kind="human",
        roles=frozenset({Role.MONITORING_OFFICER}),
        regions=frozenset({"WEST"}),
    )


def _tools(cases: CaseService, extra: dict[str, ToolSpec] | None = None) -> dict[str, ToolSpec]:
    t = {
        "case.propose": ToolSpec("case.propose", cases.propose, "propose"),
        "rules.consequence_mode": ToolSpec(
            "rules.consequence_mode",
            lambda consequence: "AUTO" if consequence == "stop_drawdown" else "APPROVAL",
            "compute",
        ),
    }
    t.update(extra or {})
    return t


def _run(agent: str, human: Principal, tools: dict[str, ToolSpec], emit=None) -> AgentRun:  # type: ignore[no-untyped-def]
    spec = AgentSpec.load(ROOT / agent / "policy.yaml")
    return AgentRun(spec, human, SCOPE, tools, emit=emit)


# --------------------------------------------------------------------- runtime -------
def test_deny_by_default_and_denials_are_audited(cases, human) -> None:  # type: ignore[no-untyped-def]
    events: list[Any] = []
    run = _run("ews_analyst", human, _tools(cases), emit=lambda t, p: events.append((t, p)))
    for name in ("limit.change", "finacle.post", "rules.consequence_mode"):
        with pytest.raises(AgentDenied):
            run.call(name)  # explicit deny, or simply not on this agent's allow-list
    ev = run.finish({})
    assert ev.denied_calls == 3 and events[0][0] == "agent.run.completed"
    with pytest.raises(AgentDenied):
        run.call("case.propose")  # finished runs accept no more calls


def test_agent_cannot_choose_its_own_identity_or_the_human(cases, human) -> None:  # type: ignore[no-untyped-def]
    case = cases.open_case(CaseType.EWS, "OBL-1", SCOPE, "x", sla_days=3)
    run = _run("ews_analyst", human, _tools(cases))
    for forged in (
        {"proposed_by": "mo1"},
        {"on_behalf_of": "someone-else"},
        {"proposer_kind": "human"},
    ):
        with pytest.raises(AgentDenied):
            run.call("case.propose", case_id=case.case_id, kind="consequence", payload={}, **forged)
    p = run.call("case.propose", case_id=case.case_id, kind="consequence", payload={"c": 1})
    assert p.proposed_by == "ews_analyst" and p.proposer_kind == "agent" and p.on_behalf_of == "mo1"


def test_scope_is_inherited_from_the_human(cases) -> None:  # type: ignore[no-untyped-def]
    east = Principal(
        id="mo-east",
        kind="human",
        roles=frozenset({Role.MONITORING_OFFICER}),
        regions=frozenset({"EAST"}),
    )
    tools = {
        "ews.read_state": ToolSpec(
            "ews.read_state", lambda obligor_id: {}, "read", Permission.READ_OBLIGOR
        )
    }
    run = _run("ews_analyst", east, tools)
    with pytest.raises(AgentDenied):
        run.call("ews.read_state", obligor_id="OBL-1")  # case is in WEST


def test_decision_tools_are_refused_even_if_registered_and_allowed(cases, human) -> None:  # type: ignore[no-untyped-def]
    tools = {
        "ews.read_state": ToolSpec(
            "ews.read_state", lambda obligor_id: {}, "read", Permission.CASE_DECIDE
        )
    }
    run = _run("ews_analyst", human, tools)
    with pytest.raises(AgentDenied):
        run.call("ews.read_state", obligor_id="OBL-1")  # agents hold no decision rights


def test_tool_call_budget(cases, human) -> None:  # type: ignore[no-untyped-def]
    spec = AgentSpec.load(ROOT / "ews_analyst" / "policy.yaml")
    spec = AgentSpec(**{**spec.__dict__, "max_tool_calls": 2})
    tools = {"ews.read_state": ToolSpec("ews.read_state", lambda obligor_id: {}, "read")}
    run = AgentRun(spec, human, SCOPE, tools)
    run.call("ews.read_state", obligor_id="a")
    run.call("ews.read_state", obligor_id="b")
    with pytest.raises(AgentBudgetExceeded):
        run.call("ews.read_state", obligor_id="c")


def test_every_agent_policy_loads_and_prompts_have_front_matter() -> None:
    for name in (
        "covenant_agent",
        "ews_analyst",
        "review_memo",
        "recovery_agent",
        "sanction_compiler",
    ):
        spec = AgentSpec.load(ROOT / name / "policy.yaml")
        assert spec.risk_tier >= 1 and spec.tools_denied
        assert not any(
            t.startswith(("limit.", "finacle.", "payments.")) for t in spec.tools_allowed
        )
    for p in (ROOT).glob("*/prompts/*.md"):
        meta, body = load_prompt(p)
        assert meta["prompt_id"] and body
    with pytest.raises(ValidationFailure):
        load_prompt(ROOT / "runtime.py")


# ----------------------------------------------------------------- number binding ----
def test_verify_numbers_accepts_formatting_and_ignores_dates_and_ids() -> None:
    allowed = ["1.42", "₹1,50,000", "12.5%", "5000000"]
    ok = "On 2026-09-30 case CASE-01 showed DSCR 1.42, ₹150000 or 12.5 percent; 5,000,000 paise."
    assert verify_numbers(ok, allowed) == []
    assert verify_numbers("The ratio improved to 1.50.", allowed) == ["1.50"]
    assert verify_numbers("Revenue grew 18%", allowed) == ["18%"]


def _llm(text: str | None = None, exc: Exception | None = None) -> Any:
    def structured(**kw: Any) -> Any:
        if exc:
            raise exc
        return SimpleNamespace(parsed={"text": text}, call_id="LLM-1")

    return SimpleNamespace(structured=structured)


def test_draft_uses_model_text_only_when_numbers_check_out() -> None:
    facts = {"value": "1.42", "threshold": "1.50"}
    tmpl = lambda f: f"value {f['value']} vs {f['threshold']}"  # noqa: E731
    kw = dict(use_case="u", instruction="i", facts=facts, template=tmpl)
    good = draft_narrative(_llm("DSCR was 1.42 against 1.50."), **kw)  # type: ignore[arg-type]
    assert good.source == "llm" and good.call_id == "LLM-1"
    bad = draft_narrative(_llm("DSCR was 1.45 against 1.50."), **kw)  # type: ignore[arg-type]
    assert bad.source == "template" and bad.unsupported_numbers == ("1.45",)
    off = draft_narrative(_llm(exc=KillSwitchActive("off")), **kw)  # type: ignore[arg-type]
    assert (
        off.source == "template" and "kill" in (off.fallback_reason or "").lower()
    ) or off.source == "template"
    assert draft_narrative(None, **kw).source == "template"  # type: ignore[arg-type]
    assert draft_narrative(_llm("   "), **kw).source == "template"  # type: ignore[arg-type]


# ------------------------------------------------------------------------ agents -----
def _breach_record(result: str = "BREACHED") -> dict[str, Any]:
    return {
        "result": result,
        "value": "1.42",
        "threshold": "1.50",
        "operator": ">=",
        "period_end": date(2026, 9, 30).isoformat(),
    }


def test_covenant_agent_proposes_but_never_applies(cases, human) -> None:  # type: ignore[no-untyped-def]
    case = cases.open_case(CaseType.COVENANT_BREACH, "OBL-1", SCOPE, "DSCR", sla_days=5)
    tools = _tools(
        cases,
        {
            "covenant.read_result": ToolSpec(
                "covenant.read_result", lambda test_id: _breach_record(), "read"
            )
        },
    )
    run = _run("covenant_agent", human, tools)
    res = covenant_agent.run(
        run,
        test_id="TST-1",
        case_id=case.case_id,
        covenant_name="DSCR",
        consequences=["stop_drawdown", "reprice:+50bps"],
    )
    assert len(res.proposals) == 3  # two consequences + one letter
    kinds = sorted(p.kind for p in case.proposals)
    assert kinds == ["consequence", "consequence", "letter"]
    assert all(p.status == "pending" and p.proposer_kind == "agent" for p in case.proposals)
    letter = res.drafts["reservation_of_rights"]
    assert letter.source == "template" and "reserves all of its rights" in letter.text
    assert not verify_numbers(letter.text, ["1.42", "1.50", "2026-09-30"])


def test_covenant_agent_ignores_compliant_or_incomplete_results(cases, human) -> None:  # type: ignore[no-untyped-def]
    case = cases.open_case(CaseType.COVENANT_BREACH, "OBL-1", SCOPE, "x", sla_days=5)
    for rec in (_breach_record("COMPLIED"), {**_breach_record(), "value": None}):
        tools = _tools(
            cases,
            {
                "covenant.read_result": ToolSpec(
                    "covenant.read_result", lambda test_id, r=rec: r, "read"
                )
            },
        )
        res = covenant_agent.run(
            _run("covenant_agent", human, tools),
            test_id="T",
            case_id=case.case_id,
            covenant_name="DSCR",
            consequences=["cap"],
        )
        assert res.proposals == [] and res.notes
    assert case.proposals == []


def test_covenant_agent_uses_model_but_rejects_invented_figures(cases, human) -> None:  # type: ignore[no-untyped-def]
    case = cases.open_case(CaseType.COVENANT_BREACH, "OBL-1", SCOPE, "x", sla_days=5)
    tools = _tools(
        cases,
        {
            "covenant.read_result": ToolSpec(
                "covenant.read_result", lambda test_id: _breach_record(), "read"
            )
        },
    )
    res = covenant_agent.run(
        _run("covenant_agent", human, tools),
        test_id="T",
        case_id=case.case_id,
        covenant_name="DSCR",
        consequences=[],
        llm=_llm("The DSCR of 1.42 missed 1.50 by 5% and the bank reserves its rights."),
    )
    d = res.drafts["reservation_of_rights"]
    assert d.source == "template" and "5%" in d.unsupported_numbers


def _ews_state(level: str) -> dict[str, Any]:
    return {
        "score": 72,
        "level": level,
        "fired": ["EWS01_cheque_returns"],
        "not_evaluated": ["a", "b"],
        "data_gap": True,
    }


def test_ews_analyst_proposes_precaution_only_at_critical(cases, human) -> None:  # type: ignore[no-untyped-def]
    case = cases.open_case(CaseType.EWS, "OBL-1", SCOPE, "x", sla_days=3)
    for level, expect in (("none", 0), ("elevated", 0), ("critical", 1)):
        tools = _tools(
            cases,
            {
                "ews.read_state": ToolSpec(
                    "ews.read_state", lambda obligor_id, lv=level: _ews_state(lv), "read"
                )
            },
        )
        res = ews_analyst.run(
            _run("ews_analyst", human, tools), obligor_id="OBL-1", case_id=case.case_id
        )
        assert len(res.proposals) == expect
    p = case.proposals[0]
    assert p.payload["consequence"] == "stop_drawdown" and "Data is incomplete" in p.payload["note"]


def test_review_memo_refuses_when_too_much_is_missing(cases, human) -> None:  # type: ignore[no-untyped-def]
    def pack(missing: list[str]) -> dict[str, Any]:
        return {
            "values": {"rating": "CR3", "latest_dscr": "1.42"},
            "missing": missing,
            "pack_hash": "h",
        }

    ok = {
        "review.read_pack": ToolSpec(
            "review.read_pack", lambda obligor_id: pack(["ews_level"]), "read"
        )
    }
    res = review_memo.run(_run("review_memo", human, ok), obligor_id="OBL-1")
    assert "Not available for this review: ews_level" in res.drafts["memo"].text
    many = {
        "review.read_pack": ToolSpec(
            "review.read_pack", lambda obligor_id: pack(list("abcd")), "read"
        )
    }
    res2 = review_memo.run(_run("review_memo", human, many), obligor_id="OBL-1")
    assert "memo" not in res2.drafts and res2.notes


def test_recovery_agent_files_an_ots_proposal_with_exact_numbers(cases, human) -> None:  # type: ignore[no-untyped-def]
    case = cases.open_case(CaseType.RECOVERY, "OBL-1", SCOPE, "ots", sla_days=10)

    def analyse(
        outstanding_paise: int, offer_paise: int, security_realisable_paise: int
    ) -> dict[str, Any]:
        a = RecoveryService.analyse_ots(outstanding_paise, offer_paise, security_realisable_paise)
        return {
            "outstanding_paise": a.outstanding_paise,
            "offer_paise": a.offer_paise,
            "security_realisable_paise": a.security_realisable_paise,
            "recovery_ratio": str(a.recovery_ratio),
            "below_security_value": a.below_security_value,
            "haircut_paise": a.haircut_paise,
        }

    tools = _tools(
        cases, {"recovery.analyse_ots": ToolSpec("recovery.analyse_ots", analyse, "compute")}
    )
    res = recovery_agent.run(
        _run("recovery_agent", human, tools),
        case_id=case.case_id,
        outstanding_paise=1_000_000,
        offer_paise=400_000,
        security_realisable_paise=600_000,
        terms="lump sum in 30 days",
    )
    p = case.proposals[0]
    assert (
        p.kind == "ots"
        and p.payload["recovery_ratio"] == "0.4000"
        and p.payload["below_security_value"]
    )
    assert "recovery ratio of 0.4000" in res.drafts["summary"].text
