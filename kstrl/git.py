"""Git operations for kstrl."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
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
