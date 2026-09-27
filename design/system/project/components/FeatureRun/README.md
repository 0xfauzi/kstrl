Proposal: a one-off feature run (`ks feature`), at its only checkpoint. The engineer has written what it understood about the feature, and implementation waits for you.

**Why this shape.** A feature run looks like a small factory run and is not one, so the page says what it lacks in the ink tile: no reviewer, no security check, no commit, no pull request. You are the reviewer. The rest of the page is the one decision it asks of you, with the evidence for it: the understand file's quick facts, story coverage, the risks and the open questions it recorded.

**The flow, from `feature_cmd.py`:**
1. **Understand**: an engineer loop that may edit only `scripts/kstrl/feature/<name>/understand.md`, and stops when every story in the coverage checklist is ticked.
2. **The gate** (`checkpoint_requested`, kind `feature_gate`): Start implementation, or Quit to amend. With no terminal and no app to answer, kstrl refuses unless `--implementation-auto-run` is given.
3. **Implement**: one engineer loop in your checkout (no worktree), with at most one iteration per story.
4. **Repairs**: if implementing did not finish, up to `--repair-max-runs` (5) runs of `--repair-iterations` (5), each from a repair PRD written to `repairs/`.

Verify (tests, typecheck, lint) runs at the baseline, after implementing and after each repair, and is a report, never a gate. Nothing in the flow commits.

**What its engineer reads.** Unlike a factory part, a feature run passes no context prefix to its loop. So it reads CLAUDE.md and the engineer prompt, and not kstrl's facts, your golden patterns or your guidance. The Learning page's stack applies to factory parts only.

**Tabs.** Understand is this view. Implement follows the loop like the one-agent view. Repairs lists each repair run with its verify report.

**Built on**: `.kstrl/runs/<run>/events.jsonl` (phases understand, implement and repair-N; `checkpoint_*` with kind `feature_gate`; `artifact_written` for understand_file and repair_prd; advisory verification results), `understand.md`, `repairs/*.json`, and the run's `engineer.log`.
