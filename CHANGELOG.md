# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Work in progress toward the Dark Factory cycle (continuous intake, a release
stage, runtime feedback, and an earned-autonomy ladder). See
[`docs/dark-factory-roadmap.md`](docs/dark-factory-roadmap.md) and the
[R8 milestone](https://github.com/0xfauzi/kstrl/milestone/1).

### Added

- `ks sense`: run the mechanical sensors (test suite, typecheck, linter,
  diff scope, bad patterns, plus any opt-in policy / adequacy / dead-code /
  mutation checks) against any tree by hand, with no PRD, branch, worktree
  or agent spend. `--json` emits one machine-readable document; exit 0 on
  pass, 1 on any failed check, 2 when the measurement could not run.
  `run_mechanical_verification` now accepts `prd_path=None` and skips only
  the PRD-dependent checks, and `read_only=True` to measure a tree without
  changing it. Because `ks sense` runs against your live checkout rather
  than a worktree kstrl owns, the measurement never edits, stages, commits
  or leaves bytecode: the dead-code check reports what it would remove
  instead of removing it, and mutation testing is skipped because mutmut
  works by rewriting source. A base branch git cannot resolve is exit 2,
  never a pass on an empty diff (R10.1, #222).

### Changed

- The retry context handed to the engineer is now level-triggered: it renders
  the failures measured in the latest attempt, lists earlier findings whose
  sensor did not run again under "Not re-measured", and replaces the rest with
  a count. Before this it was an integrator with no discharge - every failure
  ever accumulated was re-rendered on every retry under "Fix ALL issues listed
  above before completing", so an agent on attempt 3 was told to fix attempt
  1's failures whether or not attempt 2 had already fixed them. Each failure
  now records the attempt it was measured in and the phase that measured it.
  A finding is only retired when that is observed (the same phase produced a
  fresh reading) or safely inferred (a phase that always runs once its
  predecessor passes). Review and security are excluded from the inference
  because an exhausted `max_adversarial_calls` budget downgrades them to skip
  mid-run, so a later failure does not prove the reviewer ran.
  `IterationContext.from_json` still reads contexts serialised in the old
  shape, and those undated findings always render as un-re-measured
  (R10.2, #223).

### Removed

- **Breaking:** the one-release compatibility layer for the pre-rename
  names. The legacy environment-variable prefix, config filename, state
  directory, and console script are no longer read or installed. Move to
  `KSTRL_*`, `kstrl.toml`, `.kstrl/`, and the `ks` (or `kstrl`) command.

## [0.2.0] - 2026-07-21

The first release under the **kstrl** name.

### Added

- **Adversarial factory pipeline**: an architect red-teams the spec and
  decomposes it into a component DAG; each component is built by a coding agent
  in an isolated git worktree and gated through mechanical verification, code
  review, security review, and cross-component contract testing before its PR
  merges. An optional human checkpoint can pause before merge.
- **Textual TUI and events substrate**: every run writes a typed
  `events.jsonl` that every surface projects - a live dashboard, the bare-`ks`
  home shell with a run browser, `ks dash` (attach read-only to any run), and
  `ks status` for scripts and CI.
- **Agent adapters**: `claude-code`, `codex`, `custom`, and an opt-in
  `claude-sdk` adapter (installed via the `kstrl[sdk]` extra) with in-loop
  budget enforcement.
- **Safety systems**: per-phase and per-component timeouts, a no-progress
  circuit breaker, adversarial-call and token budgets, an OS-level agent
  sandbox, and a sandboxed approved-fixtures oracle.
- **Learning loop**: a calibration suite with planted-bug fixtures, an
  evolution journal, knowledge distillation across runs, and `ks evolve`
  harness-improvement proposals.
- **Linear mirror**: an optional one-way outbound sink that reflects factory
  progress into a Linear tracker.
- **Dark Factory roadmap**: `docs/dark-factory-roadmap.md` plus the R8 issue
  set defining the path to a governed autonomous factory.

### Changed

- Renamed the project to **kstrl** (CLI `ks`/`kstrl`, config `kstrl.toml`,
  state `.kstrl/`, env prefix `KSTRL_*`). The previous names were honored
  for one release with a deprecation warning.

[Unreleased]: https://github.com/0xfauzi/kstrl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/0xfauzi/kstrl/releases/tag/v0.2.0
