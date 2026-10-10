"""`ks check` and `ks doctor`: the mechanical verification of a diff and the
readiness report, with the JSON document `ks check --json` writes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import click

from kstrl import baseline, baseline_report
from kstrl.cli_seam import _console_ui, _preflight_warn, cli
from kstrl.config_report import UI_MODES
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.git import get_head_sha, get_origin_slug, resolve_base_branch
from kstrl.stack import NO_STACK

if TYPE_CHECKING:
    from kstrl.policy import PolicyConfig
    from kstrl.verify_model import VerificationResult, VerifyConfig


#: 2 (#306): a ``not_measured`` array joined the document, and the
#: meaning of an absent ``mutation_testing`` row changed with it. Under
#: 1 that absence meant one thing, "turned off in kstrl.toml", because
#: an enabled mutation check emitted a row even when it had measured
#: nothing. Under 2 it also covers "asked for, measured nothing", and
#: ``not_measured`` is what tells the two apart. A v1 reader inferring
#: "absent means disabled" is wrong about a v2 document, which is why
#: this is a bump and not a silent addition.
#:
#: 3 (#335): the same two changes for the dead-code gate, by v2's own
#: rule. ``check_dead_code`` fused a ruff auto-fix phase and a vulture
#: scan into one row, so an absent ``dead_code`` row still meant only
#: "turned off"; now it also means "asked for, measured nothing". And a
#: NEW row name appears in ``checks``, ``dead_code_ruff``, which is the
#: ruff half answering for itself. A v2 reader is wrong about a v3
#: document on both counts.
#:
#: Still not a complete index of every check that did not run:
#: ``require_self_critique`` with no ``progress_file_path`` and no PRD
#: emits neither a row nor a gap. That predates this and is a follow-up
#: on #306; a reader must not read an empty array as "everything
#: enabled was measured".
#:
#: #227 added a ``baseline`` key and did NOT bump this again, on the rule
#: the v2 and v3 bumps were made under: a bump is for an addition that
#: changes what an EXISTING key means. That key is absent exactly when
#: ``--compare-baseline`` was not asked for, it restates nothing, and a
#: reader that does not know it ignores it.
#:
#: v4 (#395): the ``baseline`` key was called ``dampener`` through v3.
#: Renaming an EXISTING key is strictly louder than the additions that
#: earned v2 and v3, so this bumps. A v3 reader looking for ``dampener``
#: finds nothing under v4; there is no alias, because this document's
#: keys retire by rename the same way a retired kstrl.toml name does.
#:
#: v5 (#696 slice 8): kstrl retired six checks
#: (``kstrl.baseline.RETIRED_CHECKS``), so their names no longer appear in
#: ``checks``. The ``baseline`` block's ``stopped_measuring`` leaves them out
#: and a new ``retired`` key names them, which changes what an existing key
#: holds.
CHECK_SCHEMA_VERSION = 5


def _check_document(
    path: Path,
    base: str,
    result: VerificationResult,
    baseline_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``ks check --json`` document, at :data:`CHECK_SCHEMA_VERSION`.

    Its own function because it is a published contract and ``check``
    is a 200-line command: a reader checking what the current schema
    promises should not have to find it among the preflight, the base
    resolution and the terminal rendering.

    ``baseline_block`` is the #227 comparison, present only when
    ``--compare-baseline`` was given and omitted entirely otherwise.
    """
    document: dict[str, Any] = {
        "schema_version": CHECK_SCHEMA_VERSION,
        "path": str(path),
        "base_branch": base,
        "passed": result.passed,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "message": check.message,
                "details": list(check.details),
                "duration_seconds": check.duration_seconds,
                "findings": [f.to_dict() for f in check.findings],
            }
            for check in result.checks
        ],
        # Beside ``checks``, never inside it: an entry here is a check
        # that ran no measurement, and putting it in the array a reader
        # folds with ``all(passed)`` is exactly the defect #306 closed.
        # Empty for a tree where every enabled check measured something.
        "not_measured": [gap.to_dict() for gap in result.not_measured],
    }
    if baseline_block is not None:
        document["baseline"] = baseline_block
    return document


def _check_verify_digest(
    verify_cfg: VerifyConfig,
    *,
    mode: baseline.Mode | None,
    as_json: bool,
) -> str:
    """The digest of HOW this run measures, and the refusal of a foreign baseline.

    Its own function for two reasons. It is the one place a comparison is
    refused for having been measured differently, so a reader looking for that
    rule finds it whole. And ``check`` is held at its cognitive number by a
    gate that fails rather than advises: inlined, this cost three points.

    Placed after the config load, because the digest is a function of the
    ``[stack]`` and the timeout, and still before the checks: a
    comparison that cannot be trusted is refused in a tenth of a second rather
    than after five minutes of measurement.
    """
    stack = verify_cfg.project_stack
    if stack is None:
        # #696 slice 4: no check to run, so nothing to measure or compare.
        _check_error(NO_STACK, as_json)
    digest = baseline.verify_digest(stack, verify_cfg.subprocess_timeout)
    if isinstance(mode, baseline.CompareMode):
        try:
            baseline.refuse_foreign_baseline(mode.baseline, digest)
        except baseline.BaselineError as exc:
            _check_error(str(exc), as_json)
    return digest


def _check_baseline_report(
    path: Path,
    base: str,
    result: VerificationResult,
    *,
    mode: baseline.Mode,
    as_json: bool,
    digest: str,
) -> NoReturn:
    """The #227 baseline's own output, printed instead of the check table.

    ``--write-baseline`` exits on the CHECK's verdict, because writing a
    baseline is a measurement of the tree. ``--compare-baseline`` exits on the
    COMPARISON and never on ``result.passed``: a red tree is the normal state
    for the brownfield repository this exists for, and the issue's own
    acceptance requires exit 0 on a tree carrying a fresh E501.

    ``mode`` already carries the baseline on the compare side, read before the
    checks ran, so there is nothing here to fetch and nothing to assert about
    two functions having agreed offstage.
    """
    current = baseline.baseline_from_result(
        result,
        base_ref=get_head_sha(path),
        # `owner/repo` from origin, never the directory name: every kstrl lane
        # measures inside a git worktree named after an issue number, so a
        # baseline written in one would record "227" and make every later
        # comparison in a normal checkout report a mismatch that means nothing.
        project=get_origin_slug(path) or path.name,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        check_schema_version=CHECK_SCHEMA_VERSION,
        digest=digest,
    )
    if isinstance(mode, baseline.WriteMode):
        try:
            baseline.write_baseline(mode.path, current, force=mode.force)
        except (OSError, baseline.BaselineError) as exc:
            _check_error(str(exc), as_json)
        click.echo(baseline.write_summary_line(mode.path, current))
        sys.exit(0 if result.passed else 1)

    comparison = baseline.compare(mode.baseline, current)
    if as_json:
        block = baseline_report.comparison_document(comparison, mode.baseline, current, mode.path)
        click.echo(json.dumps(_check_document(path, base, result, block), indent=2))
    elif mode.output_format == baseline.FORMAT_MARKDOWN:
        click.echo(
            baseline_report.render_markdown(
                comparison,
                mode.baseline,
                mode.path,
                fail_on_regression=mode.fail_on_regression,
            )
        )
    else:
        for line in baseline_report.render_human(comparison, mode.baseline, mode.path):
            click.echo(line)
    sys.exit(baseline.exit_code_for(comparison, fail_on_regression=mode.fail_on_regression))


def _check_report(
    path: Path,
    base: str,
    result: VerificationResult,
    *,
    mode: baseline.Mode | None,
    as_json: bool,
    ui: str,
    no_color: bool,
    digest: str,
) -> NoReturn:
    """Print the measurement and exit: 0 when every check passed, 1 otherwise.

    Its own function because ``check`` is a 200-line command that was at
    cyclomatic 9 against a gate of 10, and the rendering is the half of it
    that has nothing to do with deciding WHAT to measure. Lifting it out is
    what left room for the baseline branch in the command body: measured
    against the ratchet's pinned ruff, ``check`` is at 7 with this extracted
    and the branch added.
    """
    if mode is not None:
        _check_baseline_report(path, base, result, mode=mode, as_json=as_json, digest=digest)

    if as_json:
        click.echo(json.dumps(_check_document(path, base, result), indent=2))
        sys.exit(0 if result.passed else 1)

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)
    ui_impl.section("ks check")
    ui_impl.kv("Path", str(path))
    ui_impl.kv("Base branch", base)
    ui_impl.info("")
    # Shared with `ks feature`'s #288 report: one renderer for this
    # object, so a column change cannot land in one command and silently
    # not the other.
    for line in result.report_lines():
        ui_impl.info(line)
    ui_impl.info("")
    failed = sum(1 for c in result.checks if not c.passed)
    if result.passed:
        ui_impl.ok("check: PASS")
        sys.exit(0)
    ui_impl.err(f"check: FAIL ({failed} of {len(result.checks)} checks failed)")
    sys.exit(1)


def _check_error(
    message: str,
    as_json: bool,
    *,
    schema_version: int = CHECK_SCHEMA_VERSION,
) -> NoReturn:
    """Exit 2: the measurement itself could not run.

    One ``error:`` line on stderr always; with ``--json`` a one-key
    document on stdout so a pipe reading stdout sees the failure too.

    Shared with ``ks doctor``'s refusal of a ``--root`` that is not a
    directory, which named this exact
    contract in the PR body without going through it: printing the
    JSON error document is what ``--json`` on a refusal means, and a
    caller with its own schema names it with ``schema_version`` rather
    than getting check's.
    """
    click.echo(f"error: {message}", err=True)
    if as_json:
        click.echo(
            json.dumps(
                {"schema_version": schema_version, "error": message},
            )
        )
    sys.exit(2)


def _check_needs_diff(verify_cfg: VerifyConfig, policy_cfg: PolicyConfig) -> bool:
    """Whether a check `ks check` is about to run reads ``git diff``.

    ``diff_scope`` and ``bad_patterns`` consume the diff through the
    LENIENT git helpers, which map a bad ref, a missing base or a
    non-repository onto an EMPTY file list, indistinguishable from
    "nothing changed". diff_scope then reports "0 files, all within
    scope", bad_patterns "no files in the diff", and ``ks check`` exits
    0 having measured nothing. So the answer here gates one strict read
    up front, and cannot-measure becomes exit 2.

    Its own function because ``check`` is grandfathered at the cognitive
    ratchet, so the extra clause is a refusal at commit time if it stays
    inline - and because "does anything here need a base" has an answer
    worth stating once.
    """
    return bool(verify_cfg.check_diff_scope or verify_cfg.check_bad_patterns or policy_cfg.enabled)


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root; kstrl.toml is read from here (defaults to current directory)",
)
@click.option(
    "--path",
    "tree_path",
    type=click.Path(path_type=Path),
    help="Tree to measure: a worktree, a checkout, any directory (defaults to --root)",
)
@click.option(
    "--base",
    "base_branch",
    type=str,
    default=None,
    help="Base branch for the diff-scope and bad-pattern checks "
    "(default: auto-detected from the repository)",
)
@click.option(
    "--prd",
    "prd_path",
    type=click.Path(path_type=Path),
    help="PRD file; when given, the prd_stories check also runs",
)
@click.option(
    "--allowed-path",
    "allowed_paths",
    multiple=True,
    help="Glob the diff must stay inside (repeatable); when absent "
    "diff-scope reports no scope constraints",
)
@click.option(
    "--write-baseline",
    "write_baseline",
    # An option with an OPTIONAL value: `is_flag=False` plus a `flag_value`
    # makes the bare flag yield the sentinel. `type=str`, not `click.Path`, so
    # the sentinel is never handed to a path converter. Measured against click
    # 8.4.2: `--write-baseline --force` does not swallow `--force` as the
    # value, and the sentinel does not appear in `--help`.
    is_flag=False,
    flag_value=baseline.OPTIONAL_VALUE_SENTINEL,
    default=None,
    type=str,
    metavar="[PATH]",
    help="Record the current signature counts as a baseline "
    "(default: scripts/kstrl/baseline.json; a relative PATH is "
    "resolved under --root)",
)
@click.option(
    "--compare-baseline",
    "compare_baseline",
    is_flag=False,
    flag_value=baseline.OPTIONAL_VALUE_SENTINEL,
    default=None,
    type=str,
    metavar="[PATH]",
    help="Report what this tree added to a recorded baseline "
    "(default: scripts/kstrl/baseline.json; a relative PATH is "
    "resolved under --root)",
)
@click.option(
    "--force",
    is_flag=True,
    help="With --write-baseline: replace an existing baseline file",
)
@click.option(
    "--fail-on-regression",
    "fail_on_regression",
    is_flag=True,
    help="With --compare-baseline: exit 1 on a regression (default: advisory, always exit 0)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(baseline.OUTPUT_FORMATS),
    # None, not "human", so an explicit --format human can be told apart from
    # the default and refused alongside the other flags that would do nothing.
    default=None,
    help="With --compare-baseline: report format (markdown suits a PR comment)",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print the measurement as one JSON document instead of a table",
)
@click.option(
    "--ui",
    type=click.Choice(UI_MODES),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
def check(
    root: Path | None,
    tree_path: Path | None,
    base_branch: str | None,
    prd_path: Path | None,
    allowed_paths: tuple[str, ...],
    write_baseline: str | None,
    compare_baseline: str | None,
    force: bool,
    fail_on_regression: bool,
    output_format: str | None,
    as_json: bool,
    ui: str,
    no_color: bool,
) -> None:
    """Run the mechanical checks against a tree and print the measurement.

    R10.1: the same checks Phase 1 runs inside the factory (the checks
    of the confirmed [stack], diff scope, bad patterns, plus the opt-in
    policy check from kstrl.toml), run by hand with no PRD, no branch,
    no worktree and no agent spend.

    The measurement is read-only. It runs against your live checkout,
    not a worktree kstrl owns, so it writes nothing to .kstrl/ and never
    edits, stages or commits. The exception is the [stack] checks, which
    are your programs and write their own caches.

    A check that could not run gets NO row: it is reported under
    not_measured with the reason, never as a passing check (#306).

    Most checks read `git diff <base>...HEAD`, so the tree must be a git
    repository with a reachable base unless every diff-based check is
    turned off in kstrl.toml.

    Exit 0 when every check passed, 1 when any failed, 2 when the
    measurement itself could not run (missing path, bad kstrl.toml, or
    git cannot produce the diff).

    R10.6 (#227): --write-baseline records the signature counts to a file the
    repository tracks and --compare-baseline reports what this tree added to
    one. The comparison is ADVISORY - it exits 0 whether or not it found a
    regression - until --fail-on-regression is passed. Its exit code never
    follows result.passed, because a red tree is the normal state for the
    brownfield repositories the baseline exists for.
    """
    root_dir = root.resolve() if root else Path.cwd()
    path = tree_path.resolve() if tree_path else root_dir

    if not root_dir.is_dir():
        _check_error(f"root is not a directory: {root_dir}", as_json)
    if not path.is_dir():
        _check_error(f"path is not a directory: {path}", as_json)

    # Before the checks, not after: a full run here costs minutes, so a
    # refused flag combination or an unreadable baseline is reported in a
    # tenth of a second rather than after the test suite.
    try:
        mode = baseline.resolve_mode(
            write_baseline=write_baseline,
            compare_baseline=compare_baseline,
            force=force,
            fail_on_regression=fail_on_regression,
            output_format=output_format,
            as_json=as_json,
            root_dir=root_dir,
        )
    except (baseline.BaselineUsage, baseline.BaselineError) as exc:
        _check_error(str(exc), as_json)

    from kstrl.config_preflight import preflight_config
    from kstrl.policy import PolicyConfig
    from kstrl.verify import run_mechanical_verification
    from kstrl.verify_model import VerifyConfig

    try:
        # The WHOLE configuration, not only the three sections this
        # command reads. `check` is exempt from the entry seam because
        # its contract adds a JSON error document to the seam's exit 2,
        # and an exemption is only honest if the command does the same
        # check: checking four of twenty-two would keep exactly the
        # "depends which section you typo'd" property #272 removed,
        # inside the exemption.
        preflight_config(root_dir, warn=_preflight_warn)
        verify_cfg = VerifyConfig.load(root_dir)
        policy_cfg = PolicyConfig.load(root_dir)
    except (OSError, ValueError) as exc:
        # ValueError covers malformed TOML (load_toml_section), the
        # preflight's ConfigError, and the loaders' own validation
        # errors (PolicyConfigError is one).
        _check_error(f"could not load kstrl.toml from {root_dir}: {exc}", as_json)

    digest = _check_verify_digest(verify_cfg, mode=mode, as_json=as_json)

    base = resolve_base_branch(base_branch, path)

    if _check_needs_diff(verify_cfg, policy_cfg):
        # Ask git the same question once, strictly, before any check
        # runs. Cannot-measure is exit 2; it is never a pass.
        from kstrl import git as _git

        try:
            _git.get_diff_names(base, path, strict=True)
        except _git.GitDiffError as exc:
            origin = (
                "from --base" if base_branch else "auto-detected; name the right one with --base"
            )
            _check_error(
                f"git cannot measure the diff against {base!r} ({origin}): {exc}",
                as_json,
            )

    result = run_mechanical_verification(
        worktree_path=path,
        prd_path=prd_path.resolve() if prd_path is not None else None,
        base_branch=base,
        allowed_paths=list(allowed_paths) or None,
        config=verify_cfg,
        policy_config=policy_cfg,
        autonomy_level=0,
    )

    _check_report(
        path,
        base,
        result,
        mode=mode,
        as_json=as_json,
        ui=ui,
        no_color=no_color,
        digest=digest,
    )


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Repository to assess; kstrl.toml is read from here (defaults to current directory)",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print the report as one JSON document instead of a table",
)
@click.option(
    "--measure",
    is_flag=True,
    help="Tier B: also run the test, typecheck and lint commands on the base "
    "branch, as ks factory does before any engineer runs",
)
def doctor(root: Path | None, as_json: bool, measure: bool) -> None:
    """Assess whether this repository is ready to point kstrl at.

    Exit 0 for ready and ready-with-warnings, 1 for not-ready (a
    finding; with --measure, a gate that fails on the base branch is
    one), 2 when it cannot run (an unusable --root).
    """
    from kstrl import doctor as doctor_mod
    from kstrl.ui.plain import PlainUI

    root_dir = (root or Path.cwd()).resolve()
    if not root_dir.is_dir():
        _check_error(
            f"root is not a directory: {root_dir}",
            as_json,
            schema_version=doctor_mod.DOCTOR_SCHEMA_VERSION,
        )

    # PlainUI writes to stderr, so --json stays one document on stdout.
    document = doctor_mod.diagnose(root_dir, PlainUI() if measure else None)
    failure = doctor_mod.write_report(document)
    if failure is not None:
        click.echo(f"warning: {failure}", err=True)
    if as_json:
        click.echo(json.dumps(document, indent=2))
    else:
        click.echo(doctor_mod.render_text(document))
    sys.exit(doctor_mod.exit_code_for(document["verdict"]))
