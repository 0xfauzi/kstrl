# F4: Ultra-Review Commands for the Hardening Cycle

> Rename note (2026-07-20): the project was renamed Ralph -> kstrl (package `kstrl`, CLI `ks`, config `kstrl.toml`, state `.kstrl/`, env `KSTRL_*`). Historical entries below keep the names that were current when they were written.

Per H1 of the hardening roadmap, the assistant does not run `/code-review` on
its own code. This doc lists the cumulative set of PRs from the hardening
cycle along with the exact `/code-review ultra` commands to invoke.

`/code-review ultra` is the user-driven, multi-agent cloud-review path. It is
billed and cannot be launched from inside the assistant turn — you (the user)
must type the slash command yourself.

## Scope

Adversarial-factory cycle PRs:

| PR | Title | Merge commit | Phase |
|---|---|---|---|
| [#35](https://github.com/0xfauzi/ralph-loop/pull/35) | feat: per-component semantic knowledge layer | (merged pre-cycle) | Pre-cycle |
| [#36](https://github.com/0xfauzi/ralph-loop/pull/36) | feat: adversarial factory roles | `11da226` | Pre-cycle |
| [#37](https://github.com/0xfauzi/ralph-loop/pull/37) | feat: Phase A - critical correctness fixes | `8cda264` | A |
| [#38](https://github.com/0xfauzi/ralph-loop/pull/38) | feat: Phase D - planted-bug fixtures + calibration | `a3ea255` | D |
| [#39](https://github.com/0xfauzi/ralph-loop/pull/39) | docs: Phase F - real-world validation | `ba01bb0` | F |
| [#40](https://github.com/0xfauzi/ralph-loop/pull/40) | feat: Phase B - complete ralph.toml loader | `25c99b7` | B |
| [#41](https://github.com/0xfauzi/ralph-loop/pull/41) | test: Phase C - integration coverage | `bd0b077` | C |
| [#42](https://github.com/0xfauzi/ralph-loop/pull/42) | feat: Phase E (partial) - architectural refinements | `2e551bf` | E partial |
| [#43](https://github.com/0xfauzi/ralph-loop/pull/43) | docs: Phase G - close out the roadmap | `8b423c0` | G |

Plus this PR (deferred follow-ups F5+F4+H3+E8+E3) and any future hardening PRs.

## Commands

Run each of these in this Claude Code session (or any session with the project
checked out):

```
/code-review ultra 35
/code-review ultra 36
/code-review ultra 37
/code-review ultra 38
/code-review ultra 39
/code-review ultra 40
/code-review ultra 41
/code-review ultra 42
/code-review ultra 43
```

For the cumulative diff of the hardening cycle (everything since `#35`):

```
/code-review ultra
```

run from a feature branch whose `git merge-base origin/main` predates `#35`.
Otherwise the no-arg form reviews only the current branch, which is fine when
you want to spot-check a single PR locally before opening it.

## What each phase's review should look for

| Phase | Focus |
|---|---|
| A | Prompt-injection sanitizer in `knowledge.py` (Phase A1 patterns + length cap); `fcntl.flock` correctness around worktree setup; 5MB stream cap not blocking legitimate large outputs; Self-Critique regex correctness against the fuzz corpus |
| D | Fixture realism (planted bugs should be genuine, not contrived); calibration runner correctness — especially `must_detect` matching logic (the F5 baseline run found a `finding.evidence` bug in this) |
| F | The `examples/file-upload-spec.md` spec is what the factory is graded against — its planted concerns should be defensible as real security/quality issues |
| B | TOML loader precedence (env > toml > defaults); enum validation in `__post_init__` happens for every config with enum-typed fields |
| C | The pickling round-trip test — every config in `ralph_py/*.py` should be picklable for ProcessPoolExecutor; the Windows skip markers should not silently hide bugs |
| E | Confidence-tier rename (legacy alias should be removed eventually); HITL checkpoint's non-interactive fallback; security `infrastructure_error` flag downstream consumers |
| G | Docs are accurate against code (especially `docs/env-vars.md`); no doc references undefined env vars |

## Process going forward

H5 of the hardening roadmap: the user runs ultra-review retroactively on this
cycle's PRs. Once a PR has been ultra-reviewed, mark it `[x]` in
`docs/adversarial-roadmap.md`'s Phase H section.
