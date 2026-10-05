"""The command that installs a kstrl worktree's own dependencies (#624).

Every kstrl worktree is nested under the project root, so a tool that
resolves dependencies by walking up the directory tree finds the ROOT
checkout's copy when the worktree has none. Node does this: a gate run in
``.kstrl/worktrees/<run>/<comp>`` with no ``node_modules`` of its own
resolved every package from ``<root>/node_modules``, so a branch that
added or upgraded a dependency was measured against whatever the operator
had installed, a false fail or a false pass, with nothing in the output
saying so.

``[factory] worktree_setup_command`` names the command that gives a
worktree its own dependency tree. It runs where the branch's lockfile is
what the tree holds: in a component worktree before the engineer starts
and again before Phase 1, and in a contract or integration worktree after
its merges and before its tests or reviewer. A component's ``scaffold``
replaces it for that component's worktree. Empty means no setup.

It runs through :func:`kstrl.verify.run_scrubbed`, so it gets exactly the
environment the gates get (no secrets) and, on timeout, its whole process
group is killed, not only the shell. ``worktree_setup_timeout`` of 0
means no limit (#467).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from kstrl.rung import ProvenRung
from kstrl.timeout import limit_seconds
from kstrl.verify import ChildOutputDecodeError, run_scrubbed

#: How many of a failed setup's last output lines the error keeps.
OUTPUT_TAIL_LINES = 20


@dataclass(frozen=True)
class WorktreeSetup:
    """One worktree setup: the shell command and its time limit in seconds."""

    command: str = ""
    timeout: float = 0.0
    #: A ``[stack]``'s ``env`` (#696): the setup sees the stack's scrub,
    #: as its checks do. None is the allowlist every gate gets without one.
    env: tuple[str, ...] | None = None
    #: #700 slice 2: the SETUP-zone rung of a run under a ``[stack]``; the
    #: setup runs inside it. None runs on the host.
    rung: ProvenRung | None = None
    #: Why this setup must not run, or "": a ``[stack]`` no person
    #: confirmed (#696 slice 3, ``Stack.unconfirmed``) runs nothing.
    refusal: str = ""

    def prepare(self, worktree: Path) -> str:
        """Run the setup in ``worktree``; "" on success or when there is none.

        A failure comes back as one sentence naming the command and what
        went wrong, followed by the last lines of its output. A refused
        setup is a failure, whatever its command, so nothing proceeds on it.
        """
        head = f"worktree setup `{self.command}`"
        if self.refusal:
            return f"{head} not run: {self.refusal}"
        if not self.command:
            return ""
        try:
            result = run_scrubbed(
                self.command,
                cwd=worktree,
                timeout=limit_seconds(self.timeout),
                declared_env=self.env,
                rung=self.rung,
            )
        except subprocess.TimeoutExpired:
            return f"{head} did not finish within {self.timeout}s; its process group was killed"
        except ChildOutputDecodeError as exc:
            return f"{head} wrote output that could not be decoded: {exc}"
        except OSError as exc:
            return f"{head} could not start in {worktree}: {exc}"
        if result.returncode == 0:
            return ""
        tail = (result.stdout + result.stderr).strip().splitlines()[-OUTPUT_TAIL_LINES:]
        return "\n".join([f"{head} exited {result.returncode}", *tail])


#: No setup: what a caller that was given none runs.
NO_SETUP = WorktreeSetup()
