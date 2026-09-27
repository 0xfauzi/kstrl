How a live view changes: search-highlight's card across its first try, each change labelled with the event that caused it, and the rules every level keeps. The cards are the Part card component, and each changed value carries Change (`k-fresh`, shown here paused a quarter of the way through its fade).

**What moves, and why**, from kstrl's events:
- `worker_heartbeat`, every 15 s (`HEARTBEAT_INTERVAL_SECONDS`), written to the worker's `engineer.jsonl`. The run state merges it, so the header's "last event" age starts again, and nothing on the card changes.
- `iteration_started` marks the iteration count.
- `phase_completed` with `component_usage`: spend appears, because kstrl counts spend when a phase ends. Before a part's first phase ends, its card shows only the try. Verify runs no agent, so the card names the state ("checking") in place of a role chip.
- `review_result` (its `fail_count`), the review's `component_usage` and `component_retrying` (its `attempt`): the reason, the tries and the spend are each marked.

**Rules**: the layout never moves; say whether the view is live (events in the last 60 s); a silent agent's card says "no output for 2m", because kstrl does not act on silence; what you are reading does not change under you; spend moves when a phase ends; a new ask arrives once. The no-progress breaker's streak is not emitted, so no screen shows it until it trips.
