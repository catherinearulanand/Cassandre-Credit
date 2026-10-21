"""EWS Analyst: explains an alert and, at the top level, proposes a precaution for a human.

Reads the early-warning state, drafts a short note from it (numbers only from the state) and, for a
critical alert, proposes the policy consequence ``stop_drawdown`` for the monitoring officer to
decide. It never opens a fraud file, classifies, or changes a limit. Missing indicators and data
gaps are stated in the note.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.runtime import AgentResult, AgentRun, draft_narrative, load_prompt

HERE = Path(__file__).parent


def _facts(state: dict[str, Any]) -> dict[str, str]:
    return {
        "score": str(state["score"]),
        "level": str(state["level"]),
        "indicators_fired": ", ".join(state["fired"]) or "none",
        "indicators_not_evaluated": str(len(state["not_evaluated"])),
        "data_gap": "yes" if state["data_gap"] else "no",
    }


def _template(f: dict[str, str]) -> str:
    gap = " Data is incomplete, so the score may understate risk." if f["data_gap"] == "yes" else ""
    return (
        f"Early-warning level is {f['level']} with score {f['score']}. "
        f"Indicators fired: {f['indicators_fired']}. "
        f"{f['indicators_not_evaluated']} indicator(s) could not be evaluated.{gap} "
        "Officer review is recommended."
    )


def run(
    agent_run: AgentRun, *, obligor_id: str, case_id: str, llm: Any | None = None
) -> AgentResult:
    out = AgentResult(agent_run)
    state = agent_run.call("ews.read_state", obligor_id=obligor_id)
    if state["level"] == "none":
        out.notes.append("no active alert")
        agent_run.finish({"proposals": []})
        return out
    _, instruction = load_prompt(HERE / "prompts" / "memo.md")
    draft = draft_narrative(
        llm,
        use_case="ews_analyst.memo",
        instruction=instruction,
        facts=_facts(state),
        template=_template,
    )
    out.drafts["note"] = draft
    if state["level"] == "critical":
        p = agent_run.call(
            "case.propose",
            case_id=case_id,
            kind="consequence",
            payload={
                "consequence": "stop_drawdown",
                "application": "AUTO",
                "reason": "critical early-warning level",
                "score": state["score"],
                "note": draft.text,
            },
        )
        out.proposals.append(str(p.proposal_id))
    agent_run.finish({"proposals": out.proposals, "draft_source": draft.source})
    return out
