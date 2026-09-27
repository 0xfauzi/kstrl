Proposal, step level, Grid at eight agents: the `import` spec with `[factory] max_parallel = 8`. It shows how the Grid gives up space as the number of agents grows, with nobody dropping off the screen.

**How it scales.** At four, each agent has a large tile and the run takes a 2×2 hero. At eight, the run takes one column, with the plan drawn top to bottom (what merged, the eight working as pills, what waits). Agent tiles drop one size: a 44px ring, the number, and at most two lines of what the agent last wrote, clamped with an ellipsis because the text is supplied at run time. Checkers keep their check sequence instead of a ring. The latest checker statement keeps the ink tile.

**Roles.** Three role chips now appear: `engineer`, `reviewer`, and `security` for the security agent. Security is off by default (`[security] mode = "skip"`); this project runs it, and a finding at or above `fail_threshold` (default `high`) blocks, so the part goes back to its engineer with the finding.

**From the code.** `max_parallel` defaults to 4 and has no upper bound, only a check that it is finite and not negative. At 2 or more, kstrl runs a process pool of that size. `ks serve` does not pass it on: the `ks factory` it starts reads `[factory]` from kstrl.toml. It is forced to 1 when worktrees are off or a single PR is requested. The pill names leave off the shared `import-` prefix; the tiles give the full ids.

**Built on**: the same records as the Grid at four agents (`iteration_*`, `phase_*` and `component_retrying` events, `component_usage`, log tails with their ages, the plan's dependency graph).
