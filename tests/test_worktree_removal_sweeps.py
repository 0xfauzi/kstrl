"""A process left in a contract, integration or retry-evidence worktree dies
before that worktree is removed, and a warning names it (#528). So does one
left in a stale worktree the next run prunes, or in a component worktree a
retry recreates (#642).

#461 swept the component worktrees. Three other worktrees kstrl creates were
removed with nothing killed: the Phase 3 contract worktrees, where the
project's own test command runs (a tier check, its bisection, and the
integrated check); the integration review's worktree, where a reviewer agent
with a shell tool runs; and the failed attempt's evidence worktree that
``ks retry`` removes. A process started in a session of its own in any of
them outlived the worktree, the phase and the run.

The record is a warning line, which a factory run also writes to its
events.jsonl. None of the first three worktrees belongs to one component,
and a stale worktree belongs to no component of the run that prunes it, so
no component finding can hold the record. The retry's setup warns the same
way, because ``_setup_worktree`` is handed a UI and not the run's pipeline.

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
from kstrl.events import Log, read_events
from kstrl.factory import FactoryResult
from kstrl.manifest import ComponentStatus
from tests.helpers import gitrepo, procs
from tests.helpers import integration_harness as harness
from tests.helpers.run_limits import every_limit_argv
from tests.helpers.stack_confirmation import confirm_stack, in_process_stack, write_stack
from tests.test_agent_processes_outlive_run import (
    COMP,
    COMPLETE,
    _deaf_sleep_in,
    _dispose,
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


def _restack(root: Path, tests: str, *, writable: tuple[str, ...] = ()) -> None:
    """Replace the repository's confirmed [stack] with one whose only check
    is ``tests``, committed and confirmed (#696: the stack is the only
    source of a check command, and Phase 1, the base gates and Phase 3 all
    run the same checks). ``writable`` names a directory outside the
    worktree the command writes to (#700 rule 12): a pidfile under
    tmp_path, which the rung otherwise denies."""
    text = (root / "kstrl.toml").read_text(encoding="utf-8")
    (root / "kstrl.toml").write_text(text[: text.index("[stack]")], encoding="utf-8")
    write_stack(root, {"tests": tests}, writable=writable)
    gitrepo.git_in(root, "commit", "-q", "-am", "restack")
    confirm_stack(root)


def _only_under(where: str, command: str) -> str:
    """``command`` where the working directory matches the shell pattern
    ``where`` and a pass everywhere else, so the base gates (under
    ``.kstrl/contract/base-gates-*``) do not refuse the run."""
    return (
        f'case "$(pwd)" in */.kstrl/contract/base-gates-*) exit 0;; {where}) {command};; '
        "*) exit 0;; esac"
    )


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
    """Whether one line of ``text`` is the ``phase`` WARNING naming ``pid``.

    ``text`` is PlainUI output, which starts a warning with ``WARN:``; a line
    printed at any other severity does not count."""
    return any(
        line.startswith("WARN: ") and f"orphan_process ({phase})" in line and f"pid {pid} " in line
        for line in text.splitlines()
    )


def _run_warnings(root: Path) -> str:
    """The run's warn-severity events.jsonl lines, spelled as PlainUI prints them."""
    return "\n".join(
        f"WARN: {event.text}"
        for path in sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
        for event in read_events(path)
        if isinstance(event, Log) and event.severity == "warn"
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
    _restack(root, _only_under("*/.kstrl/contract/*", command), writable=(str(tmp_path),))
    proc = _factory(root, COMPLETE, "1", "--contract-check", "final")
    try:
        out, _ = proc.communicate(timeout=240)
        assert (proc.returncode == 0) is (test_exit == 0), out
        left = _left(pidfile)
        assert sorted(Path(cwd).name.split("-")[0] for _pid, cwd in left) == worktrees, (
            f"precondition: the contract command ran once in each worktree: {left}\n{out}"
        )
        events = _run_warnings(root)
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


@pytest.mark.parametrize("layout", ["run-dir", "flat"])
def test_the_next_run_kills_and_names_what_a_stale_worktree_held(
    tmp_path: Path, layout: str
) -> None:
    """A previous run that was killed leaves its worktrees, and whatever its
    agents' tools left running in them. The next run's prune removes the
    directories; the processes go first (#461) and a warning names each
    (#642). ``flat`` is the pre-R0.5 layout, a worktree directly under
    ``.kstrl/worktrees/``, which the prune recognises by the ``.git`` file
    inside it. The two layouts are the prune's two sweeps."""
    root = _repo(tmp_path)
    worktrees = root / ".kstrl" / "worktrees"
    stale = worktrees / "factory-old" / COMP if layout == "run-dir" else worktrees / "comp-old"
    stale.mkdir(parents=True)
    if layout == "flat":
        (stale / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    child = _deaf_sleep_in(stale)
    proc = _factory(root, COMPLETE, "1")
    try:
        out, _ = proc.communicate(timeout=240)
        assert proc.returncode == 0, out
        assert child.wait(timeout=10) == -signal.SIGKILL
        assert not stale.exists()
        assert _names(_run_warnings(root), child.pid, "stale worktree"), (
            f"no stale worktree warning in events.jsonl names pid {child.pid}"
        )
    finally:
        _stop_factory(proc)
        _dispose(child)


def test_a_retry_kills_and_names_what_phase_1_left_in_the_worktree(tmp_path: Path) -> None:
    """A retry recreates the component's worktree at the same path. Phase 1's
    test command leaves a process there and fails; with one retry allowed,
    the retry's worktree setup is the first sweep of that worktree after
    Phase 1, and it kills the process and names it (#642). The command also
    runs in the base-gate worktree and in the second attempt's Phase 1, so
    the first row recorded in the component worktree is the one the retry
    kills."""
    root = _repo(tmp_path)
    pidfile = tmp_path / "phase1.pids"
    command = f"{shlex.join(_leaves_a_process(tmp_path, pidfile))} && exit 1"
    _restack(root, _only_under("*/.kstrl/worktrees/*", command), writable=(str(tmp_path),))
    proc = _factory(root, COMPLETE, "1", "--max-retries", "1")
    try:
        out, _ = proc.communicate(timeout=240)
        in_worktree = [row for row in _left(pidfile) if "/.kstrl/worktrees/" in row[1]]
        assert len(in_worktree) == 2, (
            f"precondition: Phase 1 ran once per attempt in the component worktree: "
            f"{_left(pidfile)}\n{out}"
        )
        pid, cwd = in_worktree[0]
        assert procs.wait_for_pid_to_die(pid, timeout=10), (
            f"pid {pid}, left in the component worktree {cwd}, outlived the retry's setup"
        )
        assert _names(_run_warnings(root), pid, "worktree setup"), (
            f"no worktree setup warning in events.jsonl names pid {pid}"
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
            # #700: the rung's writable set comes from the FACTORY-level
            # project_stack, never the contract's own one, even for a
            # contract check: both have to name tmp_path.
            project_stack=in_process_stack(
                {"tests": "true", "typecheck": "true", "lint": "true"},
                writable=(str(tmp_path),),
            ),
            contract_config=ContractConfig(
                mode=ContractMode.TIER.value,
                project_stack=in_process_stack(
                    {"tests": shlex.join(_leaves_a_process(tmp_path, check_pids))},
                    writable=(str(tmp_path),),
                ),
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
    # `ks retry` is the real CLI here, so it reads kstrl.toml from disk and
    # refuses with no confirmed [stack] (#696 flag day, rule 1).
    write_stack(root)
    confirm_stack(root)
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
    # No launch record: state every run limit, or the retry refuses (#526).
    return CliRunner().invoke(
        cli, ["retry", "comp-a", "--root", str(root), "--yes", *every_limit_argv()]
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
        line.startswith("WARN: ")
        and "orphan_process (retry)" in line
        and "census could not run" in line
        for line in result.output.splitlines()
    ), f"no retry warning says the census could not run:\n{result.output}"
