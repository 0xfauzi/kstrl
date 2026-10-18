"""The bodies that `ks init` and `ks feature` write, with their version constants."""

from __future__ import annotations

from kstrl.project.operator_context import GUIDANCE_HEADING

DEFAULT_PROMPT_VERSION = "1.6.0"

# v1.6.0 (#700 slice 10a, owner decision 13): the section "An acceptance
# check you think is incorrect" gives the engineer one line in its progress
# entry, `Dispute: <check-id>: <cause>`, to dispute a visible acceptance
# check instead of changing code only to make it pass.
# `kstrl.acceptance.acceptance_record.read_dispute` reads that line on each head; a
# dispute it accepts halts the component for a person, and it never
# passes a check. No calibration fixture scores the engineer (H2 does not
# apply); H3 is this bump, the snapshot and the SCAFFOLDED_TEMPLATES row.

# v1.5.0 (#696 slice 4): step 9 names the `Stack` block's checks. The
# `Verification Commands (resolved by kstrl)` block it named is gone with
# the commands kstrl used to choose: a confirmed [stack] is the only source
# of the commands kstrl runs, and `iteration_prompt.build_project_context` renders it.

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

## An acceptance check you think is incorrect

A retry can name an acceptance check that did not pass, with its command and
what it printed. If you are sure that such a check is incorrect, or that no
correct implementation can pass it, do not change code only to make it pass.
Write this line in this iteration's entry in `$progress_path`, one line for
each such check:

Dispute: <check-id>: <cause>

kstrl then stops the component and shows the check, its output and your
cause to a person. A dispute never passes a check. kstrl rejects a line in
another form, a dispute of a held-out check (a retry names one by its id
only) and a dispute of a check that passed, and the check result then
stands. Do not dispute a check only because it is difficult to pass.

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

# 1.0.0 (#654 slice 8): opens the series on the body ledgered as 2026-07-21.
DEFAULT_UNDERSTAND_PROMPT_VERSION = "1.0.0"

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

# 1.0.0 (#654 slice 8): opens the series on the body ledgered as 2026-07-21.
DEFAULT_FEATURE_UNDERSTAND_PROMPT_VERSION = "1.0.0"

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
