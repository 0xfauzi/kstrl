Proposal: the inbox. Every ask and notice kstrl has filed for this repo, and for each one what an answer actually does. It opens from the Needs you strip (which shows only the asks) and from ⌘K ("What is waiting on me?").

**Why this shape.** A list on the left, grouped the way kstrl groups items: waiting on you (the kinds kstrl marks `action_required`), for your information (the rest), and closed. The selected item on the right says why it is here, what waits on it, the evidence kstrl recorded, and every action with its consequence before you take it. Within a group the order is kstrl's: priority, then oldest first.

**Nine kinds, and what answering does.** For only one of them does approve or reject change anything in kstrl. For the rest, the screen offers the action that does, and says that closing changes nothing.

| kind | shown as | the action that does something | approve / reject |
|---|---|---|---|
| `merge_gate` | Merge approval | Approve merges the exact commit that was parked (`head_sha`); if the branch moved, the part fails. Reject fails it and skips its dependents. Either takes effect when a factory run starts. | acts (starts `ks factory`) |
| `halted_run` | Stopped | Retry resets the part and its skipped dependents (`ks inbox retry`) | closes only |
| `spec_escalation` | A question from the architect | Change the spec; a clean plan resolves it | closes only |
| `budget_overrun` | Budget reached | Raise `[factory] max_cost_usd` or `[serve] daily_budget_usd`, or wait for midnight | closes only |
| `policy_exception` | Policy exception | None in kstrl today | closes only |
| `test_adequacy` | Tests too weak | Retry with guidance | closes only |
| `demotion_notice` | Trust lowered | None; a notice | closes only |
| `calibration_drift` | Reviewer calibration slipped | None; a notice | closes only |
| `health_breach` | Health check | None; a notice | closes only |

**When an approval takes effect.** kstrl applies merge decisions only when a factory run starts, before it schedules anything (`factory.py:4526`). `ks inbox approve` records the decision and then starts `ks factory` itself. If the run that parked the merge is still going, that second run is refused by the run lock ("Another kstrl invocation holds factory.lock"), and the decision waits for the next run. At 21:40 search is still running, so the Approve card says the merge happens after this run ends. The app starts that run when the lock frees; that is the app's behaviour, not kstrl's. From a terminal you would run `ks factory --manifest scripts/kstrl/manifest.json` yourself.

**Rules from the code:**
- A repeat of an open item adds to it (`occurrences`, `last_seen_at`) rather than filing a new one. "Seen twice" is that count.
- Snooze lasts 24 hours by default. A snoozed item is not open, so it does not count toward the cap, but a snoozed merge approval stays parked, so `ks serve` still waits.
- Items close themselves when their part completes, when a PR closes, or when a clean plan resolves an escalation.
- Nothing expires except a snooze.
- At 50 open items `ks serve` admits no new spec. Emitters keep filing past 50, so the bar can pass the end.
- Reject always needs a reason.

**Titles** are composed from the kind, the part and the evidence ("search-index is ready to merge"), not taken from kstrl's free-text title ("search-index awaiting merge approval"). The free text is still under ⌘K › Show the record.

**Built on**: `${XDG_STATE_HOME}/kstrl/<repo-id>/inbox.jsonl` (append-only; the latest line per id wins); the part's `manifest.json` entry and `review_result` counts for the evidence; the dependency graph for "what waits on it".
