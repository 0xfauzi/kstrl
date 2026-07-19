"""No-progress circuit breaker for the engineer loop (R7.5).

The most-repeated community fix for Ralph-loop stalls: an agent that
keeps burning iterations without changing the tree (or its test
outcome) must be halted loudly instead of spending the whole iteration
budget re-reading the same prompt.

The breaker fingerprints the worktree after every non-completing
iteration. When ``no_progress_iterations`` consecutive iterations end
with an UNCHANGED diff hash AND an UNCHANGED test-failure signature,
the loop halts with a distinct error that the factory records in the
progress log and the evolution journal.

First-principles note on the AND: an identical tree implies identical
test results for a deterministic suite, so the test probe only runs
when the diff hash already matched (cheap short-circuit). Its job is
to protect against the one false-trip mode diff hashing cannot see -
tests whose outcome depends on external state the agent is legitimately
working on. A flaky suite changes the signature between probes, which
RESETS the stall streak: the breaker fails open, never spuriously.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
    the breaker. ``test_command`` is the optional stall probe run ONLY
    when the diff hash already matched the previous iteration; when
    unset the signature is a constant, which makes the breaker
    effectively diff-hash-only (an identical tree implies identical
    deterministic test results, so this loses nothing for hermetic
    suites - stated here rather than assumed silently, H4).
    """

    no_progress_iterations: int = 3
    test_command: str | None = None
    test_timeout: float = 300.0

    @classmethod
    def from_env(cls) -> BreakerConfig:
        """Load breaker config from environment variables only."""
        config = cls()
        return cls(
            no_progress_iterations=int(
                os.environ.get(
                    "RALPH_BREAKER_ITERATIONS",
                    str(config.no_progress_iterations),
                )
            ),
            test_command=os.environ.get("RALPH_BREAKER_TEST_CMD") or None,
            test_timeout=float(
                os.environ.get(
                    "RALPH_BREAKER_TEST_TIMEOUT", str(config.test_timeout),
                )
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> BreakerConfig:
        """Load breaker config with precedence: env > toml > defaults.

        Reads the ``[breaker]`` section from ``<root_dir>/ralph.toml``.
        """
        from ralph_py.config import load_toml_section

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(root_dir / "ralph.toml", "breaker")
        no_progress_iterations = cls.no_progress_iterations
        test_command = cls.test_command
        test_timeout = cls.test_timeout
        if "no_progress_iterations" in section:
            no_progress_iterations = int(section["no_progress_iterations"])
        if isinstance(section.get("test_command"), str) and section["test_command"]:
            test_command = str(section["test_command"])
        if "test_timeout" in section:
            test_timeout = float(section["test_timeout"])
        if "RALPH_BREAKER_ITERATIONS" in os.environ:
            no_progress_iterations = int(os.environ["RALPH_BREAKER_ITERATIONS"])
        if os.environ.get("RALPH_BREAKER_TEST_CMD"):
            test_command = os.environ["RALPH_BREAKER_TEST_CMD"]
        if "RALPH_BREAKER_TEST_TIMEOUT" in os.environ:
            test_timeout = float(os.environ["RALPH_BREAKER_TEST_TIMEOUT"])
        return cls(
            no_progress_iterations=no_progress_iterations,
            test_command=test_command,
            test_timeout=test_timeout,
        )


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a git command; None on any failure (breaker fails open)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


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
    status = _git(["status", "--porcelain", "-uall"], cwd)
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

    untracked = [
        line[3:]
        for line in status.splitlines()
        if line.startswith("?? ")
    ]
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Volatile tokens that differ between two runs of the SAME failing
# suite on the SAME tree: wall-clock durations, memory addresses,
# tmp-path suffixes pytest generates per run.
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?s\b")
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_TMP_PATH_RE = re.compile(r"/(?:tmp|var/folders)/[^\s'\"]+")

NO_TEST_COMMAND_SIGNATURE = "no-test-command"


def _normalize_test_line(line: str) -> str:
    line = _ANSI_RE.sub("", line)
    line = _DURATION_RE.sub("<T>", line)
    line = _HEX_ADDR_RE.sub("<ADDR>", line)
    line = _TMP_PATH_RE.sub("<TMP>", line)
    return line.rstrip()


def compute_test_signature(cwd: Path, config: BreakerConfig) -> str:
    """Hash of the configured test command's failure shape.

    The signature is the return code plus the normalized failure lines
    (lines mentioning fail/error), with volatile tokens (durations,
    addresses, tmp paths) masked so two runs on an identical tree hash
    identically. A passing suite signs as its return code alone - an
    agent stuck with green tests but no completion marker is still a
    stall worth halting.
    """
    if not config.test_command:
        return NO_TEST_COMMAND_SIGNATURE
    from ralph_py.verify import run_scrubbed

    try:
        result = run_scrubbed(
            config.test_command, cwd=cwd, timeout=config.test_timeout,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError as exc:
        return f"probe-error:{type(exc).__name__}"
    failure_lines = [
        _normalize_test_line(line)
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if re.search(r"fail|error", line, re.IGNORECASE)
    ]
    hasher = hashlib.sha256()
    hasher.update(str(result.returncode).encode())
    for line in failure_lines:
        hasher.update(b"\x00")
        hasher.update(line.encode())
    return f"rc{result.returncode}:{hasher.hexdigest()}"


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
        self._prev_fingerprint: str | None = (
            compute_diff_hash(cwd) if self._enabled else None
        )
        self._prev_signature: str | None = None

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
            self._prev_signature = None
            return False
        if fingerprint != self._prev_fingerprint:
            self._prev_fingerprint = fingerprint
            self._stall_count = 0
            self._prev_signature = None
            return False
        # Tree unchanged by this iteration: consult the test probe.
        signature = compute_test_signature(self._cwd, self._config)
        if self._prev_signature is not None and signature != self._prev_signature:
            # Same tree but a different test outcome: external state
            # moved (or the suite is flaky). Restart the streak at this
            # iteration instead of tripping on it.
            self._stall_count = 1
        else:
            self._stall_count += 1
        self._prev_signature = signature
        return self._stall_count >= self._config.no_progress_iterations

    def halt_message(self) -> str:
        probe = (
            "unchanged test-failure signature"
            if self._config.test_command
            else "no test probe configured (diff hash only)"
        )
        return (
            f"{NO_PROGRESS_MESSAGE_PREFIX}: {self._stall_count} consecutive "
            f"iteration(s) produced an unchanged diff hash and {probe}; "
            f"halting component instead of burning further iterations"
        )
