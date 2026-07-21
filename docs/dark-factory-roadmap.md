# Dark Factory Roadmap (R8): from merged-PR factory to governed autonomous factory

Durable tracker for the R8 cycle. Goal: close the four structural gaps between
Kestrel and a full software factory - continuous intake, a release stage,
runtime feedback, and an explicit earned-autonomy model - plus the hardening
that makes reduced-human-gating defensible.

Tracking issue: [#156](https://github.com/0xfauzi/kstrl/issues/156).
Milestone: `R8: Dark Factory`.

Provenance: a 2026-07-21 gap analysis diffed Kestrel against a 15-pillar
reference model synthesized from the software-factory lineage (Cusumano's
Japanese factories, Greenfield/Short's Microsoft software factories, SEI
software product lines, DoD DevSecOps reference designs and cATO, lights-out
manufacturing) and 2024-2026 agentic-SWE literature. Each item below was then
researched individually with a build-vs-integrate lens; key sources are cited
inline. Caveat recorded per H4: the definitional/historical claims survived
adversarial verification; the manufacturing and agentic-SWE claims were
extracted from sources but not adversarially verified.

Status legend: `[ ]` pending - `[~]` in progress - `[x]` done - `[-]` skipped.
Sizing: S (small diff, <~100 lines), M (one PR), L (multi-PR workstream).
Sizing is diff scope, not duration.

Process rules that bind this plan (inherited unchanged):

- H1: no self-review. Every item lands as one or more PRs gated by the user.
- H2: any prompt-body change re-runs calibration and records the delta.
- H3: every prompt edit bumps `*_PROMPT_VERSION` and the snapshot tuple together.
- H4: every "done" claim states what was tested vs assumed. Each item has a
  measurable "Done when" gate.
- R8 addition - no assumed thresholds: every numeric threshold in this plan
  (ladder entry counts, EWMA lambda, sigma multipliers, mutation floors,
  signal thresholds) is a placeholder until measured. Before any threshold
  gates or demotes, replay it against historical data (`experiments.tsv`,
  evolution journal) and record the would-have-fired count in this doc.

---

## The frame: what "dark factory" means here

The research is unambiguous that 100% dark is not a defensible goal. The
coiners of the modern "software factory" term explicitly denied full
mechanization; the DoD's most automated reference design retains two human
touchpoints (merge review, production deploy decision); near-dark physical
plants keep humans precisely for QA. The defensible end state is the cATO
shape: **autonomy that is earned after demonstrated baseline compliance,
bounded by an explicit written envelope, continuously monitored, and revocable
with automatic reversion to human-gated mode**. Humans move from in-the-loop
approval to over-the-loop exception handling.

Kestrel's scorecard against the reference model: the verification core
(adversarial phases, breakers, budgets, audit trail, calibration) is already
at or above the bar. Absent entirely: continuous intake, release/deploy,
runtime telemetry of built products, and an autonomy maturity ladder. Partial:
policy gating (implicit, scattered), the over-the-loop human surface, and the
learning loop (build-time signals only).

Doctrine for this cycle:

1. **Integrate at the edges, build only thin middles.** Queue front-ends,
   deploy engines, error tracking, license metadata, notifications: integrate.
   The state machines, policy checks, and gates that carry trust: build,
   small, harness-side, mechanically verifiable.
2. **Autonomy is earned, bounded, revocable.** Promotion requires evidence
   plus a recorded human ack; demotion is automatic; fast down, slow up.
3. **Every new surface is a projection of `events.jsonl`.** Queue, inbox,
   release states, runtime signals all emit typed events; the files remain
   the record.
4. **Enforcement reads artifacts, never agent self-report.** Policy and
   adequacy checks run on the git diff, lockfiles, and coverage/mutation
   output, in the mechanical verifier.

---

## User decisions required

Blocking decisions are marked on the items that need them.

1. **Remote-item merge policy** (R8.6): is `stop_at_pr` the permanent default
   for queue items sourced from GitHub/Linear, with `auto-merge` per-item and
   ladder-gated? (Recommended: yes.)
2. **Unattended spend** (R8.6): acceptable `daily_budget_usd` for `ks serve`,
   and on exhaustion: pause queue until next day, or notify-only?
3. **Queue scope** (R8.6): per-target-repo `.kstrl/queue/` or one global
   `~/.kstrl/queue/` with items carrying a target-repo field? Multi-project
   intake forces global.
4. **Deploy reality** (R8.7): what do built products actually deploy to today
   (fly.io, VPS compose, nothing yet)? Decides whether the `gha` driver ships
   in v1 or the command driver alone suffices, and whether L4 is real yet.
5. **Revert doctrine default** (R8.7): `revert-and-requeue` or `fix-forward`
   after a bad release. Philosophy, not engineering.
6. **Migrations** (R8.7): will built components own databases? If yes,
   expand-contract migration discipline belongs in `DECOMPOSE_PROMPT` (a
   prompt change: H2/H3 apply).
7. **Error-tracker license** (R8.8): Bugsink is Polyform Shield (free
   self-host, not MIT). Acceptable, or require GlitchTip (MIT, heavier)?
8. **Runtime signal routing** (R8.8): do signals enter the queue directly, or
   via human triage first, and what severity draws the line?
9. **Second model family** (R8.5): which family reviews Claude-engineered
   code at L3+ (codex adapter exists), and does the calibration family-delta
   justify it? Overlaps remediation-roadmap R7.1.
10. **Test immutability** (R8.5): should existing tests be read-only to the
    engineer role, with test modifications routed through a separate approval
    path? Strongest anti-gaming lever, but constrains legitimate refactors.
11. **Always-on machine** (R8.6): is a Mac mini / small server plausible in
    the next ~6 months? If no, sleep-resilience (lease reaping, post-wake
    retry classification) deserves its own tests.

## User-run measurements required

The ladder's entry criteria are the factory's cATO evidence. These are
already tracked in `docs/remediation-roadmap.md` and remain blocking there;
R8.2 consumes them:

- Calibration baselines green at threshold over 3 runs (R5.1-R5.3).
- Same-family vs cross-family reviewer detection delta (R7.1).
- Two real factory runs with nonzero fact-utilization and one traceable
  evolve proposal.

L2+ entry is blocked until these exist. R8 adds no new user-run gates beyond
threshold-replay captures noted per item.

---

## Sequencing

Waves order the work by dependency, not importance. Within a wave, items can
proceed in parallel.

| Wave | Items | Rationale |
|---|---|---|
| 1 - governance core | R8.1 policy envelope, R8.4 health trending, R8.2 autonomy ladder, R8.3 inbox | Small code, no new risk surface, everything else gates on the ladder |
| 2 - adequacy | R8.5 test adequacy gate | The lights-out precondition; advisory-first so it can land early |
| 3 - operation | R8.6 continuous intake | Needs merge dispositions (R8.2) and inbox routing (R8.3) |
| 4 - release + loop | R8.7 release stage, then R8.8 runtime feedback | Highest blast radius, lands on top of the ladder and envelope |

Dependency edges: R8.2 needs R8.1 (envelope defines L3) and consumes R8.4
triggers when available. R8.3 needs R8.1/R8.2 item types (can land with
today's subset). R8.6 needs R8.2 + R8.3. R8.7 needs R8.2 (L4) + R8.6
(re-queue). R8.8 needs R8.6 + R8.7.

---

## R8.1 Policy envelope (M) - [#148](https://github.com/0xfauzi/kstrl/issues/148)

Status: `[ ]`

**Why.** Machine-made merge decisions are only defensible inside an explicit,
written envelope. Today the rules are implicit and scattered (diff-scope,
allowed paths, bad patterns).

**Verdict: build the checks, integrate license metadata.** OPA/conftest need
a Go sidecar; Cedar's Python bindings are early-stage; both are foreign
runtimes for rules that fit in ten TOML lines. License gating integrates
`pip-licenses` or `licensecheck` (explicit allowlist + partial-match deny for
copyleft; compound SPDX expressions defeat exact matching).

**Design.**

```toml
[policy]
paths_deny = [".github/workflows/**", "kstrl.toml", ".kstrl/**", "**/*.pem", "**/.env*"]
max_files_changed = 40
max_lines_changed = 1500
deps_allow_new = false          # L3+ may set true
license_allow = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0"]
license_deny_partial = ["GPL", "AGPL", "SSPL", "Commons-Clause"]
secret_patterns = ["AKIA[0-9A-Z]{16}", "-----BEGIN .*PRIVATE KEY"]
deploy = false
```

Enforcement runs harness-side in the Phase 1 mechanical verifier on
artifacts (git diff, `uv.lock`, dist metadata), never on agent self-report.
Violations emit typed `Finding`s and route to the inbox (R8.3) with
approve-once / approve-and-amend-policy / reject actions. The policy file
hash is recorded in the run manifest. A diff touching enforcement machinery
(policy file, CI workflows, verifier code) is an instant halt at every
autonomy level.

**Failure modes.** Agents relocating violations into generated scripts the
pipeline later executes (mitigated by sandbox network-deny backstop and
deny-listing script dirs that CI executes); license-string mismatch on
compound expressions (partial-match deny); policy drift between repos (each
repo owns its envelope; `ks init` writes the conservative default).

**Done when:** planted violations in every category are caught in tests;
policy hash lands in the manifest; enforcement-machinery halt is tested;
`kstrl.toml.example` and docs updated.

---

## R8.2 Autonomy ladder (M) - [#149](https://github.com/0xfauzi/kstrl/issues/149)

Status: `[ ]` - Depends on: R8.1 (R8.4 enriches triggers later)

**Why.** Autonomy today is a scatter of flags. The cATO shape: earned,
bounded, revocable. Prior art converges (Claude Code permission modes,
OpenHands confirmation policy x risk analyzer, arXiv:2506.12469 levels
defined by the human's remaining role).

**Verdict: build.** ~200 lines of state machine over flags that already
exist. Nothing integrable exists for a local CLI.

**Design.** State in `.kstrl/autonomy.json`; transitions append to
`events.jsonl` and the evolution journal.

| Level | Meaning | Flag bundle | Entry criteria (placeholders until replayed) |
|---|---|---|---|
| L1 Supervised | human approves plan and merge | merge gate on, strictest hard mode, `deps_allow_new=false`, deploy off | default for new repos |
| L2 Gated-merge | human gates merge only | plans auto-accepted | 5 components merged at L1, zero policy violations, calibration compare green |
| L3 Enveloped auto-merge | merge gate off when fully green AND inside envelope | breaches route to inbox instead of merging | 15 consecutive L2 merges approved without edits, health metrics inside limits, recorded `ks autonomy promote` ack |
| L4 Deploy | L3 + release stage enabled | `[release] enabled=true` reachable | L3 held for 30 merged components, explicit promote, deploy target exists |

Promotion needs evidence AND a recorded human ack - agents cannot promote
themselves. Demotion is automatic, one level per trigger: policy violation,
calibration regression beyond baseline tolerance, health-metric breach
(R8.4), human rejects an L3 auto-merge candidate. Fast down, slow up:
re-promotion locked for 10 decisive runs after a demotion. Every demotion
emits an inbox item carrying the triggering evidence.

**Failure modes.** Demotion flapping on 3-run noise (minimum n >= 8 decisive
runs, EWMA not raw points, cool-down); Goodhart pressure lowering retry rate
by weakening verification (calibration detection rate stays in the demotion
basis - adversarial ground truth that laxer verification cannot improve);
stale ladder state after manual config edits (flag bundle derived from level
at run start, manual overrides logged as such).

**Done when:** levels drive the flag bundles; promotion requires a recorded
ack; demotion fires on planted trigger fixtures; the threshold-replay tool
exists and its output over historical `experiments.tsv` is captured here.

---

## R8.3 Exception inbox (L) - [#150](https://github.com/0xfauzi/kstrl/issues/150)

Status: `[ ]` - Depends on: R8.1/R8.2 item types (can land with today's subset)

**Why.** Over-the-loop operation needs one surface for everything awaiting a
human decision. Today: SpecBlocker exit codes, FAILED components,
MERGE_PENDING, evolve proposals, calibration captures - all scattered.

**Verdict: build the inbox (thin), integrate ntfy.sh for push.** The inbox is
`.kstrl/inbox.jsonl` + CLI verbs + a Textual screen in the home shell.
HumanLayer-style approval SaaS is the wrong fit for local-first solo. ntfy.sh
is one HTTP POST through the existing notify hook, self-hostable, priority
tiers; notification stays one-way (actions happen in `ks inbox`, which avoids
running an inbound HTTP endpoint).

**Design.** Item types: policy exception, merge gate (L1-L2), halted run,
budget overrun, demotion notice, calibration drift. Linear-Triage-style
one-key actions per type: approve / reject-with-comment / retry / edit-spec /
snooze-with-TTL. `approve-and-amend-policy` converts repeated approvals into
envelope widening - the learning loop that shrinks the inbox. Open-item cap
pauses queue intake beyond N items. Notify only action-required items and
demotions; successes silent; daily digest for informational items. Every
decision journaled.

**Failure modes.** Inbox becomes a second job (open-item cap, policy
amendment loop, snooze TTLs, digest batching); alert fatigue (priority
tiers, silence on success); decisions lost to crashes (append-only JSONL,
same atomic-write pattern as the manifest).

**Done when:** all existing halt paths emit inbox items; actions round-trip
(approving a merge-gate item resumes the component); the Textual screen
renders and acts; an ntfy hook example is documented.

---

## R8.4 Factory health trending (M) - [#151](https://github.com/0xfauzi/kstrl/issues/151)

Status: `[ ]` - Depends on: none (feeds R8.2)

**Why.** Demotion triggers need trend detection over run metrics, and the
operator needs an evidence surface. The journal and `experiments.tsv` record
the data; nothing trends it.

**Verdict: hand-roll detection (stdlib), DuckDB as optional query layer.** No
maintained lightweight SPC library exists (pyspc stale; river heavy for
this). EWMA (lambda ~0.2) plus two Western Electric rules (1 point beyond 3
sigma; 2-of-3 beyond 2 sigma same side) is ~30 lines, designed for small
persistent shifts. DuckDB reads JSONL natively via `read_json_auto`, single
self-contained wheel - right for `ks health query "<sql>"` but stays an
optional extra: autonomy safety logic must not depend on an optional
dependency.

**Design.** Metrics: retry rate, cost per merged component,
`infrastructure_error` rate, calibration detection deltas, human-edit rate.
Control limits computed from the repo's own baseline period, never fixed
constants; minimum n >= 8 decisive runs before any automatic transition.
`ks health` renders per-metric EWMA vs control limits with sparklines;
`ks health why-demoted` replays triggering evidence.

**Failure modes.** False alarms from mixed run kinds (segment by run kind);
baseline contamination by early chaotic runs (explicit baseline window
selection, recorded); metric gaps when runs fail early (decisive-run
definition excludes infra-aborted runs).

**Done when:** `ks health` renders from real journal data; trigger rules are
unit-tested on synthetic drift; the historical replay is documented; the
duckdb extra is wired for the query subcommand only.

---

## R8.5 Test-suite adequacy gate (L) - [#152](https://github.com/0xfauzi/kstrl/issues/152)

Status: `[ ]` - Depends on: R8.2 for level-gated behavior (lands advisory-first)

**Why.** The lights-out precondition in every tradition is an evaluator-grade
test suite, and the evidence says agent-written tests cannot be assumed
adequate: 80.2% of 86k agent-authored test patches carry weak or no oracle
signals (arXiv:2606.18168); LLM assertions encode actual rather than expected
behavior; test-gaming is measured, not hypothetical (ImpossibleBench,
arXiv:2510.20270). Green tests alone cannot be a merge gate at L3+.

**Verdict: build thin gates, integrate the tooling** (diff-cover, mutmut,
StrykerJS, cargo-mutants).

**Design.** Four layers, complementary (mutation does not catch
wrong-expectation tests; only spec-derived oracles do):

- **Layer 0 - test-diff discipline (mechanical, free).** Extend
  diff-scope/bad-patterns: fail diffs that delete tests, add skip/xfail, or
  loosen assertions without spec-linked justification. Oracle-signal linter
  on new/changed test files (W1-W5/S1-S3 taxonomy from arXiv:2606.18168):
  at least one strong-oracle assertion per new test file.
- **Layer 1 - patch coverage floor.** `diff-cover --fail-under` on changed
  lines (~85%), reusing the existing coverage run. Screens untested code;
  says nothing about oracle strength.
- **Layer 2 - diff-scoped mutation score.** Google-style: mutants only on
  changed+covered lines, max 1 per line, hard wall-clock cap (10 min or 3x
  baseline suite) with sampling recorded in the audit trail. Tools: mutmut
  (Python, coverage-limited; components are small so file-scoping
  approximates diff-scoping), StrykerJS `--incremental` (JS/TS),
  cargo-mutants `--in-diff` (Rust); Go targets get fixtures/property-heavy
  treatment with mutation advisory only. Gate >= 70% killed: advisory first,
  thresholds set from the empirical distribution, ratchet up only. Surviving
  mutants feed back to the engineer as concrete test targets (Meta ACH
  pattern) for one remediation iteration before gating.
- **Layer 3 - fixtures oracle.** Promote the approved input/output fixtures
  from opt-in to required at high autonomy - the only layer whose ground
  truth the engineer cannot rewrite.

Verification independence: cross-family review defaults on at L3+
(arXiv:2506.07962: same-family builder/reviewer pairs have measurably
correlated blind spots - when two models are both wrong they agree ~60% of
the time). Calibration gains the same-family vs cross-family delta (overlaps
remediation R7.1).

Behavior by level: L1 - Layer 0 blocking, 1-2 advisory. L2 - 0-1 blocking, 2
blocking after the remediation iteration. L3+ - all blocking, fixtures
mandatory, cross-family reviewer mandatory, and mutation infra failure halts
for a human rather than skipping (`infrastructure_error` convention,
halt-over-heroics).

**Failure modes.** Runtime blowups (scoping, caps, sampling, nightly full
runs to refresh incremental caches); equivalent mutants deflating scores
(threshold well below 100%, capped equivalence claims with recorded
justification); flaky tests poisoning every layer (clean baseline run before
mutants, rerun-on-fail quarantine as its own Finding); agents gaming the gate
(Layer 0 rules, fixtures never shown verbatim, cross-family review of test
diffs, periodic meta-calibration planting bugs against the whole gate).

**Done when:** Layers 0-1 gate with typed findings; Layer 2 runs diff-scoped
on a Python target within budget with sampling in the audit trail;
level-dependent behavior tested; calibration captures the family delta.

---

## R8.6 Continuous intake (L) - [#153](https://github.com/0xfauzi/kstrl/issues/153)

Status: `[ ]` - Depends on: R8.2 (merge dispositions), R8.3 (notifications)

**Why.** Intake is one-shot; the factory has no queue. The first US software
factory (SDC, 1972-78) died because work was not required to flow through
it - intake is a survival capability, not plumbing.

**Verdict: build a thin local substrate, integrate the edges.** Queue
libraries evaluated and rejected (litequeue near-dormant + Python-floor
conflict; persist-queue buys nothing at this concurrency; huey inverts
control). GitHub Issues integrates as the primary remote inbox; launchd
integrates as the scheduler.

**Design.**

- **Substrate:** maildir-style `.kstrl/queue/` (`queued/ leased/ running/
  done/ failed/ poison/`), item = spec file + `meta.json`, transitions via
  `os.replace`, flock singleton, pid/ttl leases with a reaper (the
  sleep/crash recovery path), every transition journaled. ~200-300 lines.
- **Front-ends (pull, no webhooks):** GitHub Issues - poll a `kstrl:queued`
  label via `gh` (ETag-cheap, ~30 req/hr against 5,000/hr), write back state
  labels + result comments, close on merge. Linear - optional second polling
  adapter on the existing client. Linear Agents API deferred until it exits
  preview. Processed-ids ledger makes re-seen issues idempotent; front-end
  outages never block the local queue.
- **Scheduler:** `ks serve` as a launchd KeepAlive LaunchAgent
  (StartInterval misses intervals elapsed during sleep); active runs execute
  under `caffeinate -i`, held only during work so the laptop sleeps between.
- **CLI:** `ks queue add spec.md [--priority N] [--auto-merge|--stop-at-pr]`,
  `ks queue ls|show|retry|rm|pause|resume`, `ks queue sync`,
  `ks serve [--once]` (cron-fallback mode).
- **Safety:** only `infrastructure_error` failures auto-retry with backoff;
  spec failures go straight to `poison/` with a comment back to the source -
  no token-burning crash loops. Remote items default `stop_at_pr`; auto-merge
  is per-item opt-in and ladder-gated. Queue-level `daily_budget_usd` hard
  stop, independent of per-run caps.

**Failure modes.** Queue poisoning (max_attempts + infra/spec distinction);
double-lease (flock singleton + pid-liveness; two-machine operation is an
explicit non-goal for now); laptop sleep mid-run (caffeinate, post-wake
failures classified infra/retryable, lease reaper); unattended spend (the
daily budget is the real cap); governance erosion (continuous intake deletes
the human trigger - `stop_at_pr` default preserves the merge gate).

**Done when:** all queue verbs + `ks serve --once` work; poison path and
budget pause tested; GitHub adapter round-trips labels/comments against a
test repo; launchd plist + caffeinate behavior documented.

---

## R8.7 Release stage, Phase 4 (L) - [#154](https://github.com/0xfauzi/kstrl/issues/154)

Status: `[ ]` - Depends on: R8.2 (L4 gates deploy), R8.6 (re-queue)

**Why.** The factory stops at merged PR + contract tests. Every factory
tradition ends the line at operate, not merge. Largest structural gap.

**Verdict: thin orchestration, two drivers, never per-platform adapters.**
`command` (user shell command + optional `status_command`/`rollback_command`)
and `gha` (`gh workflow run` + `gh run watch --exit-status`; since gh v2.87.0
the run ID returns directly - version-check and keep a poll fallback).
Platform CLIs (fly, Render, Railway, compose-over-SSH) all collapse into the
command driver. Integrate GitHub machinery: Deployments API as the audit
record; environments give approval gates and wait timers for free (plan-gated
on private repos - detect and warn).

**Design.** Phase 4, per-run by default; release ref = merge SHA of the final
tier (always deploy the recorded SHA - main may have moved).

State split: `MERGED` (was `COMPLETED`) -> `RELEASING` -> `RELEASED` |
`RELEASE_FAILED` -> `ROLLING_BACK` -> `ROLLED_BACK` | `ROLLBACK_FAILED`
(halt, human required). Legacy `COMPLETED` aliased to `MERGED` on read,
mirroring the confidence-tier aliasing precedent. Every transition emits
events and a Deployment status. Write-ahead intent record before executing:
deploys are not idempotent; resume asks "did attempt N complete?" via
status_command / `gh run view` instead of re-firing.

Verification ladder (each rung optional, failure budget, Argo-analysis
style): exit code -> health poll with SHA match (endpoint must echo the
deployed git SHA or polling can pass against the previous release - which
means DECOMPOSE_PROMPT must require a version-echoing health endpoint in
built services; that is a prompt change, H2/H3 apply) -> `smoke_command` ->
agent-driven E2E (Playwright against the deployed URL, adversarial framing,
separate `agent_verify_max_calls` budget, findings in the standard `Finding`
stream).

Rollback doctrine: restore service first (`rollback_command`), then repo
truth: `revert-and-requeue` (git revert via PR, re-queue with failure
evidence as feedforward - the revert must be in the re-queued story's
feedforward or the engineer will reintroduce the reverted code) or
`fix-forward` per config. Halt-over-heroics for migrations: a failing release
whose diff touched DB migrations halts for a human; the factory does not
auto-undo migrations.

Containment: `enabled=false` default; environment allowlist checked before
any driver runs; `dry_run` prints the exact command + resolved env; release
flock prevents double-deploy; deploy secrets via env allowlist invisible to
engineer agents; release runs from the main checkout, never inside engineer
worktrees; production-named environments require approval unconditionally.

**Failure modes.** Resume-mid-RELEASING double-deploy (write-ahead intent +
status re-query, tested explicitly); health false positives (SHA-stamped
health); rollback restoring the image but not the database (migration halt
rule); revert-requeue skew (revert in feedforward); consumers of terminal
`COMPLETED` (TUI, Linear sync, evolution, resume) all learn the split -
audit them in one PR.

**Done when:** command driver + verification ladder green against a demo
app; resume-mid-RELEASING test proves no double-deploy; rollback and
revert-and-requeue tested; state split lands with the legacy alias and all
consumers updated.

---

## R8.8 Runtime feedback (L) - [#155](https://github.com/0xfauzi/kstrl/issues/155)

Status: `[ ]` - Depends on: R8.6 (queue), R8.7 (release identity)

**Why.** Nothing observes built products at runtime; the learning loop sees
only build-time signals. A factory closes the loop: production behavior flows
back into the queue and the learning substrate.

**Verdict: integrate observability, build a thin poller.** Errors: Bugsink
(single container, SQLite-capable, Sentry-SDK-compatible ingest, versioned
REST API; license is Polyform Shield - user decision 7). Fallback: GlitchTip
(MIT, 4 containers, Sentry-API compatible). Sentry SaaS free tier only for
off-machine products (5k events/mo with silent drop); self-hosted Sentry
ruled out (~30 containers). Health: Gatus for active probes (single binary,
YAML the factory can generate per product, N-consecutive-failures
conditions); dead-man heartbeats for cron-like products - the only
affirmative signal for low-traffic things (never compute error rates on tiny
N; silence proves nothing without a heartbeat). Poll, never webhook: no
inbound HTTP surface, survives laptop sleep.

**Design.**

- **Correlation spine:** scaffold injects SDK init with
  `release="<product>@<version>+<git-sha>"` and environment;
  `.kstrl/releases.jsonl` (written by Phase 4) resolves any error's release
  locally to run/PR/stories.
- **Signal record:** `ks signals poll` normalizes API responses (~30 lines
  per source adapter) into typed events: kind (new_issue, regression,
  frequency_breach, health_down, heartbeat_missed), product, fingerprint,
  release, counts, culprit run/PR, truncated prompt-ready sample, deep link.
- **Dedup:** queue key = `(product, fingerprint)` - one open item per key,
  repeats bump count; a fix PR marks the tracker issue resolved-in-release;
  the same fingerprint in a later release is a regression, reopened at
  higher priority.
- **Threshold ladder:** regression -> enqueue immediately (a shipped fix
  failed - strongest signal); new issue in latest release -> enqueue after
  >= 3 events or 2 distinct users, else watch 24h; frequency breach on
  old/ignored issues -> notify only.
- **Breakers:** storm breaker - more than X new fingerprints within an hour
  of deploy collapses into a single "bad release - investigate/rollback"
  item (sorted by users/count before capping so the cap cannot hide the
  worst issue); lineage breaker - a fingerprint that regresses twice against
  factory-authored fixes stops auto-queueing and flags `needs_human`.
- **Doctrine:** every runtime-fix PR must include a reproducing test -
  converting the runtime signal into a build-time signal the existing
  verifier can hold. This, not the poller, is what prevents
  symptom-patching.
- **Learning:** escaped-defects-per-release becomes a ground-truth stream:
  evolution records which components/story types generate runtime defects;
  calibration gains "did review flag the code that later threw in prod?".

**Failure modes.** Queue floods from bad deploys (storm breaker);
self-fix oscillation (lineage breaker + reproducing-test rule); fingerprint
instability across refactors (SDK fingerprint hints for known error
classes); monitoring the monitor (Gatus probes Bugsink itself); runtime
noise starving planned work (its own budget/priority lane - ties into user
decision 8).

**Done when:** poller normalizes fixture responses from both sources; dedup,
thresholds, and both breakers unit-tested; scaffold injects SDK init +
release tag; end-to-end demo: planted error -> signal -> queue item -> fix
PR carrying a reproducing test.

---

## Non-goals for R8

- **100% dark operation.** Sampled human review persists at every level
  (ironies-of-automation: monitors who never intervene lose the ability
  to); the E6 checkpoint machinery is repurposed, never deleted.
- **Two-machine queue operation.** The processed-ids ledger is per-machine;
  distributed intake is out of scope until an always-on box exists.
- **Per-platform deploy adapters.** The command driver is the escape hatch;
  adapters chasing third-party CLI churn are a maintenance liability.
- **Webhook infrastructure.** Everything polls; Kestrel runs no inbound
  HTTP surface. Revisit only if the Linear Agents API exits preview and
  earns its tunnel.
- **Building queue/monitoring/policy engines in-house** beyond the thin
  substrates specified above.

## Research references

Primary threads behind this plan: DoD cATO evaluation criteria and
DevSecOps reference designs (earned/revocable autonomy); Cusumano, Japan's
Software Factories (intake as survival, process discipline); Greenfield &
Short, Software Factories (economies of scope, limits of mechanization);
Google mutation-testing-at-scale (TSE 2021); "All Smoke, No Alarm"
(arXiv:2606.18168); ImpossibleBench (arXiv:2510.20270); Correlated Errors
in LLMs (arXiv:2506.07962); Meta ACH (arXiv:2501.12862); Levels of Autonomy
for AI Agents (arXiv:2506.12469); Argo Rollouts analysis templates; Western
Electric rules. Per-item source lists live in the R8 issues (#148-#155).
