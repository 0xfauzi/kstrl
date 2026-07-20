# Evolution metrics and journal format

This document defines every metric the evolution layer records: what is
measured, where it comes from, and what it does NOT mean. Definitions
describe the code as implemented (R6.4); nothing here is aspirational.

## Journal: `.kstrl/evolution.jsonl`

Append-only JSONL. Every entry carries `schema_version` so future format
migrations are detectable.

- **Version 2** (current): structured failure signatures (R6.1).
- **Version 1**: entries without a `schema_version` field (pre-R6
  shape). Wave 1 (R4.1) archived the polluted v1 journals to
  `.kstrl/archive/`; they are kept for forensic reference only and are
  never read by current metrics. Fresh journals contain v2 entries only.

### Entry types

| `event_type` | Written by | When |
|---|---|---|
| `component_result` | `EvolutionJournal.record_run` | Once per component at the end of every factory run |
| `findings_superseded` | factory `_journal_superseded_findings` | When a retry supersedes an attempt's findings (R3.3) |
| `contract_result` | factory `_record_contract_event` | After every contract-test tier, pass or fail (R0.3) |

### `component_result` fields

| Field | Definition |
|---|---|
| `schema_version` | Journal format version (see above). |
| `timestamp` | UTC ISO-8601, one per run (shared by all components of the run). |
| `run_id` | Factory run id (microsecond precision plus nonce, R1.6). |
| `project` | `manifest.project_name`. |
| `component_id` | Manifest component id. |
| `status` | Terminal manifest status (`completed`, `failed`, `pending`, ...). `pending` with a non-empty `error` means the component was retried and the run ended before another attempt. |
| `retries` | Retry counter at end of run. |
| `error` | Flattened human-readable error of the last failure, `""` on success. Display only: metrics must use `failure_signatures`. |
| `failure_signatures` | R6.1: list of structured `"<check>:<code>"` signatures for the last failed attempt, e.g. `linter:E501`, `typecheck:arg-type`, `test_suite:assertion-error`, `review:scope_creep`, `security:injection`, `diff_scope:files-outside-allowed-scope`, `contract:tier_1`, `engineer:component-timeout`, `token_budget:exceeded`, `pr:closed-without-merge`. Codes come from the tool parser (ruff rule, mypy error code, pytest exception type) or the finding taxonomy; sites without parser codes record a stable slug of the error text (paths, line numbers, and counts stripped). Empty on success. |
| `check_name` / `error_signature` | Convenience split of the FIRST signature (`check_name:error_signature`). Kept for v1-shaped readers; new consumers should read `failure_signatures`. |
| `failed_phase` / `failed_check` | R3.3 post-mortem pointers: which phase and gate fired last. |
| `duration_seconds` | Wall-clock of the component's LAST attempt, measured from the PENDING->RUNNING transition to the terminal transition (completed / failed / merge-pending / retry scheduled / scheduler backstop). Includes the engineer loop, mechanical verification, review, security review, and PR flow. It is NOT the sum across retries, and 0.0 appears only for components that never started an attempt in this process (e.g. skipped, or state inherited from a crashed run). |
| `iteration_count` | Engineer-loop iterations of the last attempt. |
| `findings` | Full typed Finding stream of the last attempt (E3), attempt-tagged. |
| `findings_summary` | Aggregates of `findings`: `total`, `by_phase`, `by_severity`, `by_category`, `by_owasp`, `infrastructure_errors`. |
| `usage` | R3.1 per-phase token/cost self-reports (lower bounds when `unreported_calls` > 0). |

## Experiments: `.kstrl/experiments.tsv`

One row per factory run, appended by `record_run`. Columns:

| Column | Definition |
|---|---|
| `run_id` | Factory run id. |
| `timestamp` | UTC ISO-8601 at record time. |
| `project` | Manifest project name. |
| `components_total` | Number of components in the manifest. |
| `completed` / `failed` / `skipped` | Counts from the run's FactoryResult (skipped = cascade-skipped dependents of failures). |
| `avg_iterations` | Mean engineer-loop iterations over components with `iteration_count` > 0. 0.00 when no component ran. |
| `avg_duration_s` | Mean `duration_seconds` (last-attempt wall clock, see above) over components with a duration > 0. |
| `retry_rate` | Total retries across ALL components divided by `components_total`: the average number of retries per component, NOT the fraction of components that were retried. A run of 4 components where one burned 3 retries records 0.75. Can exceed 1.0. |
| `common_failure` | The most frequent full `"<check>:<code>"` failure signature among FAILED components this run; `""` when nothing failed. |
| `total_tokens` / `total_cost_usd` / `unreported_calls` | R3.1 run totals. Empty string (not 0) when usage tracking was unavailable: zero would misread as "measured, free". Figures are agent-CLI self-reports and are lower bounds whenever `unreported_calls` > 0. |

Rows written before a column existed keep their shorter header;
`get_experiment_trends` (csv.DictReader) tolerates both shapes.

## Concern hit rate (`ks evolve` internals, D8)

`get_concern_hit_rate` consumes `findings_summary.by_category` on
`component_result` entries (R6.2). A component counts as "with concern"
when it has at least one finding in a real category; the synthetic
`infrastructure_error` and `phase_skipped` categories mark
non-execution, not adversarial signal, and are excluded. `by_category`
in the result sums finding counts (not component counts) per category
across the window.

## Proposals: `.kstrl/proposals/prop-NNN.md`

- IDs are monotonic across `ks evolve` invocations: numbering
  continues after the highest `prop-NNN.md` already on disk (R6.2).
- Existing proposal files are never overwritten; a proposal whose title
  already exists on disk is skipped, not duplicated.
- `ks evolve --apply PROP-NNN` (or `all`) really applies only
  convention-type proposals (computational, target `claude_md`): after
  explicit confirmation it appends the convention to the project
  CLAUDE.md `## Agent Learnings` section and stamps the proposal file
  with `**Applied**: <timestamp>` so a re-apply is a no-op.
  `[evolution] auto_apply_computational = true` skips the confirmation
  prompt. Every other target prints manual instructions (R6.3).
- `[evolution] auto_propose = false` restricts `ks evolve` to
  pattern reporting; no proposal files are generated.
