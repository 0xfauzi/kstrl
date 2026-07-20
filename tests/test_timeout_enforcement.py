"""R0.1: timeout enforcement tests with real subprocesses (no LLM).

Covers the enforcement layers end to end:

- Adapter level: a sleep-forever fake agent (silent, or silent AFTER one
  output line) is killed within the deadline; a grandchild spawned via
  ``sh -c 'sleep N & wait'`` dies with its parent (start_new_session +
  killpg); all three adapters honor their ``timeout`` parameter.
- Loop level: ``agent_iteration`` is passed into ``agent.run`` (capped by
  the remaining component budget); ``component_total`` aborts the loop and
  reports which limit fired.
- Factory level: a timed-out component is FAILED; a timeout retry recreates
  the worktree from base, removes the stale index.lock, and says so in the
  retry error string; the scheduler backstop fails a hung worker's
  component and the run continues.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path

import pytest

from kstrl.agents.claude_code import ClaudeCodeAgent
from kstrl.agents.claude_sdk import ClaudeSdkAgent
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.agents.proc import TIMEOUT_MESSAGE_PREFIX
from kstrl.config import KstrlConfig
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    _expired_futures,
    _next_backstop_wait,
    _remove_stale_index_lock,
    _setup_worktree,
    run_factory,
)
from kstrl.loop import run_loop
from kstrl.manifest import Component, Manifest
from kstrl.timeout import TimeoutConfig
from kstrl.ui.plain import PlainUI

# Generous bound for "killed within the deadline": 1s deadline + 5s
# SIGTERM grace + slack. A hang would previously block forever.
KILL_BOUND_SECONDS = 12.0


def _wait_pid_dead(pid: int, timeout: float = 8.0) -> bool:
    """Poll until signal 0 reports the pid gone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _read_pid(pidfile: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = pidfile.read_text().strip()
            if text:
                return int(text)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"pid file never appeared: {pidfile}")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, timeout=30,
    )


def _init_repo(root: Path) -> None:
    """Real git repo with the ralph scaffolding committed to main."""
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("test prompt\n")
    feature_dir = ralph_dir / "feature" / "a"
    feature_dir.mkdir(parents=True)
    (feature_dir / "prd.json").write_text(json.dumps({
        "branchName": "ralph/factory/a",
        "userStories": [{
            "id": "US-001", "title": "Test",
            "acceptanceCriteria": ["AC1"],
            "priority": 1, "passes": True, "notes": "",
        }],
    }))
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)


class TestCustomAgentDeadline:
    """CustomAgent runs a real subprocess; these are the canonical
    fake-agent kill scenarios from R0.1."""

    def test_silent_hang_is_killed_within_deadline(self, tmp_path: Path) -> None:
        """A sleep-forever agent that emits NO output still trips the
        deadline (reader-thread enforcement, not per-line clock checks)."""
        pidfile = tmp_path / "agent.pid"
        agent = CustomAgent(f"echo $$ > {pidfile}; exec sleep 300")

        start = time.monotonic()
        lines = list(agent.run("prompt", tmp_path, timeout=1.0))
        elapsed = time.monotonic() - start

        assert elapsed < KILL_BOUND_SECONDS
        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        assert agent.final_message is None
        pid = _read_pid(pidfile)
        assert _wait_pid_dead(pid), f"agent process {pid} survived the kill"

    def test_grandchild_is_killed_too(self, tmp_path: Path) -> None:
        """`sh -c 'sleep 300 & wait'` spawns a grandchild; killpg on the
        session started by start_new_session must take it down as well."""
        child_pidfile = tmp_path / "child.pid"
        grandchild_pidfile = tmp_path / "grandchild.pid"
        agent = CustomAgent(
            f"sh -c 'echo $$ > {child_pidfile}; "
            f"sleep 300 & echo $! > {grandchild_pidfile}; wait'"
        )

        start = time.monotonic()
        lines = list(agent.run("prompt", tmp_path, timeout=1.0))
        elapsed = time.monotonic() - start

        assert elapsed < KILL_BOUND_SECONDS
        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        child = _read_pid(child_pidfile)
        grandchild = _read_pid(grandchild_pidfile)
        assert _wait_pid_dead(child), f"child {child} survived"
        assert _wait_pid_dead(grandchild), f"grandchild {grandchild} survived"

    def test_hang_after_one_line_is_killed(self, tmp_path: Path) -> None:
        """An agent that emits one line then hangs silently must still be
        killed: pre-R0.1 the clock was only checked when a line arrived."""
        pidfile = tmp_path / "agent.pid"
        agent = CustomAgent(f"echo hello; echo $$ > {pidfile}; exec sleep 300")

        start = time.monotonic()
        lines = list(agent.run("prompt", tmp_path, timeout=1.0))
        elapsed = time.monotonic() - start

        assert elapsed < KILL_BOUND_SECONDS
        assert "hello" in lines
        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        pid = _read_pid(pidfile)
        assert _wait_pid_dead(pid), f"agent process {pid} survived the kill"

    def test_no_timeout_still_completes_normally(self, tmp_path: Path) -> None:
        agent = CustomAgent("echo done")
        lines = list(agent.run("prompt", tmp_path, timeout=None))
        assert "done" in lines
        assert agent.final_message == "done"

    def test_agent_ignoring_stdin_does_not_block_on_large_prompt(
        self, tmp_path: Path,
    ) -> None:
        """A child that never reads stdin must not deadlock the harness on
        a prompt bigger than the pipe buffer (stdin is written on its own
        thread)."""
        pidfile = tmp_path / "agent.pid"
        agent = CustomAgent(f"echo $$ > {pidfile}; exec sleep 300")
        big_prompt = "x" * 512 * 1024  # > 64KB pipe buffer

        start = time.monotonic()
        lines = list(agent.run(big_prompt, tmp_path, timeout=1.0))
        elapsed = time.monotonic() - start

        assert elapsed < KILL_BOUND_SECONDS
        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        assert _wait_pid_dead(_read_pid(pidfile))


class TestClaudeCodeAgentDeadline:
    """Real-subprocess timeout coverage for the claude adapter via a fake
    `claude` executable on PATH."""

    def test_hang_after_stream_event_is_killed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        pidfile = tmp_path / "claude.pid"
        event = (
            '{"type":"assistant","message":'
            '{"content":[{"type":"text","text":"working"}]}}'
        )
        script = (
            "#!/bin/sh\n"
            f"echo '{event}'\n"
            f"echo $$ > {pidfile}\n"
            "exec sleep 300\n"
        )
        fake = bindir / "claude"
        fake.write_text(script)
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

        agent = ClaudeCodeAgent()
        start = time.monotonic()
        lines = list(agent.run("prompt", tmp_path, timeout=1.0))
        elapsed = time.monotonic() - start

        assert elapsed < KILL_BOUND_SECONDS
        assert "working" in lines
        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        assert _wait_pid_dead(_read_pid(pidfile))


class TestCodexAgentDeadline:
    """Real-subprocess timeout coverage for the codex adapter via a fake
    `codex` executable on PATH."""

    def test_silent_hang_is_killed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        pidfile = tmp_path / "codex.pid"
        script = (
            "#!/bin/sh\n"
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            "    --help) exit 0 ;;\n"
            "  esac\n"
            "done\n"
            "echo starting\n"
            f"echo $$ > {pidfile}\n"
            "exec sleep 300\n"
        )
        fake = bindir / "codex"
        fake.write_text(script)
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        # Reset the memoized --output-last-message probe so it targets the
        # fake CLI (monkeypatch restores the original value afterwards).
        monkeypatch.setattr(CodexAgent, "_supports_output_last_message", None)

        agent = CodexAgent()
        start = time.monotonic()
        lines = list(agent.run("prompt", tmp_path, timeout=1.0))
        elapsed = time.monotonic() - start

        assert elapsed < KILL_BOUND_SECONDS
        assert "starting" in lines
        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        assert agent.final_message is None
        assert _wait_pid_dead(_read_pid(pidfile))


class TestClaudeSdkAgentDeadline:
    """R0.1 battery against the SDK transport (R7.6 gate).

    The claude-sdk adapter runs the SDK in a runner subprocess spawned
    through DeadlineStreamer precisely because the SDK's own transport
    spawns the CLI WITHOUT ``start_new_session`` and only signals the
    direct child on close (measured 2026-07-20, SDK 0.2.123) - so these
    tests drive the REAL runner + REAL SDK against fake CLIs injected
    via ``ClaudeAgentOptions.cli_path`` and assert the whole tree dies
    on breach. Startup overhead is measured (~0.2s SDK import), so the
    deadlines below have ample margin.
    """

    def _fake_cli(self, tmp_path: Path, body: str) -> Path:
        fake = tmp_path / "fake-claude"
        fake.write_text("#!/bin/sh\n" + body)
        fake.chmod(0o755)
        return fake

    def _agent(self, cli: Path) -> ClaudeSdkAgent:
        agent = ClaudeSdkAgent(model="haiku")
        agent._cli_path = str(cli)
        return agent

    def test_silent_hang_is_killed(self, tmp_path: Path) -> None:
        """A CLI that never answers the SDK handshake (no output at
        all) still trips the wall-clock deadline; the SDK's own 60s
        initialize timeout never gets the chance to matter."""
        pidfile = tmp_path / "cli.pid"
        cli = self._fake_cli(
            tmp_path, f"echo $$ > {pidfile}\nexec sleep 300\n",
        )
        agent = self._agent(cli)
        start = time.monotonic()
        lines = list(agent.run("prompt", tmp_path, timeout=4.0))
        elapsed = time.monotonic() - start

        assert elapsed < 4.0 + KILL_BOUND_SECONDS
        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        assert agent.usage_records[-1].source == "timeout"
        assert _wait_pid_dead(_read_pid(pidfile))

    def test_hang_after_output_is_killed(self, tmp_path: Path) -> None:
        """Output before the hang must not reset the absolute deadline.

        The marker goes to the CLI's stderr, which is inherited from
        the runner and merged into the adapter stream - visible without
        having to speak the SDK's stdout JSON protocol."""
        pidfile = tmp_path / "cli.pid"
        cli = self._fake_cli(
            tmp_path,
            f"echo fake-cli-started 1>&2\n"
            f"echo $$ > {pidfile}\nexec sleep 300\n",
        )
        agent = self._agent(cli)
        start = time.monotonic()
        lines = list(agent.run("prompt", tmp_path, timeout=4.0))
        elapsed = time.monotonic() - start

        assert elapsed < 4.0 + KILL_BOUND_SECONDS
        assert "fake-cli-started" in lines
        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        assert _wait_pid_dead(_read_pid(pidfile))

    def test_grandchild_is_killed_too(self, tmp_path: Path) -> None:
        """The R7.6 gate's core case: a tool-like process spawned BY the
        CLI (a grandchild of the runner, great-grandchild of the
        harness) dies on breach. This is exactly what the SDK's own
        direct-child close() cannot guarantee and why the runner owns
        the process group."""
        grandchild_pidfile = tmp_path / "grandchild.pid"
        cli = self._fake_cli(
            tmp_path,
            f"sleep 300 &\necho $! > {grandchild_pidfile}\nwait\n",
        )
        agent = self._agent(cli)
        lines = list(agent.run("prompt", tmp_path, timeout=4.0))

        assert any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
        assert _wait_pid_dead(_read_pid(grandchild_pidfile))

    def test_missing_sdk_fails_fast_with_install_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without the sdk extra the runner emits the install hint and
        exits - no hang, no timeout, no traceback spew."""
        shadow = tmp_path / "shadow" / "claude_agent_sdk"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text(
            'raise ImportError("claude-agent-sdk deliberately shadowed")\n'
        )
        monkeypatch.setenv("PYTHONPATH", str(tmp_path / "shadow"))

        agent = ClaudeSdkAgent()
        start = time.monotonic()
        lines = list(agent.run("prompt", tmp_path, timeout=30.0))
        elapsed = time.monotonic() - start

        assert elapsed < KILL_BOUND_SECONDS
        assert any("claude-agent-sdk is not installed" in line for line in lines)
        assert not any(
            line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines
        )
        assert agent.usage_records[-1].source == "unavailable"


class TestSignalGroupSafety:
    """_signal_group must never group-kill a pathological pgid.

    Regression: a mocked Popen's pid coerces to 1 via MagicMock.__index__,
    so os.getpgid(pid) did NOT raise TypeError as assumed; killpg(1, sig)
    is kill(-1, sig) ("signal everything this user can") and took down the
    whole CI runner. The guard must fall back to signalling the direct
    child for any non-int pid, pid <= 1, resolved pgid <= 1, or our own
    process group.
    """

    def _streamer_with_fake_proc(self, pid: object) -> tuple[object, object]:
        from unittest.mock import MagicMock, patch

        from kstrl.agents.proc import DeadlineStreamer

        fake_proc = MagicMock()
        fake_proc.pid = pid
        fake_proc.stdout = iter([])
        fake_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=fake_proc):
            streamer = DeadlineStreamer(["true"])
        return streamer, fake_proc

    @pytest.mark.parametrize("bad_pid", [None, 0, 1, -1])
    def test_never_killpg_for_unsafe_pids(
        self, bad_pid: object, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import signal as _signal

        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            os, "killpg",
            lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        streamer, fake_proc = self._streamer_with_fake_proc(bad_pid)

        streamer._signal_group(_signal.SIGTERM)

        assert killpg_calls == []
        fake_proc.terminate.assert_called_once()

    def test_mock_pid_falls_back_to_terminate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The exact CI-killer shape: MagicMock pid (coerces to 1)."""
        import signal as _signal
        from unittest.mock import MagicMock

        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            os, "killpg",
            lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        streamer, fake_proc = self._streamer_with_fake_proc(MagicMock())

        streamer._signal_group(_signal.SIGTERM)

        assert killpg_calls == []
        fake_proc.terminate.assert_called_once()

    def test_own_process_group_is_never_group_killed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pid resolving to the harness's own pgid must not be killpg'd."""
        import signal as _signal

        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            os, "killpg",
            lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
        streamer, fake_proc = self._streamer_with_fake_proc(os.getpid())

        streamer._signal_group(_signal.SIGTERM)

        assert killpg_calls == []
        fake_proc.terminate.assert_called_once()


class _RecordingAgent:
    """In-process fake that records the timeout passed by run_loop."""

    name = "recording"
    final_message: str | None = None

    def __init__(
        self, sleep_seconds: float = 0.0, lines: list[str] | None = None,
    ) -> None:
        self.received_timeouts: list[float | None] = []
        self._sleep_seconds = sleep_seconds
        self._lines = lines if lines is not None else ["working"]

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        self.received_timeouts.append(timeout)
        if self._sleep_seconds:
            time.sleep(self._sleep_seconds)
        yield from self._lines


def _loop_config(tmp_path: Path, max_iterations: int) -> KstrlConfig:
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    return KstrlConfig(
        max_iterations=max_iterations,
        prompt_file=ralph_dir / "prompt.md",
        prd_file=ralph_dir / "prd.json",
        sleep_seconds=0,
        ralph_branch="",
        ralph_branch_explicit=True,
    )


class TestLoopTimeouts:
    """run_loop passes agent_iteration into agent.run and enforces
    component_total as a wall clock across iterations."""

    def test_agent_iteration_timeout_reaches_agent(self, tmp_path: Path) -> None:
        config = _loop_config(tmp_path, max_iterations=1)
        agent = _RecordingAgent()
        timeouts = TimeoutConfig(agent_iteration=123.0, component_total=0)

        run_loop(config, PlainUI(no_color=True), agent, tmp_path, timeouts=timeouts)

        assert agent.received_timeouts == [123.0]

    def test_iteration_timeout_capped_by_component_budget(
        self, tmp_path: Path,
    ) -> None:
        config = _loop_config(tmp_path, max_iterations=1)
        agent = _RecordingAgent()
        timeouts = TimeoutConfig(agent_iteration=500.0, component_total=5.0)

        run_loop(config, PlainUI(no_color=True), agent, tmp_path, timeouts=timeouts)

        assert len(agent.received_timeouts) == 1
        received = agent.received_timeouts[0]
        assert received is not None
        assert 0 < received <= 5.0

    def test_component_timeout_aborts_loop(self, tmp_path: Path) -> None:
        config = _loop_config(tmp_path, max_iterations=100)
        agent = _RecordingAgent(sleep_seconds=0.2)
        timeouts = TimeoutConfig(agent_iteration=0, component_total=0.3)

        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path, timeouts=timeouts,
        )

        assert result.completed is False
        assert result.exit_code == 1
        assert result.timeout_limit == "component"
        assert result.iterations < 100

    def test_disabled_timeouts_run_to_max_iterations(self, tmp_path: Path) -> None:
        config = _loop_config(tmp_path, max_iterations=3)
        agent = _RecordingAgent()
        timeouts = TimeoutConfig(agent_iteration=0, component_total=0)

        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path, timeouts=timeouts,
        )

        assert result.iterations == 3
        assert result.timeout_limit is None
        assert agent.received_timeouts == [None, None, None]

    def test_timed_out_iterations_counted(self, tmp_path: Path) -> None:
        config = _loop_config(tmp_path, max_iterations=2)
        agent = _RecordingAgent(lines=[f"{TIMEOUT_MESSAGE_PREFIX} after 1.0s"])
        timeouts = TimeoutConfig(agent_iteration=60.0, component_total=0)

        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path, timeouts=timeouts,
        )

        assert result.timed_out_iterations == 2


class TestFactoryComponentTimeout:
    """A sleep-forever fake agent times out and the component is FAILED."""

    def test_component_failed_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test prompt")
        feature_dir = ralph_dir / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
        pidfile = tmp_path / "agent.pid"

        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="t",
            base_branch="main", single_pr=False,
            components=[Component(
                "a", "A", "", [], "scripts/ralph/feature/a/prd.json", "b/a",
            )],
        )
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            max_retries=0, retry_delay=0, review_mode="skip",
            timeout_config=TimeoutConfig(
                agent_iteration=0.5, component_total=1.0,
            ),
        )
        base = KstrlConfig(
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            agent_cmd=f"echo $$ > {pidfile}; exec sleep 300",
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )

        start = time.monotonic()
        result = run_factory(
            manifest, config, base, PlainUI(no_color=True), tmp_path,
        )
        elapsed = time.monotonic() - start

        assert elapsed < 30.0
        assert "a" in result.failed
        assert result.exit_code == 1
        comp = manifest.get_component("a")
        assert comp is not None
        assert comp.status == "failed"
        assert "timeout" in comp.error.lower()
        assert _wait_pid_dead(_read_pid(pidfile))

    def test_timeout_retry_notes_recreate_from_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A timeout retry must say it recreates the worktree from base in
        the retry error string (R0.1 requirement 5)."""
        monkeypatch.chdir(tmp_path)
        _init_repo(tmp_path)
        log_path = tmp_path / "progress.jsonl"

        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="t",
            base_branch="main", single_pr=False,
            components=[Component(
                "a", "A", "", [], "scripts/ralph/feature/a/prd.json",
                "ralph/factory/a",
            )],
        )
        config = FactoryConfig(
            use_worktrees=True, create_prs=False, max_parallel=1,
            max_retries=1, retry_delay=0, review_mode="skip",
            progress_log_path=log_path,
            timeout_config=TimeoutConfig(
                agent_iteration=0.3, component_total=0.5,
            ),
        )
        base = KstrlConfig(
            prompt_file=tmp_path / "scripts" / "ralph" / "prompt.md",
            prd_file=tmp_path / "scripts" / "ralph" / "prd.json",
            sleep_seconds=0,
            agent_cmd="exec sleep 300",
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )

        result = run_factory(
            manifest, config, base, PlainUI(no_color=True), tmp_path,
        )

        assert "a" in result.failed
        comp = manifest.get_component("a")
        assert comp is not None
        assert comp.retries == 1

        events = [
            json.loads(line) for line in log_path.read_text().splitlines()
        ]
        retry_events = [e for e in events if e["event"] == "component_retrying"]
        assert retry_events, "expected a component_retrying event"
        reason = retry_events[0]["data"]["reason"]
        assert "timeout" in reason.lower()
        assert "recreated from base" in reason
        assert "index.lock" in reason


class TestWorktreeTimeoutHygiene:
    """_setup_worktree(fresh_from_base=True) resets the branch to base and
    stale index locks are removed."""

    def test_fresh_from_base_resets_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = _setup_worktree("a", "b/a", "main", tmp_path, "run1")

        # Simulate a killed attempt that left a commit on the branch.
        (wt / "leftover.txt").write_text("dirty state from killed attempt")
        _git("add", "-A", cwd=wt)
        _git("commit", "-q", "-m", "partial work", cwd=wt)
        branch_tip = subprocess.run(
            ["git", "rev-parse", "b/a"], cwd=tmp_path,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        main_tip = subprocess.run(
            ["git", "rev-parse", "main"], cwd=tmp_path,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        assert branch_tip != main_tip

        # Plant a stale lock like a SIGKILLed git op would leave.
        lock = tmp_path / ".git" / "worktrees" / "a" / "index.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")

        wt2 = _setup_worktree(
            "a", "b/a", "main", tmp_path, "run1", fresh_from_base=True,
        )

        new_tip = subprocess.run(
            ["git", "rev-parse", "b/a"], cwd=tmp_path,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        assert new_tip == main_tip, "branch was not recreated from base"
        assert not (wt2 / "leftover.txt").exists()
        assert not lock.exists()

    def test_default_retry_keeps_branch_commits(self, tmp_path: Path) -> None:
        """Without fresh_from_base the existing branch is reused (the
        pre-R0.1 retry behavior for non-timeout failures is preserved)."""
        _init_repo(tmp_path)
        wt = _setup_worktree("a", "b/a", "main", tmp_path, "run1")
        (wt / "progress.txt").write_text("legit progress")
        _git("add", "-A", cwd=wt)
        _git("commit", "-q", "-m", "progress", cwd=wt)
        branch_tip = subprocess.run(
            ["git", "rev-parse", "b/a"], cwd=tmp_path,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()

        _setup_worktree("a", "b/a", "main", tmp_path, "run1")

        new_tip = subprocess.run(
            ["git", "rev-parse", "b/a"], cwd=tmp_path,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        assert new_tip == branch_tip

    def test_remove_stale_index_lock(self, tmp_path: Path) -> None:
        lock = tmp_path / ".git" / "worktrees" / "comp" / "index.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("")
        _remove_stale_index_lock(tmp_path, "comp")
        assert not lock.exists()
        # Absent lock is a no-op, not an error.
        _remove_stale_index_lock(tmp_path, "comp")


class TestSchedulerBackstop:
    """Per-future deadline of component_total + margin in the parallel
    scheduler."""

    def test_expired_futures_selection(self) -> None:
        hung: Future[ComponentResult] = Future()
        done: Future[ComponentResult] = Future()
        done.set_result(ComponentResult("done", success=True))
        fresh: Future[ComponentResult] = Future()

        running = {hung: "hung", done: "done", fresh: "fresh"}
        deadlines = {hung: 100.0, done: 100.0, fresh: 200.0}

        expired = _expired_futures(running, deadlines, now=150.0)
        assert expired == [hung]

    def test_next_backstop_wait(self) -> None:
        f1: Future[ComponentResult] = Future()
        f2: Future[ComponentResult] = Future()

        assert _next_backstop_wait({f1: "a"}, {}, now=0.0) is None
        wait_s = _next_backstop_wait(
            {f1: "a", f2: "b"}, {f1: 50.0, f2: 30.0}, now=10.0,
        )
        assert wait_s == 20.0
        # A deadline already in the past floors at zero (poll immediately).
        assert _next_backstop_wait({f1: "a"}, {f1: 5.0}, now=10.0) == 0.0

    def test_backstop_fails_component_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker hung OUTSIDE the loop/adapter enforcement (here: a
        stuck scaffold command) is abandoned at component_total + margin:
        the component is FAILED with error 'component timeout', the run
        finishes without waiting for the worker, and the leaked worker's
        worktree is left in place."""
        monkeypatch.chdir(tmp_path)
        _init_repo(tmp_path)

        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="t",
            base_branch="main", single_pr=False,
            components=[Component(
                "a", "A", "", [], "scripts/ralph/feature/a/prd.json",
                "ralph/factory/a", scaffold="sleep 5",
            )],
        )
        config = FactoryConfig(
            use_worktrees=True, create_prs=False, max_parallel=2,
            max_retries=0, retry_delay=0, review_mode="skip",
            timeout_config=TimeoutConfig(
                agent_iteration=5.0, component_total=0.5,
                scheduler_backstop_margin=0.5,
            ),
        )
        base = KstrlConfig(
            prompt_file=tmp_path / "scripts" / "ralph" / "prompt.md",
            prd_file=tmp_path / "scripts" / "ralph" / "prd.json",
            sleep_seconds=0,
            agent_cmd="echo done",
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )

        start = time.monotonic()
        result = run_factory(
            manifest, config, base, PlainUI(no_color=True), tmp_path,
        )
        elapsed = time.monotonic() - start

        # Returned without waiting out the 5s scaffold hang.
        assert elapsed < 5.0, f"run waited for the hung worker ({elapsed:.1f}s)"
        assert "a" in result.failed
        assert result.exit_code == 1
        comp = manifest.get_component("a")
        assert comp is not None
        assert comp.status == "failed"
        assert comp.error == "component timeout"
        # Leaked worker's worktree is kept, not ripped out from under it.
        # R0.5: worktrees are keyed .ralph/worktrees/<run_id>/<component_id>
        leaked = list((tmp_path / ".ralph" / "worktrees").glob("*/a"))
        assert leaked, "leaked worker's worktree was removed"


class TestTimeoutConfigLoading:
    """TimeoutConfig is the single source: toml [timeout] + env overlay."""

    def test_load_reads_toml_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "RALPH_TIMEOUT_AGENT_ITERATION", "RALPH_TIMEOUT_COMPONENT",
            "RALPH_TIMEOUT_BACKSTOP_MARGIN",
        ):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / "ralph.toml").write_text(
            "[timeout]\n"
            "agent_iteration = 11\n"
            "component_total = 22\n"
            "scheduler_backstop_margin = 5\n"
        )
        config = TimeoutConfig.load(tmp_path)
        assert config.agent_iteration == 11.0
        assert config.component_total == 22.0
        assert config.scheduler_backstop_margin == 5.0
        # Untouched keys keep their defaults.
        assert config.git_operation == 30.0

    def test_env_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "ralph.toml").write_text(
            "[timeout]\nagent_iteration = 11\ncomponent_total = 22\n"
        )
        monkeypatch.setenv("RALPH_TIMEOUT_AGENT_ITERATION", "33")
        config = TimeoutConfig.load(tmp_path)
        assert config.agent_iteration == 33.0
        assert config.component_total == 22.0

    def test_missing_toml_uses_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "RALPH_TIMEOUT_AGENT_ITERATION", "RALPH_TIMEOUT_COMPONENT",
        ):
            monkeypatch.delenv(var, raising=False)
        config = TimeoutConfig.load(tmp_path)
        assert config.agent_iteration == 1800.0
        assert config.component_total == 7200.0
        assert config.scheduler_backstop_margin == 60.0

    def test_ralph_config_duplicate_fields_deleted(self) -> None:
        """R0.1 requirement 4: the dead duplicate fields on KstrlConfig are
        gone; TimeoutConfig is the only source."""
        config = KstrlConfig()
        assert not hasattr(config, "agent_iteration_timeout")
        assert not hasattr(config, "component_timeout")
        assert not hasattr(config, "subprocess_timeout")


class TestCliTimeoutFlags:
    """`ralph factory --agent-timeout/--component-timeout` reach the
    resolved TimeoutConfig (previously bound and never used)."""

    def _write_manifest(self, tmp_path: Path) -> Path:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "version": "1",
            "specFile": "spec.md",
            "projectName": "t",
            "baseBranch": "main",
            "singlePr": False,
            "components": [],
        }))
        return manifest_path

    def _invoke_factory(
        self, tmp_path: Path, extra_args: list[str],
    ) -> FactoryConfig:
        from unittest.mock import patch

        from click.testing import CliRunner

        from kstrl.cli import cli
        from kstrl.factory import FactoryResult

        manifest_path = self._write_manifest(tmp_path)
        runner = CliRunner()
        with patch("kstrl.cli.run_factory") as mock_run:
            mock_run.return_value = FactoryResult()
            result = runner.invoke(cli, [
                "factory",
                "--manifest", str(manifest_path),
                "--root", str(tmp_path),
                "--agent-cmd", "echo hi",
                "--yes",
                *extra_args,
            ])
            assert result.exit_code == 0, result.output
            factory_config = mock_run.call_args[0][1]
        assert isinstance(factory_config, FactoryConfig)
        return factory_config

    def test_flags_reach_timeout_config(self, tmp_path: Path) -> None:
        factory_config = self._invoke_factory(
            tmp_path,
            ["--agent-timeout", "111", "--component-timeout", "222"],
        )
        assert factory_config.timeout_config is not None
        assert factory_config.timeout_config.agent_iteration == 111.0
        assert factory_config.timeout_config.component_total == 222.0

    def test_toml_used_when_flags_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "RALPH_TIMEOUT_AGENT_ITERATION", "RALPH_TIMEOUT_COMPONENT",
        ):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / "ralph.toml").write_text(
            "[timeout]\nagent_iteration = 44\ncomponent_total = 55\n"
        )
        factory_config = self._invoke_factory(tmp_path, [])
        assert factory_config.timeout_config is not None
        assert factory_config.timeout_config.agent_iteration == 44.0
        assert factory_config.timeout_config.component_total == 55.0

    def test_flag_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("RALPH_TIMEOUT_AGENT_ITERATION", raising=False)
        (tmp_path / "ralph.toml").write_text(
            "[timeout]\nagent_iteration = 44\n"
        )
        factory_config = self._invoke_factory(
            tmp_path, ["--agent-timeout", "111"],
        )
        assert factory_config.timeout_config is not None
        assert factory_config.timeout_config.agent_iteration == 111.0


# ---------------------------------------------------------------------------
# Static audit: no subprocess call without a timeout (A+ orchestration gate)
# ---------------------------------------------------------------------------


class TestSubprocessTimeoutAudit:
    """The A+ factory-orchestration gate requires that no subprocess call
    in kstrl ships without a timeout, enforced by a static test. This
    is that test: an AST walk over every module, alias-aware
    (``import subprocess as _sp`` counts), so a new call site without a
    ``timeout=`` fails CI instead of hanging a run someday.

    ``Popen`` takes no timeout kwarg; it is legitimate ONLY in modules
    that implement their own deadline management, each covered by the
    runtime kill tests in this file's suite or their own:

    - kstrl/agents/proc.py: reader-thread deadline + group kill (R0.1)
    - kstrl/verify.py: run_scrubbed communicate(timeout) + group kill
      (R2.6)
    """

    SPAWN_FUNCS = frozenset({"run", "call", "check_call", "check_output"})
    POPEN_ALLOWLIST = frozenset({
        "kstrl/agents/proc.py",
        "kstrl/verify.py",
    })

    @staticmethod
    def _subprocess_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
        """Names bound to the subprocess module / its spawn functions."""
        module_aliases: set[str] = set()
        direct_funcs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess":
                        module_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "subprocess":
                    for alias in node.names:
                        direct_funcs.add(alias.asname or alias.name)
        return module_aliases, direct_funcs

    def test_every_subprocess_call_has_timeout(self) -> None:
        package_root = Path(__file__).resolve().parent.parent / "kstrl"
        violations: list[str] = []
        popen_violations: list[str] = []
        sites_seen = 0

        for py_file in sorted(package_root.rglob("*.py")):
            rel = py_file.relative_to(package_root.parent).as_posix()
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            module_aliases, direct_funcs = self._subprocess_aliases(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                called: str | None = None
                if (
                    isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id in module_aliases
                ):
                    called = fn.attr
                elif isinstance(fn, ast.Name) and fn.id in direct_funcs:
                    called = fn.id
                if called is None:
                    continue
                if called == "Popen":
                    sites_seen += 1
                    if rel not in self.POPEN_ALLOWLIST:
                        popen_violations.append(f"{rel}:{node.lineno}")
                elif called in self.SPAWN_FUNCS:
                    sites_seen += 1
                    if not any(k.arg == "timeout" for k in node.keywords):
                        violations.append(f"{rel}:{node.lineno} {called}")

        # If the walk ever finds nothing, the audit itself broke (import
        # style changed, package moved) - fail loudly, never vacuously.
        assert sites_seen >= 20, (
            f"audit only found {sites_seen} subprocess call sites; "
            "the scan is broken, not the code clean"
        )
        assert not violations, (
            "subprocess calls without an explicit timeout= (add one, or "
            "route through a deadline-managed runner):\n  "
            + "\n  ".join(violations)
        )
        assert not popen_violations, (
            "Popen outside the deadline-managed allowlist (see class "
            "docstring):\n  " + "\n  ".join(popen_violations)
        )
