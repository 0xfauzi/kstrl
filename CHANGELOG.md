# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Work in progress toward the Dark Factory cycle (continuous intake, a release
stage, runtime feedback, and an earned-autonomy ladder). See
[`docs/dark-factory-roadmap.md`](docs/dark-factory-roadmap.md) and the
[R8 milestone](https://github.com/0xfauzi/kstrl/milestone/1).

## [0.2.0] - 2026-07-21

The first release under the **kstrl** name (the project was formerly "Ralph").

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

- Renamed the project from Ralph to **kstrl** (CLI `ks`/`kstrl`, config
  `kstrl.toml`, state `.kstrl/`, env prefix `KSTRL_*`). Legacy `RALPH_*` and
  `.ralph/` are honored for one release with a deprecation warning.

[Unreleased]: https://github.com/0xfauzi/kstrl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/0xfauzi/kstrl/releases/tag/v0.2.0
