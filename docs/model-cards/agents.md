# Agent cards

Common design (`agents/runtime.py`): an agent acts for one named human, inherits that human's scope, may
call only tools on its allow-list, and has no write tools. It can *propose*; the runtime stamps the proposal
with the agent identity and the human it acts for, and a different human decides. Numbers in any drafted text
must come from tool results, or the draft is replaced by a deterministic template. Every call, including
denied ones, is logged as `agent.run.completed`. Status for all: **development, not validated, LLM path not measured.**

| Agent | Does | Never does | Human step |
|---|---|---|---|
| Sanction Compiler | Drafts a CTS from sanction documents with citations; lists unresolved fields | Publishes, invents a value | CAD checker publishes |
| Covenant Agent | After a recorded breach, proposes each policy consequence and drafts a reservation-of-rights letter | Applies a consequence, sends a letter, grants a waiver, touches limits | Monitoring officer decides and sends |
| EWS Analyst | Explains an alert; at the critical level proposes `stop_drawdown` | Opens a fraud file, classifies, changes limits | A second officer decides (the agent's own principal cannot) |
| Review Memo | Writes the review narrative from the fact pack; lists missing facts; refuses if more than 3 required sections are missing | Proposes anything; recommends a rating or limit change | Reviewer and sanctioning authority |
| Recovery Agent | Computes the settlement ratio and haircut via a deterministic tool; files an OTS proposal | Negotiates, approves, designates wilful defaulters | Recovery head, at delegated power |

## Tests that prove the controls (`tests/unit/test_agents.py`, `tests/integration/test_lifecycle.py`)
Deny by default and audited; identity and principal cannot be forged; scope inherited; decision permissions
refused even if a tool is registered; call budget; invented figures rejected; a pending proposal changes
nothing; the agent's own principal cannot approve its work.

## What is not measured
How often a real model's drafts would be rejected for inventing a number, and how good the drafts are. Until
the model path is run on a golden set, the deterministic template is what ships. The first pilot task is to
measure the rejection rate and reviewer edit distance.
