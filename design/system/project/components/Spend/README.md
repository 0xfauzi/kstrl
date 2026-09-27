Proposal: spend. What today cost against the daily budget, what each spec cost this week, every limit and what reaching it does, and how sure the numbers are.

**Why this shape.** Today's spend is one number against one cap, so it is a hero number with a single bar, not a chart. The bar is split by run (a 2px gap between segments), and the cap is a `fail` rule the bar runs past. It runs past because the daily budget is checked before a spec starts, not while it runs: search started at $9.26 and finished at $42.46. What each spec cost is one series, so it is one colour of thin bars, sorted, with the values in text. The limits are a list, because each one's consequence is the point.

**Limits, from the code** (no budget is on by default):
- `[serve] daily_budget_usd`: checked before each spec is admitted. At the cap, the queue pauses with `resume_after` at the next local midnight and files a `budget_overrun` item. Resuming by hand is re-paused within one cycle, because the budget is checked every cycle.
- `[factory] max_cost_usd` and `max_total_tokens`: checked between phases and iterations, so not a hard cap. At the cap the current part fails and the parts not yet started fail at scheduling.
- `max_adversarial_calls`: counts review, security and distill calls. When it runs out, a hard-mode review or security check refuses and the part fails (advisory mode skips; distill is skipped).
- No per-part limit exists. `[agent] budget_usd` applies per turn to the claude-sdk agent only.

**How sure the numbers are.** Totals come from agent-reported usage, and a call that reports nothing makes every total a lower bound, hence ≥. The ledger records `covered_calls` against `total_calls` per day, which this tile shows. `ks serve` refuses to spend at all when no call has reported a cost, unless `allow_uncovered_cost` is set.

**Built on**: `spend.json` in the kstrl state directory (`spend{date, spent_usd, runs, covered_calls, total_calls, unmetered_phases}`), `component_usage` per part and phase, the run directories, and the queue's pause state.
