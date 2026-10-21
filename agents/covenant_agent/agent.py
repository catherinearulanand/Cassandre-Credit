"""Covenant Agent: turns a recorded breach into proposals and a draft letter for a human.

It reads the test result, looks up how each consequence is applied under policy, and files one
proposal per consequence plus one draft reservation-of-rights letter. It never applies a
consequence, sends a letter, grants a waiver or touches limits: those are human decisions and the
runtime does not give it the tools. Numbers in the letter come from the recorded test result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.runtime import AgentResult, AgentRun, draft_narrative, load_prompt

HERE = Path(__file__).parent
_BREACH = {"BREACHED"}


def _facts(rec: dict[str, Any], covenant_name: str) -> dict[str, str]:
    return {
        "covenant": covenant_name,
        "tested_value": str(rec["value"]),
        "threshold": str(rec["threshold"]),
        "operator": str(rec["operator"]),
        "period_end": str(rec["period_end"]),
    }


def _template(f: dict[str, str]) -> str:
    return (
        f'We refer to the financial covenant "{f["covenant"]}" in your facility documents. '
        f"For the period ended {f['period_end']} the tested value was {f['tested_value']} against "
        f"the required threshold ({f['operator']} {f['threshold']}). The bank reserves all of its "
        "rights and remedies under the facility documents and applicable law, and nothing in this "
        "letter waives any of them."
    )


def run(
    agent_run: AgentRun,
    *,
    test_id: str,
    case_id: str,
    covenant_name: str,
    consequences: list[str],
    llm: Any | None = None,
) -> AgentResult:
    out = AgentResult(agent_run)
    rec = agent_run.call("covenant.read_result", test_id=test_id)
    if rec["result"] not in _BREACH:
        out.notes.append(f"result is {rec['result']}: nothing to propose")
        agent_run.finish({"proposals": []})
        return out
    if rec.get("value") is None or rec.get("threshold") is None or rec.get("operator") is None:
        out.notes.append("breach record is incomplete; refusing to draft")
        agent_run.finish({"proposals": []})
        return out
    for c in consequences:
        mode = agent_run.call("rules.consequence_mode", consequence=c)
        p = agent_run.call(
            "case.propose",
            case_id=case_id,
            kind="consequence",
            payload={"consequence": c, "application": mode, "test_id": test_id},
        )
        out.proposals.append(str(p.proposal_id))
    facts = _facts(rec, covenant_name)
    _, instruction = load_prompt(HERE / "prompts" / "draft_letter.md")
    draft = draft_narrative(
        llm,
        use_case="covenant_agent.draft_letter",
        instruction=instruction,
        facts=facts,
        template=_template,
    )
    out.drafts["reservation_of_rights"] = draft
    p = agent_run.call(
        "case.propose",
        case_id=case_id,
        kind="letter",
        payload={
            "letter_type": "reservation_of_rights",
            "text": draft.text,
            "drafted_by": draft.source,
            "test_id": test_id,
        },
    )
    out.proposals.append(str(p.proposal_id))
    agent_run.finish({"proposals": out.proposals, "draft_source": draft.source})
    return out
