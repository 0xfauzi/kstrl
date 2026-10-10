"""Phase 1: Mechanical verification - independent checks after agent execution."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from kstrl.config import component_progress_path
from kstrl.policy import (
    DEFAULT_SECRET_PATTERNS,
    PolicyConfig,
)
from kstrl.prd import PRD
from kstrl.verify_commands import _command_gates
from kstrl.verify_diff import _scope_checks, check_bad_patterns, check_policy_envelope
from kstrl.verify_model import (
    LAYER0_NOT_MEASURED,
    CheckResult,
    NotMeasured,
    VerificationResult,
    VerifyConfig,
    gate_names,
)
from kstrl.waivers import Waivers

# Engineer prompt mandates the EXACT heading `## Self-Critique`.
# Accept also `- **Self-Critique:**` (common bullet-in-list form) and
# `## Self Critique` (loose hyphen-space variant). Reject prose like
# "the self-critique above" so we don't false-positive on body text.
# Both forms must START the line after at most a list marker + whitespace.
_SELF_CRITIQUE_HEADING_RE = re.compile(
    r"""^
    (?:
        \#{2,3}\s+                  # H2 / H3: '## ' or '### '
      | [\-*]\s+\*{2}\s*            # '- **' or '* **'
    )
    Self[-\s]Critique
    (?:
        \s*\*{2}                    # '**' (close bold)
      | \s*:                        # ':'
      | \s*\*{2}\s*:                # '**:'
      | \s*$                        # end-of-line
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An iteration entry boundary in progress.txt. The engineer prompt's
# documented format starts each appended entry with
# `## [YYYY-MM-DD] - [Story ID]`; agents also commonly write
# `## Iteration N`. Exactly two hashes: H3 sub-headings inside an
# entry must not be mistaken for a new entry.
_ITERATION_HEADING_RE = re.compile(
    r"""^\#\#\s+
    (?:
        \[?\d{4}-\d{2}-\d{2}        # '## [YYYY-MM-DD] - ...' (documented form)
      | Iteration\b                 # '## Iteration N' (loose variant)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An UNINDENTED bullet opening with a closed bold label, e.g.
# `- **Learnings:**` or `- **Interpretations** (only if ...): ...`.
# In the engineer prompt's entry format these are sibling sections of
# `- **Self-Critique:**`, so one of them terminates the bullet count.
# Applied to the raw line: the Self-Critique block's own nested bullets
# are indented and therefore never match.
_SECTION_BULLET_RE = re.compile(r"^[\-*]\s+\*{2}[^*]+\*{2}")

# Thematic break: the engineer prompt's entry format ends each entry
# with `---`.
_ENTRY_SEPARATOR_RE = re.compile(r"^-{3,}$")


def latest_entry(lines: Sequence[str]) -> int | None:
    """The index of the line that starts the latest iteration entry of a
    progress log, or None when no line is an iteration heading.

    Entries are appended, so the LAST iteration heading starts the
    current iteration's entry. The one definition of "this iteration's
    entry": the self-critique check reads it, and so does the acceptance
    dispute (``kstrl.acceptance_record.read_dispute``, #700 slice 10a).
    """
    for index in range(len(lines) - 1, -1, -1):
        if _ITERATION_HEADING_RE.match(lines[index]):
            return index
    return None


def _self_critique_text(progress_path: Path, start: float) -> str | CheckResult:
    """The progress file's text, or the failing check explaining why not.

    Two handlers because the remedies differ: "could not read" sends the
    operator to the file's permissions, and this file opened fine - the
    agent wrote bytes that are not UTF-8, which is a fact about the
    agent's output rather than about the disk. Before #320 the decode was
    not caught at all and a Phase 1 gate died with a traceback instead of
    reporting red.

    Split out of :func:`check_self_critique` so the second handler does
    not push that function past the complexity ratchets.
    """
    try:
        return progress_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Could not read progress file: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except UnicodeDecodeError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Progress file is not valid UTF-8: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )


def check_self_critique(
    progress_path: Path,
    min_bullets: int = 3,
) -> CheckResult:
    """Confirm the CURRENT (latest) progress.txt entry contains a
    Self-Critique block with at least ``min_bullets`` bullet points.

    Shape check only (H4): this verifies that a Self-Critique block of
    the right shape exists in the right place. It does NOT verify the
    substance of the bullets - vacuous-but-plausible failure modes
    pass. Substance is the reviewer's job.

    Format assumption (from the engineer prompt's Progress Format):
    each iteration appends an entry starting with an H2 heading of the
    form `## [YYYY-MM-DD] - [Story ID]` (the loose `## Iteration N`
    variant is also recognized), containing `- **Self-Critique:**` (or
    `## Self-Critique`) followed by bullets, sibling bold-label
    sections such as `- **Interpretations:**`, and a closing `---`.

    The check first locates the latest iteration boundary (the LAST
    line matching ``_ITERATION_HEADING_RE``), then requires a
    Self-Critique heading within that entry - a block written by an
    EARLIER iteration does not satisfy the check for the current one.
    If no iteration heading exists anywhere, the whole file is treated
    as a single entry (fallback for free-form progress files; per-
    iteration association is not possible there).

    Bullet counting stops at the next `##` heading, a `---` entry
    separator, or an unindented bold-label bullet (a sibling section
    like `- **Interpretations:**`), so bullets belonging to later
    sections do not inflate the count. Consequence of the format
    assumption: critique bullets themselves must either be indented
    under the `- **Self-Critique:**` bullet (the documented format) or
    not open with a bold label, otherwise they read as a sibling
    section and the check fails loudly rather than over-counting.

    Without this mechanical check, the engineer prompt's mandate to
    list >=3 failure modes can silently rot - the only enforcement
    path otherwise is the reviewer noticing, which is unreliable.
    """
    start = time.monotonic()
    text = _self_critique_text(progress_path, start)
    if isinstance(text, CheckResult):
        return text

    lines = text.splitlines()
    found = latest_entry(lines)
    entry_start, entry_found = (0, False) if found is None else (found, True)

    # Find the LAST self-critique heading WITHIN the latest entry, so
    # an earlier iteration's block cannot satisfy the current one and
    # repeated blocks inside one entry resolve to the newest.
    heading_idx: int | None = None
    for i in range(len(lines) - 1, entry_start - 1, -1):
        if _SELF_CRITIQUE_HEADING_RE.match(lines[i]):
            heading_idx = i
            break

    if heading_idx is None:
        where = (
            f"in the latest iteration entry (line {entry_start + 1}: "
            f"{lines[entry_start].strip()[:60]!r})"
            if entry_found
            else "in progress file"
        )
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"No '## Self-Critique' block found {where}. "
                "Engineer prompt mandates >=3 failure-mode bullets "
                "before declaring done."
            ),
            duration_seconds=time.monotonic() - start,
        )

    # Count bullets after the heading until the entry's content ends:
    # next `##` heading, `---` separator, or a sibling bold-label
    # bullet section (e.g. `- **Interpretations:**`).
    bullet_count = 0
    bullet_lines: list[str] = []
    for line in lines[heading_idx + 1 :]:
        stripped = line.strip()
        # Stop at next major heading
        if stripped.startswith("##"):
            break
        # Stop at the entry separator
        if _ENTRY_SEPARATOR_RE.match(stripped):
            break
        # Stop at the next sibling section: an UNINDENTED bold-label
        # bullet (matched on the raw line so the block's own indented
        # bullets never terminate the count).
        if _SECTION_BULLET_RE.match(line):
            break
        # Count substantive bullets (require non-trivial content after the marker)
        if stripped.startswith("- ") or stripped.startswith("* "):
            body = stripped[2:].strip()
            if body and not body.lower().startswith(("tbd", "todo", "n/a")):
                bullet_count += 1
                bullet_lines.append(body[:80])

    if bullet_count < min_bullets:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"Self-Critique block has {bullet_count} bullets; minimum required is {min_bullets}"
            ),
            details=bullet_lines,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="self_critique",
        passed=True,
        message=f"{bullet_count} failure modes listed",
        duration_seconds=time.monotonic() - start,
    )


def _tamper_changes(prd: PRD, pre_run_prd_path: Path | None) -> list[str]:
    """How ``prd`` differs from the pre-run copy in ways no engineer may.

    Defence in depth for #264's carve-out, kept deliberately after #269
    made the SCOPE half of this comparison unnecessary. The plan-time
    snapshot (``kstrl.scope``) settles what a component may write, so an
    ``allowedPaths`` the agent edits is inert and is no longer compared:
    see that module for why comparing a value the agent can rewrite is
    the weaker answer.

    What the snapshot does NOT cover is everything else that reads this
    file, and a lot does: ``check_prd_stories`` below, the approved
    fixtures oracle, the acceptance criteria handed to the reviewer, the
    R10.3 claim check. None can be served from a snapshot, because
    the agent setting ``passes`` is the whole job, so the live file has
    to be trusted and a comparison is the only answer available for it.
    Drop this and an agent can delete an acceptance criterion or neuter
    an executable oracle and pass a gate it authored.

    WHICH fields are the engineer's to write is the PRD's own business
    and lives in ``PRD.tamper_changes``, beside the fields it pins.

    Empty when there is nothing to compare, which is not the same as
    waving something through:

    - ``pre_run_prd_path`` is None. The caller has no trustworthy copy
      to offer: ``ks check`` judges an operator's own working tree.
    - It is the SAME file, which is ``use_worktrees=False``: both reads
      return the same document and the comparison is empty by
      arithmetic rather than by a special case. That mode has no
      isolation boundary, so this check cannot be what gives it one.
      Scope is the part of the answer that does survive there, because
      #269 reads it before the agent starts.

    A pre-run copy that will not load raises what ``PRD.load`` raises,
    ``OSError`` or ``ValueError``, and ``check_prd_stories`` fails closed
    on it (#568). It was read once already, at plan time, so it went
    missing or changed during the run, and an empty comparison would be
    the mechanism removed without a word.
    """
    if pre_run_prd_path is None:
        return []
    return prd.tamper_changes(PRD.load(pre_run_prd_path))


#: H3 (#303): fragments check_prd_stories' tamper branch assembles;
#: versioned as one body (docs/adversarial-roadmap.md, H3a sweep row).
PRD_TAMPER_PROMPT_VERSION = "1.0.0"

PRD_TAMPER_FIELDS_PROMPT = (
    "It {changes}. A component may set `passes` "
    "and `notes` on its own stories and nothing else: it may "
    "not rewrite the criteria or the fixtures it is judged "
    "against."
)
PRD_TAMPER_GATES_PROMPT = (
    "Every gate that reads this file - these stories, the "
    "approved fixtures, the criteria the reviewer is given - "
    "is judging a document the component rewrote. Restore it "
    "to what the run started with; do not treat this as "
    "permission to change what the component is measured "
    "against."
)


def check_prd_stories(prd_path: Path, pre_run_prd_path: Path | None = None) -> CheckResult:
    """Re-read PRD from disk and verify all stories have passes=true.

    ``pre_run_prd_path`` (#269) is the copy of the same PRD the run
    started with, which lives outside every worktree and so is not
    agent-writable. Given one, this check also refuses a PRD the
    component rewrote in its own favour (``_tamper_changes``).

    This is the check that carries that refusal, rather than
    ``diff_scope``, for two reasons. It is a statement about the
    STORIES, which is what this check reads and what a rewrite attacks;
    scope stopped being the question when #269 made the plan-time
    snapshot the only scope source. And ``diff_scope`` is switchable off
    (``[verify] check_diff_scope``), while this one runs whenever there
    is a PRD at all: defence in depth an unrelated toggle can disable is
    not defence in depth.
    """
    start = time.monotonic()
    try:
        prd = PRD.load(prd_path)
    except Exception as exc:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"Failed to load PRD: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    try:
        tampered = _tamper_changes(prd, pre_run_prd_path)
    except (OSError, ValueError) as exc:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=(
                f"The PRD this run started from, {pre_run_prd_path}, could not be "
                f"read; failing closed: {exc}"
            ),
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    if tampered:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message="The PRD is not the one this run started with; failing closed",
            details=[
                PRD_TAMPER_FIELDS_PROMPT.format(changes="; ".join(tampered)),
                PRD_TAMPER_GATES_PROMPT,
            ],
            duration_seconds=time.monotonic() - start,
        )

    failing = [s for s in prd.user_stories if not s.passes]
    if failing:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"{len(failing)} stories not marked as passing",
            details=[f"{s.id}: {s.title}" for s in failing],
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="prd_stories",
        passed=True,
        message=f"All {len(prd.user_stories)} stories passing",
        duration_seconds=time.monotonic() - start,
    )


#: Every check :func:`run_mechanical_verification` appends that answers
#: its question by reading ``git diff <base>...HEAD``.
#:
#: Beside the function that appends them, because a caller that has no
#: measurable base has to know which checks that rules out, and deriving
#: the list by reading this module's source is how two callers end up
#: disagreeing about it.
DIFF_DEPENDENT_CHECKS: tuple[str, ...] = (
    "diff_scope",
    "bad_patterns",
    "policy_envelope",
)


def self_critique_progress_path(
    config: VerifyConfig,
    worktree_path: Path,
    prd_path: Path | None,
) -> Path | None:
    """The log ``check_self_critique`` would read, or None if it will not run.

    Read the log the engineer was actually pointed at: a factory
    component writes NEXT TO its PRD (the only location inside its
    allowedPaths), so resolving a repo-root default here would check a
    file that was never written and fail the component for the harness's
    own path confusion. An explicit config wins. ``prd_path`` is
    worktree-absolute at the factory call site, so the derived sibling is
    too; the join is a no-op for an absolute path and still anchors a
    relative one. With neither a PRD nor an explicit path there is no log
    to read, so the check is skipped rather than run against a path that
    cannot exist.

    Extracted (#288 review) because a caller has to be able to ask
    whether this check will run BEFORE the run, to say so: `ks feature`
    announces its report up front, and the announcement was silently
    wrong for an operator who had set ``require_self_critique``. Two
    copies of the rule is how the announcement and the run disagree, so
    there is one, and :func:`run_mechanical_verification` calls it too.
    """
    if not config.require_self_critique:
        return None
    if config.progress_file_path is not None:
        return worktree_path / Path(config.progress_file_path)
    if prd_path is not None:
        return worktree_path / component_progress_path(prd_path, None)
    return None


def run_undiffed_verification(
    worktree_path: Path,
    config: VerifyConfig,
) -> VerificationResult:
    """Mechanical verification over a tree with no base to diff against.

    The ONLY safe entry point for that case, and it is a function rather
    than a documented convention because :func:`narrow_to_undiffed`
    cannot deliver the guarantee its name promises (#288 review round
    2). Its ``replace`` reaches two of the three
    :data:`DIFF_DEPENDENT_CHECKS`; the third, ``policy_envelope``, is
    gated by ``policy_config``, a separate ARGUMENT to
    :func:`run_mechanical_verification`, and ``allowed_paths_error``
    outranks the ``check_diff_scope`` toggle entirely because
    :func:`_scope_checks` reads it first and appends the ungated
    ``scope_unreadable`` on any non-None value. So a second caller
    writing ``config=narrow_to_undiffed(cfg), policy_config=pc`` gets
    ``policy_envelope`` reporting a PASS over an empty diff: the exact
    defect the narrowing is named for, reintroduced by an argument the
    narrowing cannot see.

    This owns all of them. There is no parameter here for anything that
    consumes a diff, so the checks suppressed by config and the one
    suppressed by argument are suppressed the same way: by not being
    reachable.

    ``base_branch=""`` is the honest value for "there is no base here"
    and is never read, because nothing left running consumes one.
    ``prd_path=None`` skips the PRD-derived checks: ``prd_stories``
    re-reads a flag the agent itself set, which is a self-report rather
    than an independent measurement.

    The structural version of this - one object owning every argument
    that decides whether a check can honestly run - is tracked on #305.
    """
    return run_mechanical_verification(
        worktree_path=worktree_path,
        prd_path=None,
        base_branch="",
        allowed_paths=None,
        allowed_paths_error=None,
        config=narrow_to_undiffed(config),
    )


def narrow_to_undiffed(config: VerifyConfig) -> VerifyConfig:
    """``config`` with every :data:`DIFF_DEPENDENT_CHECKS` toggle off.

    Prefer :func:`run_undiffed_verification`, which owns the arguments
    this cannot reach. Exported on its own only because the announcement
    side of a report needs the narrowed config to say what will run.

    For a caller whose tree has no base it can honestly diff against -
    `ks feature` (#288), where nothing commits for the agent and the
    branch the loop checks out may BE the base branch, so
    ``base...HEAD`` is routinely empty and a diff-based check would
    report ``0 files, all within scope`` over work it never saw.

    An empty diff is indistinguishable from nothing changed: the lenient
    git helpers return an empty file list either way, and even
    ``get_diff_names(..., strict=True)`` returns ``[]`` without raising.
    So the only honest answer is not to run those checks, which is what
    this does.

    Note what it does NOT cover, because the toggles cannot. ``policy``
    is a separate config object and is suppressed by not being passed at
    all. And ``allowed_paths_error`` outranks ``check_diff_scope``
    entirely: :func:`_scope_checks` reads it first and, on ANY non-None
    value, appends :func:`check_scope_unreadable` instead, which is
    ungated by this config and fails closed by design (#294). So a caller
    relying on this narrowing must still leave that argument None, but
    for the opposite reason to the one that held before #294: the risk is
    no longer a ``diff_scope`` PASS over a diff it never saw, it is a
    hard scope_unreadable FAIL over a scope the caller never had.
    """
    return replace(config, check_diff_scope=False, check_bad_patterns=False)


class MechanicalVerification(Protocol):
    """The call shape of :func:`run_mechanical_verification` (#316).

    ``PipelineHooks.run_mechanical_verification`` was typed
    ``Callable[..., VerificationResult]``, and ``...`` means mypy checks
    NOTHING about the arguments - which matters because that hook is how
    the only call site carrying a real component's scope reaches the
    function. Measured on this branch: with the hook typed ``...``,
    swapping ``harness_paths=scope.harness_paths`` for
    ``harness_paths=scope.error`` - a ``str | None`` into a
    ``list[str] | None`` slot, an authored carve-out replaced by the
    snapshot's failure to read one - left ``mypy --strict`` reporting
    SUCCESS. With this Protocol the same swap is
    ``error: Argument "harness_paths" to "__call__" of
    "MechanicalVerification" has incompatible type "str | None";
    expected "list[str] | None"``.

    Making the arguments keyword-only stops a SLOT from being inherited
    silently; it cannot stop a wrong value being handed to the right
    name. Only a type can, and only if there is one.

    The defaults below are spelled as real values rather than the
    conventional ``= ...`` so that ``inspect.Signature`` equality can
    compare this to the function in one assertion; a Protocol that has
    drifted is worse than none, because it would type-check calls the
    function rejects. See
    ``test_the_protocol_says_exactly_what_the_function_says``.
    """

    def __call__(
        self,
        worktree_path: Path,
        prd_path: Path | None,
        base_branch: str,
        allowed_paths: list[str] | None,
        config: VerifyConfig,
        *,
        allowed_paths_error: str | None = None,
        harness_paths: list[str] | None = None,
        pre_run_prd_path: Path | None = None,
        policy_config: PolicyConfig | None = None,
        autonomy_level: int = 0,
        waivers: Waivers | None = None,
    ) -> VerificationResult: ...


def run_mechanical_verification(
    worktree_path: Path,
    prd_path: Path | None,
    base_branch: str,
    allowed_paths: list[str] | None,
    config: VerifyConfig,
    *,
    allowed_paths_error: str | None = None,
    harness_paths: list[str] | None = None,
    pre_run_prd_path: Path | None = None,
    policy_config: PolicyConfig | None = None,
    autonomy_level: int = 0,
    waivers: Waivers | None = None,
) -> VerificationResult:
    """Run all mechanical checks. All checks run even if earlier ones fail.

    Everything after ``config`` is keyword-only (#316), so an inserted
    parameter cannot shift a later argument into a slot that means
    something else - and three of the arguments here mean opposite
    things in near-identical types (see :class:`MechanicalVerification`,
    which covers the half that keyword-only does not). Cost: none. No
    caller passed any of them positionally.

    ``prd_path=None`` (R10.1, ``ks check``) skips the PRD-dependent
    checks: ``prd_stories`` and ``self_critique`` unless
    ``config.progress_file_path`` names the log explicitly (with no PRD
    there is no sibling to derive it from). Every other check runs
    exactly as it does with a real path.

    ``harness_paths`` (#264) is the per-component carve-out for kstrl's
    OWN files, forwarded to ``check_diff_scope``. It reaches the factory
    from the run's plan-time scope snapshot (``scope.RunScope``), which
    is also where ``allowed_paths`` comes from; ``ks check`` leaves both
    None because it judges an operator's diff, not a factory
    component's.

    ``allowed_paths_error`` (#269) is that snapshot reporting that it
    could not read the component's scope at all. It replaces the
    ``diff_scope`` comparison with ``scope_unreadable``, an ungated
    fail-closed refusal named for its own cause (#294) - see
    ``_scope_checks``. Any non-None value refuses, empty included. ``ks check`` never sets it: it
    has no plan-time snapshot, so its scope is whatever
    ``--allowed-paths`` gave it.

    ``pre_run_prd_path`` (#269) is the copy of ``prd_path`` the run
    started with, forwarded to ``check_prd_stories``, which fails closed
    on a PRD the component rewrote. Also None for ``ks check``: there is
    no pre-run copy to compare an operator's working tree against.

    ``autonomy_level`` 1 or above records :data:`LAYER0_NOT_MEASURED` in
    :attr:`VerificationResult.not_measured` (#696 decision 7). The R8.5
    roadmap made Layer 0 blocking from L1, and kstrl no longer has a
    mechanical Layer 0, so the gap says so where Layer 0 ran. At level 0
    the ladder is off and nothing asked for Layer 0, so nothing is
    recorded. See :class:`NotMeasured` for why a gap is beside
    ``checks`` rather than in it.
    """
    checks: list[CheckResult] = []
    not_measured: list[NotMeasured] = []
    # #399 addendum: the envelope's own secret_patterns, read whether or not
    # [policy] enabled is true (PolicyConfig.load populates the field
    # unconditionally), so check_bad_patterns and check_policy_envelope
    # enforce one rule.
    bad_patterns_secret_patterns = (
        policy_config.secret_patterns if policy_config is not None else DEFAULT_SECRET_PATTERNS
    )
    # #646 slice 4: one owner for the secret rule. When the envelope runs it
    # reports a secret, waivably; check_bad_patterns then does not scan for
    # one, so a secret is one finding and an approval of it can pass.
    envelope = policy_config if policy_config is not None and policy_config.enabled else None

    if prd_path is not None:
        checks.append(check_prd_stories(prd_path, pre_run_prd_path))

    gate_rows, gate_gaps = _command_gates(worktree_path, config, gate_names(config))
    checks.extend(gate_rows)
    not_measured.extend(gate_gaps)

    checks.extend(
        _scope_checks(
            worktree_path,
            base_branch,
            allowed_paths=allowed_paths,
            allowed_paths_error=allowed_paths_error,
            harness_paths=harness_paths,
            compare=config.check_diff_scope,
        )
    )

    if config.check_bad_patterns:
        checks.append(
            check_bad_patterns(
                worktree_path,
                base_branch,
                bad_patterns_secret_patterns,
                secrets_owned_by_envelope=envelope is not None,
            )
        )

    # R8.1 policy envelope: opt-in ([policy] enabled). When disabled the
    # check is not appended, so existing runs are unchanged.
    if envelope is not None:
        checks.append(
            check_policy_envelope(
                worktree_path,
                base_branch,
                envelope,
                waivers=waivers,
            )
        )

    # #696 decision 7: where Layer 0 ran, say that nothing measured it.
    if autonomy_level >= 1:
        not_measured.append(LAYER0_NOT_MEASURED)

    progress_path = self_critique_progress_path(config, worktree_path, prd_path)
    if progress_path is not None:
        checks.append(
            check_self_critique(
                progress_path,
                config.self_critique_min_bullets,
            )
        )

    # ``checks`` only: a check that measured nothing neither passes nor
    # fails the run (#306).
    passed = all(c.passed for c in checks)
    return VerificationResult(passed=passed, checks=checks, not_measured=not_measured)
