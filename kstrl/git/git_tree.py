"""The files of a worktree: changes, tracked files, the baseline of an iteration, restores."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from kstrl.git.git import (
    DEFAULT_TIMEOUT,
    GitDiffError,
    _nul_paths,
    _parse_name_status_records,
    _undecodable_message,
    _unique_paths,
    get_head_sha,
)
from kstrl.git.git_base import merge_base_ref, resolve_base_ref


def get_changed_files(
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> set[str]:
    """Get all changed files (staged, unstaged, and untracked).

    Returns paths relative to repo root, exactly as git spells them: both
    spawns run with ``-z`` so a path holding a non-ASCII byte, a tab, a
    double quote or a backslash arrives NUL-separated and unquoted rather
    than as git's C-quoted rendering of it (#423). A path this process
    cannot decode as utf-8 is a diff it could not obtain, so it raises
    ``GitDiffError`` (#416's contract) rather than silently omitting it.
    """
    files: set[str] = set()

    try:
        # Unstaged changes
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
        if result.returncode == 0:
            files.update(_nul_paths(result.stdout))

        # Staged changes
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached", "-z"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
        if result.returncode == 0:
            files.update(_nul_paths(result.stdout))

    except subprocess.TimeoutExpired:
        pass
    except UnicodeDecodeError as exc:
        raise GitDiffError(_undecodable_message("git diff --name-only", exc)) from exc

    files.update(get_untracked_files(cwd, timeout))
    return files


def get_untracked_files(
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> set[str]:
    """Untracked, non-ignored files. Separate from ``get_changed_files``
    because a baseline needs them WITHOUT the index/HEAD
    deltas that function also folds in (see get_changed_files_since).

    Runs with ``-z`` so a tricky name arrives as the file's own spelling,
    not git's C-quoted rendering of it (#423); a path this process cannot
    decode as utf-8 raises ``GitDiffError`` rather than being dropped.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return set()
    except UnicodeDecodeError as exc:
        raise GitDiffError(_undecodable_message("git ls-files --others", exc)) from exc
    if result.returncode != 0:
        return set()
    return _nul_paths(result.stdout)


def status_entries(cwd: Path, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Every entry ``git status --porcelain --untracked-files=all`` prints in ``cwd``.

    Untracked files one by one, modified and deleted files too, ignored
    files never. ``-z``, so a path arrives as its own spelling (#423). Raises
    ``GitDiffError`` when git cannot answer: a caller that refuses on what it
    finds must not read a failed read as a clean tree (#696).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "-z"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitDiffError(f"git status did not finish within {timeout}s") from exc
    except UnicodeDecodeError as exc:
        raise GitDiffError(_undecodable_message("git status", exc)) from exc
    if result.returncode != 0:
        raise GitDiffError(f"git status exited {result.returncode}: {result.stderr.strip()}")
    return [entry for entry in result.stdout.split("\0") if entry]


def tracked_files_at(
    sha: str, cwd: Path | None, timeout: float = DEFAULT_TIMEOUT
) -> frozenset[str]:
    """Every file tracked at commit ``sha``, spelled as git stores it
    (``-z``, #423).

    Raises :class:`GitDiffError` on a timeout, a spawn failure, a non-zero
    exit or a name this process cannot decode. An empty answer would read
    as "no cited file exists" and hand every integration finding off (#500).
    """
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "-z", sha],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GitDiffError(f"git ls-tree {sha} did not run: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise GitDiffError(_undecodable_message(f"git ls-tree {sha}", exc)) from exc
    if result.returncode != 0:
        raise GitDiffError(f"git ls-tree {sha} exited {result.returncode}: {result.stderr.strip()}")
    return frozenset(_nul_paths(result.stdout))


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
    pre-existing behavior. Cheap: a small, fixed number of plumbing
    calls, taken once per loop rather than once per iteration.

    The base is resolved through :func:`resolve_base_ref` (``origin/<base>``
    preferred, R0.2) and then through :func:`merge_base_ref`, which anchors
    on the MERGE BASE of that ref and HEAD, never the base branch's current
    TIP (#435 review, finding A0). A tip anchor fixed #435's own shape -
    a previous tier merged before the worktree was cut - but stayed wrong
    the moment the base branch moves AFTER the cut, which the factory
    makes routine: a sibling component's PR can squash-merge while this
    component is still mid-loop, since ``refs/remotes/origin/*`` is
    fetched into every worktree that shares the repository. A tip anchor
    then blames the sibling's files on this engineer; the merge base does
    not move, because it is the commit this worktree actually forked
    from. This is the identical anchor :func:`get_diff_name_status` and
    :func:`get_diff_content` already use via their three-dot diffs, so
    the in-loop guard and the Phase 1 gate now measure the same
    revision - which is the whole of #435's original complaint, restated
    one layer down. Neither :func:`resolve_base_ref` nor
    :func:`merge_base_ref` raises: both refuse by returning a fallback
    value, so this function adds no try/except of its own, and a
    genuine programming error in either one is not a candidate for the
    HEAD fallback below - it propagates.
    """

    head: str | None = None
    if base_ref:
        base_label = resolve_base_ref(base_ref, cwd, timeout)
        head = merge_base_ref(base_label, cwd, timeout) or None
    return WorkspaceBaseline(
        head=head or get_head_sha(cwd, timeout),
        dirty=frozenset(get_changed_files(cwd, timeout)),
    )


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
    A path this process cannot decode as utf-8 is different: it is a diff
    that WAS produced and that a reader cannot read, so it raises
    ``GitDiffError`` (#416's contract) rather than silently reporting no
    change (#423).
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

    Fails OPEN on a diff that cannot be produced (timeout, non-zero
    exit): returning ``[]`` there falls back to the working-tree view,
    which is today's behaviour. A path this process cannot decode as
    utf-8 is different: it is a diff that WAS produced and that this
    reader cannot read, so it raises ``GitDiffError`` (#416's contract)
    rather than silently reporting no change (#423).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", "-z", ref, "--"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return []
    except UnicodeDecodeError as exc:
        raise GitDiffError(_undecodable_message("git diff --name-status", exc)) from exc
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


def listed_files(cwd: Path, timeout: float = DEFAULT_TIMEOUT) -> frozenset[str] | None:
    """The files git lists under ``cwd``, or None when git could not answer.

    One ``git ls-files --cached --others --exclude-standard -z`` call:
    every tracked file, and every untracked file no ignore rule matches,
    spelled relative to ``cwd``. A directory that is not in a repository
    (exit 128), a missing git, a timeout and a name this process cannot
    decode as utf-8 are all None rather than an empty set, because an
    empty set reads as "the repository holds no files".
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return frozenset(_nul_paths(result.stdout))


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
