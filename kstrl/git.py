"""Git operations for kstrl."""

from __future__ import annotations

import re as _re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kstrl.ui.base import UI

DEFAULT_TIMEOUT = 30.0

# Network fetches get a longer budget than local plumbing calls.
FETCH_TIMEOUT = 120.0


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
                "git", "rev-parse", "--verify", "--quiet",
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
        return (
            result.stderr.strip()
            or f"git fetch origin {base_branch} failed"
        )
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
    branch: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
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
    cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
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
            files.update(
                line.strip() for line in result.stdout.splitlines() if line.strip()
            )

        # Staged changes
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            files.update(
                line.strip() for line in result.stdout.splitlines() if line.strip()
            )

    except subprocess.TimeoutExpired:
        pass

    files.update(get_untracked_files(cwd, timeout))
    return files


def get_untracked_files(
    cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
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
    return {
        line.strip() for line in result.stdout.splitlines() if line.strip()
    }


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
    cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
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
    ref: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """The sha ``ref`` names, or None when it does not resolve.

    Tries the local ref first, then ``origin/<ref>``: a fresh worktree
    may carry the remote-tracking ref only.
    """
    for candidate in (ref, f"origin/{ref}"):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                cwd=cwd, capture_output=True, text=True, timeout=timeout,
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
    ref: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
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
                "git", "restore", f"--source={ref}", "--staged", "--worktree",
                "--", file,
            ],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def remove_from_index(
    file: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
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
    file: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
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
    file: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
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
        path for _, path in get_diff_name_status(
            base_branch, cwd, timeout, strict=strict,
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
                "git", "diff", "--name-status", "-z", "-M", "-C",
                f"{base_ref}...HEAD", "--",
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
                f"git diff --name-status against {base_ref} timed out "
                f"after {timeout}s"
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
            records.extend(
                (status, path) for path in tokens[i + 1:i + 3] if path
            )
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
        raise GitDiffError(
            f"git diff against {base_ref} timed out after {timeout}s"
        ) from exc
    if result.returncode != 0:
        raise GitDiffError(
            f"git diff against {base_ref} exited "
            f"{result.returncode}: {result.stderr.strip()[:500]}"
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
                f"git diff --numstat against {base_ref} timed out "
                f"after {timeout}s"
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


# Shared budget for diff content injected into LLM prompts. Centralized
# here so review / security / knowledge prompts truncate to the same
# limit; if the LLM context window changes, edit one place.
DEFAULT_PROMPT_DIFF_CHAR_LIMIT = 50_000


def truncate_diff_for_prompt(
    diff_content: str, limit: int = DEFAULT_PROMPT_DIFF_CHAR_LIMIT,
) -> str:
    """Truncate a diff string for inclusion in an LLM prompt.

    Appends a single trailing line noting the truncation so the reviewer
    knows it isn't seeing the full diff.
    """
    if len(diff_content) <= limit:
        return diff_content
    return diff_content[:limit] + f"\n... (diff truncated at {limit // 1000}KB)"


class DiffUnsplittableError(ValueError):
    """An oversized diff cannot be split into <=limit chunks: a single
    *hunk* alone exceeds the limit, a file's header leaves no room for
    hunks, or the diff has no ``diff --git`` / ``@@`` boundaries at all.

    R1.4 (H-16): hard-mode callers must treat this as fail-closed - the
    diff cannot be fully reviewed, so it must not merge."""


# Start of a per-file segment in `git diff` output. Anchored to line
# start so a "diff --git" string INSIDE a hunk (e.g. a diff quoted in a
# test fixture) only splits when it begins a line - the worst case of a
# crafted in-hunk marker is a smaller-than-necessary chunk, never
# dropped content, because chunks are contiguous slices of the input.
_DIFF_FILE_BOUNDARY_RE = _re.compile(r"^diff --git ", _re.MULTILINE)

# Start of a hunk inside one file's segment. The trailing space keeps
# combined-diff markers ("@@@ ...") out, and unified-diff content lines
# are always prefixed with ' ', '+' or '-', so a line that begins with
# "@@ " is a real hunk header (a diff quoted inside a fixture shows up
# as "+@@ ..."). As with the file boundary, a missed boundary only
# produces a coarser split, never dropped content: parts are contiguous
# slices of the segment.
_DIFF_HUNK_BOUNDARY_RE = _re.compile(r"^@@ ", _re.MULTILINE)

# Headroom reserved inside each chunk for the one-line provenance
# header split_diff_for_prompt prepends, so header + content <= limit.
_CHUNK_HEADER_RESERVE = 120

# Provenance headers for a chunk. The file-boundary wording is the
# pre-R8 text and is kept byte-identical for chunks that carry only
# whole files, so diffs that already split cleanly chunk exactly as
# before; chunks carrying a within-file part get the accurate wording
# instead (a reviewer must not be told "file boundaries" while holding
# a fragment of a file).
_CHUNK_HEADER = (
    "# [kstrl R1.4] diff chunk {i} of {n}: oversized diff split on file "
    "boundaries; other files are in other chunks\n"
)
_CHUNK_HEADER_PARTIAL_FILE = (
    "# [kstrl R1.4] diff chunk {i} of {n}: oversized diff split on "
    "file/hunk boundaries; the rest is in other chunks\n"
)

# Markers prepended to each part of a file that had to be split within
# itself. Every part names the file and its part number so a reviewer
# cannot read one part as the file's whole change; parts after the
# first also carry a repeat of the file header (diff --git / --- / +++)
# so the part is a self-describing, reviewable unit, and the marker
# says the header is a repeat so it is not read as a second change.
_FILE_PART_MARKER = (
    "# [kstrl R1.4] file part {i} of {n}: {path} - this file's diff is "
    "split on hunk boundaries; the other parts are in other chunks\n"
)
_FILE_PART_CONTINUED_MARKER = (
    "# [kstrl R1.4] file part {i} of {n}: {path} - continued; the file "
    "header below is repeated for context, not a second change\n"
)


@dataclass(frozen=True)
class _DiffUnit:
    """One indivisible piece of a diff for packing purposes: either a
    whole file's segment or one within-file part."""

    text: str
    is_file_part: bool


def _file_segment_label(header: str) -> str:
    """Human-readable path for a file segment's part markers.

    Best-effort: the label is provenance for the reviewer, not a parsed
    path, so anything unrecognized falls back to the raw first line.
    """
    first_line = header.split("\n", 1)[0]
    if first_line.startswith("diff --git a/"):
        a_path = first_line[len("diff --git a/"):].split(" b/", 1)[0]
        if a_path:
            return a_path[:200]
    return first_line[:200] or "(unnamed diff segment)"


def _split_file_segment(segment: str, budget: int) -> list[_DiffUnit]:
    """R8: split ONE file's diff segment into ``<=budget`` parts on
    ``@@`` hunk boundaries, repeating the file header on every part.

    Motivated by a 2026-07-27 factory run: hard-mode review halted on
    ``single-file diff segment is 55710 chars`` for a legitimately large
    test file, and recovery cost a full engineer-loop pass ($3.99) to
    repackage a diff the harness simply could not chunk. A file bigger
    than the per-chunk budget is a normal outcome, not misbehaviour.

    Raises :class:`DiffUnsplittableError` when even hunk granularity is
    not enough (one hunk over the budget, or no hunks at all). That
    floor is deliberate: R1.4 forbids truncating a diff that is being
    reviewed for correctness, so an unreviewable diff must fail closed.
    """
    starts = [m.start() for m in _DIFF_HUNK_BOUNDARY_RE.finditer(segment)]
    first_line = segment.split("\n", 1)[0][:200]
    if not starts:
        raise DiffUnsplittableError(
            f"single-file diff segment is {len(segment)} chars, over the "
            f"{budget}-char per-chunk budget, and contains no '@@ ' hunk "
            f"boundaries to split on ({first_line})"
        )
    header = segment[: starts[0]]
    hunks = [
        segment[start:end]
        for start, end in zip(
            starts, starts[1:] + [len(segment)], strict=True,
        )
    ]
    path = _file_segment_label(header)

    # The marker cannot be rendered until the part count is known, so
    # reserve its worst-case width now. Part indices and the part count
    # are both bounded by the hunk count, so formatting with that count
    # bounds the marker's width exactly - no guessed digit padding.
    widest = len(hunks)
    reserve = max(
        len(_FILE_PART_MARKER.format(i=widest, n=widest, path=path)),
        len(_FILE_PART_CONTINUED_MARKER.format(i=widest, n=widest, path=path)),
    )
    content_budget = budget - reserve - len(header)
    if content_budget <= 0:
        raise DiffUnsplittableError(
            f"file header for {path} is {len(header)} chars, leaving no "
            f"room for hunks in the {budget}-char per-chunk budget "
            f"({first_line})"
        )

    # Greedy packing preserves hunk order, so parts stay contiguous
    # slices of the segment and the reassembly invariant holds.
    groups: list[list[str]] = [[]]
    size = 0
    for hunk in hunks:
        if len(hunk) > content_budget:
            hunk_line = hunk.split("\n", 1)[0][:200]
            raise DiffUnsplittableError(
                f"single hunk in {path} is {len(hunk)} chars, over the "
                f"{content_budget} chars left for hunk content after the "
                f"{len(header)}-char file header; a diff cannot be split "
                f"below hunk granularity ({hunk_line})"
            )
        if groups[-1] and size + len(hunk) > content_budget:
            groups.append([])
            size = 0
        groups[-1].append(hunk)
        size += len(hunk)

    total = len(groups)
    parts: list[_DiffUnit] = []
    for i, group in enumerate(groups, 1):
        template = (
            _FILE_PART_MARKER if i == 1 else _FILE_PART_CONTINUED_MARKER
        )
        marker = template.format(i=i, n=total, path=path)
        text = marker + header + "".join(group)
        if len(text) > budget:  # defensive: the reserve math prevents this
            raise DiffUnsplittableError(
                f"file part {i}/{total} of {path} is {len(text)} chars, "
                f"over the {budget}-char per-chunk budget"
            )
        parts.append(_DiffUnit(text=text, is_file_part=True))
    return parts


def split_diff_for_prompt(
    diff_content: str, limit: int = DEFAULT_PROMPT_DIFF_CHAR_LIMIT,
) -> list[str]:
    """R1.4 (H-16): split an oversized diff into chunks on ``diff --git``
    file boundaries, each at most ``limit`` chars including a one-line
    provenance header, so every byte of the diff can be reviewed across
    multiple passes instead of head-truncating (which lets a rogue
    engineer pad the first 50KB with benign churn and land a malicious
    hunk after the cut).

    R8: a file whose own diff exceeds the per-chunk budget is split
    further on ``@@`` hunk boundaries (see :func:`_split_file_segment`)
    instead of failing the component; before that, one oversized file
    cost a full engineer-loop pass to repackage.

    Returns ``[diff_content]`` unchanged when it already fits. Raises
    :class:`DiffUnsplittableError` when even hunk granularity cannot
    produce compliant chunks (a single hunk over the limit, or no
    boundaries found at all).

    Invariant: concatenating the chunks with the injected provenance
    lines (and the file header repeated on continuation parts) removed
    reproduces the input exactly - chunking never drops content.
    """
    if limit <= _CHUNK_HEADER_RESERVE:
        raise ValueError(
            f"limit must exceed the {_CHUNK_HEADER_RESERVE}-char header "
            f"reserve, got {limit}"
        )
    if len(diff_content) <= limit:
        return [diff_content]

    boundaries = [
        m.start() for m in _DIFF_FILE_BOUNDARY_RE.finditer(diff_content)
    ]
    if not boundaries:
        raise DiffUnsplittableError(
            f"diff is {len(diff_content)} chars (limit {limit}) but "
            "contains no 'diff --git' file boundaries to split on"
        )

    # Per-file segments; any preamble before the first boundary becomes
    # its own leading segment so no content is lost.
    starts = [0] if boundaries[0] != 0 else []
    starts.extend(boundaries)
    segments = [
        diff_content[start:end]
        for start, end in zip(
            starts, starts[1:] + [len(diff_content)], strict=True,
        )
    ]

    budget = limit - _CHUNK_HEADER_RESERVE
    # R8: an over-budget file is split within itself rather than being
    # a hard stop; files that already fit stay whole, so multi-file
    # diffs that split cleanly pack exactly as they did before.
    units: list[_DiffUnit] = []
    for seg in segments:
        if len(seg) > budget:
            units.extend(_split_file_segment(seg, budget))
        else:
            units.append(_DiffUnit(text=seg, is_file_part=False))

    # Greedy packing preserves unit order, so contiguity (and the
    # reassembly invariant) holds.
    packed: list[list[_DiffUnit]] = [[]]
    size = 0
    for unit in units:
        if packed[-1] and size + len(unit.text) > budget:
            packed.append([])
            size = 0
        packed[-1].append(unit)
        size += len(unit.text)

    total = len(packed)
    chunks = []
    for i, group in enumerate(packed, 1):
        # Provenance only, no reviewer directives: prompt-body guidance
        # about truncated/chunked diffs is Session 8C's calibrated change.
        template = (
            _CHUNK_HEADER_PARTIAL_FILE
            if any(u.is_file_part for u in group)
            else _CHUNK_HEADER
        )
        header = template.format(i=i, n=total)
        chunk = header + "".join(u.text for u in group)
        if len(chunk) > limit:  # defensive: budget math above prevents this
            raise DiffUnsplittableError(
                f"chunk {i}/{total} is {len(chunk)} chars, over limit {limit}"
            )
        chunks.append(chunk)
    return chunks


# E2: regex matches a Self-Critique block in a diff. Used by the
# reviewer-prep step to remove the engineer's self-reported failure
# modes from what the reviewer sees, so the reviewer is not biased
# toward "the implementer already thought of that" and skips checking.
_SELF_CRITIQUE_BLOCK_RE = _re.compile(
    r"""
    \+\#{2,3}\s+Self[-\s]Critique[\s\S]*?       # heading + content
    (?=                                          # stop before:
        ^\+\#{1,6}\s                             #   any other heading
      | ^\+---\s*$                               #   ---  separator
      | ^[^+]                                    #   non-add line
      | \Z                                       #   end of string
    )
    """,
    _re.MULTILINE | _re.VERBOSE | _re.IGNORECASE,
)


def strip_self_critique_from_diff(diff_content: str) -> str:
    """Remove the engineer's Self-Critique block from a diff before
    showing it to the reviewer.

    The Self-Critique block is the engineer's self-reported list of
    failure modes (verify.py mandates >=3 bullets). If the reviewer
    sees it inline in progress.txt's diff, the reviewer is biased to
    think those failure modes are already handled. The reviewer should
    arrive at its concerns independently.

    Returns the diff with the block stripped; if no block is found,
    returns the input unchanged.
    """
    return _SELF_CRITIQUE_BLOCK_RE.sub("", diff_content)


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
