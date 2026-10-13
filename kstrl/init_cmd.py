"""Init command for kstrl - initialize harness in a project."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kstrl.git import git
from kstrl.io import append_records, read_json_file
from kstrl.plan import PRD
from kstrl.scaffold_ledger import (
    _read_text_or_none,
    _upgrade_scaffolded_templates,
    staleness_notice,
)
from kstrl.scaffold_templates import (
    DEFAULT_CODEBASE_MAP,
    DEFAULT_FEATURE_UNDERSTAND_PROMPT,
    DEFAULT_GOLDEN_PATTERNS,
    DEFAULT_MEMORY,
    DEFAULT_PROMPT,
    DEFAULT_UNDERSTAND_PROMPT,
)

if TYPE_CHECKING:
    from kstrl.ui import UI

# Default file contents
DEFAULT_PRD = {
    "branchName": "kstrl/feature",
    "userStories": [],
}


DEFAULT_PROGRESS = """# kstrl Progress Log

## Codebase Patterns
- (add reusable patterns here)

## Iteration Notes
- (append entries below using the format in prompt.md)

---
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
# the git diff, never agent self-report. Opt-in; when
# enabled a violation blocks the merge, and editing enforcement machinery
# (this file, CI workflows, or the verifier code itself) is a
# non-overridable halt.
[policy]
# enabled = false
# paths_deny = [".github/workflows/**", "kstrl.toml", ".kstrl/**", "**/*.pem", "**/.env*"]
# max_files_changed = 40
# max_lines_changed = 1500
# secret_patterns = ["AKIA[0-9A-Z]{16}", "-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"]
# enforcement_paths_extra = []     # ADDS to the halt set; can never shrink it
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

# The engineer runs in the sandbox of its own harness (claude-code,
# claude-sdk or codex), which denies writes outside its worktree. Outbound
# network is open, so the engineer can add a package. A custom agent
# command, or enabled set to false, runs with no sandbox, and the run warns
# naming the role.
[sandbox]
# enabled = true
# allow_network = true             # false denies outbound network inside the sandbox

# Phase 2.5 security review (independent adversarial pass focused on vulns).
[security]
# mode = "advisory"               # skip | advisory | hard (advisory = default; skip turns the review off)
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

# What `ks init` does to a scaffolded file.
ScaffoldAction = Literal["create", "keep", "append"]


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


def gitignore_block() -> str:
    """The .gitignore block `ks init` writes, the same on every tree (#696)."""
    return _GITIGNORE_BLOCK_HEADER + "\n".join(_COMMON_IGNORES) + "\n"


def _gitignore_state(root: Path) -> tuple[ScaffoldAction, str | None]:
    """What init would do to ``root``/.gitignore, and the text it read.

    One decision for the write below. "keep" covers both "the block is already there" and
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
# (iteration_prompt.build_project_context), so anything written here is an
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
