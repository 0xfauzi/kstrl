"""Git operations for kstrl."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kstrl.ui.base import UI

DEFAULT_TIMEOUT = 30.0

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

# Local plumbing, called on the way into a command and on the Textual
# event loop when the decompose launch form composes.
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
    ``ks factory`` (worktree base), ``ks sense`` (diff base) and the TUI
    launch forms.

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
    cannot-measure (`ks sense` exit 2, naming ``--base``) into a silent
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
            text=True,
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
    ``--base-branch``, ``ks sense``'s ``--base``, and the TUI launch
    form's branch field (#259).
    """
    return base_branch or detect_base_branch(cwd)


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
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"git fetch origin {base_branch} timed out after {timeout}s"
    if result.returncode != 0:
        return result.stderr.strip() or f"git fetch origin {base_branch} failed"
    return None


def is_git_repo(path: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def get_repo_root(path: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> Path | None:
    """Get the root directory of the git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def branch_exists(
    branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Check if a branch exists."""
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def checkout_branch(
    branch: str,
    ui: UI,
    cwd: Path | None = None,
    source: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Checkout or create a branch.

    Returns True on success, False on failure.
    """
    source_suffix = f" ({source})" if source else ""

    try:
        if branch_exists(branch, cwd, timeout=timeout):
            ui.info(f"Branch: checking out existing branch {branch}{source_suffix}")
            # Trailing "--" pins the argument as a ref, never a pathspec
            # (R0.6). Note "--" cannot stop git from parsing a leading
            # "-" ref as an option here; that shape is rejected upstream
            # by manifest.validate_branch_name.
            result = subprocess.run(
                ["git", "checkout", branch, "--"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            ui.info(f"Branch: creating branch {branch}{source_suffix}")
            result = subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        ui.err(f"Branch checkout timed out after {timeout}s")
        return False

    output = result.stdout + result.stderr
    for line in output.strip().splitlines():
        if line:
            ui.stream_line("GIT", line)

    return result.returncode == 0


def get_changed_files(
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> set[str]:
    """Get all changed files (staged, unstaged, and untracked).

    Returns paths relative to repo root.
    """
    files: set[str] = set()

    try:
        # Unstaged changes
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            files.update(line.strip() for line in result.stdout.splitlines() if line.strip())

        # Staged changes
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            files.update(line.strip() for line in result.stdout.splitlines() if line.strip())

    except subprocess.TimeoutExpired:
        pass

    files.update(get_untracked_files(cwd, timeout))
    return files


def get_untracked_files(
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> set[str]:
    """Untracked, non-ignored files. Separate from ``get_changed_files``
    because a baseline comparison needs them WITHOUT the index/HEAD
    deltas that function also folds in (see get_changed_files_since)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


@dataclass(frozen=True)
class WorkspaceBaseline:
    """The workspace as it stood BEFORE an agent was let loose on it.

    ``head`` is the commit HEAD pointed at (None in a repo with no
    commits yet, or when rev-parse failed); ``dirty`` is the set of files
    that were already staged, modified or untracked at that moment.

    Both halves exist because ``get_changed_files`` alone answers the
    wrong question for scope enforcement (R8 review finding 4). It sees
    only the index and the working tree, and the engineer prompt tells
    the agent to COMMIT after every story - so an out-of-scope file that
    was committed is invisible to it, i.e. the tripwire was blind exactly
    when the agent had done the most work. The converse also bit: in a
    --no-worktrees run, an operator's own uncommitted file was reported
    as the AGENT's violation and could be destroyed by "Revert and
    continue".
    """

    head: str | None
    dirty: frozenset[str]


def get_head_sha(
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """The commit HEAD points at, or None (unborn HEAD, not a repo,
    timeout)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def capture_workspace_baseline(
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    base_ref: str | None = None,
) -> WorkspaceBaseline:
    """Snapshot the comparison commit and the pre-existing dirty set.

    ``base_ref`` should be the component's BASE BRANCH, so this guard
    judges scope from the same point ``check_diff_scope`` does. Anchoring
    on the worker's starting HEAD instead looked more natural and was
    wrong: a retry resumes the component branch carrying the previous
    attempt's out-of-scope commit, so DELETING that file - the exact fix
    the retry prompt asks for - registers as a fresh out-of-scope change
    and the guard rejects its own remedy. Against the base branch the
    add-and-delete nets to nothing, which is why Phase 1 never had this
    problem.

    Falls back to HEAD when no ref is given or it cannot be resolved
    (``ks run`` outside a factory has no base branch), which is the
    pre-existing behavior. Cheap: two plumbing calls, taken once per
    loop rather than once per iteration.
    """
    head = resolve_ref(base_ref, cwd, timeout) if base_ref else None
    return WorkspaceBaseline(
        head=head or get_head_sha(cwd, timeout),
        dirty=frozenset(get_changed_files(cwd, timeout)),
    )


def resolve_ref(
    ref: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """The sha ``ref`` names, or None when it does not resolve.

    Tries the local ref first, then ``origin/<ref>``: a fresh worktree
    may carry the remote-tracking ref only.
    """
    for candidate in (ref, f"origin/{ref}"):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def get_changed_files_since(
    baseline: WorkspaceBaseline,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> set[str]:
    """The COMPLETE delta an agent produced since ``baseline``.

    Every tracked file that differs between the baseline COMMIT and the
    working tree - which spans the agent's commits, its index and its
    unstaged edits in one comparison - plus everything untracked, MINUS
    the files that were already dirty when the baseline was taken.

    Measured against the BASELINE, never against HEAD: HEAD moves as the
    agent commits, so a file reverted to its baseline content still
    differs from HEAD and would be reported as an outstanding change
    forever. The question here is only "does this differ from how the
    agent found it".

    The subtraction is what keeps an operator's own uncommitted work out
    of the agent's violation list in a --no-worktrees run. Its cost is a
    known false NEGATIVE: a file the operator had already modified and
    the agent then modified again is attributed to the operator and not
    reported. Chosen over refusing to run in a dirty checkout because it
    is the less disruptive half of that trade, and because the missed
    case is bounded by files a human touched deliberately - whereas the
    committed-file blind spot it replaces covered EVERY file the agent
    touched after its first commit.

    Fails OPEN like the rest of this module: an unborn HEAD (nothing to
    diff against) or a diff that cannot be produced falls back to the
    working-tree view, which is today's behavior, rather than raising.
    """
    if baseline.head is None:
        changed = get_changed_files(cwd, timeout)
    else:
        changed = set(_committed_since(baseline.head, cwd, timeout))
        changed.update(get_untracked_files(cwd, timeout))
    return changed - set(baseline.dirty)


def _committed_since(
    ref: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    """Paths differing between ``ref`` and the working tree.

    ``git diff <ref>`` spans commits AND the index AND the working tree,
    so one call covers "committed since the baseline" without a separate
    rev-list. Rename/copy records contribute both paths, exactly as
    ``get_diff_names`` does, because for scope purposes content left the
    source too.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", "-z", ref, "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0:
        return []
    return _unique_paths(path for _, path in _parse_name_status_records(result.stdout))


def restore_file_from(
    file: str,
    ref: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Restore ``file`` to its content at ``ref`` (index AND working tree).

    Returns False when the file did not exist at ``ref`` - the caller's
    cue that reverting means deleting it, not restoring it. Needed
    because a baseline-aware guard can be reverting a COMMITTED change,
    which ``restore_file`` (index/HEAD as the source) would treat as
    already-correct and leave in place.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "restore",
                f"--source={ref}",
                "--staged",
                "--worktree",
                "--",
                file,
            ],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def stage_file(
    file: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """Stage one path (``git add``), leaving history alone.

    The inverse of :func:`remove_from_index`, and the smallest write that
    makes a path TRACKED: no commit is created.

    Returns an error message, or None on success, like
    :func:`fetch_base_branch` and unlike the bool helpers around it. The
    reason matters here: ``git add`` refuses an ignored path, and a
    caller that only knows "false" ends up advising the very command
    that just failed (#256 review).
    """
    try:
        result = subprocess.run(
            ["git", "add", "--", file],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"git add {file} timed out after {timeout}s"
    if result.returncode != 0:
        return result.stderr.strip().splitlines()[0] if result.stderr.strip() else "git add failed"
    return None


def ignore_source(
    file: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """Where an ignore rule for ``file`` lives, or None if not ignored.

    ``git check-ignore -v`` answers with ``<source>:<line>:<pattern>``,
    so the caller can name the file and line the operator has to edit
    instead of reporting that git said no. Exit 1 means "not ignored"
    and 128 means git could not answer; both read as None.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-v", "--", file],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # "<source>:<line>:<pattern>\t<pathname>" - the pathname is the file
    # we asked about, so only the rule's location is worth reporting.
    return result.stdout.strip().splitlines()[0].split("\t")[0]


def remove_from_index(
    file: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Drop ``file`` from the index, leaving the working tree alone.

    Paired with ``delete_untracked`` to revert a file the agent both
    created and committed: after both, the path is absent from the index
    and from disk, so it no longer appears in the delta against the
    baseline.
    """
    try:
        result = subprocess.run(
            ["git", "rm", "--cached", "--ignore-unmatch", "-q", "--", file],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def restore_file(
    file: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Restore a tracked file (staged and working tree)."""
    try:
        result = subprocess.run(
            ["git", "restore", "--staged", "--worktree", "--", file],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def delete_untracked(file: str, cwd: Path | None = None) -> bool:
    """Delete an untracked file."""
    try:
        path = Path(cwd or ".") / file
        if path.exists():
            if path.is_dir():
                import shutil

                shutil.rmtree(path)
            else:
                path.unlink()
        return True
    except Exception:
        return False


def is_file_tracked(
    file: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Check if a file is tracked by git."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", file],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def get_diff_names(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    strict: bool = False,
) -> list[str]:
    """Get list of changed file names compared to a base branch.

    The base is resolved through :func:`resolve_base_ref` so diffs
    measure against ``origin/<base>`` whenever a remote exists (R0.2).

    Rename/copy detection is explicit (`-M -C`) and BOTH sides of a
    rename or copy count as changed paths. With `--name-only`, git's
    rename detection reports only the destination, so
    `git mv protected/gate.py allowed/gate.py` looked like a change
    confined to `allowed/` and defeated the diff-scope guard
    (R1.5 / H-5). For scope purposes the source changed too: content
    left it.

    ``strict=True`` raises :class:`GitDiffError` on timeout or nonzero
    exit instead of returning ``[]`` (R8.1). The lenient default is
    fail-OPEN - an empty list is indistinguishable from "no files
    changed" - which silently turns a policy check into a vacuous pass.
    Enforcement callers must pass ``strict=True``; the pre-existing
    callers keep the lenient contract they were written against.
    """
    return _unique_paths(
        path
        for _, path in get_diff_name_status(
            base_branch,
            cwd,
            timeout,
            strict=strict,
        )
    )


def get_diff_name_status(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    strict: bool = False,
) -> list[tuple[str, str]]:
    """``(status, path)`` records for the diff against a base branch.

    Same shell-out and same base resolution as :func:`get_diff_names`,
    which is built on this - callers that need to tell an ADDED file from
    a MODIFIED one should use this rather than re-running git.

    The status is git's raw token (``A``, ``M``, ``D``, ``R100``, ...).
    Rename and copy records yield TWO entries carrying the SAME status,
    one per path, for the reason :func:`get_diff_names` documents: for
    scope purposes content left the source too. A rename destination is
    therefore reported as ``R``/``C`` and never as ``A``: its content is
    not new, it moved, and treating it as new would apply new-file rules
    to tests nobody wrote here.

    ``strict`` carries the identical contract to :func:`get_diff_names`:
    lenient returns ``[]`` on failure (fail-OPEN, indistinguishable from
    a clean diff), strict raises :class:`GitDiffError`.
    """
    base_ref = resolve_base_ref(base_branch, cwd, timeout)
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--name-status",
                "-z",
                "-M",
                "-C",
                f"{base_ref}...HEAD",
                "--",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return _parse_name_status_records(result.stdout)
    except subprocess.TimeoutExpired as exc:
        if strict:
            raise GitDiffError(
                f"git diff --name-status against {base_ref} timed out after {timeout}s"
            ) from exc
        return []
    if strict:
        raise GitDiffError(
            f"git diff --name-status against {base_ref} exited "
            f"{result.returncode}: {result.stderr.strip()[:500]}"
        )
    return []


def _parse_name_status_records(output: str) -> list[tuple[str, str]]:
    """Parse `git diff --name-status -z` into ``(status, path)`` records.

    Records are NUL-separated: a status token followed by one path,
    except rename/copy statuses (`R<score>`/`C<score>`) which carry
    source AND destination. Both are emitted, under the same status.
    """
    tokens = output.split("\0")
    records: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        if status[0] in ("R", "C"):
            records.extend((status, path) for path in tokens[i + 1 : i + 3] if path)
            i += 3
        else:
            if i + 1 < len(tokens) and tokens[i + 1]:
                records.append((status, tokens[i + 1]))
            i += 2
    return records


def _unique_paths(paths: Iterable[str]) -> list[str]:
    """Order-preserving dedupe (a copy source may also be modified)."""
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _parse_name_status_z(output: str) -> list[str]:
    """Flat, deduped path list from `git diff --name-status -z` output."""
    return _unique_paths(path for _, path in _parse_name_status_records(output))


class GitDiffError(RuntimeError):
    """Raised when ``git diff`` cannot produce a diff (nonzero exit,
    e.g. bad ref or not a repository, or a timeout). Callers must treat
    this as an infrastructure failure: before R1.3 (H-14) these errors
    returned an empty string, and review/security/knowledge silently
    "reviewed" a diff of nothing and passed."""


def get_diff_content(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Get full diff content compared to a base branch.

    The base is resolved through :func:`resolve_base_ref` so diffs
    measure against ``origin/<base>`` whenever a remote exists (R0.2).

    Raises :class:`GitDiffError` on git failure or timeout (R1.3). An
    empty return string therefore always means a genuinely empty diff,
    never a swallowed error.
    """
    base_ref = resolve_base_ref(base_branch, cwd, timeout)
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_ref}...HEAD", "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitDiffError(f"git diff against {base_ref} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise GitDiffError(
            f"git diff against {base_ref} exited {result.returncode}: {result.stderr.strip()[:500]}"
        )
    return result.stdout


def _normalize_numstat_path(path: str) -> str:
    """Resolve a ``git diff --numstat`` rename path to its destination.

    Renames render as ``old => new`` or, when a common prefix/suffix
    exists, ``pre{old => new}post``. Both collapse to the new path so the
    policy size caps count one file per change, not two.
    """
    if "=>" not in path:
        return path
    if "{" in path and "}" in path:
        pre, rest = path.split("{", 1)
        mid, post = rest.split("}", 1)
        _old, _sep, new = mid.partition("=>")
        return (pre + new.strip() + post).replace("//", "/")
    _old, _sep, new = path.partition("=>")
    return new.strip()


def get_diff_numstat(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    strict: bool = False,
) -> list[tuple[int | None, int | None, str]]:
    """Per-file ``(added, removed, path)`` counts vs a base branch.

    ``added``/``removed`` are None for binary files (git prints ``-``).
    Rename paths are normalized to the destination. Base resolution
    mirrors :func:`get_diff_names` (``origin/<base>`` when a remote
    exists).

    ``strict=True`` raises :class:`GitDiffError` on timeout or nonzero
    exit instead of returning ``[]``. This is load-bearing for the R8.1
    policy check: an empty list reads as "zero files, zero lines", which
    silently satisfies every size cap. A successful earlier
    :func:`get_diff_content` does NOT prove this later, separate
    subprocess succeeded, so enforcement callers must ask for strict.
    """
    base_ref = resolve_base_ref(base_branch, cwd, timeout)
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", f"{base_ref}...HEAD", "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if strict:
            raise GitDiffError(
                f"git diff --numstat against {base_ref} timed out after {timeout}s"
            ) from exc
        return []
    if result.returncode != 0:
        if strict:
            raise GitDiffError(
                f"git diff --numstat against {base_ref} exited "
                f"{result.returncode}: {result.stderr.strip()[:500]}"
            )
        return []
    rows: list[tuple[int | None, int | None, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_raw, removed_raw, path = parts
        added = None if added_raw == "-" else int(added_raw)
        removed = None if removed_raw == "-" else int(removed_raw)
        rows.append((added, removed, _normalize_numstat_path(path)))
    return rows


@dataclass(frozen=True)
class DiffStat:
    """The shape of a change, as ``git diff --numstat`` reports it.

    #266: the reviewer no longer receives the diff in its prompt - it
    runs git itself inside the worktree - so the harness needs a
    mechanical description of the change it can compare the reviewer's
    own account against. This is that description.

    ``files`` counts every row ``git diff --numstat <base>...HEAD``
    prints, including binary rows. ``insertions`` and ``deletions`` sum
    columns one and two, and binary rows (which git prints as ``-``)
    contribute their file and no lines. Lockfiles are NOT excluded here,
    unlike ``policy.count_diff_size``: this figure is compared against
    what a reviewer typing the plain command sees, and that command has
    no idea what a lockfile is.
    """

    files: int
    insertions: int
    deletions: int

    def render(self) -> str:
        """One-line human/prompt rendering, e.g. ``3 files, +120/-4``."""
        return f"{self.files} files, +{self.insertions}/-{self.deletions}"

    def as_payload(self) -> dict[str, int]:
        """The wire shape the reviewer prompts ask for.

        One place for the three key names, because they are a contract
        between the prompt schema, :func:`parse_observed_diffstat`, and
        every fixture that fakes a reply.
        """
        return {
            "files": self.files,
            "insertions": self.insertions,
            "deletions": self.deletions,
        }


def parse_observed_diffstat(value: object) -> DiffStat | None:
    """#266: a reviewer's reported diffstat, or None if it reported none.

    Shared by the review and security parsers, which ask for the same
    field in the same shape. It lives here rather than in either of
    them: it builds a :class:`DiffStat` and its counterpart
    :func:`diffstat_disagreement` is here too, and putting it in one
    reviewer would make the other import it, for one coercion.

    None on ANY malformed reading - a missing key, a non-object, a
    non-integer count, a bool (``True`` is an ``int`` in Python and
    would otherwise land as ``1``), a negative count. All of those mean
    the same thing downstream: no usable claim about what was read,
    which :func:`diffstat_disagreement` reports as an unverified review.
    Coercing a broken shape into a number would manufacture a claim the
    reviewer never made.
    """
    if not isinstance(value, dict):
        return None
    counts: list[int] = []
    for key in ("files", "insertions", "deletions"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None
        counts.append(raw)
    return DiffStat(files=counts[0], insertions=counts[1], deletions=counts[2])


def diffstat_from_numstat(
    numstat: Sequence[tuple[int | None, int | None, str]],
) -> DiffStat:
    """Fold ``get_diff_numstat`` rows into a :class:`DiffStat`."""
    return DiffStat(
        files=len(numstat),
        insertions=sum(added or 0 for added, _removed, _path in numstat),
        deletions=sum(removed or 0 for _added, removed, _path in numstat),
    )


def diffstat_disagreement(claimed: DiffStat | None, actual: DiffStat) -> str | None:
    """How a reviewer's reported diffstat disagrees with git's, or None.

    #266: the anti-padding guarantee, restated for a reviewer that reads
    the repository instead of being handed a diff.

    What it catches, stated precisely so nobody reads more into it. It
    catches a reviewer that never obtained the change: one that answered
    from the PRD alone, ran against the wrong base ref or the wrong
    range, sat in the wrong directory, or read a worktree that had moved
    under it. In every one of those the reported figure is not git's,
    and the verdict was reached without the evidence.

    What it does NOT catch: a reviewer that obtains the whole change,
    reports the correct figure, and then skims it. ``git diff --numstat``
    is cheap and its output is not proof of attention. The chunking this
    replaced did not catch that either - splitting a diff across N
    prompts proves delivery, not reading - so the property being traded
    is delivery-proof for acquisition-proof, and neither was ever
    attention-proof. The padding attack chunking specifically defended
    against (bury a malicious hunk past a 50KB head-truncation cut)
    needs a cut to exist; there is no cut when the reviewer holds the
    whole repository.
    """
    if claimed is None:
        return "the reviewer reported no diffstat, so nothing says it read the change"
    if claimed == actual:
        return None
    return (
        f"the reviewer reports it reviewed {claimed.render()} but git "
        f"reports {actual.render()} for this change"
    )


#: What an operator should check when a reviewer's diffstat does not
#: match git's. Shared by both reviewer phases: the marker they attach
#: is the PR body's account of the condition, and two hand-maintained
#: copies of one sentence eventually say different things about it.
COVERAGE_SUGGESTION = (
    "Check that the reviewer can run git in the worktree and is reading "
    "the same base ref the harness measures against."
)


def coverage_marker_text(label: str, disagreement: str) -> str:
    """The marker finding's explanation for an unverified *label* review."""
    return (
        f"Unverified {label} coverage (#266): {disagreement}. "
        "The findings below may not cover the whole change."
    )


def coverage_notes_prefix(label: str, disagreement: str) -> str:
    """The overall-notes prefix hard mode adds when it refuses."""
    return f"Hard-mode {label} coverage unverified: {disagreement} (#266)."


# ---------------------------------------------------------------------------
# How a reviewer obtains the change (#266)
# ---------------------------------------------------------------------------
#
# The reviewer roles already run with ``cwd`` set to the worktree under
# review (``Agent.run(prompt, cwd, timeout)`` has always carried it), so
# the change does not have to be pasted into their prompt - and pasting
# it is what forced a size cap, chunking, and a fail-closed stop on any
# single hunk bigger than the cap. A newly added file is always exactly
# one hunk, so any new file over roughly 50KB was permanently
# unreviewable.
#
# Both blocks below are HARNESS-authored text and sit outside the data
# delimiters. The pasted variant opens its own delimited section around
# the untrusted diff bytes.


def repo_change_source(base_ref: str) -> str:
    """Instructions for a reviewer that can read the repository itself.

    This is the production path. ``base_ref`` must be the ref the
    harness itself measures against (:func:`resolve_base_ref`), not the
    branch name it was derived from: ``origin/main`` and ``main`` are
    different commits often enough that handing over the wrong one would
    make every diffstat disagree for a reason that is not the reviewer's
    fault.
    """
    return f"""\
Your working directory IS the git worktree that holds the change under
review, and git is on your path. Nothing has been pasted into this
prompt. Obtain the change yourself.

The change is everything between the base ref and HEAD. Run, verbatim:

    git diff {base_ref}...HEAD

Read all of it - every file, every hunk. Use `git show`, `git log`, and
ordinary file reads for any surrounding context you need: unlike a
reviewer handed a fixed excerpt, you can open the callers of a function
the change touches and the tests that were supposed to cover it, and you
are expected to.

You have READ-ONLY access to this tree. Do not create, modify, or delete
anything, and do not run any command that would - not a formatter, not a
test run, not an import that writes bytecode. You are judging this tree;
changing it destroys the evidence.

Then run, verbatim:

    git diff {base_ref}...HEAD --numstat

and report what it printed under "observedDiffstat": "files" is the
number of rows, "insertions" the sum of the first column, "deletions"
the sum of the second. A row whose two columns are both "-" is a binary
file: count the row, add no lines. The harness runs the same command and
compares. A figure that does not match is read as "this review did not
cover the change" and the verdict is discarded, so report what you
actually saw and never what you expect the answer to be."""


def pasted_change_source(diff_content: str, data_delimiter: str) -> str:
    """The change pasted inline, for a caller with no repository.

    NOT the production path and not reachable from the factory: the
    review and security phases run inside the worktree and always take
    :func:`repo_change_source`. This exists for callers that hold a diff
    and no repo to read it from - the planted-bug calibration fixtures,
    which are hand-written diffs that do not apply to any tree.

    Deliberately applies no size cap. The cap is what this issue removed,
    and a truncation reintroduced here would silently restore the failure
    on the one path whose job is to measure the prompt honestly. Callers
    on this path own the size of what they paste.
    """
    return f"""\
No repository is available to you, so the complete change is pasted
below between the delimiter lines. It is all there is to review; there
is no other file to open and nothing was withheld.

<<<{data_delimiter}:BEGIN GIT DIFF (changes to review)>>>
{diff_content}
<<<{data_delimiter}:END GIT DIFF>>>

Report "observedDiffstat" from the diff above: "files" is the number of
"diff --git" lines, "insertions" the number of lines beginning with a
single "+" (not the "+++" file headers), "deletions" the number
beginning with a single "-" (not the "---" headers)."""


def get_diff_stat(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> DiffStat:
    """The :class:`DiffStat` of ``<base>...HEAD``, or raise.

    Always strict: this is a measurement the reviewer's claim is checked
    against, and the lenient ``get_diff_numstat`` contract returns ``[]``
    on failure - which folds to ``0 files, +0/-0`` and would silently
    agree with a reviewer that saw nothing. A missing measurement must
    surface as :class:`GitDiffError`, never as a zero.
    """
    return diffstat_from_numstat(
        get_diff_numstat(base_branch, cwd, timeout, strict=True),
    )


# House budget for diff content injected into LLM prompts, and the
# default of :func:`truncate_diff_for_prompt`.
#
# #266 removed its last enforcing caller: the review and security
# prompts no longer carry a diff at all, because they read the worktree
# they already run in. The two paste sites that remain both name their
# own number - the HITL checkpoint passes CHECKPOINT_DIFF_CHAR_LIMIT
# (20,000) and the knowledge distiller cuts at a literal 50,000 of its
# own - so nothing in kstrl/ reads this value today. It is kept as the
# documented house limit and as the function's default rather than
# deleted, so a future paste site has one number to adopt instead of
# inventing a third.
DEFAULT_PROMPT_DIFF_CHAR_LIMIT = 50_000


def truncate_diff_for_prompt(
    diff_content: str,
    limit: int = DEFAULT_PROMPT_DIFF_CHAR_LIMIT,
) -> str:
    """Truncate a diff string for inclusion in an LLM prompt.

    Appends a single trailing line noting the truncation so the reviewer
    knows it isn't seeing the full diff.
    """
    if len(diff_content) <= limit:
        return diff_content
    return diff_content[:limit] + f"\n... (diff truncated at {limit // 1000}KB)"


def merge_branch(
    branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Merge a branch into the current branch (no-edit)."""
    try:
        # "--" makes a crafted branch value an invalid ref instead of a
        # git option (R0.6).
        result = subprocess.run(
            ["git", "merge", "--no-edit", "--", branch],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def create_branch_from(
    branch_name: str,
    base: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Create and checkout a new branch from a base ref."""
    try:
        # Trailing "--" pins *base* as a ref, never a pathspec (R0.6).
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name, base, "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def delete_branch(
    branch_name: str,
    cwd: Path | None = None,
    force: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Delete a local branch."""
    flag = "-D" if force else "-d"
    try:
        # "--" makes a crafted branch value an unknown ref instead of a
        # git option (R0.6).
        result = subprocess.run(
            ["git", "branch", flag, "--", branch_name],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def checkout_existing(
    branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Checkout an existing branch without creating it."""
    try:
        # Trailing "--" pins the argument as a ref, never a pathspec
        # (R0.6); leading "-" shapes are rejected upstream by
        # manifest.validate_branch_name.
        result = subprocess.run(
            ["git", "checkout", branch, "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
