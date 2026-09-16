"""`ks doctor`: is this repository ready to point kstrl at? (#198)

Tier A only. Every check is static and mechanical: nothing here runs
the repository's own test, typecheck or lint commands, spawns an
agent, or spends anything. Measured cost of the whole set: 0.392 s
on this repository and 0.437 s on deckgen, under load average 5.6 to
6.3. `ks doctor --measure` (Tier B) is not built; `ks sense` already
runs the measurement it would wrap.

The anti-chimera rule from the issue: doctor checks ONLY what kstrl
consumes, and every check's ``detail`` names the kstrl component
that consumes the signal. A check whose signal nothing in kstrl
reads does not belong here, however useful it would be in a general
repository linter.

Order matters in one place: the checks run BEFORE the report is
written, so the report file this command creates under ``.kstrl/``
is never what the next run's clean-tree check trips over.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kstrl import git, pr
from kstrl.adequacy import is_test_path
from kstrl.atomicio import atomic_write_json
from kstrl.config import resolve_config_file
from kstrl.config_preflight import collect_config_problems
from kstrl.feedforward import (
    _MAX_PUBLIC_INTERFACE_FILES,
    _find_top_source_dirs,
    extract_public_interfaces,
)
from kstrl.policy import ENFORCEMENT_MACHINERY_PATHS, PolicyConfig, _match_glob
from kstrl.statedir import STATE_DIR_NAME, state_dir
from kstrl.verify import VerifyConfig, resolve_verify_commands

#: Version of the `ks doctor --json` document. Its own number, not
#: `SENSE_SCHEMA_VERSION`: the two documents answer different
#: questions and a reader of one must not infer the other's shape.
DOCTOR_SCHEMA_VERSION = 1

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

VERDICT_READY = "ready"
VERDICT_READY_WITH_WARNINGS = "ready-with-warnings"
VERDICT_NOT_READY = "not-ready"

#: Exit code for every refusal this command makes: a not-ready
#: verdict, an unusable --root, and --measure. 2 is what `ks sense`
#: and `ks serve` already document for "cannot run".
EXIT_REFUSED = 2

#: Subdirectory of the state directory the report lands in. Declared
#: in `statedir.STATE_SUBDIRS` as well, which is what
#: `tests/test_state_dir_scope.py` checks this against.
DOCTOR_DIR_NAME = "doctor"

_STAMP_FORMAT = "%Y%m%d-%H%M%S"

#: How long one git subprocess this module runs may take.
_GIT_TIMEOUT = 30.0

#: What `--measure` says instead of measuring. Tier B is not built;
#: the measurement it would wrap already ships as `ks sense` (#222).
MEASURE_NOT_BUILT = (
    "ks doctor --measure (Tier B) is not built. The measurement it would "
    "run already ships as `ks sense`, which runs the mechanical sensors "
    "against a tree with no PRD, branch, worktree or agent spend: try "
    "`ks sense --root <path> --json`. Tier B adds a flakiness smoke and a "
    "cost projection on top of that and is tracked on issue #198."
)

#: What a green verdict does NOT mean. Printed in every report and
#: mirrored in docs/runbook.md; the issue makes both a condition of
#: being done.
FIT_BOUNDARIES: tuple[str, ...] = (
    "A green verdict is repo-readiness, not spec-readiness. Nothing here "
    "can tell you whether the work you are about to describe fits the "
    "component model.",
    "kstrl is not for cross-cutting refactors. The factory decomposes a "
    "spec into components that each merge on their own, and a change that "
    "has to land everywhere at once has no such decomposition.",
    "kstrl is not for spec-free exploration. Every iteration is graded "
    "against a PRD, so work whose acceptance criteria are not known yet "
    "has nothing to grade.",
    "Tier A reads the repository and runs none of your commands, so it "
    "cannot tell you whether your suite is green, fast or flaky. Run "
    "`ks sense` for that.",
)

#: Paths worth protecting that kstrl does not protect by default.
#: A heuristic list, and said to be one in the check's detail: what
#: is NOT heuristic is the coverage test, which asks the policy
#: gate's own matcher.
PROTECTED_PATH_CANDIDATES: tuple[str, ...] = (
    ".github/workflows",
    ".gitlab-ci.yml",
    ".circleci",
    "migrations",
    "db/migrate",
    "alembic",
    "Dockerfile",
    "docker-compose.yml",
    "terraform",
    "deploy",
    "infra",
)


@dataclass(frozen=True)
class DoctorCheck:
    """One Tier A answer: what was measured, and what to do about it."""

    name: str
    status: str
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


def check_git_repo(root: Path) -> DoctorCheck:
    """A repository with commits and a base branch kstrl can reach.

    Consumed by `factory`, which cuts one git worktree per component
    off the base branch, and by every Phase 1 check that reads
    `git diff <base>...HEAD`.
    """
    name = "git_repo"
    if not git.is_git_repo(root):
        return DoctorCheck(
            name,
            STATUS_FAIL,
            f"{root} is not inside a git repository; factory cuts a "
            f"worktree per component and Phase 1 diffs against a base "
            f"branch, so neither can run",
            "Run `git init` and make one commit, or point --root at a checkout.",
        )
    head = git.get_head_sha(root)
    if head is None:
        return DoctorCheck(
            name,
            STATUS_FAIL,
            "the repository has no commits, so there is no base ref for "
            "factory to cut a component worktree from",
            "Make one commit.",
        )
    base = git.detect_base_branch(root)
    try:
        git.get_diff_names(base, root, strict=True)
    except git.GitDiffError as exc:
        return DoctorCheck(
            name,
            STATUS_WARN,
            f"git.detect_base_branch answered {base!r} and git cannot "
            f"measure a diff against it ({exc}); the diff-scope, "
            f"bad-patterns, policy and adequacy checks all read that diff",
            f"Create or fetch {base}, or name the long-lived branch with "
            f"`ks sense --base` and `ks run --base-branch`.",
        )
    return DoctorCheck(
        name,
        STATUS_OK,
        f"git repository at HEAD {head[:12]}, base branch {base} "
        f"(git.detect_base_branch, what factory cuts worktrees from)",
    )


def check_git_clean(root: Path) -> DoctorCheck:
    """Uncommitted work does not reach the engineer.

    Consumed by `factory` (each component worktree starts from the
    base ref) and by `git.capture_workspace_baseline`, which has to
    subtract pre-existing dirt from the in-loop scope guard.
    """
    name = "git_clean"
    if not git.is_git_repo(root):
        return DoctorCheck(
            name,
            STATUS_WARN,
            "not a git repository, so the working tree could not be read",
            "Fix git_repo first.",
        )
    changed = sorted(git.get_changed_files(root))
    if changed:
        listed = ", ".join(changed[:5])
        return DoctorCheck(
            name,
            STATUS_WARN,
            f"{len(changed)} uncommitted or untracked file(s) ({listed}); "
            f"factory cuts each component worktree from the base ref, so "
            f"none of this reaches the engineer, and "
            f"git.capture_workspace_baseline has to subtract it from the "
            f"in-loop scope guard",
            "Commit or stash before starting a run.",
        )
    return DoctorCheck(
        name,
        STATUS_OK,
        "clean tree; every component worktree starts from the base ref "
        "with nothing for the scope guard to subtract",
    )


def check_github_cli(root: Path) -> DoctorCheck:
    """Pushing a branch and opening a PR.

    Consumed by `pr.push_create_and_merge_pr`, which shells out to
    `gh`. Degraded mode, never a failure: the factory runs with
    `[factory] create_prs = false` and the operator merges by hand.
    """
    name = "github_cli"
    slug = git.get_origin_slug(root)
    available = pr.is_gh_available()
    if available and slug:
        return DoctorCheck(
            name,
            STATUS_OK,
            f"gh is authenticated and origin is {slug}, so "
            f"pr.push_create_and_merge_pr can push and open PRs",
        )
    missing = []
    if not available:
        missing.append("`gh` is not on PATH or `gh auth status` exited non-zero")
    if not slug:
        missing.append("there is no `origin` remote")
    return DoctorCheck(
        name,
        STATUS_WARN,
        "degraded mode: " + "; ".join(missing) + "; kstrl.pr can push no branch and open no PR",
        "Run `gh auth login` and add an origin remote, or set "
        "[factory] create_prs = false and merge by hand.",
    )


def check_kstrl_config(root: Path) -> DoctorCheck:
    """Every configuration section resolves.

    Consumed by `config_preflight.preflight_config`, the same check
    every other command runs before it starts anything. This command
    is exempt from that seam and runs the check here instead, so a
    broken file is a reported finding rather than a refusal.
    """
    name = "kstrl_config"
    path = resolve_config_file(root)
    warnings: list[str] = []
    try:
        problems = collect_config_problems(root, warnings.append)
    except (OSError, ValueError) as exc:
        # The same breadth `ks sense` uses at cli.py's own call of the
        # preflight: ConfigError is a ValueError, and that is what a
        # document which will not parse arrives as.
        return DoctorCheck(
            name,
            STATUS_FAIL,
            f"{path} cannot be used: {exc}",
            "Fix kstrl.toml; `ks config show` prints every row it can "
            "still resolve beside the rejected sections.",
        )
    if problems:
        return DoctorCheck(
            name,
            STATUS_FAIL,
            f"{len(problems)} unusable section(s) in {path}: " + "; ".join(problems),
            "Fix the named sections; every other kstrl command refuses while they stand.",
        )
    if not path.exists():
        return DoctorCheck(
            name,
            STATUS_OK,
            f"no kstrl.toml at {path}; every section uses its built-in "
            f"default, which is a supported configuration",
        )
    if warnings:
        return DoctorCheck(
            name,
            STATUS_WARN,
            "; ".join(warnings),
            "Fix the degrading sections; they warn and continue today.",
        )
    return DoctorCheck(
        name,
        STATUS_OK,
        f"{path} resolves in full (config_preflight.collect_config_problems)",
    )


def check_verify_commands(root: Path) -> DoctorCheck:
    """The three commands Phase 1 will run.

    Consumed by `verify.resolve_verify_commands`. Tier A resolves
    them and prints them; it does not run them, so it cannot say
    whether they pass.
    """
    name = "verify_commands"
    try:
        config = VerifyConfig.load(root)
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            name,
            STATUS_FAIL,
            f"[verify] could not be loaded ({exc}), so Phase 1 cannot resolve the commands it runs",
            "Fix the [verify] section of kstrl.toml.",
        )
    commands = resolve_verify_commands(config, root)
    stated = f"test `{commands.test}`, typecheck `{commands.typecheck}`, lint `{commands.lint}`"
    unset = [
        key
        for key, value in (
            ("test_command", config.test_command),
            ("typecheck_command", config.typecheck_command),
            ("lint_command", config.lint_command),
        )
        if value is None
    ]
    if unset and not (root / "pyproject.toml").exists():
        return DoctorCheck(
            name,
            STATUS_WARN,
            f"{len(unset)} of 3 commands fall back to a `uv run` default "
            f"({', '.join(unset)}) and there is no pyproject.toml at "
            f"{root}, so `uv run` has no project to run in: {stated}",
            "Set [verify] test_command / typecheck_command / lint_command "
            "to the commands this project actually uses.",
        )
    return DoctorCheck(
        name,
        STATUS_OK,
        f"Phase 1 will run {stated} (verify.resolve_verify_commands); "
        f"Tier A does not run them, `ks sense` does",
    )


def _interface_file_count(text: str) -> int:
    """How many FILES a "Public interfaces" section lists.

    The section is one `<path>: <symbols>` line per file, so a line
    whose text before the FIRST colon ends in `.py` is a file. Two
    things this deliberately survives. A symbol list contains `: `
    of its own (`def build(name: str) -> Deck`), which the split on
    the first colon handles. And `extract_public_interfaces` returns
    `""` on main but a `(none: ...)` sentence after PR #378/#381,
    which counts as 0 either way: a count of output LINES would read
    1 there and turn this check green on the repository it exists
    for.

    Acknowledged limit: a source path containing a colon is not
    counted. That undercounts by one line in a case no repository in
    the intake has.
    """
    return sum(1 for line in text.splitlines() if line.split(":", 1)[0].endswith(".py"))


def check_source_root(root: Path) -> DoctorCheck:
    """What the engineer is actually shown of this repository.

    Consumed by `feedforward.extract_public_interfaces`, the Phase 0
    stage that writes the "Public interfaces" section of the
    engineer's context block. Keyed on the source-root result and
    the file-budget outcome rather than on language (#198 comment of
    2026-09-16): on deckgen, a Python repository,
    `_find_top_source_dirs` returns nothing and the section is
    empty.
    """
    name = "source_root"
    # `is_relative_to` rather than a bare `relative_to`: PR #381
    # rewrites the function this reads, and a path it returned from
    # outside `root` would turn a report into a ValueError traceback.
    found = _find_top_source_dirs(root)
    roots = sorted(
        str(path.relative_to(root)) if path.is_relative_to(root) else str(path) for path in found
    )
    listed = ", ".join(roots[:5]) if roots else "none"
    count = _interface_file_count(extract_public_interfaces(root))
    if count == 0:
        return DoctorCheck(
            name,
            STATUS_WARN,
            f"feedforward.extract_public_interfaces summarises 0 files "
            f"for the engineer; source roots found: {listed}",
            "Nothing is broken in your repository; kstrl's interface "
            "extraction does not reach this layout, so the engineer works "
            "without an interface section. Track issue #378.",
        )
    return DoctorCheck(
        name,
        STATUS_OK,
        f"feedforward.extract_public_interfaces summarises {count} file(s) "
        f"of a {_MAX_PUBLIC_INTERFACE_FILES}-file budget from source "
        f"root(s): {listed}",
    )


def check_test_root(root: Path) -> DoctorCheck:
    """Tracked files that read as tests.

    Counted with `adequacy.is_test_path`, the same predicate the
    `[adequacy]` gate uses to classify a diff's files, so the answer
    here and the gate's answer cannot disagree. A root `tests/`
    probe would find nothing on deckgen, whose tests live under
    `packages/*/tests`.
    """
    name = "test_root"
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return DoctorCheck(name, STATUS_WARN, f"git could not list tracked files: {exc}", "")
    if result.returncode != 0:
        return DoctorCheck(
            name,
            STATUS_WARN,
            f"git ls-files exited {result.returncode}, so no tracked file could be classified",
            "Fix git_repo first.",
        )
    tests = sorted(path for path in result.stdout.split("\0") if path and is_test_path(path))
    if not tests:
        return DoctorCheck(
            name,
            STATUS_WARN,
            "0 tracked paths read as tests to adequacy.is_test_path, the "
            "predicate the [adequacy] gate classifies a diff with, so that "
            "gate sees no test file and the Phase 1 test command has "
            "nothing to run",
            "Add tests, or expect the adequacy gate to report nothing.",
        )
    return DoctorCheck(
        name,
        STATUS_OK,
        f"{len(tests)} tracked test path(s), e.g. {', '.join(tests[:3])} (adequacy.is_test_path)",
    )


def check_gitignore(root: Path) -> DoctorCheck:
    """`.kstrl/` is ignored.

    Asked of git rather than of the .gitignore text, because the
    in-loop scope guard walks `git ls-files --others
    --exclude-standard` and that honours `.git/info/exclude` and the
    global excludes file too.

    `git.ignore_source` asks nearly this question and is deliberately
    not reused: it returns None for "not ignored" AND for "git could
    not answer", and telling those two apart is the whole point of
    the three branches below.

    Deliberately NOT about `scripts/kstrl/`: that directory is the
    versioned per-project kstrl home (prompt.md, decisions.json,
    sense-baseline.json), the three harness files the engineer writes
    are carved out of both scope guards by exact path
    (`config.component_harness_files`), and ignoring it would hide
    files kstrl expects to be committed.
    """
    name = "gitignore"
    probe = f"{STATE_DIR_NAME}/runs/probe.json"
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", probe],
            cwd=root,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return DoctorCheck(name, STATUS_WARN, f"git check-ignore failed: {exc}", "")
    if result.returncode == 0:
        return DoctorCheck(
            name,
            STATUS_OK,
            f"git ignores {probe}, so the in-loop scope guard does not "
            f"count kstrl's own run journals against a component",
        )
    if result.returncode == 1:
        return DoctorCheck(
            name,
            STATUS_WARN,
            f"{probe} is not ignored; the in-loop scope guard counts every "
            f"untracked file against the component's allowedPaths, and "
            f"`git add -A` would commit kstrl's run journals",
            "Add the line `.kstrl/` to .gitignore, which is what "
            "`ks init` scaffolds (init_cmd.gitignore_block).",
        )
    return DoctorCheck(
        name,
        STATUS_WARN,
        f"git check-ignore exited {result.returncode}, so whether "
        f"{probe} is ignored could not be decided",
        "Fix git_repo first.",
    )


def check_protected_paths(root: Path) -> DoctorCheck:
    """CI, migration and deploy paths a component could edit.

    Consumed by `policy.evaluate_policy` through `[policy]
    paths_deny`. `verify.py` calls that gate only when the section is
    enabled, and that gate carries the non-overridable
    ENFORCEMENT_MACHINERY_PATHS halt, so a disabled section leaves CI
    unprotected as well.
    """
    name = "protected_paths"
    try:
        config = PolicyConfig.load(root)
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            name,
            STATUS_FAIL,
            f"[policy] could not be loaded ({exc})",
            "Fix the [policy] section of kstrl.toml.",
        )
    present = [name_ for name_ in PROTECTED_PATH_CANDIDATES if (root / name_).exists()]
    if not present:
        return DoctorCheck(
            name,
            STATUS_OK,
            "none of the candidate CI, migration or deploy paths exist here",
        )
    if not config.enabled:
        return DoctorCheck(
            name,
            STATUS_WARN,
            f"found {', '.join(present)}, and [policy] enabled is false, so "
            f"neither paths_deny nor the non-overridable "
            f"ENFORCEMENT_MACHINERY_PATHS halt runs at all",
            "Set [policy] enabled = true in kstrl.toml.",
        )
    patterns = [
        *config.paths_deny,
        *ENFORCEMENT_MACHINERY_PATHS,
        *config.enforcement_paths_extra,
    ]
    # Both spellings, measured: `_match_glob(".github/workflows",
    # DEFAULT_PATHS_DENY + ENFORCEMENT_MACHINERY_PATHS)` is None while
    # `".github/workflows/probe"` matches `.github/workflows/**`, and a
    # file candidate such as `Dockerfile` can only match under its own
    # name. Asking both is what makes a directory candidate and a file
    # candidate one question.
    uncovered = [
        candidate
        for candidate in present
        if _match_glob(candidate, patterns) is None
        and _match_glob(f"{candidate}/probe", patterns) is None
    ]
    if uncovered:
        suggestions = ", ".join(f'"{candidate}/**"' for candidate in uncovered)
        return DoctorCheck(
            name,
            STATUS_WARN,
            f"[policy] is enabled and its patterns do not cover "
            f"{', '.join(uncovered)} (tested with policy._match_glob, the "
            f"gate's own matcher; the candidate list itself is a heuristic)",
            f"Add to [policy] paths_deny: {suggestions}",
        )
    return DoctorCheck(
        name,
        STATUS_OK,
        f"[policy] is enabled and covers every candidate found ({', '.join(present)})",
    )


#: Every check, in report order. A tuple rather than a list built
#: inside `run_checks`, so the order is a declaration rather than a
#: side effect of how the loop happens to be written.
CHECKS = (
    check_git_repo,
    check_git_clean,
    check_github_cli,
    check_kstrl_config,
    check_verify_commands,
    check_source_root,
    check_test_root,
    check_gitignore,
    check_protected_paths,
)

# There is deliberately NO `CHECK_NAMES` constant here (decision 12).
# Each name is written once, in its own check function, and
# `tests/test_doctor.py` owns the tuple it compares the report
# against.


def run_checks(root: Path) -> list[DoctorCheck]:
    """Run every Tier A check against ``root``, in report order."""
    return [check(root) for check in CHECKS]


def verdict(checks: Sequence[DoctorCheck]) -> str:
    """One fail is not-ready; one warn is ready-with-warnings."""
    if any(check.status == STATUS_FAIL for check in checks):
        return VERDICT_NOT_READY
    if any(check.status == STATUS_WARN for check in checks):
        return VERDICT_READY_WITH_WARNINGS
    return VERDICT_READY


def exit_code_for(value: str) -> int:
    """0 for both ready verdicts, 2 for not-ready."""
    return EXIT_REFUSED if value == VERDICT_NOT_READY else 0


def fix_first(checks: Sequence[DoctorCheck]) -> list[str]:
    """The ordered fix-first list: failures, then warnings."""
    ordered = [check for check in checks if check.status == STATUS_FAIL]
    ordered += [check for check in checks if check.status == STATUS_WARN]
    return [check.fix for check in ordered if check.fix]


def report_path(root: Path, stamp: str) -> Path:
    """Where this run's report is written."""
    directory = state_dir(root) / DOCTOR_DIR_NAME
    return directory / f"report-{stamp}.json"


def diagnose(root: Path) -> dict[str, Any]:
    """Run Tier A and build the report document. Writes nothing."""
    checks = run_checks(root)
    now = datetime.now(UTC)
    return {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "root": str(root),
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "verdict": verdict(checks),
        "checks": [check.to_dict() for check in checks],
        "fix_first": fix_first(checks),
        "fit_boundaries": list(FIT_BOUNDARIES),
        "report_path": str(report_path(root, now.strftime(_STAMP_FORMAT))),
    }


def write_report(document: dict[str, Any]) -> str | None:
    """Write the report; return an error message, or None.

    A failure here does NOT change the verdict. Nothing in kstrl
    reads this file yet (the `ks serve` staleness hook is noted in
    #198 and not built), and the operator already has the whole
    report on stdout, so a read-only or full state directory is
    worth a line on stderr and nothing more.
    """
    path = Path(document["report_path"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, document)
    except OSError as exc:
        return f"could not write the doctor report to {path}: {exc}"
    return None


def render_text(document: dict[str, Any]) -> str:
    """The terminal report. Unstyled, so stdout, a pipe and the file
    cannot disagree about what was found."""
    lines = [f"ks doctor: {document['verdict']}", f"root: {document['root']}", ""]
    for check in document["checks"]:
        lines.append(f"  [{check['status']}] {check['name']}: {check['detail']}")
    fixes = document["fix_first"]
    if fixes:
        lines.extend(["", "Fix first:"])
        lines.extend(f"  {index}. {fix}" for index, fix in enumerate(fixes, start=1))
    lines.extend(["", "What this report does not tell you:"])
    lines.extend(f"  - {boundary}" for boundary in document["fit_boundaries"])
    lines.extend(["", f"report: {document['report_path']}"])
    return "\n".join(lines)
