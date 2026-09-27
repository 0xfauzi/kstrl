Proposal: health. Is the factory working as it usually does? `ks health`'s three control charts, what a breach does, safe mode, and `ks doctor`.

**Why this shape.** This is the one screen where a chart earns its place: each metric is a series over runs, judged against limits taken from the project's own history. They are small multiples, one per metric, never on a shared axis. Each chart shows the latest value as a hero number, the mean and the 2σ and 3σ limits as hairlines, and the three monitored runs in a shaded window; a point beyond a limit is `fail`. The breaching chart gets a `fail` outline and a sentence naming the rule.

**The arithmetic is kstrl's** (`health.py`), computed in the generator rather than drawn by hand: the baseline is every decisive run but the last 3 (at least 8 runs); mean is `fmean`, sigma is `pstdev`; only upward breaches count. The rules are checked in order (one point beyond 3σ, then 2 of 3 beyond 2σ, then EWMA(0.2) beyond 3σ), so the chart names the first one that fires. Here retry_rate's latest 0.43 is beyond its 3σ limit of 0.31.

**What a breach does** gets the ink tile, because the answer surprises: it files a `health_breach` notice and nothing else. It lowers trust only if `demote_on_health_breach` is on, and that is off by default.

**Safe mode** is a report, not a switch: it turns nothing off, and each reason clears when its cause does. Its four sources are an untrusted control directory, a degraded or clamped ladder, an active queue pause, and review or security skipped in the newest finished run.

**Readiness** (`ks doctor`) is ten static checks, each naming what in kstrl consumes it: git_repo, git_clean, github_cli, kstrl_config, build_manifest, verify_commands, source_root, test_root, gitignore, protected_paths.

**Built on**: `ks health` (metrics retry_rate, cost_per_merged_component, infrastructure_error_rate), `safemode.py`, `ks doctor`'s report.
