"""No-progress circuit breaker for the engineer loop (R7.5).

The most-repeated community fix for kstrl-loop stalls: an agent that
keeps burning iterations without changing the tree must be halted loudly
instead of spending the whole iteration budget re-reading the same prompt.

The breaker fingerprints the worktree after every non-completing
iteration. When ``no_progress_iterations`` consecutive iterations end
with an UNCHANGED diff hash, the loop halts with a distinct error that
the factory records in the progress log and the evolution journal.

It reads the diff only (#696 decision 10). The test probe it also ran,
``[breaker] test_command``, is retired with every other command source
kstrl did not take from a confirmed ``[stack]`` (#696 slice 4). What that
loses, stated: a suite whose outcome depends on external state the agent
is changing no longer resets the streak on an unchanged tree.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kstrl.config_numbers import check_numbers

# Distinct, greppable error prefix. loop.py builds its halt message with
# it and the factory routes on the typed LoopResult/ComponentResult flag
# (never on this string - it is for humans and logs).
NO_PROGRESS_MESSAGE_PREFIX = "no-progress circuit breaker tripped"

_GIT_TIMEOUT = 60.0

# Fingerprinting caps: past these the untracked-file walk falls back to
# names+sizes so a worktree full of build artifacts cannot stall the
# breaker itself.
_MAX_HASHED_UNTRACKED_FILES = 500
_MAX_HASHED_UNTRACKED_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class BreakerConfig:
    """Configuration for the no-progress circuit breaker.

    ``no_progress_iterations`` is the N of "halt after N consecutive
    no-progress iterations" (default 3, the community norm); 0 disables
    the breaker.
    """

    no_progress_iterations: int = 3

    @classmethod
    def from_env(cls) -> BreakerConfig:
        """Load breaker config from environment variables only."""
        config = cls()
        return cls(
            no_progress_iterations=int(
                os.environ.get("KSTRL_BREAKER_ITERATIONS", str(config.no_progress_iterations))
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> BreakerConfig:
        """Load breaker config with precedence: env > toml > defaults.

        Reads the ``[breaker]`` section from ``<root_dir>/kstrl.toml``.
        """
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "breaker")
        no_progress_iterations = cls.no_progress_iterations
        if "no_progress_iterations" in section:
            no_progress_iterations = int(section["no_progress_iterations"])
        if "KSTRL_BREAKER_ITERATIONS" in os.environ:
            no_progress_iterations = int(os.environ["KSTRL_BREAKER_ITERATIONS"])
        return check_numbers(cls(no_progress_iterations=no_progress_iterations))


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a git command; None on any failure (breaker fails open).

    ``UnicodeDecodeError`` joins the tuple because ``-z`` (#423) makes raw,
    possibly non-utf-8 bytes reachable on the status spawn; None is the
    breaker's documented "cannot measure", which the caller turns into a
    skipped stall count rather than a raised error.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _untracked_from_status_z(status: str) -> list[str]:
    """Untracked paths from ``git status --porcelain -uall -z``.

    Records are NUL-separated. Each is ``XY <path>``; a rename or copy
    (X in "RC") carries its SOURCE as one extra NUL-separated field with
    no XY code of its own, so a walk that treats every token as a record
    can read a source file literally named ``?? x`` as untracked.
    Measured on git 2.47.1: ``R  new.py\0old.py\0``.
    """
    paths: list[str] = []
    tokens = status.split("\0")
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        if code[0] in ("R", "C"):
            i += 1
        if code == "??":
            paths.append(path)
    return paths


def compute_diff_hash(cwd: Path) -> str | None:
    """Fingerprint the worktree state relative to its git history.

    Covers every way an engineer iteration can make progress: new
    commits (HEAD moves), staged/unstaged edits to tracked files
    (``git diff HEAD``), and untracked files (listed by ``git status
    --porcelain -uall`` with their CONTENT hashed - the status line
    alone would miss an edit inside an already-untracked file).

    Returns None when ``cwd`` is not a usable git repo or git itself
    fails; callers must treat None as "cannot measure" and skip the
    stall count for that iteration (fail open, never fail the
    component on the breaker's own infrastructure).
    """
    status = _git(["status", "--porcelain", "-uall", "-z"], cwd)
    if status is None:
        return None

    head = _git(["rev-parse", "HEAD"], cwd)
    if head is None:
        # Repo without commits yet: fingerprint from status + untracked
        # content only.
        head = "no-head"
        diff = ""
    else:
        tracked_diff = _git(["diff", "HEAD"], cwd)
        if tracked_diff is None:
            return None
        diff = tracked_diff

    hasher = hashlib.sha256()
    hasher.update(head.encode())
    hasher.update(b"\x00")
    hasher.update(status.encode())
    hasher.update(b"\x00")
    hasher.update(diff.encode())

    untracked = _untracked_from_status_z(status)
    hashed_bytes = 0
    for index, rel in enumerate(sorted(untracked)):
        hasher.update(b"\x00")
        hasher.update(rel.encode())
        path = cwd / rel
        try:
            size = path.stat().st_size
        except OSError:
            continue
        over_caps = (
            index >= _MAX_HASHED_UNTRACKED_FILES
            or hashed_bytes + size > _MAX_HASHED_UNTRACKED_BYTES
        )
        if over_caps:
            # Names + sizes only past the caps: coarser, but a stalled
            # agent that stops changing anything still fingerprints
            # identically, which is the property the breaker needs.
            hasher.update(str(size).encode())
            continue
        try:
            hasher.update(path.read_bytes())
            hashed_bytes += size
        except OSError:
            continue
    return hasher.hexdigest()


class NoProgressBreaker:
    """Tracks the stall streak across one engineer loop's iterations.

    Usage: construct once before iteration 1 (captures the baseline
    fingerprint), call :meth:`record_iteration` after every iteration
    that did not complete; a True return means the breaker tripped and
    the loop must halt.
    """

    def __init__(self, cwd: Path, config: BreakerConfig) -> None:
        self._cwd = cwd
        self._config = config
        self._enabled = config.no_progress_iterations > 0
        self._stall_count = 0
        self._prev_fingerprint: str | None = compute_diff_hash(cwd) if self._enabled else None

    @property
    def enabled(self) -> bool:
        """False when disabled by config OR the baseline fingerprint
        could not be computed (not a git repo)."""
        return self._enabled and self._prev_fingerprint is not None

    @property
    def stall_count(self) -> int:
        return self._stall_count

    def record_iteration(self) -> bool:
        """Fold one completed (non-COMPLETE) iteration into the streak.

        Returns True when the configured threshold of consecutive
        no-progress iterations has been reached.
        """
        if not self.enabled:
            return False
        fingerprint = compute_diff_hash(self._cwd)
        if fingerprint is None:
            # Cannot measure this iteration: reset rather than guess.
            self._stall_count = 0
            return False
        if fingerprint != self._prev_fingerprint:
            self._prev_fingerprint = fingerprint
            self._stall_count = 0
            return False
        self._stall_count += 1
        return self._stall_count >= self._config.no_progress_iterations

    def halt_message(self) -> str:
        return (
            f"{NO_PROGRESS_MESSAGE_PREFIX}: {self._stall_count} consecutive "
            f"iteration(s) produced an unchanged diff hash; "
            f"halting component instead of burning further iterations"
        )
