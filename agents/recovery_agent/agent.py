"""Recovery Agent: analyses a one-time settlement offer and prepares the committee proposal.

The recovery ratio, the haircut and the comparison with realisable security come from a
deterministic tool. The agent attaches a short summary and files the proposal; it cannot approve,
negotiate, designate a wilful defaulter or touch an account.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.runtime import AgentResult, AgentRun, draft_narrative, load_prompt

HERE = Path(__file__).parent


def _template(f: dict[str, str]) -> str:
    below = "below" if f["below_security_value"] == "yes" else "not below"
    return (
        f"Outstanding is {f['outstanding_paise']} paise. The offer is {f['offer_paise']} paise, "
        f"a recovery ratio of {f['recovery_ratio']}. Realisable security is "
        f"{f['security_realisable_paise']} paise; the offer is {below} that value."
    )


def run(
    agent_run: AgentRun,
    *,
    case_id: str,
    outstanding_paise: int,
    offer_paise: int,
    security_realisable_paise: int,
    terms: str,
    llm: Any | None = None,
) -> AgentResult:
    out = AgentResult(agent_run)
    a = agent_run.call(
        "recovery.analyse_ots",
        outstanding_paise=outstanding_paise,
        offer_paise=offer_paise,
        security_realisable_paise=security_realisable_paise,
    )
    facts = {
        "outstanding_paise": str(a["outstanding_paise"]),
        "offer_paise": str(a["offer_paise"]),
        "security_realisable_paise": str(a["security_realisable_paise"]),
        "recovery_ratio": str(a["recovery_ratio"]),
        "below_security_value": "yes" if a["below_security_value"] else "no",
    }
    _, instruction = load_prompt(HERE / "prompts" / "draft.md")
    draft = draft_narrative(
        llm,
        use_case="recovery_agent.draft",
        instruction=instruction,
        facts=facts,
        template=_template,
    )
    out.drafts["summary"] = draft
    p = agent_run.call(
        "case.propose",
        case_id=case_id,
        kind="ots",
        payload={**a, "terms": terms, "summary": draft.text, "summary_source": draft.source},
    )
    out.proposals.append(str(p.proposal_id))
    agent_run.finish({"proposals": out.proposals})
    return out
