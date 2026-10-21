# Agent cards

**Status:** development. Not independently validated. The LLM drafting path is not measured; the deterministic templates are what ships.

## Common design (`agents/runtime.py`)
An agent acts for one named human and may call only tools on its allow-list. It has no write tools: it can
*propose*, and only the proposal kinds listed in its policy (`propose_kinds`). The runtime stamps each proposal
with the agent identity and the human it acts for and refuses those fields from the agent, whatever a tool is
labelled. Every call is authorised against the resource it names (case, obligor), not one fixed scope. Only
parameters a tool declares are accepted. Failed and denied attempts count against the budget and are logged, and
a run that crashes is still audited once as `agent.run.completed`.

Drafted prose is held to four rules, enforced by `draft_narrative` and `verify_numbers`:
1. facts reach the model only as screened, redacted data blocks (structured `context` is dropped by the gateway for any non-local provider);
2. every digit run must equal a fact with the same unit (a ratio is not a percentage, paise are not rupees, ids and dates are not exempt) and number words must appear in a fact;
3. no waiver, extension, forbearance or release language;
4. required wording (for the covenant letter, the reservation of rights) must be present.
Anything else is replaced by the deterministic template.

| Agent | Does | Proposal kinds | Never does | Human step |
|---|---|---|---|---|
| Sanction Compiler | Drafts a CTS from sanction documents with citations; lists unresolved fields | none (writes a draft) | Publishes, invents a value | CAD checker publishes |
| Covenant Agent | After a recorded breach, proposes each policy consequence and drafts a reservation-of-rights letter | consequence, letter | Applies a consequence, sends a letter, grants a waiver, touches limits | Monitoring officer decides and sends |
| EWS Analyst | Explains an alert; at the critical level proposes `stop_drawdown` | consequence | Opens a fraud file, classifies, changes limits | A second officer decides; the agent's own principal cannot |
| Review Memo | Writes the review narrative from the fact pack; lists missing facts; refuses if more than 3 required sections are missing | none | Proposes anything; recommends a rating or limit change | Reviewer and sanctioning authority |
| Recovery Agent | Computes the settlement ratio and haircut via a deterministic tool; files an OTS proposal | ots | Negotiates, approves, designates wilful defaulters | Recovery head, at delegated power |

## Tests that prove the controls
`tests/unit/test_agents.py`, `tests/integration/test_lifecycle.py` and the regression tests named after the
audit findings: deny by default and audited; identity cannot be forged under a mislabelled tool; proposal kinds and
scope enforced per call; decision permissions refused; budgets count failures; invented, glued, scaled and
re-united figures rejected; concessions rejected; a pending proposal changes nothing; the agent's principal cannot
approve its work.

## Limitations (known, not hidden)
- `verify_numbers` and the concession screen are pattern checks. They stop the bypasses found in review but are not a proof; a human still approves every letter.
- Spelled-out numbers are rejected unless the same word is in a fact, which will also reject harmless prose ("first quarter") and send it to the template.
- How often a real model's drafts are rejected, and how good they are, is not measured. The first pilot task is to measure the rejection rate and reviewer edit distance on a golden set.
