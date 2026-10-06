"""`ks doctor`: is this repository ready to point kstrl at? (#198)

Tier A here. Every check is static and mechanical: nothing here runs
the repository's own [stack] checks, spawns an
agent, or spends anything. Measured cost: about 0.3 to 0.5 s per run
on this repository (three runs: 387, 403 and 493 ms), of which one
gh auth status network round trip is about 250 ms (bounded by
pr.GH_TIMEOUT when offline); the local checks together are
the rest. `ks doctor --measure` (#654) then runs Phase 1's [stack]
checks on the base branch, the reading `ks factory`
takes before any engineer runs, and fails the verdict where it would refuse.

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

import dataclasses
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git, pr
from kstrl.atomicio import atomic_write_json
from kstrl.config import resolve_config_file
from kstrl.config_preflight import SURFACE_REJECTIONS, config_problem_lines, raise_if_defect
from kstrl.init_cmd import BUILD_MANIFEST_FIX, build_manifest_blocker, build_manifest_ok_reason
from kstrl.policy import ENFORCEMENT_MACHINERY_PATHS, PolicyConfig, _match_glob
from kstrl.stack import NO_STACK, file_stack_item, legacy_proposal, stack_in_force
from kstrl.statedir import STATE_DIR_NAME, state_dir

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: Version of the `ks doctor --json` document. Its own number, not
#: `CHECK_SCHEMA_VERSION`: the two documents answer different
#: questions and a reader of one must not infer the other's shape.
#: 2 (#654): the `base_gates` key joined, null without `--measure`.
DOCTOR_SCHEMA_VERSION = 2

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

#: What to do about a ``[stack]`` nobody confirmed (#696 slice 3).
STACK_CONFIRM_FIX = "Run ks factory and confirm it, or ks inbox approve its item."

VERDICT_READY = "ready"
VERDICT_READY_WITH_WARNINGS = "ready-with-warnings"
VERDICT_NOT_READY = "not-ready"

#: Exit code for a not-ready verdict: the checks ran and the answer
#: is a finding, which is what 1 means on every `ks` command (#452).
EXIT_NOT_READY = 1

#: Subdirectory of the state directory the report lands in. Declared
#: in `statedir.STATE_SUBDIRS` as well, which is what
#: `tests/test_state_dir_scope.py` checks this against.
DOCTOR_DIR_NAME = "doctor"

_STAMP_FORMAT = "%Y%m%d-%H%M%S"

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
    "Tier A reads the repository and runs none of your commands. `ks doctor "
    "--measure` runs your [stack] checks once on the base "
    "branch, as `ks factory` does before any engineer and refuses to start "
    "when one of them fails there. One run cannot tell you whether your "
    "suite is fast or flaky.",
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


@dataclasses.dataclass(frozen=True)
class DoctorCheck:
    """One Tier A answer: what was measured, and what to do about it."""

    name: str
    status: str
    detail: str
    fix: str = ""


#: What one check function returns: status, detail, fix. The name is
#: not part of it; `CHECKS` carries that, once, so it is never a
#: fourth positional argument every check has to repeat.
_CheckResult = tuple[str, str, str]


def check_git_repo(root: Path) -> _CheckResult:
    """A repository with commits and a base branch kstrl can reach.

    Consumed by `factory`, which cuts one git worktree per component
    off the base branch, and by every Phase 1 check that reads
    `git diff <base>...HEAD`.
    """
    if not git.is_git_repo(root):
        return (
            STATUS_FAIL,
            f"{root} is not inside a git repository; factory cuts a "
            f"worktree per component and Phase 1 diffs against a base "
            f"branch, so neither can run",
            "Run `git init` and make one commit, or point --root at a checkout.",
        )
    head = git.get_head_sha(root)
    if head is None:
        return (
            STATUS_FAIL,
            "the repository has no commits, so there is no base ref for "
            "factory to cut a component worktree from",
            "Make one commit.",
        )
    base = git.detect_base_branch(root)
    try:
        git.get_diff_names(base, root, strict=True)
    except git.GitDiffError as exc:
        return (
            STATUS_WARN,
            f"the detected base branch {base!r} cannot be diffed against "
            f"({exc}); the diff-scope, "
            f"bad-patterns and policy checks all read that diff",
            f"Create or fetch {base}, or name the long-lived branch with "
            f"`ks check --base` and `ks run --base-branch`.",
        )
    return (
        STATUS_OK,
        f"git repository at HEAD {head[:12]}, base branch {base} "
        f"(the branch factory cuts component worktrees from)",
        "",
    )


def check_git_clean(root: Path) -> _CheckResult:
    """Uncommitted work does not reach the engineer.

    Consumed by `factory` (each component worktree starts from the
    base ref) and by `git.capture_workspace_baseline`, which has to
    subtract pre-existing dirt from the in-loop scope guard.
    """
    if not git.is_git_repo(root):
        return (
            STATUS_WARN,
            "not a git repository, so the working tree could not be read",
            "Fix git_repo first.",
        )
    try:
        changed = sorted(git.get_changed_files(root))
    except git.GitDiffError as exc:
        return (STATUS_WARN, f"the working tree could not be read: {exc}", "")
    if changed:
        listed = ", ".join(changed[:5])
        return (
            STATUS_WARN,
            f"{len(changed)} uncommitted or untracked file(s) ({listed}); "
            f"factory cuts each component worktree from the base ref, so "
            f"none of this reaches the engineer, and the in-loop scope "
            f"guard has to subtract it",
            "Commit or stash before starting a run.",
        )
    return (
        STATUS_OK,
        "clean tree; every component worktree starts from the base ref "
        "with nothing for the scope guard to subtract",
        "",
    )


def check_github_cli(root: Path) -> _CheckResult:
    """Pushing a branch and opening a PR.

    Consumed by `pr.push_create_and_merge_pr`, which shells out to
    `gh`. Degraded mode, never a failure: the factory runs with
    `[factory] create_prs = false` and the operator merges by hand.
    """
    slug = git.get_origin_slug(root)
    available = pr.is_gh_available()
    if available and slug:
        return (
            STATUS_OK,
            f"gh is authenticated and origin is {slug}, so kstrl can push branches and open PRs",
            "",
        )
    missing = []
    if not available:
        missing.append("`gh` is not on PATH or `gh auth status` exited non-zero")
    if not slug:
        missing.append("there is no `origin` remote")
    return (
        STATUS_WARN,
        "degraded mode: " + "; ".join(missing) + "; kstrl can push no branch and open no PR",
        "Run `gh auth login` and add an origin remote, or set "
        "[factory] create_prs = false and merge by hand.",
    )


def check_kstrl_config(root: Path) -> _CheckResult:
    """Every configuration section resolves.

    Consumed by `config_preflight.preflight_config`, the same check
    every other command runs before it starts anything. This command
    is exempt from that seam and reports the same problems as a
    finding instead of a refusal, through
    `config_preflight.config_problem_lines`, the helper already split
    out for `ks config show` and the TUI config screen so a third copy
    of "what is wrong with this configuration" is not written here. A
    document that will not parse folds into that one line; this check
    keeps no narrower idea of a parse failure than that helper does.
    """
    path = resolve_config_file(root)
    warnings: list[str] = []
    problems = config_problem_lines(root, warn=warnings.append)
    if problems:
        return (
            STATUS_FAIL,
            f"{len(problems)} unusable section(s) in {path}: " + "; ".join(problems),
            "Fix the named sections; every other kstrl command refuses while they stand.",
        )
    if not path.exists():
        return (
            STATUS_OK,
            f"no kstrl.toml at {path}; every section uses its built-in "
            f"default, which is a supported configuration",
            "",
        )
    if warnings:
        return (
            STATUS_WARN,
            "; ".join(warnings),
            "Fix the degrading sections; they warn and continue today.",
        )
    return (
        STATUS_OK,
        f"{path} resolves in full",
        "",
    )


def _not_evaluated(name: str) -> _CheckResult:
    """The shared row for a check whose own section could not be
    loaded because kstrl.toml itself did not load.

    `check_kstrl_config` already reports that failure once, with the
    fragment naming what is wrong; a check that re-runs the same
    failed load to build its own message would repeat that text under
    a second name, and a third check doing the same would make it
    three. This is the pointer instead. Each caller still keeps its
    own `except (OSError, ValueError)`: a bug inside a check must
    still traceback rather than get silently swallowed by a catch-all
    in `run_checks`.
    """
    return (
        STATUS_FAIL,
        "not evaluated: kstrl.toml did not load (see kstrl_config)",
        "",
    )


def check_build_manifest(root: Path) -> _CheckResult:
    """A build manifest kstrl will not have to create (#434).

    Consumed by the pre-spend refusal in `ks decompose` and `ks factory
    --spec` (`init_cmd.build_manifest_blocker`): no component may list
    a root build manifest in its allowedPaths, so without one the
    architect can only halt and ask who writes it, after a paid call.
    A kstrl.toml that does not load routes through `_not_evaluated`,
    because whether it holds a ``[stack]`` is then unknown.
    """
    try:
        blocker = build_manifest_blocker(root)
    except (OSError, ValueError):
        return _not_evaluated("build_manifest")
    if blocker is not None:
        return (STATUS_FAIL, blocker, BUILD_MANIFEST_FIX)
    return (STATUS_OK, build_manifest_ok_reason(root), "")


def check_verify_commands(root: Path) -> _CheckResult:
    """The ``[stack]`` checks Phase 1 and Phase 3 will run (#696).

    Tier A reads them and prints them; it does not run them. With no
    ``[stack]`` every run refuses, and when kstrl.toml still holds retired
    ``[verify]`` commands they are filed as a proposed stack in the inbox
    (#696 decision 12): one open stack_confirmation item, which nothing
    approves but a person, and only once kstrl.toml holds its text. A failed
    load routes through `_not_evaluated`, as before.
    """
    try:
        stack = stack_in_force(root)
        proposal = legacy_proposal(root) if stack is None else None
    except (OSError, ValueError):
        return _not_evaluated("verify_commands")
    if stack is not None and stack.unconfirmed:
        return (STATUS_FAIL, f"the [stack] in kstrl.toml {stack.unconfirmed}", STACK_CONFIRM_FIX)
    if stack is not None:
        listed = ", ".join(f"{name} `{cmd}`" for name, cmd in stack.checks)
        return (STATUS_OK, f"Phase 1 and Phase 3 will run the [stack] checks: {listed}", "")
    if proposal is None:
        return (STATUS_FAIL, NO_STACK, "Write a [stack] in kstrl.toml, then confirm it.")
    try:
        item = file_stack_item(root, proposal, proposal=True)
    except SURFACE_REJECTIONS as exc:
        raise_if_defect(exc)
        return (STATUS_FAIL, f"{NO_STACK} No proposal was filed: {exc}", "")
    return (
        STATUS_FAIL,
        f"{NO_STACK} Filed the retired [verify] commands as proposed [stack] "
        f"{proposal.digest[:12]} (inbox item {item.id[:8]}); nothing approved it.",
        f"ks inbox show {item.id[:8]} prints the table: replace the [verify] command keys "
        f"in kstrl.toml with it, then ks inbox approve {item.id[:8]}.",
    )


def check_gitignore(root: Path) -> _CheckResult:
    """`.kstrl/` is ignored.

    What a ``[stack]`` check writes is not asked here: kstrl names no
    build output of its own (#696). The base gates run the checks once on
    the base branch and refuse on every entry ``git status`` shows after
    them (``base_gates.refusal_lines``), which `ks doctor --measure`
    reports.

    Asked of git rather than of the .gitignore text, because the
    in-loop scope guard walks `git ls-files --others
    --exclude-standard` and that honours `.git/info/exclude` and the
    global excludes file too.

    The three branches below tell "not ignored" apart from "git could
    not answer".

    Deliberately NOT about `scripts/kstrl/`: that directory is the
    versioned per-project kstrl home (prompt.md, decisions.json,
    baseline.json), the three harness files the engineer writes
    are carved out of both scope guards by exact path
    (`config.component_harness_files`), and ignoring it would hide
    files kstrl expects to be committed.
    """
    probe = f"{STATE_DIR_NAME}/runs/probe.json"
    ignore_line = f"{STATE_DIR_NAME}/"
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", probe],
            cwd=root,
            capture_output=True,
            timeout=git.DEFAULT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (STATUS_WARN, f"git check-ignore failed: {exc}", "")
    if result.returncode == 0:
        return (
            STATUS_OK,
            f"git ignores {probe}, so the in-loop scope guard does not "
            f"count kstrl's own run journals against a component",
            "",
        )
    if result.returncode == 1:
        return (
            STATUS_WARN,
            f"{probe} is not ignored; the in-loop scope guard counts every "
            f"untracked file against the component's allowedPaths, and "
            f"`git add -A` would commit kstrl's run journals",
            f"Add the line `{ignore_line}` to .gitignore, which is what `ks init` scaffolds.",
        )
    return (
        STATUS_WARN,
        f"git check-ignore exited {result.returncode}, so whether "
        f"{probe} is ignored could not be decided",
        "Fix git_repo first.",
    )


def check_protected_paths(root: Path) -> _CheckResult:
    """CI, migration and deploy paths a component could edit.

    Consumed by `policy.evaluate_policy` through `[policy]
    paths_deny`. `verify.py` calls that gate only when the section is
    enabled, and that gate carries the non-overridable
    ENFORCEMENT_MACHINERY_PATHS halt, so a disabled section leaves CI
    unprotected as well. A failed load routes through `_not_evaluated`;
    see `check_verify_commands` for why.
    """
    try:
        config = PolicyConfig.load(root)
    except (OSError, ValueError):
        return _not_evaluated("protected_paths")
    present = [name_ for name_ in PROTECTED_PATH_CANDIDATES if (root / name_).exists()]
    if not present:
        return (
            STATUS_OK,
            "none of the candidate CI, migration or deploy paths exist here",
            "",
        )
    if not config.enabled:
        return (
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
        return (
            STATUS_WARN,
            f"[policy] is enabled and its patterns do not cover "
            f"{', '.join(uncovered)} (tested with the policy gate's own "
            f"matcher; the candidate list itself is a heuristic)",
            f"Add to [policy] paths_deny: {suggestions}",
        )
    return (
        STATUS_OK,
        f"[policy] is enabled and covers every candidate found ({', '.join(present)})",
        "",
    )


#: Every check, in report order, paired with the name it is reported
#: under. A tuple rather than a list built inside `run_checks`, so the
#: order is a declaration rather than a side effect of how the loop
#: happens to be written. The name lives here, once, rather than as a
#: local inside each check function repeated into every one of its
#: returns.
CHECKS: tuple[tuple[str, Callable[[Path], _CheckResult]], ...] = (
    ("git_repo", check_git_repo),
    ("git_clean", check_git_clean),
    ("github_cli", check_github_cli),
    ("kstrl_config", check_kstrl_config),
    ("build_manifest", check_build_manifest),
    ("verify_commands", check_verify_commands),
    ("gitignore", check_gitignore),
    ("protected_paths", check_protected_paths),
)

# There is deliberately NO `CHECK_NAMES` constant here (decision 12).
# Each name is written once, in the `CHECKS` table above, and
# `tests/test_doctor.py` owns the tuple it compares the report
# against.


def run_checks(root: Path) -> list[DoctorCheck]:
    """Run every Tier A check against ``root``, in report order."""
    return [DoctorCheck(name, *check(root)) for name, check in CHECKS]


def verdict(checks: Sequence[DoctorCheck]) -> str:
    """One fail is not-ready; one warn is ready-with-warnings."""
    if any(check.status == STATUS_FAIL for check in checks):
        return VERDICT_NOT_READY
    if any(check.status == STATUS_WARN for check in checks):
        return VERDICT_READY_WITH_WARNINGS
    return VERDICT_READY


def exit_code_for(value: str) -> int:
    """0 for both ready verdicts, 1 for not-ready."""
    return EXIT_NOT_READY if value == VERDICT_NOT_READY else 0


def fix_first(checks: Sequence[DoctorCheck]) -> list[str]:
    """The ordered fix-first list: failures, then warnings."""
    ordered = [check for check in checks if check.status == STATUS_FAIL]
    ordered += [check for check in checks if check.status == STATUS_WARN]
    return [check.fix for check in ordered if check.fix]


def report_path(root: Path, stamp: str) -> Path:
    """Where this run's report is written."""
    directory = state_dir(root) / DOCTOR_DIR_NAME
    return directory / f"report-{stamp}.json"


def diagnose(root: Path, measure_ui: UI | None = None) -> dict[str, Any]:
    """Run Tier A, then Tier B when given a UI to report its progress on,
    and build the report document. Only Tier B runs a repository command."""
    checks = run_checks(root)
    readings: dict[str, Any] = {"base_gates": None, "isolation": None, "replay": None}
    if measure_ui is not None:
        # Here, not at the top: doctor_measure imports this module.
        from kstrl.doctor_measure import measure_tier_b

        readings = measure_tier_b(root, checks, measure_ui)
    now = datetime.now(UTC)
    return {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "root": str(root),
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "verdict": verdict(checks),
        "checks": [dataclasses.asdict(check) for check in checks],
        **readings,
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
