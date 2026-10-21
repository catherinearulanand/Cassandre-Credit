"""Review Memo Agent: writes the narrative of a periodic review from the fact pack only.

The pack is the single source of numbers. The memo lists every fact the pack was missing instead of
smoothing over it, and refuses to produce a memo when too many required sections are absent. The
agent proposes nothing: the memo is input to a human review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.runtime import AgentResult, AgentRun, closing, draft_narrative, load_prompt

HERE = Path(__file__).parent
MAX_MISSING = 3


def _template(f: dict[str, str]) -> str:
    lines = [f"{k.replace('_', ' ').capitalize()}: {v}." for k, v in f.items() if k != "missing"]
    if f.get("missing"):
        lines.append(f"Not available for this review: {f['missing']}.")
    return " ".join(lines)


@closing
def run(agent_run: AgentRun, *, obligor_id: str, llm: Any | None = None) -> AgentResult:
    out = AgentResult(agent_run)
    pack = agent_run.call("review.read_pack", obligor_id=obligor_id)
    if len(pack["missing"]) > MAX_MISSING:
        out.notes.append(
            "too many required sections are missing to draft a memo: " + ", ".join(pack["missing"])
        )
        agent_run.finish({"memo": None})
        return out
    facts = dict(pack["values"])
    facts["missing"] = ", ".join(pack["missing"])
    _, instruction = load_prompt(HERE / "prompts" / "draft.md")
    draft = draft_narrative(
        llm, use_case="review_memo.draft", instruction=instruction, facts=facts, template=_template
    )
    out.drafts["memo"] = draft
    agent_run.finish({"memo_source": draft.source, "pack": pack["pack_hash"]})
    return out
