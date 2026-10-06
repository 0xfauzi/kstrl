"""Init command for kstrl - initialize harness in a project."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from kstrl import git
from kstrl.appendio import append_records
from kstrl.atomicio import atomic_write_text
from kstrl.jsonread import read_json_file
from kstrl.operator_context import GUIDANCE_HEADING
from kstrl.prd import PRD
from kstrl.stack import load_stack
from kstrl.toolchains import has_build_manifest

if TYPE_CHECKING:
    from kstrl.ui.base import UI

# Default file contents
DEFAULT_PRD = {
    "branchName": "kstrl/feature",
    "userStories": [],
}

DEFAULT_PROMPT_VERSION = "1.5.0"

# v1.5.0 (#696 slice 4): step 9 names the `Stack` block's checks. The
# `Verification Commands (resolved by kstrl)` block it named is gone with
# the commands kstrl used to choose: a confirmed [stack] is the only source
# of the commands kstrl runs, and `loop.build_project_context` renders it.

# v1.4.0 (#585): the engineer no longer writes the codebase map. Step 10
# sent its facts to `$codebase_map_path`, a file every component shares
# and the operator owns. With `ks init` output uncommitted the base branch
# does not track the map, so the branch created and committed it and
# `git merge` refused on the untracked copy in the root checkout. With it
# committed, two same-tier components that both appended to it made the
# component depending on both fail `worktree_setup`, because their
# branches conflict on the map. Step 10 now sends the facts to the
# component's own `$progress_path`, which its branch already carries and
# which the knowledge distiller reads as part of the branch diff. The map
# is read-only in step 4, and the factory substitutes the root checkout's
# copy for it. Step 11 excludes the repository root's AGENTS.md and
# CLAUDE.md, which are the operator's files. Measured on 1.3.0: an
# engineer that appends to the root AGENTS.md fails the component on
# diff_scope whether or not `ks init` output is committed, and when it is
# committed the write lands on CLAUDE.md through the AGENTS.md symlink.
#
# v1.3.0 (#276): step 9 deferred to the verification block the harness
# injected (verify.VERIFY_COMMANDS_PROMPT, retired at the #696 flag day;
# step 9 is now neutral) instead of telling the agent to
# find its own typecheck and test commands. #261 made
# verify.resolve_verify_commands the only answer to "what does Phase 1
# run" and injected it above this body every iteration, but step 9 still
# told the agent to derive two of the three and never mentioned lint at
# all - so a blocking `ruff check` failure was discovered by the gate
# rather than by the agent, which costs a whole engineer iteration.
# Step 14 carried a second copy of the same done-rule ("only after
# tests/typecheck pass"), with the same lint omission, at the moment the
# agent flips passes to true; it now refers to step 9 instead.
#
# The no-block branch states a FLOOR, not a proportionality hint, and it
# is scoped to marking a story done rather than to every iteration:
# `ks feature`'s implement and repair loops reach it while writing
# production code with no Phase 1 at all (feature_cmd has no
# verification), while an `ks understand` iteration that reaches it -
# only un-init'd, since `ks init` scaffolds a separate
# understand_prompt.md - marks no story and so owes nothing.
#
# INTERIM, tracked as #288. A text floor is not a mechanism: nothing on
# that path samples whether the agent ran anything, and it asks the agent
# to find commands that resolve_verify_commands could have named, which
# is the derive-your-own shape #261 exists to remove. The harness stays
# silent there because VERIFY_COMMANDS_PROMPT (retired at the #696 flag
# day) claimed the gate ran the commands, which would be false, and because #261 decided that
# verify_config=None states nothing. Revisiting that is #288's job.
#
# The $prd_path / $progress_path / $codebase_map_path placeholders are
# substituted by loop.run_loop (string.Template.safe_substitute) with the
# per-component paths, so a decomposed component's agent reads the SAME
# PRD file that verify.check_prd_stories re-reads (R2.3, H-11). Before
# v1.1.0 the body hardcoded scripts/kstrl/prd.json while decomposed PRDs
# live at scripts/kstrl/feature/<id>/prd.json - the agent and the
# verifier disagreed on which file mattered.
DEFAULT_PROMPT = """# kstrl Agent Instructions

You are the implementing engineer in a software factory. You will be
reviewed by a hostile code reviewer when you declare done; treat that
reviewer as already reading your diff while you write it.

## Your Task (one iteration)

1. Read the PRD file for this run: `$prd_path`
2. Read `$progress_path` (check `## Codebase Patterns` first)
3. Derive a short list of keywords from the PRD intent, not just exact wording.
4. If `$codebase_map_path` exists, query it for sections relevant to your story
   using those keywords.
   - Do not load the entire file.
   - Always check **Quick Facts** and any relevant **Iteration Notes**.
   - Read it only. It is the operator's file and not part of your branch.
5. If a feature understand file exists for this PRD, query it using the same keywords.
   - Default path: `scripts/kstrl/feature/<feature_name>/understand.md`
   - If the PRD is at `scripts/kstrl/feature/<feature_name>/prd.json`, use that folder name.
   - Otherwise use the PRD filename stem as `<feature_name>`.
6. Branch is pre-checked out to `branchName` from the PRD
   (verify only; do not switch)
7. Pick the highest priority story where `passes` is `false` (lowest `priority` wins)
8. Implement that ONE story (keep the change small and focused)
9. Run the checks:
   - Run every command under `Checks` in the `Stack` block above, in order,
     exactly as written. Do NOT derive your own or substitute a narrower or
     broader variant (an added path, a filter, a dropped flag): a command kstrl
     will not run proves nothing.
   - If a check fails for a reason other than your work, fix the cause and never
     substitute a different command. Missing tooling is yours to install. A
     check that is wrong for this project is not: name the `[stack]` section of
     `kstrl.toml` in your progress entry rather than editing it, because kstrl's
     policy envelope can treat that edit as tampering.
   - Do NOT mark the story as done until every check passes. If that block is
     absent, nothing will check this work mechanically: run the project's own
     checks yourself first.
10. If you discover durable, reusable codebase facts, add a brief, evidence-based note
   under `## Codebase Patterns` at the top of `$progress_path` (skip if nothing new).
   Do not edit `$codebase_map_path`.
11. Update `AGENTS.md` files with reusable learnings
   (only if you discovered something worth preserving):
   - Only update `AGENTS.md` in directories you edited, never the repository
     root's `AGENTS.md` or `CLAUDE.md`: those are the operator's files
   - Add patterns/gotchas/conventions, not story-specific notes
12. **Adversarial self-check.** Before declaring done, append the EXACT
    heading `## Self-Critique` (verbatim, two hash marks - the harness
    verifies this string) followed by AT LEAST 3 bullet lines (`- `).
    Each bullet must be substantive: not `TBD`, not `TODO`, not `N/A`.
    Format each bullet as: `- If X happens, this code will do Y, which
    is wrong because Z.` Categories to consider: invalid/empty/None
    input, concurrent access, partial-failure mid-way through a
    multi-step operation, hostile input, schema drift, missing
    auth/authz check, swallowed errors, performance under load,
    time/locale dependence. If you genuinely cannot find three, look at
    every new function and ask what could break it. Placeholders will
    fail the mechanical check.
13. Commit with message: `feat: [ID] - [Title]`
14. Update `$prd_path`: set that story's `passes` to `true`
    (only after step 9 is green AND the self-critique is written)
15. Append learnings to `$progress_path`

## PRD ambiguity

If the PRD is too vague to implement responsibly, do NOT guess. Append an
`## INTERPRETATION` block to `$progress_path` stating what
assumptions you are making and why. The reviewer will see this and can
push back; silent guesses become silent bugs.

## Progress Format

Append this to the END of `$progress_path`:

## [YYYY-MM-DD] - [Story ID]
- What was implemented
- Files changed
- Verification run (exact commands)
- **Learnings:**
  - Patterns discovered
  - Gotchas encountered
- **Self-Critique:**
  - Failure mode 1: ...
  - Failure mode 2: ...
  - Failure mode 3: ...
- **Interpretations** (only if PRD was ambiguous): ...
---

## Codebase Patterns

Add reusable patterns to the TOP section in `$progress_path`
under `## Codebase Patterns`.

## Stop Condition

If ALL stories pass, reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""

DEFAULT_PROGRESS = """# kstrl Progress Log

## Codebase Patterns
- (add reusable patterns here)

## Iteration Notes
- (append entries below using the format in prompt.md)

---
"""

DEFAULT_CODEBASE_MAP = """# Codebase Map (Brownfield Notes)

This file is meant to be built over time using the kstrl **codebase understanding** loop.

## How to use this map

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Read-only mode**: in understanding mode, the agent should ONLY edit this file.
- **Small increments**: one topic per iteration keeps notes high-signal.

## Next Topics (checklist)

Edit this list to match your repo. During the understanding loop, mark items as done.

- [ ] How to run locally (setup, env vars, start commands)
- [ ] Build / test / lint / CI gates (what runs in CI and how)
- [ ] Repo topology & module boundaries (where code lives, layering rules)
- [ ] Entrypoints (server, worker, cron, CLI)
- [ ] Configuration, env vars, secrets, feature flags
- [ ] Authn/Authz (where permissions are enforced)
- [ ] Data model & persistence (migrations, ORM patterns, transactions)
- [ ] Core domain flow #1 (trace end-to-end)
- [ ] Core domain flow #2 (trace end-to-end)
- [ ] External integrations (third-party APIs, webhooks, queues)
- [ ] Observability (logging, metrics, tracing, error reporting)
- [ ] Deployment / release process

## Quick Facts (keep updated)

- **Language / framework**:
- **How to run**:
- **How to test**:
- **How to typecheck/lint**:
- **Primary entrypoints**:
- **Data store**:

## Known "Do Not Touch" Areas (optional)

- (add directories/files that are fragile or off-limits)

---

## Iteration Notes

(New notes append below; keep older notes for history.)
"""

# R10.8. Enrolled in SCAFFOLDED_TEMPLATES, and the digest history there
# is load-bearing rather than advisory: operator_context.load_operator_file
# asks shipped_label whether the file on disk is a body kstrl itself
# wrote, and injects nothing when it is. Review round 1 measured the
# alternative end to end: without that check an untouched `ks init`
# scaffold put 479 characters of angle-bracket placeholders at the head
# of every engineer prompt of every component of every attempt, under
# a header asserting the operator had authored them. So a body change
# here APPENDS a row below; editing or dropping one stops kstrl
# recognising the copy already on an operator's disk and re-opens that.
#
# EVERY SENTENCE HERE HAS TWO READERS AND MUST BE TRUE FOR BOTH. The
# scaffold is suppressed only while it is unchanged, so the whole body
# reaches the engineer's prompt from the operator's first edit onward,
# under a header saying the operator wrote it. R10.9 round 1 measured
# what the old body then said there: "While this file is unchanged since
# `ks init` wrote it, nothing is injected" was read by the engineer at
# the moment it was injected, which makes it false exactly where it is
# read, and "Keep it short" and "Replace the placeholders" are
# imperatives addressed to somebody who is not reading. Lifecycle text
# belongs in docs/runbook.md and in the README, where it costs no
# tokens; what stays here is a title and one declarative sentence.
DEFAULT_GOLDEN_PATTERNS = """# Golden patterns

Operator-authored: what a good change looks like in this repository,
with a file to copy from for each pattern.

## Follow these (with a file to copy from)

- <pattern>: see `<path/to/exemplar.py>`

## Avoid these

- <anti-pattern and why>

## When unsure

- <which existing module to imitate>
"""

# R10.9. Enrolled in SCAFFOLDED_TEMPLATES for the same load-bearing
# reason DEFAULT_GOLDEN_PATTERNS is: without a row, an untouched skeleton
# is injected into every engineer prompt forever. So a body change here
# APPENDS a row below and never edits or drops one.
#
# The preamble is a title and one declarative sentence, for the reason
# stated above DEFAULT_GOLDEN_PATTERNS: it is not operator-only text.
# The moment anyone edits this file the whole body reaches the engineer
# on every attempt, preamble included, so a sentence about the
# scaffold's own lifecycle is either false there ("Nothing is injected
# while this file is unchanged", read while it is being injected) or
# addressed to somebody who is not reading it ("Keep `## Guidance`
# last"). Both are gone. What belongs in the file is in docs/runbook.md
# and in the README, where it costs no tokens.
#
# `## Guidance` is LAST because that is where an operator looks for it,
# not because the writer depends on it: R10.10 (#231) finds the heading
# and inserts at the end of THAT section, and adds the heading when the
# file has none. A section added after `## Guidance` therefore keeps its
# own contents. That is stated here and held by
# `tests/test_steering.py`, not by a line of the shipped body, because
# the engineer cannot act on it and the operator reads it in the runbook.
# Interpolated rather than a second literal of the heading (#231 B1):
# `operator_context.GUIDANCE_HEADING` is what the writer and the
# truncator both read, and `tests/test_steering.py` pins this body to
# end with it so the three cannot drift apart again.
DEFAULT_MEMORY = f"""# Memory

Standing feedback for kstrl runs in this repository: durable rules that
should change future runs, not one-off instructions.

{GUIDANCE_HEADING}
"""

DEFAULT_FEATURE_UNDERSTAND = """# Feature Understand Notes

This file captures feature-specific understanding tied to one PRD.

## How to use this file

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Feature scope**: keep notes anchored to the PRD for this feature.
- **Small increments**: one topic per iteration keeps notes high-signal.

## Quick Feature Facts (keep updated)

- **PRD**:
- **Branch**:
- **Stories in scope**:
- **Primary entrypoints**:
- **Data touched**:
- **Tests / commands**:

## Story Coverage (checklist)

- [ ] (add story IDs from the PRD)

## Known Risks / Hotspots (optional)

- (add areas likely to break or require extra care)

---

## Iteration Notes

(New notes append below; keep older notes for history.)
"""

DEFAULT_UNDERSTAND_PROMPT = """# kstrl Codebase Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **codebase understanding** loop. Your job is to explore the existing codebase
and write an evidence-based "map" for humans.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is:**
- `scripts/kstrl/codebase_map.md`

If you think code changes are needed, write that as a note in the map under
**Open questions / Follow-ups**. Do not implement changes in this mode.

## What to do

1. Read `scripts/kstrl/codebase_map.md`.
2. Choose ONE topic to investigate this iteration:
   - If `codebase_map.md` has a **Next Topics** checklist, pick the first unchecked item.
   - Otherwise follow this default order:
     1) How to run locally
     2) Build / test / lint / CI gates
     3) Repo topology & module boundaries
     4) Entrypoints (server/worker/cron/CLI)
     5) Configuration, env vars, secrets, feature flags
     6) Authn/Authz
     7) Data model & persistence (migrations, ORM patterns)
     8) Core domain flows (trace one end-to-end)
     9) External integrations
     10) Observability (logging/metrics/tracing)
     11) Deployment / release process
3. Investigate by reading docs, configs, and code. Prefer fast, high-signal entrypoints:
   - README / docs
   - package/lock files
   - build/test scripts
   - app entrypoints (server/main)
   - routes/controllers
   - data layer (models, migrations)
4. Update **ONLY** `scripts/kstrl/codebase_map.md`:
   - Append a new **Iteration Notes** section for this topic (template below)
   - If you used a Next Topics checklist, mark the topic as done (`[x]`)
   - Keep notes concise, factual, and verifiable

## Evidence rules (important)

- Every "fact" should include **evidence**:
  - File paths
  - What to look for (function/class name)
  - Preferably line ranges (if your tooling can provide them)
- If you are uncertain, label it clearly as a hypothesis and add an **Open question**.

## Iteration Notes format

Append this to the END of `scripts/kstrl/codebase_map.md`:

## [YYYY-MM-DD] - [Topic]

- **Summary**: 1-3 bullets on what you learned
- **Evidence**:
  - `path/to/file.ext` - what to look for (and line range if available)
- **Conventions / invariants**:
  - "Do X, don't do Y" rules implied by the codebase
- **Risks / hotspots**:
  - Areas likely to break or require extra care
- **Open questions / follow-ups**:
  - What's unclear, what needs human confirmation

---

## Stop condition

If there are **no remaining unchecked topics** in the Next Topics checklist
(or you have covered the default list above), reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""

DEFAULT_FEATURE_UNDERSTAND_PROMPT = """# kstrl Feature Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **feature understanding** loop for a specific PRD.
Your job is to build a focused, evidence-based map of the code that this feature touches.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is the feature understand file, for example:**
- `scripts/kstrl/feature/<feature_name>/understand.md`

If you think code changes are needed, write that as a note in the feature understand file
under **Open questions / Follow-ups**. Do not implement changes in this mode.

## What to do

1. Read the feature PRD file you were given.
2. Derive a short list of keywords from the PRD intent, not just exact wording.
3. Read `scripts/kstrl/codebase_map.md` and query only the sections relevant to this feature.
   - Always check **Quick Facts** and any relevant **Iteration Notes**.
   - Do not load the entire file.
4. Investigate by reading docs, configs, and code. Prefer fast, high-signal entrypoints:
   - README / docs
   - build/test scripts
   - app entrypoints (server/main)
   - routes/controllers
   - data layer (models, migrations)
5. Update **ONLY** the feature understand file:
   - Update **Quick Feature Facts** if you learned something durable
   - Append a new **Iteration Notes** section for this topic (template below)
   - If there is a **Story Coverage** checklist, mark items you verified

## Evidence rules (important)

- Every "fact" should include **evidence**:
  - File paths
  - What to look for (function/class name)
  - Preferably line ranges (if your tooling can provide them)
- If you are uncertain, label it clearly as a hypothesis and add an **Open question**.

## Iteration Notes format

Append this to the END of the feature understand file:

## [YYYY-MM-DD] - [Topic]

- **Summary**: 1-3 bullets on what you learned
- **Evidence**:
  - `path/to/file.ext` - what to look for (and line range if available)
- **Conventions / invariants**:
  - "Do X, don't do Y" rules implied by the codebase
- **Risks / hotspots**:
  - Areas likely to break or require extra care
- **Open questions / follow-ups**:
  - What's unclear, what needs human confirmation

---

## Stop condition

If there are **no remaining unchecked stories** in the **Story Coverage** checklist,
reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""


# Scaffolded kstrl.toml (R2.1): the project's discoverable config
# surface. Each key is commented out and shows its built-in default, so
# scaffolding changes no effective value; uncommenting a line is the
# explicit opt-in. Content mirrors kstrl.toml.example trimmed to keys the
# loaders actually read, plus the [timeout] section wired in R0.1.
DEFAULT_KSTRL_TOML = """\
# kstrl configuration (scaffolded by `ks init`).
# Each key below is commented out and shows its built-in default: uncomment
# a line to override it. This file lists the keys most projects set; the
# README's configuration reference lists every section and key.
# Precedence: CLI flag > environment variable > this file > built-in
# default. See docs/env-vars.md for the env-var mapping.

[agent]
# type = ""                        # "claude-code" | "claude-sdk" | "codex" (empty = auto-detect)
# command = ""                     # custom agent shell command; overrides type
# model = ""                       # e.g. "sonnet" for claude, "gpt-5.5" for codex (empty = agent default)
# reasoning_effort = ""            # low|medium|high|max

[run]
# max_iterations = 10
# sleep_seconds = 2
# interactive = false

[paths]
# prompt = "scripts/kstrl/prompt.md"
# prd = "scripts/kstrl/prd.json"
# Setting `progress` forces ONE path on every factory component; left
# unset, each component's engineer writes beside its own PRD, which is
# inside that component's allowedPaths.
# progress = "scripts/kstrl/progress.txt"
# codebase_map = "scripts/kstrl/codebase_map.md"
# golden_patterns = "scripts/kstrl/golden-patterns.md"
# memory = "scripts/kstrl/memory.md"
# allowed = []                     # e.g. ["scripts/kstrl/", "src/"]

[git]
# branch = ""                      # override branch (empty = use PRD branchName)
# auto_checkout = true

[ui]
# ascii = false

# Factory orchestrator settings (Phase 0-3 pipeline coordination).
[factory]
# max_parallel = 4                 # concurrent component workers
# max_retries = 3                  # per-component retry budget across all phases
# retry_delay = 5.0                # seconds between retry attempts
# use_worktrees = true             # branch each component into .kstrl/worktrees/<id>
# single_pr = false                # one PR for the whole factory vs per-component
# create_prs = true                # call `gh` to push + merge per component
# review_mode = "hard"             # hard | advisory | skip
# review_timeout_seconds = 0.0     # code and integration reviewer call; 0 = no limit
# architect_timeout_seconds = 0.0  # architect call (ks decompose, ks factory --spec); 0 = no limit
# merge_timeout = 300.0            # hang guard: seconds to wait for PR merge confirmation
# max_adversarial_calls = 0        # 0 = no limit; caps review+security+distill LLM calls per run
# At the cap, hard-mode review and security HALT the component.
# The halt records failed_check=adversarial_budget and does not retry.
# Advisory skips instead, and can still fail at the claim gate.
# Budget 3 calls per component (docs/runbook.md).
# max_total_tokens = 0             # 0 = no limit; run-level token ceiling
# max_cost_usd = 0.0               # 0 = no limit; run-level USD ceiling, checked between iterations
# pause_before_pr_merge = false    # opt-in HITL checkpoint before each PR push+merge

# The project's stack: what it is built with, and the commands kstrl runs on
# every change. kstrl runs nothing until this table exists and a person
# confirms its exact text (`ks factory` asks; `ks inbox approve` records it).
# A check passes when it exits 0. Every key is required.
# [stack]
# instructions = ""                # what the project is built with, for the models
# setup = ""                       # installs a worktree's dependencies; "" = none
# env = []                         # variables the commands may see
# [stack.checks]                   # run in this order
# tests = ""

# Phase 1 mechanical verification beyond the [stack] checks.
[verify]
# check_diff_scope = true
# check_bad_patterns = true
# subprocess_timeout = 0.0        # 0 = no limit
# require_self_critique = false    # fail Phase 1 if the ## Self-Critique block is missing/sparse
# self_critique_min_bullets = 3
# progress_file_path = ""          # empty = the log beside the component's PRD

# Phase 1 policy envelope (R8.1): declarative merge guardrails enforced on
# ARTIFACTS (git diff, lockfiles), never agent self-report. Opt-in; when
# enabled a violation blocks the merge, and editing enforcement machinery
# (this file, CI workflows, or the verifier code itself) is a
# non-overridable halt.
[policy]
# enabled = false
# paths_deny = [".github/workflows/**", "kstrl.toml", ".kstrl/**", "**/*.pem", "**/.env*"]
# max_files_changed = 40
# max_lines_changed = 1500         # lockfiles excluded from the count
# deps_allow_new = false           # block new packages in lockfiles kstrl reads; L3+ may set true
# secret_patterns = ["AKIA[0-9A-Z]{16}", "-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"]
# enforcement_paths_extra = []     # ADDS to the halt set; can never shrink it
# license_allow = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0"]
# license_deny_partial = ["GPL", "AGPL", "SSPL", "Commons-Clause"]
# license_unresolved = "block"     # block | advisory when no source resolves a license
# license_use_network = true       # PyPI fallback for PyPI packages; false = uv cache only; hashed
# deploy = false                   # reserved for the R8.7 release gate

# Exception inbox (R8.3): one surface for everything awaiting a human.
# On by default; triage with `ks inbox ls`.
[inbox]
# enabled = true
# open_item_cap = 50               # open items after which queue intake pauses
# snooze_hours = 24.0              # default snooze TTL
# notify_action_required = true    # notify only on action-required items

# Across-attempt divergence detector (#265): record (or, in block mode, fail
# on) a retry loop where the change got larger at every one of N consecutive
# failed reviews AND not one of the reviewer's blocking findings was retired.
# Advisory first; graduate to block once you have seen its output on real runs.
[divergence]
# mode = "advisory"                # skip | advisory | block
# growth_steps = 2                 # consecutive steps; needs N+1 measured attempts, must be >= 1

# Autonomy ladder (R8.2): one ordered level (L1-L4) instead of scattered
# autonomy flags. The level derives a flag bundle at run start and wins over
# contradicting flags. Opt-in: L1 is stricter than the defaults (merge gate on).
# `ks autonomy status` shows the level; promotion needs a human ack.
[autonomy]
# enabled = false
# max_level = 4                    # hard ceiling: never run above this level

# Phase 2.5 security review (independent adversarial pass focused on vulns).
[security]
# mode = "skip"                    # skip | advisory | hard (skip = default, opt in explicitly)
# fail_threshold = "high"          # critical | high | medium | low (hard mode only)
# timeout_seconds = 0.0           # 0 = no limit
# agent_cmd = ""                   # leave blank to inherit from [agent]
# agent_type = ""
# model = ""

# Phase 3 cross-component contract testing.
[contract]
# mode = "tier"                    # tier | final | skip
# timeout = 0.0                   # 0 = no limit

# Phase 0 codebase scan (computational structural scan; no LLM).
[codebase_scan]
# enabled = true
# module_map = true
# max_context_tokens = 4000

# Per-component semantic knowledge layer: durable facts about WHAT WAS
# BUILT (interfaces, invariants, contracts, gotchas), written after the
# review passes and read by downstream components automatically.
[knowledge]
# enabled = true
# max_core_tokens = 2000           # current component's facts (full text)
# max_dependency_tokens = 1000     # dependency facts (full text)
# max_sibling_tokens = 500         # other components' facts (first sentence only)
# distill_timeout_seconds = 0.0   # 0 = no limit
# distill_model = ""               # empty = falls back to [agent].model
# max_facts_per_distill = 7
# dependency_scope = "direct"      # direct | transitive

# Continuous-learning journal.
[evolution]
# enabled = true
# journal_path = ".kstrl/evolution.jsonl"
# experiments_path = ".kstrl/experiments.tsv"
# min_pattern_frequency = 2
# lookback_runs = 10

# Timeouts in seconds. Unset or 0 means no limit, which is the default for
# every work limit.
[timeout]
# agent_iteration = 0.0            # per agent iteration; 0 = no limit
# component_total = 0.0            # wall clock per component; 0 = no limit
# scheduler_backstop_margin = 60.0

# Work queue (R8.6): what `ks queue` manages and `ks serve` drains.
[queue]
# max_attempts = 3                 # execution attempts per item before poisoning
# lease_ttl_seconds = 3600.0       # claim validity; the reaper recovers anything past this

# Continuous-intake daemon (R8.6): `ks serve` runs one factory at a time.
# daily_budget_usd can only count cost an adapter reports.
[serve]
# poll_interval_seconds = 60.0     # seconds between poll cycles
# daily_budget_usd = 0.0           # 0 = no limit; any positive value is a hard stop
# max_consecutive_poison = 3       # poisoned in a row before the queue pauses
# caffeinate = true                # hold caffeinate -i per run (macOS)
# factory_timeout_seconds = 0.0    # kill a run after this long; 0 = no limit
# allow_uncovered_cost = false     # true = run unattended under an unenforceable budget
# max_open_prs = 1                 # admission stops at this many open kstrl PRs; 0 = no limit

# GitHub Issues as the remote inbox (R8.6). Off by default: enabling makes
# kstrl poll GitHub and post public comments. Remote items stop at the PR.
[intake_github]
# enabled = false
# repo = ""                        # "owner/name"; empty resolves from the checkout
# queued_label = "kstrl:queued"    # the label that authorizes work
# allowed_actors = []              # logins allowed to apply it; empty = anyone who can label
# label_prefix = "kstrl:"          # prefix of the state labels written back
# max_items_per_sync = 5           # items admitted per sync
# default_priority = 0
# comment_on_result = true         # post the verdict back to the source issue
# dry_run = false                  # true = poll and log, send no writebacks
# timeout_seconds = 60.0
# steer_enabled = false            # true = act on /memory and /iterate PR comments
"""

# What `ks init` does to a scaffolded file. The TUI wizard's preview
# renders these, so the vocabulary belongs beside the code that decides.
ScaffoldAction = Literal["create", "keep", "append"]


# ---------------------------------------------------------------------------
# #286 / H3b: telling an operator their scaffolded prompt is an older
# harness template than the one this kstrl ships. The policy and the
# argument for it live in docs/adversarial-roadmap.md H3b.
#
# The key is the file's own SHA-256, not a version stamped into it,
# because a scaffolded file is byte-identical to the constant it came
# from: `_create_if_missing` writes the body verbatim and loop.run_loop
# substitutes $prd_path and friends at READ time. So a digest lookup
# classifies files written long before this mechanism existed, separates
# a pristine older scaffold from an edited one exactly, and writes
# nothing into a file the operator owns.
#
# "Exactly" is the RAW digest, which is what `_classify` and therefore
# `ks init --upgrade-prompts` ask for: a copy with a newline appended is
# not a pristine scaffold to the path that overwrites bytes.
# `shipped_label` also offers a widened rule, and R10.9's operator-file
# loader is the caller that takes it, because its decision is whether to
# inject rather than whether to replace. The keyword at the call site is
# which rule a reader is asking for.
#
# MAINTENANCE. Each tuple is (sha256, label) oldest first, CURRENT LAST.
# When a template body changes, APPEND its new row; never edit or drop
# an old one, because an old row is the only record that can recognise a
# file already on someone's disk. ``label`` is the identifier that body
# shipped under: the ``*_VERSION`` constant where the template has one,
# otherwise the date it first appeared. Enforced by
# tests/test_prompt_versions.py and tests/test_prompt_staleness.py.


@dataclass(frozen=True)
class ScaffoldedTemplate:
    """A file `ks init` writes from a harness constant, plus the digest
    of every body that constant has ever had."""

    filename: str
    constant_name: str
    body: str
    history: tuple[tuple[str, str], ...]

    @property
    def current_label(self) -> str:
        """The identifier of the body this kstrl ships."""
        return self.history[-1][1]


# Harvested from this repository's full history by hashing the constant
# at every commit that changed it (including the pre-rename `ralph`
# era, whose bodies could still be sitting in a migrated checkout).
SCAFFOLDED_TEMPLATES: tuple[ScaffoldedTemplate, ...] = (
    ScaffoldedTemplate(
        filename="prompt.md",
        constant_name="DEFAULT_PROMPT",
        body=DEFAULT_PROMPT,
        history=(
            # No DEFAULT_PROMPT_VERSION constant existed yet; labelled by
            # the date the body first shipped.
            (
                "15810563f3843b6634f6207d052710d72aa4fda0aa32ac86aa7718de86d34140",
                "pre-1.0.0 (2026-01-15)",
            ),
            (
                "9eb6d8f4c956d6fcacf2f39eed4a696e2755ce2ae0e24f53d08e98227fc37fc3",
                "pre-1.0.0 (2026-01-28)",
            ),
            (
                "5ec3e510a0dbd6ff41b181259b707f33a715715648cee0607ae5db6cf9992046",
                "pre-1.0.0 (2026-05-27)",
            ),
            ("a4a3a090139c370d7eecd12e3ef98055352110722750bb7b4cbf9bc50b1b9125", "1.0.0"),
            ("aa7fa6acb045dc6105d1a4c4ce8b687e1e04289c7b751eb0373b7c59dca3f7ae", "1.1.0"),
            ("4f7370f5f4efb2d9b89ce6ae09fcbf7e5c3c8fb3db22cdeb07a9221ccbc638dc", "1.1.1"),
            # Never merged: an in-review draft of #276 revised before it
            # landed. Recorded because a checkout of that branch could
            # have scaffolded from it.
            ("9bde9b20785f3740396906d1d199c2228c553c11ae956dc2f85d8aa2439fb49b", "1.2.0"),
            ("392eb698daf71d486a9d4573698df3bb2b3ca4be87c178657accc8a66c54f384", "1.3.0"),
            ("f5349c9c2fb1ac1b9bfba54c2fde3cbc266f6a8a59deaf355707504273ddc124", "1.4.0"),
            ("a11b4209e38feac0361176f4897b357ed675cb0b2fd4545f648d83560ae81dd6", "1.5.0"),
        ),
    ),
    # The understand templates are H3-exempt (they produce documentation,
    # not adversarial-role output) so they carry no version constant, and
    # their labels are dates. They go stale the same way and the
    # mechanism is indifferent to which kind of label a row holds.
    ScaffoldedTemplate(
        filename="understand_prompt.md",
        constant_name="DEFAULT_UNDERSTAND_PROMPT",
        body=DEFAULT_UNDERSTAND_PROMPT,
        history=(
            ("5514376b0beeb484755d2d7d5effbe9a749b2d0972ddd30e7911e47bcf73e4ff", "2026-01-14"),
            ("1e700b55db8316392de146c549ef9fe9acf503af5c6ba2780f9d341728ac39c4", "2026-01-15"),
            ("fd02d9e3f2e559db5625c4db2d81ef0d24df481a4f4d4f5506fddd9b0962c53a", "2026-07-20"),
            ("cfd43bfeb80eaaf559ccb32d993fc2c5b2471ff90c7816648743135c2aa29688", "2026-07-21"),
        ),
    ),
    ScaffoldedTemplate(
        filename="feature_understand_prompt.md",
        constant_name="DEFAULT_FEATURE_UNDERSTAND_PROMPT",
        body=DEFAULT_FEATURE_UNDERSTAND_PROMPT,
        history=(
            ("5096447a6228e93d7d824ff5e1a334ef3eaf9edc9314a3fb7c6f7f04936cf06f", "2026-01-28"),
            ("e05fedd0ea1aff624966f4ee1e572c1af6f3926dd1b38b64678fdd6525a6f31a", "2026-07-20"),
            ("eb3637acf1918da23e27ad3f4d30bab32b1edd797b4bd1b5587b82b656affb09", "2026-07-21"),
        ),
    ),
    # R10.8. The first ledger row whose reader is the RUN and not just
    # the operator: load_operator_file suppresses a body listed here, so
    # a missing row costs an injected placeholder block rather than an
    # unshown staleness notice.
    ScaffoldedTemplate(
        filename="golden-patterns.md",
        constant_name="DEFAULT_GOLDEN_PATTERNS",
        body=DEFAULT_GOLDEN_PATTERNS,
        history=(
            # Never merged: the #229 body as it stood while the PR was in
            # review. Recorded because a checkout of that branch could
            # have scaffolded from it, and a copy on disk that kstrl does
            # not recognise is a copy it injects.
            ("b8e9cd9725308cfce280d05c26033d25cb16f51ec42af2ac4a2bb05c601e48cf", "2026-09-06a"),
            ("5f00b030f0a6e6cad4a56b678fa657ebce1a2d734e465ef82a8ca6df0638ca8a", "2026-09-06"),
            # Also never merged. Review round 2 (nit 15) measured the
            # word "byte-identical" being wrong in both directions: a
            # CRLF copy of this body IS recognised, because `read_text`
            # decodes before the digest, and a copy with one newline
            # appended is NOT. The body now says "unchanged".
            ("2dab640523bd4082e323a7cf6d13a9fae6e2a6a2886473f584cabf3b883a0300", "2026-09-07"),
            # R10.9 review round 1, should-fix 1. The preamble's two
            # lifecycle paragraphs were prompt text from the operator's
            # first edit onward: one of them ("nothing is injected") is
            # false at the point the engineer reads it, and the other two
            # sentences are imperatives addressed to the operator. 715
            # characters down to 313. Appended, never edited: an operator
            # whose disk holds the 715-character body must still be
            # recognised, or it starts being injected.
            ("103b4b2cf78aaf55163a9f10fd1c90193ba2fc05b16489687ac427b5c5d23c87", "2026-09-07b"),
        ),
    ),
    # R10.9. Same reader as the row above: a body listed here is
    # suppressed by load_operator_file, so a missing row costs an
    # injected placeholder block on every engineer prompt rather than an
    # unshown staleness notice.
    ScaffoldedTemplate(
        filename="memory.md",
        constant_name="DEFAULT_MEMORY",
        body=DEFAULT_MEMORY,
        history=(
            # Never merged outside this PR: the nine-line preamble as it
            # stood when review round 1 read it.
            ("8146096422efcb9b4196b76711fc44c80e2c7b5b920771f8a1eddcb4ba5a81c8", "2026-09-07"),
            # Round 1, should-fix 1: same defect as the row above. 354
            # characters down to 148, and "Nothing is injected while this
            # file is unchanged" is gone because the engineer only ever
            # reads it while it is being injected.
            ("07c55e3e12be359ea12cbe55f1c3296098a24c3468d6e62b66f42b0d2e1ffb83", "2026-09-07b"),
        ),
    ),
    # #199. `ks init` has always written this file; it never had a ledger
    # row, so nothing could tell an untouched scaffold from a real map.
    # The digests were re-derived by parsing the constant out of the
    # ancestor commits of main, not copied from anywhere. APPEND only.
    ScaffoldedTemplate(
        filename="codebase_map.md",
        constant_name="DEFAULT_CODEBASE_MAP",
        body=DEFAULT_CODEBASE_MAP,
        history=(
            ("48ede9d3d9ce78ebcc0e0b2b7e030cc3be3e58894627a875a4c29420b541126a", "2026-01-14"),
            ("382a66611762f9dba9098cad4720fdc4641e36e86b55865e56879aea7e7edea3", "2026-01-15"),
            ("664063e1929c7a50fef5e95e6d275c6502711ba2bcc249545f84bdc9a7160c51", "2026-07-20"),
        ),
    ),
)


def shipped_label(
    filename: str,
    text: str,
    *,
    ignore_trailing_newlines: bool = False,
) -> str | None:
    """The label of the shipped body ``text`` is, or None for anything else.

    ONE table, read by two callers that need two different answers, and
    the keyword is where the difference is written rather than in a
    second copy of the lookup. A second copy would be a second definition
    of "kstrl wrote this file", and the weaker one is the one whichever
    caller reaches it consults.

    Keyed on the file's own SHA-256, so it classifies files written long
    before this mechanism existed and writes nothing into a file the
    operator owns. Labels are unique per template
    (``test_history_rows_are_unique_and_non_empty``), so comparing the
    returned label against ``current_label`` is exactly comparing
    digests.

    THE DEFAULT IS THE RAW DIGEST, which is byte identity against a body
    kstrl shipped. :func:`_classify` takes it, and through
    ``_upgrade_scaffolded_templates`` that is the one path in `ks init`
    that REPLACES an operator's bytes: it may act only where nothing of
    theirs can be in the file, so a file that is a shipped body plus a
    trailing newline is EDITED for that path and is left alone. Review
    round 2 (should-fix 2) measured the alternative: such a file
    classified ``stale``, was overwritten 4446 bytes to 4468, and the
    operator was told "it held no local edits" about a file holding two
    newlines they may have typed.

    ``ignore_trailing_newlines=True`` widens it to a second digest, the
    text with its trailing newlines collapsed to one.
    ``operator_context.read_operator_file`` asks for that, because its
    decision is whether to INJECT a scaffold nobody has filled in: it
    renders ``text.rstrip("\\n")``, so under the raw rule
    ``DEFAULT_MEMORY + "\\n"`` injected a placeholder block whose body was
    byte-identical to the one the unedited file suppresses (round 1, nit
    3), and #231 makes a daemon the writer of that file. The asymmetry is
    the consequence of being wrong: injecting nothing is undone by
    editing the file, overwriting it is not.

    Raw first and widened second, so the keyword can only ADD matches. A
    historical body that ended in two newlines is still found by its own
    digest, which a normalise-then-look-up form would have lost, and the
    bodies for those rows no longer exist to re-derive.
    """
    template = next((t for t in SCAFFOLDED_TEMPLATES if t.filename == filename), None)
    if template is None:
        return None
    digests = dict(template.history)
    candidates = (text, text.rstrip("\n") + "\n") if ignore_trailing_newlines else (text,)
    for candidate in candidates:
        label = digests.get(hashlib.sha256(candidate.encode("utf-8")).hexdigest())
        if label is not None:
            return label
    return None


# absent        - no file there; run_loop falls back to the constant and
#                 says so itself.
# current       - byte-for-byte the body this kstrl ships.
# stale         - byte-for-byte an OLDER body this kstrl once shipped.
# unrecognised  - matches nothing kstrl has ever shipped, a copy with a
#                 trailing newline added included. Says nothing about who
#                 wrote it, which is the point: an edited file and a file
#                 from a build outside this history are indistinguishable,
#                 so neither is claimed to be stale, and neither is
#                 overwritten by `ks init --upgrade-prompts`.
TemplateStatus = Literal["absent", "current", "stale", "unrecognised"]


@dataclass(frozen=True)
class TemplateState:
    """What a scaffolded file on disk is, against the shipped history."""

    template: ScaffoldedTemplate
    path: Path
    status: TemplateStatus
    shipped_label: str | None = None


def _classify(template: ScaffoldedTemplate, path: Path) -> TemplateState:
    """Classify ``path`` as a copy of ``template``, on the RAW digest.

    Raw because of who reads the answer: ``_upgrade_scaffolded_templates``
    turns ``stale`` into an overwrite of the operator's file, and the
    argument that authorises it is that the bytes on disk are bytes kstrl
    itself wrote. A file that is a shipped body plus a trailing newline
    is therefore ``unrecognised`` here - reported, and left alone - even
    though ``operator_context`` treats the same file as an unedited
    scaffold and injects nothing from it. Two readers, two rules; see
    :func:`shipped_label`, which holds both.

    An unreadable file is ``unrecognised`` for the same reason: we cannot
    prove it is a body we shipped, so we make no claim about it (the same
    reason ``_gitignore_state`` leaves an unreadable .gitignore alone).

    The staleness notice and the wizard's preview read this too, and both
    inherit the raw rule. That is the conservative direction for them as
    well: neither says anything about a file it cannot prove kstrl wrote,
    where the widened rule would have had the notice call a file
    "unedited" that is not byte-identical to anything shipped.
    """
    if not path.exists():
        return TemplateState(template=template, path=path, status="absent")
    text = _read_text_or_none(path)
    if text is None:
        return TemplateState(template=template, path=path, status="unrecognised")
    label = shipped_label(template.filename, text)
    if label is None:
        return TemplateState(template=template, path=path, status="unrecognised")
    return TemplateState(
        template=template,
        path=path,
        status="current" if label == template.current_label else "stale",
        shipped_label=label,
    )


def classify_scaffolded_path(path: Path) -> TemplateState | None:
    """Classify ``path``, or None when kstrl does not scaffold that name.

    Matching on the basename rather than the full path is deliberate:
    ``--prompt`` and ``PROMPT_FILE`` can point anywhere, and a file the
    operator named something else is not a scaffold we can speak about.
    """
    template = next((t for t in SCAFFOLDED_TEMPLATES if t.filename == path.name), None)
    return None if template is None else _classify(template, path)


def classify_scaffold(root: Path) -> list[TemplateState]:
    """Classify every template `ks init` scaffolds under ``root``."""
    kstrl_dir = root / "scripts" / "kstrl"
    return [_classify(t, kstrl_dir / t.filename) for t in SCAFFOLDED_TEMPLATES]


def _rewrite_blockers(path: Path, root: Path | None = None) -> list[str]:
    """Every reason `ks init --upgrade-prompts` would not rewrite ``path``.

    The upgrade replaces bytes with ``os.replace``, which swaps the
    directory entry. Wherever that entry is shared, replacing it
    silently un-shares it: the project moves to the new body, the source
    every other project reads stays on the old one, and nobody is told.
    That is the outcome #286 exists to prevent, produced by the fix for
    it. Measured on this tree before the guard: a symlinked prompt.md
    became a regular file with the shared source untouched, and a
    hard-linked one had its inode replaced with the same result.

    Following the link instead of refusing is the other option, and is
    rejected on scope: `ks init <dir>` must not rewrite a file outside
    <dir>, and a shared prompt is by definition read by projects this
    invocation was not pointed at. Naming the target costs the operator
    one command and keeps the decision theirs.

    ``root``, when given, adds the check only a caller that knows the
    project can make: a symlinked ``scripts/kstrl`` directory puts the
    file outside <dir> with no link on the leaf at all. (`ks init` has
    always CREATED straight through such a link; this only refuses to
    REWRITE through it.) Reasons compose rather than shadowing each
    other, so an operator with two of them is told about both.
    """
    blockers: list[str] = []
    if path.is_symlink():
        blockers.append(f"a symlink to {path.resolve()}")
    else:
        try:
            links = path.stat().st_nlink
        except OSError:
            links = 1
        if links > 1:
            blockers.append(f"a hard link shared with {links - 1} other name(s)")
    if root is not None and root.resolve() not in path.resolve().parents:
        blockers.append(f"outside this project, at {path.resolve()}")
    if not path.parent.match("scripts/kstrl"):
        blockers.append("not the copy under scripts/kstrl/ that `ks init` upgrades")
    return blockers


def _upgrade_remedy(path: Path, root: Path | None = None) -> str:
    """The one sentence that says what to do about a stale ``path``.

    Shared by the run-time warning and by `ks init --upgrade-prompts`
    itself, so the advice an operator reads and the behaviour they then
    get cannot be worded into disagreement.
    """
    blockers = _rewrite_blockers(path, root)
    if not blockers:
        return "Run `ks init --upgrade-prompts` to take the current one."
    return (
        "`ks init --upgrade-prompts` will not touch this one: it is "
        + ", and ".join(blockers)
        + ". Update the file it shares, or replace this one by hand."
    )


class StalenessNotice(NamedTuple):
    """The two operator-facing lines about one stale scaffold."""

    headline: str
    advice: str


def staleness_notice(path: Path) -> StalenessNotice | None:
    """What to tell an operator about ``path``, or None to say nothing.

    None whenever there is nothing TRUE to say: the file is current, it
    is missing, it is not a template kstrl scaffolds, or it matches no
    body kstrl ever shipped (see ``unrecognised``).
    """
    state = classify_scaffolded_path(path)
    if state is None or state.status != "stale":
        return None
    # No count of how far behind: the ledger records bodies, not
    # releases (one row never reached main), so "N revisions behind" is
    # a number an operator cannot reconcile with the changelog. The two
    # labels are the actionable fact and both are exact.
    #
    # The remedy is only offered where it works, and _upgrade_remedy is
    # the single place that decides that, so this warning cannot promise
    # something `ks init --upgrade-prompts` then refuses to do.
    remedy = _upgrade_remedy(path)
    return StalenessNotice(
        headline=(
            f"{path} is the {state.template.filename} kstrl shipped at "
            f"{state.shipped_label}, unedited; this kstrl ships "
            f"{state.template.current_label}."
        ),
        advice=(
            "`ks init` never overwrites it, so every change to that "
            f"template since {state.shipped_label} has missed this "
            f"project. {remedy}"
        ),
    )


# First line of the .gitignore block `ks init` writes. Its presence is
# the whole idempotency test: an existing .gitignore is a user-owned
# file, so the block is appended once and never rewritten.
GITIGNORE_BLOCK_MARKER = "# kstrl: artifacts the scope guard would otherwise count"

# The marker plus the reason, so the file explains itself to whoever
# reads it next.
_GITIGNORE_BLOCK_HEADER = f"""{GITIGNORE_BLOCK_MARKER}
# The in-loop scope guard counts UNTRACKED files against a component's
# allowed paths, so anything your test / typecheck / lint commands write
# has to be ignored here or committed - otherwise the agent's own build
# artifacts read as out-of-scope edits and cost an iteration.
"""

# Ignored everywhere: kstrl's own runtime state, plus the one OS artifact
# that lands in a working tree without anybody asking for it.
#
# `.kstrl/` stays here after #274 carved the state directory out at the
# scope guard. The two answer different questions and neither subsumes
# the other: the guard only decides whether a file counts as a scope
# violation, while this line is what keeps `git add -A` from committing
# kstrl's run journals into the project's history and thence into a PR.
# Delete it and a --no-worktrees run starts shipping its own state.
_COMMON_IGNORES = (
    ".kstrl/",
    ".DS_Store",
)

# The "Next steps" block. The spec path leads because it is the one the
# README sells and the one the scaffold cannot suggest on its own: init
# writes an empty userStories array, which reads as "write these by
# hand" (#256). Not named *_PROMPT: that suffix enrols a constant in the
# adversarial prompt version snapshot, and this is UI copy.
NEXT_STEPS = """You have a spec (recommended):
  ks decompose --spec <spec.md> --project-name <name>  # plan it
  ks factory --spec <spec.md> --project-name <name>    # plan and build it

You want to drive one component by hand:
  1. Edit scripts/kstrl/prompt.md
  2. Add user stories to scripts/kstrl/prd.json
  3. ks run [iterations]                               # one component, no PR

Other modes:
  ks understand [iterations]
  ks feature [iterations] --prd scripts/kstrl/feature/<name>/prd.json

Measure before you spend (no agent, no cost):
  ks check
  ks check --allowed-path '<glob>'                     # preflight the guard
"""


def run_init(directory: Path, ui: UI, *, upgrade_prompts: bool = False) -> int:
    """Initialize kstrl harness in a project directory.

    Args:
        directory: Target project directory
        ui: UI for output
        upgrade_prompts: Rewrite any scaffolded prompt template that is a
            pristine older harness body (#286). Off by default, because
            `ks init` is otherwise non-destructive by design; this is the
            operator's explicit opt-in to that one overwrite.

    Returns:
        Exit code (0=success, 1=validation failure, 2=directory not found)
    """
    ui.title("kstrl Init")

    # Validate directory
    ui.section("Target")
    if not directory.exists():
        ui.err(f"Directory not found: {directory}")
        return 2

    root = directory.resolve()
    ui.kv("Directory", str(root))

    # Check for git repo
    is_repo = git.is_git_repo(root)
    if is_repo:
        ui.ok("Git repository detected")
    else:
        ui.warn("Not a git repository")

    ui.section("Scaffold")
    kstrl_dir = root / "scripts" / "kstrl"
    if not kstrl_dir.exists():
        kstrl_dir.mkdir(parents=True, exist_ok=True)
        ui.ok("Created scripts/kstrl/")
    else:
        ui.ok("scripts/kstrl/ exists")

    if upgrade_prompts:
        ui.section("Upgrade prompt templates")
        _upgrade_scaffolded_templates(root, ui)

    ui.section("Create defaults")
    _create_if_missing(root / "kstrl.toml", DEFAULT_KSTRL_TOML, ui)
    _create_if_missing(kstrl_dir / "prompt.md", DEFAULT_PROMPT, ui)
    _create_if_missing(kstrl_dir / "prd.json", json.dumps(DEFAULT_PRD, indent=2) + "\n", ui)
    _create_if_missing(kstrl_dir / "progress.txt", DEFAULT_PROGRESS, ui)
    _create_if_missing(kstrl_dir / "codebase_map.md", DEFAULT_CODEBASE_MAP, ui)
    _create_if_missing(kstrl_dir / "golden-patterns.md", DEFAULT_GOLDEN_PATTERNS, ui)
    _create_if_missing(kstrl_dir / "memory.md", DEFAULT_MEMORY, ui)
    _create_if_missing(kstrl_dir / "understand_prompt.md", DEFAULT_UNDERSTAND_PROMPT, ui)
    _create_if_missing(
        kstrl_dir / "feature_understand_prompt.md",
        DEFAULT_FEATURE_UNDERSTAND_PROMPT,
        ui,
    )

    # Bootstrap CLAUDE.md and AGENTS.md
    bootstrap_claude_md(root, ui)

    # Keep kstrl's own state out of the scope guard and out of commits (#201)
    ui.section("Git hygiene")
    _ensure_gitignore(root, ui)

    # Validate PRD
    ui.section("Validate PRD")
    prd_file = kstrl_dir / "prd.json"

    try:
        with open(prd_file, encoding="utf-8") as f:
            data = read_json_file(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        ui.err(f"Invalid JSON in prd.json: {e}")
        return 1

    errors = PRD.validate_schema(data)
    if errors:
        ui.err("PRD schema validation failed:")
        for error in errors:
            ui.info(f"  - {error}")
        return 1

    ui.ok("PRD schema valid")

    # PRD summary
    ui.section("PRD summary")
    prd = PRD.load(prd_file)
    ui.kv("Branch", prd.branch_name)
    ui.kv("Stories", str(len(prd.user_stories)))

    passing = sum(1 for s in prd.user_stories if s.passes)
    failing = len(prd.user_stories) - passing
    if prd.user_stories:
        ui.kv("Passing", str(passing))
        ui.kv("Failing", str(failing))

    # Next steps
    ui.section("Next steps")
    for line in NEXT_STEPS.splitlines():
        ui.info(line)

    return 0


def _create_if_missing(path: Path, content: str, ui: UI) -> None:
    """Create file if it doesn't exist.

    "already exists" was the whole report before #286, and it is the
    line an operator re-running `ks init` reads to decide whether their
    scaffold is in good shape. When the file is an OLDER harness
    template it now says so here too, because that is the moment the
    question is being asked.
    """
    if path.exists():
        ui.info(f"  {path.name} already exists")
        notice = staleness_notice(path)
        if notice is not None:
            ui.warn(f"  {notice.headline}")
            ui.info(f"  {notice.advice}")
    else:
        # utf-8 pinned to match _read_text_or_none: what init writes has
        # to read back byte-identical for the #286 digest to mean
        # anything, on any locale.
        path.write_text(content, encoding="utf-8")
        ui.ok(f"  Created {path.name}")


def _atomic_replace(target: Path, content: str) -> None:
    """Replace an EXISTING file's bytes atomically, keeping its mode.

    The write is ``atomicio.atomic_write_text``, which since #291 is the
    one owner of the mode and encoding rules this used to spell out for
    itself. ``atomicio`` is a stdlib-only leaf, so importing it here does
    NOT put ``kstrl.workqueue`` in ``kstrl.loop``'s static import
    closure via loop's deferred ``init_cmd`` import, which is what #274's
    ``tests/test_state_dir_scope.py`` refuses and what kept this copy
    hand-rolled until now.

    REPLACE, not create, is the part that stays here, because it is this
    caller's contract rather than the writer's, and a ``must_exist=``
    flag on the shared writer would be a special case carried by nine
    callers that do not want it. The one caller only ever rewrites a file
    the ledger already classified, so a missing target is a bug rather
    than a case to handle: the run stops instead of quietly doing a
    non-atomic create under a name that promises otherwise, which is what
    ``atomic_write_text`` on its own would do.
    """
    if not target.is_file():
        raise FileNotFoundError(
            f"_atomic_replace expects an existing file, got {target}: this "
            f"function only rewrites templates the scaffold ledger already "
            f"classified, so a missing target means the caller is wrong"
        )
    atomic_write_text(target, content)


def _upgrade_scaffolded_templates(root: Path, ui: UI) -> None:
    """Rewrite pristine older scaffolds with the body kstrl ships now.

    Safe to overwrite precisely because the RAW digest proved the file is
    byte-identical to a template kstrl itself wrote: there is nothing of
    the operator's in it to lose, not even a trailing newline. That is
    ``_classify``'s rule and not the loader's, which is widened; review
    round 2 (should-fix 2) is why the two are written apart, because for
    one release the widened rule reached this path and the sentence above
    was false about the file it authorised replacing. Anything the
    history does not recognise is left alone and reported, never merged
    or guessed at.

    "Nothing of the operator's to lose" is about the BYTES. The
    directory entry is a second thing they own, so a prompt they share
    by link, or one that resolves outside this project, is reported and
    left alone: see ``_rewrite_blockers``.

    The write is atomic (``_atomic_replace``) rather than a plain
    ``Path.write_text``: this is the one path in `ks init` that REPLACES
    bytes rather than creating a file, and an interruption mid-write
    would truncate the very prompt this feature exists to protect, into
    a body no ledger row recognises and so nothing will offer to fix.
    """
    for state in classify_scaffold(root):
        name = state.path.name
        if state.status == "stale":
            blockers = _rewrite_blockers(state.path, root)
            if blockers:
                ui.warn(f"  {name} is {blockers[0]}; left alone")
                ui.info(f"    {_upgrade_remedy(state.path, root)}")
                continue
            _atomic_replace(state.path, state.template.body)
            ui.ok(
                f"  {name}: {state.shipped_label} -> "
                f"{state.template.current_label} (it held no local edits)"
            )
        elif state.status == "current":
            ui.info(f"  {name} is already at {state.template.current_label}")
        elif state.status == "unrecognised":
            ui.warn(f"  {name} matches no template kstrl has shipped; left alone")
            ui.info(
                "    Either you edited it or it came from a build outside "
                "this history, and nothing here can tell those apart. "
                f"Diff it against {state.template.constant_name} in "
                "kstrl/init_cmd.py yourself."
            )
        else:  # absent: the scaffold pass below writes it
            ui.info(f"  {name} is missing; the scaffold below creates it")


def gitignore_block() -> str:
    """The .gitignore block `ks init` writes, the same on every tree (#696)."""
    return _GITIGNORE_BLOCK_HEADER + "\n".join(_COMMON_IGNORES) + "\n"


def _read_text_or_none(path: Path) -> str | None:
    """``path``'s text, or None when it cannot be read as text.

    Catches ValueError alongside OSError because UnicodeDecodeError is a
    ValueError: a .gitignore that is not UTF-8 crashed `ks init` and
    the TUI scaffold preview with a traceback (#201 review). A
    directory in the file's place raises IsADirectoryError, an OSError.

    UTF-8 is pinned rather than left to the locale because #286's
    classification is byte equality against a template `ks init` wrote:
    if the read and the write can disagree about the encoding, then on a
    non-UTF-8 locale a template carrying one non-ASCII character would
    scaffold and immediately classify as ``unrecognised``, and so would
    never be reported or upgraded. Every shipped body is ASCII today,
    which is exactly why this is cheap to pin now. It also makes the
    .gitignore read deterministic across machines; a file that is not
    UTF-8 still degrades to None and is still left alone, as before.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _gitignore_state(root: Path) -> tuple[ScaffoldAction, str | None]:
    """What init would do to ``root``/.gitignore, and the text it read.

    One decision with two consumers - the write below and the TUI
    wizard's preview - so the preview cannot describe a write that would
    not happen. "keep" covers both "the block is already there" and
    "the file cannot be read as text", because init leaves both alone;
    the second is the one where the text comes back None.
    """
    path = root / ".gitignore"
    if not path.exists():
        return "create", None
    existing = _read_text_or_none(path)
    if existing is None or GITIGNORE_BLOCK_MARKER in existing:
        return "keep", existing
    return "append", existing


def gitignore_plan(root: Path) -> ScaffoldAction:
    """The scaffold preview's half of :func:`_gitignore_state`."""
    return _gitignore_state(root)[0]


def _ensure_gitignore(root: Path, ui: UI) -> None:
    """Create .gitignore, or append the kstrl block to an existing one.

    An existing .gitignore is a user-owned file: this only ever APPENDS,
    inside a marked block, and skips entirely once the marker is present,
    so re-running `ks init` cannot duplicate it or rewrite a line the
    user wrote. A file it cannot read is left alone for the same reason:
    without the marker check, appending could duplicate the block.

    The append goes through ``appendio``, which is why this function
    reads the file once for the marker and never again to work out its
    terminator; the comment on the call has the window that closes.
    """
    path = root / ".gitignore"
    action, existing = _gitignore_state(root)

    if action == "create":
        _create_if_missing(path, gitignore_block(), ui)
        return

    if existing is None:
        ui.warn("  .gitignore could not be read as text; leaving it alone")
        ui.info("    Add the usual build-artifact rules yourself, or the scope")
        ui.info("    guard counts what your test and lint commands write.")
        return

    if action == "keep":
        ui.info("  .gitignore already has the kstrl block")
        return

    # One blank line between the user's last rule and ours, and no
    # leading blank when the file is empty. The TERMINATOR is not
    # decided here any more: ``append_records`` probes the handle it is
    # about to write through and pads an unterminated tail itself, so
    # what is left here is the blank line, which is a Markdown-style
    # separation choice and not a repair.
    #
    # That closes a read-then-write window rather than tidying a
    # branch. ``_gitignore_state`` reads the file, this appends to it,
    # and the two reads used to disagree whenever the file changed in
    # between: measured, a file reported empty by the read and holding
    # an unterminated rule by the time of the write got the kstrl block
    # header appended onto the user's last rule, on one line.
    #
    # utf-8 is pinned inside the helper and matches
    # ``_read_text_or_none``, which is what reads this file back to
    # decide whether the block is already there. The block is ASCII, so
    # this changes no byte today; what it removes is a write and a read
    # of the same file that could disagree (#320).
    separator = "" if not existing else "\n"
    append_records(path, separator + gitignore_block(), repair="")
    ui.ok("  Appended the kstrl block to .gitignore")


#: #434: what `ks doctor` and the `ks decompose` / `ks factory --spec`
#: preflight say about a repository with no build manifest. One sentence
#: for the finding and one for the fix, so the surfaces cannot word it two
#: ways.
BUILD_MANIFEST_MISSING = (
    "no build manifest at the repository root that kstrl recognises, and kstrl "
    "will not create one: no component may list a root build manifest in its "
    "allowedPaths, so `ks decompose` would pay for an architect call that can only "
    "halt and ask who writes it"
)
BUILD_MANIFEST_FIX = (
    "kstrl will not create the build manifest, so create and commit the manifest "
    "your project's own build tool writes before `ks decompose`. If the project "
    "builds with a tool kstrl does not recognise, write a [stack] in kstrl.toml instead."
)


def build_manifest_blocker(root: Path) -> str | None:
    """Why kstrl cannot plan work in ``root`` yet, or None when it can (#434).

    A build manifest is one of ``toolchains.BUILD_MANIFESTS``, which is
    ``decompose.ROOT_BUILD_MANIFESTS`` (#627): every manifest no component
    may be scoped to is one this recognises, so a repository holding it is
    not refused.

    A repository holding none of them is still let through when kstrl.toml
    holds a ``[stack]`` (#696 slice 4): the operator has told kstrl how the
    project builds, which is the answer for a build tool kstrl does not name
    (a Gemfile, a Makefile). A kstrl.toml that does not load raises the
    ``OSError`` or ``ValueError`` of ``stack.load_stack``, and the caller
    decides what that means.
    """
    if has_build_manifest(root):
        return None
    if load_stack(root) is not None:
        return None
    return BUILD_MANIFEST_MISSING


def build_manifest_ok_reason(root: Path) -> str:
    """Which of #434's two conditions let ``root`` through, for
    `doctor.check_build_manifest`'s ``[ok]`` detail.

    Only meaningful after `build_manifest_blocker(root)` has already
    returned ``None``: it repeats the same two reads rather than
    threading a reason back through that function's ``str | None``
    return, which every other caller only tests for truthiness. The
    two reasons were folded into one sentence before #434 B1
    ("a build manifest ... is at the root, or [verify] names ..."),
    which is how an escape that itself needed the missing manifest read
    as `[ok]` without saying which half applied.
    """
    if has_build_manifest(root):
        return (
            "a build manifest kstrl recognises is at the repository root, so the "
            "`ks decompose` preflight lets the architect run"
        )
    return (
        "no build manifest kstrl recognises is at the repository root, but kstrl.toml "
        "has a [stack] saying how the project builds, so the `ks decompose` preflight "
        "lets the architect run"
    )


# ---------------------------------------------------------------------------
# CLAUDE.md and AGENTS.md bootstrap
# ---------------------------------------------------------------------------


#: H3 (#303): fragments _generate_claude_md assembles; versioned as one body
#: (docs/adversarial-roadmap.md, H3a sweep row). 2.0.0 (#696 slice 6): the
#: CLAUDE.md is the same on every tree. The detected language, the
#: per-language standards and antipatterns and the principles section are
#: gone; how the project builds is its confirmed [stack].
CLAUDE_MD_PROMPT_VERSION = "2.0.0"

CLAUDE_MD_OVERVIEW_PROMPT = "# CLAUDE.md - {name}\n\n## Project Overview\n- **Project**: {name}\n"

# What the generated CLAUDE.md says about verification, and why it names
# no commands. CLAUDE.md is prepended verbatim into the engineer prompt
# (loop.build_project_context), so anything written here is an
# instruction the agent follows; deriving the right commands would still
# be a second copy, and two copies drift. See the #261 note in verify.py.
CLAUDE_MD_VERIFICATION_PROMPT = """
## Verification

kstrl runs the checks of this project's `[stack]` in `kstrl.toml` on
every change and injects them into the engineer prompt, so they are
deliberately not restated here and cannot drift out of step with the
gate that runs them. A `[stack]` runs nothing until a person confirms it.
"""

CLAUDE_MD_LEARNINGS_PROMPT = """## Agent Learnings

> This section is maintained by AI agents working on this codebase.
> Agents: append patterns, gotchas, and conventions you discover below.
> This is the single source of truth - AGENTS.md is a symlink to this file.

### Codebase Patterns
<!-- Agents: add reusable patterns you discover here -->

### Gotchas
<!-- Agents: add surprises and non-obvious behaviors here -->

### Conventions
<!-- Agents: add established conventions here -->"""


def _generate_claude_md(name: str) -> str:
    """The CLAUDE.md `ks init` writes for the project called ``name``."""
    sections = [
        CLAUDE_MD_OVERVIEW_PROMPT.format(name=name),
        CLAUDE_MD_VERIFICATION_PROMPT.strip(),
        "",
        CLAUDE_MD_LEARNINGS_PROMPT,
        "",
    ]
    return "\n".join(sections) + "\n"


def bootstrap_claude_md(root: Path, ui: UI) -> None:
    """Generate CLAUDE.md and symlink AGENTS.md to it.

    The project's name is its directory's name: kstrl reads no manifest.

    AGENTS.md is a symlink to CLAUDE.md so both names point to the same
    file. When the prompt tells agents to "update AGENTS.md", they are
    writing to CLAUDE.md.
    """

    ui.section("Agent context files")

    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        ui.info("  CLAUDE.md already exists")
    else:
        claude_md.write_text(_generate_claude_md(root.name))
        ui.ok("  Created CLAUDE.md")

    agents_md = root / "AGENTS.md"
    if agents_md.is_symlink() and os.readlink(str(agents_md)) == "CLAUDE.md":
        ui.info("  AGENTS.md already symlinked to CLAUDE.md")
    elif agents_md.exists():
        ui.info("  AGENTS.md already exists (not a symlink)")
    else:
        # Create relative symlink: AGENTS.md -> CLAUDE.md
        agents_md.symlink_to("CLAUDE.md")
        ui.ok("  Created AGENTS.md -> CLAUDE.md (symlink)")
