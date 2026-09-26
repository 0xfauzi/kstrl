"""A process left in a contract, integration or retry-evidence worktree dies
before that worktree is removed, and a warning names it (#528).

#461 swept the component worktrees. Three other worktrees kstrl creates were
removed with nothing killed: the Phase 3 contract worktrees, where the
project's own test command runs (a tier check, its bisection, and the
integrated check); the integration review's worktree, where a reviewer agent
with a shell tool runs; and the failed attempt's evidence worktree that
``ks retry`` removes. A process started in a session of its own in any of
them outlived the worktree, the phase and the run.

None of these worktrees belongs to one component, so there is no component
finding to hold the record. The record is a warning line, which a factory
run also writes to its events.jsonl.

Every process here is one a command the test supplied started, and whose pid
it wrote to a file outside the worktree (#292). Each ignores SIGTERM, as the
survivors #461 measured did, so only SIGKILL ends it.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.contract import ContractConfig, ContractMode
from kstrl.factory import FactoryResult
from kstrl.manifest import ComponentStatus
from tests.helpers import integration_harness as harness
from tests.helpers import procs
from tests.test_agent_processes_outlive_run import (
    COMPLETE,
    _factory,
    _repo,
    _stop_factory,
)
from tests.test_resume_ergonomics import _git, _init_git_repo, _make_manifest, _scaffold

#: Starts ``sleep 600`` with SIGTERM ignored, in a session of its own, in the
#: current directory, appends ``<pid> <cwd>`` to the file named by its one
#: argument and exits at once. Stdio is /dev/null, so it holds no pipe of the
#: command that ran it.
LEAVE_A_PROCESS = """\
import os, signal, subprocess, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(
    ["sleep", "600"],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(f"{child.pid} {os.getcwd()}\\n")
"""


def _leaves_a_process(tmp_path: Path, pidfile: Path) -> list[str]:
    """The argv that runs :data:`LEAVE_A_PROCESS` recording into ``pidfile``."""
    script = tmp_path / "leave_a_process.py"
    script.write_text(LEAVE_A_PROCESS, encoding="utf-8")
    return [sys.executable, str(script), str(pidfile)]


def _left(pidfile: Path) -> list[tuple[int, str]]:
    """Every ``(pid, cwd)`` the command recorded, in the order it ran."""
    if not pidfile.exists():
        return []
    rows = []
    for line in pidfile.read_text(encoding="utf-8").splitlines():
        pid, cwd = line.split(" ", 1)
        rows.append((int(pid), cwd))
    return rows


def _kill(*pidfiles: Path) -> None:
    for pidfile in pidfiles:
        for pid, _cwd in _left(pidfile):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _names(text: str, pid: int, phase: str) -> bool:
    """Whether one line of ``text`` is the ``phase`` warning naming ``pid``."""
    return any(
        f"orphan_process ({phase})" in line and f"pid {pid} " in line for line in text.splitlines()
    )


def _run_events(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    )


@pytest.mark.parametrize(
    ("test_exit", "worktrees"),
    [(0, ["tier0"]), (1, ["bisect", "tier0"])],
    ids=["passing", "failing-then-bisected"],
)
def test_the_contract_check_kills_and_names_what_its_test_command_left(
    tmp_path: Path, test_exit: int, worktrees: list[str]
) -> None:
    """The real CLI with ``--contract-check final``. A failing check also
    bisects, in a second worktree of its own, and the same command runs
    there too."""
    root = _repo(tmp_path)
    pidfile = tmp_path / "contract.pids"
    command = f"{shlex.join(_leaves_a_process(tmp_path, pidfile))} && exit {test_exit}"
    proc = _factory(
        root, COMPLETE, "1", "--contract-check", "final", "--contract-test-cmd", command
    )
    try:
        out, _ = proc.communicate(timeout=240)
        assert (proc.returncode == 0) is (test_exit == 0), out
        left = _left(pidfile)
        assert sorted(Path(cwd).name.split("-")[0] for _pid, cwd in left) == worktrees, (
            f"precondition: the contract command ran once in each worktree: {left}\n{out}"
        )
        events = _run_events(root)
        for pid, cwd in left:
            assert procs.wait_for_pid_to_die(pid, timeout=10), (
                f"pid {pid}, left in the contract worktree {cwd}, outlived its removal"
            )
            assert _names(events, pid, "contract"), (
                f"no contract warning in events.jsonl names pid {pid}"
            )
    finally:
        _stop_factory(proc)
        _kill(pidfile)


class _ReviewerThatLeavesAProcess(harness.FakeReviewer):
    """The fake reviewer, plus one shell command run in the worktree it is
    handed, which is what a reviewer agent's shell tool does."""

    def __init__(self, output: str, command: list[str]) -> None:
        super().__init__(output)
        self._command = command

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        subprocess.run(self._command, cwd=cwd, check=True, timeout=30)
        yield from super().run(prompt, cwd, timeout)


def test_the_integrated_check_and_the_integration_review_kill_and_name_what_they_left(
    tmp_path: Path,
) -> None:
    """The real ``run_factory`` in create_prs mode over a merged feature: the
    Phase 3 integrated check's test command leaves one process and the
    integration reviewer leaves another, each in its own worktree."""
    root = tmp_path / "repo"
    base, _head = harness.merged_feature(root)
    check_pids = tmp_path / "check.pids"
    review_pids = tmp_path / "review.pids"
    reviewer = _ReviewerThatLeavesAProcess(
        json.dumps(harness.review_payload(root, base)),
        _leaves_a_process(tmp_path, review_pids),
    )
    try:
        _result, out = harness.run_factory_over(
            root,
            reviewer,
            contract_config=ContractConfig(
                mode=ContractMode.TIER.value,
                test_command=shlex.join(_leaves_a_process(tmp_path, check_pids)),
                timeout=60.0,
            ),
        )
        for pidfile, label, phase in (
            (check_pids, "integrated", "contract"),
            (review_pids, "integration", "integration"),
        ):
            left = _left(pidfile)
            assert len(left) == 1 and Path(left[0][1]).name.startswith(f"{label}-"), (
                f"precondition: one process left in the {label} worktree: {left}\n{out}"
            )
            pid, cwd = left[0]
            assert procs.wait_for_pid_to_die(pid, timeout=10), (
                f"pid {pid}, left in the {label} worktree {cwd}, outlived its removal"
            )
            assert _names(out, pid, phase), f"no {phase} warning names pid {pid}:\n{out}"
    finally:
        _kill(check_pids, review_pids)


def _failed_attempt_with_evidence(root: Path) -> Path:
    """A repo whose one component failed with its worktree kept as evidence."""
    _init_git_repo(root)
    _scaffold(root, ["comp-a"])
    evidence = root / ".kstrl" / "worktrees" / "run-old" / "comp-a"
    evidence.parent.mkdir(parents=True)
    _git(root, "worktree", "add", "-q", "-b", "kstrl/comp-a", str(evidence))
    manifest = _make_manifest(["comp-a"])
    comp = manifest.components[0]
    comp.status = ComponentStatus.FAILED.value
    comp.error = "review failed"
    comp.evidence_worktree = str(evidence)
    manifest.save(root / "scripts" / "kstrl" / "manifest.json")
    return evidence


def _ks_retry(root: Path, monkeypatch: pytest.MonkeyPatch) -> Result:
    """The real ``ks retry`` with the factory it re-enters replaced: the
    removal happens before it."""
    monkeypatch.setattr("kstrl.cli.run_factory", lambda *a, **k: FactoryResult(exit_code=0))
    monkeypatch.setattr("kstrl.cli._check_agent_preflight", lambda *a, **k: None)
    monkeypatch.setenv("AGENT_CMD", "echo hi")
    return CliRunner().invoke(
        cli, ["retry", "comp-a", "--root", str(root), "--yes", "--max-cost-usd", "0"]
    )


def test_ks_retry_kills_and_names_what_runs_in_the_evidence_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ks retry`` removes the failed attempt's kept worktree. Whatever runs
    there (a server started while looking at the failure) goes first."""
    root = tmp_path
    evidence = _failed_attempt_with_evidence(root)
    pidfile = tmp_path / "evidence.pids"
    subprocess.run(_leaves_a_process(tmp_path, pidfile), cwd=evidence, check=True, timeout=30)
    try:
        result = _ks_retry(root, monkeypatch)
        assert result.exit_code == 0, result.output
        assert not evidence.exists(), "precondition: the retry removed the evidence worktree"
        [(pid, cwd)] = _left(pidfile)
        assert procs.wait_for_pid_to_die(pid, timeout=10), (
            f"pid {pid}, left in the evidence worktree {cwd}, outlived its removal"
        )
        assert _names(result.output, pid, "retry"), (
            f"no retry warning names pid {pid}:\n{result.output}"
        )
    finally:
        _kill(pidfile)


def test_ks_retry_says_when_the_census_could_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A census that could not run is reported, never read as a clean
    worktree (#461). ``lsof`` sits in a system directory on PATH, so it is
    made to miss by pointing the census at a command that does not exist."""
    root = tmp_path
    evidence = _failed_attempt_with_evidence(root)
    monkeypatch.setattr("kstrl.worktree_sweep.LSOF_ARGV", ["kstrl-no-such-command-528"])
    result = _ks_retry(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert not evidence.exists(), "precondition: the retry removed the evidence worktree"
    assert any(
        "orphan_process (retry)" in line and "census could not run" in line
        for line in result.output.splitlines()
    ), f"no retry warning says the census could not run:\n{result.output}"
