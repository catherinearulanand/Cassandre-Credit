# Does this platform need an LLM?

**Short answer.** Not for the core. Reading statements, spreading, covenant testing, drawing power,
the drawdown gate and SMA classification are deterministic and are better without a model. An LLM
earns its place in four narrow jobs, and in each it works under a schema, behind the gateway, with a
human approving anything that matters. Switch every LLM off and the platform still runs; it just
needs more human hours on unusual documents.

## What the simulation measured

| Question | Result | What it means |
|---|---|---|
| Can a fixed grammar compile the bank's **standard** sanction-letter template? | 221 of 221 letters, every field correct, every field cited | Standard templates need no model. **Caveat:** the letters and the grammar come from the same template in this repository, so this proves the plumbing, not coverage of real letters. |
| Can the same grammar compile a **negotiated** facility agreement (defined terms, clause cross-references, paraphrase)? | 0 of 79 | This is the measured case for a model. Every real agreement that departs from the template lands here. |
| Do covenant tests, drawing power, SMA, the gate need a model? | Matched independent calculators on 13,540 covenant tests, 900 gate decisions, 4,330 SMA classifications, 900 DP cases | No, and a model would only add risk. |

## Where an LLM is used, and where it is not

| Job | LLM? | Why | Control |
|---|---|---|---|
| OCR text recognition | No | Specialised recognisers are faster, cheaper and calibrated | Engine ensemble behind one interface |
| Spreading: label to chart-of-accounts code | **No at first**, small classifier later | Fuzzy match plus arithmetic checks resolves standard labels; a model is only for the residue the matcher refuses | Matcher refuses below a score; humans see the rest |
| Arithmetic, ratios, DP, thresholds, limits, SMA/NPA, ECL stage | **Never** | Must be exact, reproducible, auditable | Deterministic code, rule packs, property tests |
| Gate decisions, limit changes, classification, posting to Finacle | **Never** | Only a rule or a named human acts | Maker-checker, signed approvals |
| Reading **negotiated agreements**: definitions, cross-references, amendments, carve-outs | **Yes** (self-hosted open-weight model) | Language is open-ended; grammars cannot enumerate it | Schema-constrained output; every value cites page and box; low-confidence and unresolved fields go to the CAD checker; raw borrower text never leaves the bank |
| **Clause-diff**: does the executed agreement match the CTS in meaning? | **Yes** | Same term, different wording | Output is a candidate deviation for a legal officer, never a decision |
| **Drafting** breach, waiver and reservation-of-rights letters; stress and review memos | **Yes** (frontier model only after redaction, via private zero-retention endpoint) | Prose quality and speed | Numbers come from tool results, never from the model; every sentence cites a source; a named human approves before anything is sent |
| Indic-script legal documents (title deeds, court orders): summaries, entity extraction | **Yes** | Needs language understanding | Self-hosted only; extraction is cited and reviewed |
| News and event classification for EWS | **Small fine-tuned classifier first**; LLM only to summarise | Cheaper, testable, no data egress | Event-type F1 on a labelled set before use |

## How an LLM is kept regulated and efficient

1. **One gateway.** No service imports a provider SDK (an architecture test enforces it). Routing, data
   class, redaction, budgets, logging and the kill switch live in one place.
2. **Smallest adequate model.** Classification and extraction use self-hosted models; the frontier
   model is reserved for drafting. Cost is capped per use case per day and rate limited.
3. **Data classes.** Raw borrower data may only reach self-hosted models. Anything leaving that zone is
   redacted first (PAN, Aadhaar, accounts, phones, emails, named individuals, in many spellings) and
   re-identified on return. Redaction cannot be turned off by configuration.
4. **Untrusted input.** Document text goes in delimited data blocks that cannot close themselves, after an
   injection screen. Output must validate against a JSON Schema, with one repair retry.
5. **Proven before enabled.** Each use case needs a golden set, a measured accuracy, a tested fallback and
   an owner. The evaluation is the entry ticket: an LLM that does not beat the deterministic baseline on
   the golden set stays off.
6. **Human accountability.** Every action an agent proposes carries the human it acts for. Agents inherit
   that human's scope and can never hold a decision permission.

## What we have not measured

No model was available in this build environment, so the LLM path is specified, wired through the
gateway with a deterministic fallback, and tested for policy, but its extraction accuracy on negotiated
agreements is **not measured**. The first task for the pilot is to run the self-hosted model on a golden
set of consented agreements and publish the same style of report as the OCR one, including the share of
fields that still need a human.
