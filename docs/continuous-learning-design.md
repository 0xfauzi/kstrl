# Continuous learning: audit and redesign

Status: proposed. Supersedes the `ks evolve` proposal generator described in
[spec-harness-engineering.md](spec-harness-engineering.md) section 3.4.

## 1. Audit of the current claim

`README.md:98` states "the harness improves itself". The code does not support
the claim. Three defects, each verified against this repository.

### 1.1 Learning is siloed per repository

`EvolutionConfig.journal_path` defaults to `.kstrl/evolution.jsonl` and resolves
against `root_dir` (`kstrl/evolution.py:67`). `KnowledgeConfig.knowledge_root`
resolves to `root_dir/.kstrl/knowledge` (`kstrl/knowledge.py:59`). The R8.9 XDG
relocation keys the control directory by `repo_id(root_dir)`
(`kstrl/statedir.py:176`), so it moved the silo without removing it.

Nothing kstrl learns in one project can reach another project. This is the
capability the product claims and the capability that does not exist.

### 1.2 Proposals are string templates, not learning

`EvolutionJournal.propose_improvements` (`kstrl/evolution.py:752`) is f-string
formatting. It makes no LLM call and consults no evidence. The output in this
repository shows the result. `.kstrl/proposals/prop-003.md` reads:

> Pattern 'push-of-ralph-failed-to-https' (pr) occurred 1 times.
> Add to CLAUDE.md: > Known issue: 'push-of-ralph-failed-to-https'. Take extra
> care with this pattern.

That instruction carries no information. It also came from a single occurrence.
`.kstrl/experiments.tsv` records 5 runs, and 4 of them failed on git or PR
transport errors. The recorded patterns are infrastructure flakes, not
engineering lessons.

### 1.3 The loop is open

No code path reads a proposal back into a run. `auto_apply_computational`
defaults to `False` (`kstrl/evolution.py:72`). `kstrl/proposals.py` applies only
convention proposals, and only behind a prompt. `get_experiment_trends` records
retry rate per run, but no code attributes a change in retry rate to an applied
proposal. There is no acceptance number and no attribution.

### 1.4 What does work, and stays

The per-component knowledge layer is a real closed loop inside one project.
`distill_facts` writes facts, `build_knowledge_context` injects them into the
next component's prompt, and `measure_fact_utilization` reports an honest lower
bound on uptake. It is wired into `kstrl/pipeline.py` and `kstrl/factory.py`.
This design keeps it and extends it.

## 2. Decision

Adopt **GEPA** as the optimizer library. Port **ACE**'s playbook idea as kstrl
code. Do not adopt an agent memory store.

### 2.1 Why not Mem0, Zep, Letta, or Cognee

Those libraries store conversational facts and user state. kstrl's unit of
learning is not "what the user said". It is "this rule made verification pass".
Adopting one would supply a database for the wrong data.

### 2.2 Why GEPA

Measured facts, verified locally on 2026-08-13 against gepa 0.1.4:

| Property | Measured value |
|---|---|
| Install | `pip install gepa` succeeds |
| Transitive dependencies | zero |
| License | MIT, compatible with kstrl's MIT |
| Adapter surface | 2 methods (`evaluate`, `make_reflective_dataset`) |
| Budget controls | `max_metric_calls`, `max_reflection_cost` |
| Stop conditions | `ScoreThresholdStopper`, `NoImprovementStopper`, `TimeoutStopCondition` |
| Acceptance | `acceptance_criterion="strict_improvement"` |
| Multi-objective | `objective_scores`, `frontier_type="objective"` |
| Audit trail | `run_dir`, `candidates`, `parents`, `total_metric_calls` |

Zero transitive dependencies matters. kstrl core runs on rich, click, and
textual by deliberate choice (`pyproject.toml:33`). GEPA fits as an optional
extra without weight.

The controls map onto kstrl policy line for line. `max_metric_calls` enforces
the `max_adversarial_calls` budget rule. `acceptance_criterion` supplies the
acceptance number that CLAUDE.md demands. `run_dir` plus `parents` supplies the
audit trail that H3 requires. `frontier_type="objective"` stops a reviewer gain
from silently regressing the security reviewer.

kstrl already owns the part most adopters lack: an objective metric.
`kstrl/calibration.py` provides `role_detection_rate`, `Baseline`,
`compare_baselines`, and `min_role_rate`. That is a scored metric with a
pass/fail gate already written.

### 2.3 Why port ACE rather than depend on it

ACE (`ace-agent/ace`, Apache-2.0) is benchmark reproduction code. It is not
pip-installable, and its last commit was November 2025. Its valuable content is
a design idea, not a runtime: update a structured playbook by small delta
operations instead of rewriting it, then periodically deduplicate. That is
roughly 150 lines of kstrl code. Take the idea, not the dependency.

## 3. Routing taxonomy

The defect in section 1.2 is not that the reflection is weak. It is that every
signal shares one destination. `append_to_agent_learnings`
(`kstrl/proposals.py:119`) appends every pattern to the CLAUDE.md "Agent
Learnings" section, whatever the pattern is. A git transport failure has no
useful expression as an instruction to an agent. That is how `prop-003.md`
happened.

Routing comes first. Each kind of learning gets one destination, one writer, and
one gate.

| Kind | About | Destination | Writer | Gate | Scope |
|---|---|---|---|---|---|
| 1. Facts | What this code is | `.kstrl/knowledge/<component>/*.md` | Distiller LLM | none | One project |
| 2. Rules | What to do when building | `$XDG_STATE_HOME/kstrl/global/playbook/` | Reflector + Curator | automatic retirement | All projects |
| 3. Role defects | How well kstrl judges | `*_PROMPT` constants in `kstrl/review.py`, `kstrl/security.py`, `kstrl/decompose.py` | GEPA proposes | calibration, human, H3 | The harness |
| 4. Mechanical defects | Something is broken | A GitHub issue | Router, no LLM | none | n/a |

Kind 1 exists and works (section 1.4). Kind 2 and kind 3 are sections 5.1 and
5.3. Kind 4 is new and load-bearing.

### 3.1 Kind 4 must leave the learning system

A skipped phase, a failed push, a timeout, and a budget halt are bugs or
configuration faults. They are not lessons. The router classifies them from the
failure signature and files them. No LLM reflects on them.

This class is large, not marginal. `phase_skipped` appears in 5 of the 8
component results in `.kstrl/evolution.jsonl`, always on the security phase. 4
of the 5 runs in `.kstrl/experiments.tsv` failed on git or PR transport. Today
that traffic is the majority of what `ks evolve` reads and all of what it
"learns" from.

The `Finding` taxonomy already carries the discriminator.
`kstrl/evolution.py:1039` treats `infrastructure_error` and `phase_skipped` as
non-signal when computing hit rates. The router extends that same judgment to
the proposal path, which is where it was missing.

## 4. The self-modification ladder

"The harness improves itself" covers five different things with different blast
radii. This design commits to rungs 1 through 4 and excludes rung 5.

| Rung | What changes | Reversal | Human gate |
|---|---|---|---|
| 1. Memory | Facts, playbook bullets, journal rows accumulate | Delete the row | No |
| 2. Context | The assembled prompt, at `kstrl/factory.py:1440` | Delete the store | No |
| 3. Project instructions | `CLAUDE.md`, `kstrl.toml` | `git revert` | Yes |
| 4. Role instructions | `*_PROMPT` constants in kstrl's source | `git revert` plus version bump | Yes, plus calibration |
| 5. Harness logic | `kstrl/*.py` | n/a | Out of scope |

Rung 2 is where most of the value accrues, and it rewrites nothing. The prompt
the agent sees differs between runs, but no file on disk changes. This is ACE's
thesis: adapt the context, not the weights and not the code.

Rung 4 is the only genuine self-modification in the design. Rung 5 is excluded.
No component of this design writes kstrl's Python source.

### 4.1 The gating rule

Gates get heavier down the ladder because the failure modes differ in kind, not
in degree.

A bad playbook bullet is cheap. It wastes context on one run, its target
signature keeps recurring, the attribution counter in section 5.2 notices, and
retirement removes it. The failure announces itself.

A bad prompt rewrite is expensive. A reviewer degraded by a few percent misses
real defects on every project, silently, until somebody re-runs calibration. The
failure is an absence, and no recurrence counter detects an absence.

The rule that follows:

> Learning whose failure announces itself by recurrence may be automatic.
> Learning whose failure mode is silence requires a human and a held-out eval.

This is why rung 2 needs no approval and rung 4 needs four gates.

## 5. Architecture

Two loops with different clocks. Loop 1 runs after every factory run. Loop 2
runs rarely, offline, and behind a human gate. Loop 1 implements kind 2 of the
taxonomy. Loop 2 implements kind 3.

### 5.1 Loop 1: the global playbook (cross-project experience)

Replaces `propose_improvements`.

```
Factory run (any project)
    |
    v
[Router]      No LLM. Classifies each signature by kind (section 3).
    |         Kind 4 leaves here as a filed issue and never reaches
    |         the Reflector. Only kind 2 continues.
    v
[Reflector]   LLM. Reads failure signatures, review findings,
    |         and retry deltas. Emits candidate lessons with evidence.
    v
[Curator]     Emits delta ops against the playbook:
    |         ADD | UPDATE | DEMOTE | RETIRE. Never a full rewrite.
    v
[Global playbook]   $XDG_STATE_HOME/kstrl/global/playbook/
    |               Outside every repository. Survives repo deletion.
    v
[Injection]   Bullets appended to the engineer prompt beside the
    |         existing knowledge_prefix, under a token cap.
    v
Next factory run, any project
```

Store location: a `global` scope sibling to the existing per-repo control
directory. `kstrl/statedir.py` already provides `xdg_state_home()`, so this
extends an existing mechanism rather than adding one.

Lesson record fields: `id`, `claim`, `evidence` (run ids and failure
signatures), `scope` (language, tool, or universal), `injected_count`,
`recurrence_after_injection`, `status`.

### 5.2 Attribution: the mechanism that is missing today

A bullet earns its place or it is retired. When bullet B is injected into a run,
the reducer records whether the failure signature B targets recurred in that
run. `recurrence_after_injection` divided by `injected_count` is the bullet's
miss rate.

Retirement rule: a bullet injected at least `N` times whose target signature
recurs at or above its pre-injection base rate is demoted, then retired. The
value of `N` and the demotion threshold must be measured, not chosen. Section 7
covers this.

This is the difference between the new design and the current one. Today a
proposal is written and never checked again.

### 5.3 Loop 2: GEPA over the adversarial prompts

Seed candidate is the prompt registry:

```python
seed_candidate = {
    "reviewer": REVIEWER_PROMPT,
    "security": SECURITY_PROMPT,
    "decompose": DECOMPOSE_PROMPT,
    "distill": DISTILL_PROMPT,
}
```

Data comes from `tests/adversarial_fixtures/`. Fixture counts, measured:

| Directory | Fixtures |
|---|---|
| `concerns` | 4 |
| `concerns_negative` | 4 |
| `security` | 10 |
| `security_negative` | 4 |
| `specs` | 4 |

The negative fixtures are not optional. Without them GEPA optimizes recall and
destroys precision, because "flag everything" scores perfectly on positives
alone. Every valset split must carry its matching negatives.

Adapter: `KstrlGepaAdapter` implements the two protocol methods. `evaluate`
runs the fixtures against a candidate and returns per-role `objective_scores`.
`make_reflective_dataset` returns the misses and the false positives, which is
the material GEPA reflects on.

Gate chain, in order. All four must pass:

1. GEPA reports strict improvement on the valset.
2. `compare_baselines(old, new).passed` is true, so no role drops below
   `min_role_rate`.
3. A human approves the diff.
4. The same PR carries the prompt body, the `*_PROMPT_VERSION` bump, and the
   `_EXPECTED_SNAPSHOTS` hash update.

Step 4 preserves H3 exactly as written. GEPA proposes; the policy still gates.

## 6. The cross-project data boundary

Decision taken 2026-08-13: contribute everything by default, opt out per
project.

The opt-out must be explicit and fail closed. A project must not leak by
silence or by error.

```toml
[learning]
contribute = true   # send lessons to the global playbook
consume = true      # inject global playbook bullets into prompts
```

Fail-closed rules:

- If `kstrl.toml` cannot be read, `contribute` is false for that run.
- If the global store is unreachable or its permissions are wrong, skip the
  contribution and warn. Never fall back to an in-tree copy.
- A lesson whose `evidence` contains a literal source line is rejected at write
  time. Evidence carries signatures, paths, and run ids, not code.

Known accepted risk: a lesson distilled from one project reaches another
project's prompt by default. This is safe while every project belongs to one
owner. Revisit before kstrl runs on repositories belonging to different
parties.

## 7. What must be measured before building

Per the project epistemology rule, these are open numbers. Do not guess them.

1. **GEPA cost per optimization pass.** Unknown. A metric call runs the
   fixtures against a candidate. Measure cost per metric call on cached
   fixtures first, then multiply by the `max_metric_calls` cap.
2. **Overfitting risk at 26 fixtures.** The fixture set is small for
   evolutionary search. Measure the train and valset gap on a held-out third
   split before trusting any candidate.
3. **Attribution thresholds.** `N` injections before retirement, and the
   demotion threshold in section 5.2. Derive both from journal data once the
   playbook has run.
4. **Playbook token cost.** Injected bullets compete with the existing
   knowledge tiers for context. Measure the ceiling before setting it.

## 8. Phasing

Each phase ships and is measured before the next starts.

- **Phase 0, sub-minute pilot.** Rung 0. `KstrlGepaAdapter` against a stub LM
  and two cached fixtures. Proves the seam works. Costs no LLM spend. No prompt
  lands.
- **Phase 1, the router.** Rung 0. Classify every signature by kind and file
  kind 4 as issues. No playbook yet. This phase alone stops the noise documented
  in section 3.1, and it is a prerequisite: without it, kind 4 traffic floods the
  playbook exactly as it flooded `ks evolve`.
- **Phase 2, global playbook store.** Rung 1. Store, delta ops, opt-out, and the
  fail-closed rules. No injection yet. Verifies that lessons accumulate and that
  opt-out holds.
- **Phase 3, injection and attribution.** Rung 2. Inject bullets, record
  `recurrence_after_injection`. This is the first phase that can change a factory
  outcome.
- **Phase 4, retirement.** Rung 2. Turn on demotion and retirement using
  thresholds measured in phase 3.
- **Phase 5, GEPA on prompts.** Rung 4. Real spend, real prompts, full four-step
  gate chain.

Rung 3 (CLAUDE.md and `kstrl.toml` edits) already exists via
`kstrl/proposals.py` and is not re-built. It gains correctness for free once the
router stops feeding it kind 4 signals.

Delete `propose_improvements` and its template branches at phase 4, not before.
The old path stays until the new one demonstrates attribution.
