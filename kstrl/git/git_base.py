"""The base that a change is measured against: its branch, ref and sha."""

from __future__ import annotations

import subprocess
from pathlib import Path

from kstrl.git.git import DEFAULT_TIMEOUT, GitDiffError, _undecodable_message

# Network fetches get a longer budget than local plumbing calls.
FETCH_TIMEOUT = 120.0

# Long-lived branch names `detect_base_branch` tries, in order, when the
# remote has not recorded a default. `main` first because it is the
# modern default and a renamed repo keeps only that name; `master` next
# because it is what `git init` still produces when `init.defaultBranch`
# is unset (#259).
BASE_BRANCH_CANDIDATES = ("main", "master", "trunk", "develop")

# Answer when no candidate resolves. Deliberately still a guess, and the
# callers surface it as one.
BASE_BRANCH_FALLBACK = "main"

# Local plumbing, called on the way into a command.
_BASE_BRANCH_PROBE_TIMEOUT = 5.0

_ORIGIN_REF_PREFIX = "refs/remotes/origin/"
_ORIGIN_HEAD_REF = f"{_ORIGIN_REF_PREFIX}HEAD"

# The exact refs a candidate may live at, mapped back to its name.
# ``refs/heads/<name>`` and ``refs/remotes/origin/<name>`` are the two
# :func:`resolve_base_ref` consults, so asking about exactly these asks
# the question the consumers will ask. Frozen at import: the inputs are.
_BASE_BRANCH_REFS: dict[str, str] = {
    ref: name
    for name in BASE_BRANCH_CANDIDATES
    for ref in (f"refs/heads/{name}", f"{_ORIGIN_REF_PREFIX}{name}")
}


def detect_base_branch(cwd: Path) -> str:
    """Base branch of the repo at ``cwd``, asked of the repo itself (#259).

    Used by ``ks run`` (manifest base), ``ks decompose`` and
    ``ks factory`` (worktree base) and ``ks check`` (diff base).

    The ladder, and what each rung fails on:

    1. ``origin/HEAD``: the remote's own answer, so it outranks any
       guess. Only ``git clone`` and an explicit ``git remote set-head``
       ever write it, so a repo that grew a remote by hand does not have
       it, and neither does a plain ``git init``. It can also go stale
       when the remote renames its default branch.
    2. The first of ``main``, ``master``, ``trunk``, ``develop`` that
       exists. Fails when the project's long-lived branch is named
       something else (``release``, ``stable``), and picks the earlier
       name when a repo carries two of them mid-migration.
    3. The literal ``main``.

    All of it is ONE ``git for-each-ref`` over the candidates' two
    possible refs plus ``origin/HEAD``, which is what makes the whole
    ladder cost a single subprocess. Three measured properties of that
    command carry the rungs above:

    - It lists a ref only when the ref resolves, and a symbolic ref only
      when its target resolves. So a stale ``origin/HEAD`` naming a
      deleted branch is simply absent, which is rung 1's demotion, and
      a listed ``origin/HEAD`` needs no confirming call even when its
      target is outside the candidate set.
    - ``%(symref)`` reports ``origin/HEAD``'s target in the same
      listing, folding rung 1 in for free.
    - Asking for ``refs/heads/<name>`` and ``refs/remotes/origin/<name>``
      by exact path asks about BRANCHES. A bare-name ``rev-parse`` would
      also match a TAG (git's search order puts ``refs/tags`` ahead of
      ``refs/heads``), and a tag is not something to cut a worktree
      from. The listing is filtered on exact refnames because a pattern
      also matches at a slash boundary: with no ``main`` branch but a
      ``main/sub`` one, ``refs/heads/main`` matches ``refs/heads/main/sub``.

    Deliberately NOT a rung: the branch HEAD is on. It always resolves,
    so it would end the ladder every time, and diffing a branch against
    itself is empty - which the diff-scope and bad-pattern checks read
    as "nothing changed, all within scope". That converts a loud
    cannot-measure (`ks check` exit 2, naming ``--base``) into a silent
    green on a tree nobody measured, and cannot-measure is never a pass.
    A guess the caller reports as a guess beats a wrong answer delivered
    as a measurement.

    Any failure to ask git - no repo, git missing, timeout - falls back
    to ``main``; the caller can always override with a flag.

    Measured on macOS with a warm cache: one subprocess and 3.0 to
    4.0 ms in every repo shape, flat because the shape no longer decides
    how many calls run. The rung-at-a-time version this replaced took 2
    to 9 subprocesses and 8 to 31 ms, and its nine sequential 5 s
    timeouts stacked into a 45 s ceiling - which the decompose launch
    form would have paid on the UI thread.
    """
    try:
        listing = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname)%09%(symref)",
                _ORIGIN_HEAD_REF,
                *_BASE_BRANCH_REFS,
            ],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=_BASE_BRANCH_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return BASE_BRANCH_FALLBACK
    if listing.returncode != 0:
        return BASE_BRANCH_FALLBACK

    remote_default = ""
    present: set[str] = set()
    for line in listing.stdout.splitlines():
        refname, _, symref = line.partition("\t")
        symref = symref.strip()
        # "refs/remotes/origin/main" -> "main". Stripping the known
        # prefix rather than splitting on the last "/", because a remote
        # may well default to a slashed branch and rsplit turns
        # "release/2.0" into "2.0". The startswith is not decoration: an
        # unguarded removeprefix returns its input untouched, so an
        # origin/HEAD symrefing outside refs/remotes/origin/ (measured:
        # one pointing at refs/heads/dev) yielded the whole refname as
        # the answer. Every character in it passes validate_branch_name,
        # so it reached the manifest and `gh pr create --base` before
        # anything noticed. Leaving remote_default empty demotes it to
        # the candidate rung instead.
        if refname == _ORIGIN_HEAD_REF and symref.startswith(_ORIGIN_REF_PREFIX):
            remote_default = symref.removeprefix(_ORIGIN_REF_PREFIX)
        elif refname in _BASE_BRANCH_REFS:
            present.add(_BASE_BRANCH_REFS[refname])

    if remote_default:
        return remote_default
    for name in BASE_BRANCH_CANDIDATES:
        if name in present:
            return name
    return BASE_BRANCH_FALLBACK


def resolve_base_branch(base_branch: str | None, cwd: Path) -> str:
    """An explicit base branch, else the one :func:`detect_base_branch` finds.

    One spelling for the "the flag wins, otherwise ask the repo" rule
    every entry point needs: the two CLI commands that take
    ``--base-branch`` and ``ks check``'s ``--base`` (#259).
    """
    return base_branch or detect_base_branch(cwd)


def base_ref_candidates(base_branch: str) -> tuple[str, ...]:
    """The refs a base branch may live at, in PRECEDENCE order.

    ``origin/<base>`` first (R0.2: squash merges rewrite SHAs, so a
    stale local base produces phantom diffs), then the bare name for a
    local-only repo. Already-qualified values are returned as-is.

    Extracted so :func:`resolve_base_ref` and :func:`resolve_base_sha`
    consume one rule instead of two copies of it - and so the second one
    can ask git for the sha directly rather than resolving a name and
    then resolving the name again.
    """
    if base_branch.startswith("origin/"):
        return (base_branch,)
    return (f"origin/{base_branch}", base_branch)


def resolve_base_ref(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Resolve the ref that worktree cuts and diffs measure against.

    Returns ``origin/<base_branch>`` when that remote-tracking ref
    exists, else ``base_branch`` unchanged (local-only repos). This is
    the single place that decides the base ref (R0.2): squash merges
    rewrite SHAs, so a stale local base produces phantom diffs via
    ``base...HEAD``; cutting AND diffing against ``origin/<base>``
    removes the class. Never mutates any ref or the checkout.
    """
    if base_branch.startswith("origin/"):
        return base_branch
    try:
        result = subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{base_branch}",
            ],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return f"origin/{base_branch}"
    except (subprocess.TimeoutExpired, OSError):
        pass
    return base_branch


def fetch_base_branch(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = FETCH_TIMEOUT,
) -> str | None:
    """Update ``refs/remotes/origin/<base_branch>`` via ``git fetch``.

    Replaces the old ``git pull`` (R0.2/H-1): fetch touches only the
    remote-tracking ref, never the operator's checked-out branch or the
    local base branch. Returns an error message, or None on success.
    """
    try:
        # "--" keeps a crafted base branch out of option position (R0.6).
        result = subprocess.run(
            ["git", "fetch", "--", "origin", base_branch],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"git fetch origin {base_branch} timed out after {timeout}s"
    if result.returncode != 0:
        return result.stderr.strip() or f"git fetch origin {base_branch} failed"
    return None


def merge_base_ref(
    base_label: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """The commit ``{base_label}...HEAD`` actually diffs against, or ``""``.

    Hoisted from ``verify._merge_base_ref`` (#425) to ``kstrl/git/git.py`` so
    it has the one owner every consumer that measures ``{ref}...HEAD``
    can share (#435 review, finding A0): both
    :func:`get_diff_name_status` and :func:`get_diff_content` spell their
    diff ``{base_ref}...HEAD`` (three dots: git's own shorthand for
    ``git merge-base base_ref HEAD``), and :func:`capture_workspace_baseline`
    now anchors on the identical commit, so the in-loop guard and the
    Phase 1 gate can no longer disagree about WHICH REVISION the base
    names.

    ``""`` whenever the merge base cannot be found: an absent ref,
    unrelated histories, a timeout, or a spawn that could not run at all.
    Every caller of this function treats ``""`` as a REFUSAL - never a
    clear, never a "nothing changed" - so an uncertainty here is always
    the side that keeps looking rather than the side that stands down.
    """
    try:
        found = subprocess.run(
            ["git", "merge-base", base_label, "HEAD"],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        if found.returncode != 0:
            return ""
        return found.stdout.decode("utf-8").strip()
    except Exception:
        return ""


def is_ancestor(
    ancestor: str, descendant: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT
) -> bool:
    """Whether ``descendant`` contains ``ancestor``. False whenever git
    cannot say (an absent ref, a timeout, a spawn that could not run): a
    caller asking whether a branch's code is in the base treats no answer
    as not in it."""
    try:
        done = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return False
    return done.returncode == 0


def resolve_base_sha(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """The COMMIT the base ref names right now, as a full sha.

    :func:`resolve_base_ref` returns a NAME, and ``origin/<base>`` is a
    MOVING ref. :func:`fetch_base_branch` runs mid-run every time a
    component's PR merges, updating ``refs/remotes/origin/*`` for every
    worktree that shares the repository. So a name pins nothing across
    time, and #266's coverage check compares two measurements taken
    minutes apart: the harness runs ``--numstat`` before the reviewer
    starts, the reviewer runs its own after. A merge landing in between
    moves the merge-base under both, they disagree, and hard mode
    discards a review that read every byte of the change. Nobody
    involved did anything wrong and the component fails.

    Resolving to a sha once removes the class: the prompt names an
    immutable commit and the harness measures against the same one, so a
    disagreement can only be about what the reviewer read.

    Walks :func:`base_ref_candidates` directly rather than resolving a
    NAME through :func:`resolve_base_ref` and then resolving that name
    again: ``rev-parse --verify`` already prints the sha, so asking for
    the name first was a spawn that bought nothing. Precedence still has
    exactly one owner.

    Raises :class:`GitDiffError` when no candidate resolves, for the
    reason :func:`get_diff_stat` is strict: a missing measurement must
    surface, never degrade into a comparison against something else.
    """
    candidates = base_ref_candidates(base_branch)
    for candidate in candidates:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                cwd=cwd,
                capture_output=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitDiffError(f"git rev-parse {candidate} timed out after {timeout}s") from exc
        except OSError as exc:
            # Same breadth as resolve_base_ref, which catches OSError
            # alongside TimeoutExpired: a missing git binary must arrive
            # as the GitDiffError this function documents, not raw.
            raise GitDiffError(f"git rev-parse {candidate} could not run: {exc}") from exc
        except UnicodeDecodeError as exc:
            # No fixture reaches this branch today and that is disclosed rather
            # than hidden: `rev-parse --verify --quiet` prints a sha on success
            # and nothing at all on failure (measured, #416). It is converted
            # anyway because the guard's subject is every function here that
            # raises GitDiffError, and an exemption row is the ledger shape
            # CLAUDE.md warns about. Its cover is the static guard.
            raise GitDiffError(_undecodable_message(f"git rev-parse {candidate}", exc)) from exc
        sha = result.stdout.strip()
        if result.returncode == 0 and sha:
            return sha
    raise GitDiffError(
        f"base branch {base_branch!r} does not resolve to a commit (tried {', '.join(candidates)})"
    )


def base_moved(
    base_branch: str,
    pinned_sha: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Resolve the base again and say whether it left ``pinned_sha``.

    Returns ``(moved, current_sha)``. A verdict reached on ``pinned_sha``
    holds at that commit; when the base has moved since, the caller
    records both commits rather than presenting the verdict as one about
    the current base (#480, section 3.1).

    Raises :class:`GitDiffError` when the base does not resolve, as
    :func:`resolve_base_sha` does: a check that could not be made must
    not read as "did not move".
    """
    current = resolve_base_sha(base_branch, cwd, timeout)
    return current != pinned_sha, current
