"""Git operations for kstrl."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kstrl.ui.base import UI

DEFAULT_TIMEOUT = 30.0


def is_git_repo(path: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            encoding="utf-8",
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
            encoding="utf-8",
            timeout=timeout,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


#: The variables that point git at a repository other than the one it
#: finds from its working directory. :func:`git_common_dir` removes them
#: (#700, the security review of #765): both zones of the rung read the
#: directory it names.
REPOSITORY_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def git_common_dir(cwd: Path, timeout: float = DEFAULT_TIMEOUT) -> Path | None:
    """The repository's git common directory (``git rev-parse
    --git-common-dir``) as a resolved path, or None when ``cwd`` is not in
    a repository. Every linked worktree's own git directory is under it.
    Git runs with :data:`REPOSITORY_ENV` removed from its environment, so
    the answer is the repository git finds from ``cwd``."""
    env = {name: value for name, value in os.environ.items() if name not in REPOSITORY_ENV}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    found = result.stdout.strip()
    if result.returncode != 0 or not found:
        return None
    return Path(os.path.realpath(cwd / found))


#: The files a commit writes in the git dir of its checkout, each also with
#: its ``.lock`` (#700). A codex writable root is a path prefix, so naming
#: these, and not the git dir, keeps ``hooks``, ``config``, ``commondir`` and
#: ``config.worktree`` read-only (measured with codex-cli 0.156.1).
_COMMIT_FILES = ("index", "HEAD", "ORIG_HEAD", "AUTO_MERGE", "COMMIT_EDITMSG")


def git_write_paths(path: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> list[Path]:
    """The paths a ``git commit`` in ``path`` writes outside its working
    tree, or [] outside a repository (#700).

    The common dir's ``objects``, ``refs``, ``logs`` and ``packed-refs``, and
    in the git dir of the checkout its ``logs`` and :data:`_COMMIT_FILES`.
    Never a git dir itself: a sandboxed command that can write ``hooks``,
    ``config`` or ``commondir`` makes git run a command of its choice outside
    every sandbox. A plain checkout and a linked worktree get the same set.
    A directory that does not exist is left out. A file is named whether it
    exists or not, because a commit creates its lock.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            cwd=path,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return []
    git_dir, common = Path(lines[0]), Path(lines[1])
    dirs = [common / "objects", common / "refs", common / "logs", git_dir / "logs"]
    files = [
        base / f"{name}{lock}"
        for base, name in [*((git_dir, n) for n in _COMMIT_FILES), (common, "packed-refs")]
        for lock in ("", ".lock")
    ]
    return list(dict.fromkeys([*(d for d in dirs if d.is_dir()), *files]))


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


def current_branch(
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """The branch HEAD is on, or None (detached, unborn, not a repo).

    ``--abbrev-ref`` prints the literal ``HEAD`` on a detached head, and
    that is reported as None rather than passed through: a caller
    comparing this against a branch NAME must not get a match from a
    string that is not a branch.

    Read-only, and the counterpart of :func:`branch_exists`: together
    they answer whether a :func:`checkout_branch` would move the working
    tree, which is what #288's pre-implement baseline has to know before
    it is worth measuring anything.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    if not name or name == "HEAD":
        return None
    return name


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
                encoding="utf-8",
                timeout=timeout,
            )
        else:
            ui.info(f"Branch: creating branch {branch}{source_suffix}")
            result = subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=cwd,
                capture_output=True,
                encoding="utf-8",
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


def _nul_paths(output: str) -> set[str]:
    """Paths from a git command run with ``-z``: NUL-separated, never
    quoted, and never stripped."""
    return {path for path in output.split("\0") if path}


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
            encoding="utf-8",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        # "not a repo" and "no git" are one answer to this question, and
        # the baseline asks it on the same line as get_origin_slug.
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def branch_sha(
    branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """The commit local branch ``branch`` points at, or None when it does
    not exist (or git cannot answer).

    #465: the merge gate records this when it parks a component, and the
    approval merges only a branch still at that commit.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_origin_slug(
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """``owner/repo`` from ``origin``'s URL, or None.

    Identity for an artifact one run writes and another reads (#227's sense
    baseline). The DIRECTORY name is the obvious answer and it is the wrong
    one here: every kstrl lane works in a git worktree named after an issue
    number, so a baseline written in one records ``227`` and every later
    comparison in a normal checkout reports a mismatch that means nothing.
    ``origin`` is the same string in a worktree, in a fresh clone and under
    ``actions/checkout``, which is the property the identity needs.

    Both URL shapes, because both are ordinary: ``https://host/owner/repo.git``
    and ``git@host:owner/repo.git``. A remote that is a local path yields its
    last two segments, which is a weaker answer but still a stable one; a repo
    with no remote at all yields None and the caller falls back.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        # OSError beside the timeout, the same breadth as resolve_base_ref
        # and _resolve_candidate: this function documents None for "no
        # remote", and a machine with no git at all is the same answer
        # from the caller's side. Without it, `ks check --compare-baseline`
        # raised FileNotFoundError out of the top of the run, AFTER paying
        # for the whole measurement, where it documents exit 2.
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    if not url:
        return None
    # `git@host:owner/repo.git` has no scheme; splitting on ":" first makes
    # both shapes the same problem.
    tail = url.rsplit(":", 1)[-1] if "://" not in url else url.split("://", 1)[1]
    parts = [part for part in tail.removesuffix(".git").split("/") if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else None)


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
    # Function-local: kstrl.git_base imports kstrl.git (cycle). G2 and G3 remove this.
    from kstrl.git_base import merge_base_ref, resolve_base_ref

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


def _undecodable_message(what: str, exc: UnicodeDecodeError) -> str:
    """Why a reader could not read output git DID produce.

    One sentence for all four readers, so four copies cannot drift, and
    written to hold at all four call sites: one of them, in
    ``resolve_base_sha``, runs ``git rev-parse`` and reads no diff, so the
    sentence says "its output" rather than "the diff" (#416's simplify
    review measured the earlier wording making a false claim there).
    ``exc`` renders the codec, the offending byte and its offset, which is
    the part an operator can act on: it is how they find the file. kstrl
    neither guesses another encoding nor replaces the byte (#409, and
    CLAUDE.md's encoding learning): output this process cannot decode is
    output it could not obtain, which is what :class:`GitDiffError`
    already means.
    """
    return (
        f"{what} produced bytes that are not valid utf-8, so its output could "
        f"not be read: {exc}. kstrl decodes git output as utf-8 and neither "
        f"guesses another encoding nor replaces the byte."
    )


class GitDiffError(RuntimeError):
    """Raised when ``git diff`` cannot produce a diff (nonzero exit,
    e.g. bad ref or not a repository, or a timeout, or output this process
    could not decode as utf-8 (#416)). Callers must treat
    this as an infrastructure failure: before R1.3 (H-14) these errors
    returned an empty string, and review/security/knowledge silently
    "reviewed" a diff of nothing and passed."""


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
#
# Both are H3-enrolled (#299): declared as ``*_PROMPT`` constants so the
# enrollment walk can see them, and interpolated by the function below
# each one. Per H3a the TEMPLATE is what is hashed - ``base_ref``, the
# per-run delimiter token and the diff bytes are not. See H3/H3a in
# docs/adversarial-roadmap.md.


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
            encoding="utf-8",
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
            encoding="utf-8",
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
            encoding="utf-8",
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
            encoding="utf-8",
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
