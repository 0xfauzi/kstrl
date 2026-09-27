Proposal: trust. The autonomy ladder, where the project stands on it, what the next level needs, and what lowers it, stated as the code does it.

**Why this shape.** The ladder is a staircase because the levels only go one step at a time in both directions (no skipping on the way up; a demotion drops exactly one). The level in force is the ink tile. The next level is outlined, with the one count that moves toward it (clean merges in a row) drawn as ticks.

**What the code says, and the screen repeats:**
- L1 and L2 differ in `auto_accept_plan`, which nothing outside `autonomy.py` reads. Today they behave the same; L1's card says so.
- The level in force is the lowest of the level, `max_level`, L2 when `[policy]` is off (the default), and L2 when the control state is in the repository. So with policy off, L3 runs as L2 whatever you promote to.
- L3 needs 15 clean merges in a row, at least 8 decisive runs at L2, no policy violations and no cool-down. All four numbers are declared unmeasured placeholders in kstrl.
- "Clean" means the part completed. kstrl never records whether a person edited the code (`human_edited` is never passed), so the screen says what clean means.
- Promotion needs a terminal (`--actor`, `--ack`, and a TTY on stdin and stdout), because an agent can pass the strings but an unattended process has no controlling terminal. `--force` is recorded as `forced_over_blockers`.
- Demotion needs no terminal, drops one level and starts a 10-run cool-down. Of its automatic triggers, policy violations need policy on, and health breaches and calibration slips are off by default. So in a default project nothing lowers trust on its own, and the screen says so.
- Evidence is recorded only while `[autonomy] enabled` (off by default). A damaged `autonomy.json` falls back to L1 and says why.

**History** lists each transition (time, from, to, trigger, actor, reason). **Replay** runs the ladder over past runs (`experiments.tsv`) and shows where it would stand; it needs at least 8 decisive runs.

**Built on**: `${XDG_STATE_HOME}/kstrl/<repo-id>/autonomy.json`, `ks autonomy status` / `history` / `replay`, `[policy]` and `[autonomy]` in `kstrl.toml`.
