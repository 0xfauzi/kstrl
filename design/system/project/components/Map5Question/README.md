Proposal: asking questions in ⌘K, answered from the run records rather than by a model.

**Why.** After time away the questions are few and concrete: why did this stop, what is waiting on me, what did it cost, what did the architect decide, what runs next and why not yet, is anything not being checked. Each has a source kstrl already writes, so the answer is a lookup: the stop reason and tries from `manifest.json` and `events.jsonl`, the engineer's last output from `engineer.log`, cost by phase from `component_usage`, decisions from `decisions.json`, the queue and its admission gates from `ks serve`, safe mode's four signals. The sources are listed under every answer.

**Retry waits for the run.** `ks retry` re-enters the factory, which needs the lock the live run holds, so during a run the row says "after this run" and the bar shows the action unavailable. ↵ on the question goes to search-highlight; it never retries.

**Not in kstrl**: free-form questions answered by a model. This design does not assume them.
