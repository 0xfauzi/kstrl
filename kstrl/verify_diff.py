"""The checks that read the change: diff scope, bad patterns, policy envelope."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

from kstrl import git
from kstrl.findings import Finding, finding_waiver
from kstrl.guards import path_is_allowed
from kstrl.policy import (
    DEFAULT_SECRET_PATTERNS,
    PolicyConfig,
    PolicyConfigError,
    _scan_secrets,
    evaluate_policy,
    parse_added_lines,
)
from kstrl.verify_model import (
    CheckResult,
)
from kstrl.waivers import Waivers, apply_waivers, waiver_note

#: What the two diff-driven checks report when the diff handed them nothing.
#:
#: One constant because the baseline turns a row's message into the REASON a
#: check is unmeasured, and round 2 of review on #357 found the two checks
#: disagreeing about the same empty diff - one measured, one did not. They sit
#: on the same `git diff`, so they answer this question together or the
#: mechanism is a coin toss over which check the operator configured.
NO_FILES_IN_THE_DIFF = "no files in the diff"

#: H3 (#303): fragments _diff_scope_details assembles; versioned as one
#: body (docs/adversarial-roadmap.md, H3a sweep row).
DIFF_SCOPE_DETAILS_PROMPT_VERSION = "1.0.0"

DIFF_SCOPE_BASE_BRANCH_PROMPT = (
    "Base branch: {base_branch} "
    "(scope is judged on `git diff {base_branch}...HEAD`; "
    "do NOT `git checkout {base_branch} -- <path>`, revert only "
    "your own out-of-scope commits/edits)"
)
DIFF_SCOPE_ALLOWED_PATHS_PROMPT = "Allowed paths (complete list): {allowed_paths}"
DIFF_SCOPE_HARNESS_PATHS_PROMPT = (
    "Plus harness artifacts (kstrl's own files, already in "
    "scope, no need to widen allowedPaths): {harness_paths}"
)
DIFF_SCOPE_VIOLATIONS_PROMPT = "Files outside allowed scope:\n{violations}"
DIFF_SCOPE_TRUNCATION_PROMPT = "  ... and {count} more"


def _diff_scope_details(
    base_branch: str,
    allowed_paths: list[str],
    harness_paths: list[str] | None,
    violations: list[str],
) -> list[str]:
    """Failure details for a diff that left its scope.

    R0.4: name the base branch and the FULL allowed-paths list. Without
    them the retry agent has to guess both; the recorded e2e run guessed
    `main` as base and reverted base-branch content with `git checkout
    main -- ...`, failing again. Base branch and allowed paths are single
    detail entries at the head of the list so
    ``VerificationResult.as_context()``'s ``details[:10]`` slice carries
    them into the retry prompt verbatim.

    #264: the harness carve-out is its own entry, never folded into the
    authored list. The operator has to be able to read what THEY
    authorised, and the retry agent has to know its own PRD and progress
    log are already in scope - telling it to stop writing those is the
    one instruction it cannot obey and still pass ``prd_stories``.
    """
    shown = violations[:15]
    violation_lines = [f"  - {v}" for v in shown]
    if len(violations) > len(shown):
        violation_lines.append(
            DIFF_SCOPE_TRUNCATION_PROMPT.format(count=len(violations) - len(shown))
        )
    harness_note = (
        [DIFF_SCOPE_HARNESS_PATHS_PROMPT.format(harness_paths=", ".join(harness_paths))]
        if harness_paths
        else []
    )
    return [
        DIFF_SCOPE_BASE_BRANCH_PROMPT.format(base_branch=base_branch),
        DIFF_SCOPE_ALLOWED_PATHS_PROMPT.format(allowed_paths=", ".join(allowed_paths)),
        *harness_note,
        # One multi-line entry so as_context()'s details[:10] slice
        # cannot drop violations or the truncation marker.
        DIFF_SCOPE_VIOLATIONS_PROMPT.format(violations="\n".join(violation_lines)),
    ]


#: The Phase 1 check name for "no trustworthy scope could be read".
#:
#: Deliberately NOT ``scope_source``, which is already taken in the same
#: substrate: ``events.ComponentScopeResolved.scope_source`` is a
#: payload FIELD naming which authority supplied a component's
#: allowlist (component_prd / run_flag / unconstrained / unresolved).
#: A check of that name reaches the same ``events.jsonl`` as a VALUE in
#: ``VerificationResultEvent.checks``, so one token would carry two
#: unrelated meanings for the dashboards that read that file.
SCOPE_UNREADABLE_CHECK = "scope_unreadable"


#: Opening words of the failure recorded when a component is refused for
#: an unreadable scope. Load bearing twice over, so it is a constant
#: rather than a literal: ``evolution._classify_check`` matches on it to
#: recover the check name from a manifest written by an earlier process,
#: and it is what an operator sees first in the inbox, the notification
#: and ``comp.error``.
SCOPE_UNREADABLE_ERROR_PREFIX = "Component scope could not be read; retrying cannot change it"


def scope_unreadable_error(cause: str) -> str:
    """The recorded error for an unreadable scope, carrying its cause.

    ``pipeline.fail`` writes this to ``comp.error``, the
    ``ComponentFailed`` event, ``notify.fire_first_failure`` and the
    HALTED_RUN inbox item's detail. A fixed string left all four saying
    only THAT the scope was unreadable, while the file to restore sat in
    the check's details, where none of them look.
    ``factory._preflight_component_scope`` names the file in its own
    refusal; every refusal for this cause should read alike.
    """
    return f"{SCOPE_UNREADABLE_ERROR_PREFIX}. {cause}"


#: Rendered in place of an empty ``allowed_paths_error``. A fail-closed
#: check must not pass on an ambiguous sentinel (round 2), and it must
#: not refuse while naming no cause either (round 1). It refuses, and
#: says the cause is missing.
NO_CAUSE_RECORDED = "(no cause recorded; the scope resolver supplied an empty error)"

#: H3 (#303): fragments check_scope_unreadable assembles; versioned as one
#: body (docs/adversarial-roadmap.md, H3a sweep row).
SCOPE_UNREADABLE_PROMPT_VERSION = "1.0.0"

SCOPE_UNREADABLE_EXPLANATION_PROMPT = (
    "The allowedPaths this component must be judged against "
    "could not be established before the run started, so no "
    "diff can be proven in-scope. This is NOT a diff violation, "
    "and NOT something an engineer can fix from inside the "
    "worktree: the scope is read from the pre-run checkout, "
    "outside this worktree, and is fixed for the life of the "
    "run, so neither narrowing nor widening the diff changes "
    "this verdict."
)
SCOPE_UNREADABLE_REMEDY_PROMPT = (
    "The Error line above names which of two faults this is. A "
    "pre-run PRD that would not read or parse: restore that "
    "file in the main checkout and start a new run. No "
    "plan-time scope resolved for this component at all: the "
    "PRD is not the problem, the manifest and the run's "
    "resolved scope disagree about which components exist, and "
    "that is a harness fault to report rather than a file to "
    "repair. A run-wide --allowed-paths fixes neither: scope "
    "resolution refuses before it reaches the flag, so a re-run "
    "with it set fails identically."
)


def check_scope_unreadable(allowed_paths_error: str) -> CheckResult:
    """Report that no trustworthy scope could be established (R1.5, #294).

    Fails CLOSED: no allowlist could be read, so no diff can be proven
    in-scope, and silently skipping the guard is the hole R1.5 exists to
    close. Distinct from ``allowed_paths=None`` reaching
    ``check_diff_scope``, which means no scope was CONFIGURED -- a
    legitimate pass.

    Its own check, and not a branch of ``diff_scope``, because the two
    name different faults and the name is what a reader acts on (#294).
    ``diff_scope`` means "the diff touched files outside the allowlist",
    so its retry context is read as "narrow the diff". Here there was no
    allowlist to be outside of: it is resolved once at plan time from
    the pre-run checkout (``scope.ComponentScope``), which is OUTSIDE
    every worktree and fixed for the life of the run, so nothing the
    engineer writes can move this verdict.

    TWO producers, with different remedies, which is why the text points
    at the ``Error:`` line rather than asserting a cause:

    - ``ComponentScope.resolve`` could not read or parse the component's
      pre-run PRD. Restore that file.
    - ``RunScope.for_component`` had no snapshot for the component at
      all and returned its fail-closed stand-in. The PRD is fine; the
      manifest and the resolved run scope disagree about which
      components exist, which is a harness fault.

    An earlier version asserted the first cause unconditionally, so on
    the second it sent an operator to inspect a file that reads
    perfectly. That is round-1 finding 1 again: a remediation naming an
    action that cannot fix the failure.

    Neither remedy is ``--allowed-paths``. ``resolve`` returns
    ``unresolved`` BEFORE it consults the run-wide flag, on the argument
    that a scope nobody could read is not a scope that does not exist,
    so a run restarted with the flag hits the identical refusal.

    Carries an infrastructure ``Finding`` because this is the harness
    failing to establish its own input, not a judgement about the
    change. Without it a run that dies here leaves an empty finding
    stream, and every consumer using ``len(findings) == 0`` as "ran
    cleanly" reads a hard stop as clean.

    Whether it runs at all is ``_scope_checks``'s decision, and it is
    ungated there.
    """
    start = time.monotonic()
    cause = allowed_paths_error or NO_CAUSE_RECORDED
    return CheckResult(
        name=SCOPE_UNREADABLE_CHECK,
        # #227: this row is a refusal about an INPUT nobody could read, so
        # it measured nothing about the diff. `passed` is untouched and the
        # gate still fails closed; what `measured=False` buys is that the
        # signature never enters a baseline, so repairing the harness is not
        # reported as a fix and the check leaving `measured_checks` is not
        # reported as a check that stopped.
        passed=False,
        measured=False,
        message="Scope could not be read at plan time; failing closed",
        details=[
            f"Error: {cause}",
            SCOPE_UNREADABLE_EXPLANATION_PROMPT,
            SCOPE_UNREADABLE_REMEDY_PROMPT,
        ],
        findings=[
            Finding.infrastructure_error(
                "verify",
                f"component scope could not be established at plan time: {cause}",
            )
        ],
        duration_seconds=time.monotonic() - start,
    )


def check_diff_scope(
    cwd: Path,
    base_branch: str,
    allowed_paths: list[str] | None = None,
    *,
    harness_paths: list[str] | None = None,
) -> CheckResult:
    """Check that git diff is within expected scope.

    One question only: did the diff touch a file outside the allowlist?
    The allowlist not being READABLE is a different fault with a
    different audience, and it is ``check_scope_unreadable`` (#294).

    It no longer carries PRD TAMPERING either. That refusal moved to
    ``check_prd_stories`` when the plan-time snapshot took the scope
    question away from the worktree PRD: the file can still be rewritten
    and the stories still have to be defended, but the scope this check
    enforces is not something the rewrite can reach any more, so saying
    "scope could not be established" about it was untrue.

    ``harness_paths`` (#264) is kstrl's OWN per-component carve-out from
    ``config.component_harness_paths``: exact files kstrl's other checks
    require the agent to write (its PRD, its progress log, the codebase
    map). They widen the effective scope but are reported SEPARATELY, so
    the failure message still shows the operator what they authorised.
    They never create a scope where none was configured: with
    ``allowed_paths`` unset the check still passes unconditionally.

    Keyword-only, because #294 deleted an ``allowed_paths_error``
    parameter that sat in the 4th positional slot and this argument
    would otherwise have inherited it. Measured on the intermediate
    version: an unported caller passing the error string positionally
    got ``passed=True`` / "No scope constraints" where it intended a
    hard refusal, and with a non-empty ``allowed_paths`` the string
    splatted character by character into the effective allowlist. A
    silent fail-open is the one failure mode this check exists to
    prevent.
    """
    start = time.monotonic()

    if not allowed_paths:
        # #227: a VACUOUS pass. It reads no diff and applies no rule, so it
        # proves nothing about scope. `ks check` with no --allowed-path takes
        # this branch every time, and with measured=True it cleared: measured
        # on the head of #357, a baseline carrying
        # `diff_scope:files-outside-allowed-scope-diff-vs-base-branch` was
        # reported FIXED by a run that never looked.
        return CheckResult(
            name="diff_scope",
            passed=True,
            message="No scope constraints (allowed_paths not set)",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    # #264: the authored scope plus kstrl's own per-component files. The
    # two lists stay separate all the way into the failure details: an
    # operator reading "outside allowed scope" must be able to tell what
    # they authorised from what the harness added on their behalf.
    #
    # Deliberately NOT guards.check_violations, which is the same
    # decision on the same inputs: it takes a set and returns sorted, and
    # the violation list is truncated to 15 for the retry prompt, so
    # sorting silently changes WHICH violations the retry agent is shown.
    # Git's order is the order the operator sees elsewhere; a cosmetic
    # de-duplication is not worth moving it.
    effective = [*allowed_paths, *(harness_paths or ())]
    try:
        changed = git.get_diff_names(base_branch, cwd)
        # #435: name the ref the diff was actually judged against.
        # get_diff_names resolved it; saying "main" while measuring
        # origin/main sends the engineer to revert against the wrong tree.
        base_label = git.resolve_base_ref(base_branch, cwd)
        violations = [f for f in changed if not path_is_allowed(f, effective)]
    except git.GitDiffError as exc:
        # The lenient reader raises for exactly one family: a diff git
        # produced and this process cannot decode (#416). Everything else it
        # still answers with [], which the vacuous-pass branch below handles.
        # Failing closed here rather than falling into that branch is the
        # point: an undecodable diff is not an empty one.
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                "diff scope could not read the diff; failing closed "
                "(infrastructure error, not a scope pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "verify",
                    f"diff scope could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    if not changed:
        # The other vacuous pass, and the one round 1 of #357 missed: the rule
        # exists but there is nothing to apply it to. Round 2 of review
        # measured the two diff-driven checks side by side on one empty diff
        # and found them disagreeing - `diff_scope` measured, `bad_patterns`
        # did not - so an adopter who sets --allowed-path had every
        # `diff_scope` baseline signature CLEARED by a pull request whose diff
        # touched none of the allowed globs.
        return CheckResult(
            name="diff_scope",
            passed=True,
            message=NO_FILES_IN_THE_DIFF,
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    if violations:
        details = _diff_scope_details(
            base_label,
            allowed_paths,
            harness_paths,
            violations,
        )
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                f"{len(violations)} files outside allowed scope "
                f"(diff vs base branch '{base_label}')"
            ),
            details=details,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="diff_scope",
        passed=True,
        message=f"{len(changed)} files, all within scope",
        duration_seconds=time.monotonic() - start,
    )


#: The ``bad_patterns`` row's words when the ``[policy]`` envelope runs and
#: owns the secret rule (#646 slice 4).
SECRETS_CHECKED_BY_ENVELOPE = "secrets: checked by policy_envelope"


def _bad_patterns_message(
    issues: Sequence[str],
    changed: Sequence[str],
    *,
    added_any: bool,
    secrets_owned_by_envelope: bool,
) -> str:
    """The ``bad_patterns`` row's message: what the secret rule read, and what it found."""
    if issues:
        return f"{len(issues)} issues found in changed files"
    if not changed:
        # The same sentence as check_diff_scope when the cause is the same,
        # so an operator reading two unmeasured rows in one report does not
        # have to work out whether two spellings mean one fact.
        return NO_FILES_IN_THE_DIFF
    if secrets_owned_by_envelope:
        return SECRETS_CHECKED_BY_ENVELOPE
    if not added_any:
        return f"secrets: the {len(changed)} changed files add no lines, nothing scanned"
    return f"secrets: scanned the lines added to {len(changed)} changed files, no issues"


def check_bad_patterns(
    cwd: Path,
    base_branch: str,
    secret_patterns: Sequence[str] = DEFAULT_SECRET_PATTERNS,
    *,
    secrets_owned_by_envelope: bool = False,
) -> CheckResult:
    """Scan the lines this branch added for a secret.

    One rule, the same for every file whatever its language (#696 slice 8
    removed the empty-file and syntax-error rules, which read one source
    language). Which files add a secret is a property of the DIFF, not of
    a file (#399 simplify pass): it is computed once, by intersecting the
    lines this branch ADDED with ``secret_patterns`` via
    ``policy._scan_secrets`` - the same function ``check_policy_envelope``
    evaluates its own ``[policy] secret_patterns`` through - one rule, not
    two copies of it. The result is keyed by PATH, which is safe because
    ``policy.parse_added_lines`` (#399 addendum) unquotes a git-quoted path
    before comparing it to ``git diff --name-status``'s own, unquoted
    spelling of the same file. The caller passes the envelope's own
    ``PolicyConfig.secret_patterns`` when it has one; ``PolicyConfig.load``
    reads that field unconditionally, whether or not ``[policy] enabled``
    is true, so a stock install (no config at all) keeps this default,
    which is that same list.

    ``secrets_owned_by_envelope`` (#646 slice 4) is true when the
    ``[policy]`` envelope runs in the same verification. This check then
    neither reads the diff's added lines nor scans them for secrets:
    ``check_policy_envelope`` reports a secret once, as a finding an inbox
    approval can waive, and this row says
    ``secrets: checked by policy_envelope`` and measured nothing. False,
    the default, keeps the secret rule here, with no approval path.
    """
    start = time.monotonic()

    # #399: which changed files add a secret. The rule reads the added
    # lines of EVERY changed file (#619). One name-status call: the
    # comprehension below is `git.get_diff_names`' dedupe written out.
    # `get_diff_name_status` is inside the try as of #414: it is lenient
    # about a diff git could not produce but raises on one it could not
    # DECODE, and outside the try that left this blocking gate as a
    # traceback (PR #419 handoff 1).
    try:
        records = git.get_diff_name_status(base_branch, cwd)
        changed = list(dict.fromkeys(path for _, path in records if path))
        # get_diff_name_status is LENIENT (returns [] on a git failure, not
        # only on a genuinely empty diff), but get_diff_content is not: it
        # raises. Reading content only when there is a changed-file list
        # keeps that lenient behaviour for "no diff at all" (#619): a
        # worktree with no git repository reaches the vacuous pass below
        # rather than the exception clause. `as_stored` (#695): every
        # changed byte, so a file git treats as binary is read too.
        added = (
            []
            if secrets_owned_by_envelope or not changed
            else parse_added_lines(git.get_diff_content(base_branch, cwd, as_stored=True))
        )
        secret_hit_paths = frozenset(_scan_secrets(added, secret_patterns))
    except Exception as exc:
        # Exception exactly, broad clause last (#318). get_diff_name_status
        # is LENIENT, so the file list can arrive when the diff does not; at
        # least two unrelated families are measured reaching here: a
        # GitDiffError, and a UnicodeDecodeError (a ValueError) from a
        # diff this process could not decode. A misconfigured secret
        # pattern (PolicyConfigError, also a ValueError, raised inside
        # `_scan_secrets`) reaches the same clause for the same reason:
        # this check runs by default, so a bad regex must fail this row
        # closed rather than crash the whole verification run. The row
        # fails CLOSED, so a swallow costs a visible red gate, never a
        # silent pass.
        return CheckResult(
            name="bad_patterns",
            passed=False,
            message=(
                "bad patterns could not read the diff; failing closed "
                "(infrastructure error, not a scan pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "verify",
                    f"bad patterns could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    issues = [
        f"{path}: possible secret/credential detected"
        for path in changed
        if path in secret_hit_paths
    ]
    message = _bad_patterns_message(
        issues,
        changed,
        added_any=bool(added),
        secrets_owned_by_envelope=secrets_owned_by_envelope,
    )
    return CheckResult(
        name="bad_patterns",
        passed=not issues,
        message=message,
        details=issues,
        duration_seconds=time.monotonic() - start,
        # #227: a scan that read nothing is a vacuous pass. It cannot prove
        # a secret went away. The rule reads the diff's added lines, so an
        # added line in any file counts.
        measured=bool(added),
    )


#: H3 (#303): the fragment check_policy_envelope assembles into its
#: diff-unreadable refusal text.
POLICY_ENVELOPE_PROMPT_VERSION = "1.0.0"

POLICY_DIFF_UNREADABLE_PROMPT = (
    "The change cannot be proven within policy; do not treat this as permission to merge."
)


def check_policy_envelope(
    cwd: Path,
    base_branch: str,
    config: PolicyConfig,
    *,
    waivers: Waivers | None = None,
) -> CheckResult:
    """R8.1: enforce the declarative ``[policy]`` envelope from artifacts.

    Reads the git diff, never agent self-report.
    Fails CLOSED on any infrastructure error (diff unreadable, malformed
    policy) and on any envelope violation. Enforcement-machinery edits
    are a non-overridable halt. Violation details are packed as
    individual entries so ``VerificationResult.as_context()``'s
    ``details[:10]`` slice carries them into the retry prompt.
    """
    start = time.monotonic()
    # All three reads are strict: each is a SEPARATE git subprocess, so a
    # successful content read proves nothing about the two that follow.
    # A lenient read returns [] on timeout/nonzero exit, which the
    # evaluator cannot distinguish from "nothing changed" - the change
    # would then satisfy every path and size rule vacuously. `as_stored`
    # (#695): every changed byte, so a file git treats as binary is read too.
    try:
        diff_text = git.get_diff_content(base_branch, cwd, as_stored=True)
        changed = git.get_diff_names(base_branch, cwd, strict=True)
        numstat = git.get_diff_numstat(base_branch, cwd, strict=True)
    except git.GitDiffError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message=(
                "policy envelope could not read the diff; failing closed "
                "(infrastructure error, not a policy pass)"
            ),
            details=[
                f"Error: {exc}",
                POLICY_DIFF_UNREADABLE_PROMPT,
            ],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    try:
        evaluation = evaluate_policy(changed, numstat, diff_text, config)
    except PolicyConfigError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message="policy envelope is misconfigured; failing closed",
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope is misconfigured: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except Exception as exc:
        # Exception exactly, broad clause last (#318). evaluate_policy calls
        # policy.parse_added_lines on the diff text this function already
        # read, and a diff header path holding bytes that are not valid
        # utf-8 makes that raise UnicodeDecodeError - a ValueError, and
        # neither a GitDiffError (the diff itself DID read) nor a
        # PolicyConfigError (the config is fine). The row fails CLOSED
        # rather than let the exception escape the check (#399 blocker 1b).
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message=(
                "policy envelope could not evaluate the diff; failing closed "
                "(infrastructure error, not a policy pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope could not evaluate the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    findings = [
        Finding.policy_violation(
            category=v.category,
            explanation=v.explanation,
            location=v.location,
            severity=v.severity,
            suggestion=v.suggestion,
        )
        for v in evaluation.violations
    ]
    # #595: an approved inbox item waives the one finding it covers, and
    # it does so HERE, before `blocking` is computed, so this check stays
    # the only place a policy finding becomes blocking.
    findings, waived, refusals = apply_waivers(findings, waivers)
    blocking, advisories, details = _after_waivers(findings, refusals)
    note = waiver_note(waived, refusals)

    if not blocking:
        message = evaluation.summary if evaluation.ok else "policy envelope satisfied after waivers"
        if advisories:
            message += f"; {len(advisories)} advisory(ies)"
        message += note
        return CheckResult(
            name="policy_envelope",
            passed=True,
            message=message,
            details=details,
            findings=findings,
            duration_seconds=time.monotonic() - start,
        )
    message = f"{len(blocking)} policy violation(s)"
    if evaluation.machinery_hit:
        message += " including enforcement-machinery halt"
    message += note
    return CheckResult(
        name="policy_envelope",
        passed=False,
        message=message,
        details=details,
        findings=findings,
        duration_seconds=time.monotonic() - start,
    )


def _after_waivers(
    findings: list[Finding], refusals: list[str]
) -> tuple[list[Finding], list[Finding], list[str]]:
    """``(blocking, advisories, details)`` once waivers are applied (#595).

    ``advisories`` leaves out waived findings, so a waiver is not counted
    as an advisory. ``details`` puts blocking findings first, because
    ``as_context()`` slices ``details[:10]`` into the retry prompt and
    nothing may crowd out a real failure, then the refusal reasons, the
    waived findings and the advisories.
    """
    blocking = [f for f in findings if f.severity != "advisory"]
    waived = [f for f in findings if f.severity == "advisory" and finding_waiver(f) is not None]
    advisories = [f for f in findings if f.severity == "advisory" and finding_waiver(f) is None]
    details = [f.explanation for f in blocking] + refusals
    details += [f.explanation for f in waived] + [f.explanation for f in advisories]
    return blocking, advisories, details


def _scope_checks(
    cwd: Path,
    base_branch: str,
    *,
    allowed_paths: list[str] | None,
    allowed_paths_error: str | None,
    harness_paths: list[str] | None,
    compare: bool,
) -> list[CheckResult]:
    """The scope checks Phase 1 appends, at most one of two.

    An unreadable scope source and an out-of-scope diff are alternatives
    rather than a check with a mode (#294), so the choice is made once,
    here, instead of inside a check that would then be named for the
    wrong one of them:

    - ``allowed_paths_error`` non-empty: ``scope_unreadable`` alone,
      UNGATED. The comparison is not merely turned off, it is
      unavailable - there is no trustworthy allowlist to compare
      against - so running ``check_diff_scope`` too would report a PASS
      ("no scope constraints") beside the refusal, which is the
      fail-open reading of the same state. The error wins even when a
      caller also supplies a list: a half-loaded state must not be
      judged on paths that may be stale.

      ``is not None``, not truthiness. Both review rounds hit this from
      opposite sides and both were right about the defect: truthiness
      lets an empty-string sentinel PASS a ``diff_scope`` that had no
      allowlist to compare, which is a fail-open in the one check whose
      job is to fail closed; ``is not None`` alone refused while naming
      no cause, rendering the bare "Error: ". Neither problem requires
      the other. This refuses on any non-None value and
      ``check_scope_unreadable`` substitutes
      :data:`NO_CAUSE_RECORDED` for the empty one, so an ambiguous
      sentinel is never read as permission and the refusal always says
      something. ``ComponentScope.resolve`` never produces "", but
      ``run_mechanical_verification`` is a public entry point.
    - otherwise ``diff_scope``, gated on ``compare``, which is
      ``[verify] check_diff_scope`` and nothing else. The one flag
      rather than the whole ``VerifyConfig``: this is the only field
      the decision reads, and the two ``list[str] | None`` arguments
      beside it are keyword-only so a transposition of the authored
      allowlist and the harness carve-out cannot type-check clean.

    Returns a list rather than taking the branch in
    ``run_mechanical_verification``: that function is already over the
    cyclomatic ratchet and is judged against its own previous value, so
    an ``if``/``elif`` there is a refusal at commit time.
    """
    if allowed_paths_error is not None:
        return [check_scope_unreadable(allowed_paths_error)]
    if compare:
        return [
            check_diff_scope(
                cwd,
                base_branch,
                allowed_paths,
                harness_paths=harness_paths,
            )
        ]
    return []
