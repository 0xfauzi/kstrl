Proposal: planning stopped. search finished at 22:31 over the $40 daily budget, so `ks serve` paused the queue until midnight. At 00:01 it started `tags`; the architect closed three questions itself and refused one. The run did not start. This is the Spec level's Text view in that state.

**Why this shape.** The question is about your words, so it sits beside them: the sentence it concerns is marked in `you`, and the question itself is the one ask on the screen. The three questions the architect closed itself stay visible under it, each pinned to its sentence and labelled with its disposition. Escalating is supposed to be rare, and seeing what was closed without you is how you judge whether it was.

**The four dispositions**, from the architect's prompt: `decided` (a choice with the alternative it rejected), `assumed` (a default, which must be pinned by an acceptance criterion), `spiked` (a fact it observed by running a command, or a spike part placed before the part that needs the answer) and `escalated` (a product, scope or risk judgement, or two incompatible architectures that would be expensive to unwind). Only `escalated` halts.

**What the screen has to say, because the code does it:**
- A halted plan writes `spec-issues.json` and `decisions.json` (marked `halted`) and stops before `manifest.json`. There are no parts, so the Graph view has nothing to show and says so.
- Under `ks serve` the run exits 2, which serve classifies as `spec_failure`. That is never retried, so the queue item is poisoned at once and counts toward the poisoned streak (3 pauses the queue).
- The queue ran a copy of the spec (`queue add` copies it in). Editing `specs/tags.md` does not reach the stopped item. Answering therefore removes the poisoned item and adds the edited spec again at its priority (`ks queue rm`, then `ks queue add --priority 5`).
- One event files two inbox items: the architect's `spec_escalation` and `ks serve`'s "Queue item … poisoned". Needs you shows them as one ask, joined on the run id.
- The header reads "not live · last event 00:04": nothing is running while the question waits.

**Built on**: `scripts/kstrl/decisions.json` (question, disposition, resolution, reason, alternative, component), `spec-issues.json` (location places the pins), the queue item's `meta.json` and journal, and `inbox.jsonl`.
