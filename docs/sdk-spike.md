# Agent SDK spike: SDK-driven engineer vs CLI subprocess (R7.5)

**DECIDED 2026-07-20: GO**, as scoped by the recommendation below - an
ADDITIONAL `claude-sdk` adapter behind the existing `Agent` protocol for
the claude-code engineer path, not a replacement for the subprocess
adapters. The production-wiring gate stands unchanged: the R0.1 timeout
battery must pass against the SDK transport, and the sandbox settings
pass-through must be re-measured through `ClaudeAgentOptions`. Tracked
as R7.6 in `docs/remediation-roadmap.md`.

**IMPLEMENTED + GATE PASSED 2026-07-20** (same day): see R7.6 in the
roadmap for the evidence. Note the timeout-semantics concern below was
CONFIRMED by measurement - the SDK transport spawns the CLI without
`start_new_session` and only signals the direct child on close - which
is why the adapter runs the SDK inside a runner subprocess owned by the
R0.1 DeadlineStreamer rather than in-process.

Written comparison for **user decision 5** (adopt the Claude Agent SDK
for agent invocation, or stay on CLI subprocesses). Per the
measure-don't-assume rule, every claim below was measured on
2026-07-19 by driving ONE component implementation through the SDK in
a scratch script (not committed; reproduction recipe at the bottom).
No production code changed as part of this spike.

Setup: `claude-agent-sdk` 0.2.123 (Python), claude CLI 2.1.215,
model `haiku`, macOS. The "component" was a kstrl-shaped PRD prompt
(greeter with an EARS-style positive and negative criterion, pytest
run, DONE marker) executed in a scratch workspace via
`ClaudeSDKClient` with `permission_mode="bypassPermissions"`,
`max_turns=30`, a `PreToolUse` hook, and `max_budget_usd` caps.

## Measured comparison

### 1. Structured usage data

| | CLI subprocess (today: R3.1 meter) | Agent SDK (measured) |
|---|---|---|
| claude | Self-reported `usage` dict parsed from the stream-json `result` event; parse failure degrades to an all-None record (lower-bound semantics) | Typed `ResultMessage`: `total_cost_usd=0.0262722`, full `usage` (in/out/cache-read/cache-creation, per-iteration breakdown), `model_usage` per model id including `costUSD` and `contextWindow`. No parsing, no heuristics |
| codex | Text trailer "tokens used" - TOTAL only, no split, no cost | Not covered - the SDK drives Claude only |

The SDK's `usage` arrived complete on both runs; the CLI path's meter
explicitly documents parse-drift risk across CLI versions.

### 2. Hook-based guardrails

CLI subprocess today: no pre-execution hook surface. kstrl's
allowed-paths guard and mechanical verification run AFTER an
iteration; an out-of-scope write happens first and is caught later.

SDK, measured: a `PreToolUse` hook that denies writes outside the
workspace **actually fired during the spike** - the model's first
`Write` targeted `/Users/<user>/greeter.py` (its cwd assumption was
wrong), the hook denied it BEFORE the file existed, the agent ran
`pwd`, corrected itself, and completed inside the workspace. The
denial also appeared in `ResultMessage.permission_denials` with the
full attempted `tool_input` - an audit record the CLI path cannot
produce. This is prevention where the current design has detection.

### 3. Budget enforcement

CLI subprocess today: `max_total_tokens` (R3.1) is enforced at PHASE
boundaries from CLI self-reports; an in-flight engineer loop can
overshoot by a whole phase, and unreported calls make totals lower
bounds.

SDK, measured: `max_budget_usd=0.00001` halted the run in-loop with a
typed result - `subtype="error_max_budget_usd"`, `is_error=true`,
`errors=["Reached maximum budget ($0.00001)"]`, exact
`total_cost_usd=0.0125284` still reported. Granularity note
(measured): enforcement is per-turn - the run still spent $0.0125
against a $0.00001 cap before halting, i.e. one turn can overshoot,
but the halt is inside the agent loop rather than at kstrl's phase
boundary, and the overshoot is bounded by one turn instead of one
phase.

### 4. Failure observability

CLI subprocess today: exit codes plus stdout heuristics - the
`TIMEOUT_MESSAGE_PREFIX` line, "ERROR: claude CLI not found", the
DeadlineStreamer's silent-hang detection; usage parse failures are
logged and degraded.

SDK, measured/typed: exceptions are typed (`CLINotFoundError`,
`ProcessError`, `CLIJSONDecodeError` - all subclasses of
`ClaudeSDKError`), and `ResultMessage` carries `is_error`, `subtype`,
`errors`, `api_error_status`, `stop_reason`, `num_turns`,
`permission_denials`. A `RateLimitEvent` also surfaced in the message
stream mid-run - the CLI path never sees rate-limit state at all.

## What the SDK does NOT solve

- **codex stays subprocess.** The SDK drives Claude only. R7.1's
  cross-family review rotation (codex reviews Claude code) depends on
  the codex CLI, so the subprocess machinery cannot be deleted either
  way.
- **Timeout/kill semantics are unproven.** kstrl's R0.1 battery
  (silent hang detection, `killpg` of grandchildren, bounded waits)
  is measured against `DeadlineStreamer`. The SDK manages its own CLI
  subprocess; whether a hung tool call is killable within kstrl's
  deadlines was NOT measured in this spike and must pass the same
  battery before any production wiring.
- **A new dependency** (`claude-agent-sdk` + its transitive set) in a
  harness that today needs only stdlib + click at runtime.
- **Async surface.** The SDK is asyncio-first; kstrl's `Agent`
  protocol is a synchronous line iterator, so an adapter needs an
  asyncio bridge inside `run()`.

## Recommendation: GO, scoped to a fourth adapter

Adopt the SDK as an ADDITIONAL adapter (`claude-sdk`) for the
claude-code engineer path, behind the existing `Agent` protocol -
not a replacement for the subprocess adapters:

1. The two measured wins are structural, not incremental: pre-hoc
   guardrails (a hook denied a real out-of-scope write during this
   very spike) and typed in-loop budget enforcement directly harden
   walk-away runs - the R7.5 theme.
2. Structured usage removes the R3.1 meter's parse-drift risk for the
   dominant spend (the engineer loop) while codex/custom keep the
   existing lower-bound semantics.
3. Keeping it an adapter preserves the codex rotation path, the
   custom-agent escape hatch, and a fallback if the SDK regresses.

Gate before production wiring: the R0.1 timeout battery (sleep-forever
tool, grandchild kill, silent hang) must pass against the SDK
transport, and the sandbox settings pass-through (R7.5) must be
re-measured through `ClaudeAgentOptions.sandbox` / `settings`.

## Reproduction

Scratch script (workspace-scoped `PreToolUse` deny hook, two runs:
real budget then `max_budget_usd=0.00001`), run as:

```bash
uv run --with claude-agent-sdk python sdk_spike.py
```

Spike spend, measured from the results: $0.026 (implementation run) +
$0.013 (budget-breach run) per execution, model haiku.
