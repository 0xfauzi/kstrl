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
from tests.helpers.procs import read_pid

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


# Moved to tests/helpers/procs.py when #292 gave it a second consumer;
# aliased rather than renamed at every call site below.
_read_pid = read_pid


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=30,
    )


def _init_repo(root: Path) -> None:
    """Real git repo with the kstrl scaffolding committed to main."""
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("test prompt\n")
    feature_dir = kstrl_dir / "feature" / "a"
    feature_dir.mkdir(parents=True)
    (feature_dir / "prd.json").write_text(
        json.dumps(
            {
                "branchName": "kstrl/factory/a",
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Test",
                        "acceptanceCriteria": ["AC1"],
                        "priority": 1,
                        "passes": True,
                        "notes": "",
                    }
                ],
            }
        )
    )
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
            f"sh -c 'echo $$ > {child_pidfile}; sleep 300 & echo $! > {grandchild_pidfile}; wait'"
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
        self,
        tmp_path: Path,
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        pidfile = tmp_path / "claude.pid"
        event = '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}'
        script = f"#!/bin/sh\necho '{event}'\necho $$ > {pidfile}\nexec sleep 300\n"
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
            tmp_path,
            f"echo $$ > {pidfile}\nexec sleep 300\n",
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
            f"echo fake-cli-started 1>&2\necho $$ > {pidfile}\nexec sleep 300\n",
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
        assert not any(line.startswith(TIMEOUT_MESSAGE_PREFIX) for line in lines)
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
        self,
        bad_pid: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import signal as _signal

        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            os,
            "killpg",
            lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        streamer, fake_proc = self._streamer_with_fake_proc(bad_pid)

        streamer._signal_group(_signal.SIGTERM)

        assert killpg_calls == []
        fake_proc.terminate.assert_called_once()

    def test_mock_pid_falls_back_to_terminate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The exact CI-killer shape: MagicMock pid (coerces to 1)."""
        import signal as _signal
        from unittest.mock import MagicMock

        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            os,
            "killpg",
            lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        streamer, fake_proc = self._streamer_with_fake_proc(MagicMock())

        streamer._signal_group(_signal.SIGTERM)

        assert killpg_calls == []
        fake_proc.terminate.assert_called_once()

    def test_own_process_group_is_never_group_killed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pid resolving to the harness's own pgid must not be killpg'd."""
        import signal as _signal

        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            os,
            "killpg",
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
        self,
        sleep_seconds: float = 0.0,
        lines: list[str] | None = None,
    ) -> None:
        self.received_timeouts: list[float | None] = []
        self._sleep_seconds = sleep_seconds
        self._lines = lines if lines is not None else ["working"]

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        self.received_timeouts.append(timeout)
        if self._sleep_seconds:
            time.sleep(self._sleep_seconds)
        yield from self._lines


def _loop_config(tmp_path: Path, max_iterations: int) -> KstrlConfig:
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')
    return KstrlConfig(
        max_iterations=max_iterations,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        kstrl_branch="",
        kstrl_branch_explicit=True,
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
        self,
        tmp_path: Path,
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
            config,
            PlainUI(no_color=True),
            agent,
            tmp_path,
            timeouts=timeouts,
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
            config,
            PlainUI(no_color=True),
            agent,
            tmp_path,
            timeouts=timeouts,
        )

        assert result.iterations == 3
        assert result.timeout_limit is None
        assert agent.received_timeouts == [None, None, None]

    def test_timed_out_iterations_counted(self, tmp_path: Path) -> None:
        config = _loop_config(tmp_path, max_iterations=2)
        agent = _RecordingAgent(lines=[f"{TIMEOUT_MESSAGE_PREFIX} after 1.0s"])
        timeouts = TimeoutConfig(agent_iteration=60.0, component_total=0)

        result = run_loop(
            config,
            PlainUI(no_color=True),
            agent,
            tmp_path,
            timeouts=timeouts,
        )

        assert result.timed_out_iterations == 2


class TestFactoryComponentTimeout:
    """A sleep-forever fake agent times out and the component is FAILED."""

    def test_component_failed_on_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prompt.md").write_text("test prompt")
        feature_dir = kstrl_dir / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        pidfile = tmp_path / "agent.pid"

        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    "a",
                    "A",
                    "",
                    [],
                    "scripts/kstrl/feature/a/prd.json",
                    "b/a",
                )
            ],
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=0,
            retry_delay=0,
            review_mode="skip",
            timeout_config=TimeoutConfig(
                agent_iteration=0.5,
                component_total=1.0,
            ),
        )
        base = KstrlConfig(
            prompt_file=kstrl_dir / "prompt.md",
            prd_file=kstrl_dir / "prd.json",
            sleep_seconds=0,
            agent_cmd=f"echo $$ > {pidfile}; exec sleep 300",
            kstrl_branch="",
            kstrl_branch_explicit=True,
            ui_mode="plain",
            no_color=True,
        )

        start = time.monotonic()
        result = run_factory(
            manifest,
            config,
            base,
            PlainUI(no_color=True),
            tmp_path,
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A timeout retry must say it recreates the worktree from base in
        the retry error string (R0.1 requirement 5)."""
        monkeypatch.chdir(tmp_path)
        _init_repo(tmp_path)
        log_path = tmp_path / "progress.jsonl"

        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    "a",
                    "A",
                    "",
                    [],
                    "scripts/kstrl/feature/a/prd.json",
                    "kstrl/factory/a",
                )
            ],
        )
        config = FactoryConfig(
            use_worktrees=True,
            create_prs=False,
            max_parallel=1,
            max_retries=1,
            retry_delay=0,
            review_mode="skip",
            progress_log_path=log_path,
            timeout_config=TimeoutConfig(
                agent_iteration=0.3,
                component_total=0.5,
            ),
        )
        base = KstrlConfig(
            prompt_file=tmp_path / "scripts" / "kstrl" / "prompt.md",
            prd_file=tmp_path / "scripts" / "kstrl" / "prd.json",
            sleep_seconds=0,
            agent_cmd="exec sleep 300",
            kstrl_branch="",
            kstrl_branch_explicit=True,
            ui_mode="plain",
            no_color=True,
        )

        result = run_factory(
            manifest,
            config,
            base,
            PlainUI(no_color=True),
            tmp_path,
        )

        assert "a" in result.failed
        comp = manifest.get_component("a")
        assert comp is not None
        assert comp.retries == 1

        events = [json.loads(line) for line in log_path.read_text().splitlines()]
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
            ["git", "rev-parse", "b/a"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        main_tip = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        assert branch_tip != main_tip

        # Plant a stale lock like a SIGKILLed git op would leave.
        lock = tmp_path / ".git" / "worktrees" / "a" / "index.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")

        wt2 = _setup_worktree(
            "a",
            "b/a",
            "main",
            tmp_path,
            "run1",
            fresh_from_base=True,
        )

        new_tip = subprocess.run(
            ["git", "rev-parse", "b/a"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
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
            ["git", "rev-parse", "b/a"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

        _setup_worktree("a", "b/a", "main", tmp_path, "run1")

        new_tip = subprocess.run(
            ["git", "rev-parse", "b/a"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
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
            {f1: "a", f2: "b"},
            {f1: 50.0, f2: 30.0},
            now=10.0,
        )
        assert wait_s == 20.0
        # A deadline already in the past floors at zero (poll immediately).
        assert _next_backstop_wait({f1: "a"}, {f1: 5.0}, now=10.0) == 0.0

    def test_backstop_fails_component_and_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker hung OUTSIDE the loop/adapter enforcement (here: a
        stuck scaffold command) is abandoned at component_total + margin:
        the component is FAILED with error 'component timeout', the run
        finishes without waiting for the worker, and the leaked worker's
        worktree is left in place."""
        monkeypatch.chdir(tmp_path)
        _init_repo(tmp_path)

        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    "a",
                    "A",
                    "",
                    [],
                    "scripts/kstrl/feature/a/prd.json",
                    "kstrl/factory/a",
                    scaffold="sleep 5",
                )
            ],
        )
        config = FactoryConfig(
            use_worktrees=True,
            create_prs=False,
            max_parallel=2,
            max_retries=0,
            retry_delay=0,
            review_mode="skip",
            timeout_config=TimeoutConfig(
                agent_iteration=5.0,
                component_total=0.5,
                scheduler_backstop_margin=0.5,
            ),
        )
        base = KstrlConfig(
            prompt_file=tmp_path / "scripts" / "kstrl" / "prompt.md",
            prd_file=tmp_path / "scripts" / "kstrl" / "prd.json",
            sleep_seconds=0,
            agent_cmd="echo done",
            kstrl_branch="",
            kstrl_branch_explicit=True,
            ui_mode="plain",
            no_color=True,
        )

        start = time.monotonic()
        result = run_factory(
            manifest,
            config,
            base,
            PlainUI(no_color=True),
            tmp_path,
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
        # R0.5: worktrees are keyed .kstrl/worktrees/<run_id>/<component_id>
        leaked = list((tmp_path / ".kstrl" / "worktrees").glob("*/a"))
        assert leaked, "leaked worker's worktree was removed"


class TestTimeoutConfigLoading:
    """TimeoutConfig is the single source: toml [timeout] + env overlay."""

    def test_load_reads_toml_section(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "KSTRL_TIMEOUT_AGENT_ITERATION",
            "KSTRL_TIMEOUT_COMPONENT",
            "KSTRL_TIMEOUT_BACKSTOP_MARGIN",
        ):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / "kstrl.toml").write_text(
            "[timeout]\nagent_iteration = 11\ncomponent_total = 22\nscheduler_backstop_margin = 5\n"
        )
        config = TimeoutConfig.load(tmp_path)
        assert config.agent_iteration == 11.0
        assert config.component_total == 22.0
        assert config.scheduler_backstop_margin == 5.0
        # Untouched keys keep their defaults.
        assert config.git_operation == 30.0

    def test_env_beats_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[timeout]\nagent_iteration = 11\ncomponent_total = 22\n"
        )
        monkeypatch.setenv("KSTRL_TIMEOUT_AGENT_ITERATION", "33")
        config = TimeoutConfig.load(tmp_path)
        assert config.agent_iteration == 33.0
        assert config.component_total == 22.0

    def test_missing_toml_uses_defaults(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "KSTRL_TIMEOUT_AGENT_ITERATION",
            "KSTRL_TIMEOUT_COMPONENT",
        ):
            monkeypatch.delenv(var, raising=False)
        config = TimeoutConfig.load(tmp_path)
        assert config.agent_iteration == 1800.0
        assert config.component_total == 7200.0
        assert config.scheduler_backstop_margin == 60.0

    def test_kstrl_config_duplicate_fields_deleted(self) -> None:
        """R0.1 requirement 4: the dead duplicate fields on KstrlConfig are
        gone; TimeoutConfig is the only source."""
        config = KstrlConfig()
        assert not hasattr(config, "agent_iteration_timeout")
        assert not hasattr(config, "component_timeout")
        assert not hasattr(config, "subprocess_timeout")


class TestCliTimeoutFlags:
    """`ks factory --agent-timeout/--component-timeout` reach the
    resolved TimeoutConfig (previously bound and never used)."""

    def _write_manifest(self, tmp_path: Path) -> Path:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "version": "1",
                    "specFile": "spec.md",
                    "projectName": "t",
                    "baseBranch": "main",
                    "singlePr": False,
                    "components": [],
                }
            )
        )
        return manifest_path

    def _invoke_factory(
        self,
        tmp_path: Path,
        extra_args: list[str],
    ) -> FactoryConfig:
        from unittest.mock import patch

        from click.testing import CliRunner

        from kstrl.cli import cli
        from kstrl.factory import FactoryResult

        manifest_path = self._write_manifest(tmp_path)
        runner = CliRunner()
        with patch("kstrl.cli.run_factory") as mock_run:
            mock_run.return_value = FactoryResult()
            result = runner.invoke(
                cli,
                [
                    "factory",
                    "--manifest",
                    str(manifest_path),
                    "--root",
                    str(tmp_path),
                    "--agent-cmd",
                    "echo hi",
                    "--yes",
                    *extra_args,
                ],
            )
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "KSTRL_TIMEOUT_AGENT_ITERATION",
            "KSTRL_TIMEOUT_COMPONENT",
        ):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / "kstrl.toml").write_text(
            "[timeout]\nagent_iteration = 44\ncomponent_total = 55\n"
        )
        factory_config = self._invoke_factory(tmp_path, [])
        assert factory_config.timeout_config is not None
        assert factory_config.timeout_config.agent_iteration == 44.0
        assert factory_config.timeout_config.component_total == 55.0

    def test_flag_beats_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("KSTRL_TIMEOUT_AGENT_ITERATION", raising=False)
        (tmp_path / "kstrl.toml").write_text("[timeout]\nagent_iteration = 44\n")
        factory_config = self._invoke_factory(
            tmp_path,
            ["--agent-timeout", "111"],
        )
        assert factory_config.timeout_config is not None
        assert factory_config.timeout_config.agent_iteration == 111.0


# ---------------------------------------------------------------------------
# The #309 gate: a module on POPEN_ALLOWLIST must not wait without a deadline.
# ---------------------------------------------------------------------------

#: Methods that block on a child process. ``timeout=`` is optional on both.
_WAIT_METHODS = frozenset({"wait", "communicate"})


def _wait_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """Every ``.wait(...)`` / ``.communicate(...)`` call, with its method name."""
    return [
        (node, node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _WAIT_METHODS
    ]


def _subprocess_aliases(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Names bound to the subprocess module, and LOCAL name -> real name.

    The mapping half is #309 round 2, C2. This used to return a SET of
    local names and every caller then compared them against the ORIGINAL
    names (``called == "Popen"``, ``called in SPAWN_FUNCS``), so an ``as``
    rename walked straight through. Measured on a planted module:
    ``from subprocess import run as _run`` followed by
    ``_run(argv, capture_output=True)`` with no timeout at all passed
    clean, as did the same trick for ``Popen``.

    ONE resolver for both gates in this file. Round 2 also found the
    ``with``-on-a-Popen gate carrying a second, weaker copy of this, with
    a paragraph asserting the two agreed - which is the same shape as the
    bug above: two readings held level by prose. They are one function
    now, so the question cannot be asked again.

    KNOWN MISSES, so the reach is not overstated. Only an ``import``
    statement is resolved. ``_P = subprocess.Popen`` rebinds through an
    assignment and ``getattr(subprocess, "Popen")`` through a string, and
    following either needs dataflow this walk does not do.
    """
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    from_imports = [
        n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "subprocess"
    ]
    module_aliases = {
        alias.asname or alias.name
        for node in imports
        for alias in node.names
        if alias.name == "subprocess"
    }
    direct_funcs = {
        alias.asname or alias.name: alias.name for node in from_imports for alias in node.names
    }
    return module_aliases, direct_funcs


def _popen_names(tree: ast.AST) -> set[str]:
    """Every local name that means ``Popen``, derived not re-derived.

    KNOWN over-report, deliberately left: ``_is_popen_call`` also accepts
    any ``x.Popen(...)``, so a hypothetical ``mock.Popen()`` is flagged.
    That direction costs a comment on a line that is not really a child
    process; the other direction costs a hang.
    """
    _, direct = _subprocess_aliases(tree)
    return {"Popen", *(local for local, real in direct.items() if real == "Popen")}


def _is_popen_call(node: ast.expr, names: set[str]) -> bool:
    """Whether this expression constructs a ``Popen``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return called in names


def _popen_bound_names(tree: ast.AST, names: set[str]) -> set[str]:
    """Names assigned a ``Popen(...)``, so ``with proc:`` is recognisable.

    NAME COLLECTION, NOT SCOPE ANALYSIS, and the difference has teeth.
    Every ``x = Popen(...)`` anywhere in the module contributes its name
    to one flat set, so a ``with`` on a name bound in a different function
    still matches, and a ``with`` on a PARAMETER matches only if the
    parameter happens to share a name with some module-level binding.
    Round 2 measured that: the planted mutation is caught today partly
    because ``_kill_or_abandon``'s parameter is called ``process`` and so
    is the binding in ``_read_ps``; renaming it to ``proc`` makes the
    plant pass clean. A ``with`` on a Popen returned from a helper is
    missed outright. Both want dataflow this walk does not do, and are
    left for a follow-up rather than papered over here.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_popen_call(node.value, names):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return bound


def _bare_wait_findings(tree: ast.AST) -> list[tuple[int, str]]:
    """Waits on a child that name no deadline, or name one that is None."""
    found: list[tuple[int, str]] = []
    for node, method in _wait_calls(tree):
        deadline = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
        if deadline is None:
            found.append((node.lineno, f".{method}() names no timeout="))
        elif isinstance(deadline, ast.Constant) and deadline.value is None:
            found.append((node.lineno, f".{method}(timeout=None) is not a deadline"))
    return found


def _with_popen_findings(tree: ast.AST) -> list[tuple[int, str]]:
    """``with`` blocks on a Popen, whose ``__exit__`` waits with no deadline."""
    names = _popen_names(tree)
    bound = _popen_bound_names(tree, names)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        for item in node.items:
            context = item.context_expr
            named = isinstance(context, ast.Name) and context.id in bound
            if _is_popen_call(context, names) or named:
                found.append((node.lineno, "`with` on a Popen: __exit__ waits with no deadline"))
    return found


def _unbounded_wait_findings(tree: ast.AST) -> list[str]:
    """Every way this module can wait on a child process forever.

    Three forms, because #309 round 1 found the first version of this
    check catching only one of them:

    * ``.wait()`` / ``.communicate()`` with no ``timeout=`` at all.
    * the same with ``timeout=None``, which READS as a deadline and is
      not one. The first version passed this.
    * ``with Popen(...)``, where the wait is ``__exit__`` and there is no
      Call node anywhere in the tree to inspect. The first version passed
      this too.

    KNOWN MISS, stated so the gate is not trusted past its reach: only a
    LITERAL ``None`` is visible here. A ``timeout=`` whose value is a name
    that happens to hold None at run time reads exactly like a real
    deadline, and following it needs dataflow an AST walk does not do.
    """
    found = _bare_wait_findings(tree) + _with_popen_findings(tree)
    return [f"{line} {text}" for line, text in sorted(found)]


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
    - kstrl/serve.py: subprocess_factory_runner communicate(timeout) +
      group kill (R8.6). Popen is REQUIRED here rather than incidental:
      review #186 F1 showed subprocess.run's timeout signals only the
      direct child, which on macOS is the caffeinate wrapper, so the
      factory itself outlived the timeout and the daemon requeued an
      item that was still executing.
    - kstrl/procgroup.py: two bounded communicate(timeout) calls around a
      kill, then the child is abandoned (#309). Popen is REQUIRED here
      too, and for a different reason: subprocess.run's timeout handler
      waits on the killed child with NO deadline and Popen.__exit__ waits
      again, so `timeout=` cannot bound a ps that will not die. Measured
      under a fake wedged child: 60.06s before, sub-second after
      (tests/test_procgroup.py::TestThePsCallIsBounded).
    """

    SPAWN_FUNCS = frozenset({"run", "call", "check_call", "check_output"})
    POPEN_ALLOWLIST = frozenset(
        {
            "kstrl/agents/proc.py",
            "kstrl/procgroup.py",
            "kstrl/serve.py",
            "kstrl/verify.py",
        }
    )

    @classmethod
    def _spawn_sites(cls, tree: ast.Module) -> list[tuple[ast.Call, str]]:
        """``(call node, subprocess name)`` for every spawn call in the tree.

        Split out of the test in #309 round 2 so it can be run over a
        planted module. C2 was found by planting one by hand; a check
        nobody can re-run is how the alias hole survived to be found by
        hand in the first place.

        ``called`` is always the name SUBPROCESS knows it by, never the
        local alias, which is the C2 fix.
        """
        module_aliases, direct_funcs = _subprocess_aliases(tree)
        sites: list[tuple[ast.Call, str]] = []
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
                called = direct_funcs[fn.id]
            if called == "Popen" or called in cls.SPAWN_FUNCS:
                sites.append((node, called))
        return sites

    def test_every_subprocess_call_has_timeout(self) -> None:
        package_root = Path(__file__).resolve().parent.parent / "kstrl"
        violations: list[str] = []
        popen_violations: list[str] = []
        sites_seen = 0

        for py_file in sorted(package_root.rglob("*.py")):
            rel = py_file.relative_to(package_root.parent).as_posix()
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node, called in self._spawn_sites(tree):
                sites_seen += 1
                if called == "Popen":
                    if rel not in self.POPEN_ALLOWLIST:
                        popen_violations.append(f"{rel}:{node.lineno}")
                elif not any(k.arg == "timeout" for k in node.keywords):
                    violations.append(f"{rel}:{node.lineno} {called}")

        # If the walk ever finds nothing, the audit itself broke (import
        # style changed, package moved) - fail loudly, never vacuously.
        assert sites_seen >= 20, (
            f"audit only found {sites_seen} subprocess call sites; "
            "the scan is broken, not the code clean"
        )
        assert not violations, (
            "subprocess calls without an explicit timeout= (add one, or "
            "route through a deadline-managed runner):\n  " + "\n  ".join(violations)
        )
        assert not popen_violations, (
            "Popen outside the deadline-managed allowlist (see class "
            "docstring):\n  " + "\n  ".join(popen_violations)
        )

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            # The plain forms, which always worked.
            ("import subprocess\nsubprocess.run(argv)\n", [(2, "run")]),
            ("import subprocess as sp\nsp.check_output(argv)\n", [(2, "check_output")]),
            ("import subprocess\nsubprocess.Popen(argv)\n", [(2, "Popen")]),
            # #309 round 2, C2. Every one of these passed the audit clean
            # before the local-name mapping, including a brand new module
            # calling run with no deadline at all.
            ("from subprocess import run as _run\n_run(argv)\n", [(2, "run")]),
            ("from subprocess import Popen as Spawn\nSpawn(argv)\n", [(2, "Popen")]),
            (
                "from subprocess import check_call as _cc\n_cc(argv)\n",
                [(2, "check_call")],
            ),
            # The unrenamed from-import, which must keep working.
            ("from subprocess import run\nrun(argv)\n", [(2, "run")]),
        ],
    )
    def test_the_audit_resolves_a_renamed_import(
        self,
        body: str,
        expected: list[tuple[int, str]],
    ) -> None:
        """C2's fix, as a mechanism instead of a sentence.

        The audit compares against the name subprocess knows, so the
        resolution has to hand it that name and not the local one. Planted
        here because C2 was found by planting a module by hand, and a
        check nobody can re-run is how the hole lasted.
        """
        sites = self._spawn_sites(ast.parse(body))
        assert [(node.lineno, called) for node, called in sites] == expected, body

    @pytest.mark.parametrize(
        "body",
        [
            # Rebound through an assignment, not an import.
            "import subprocess\n_P = subprocess.Popen\n_P(argv)\n",
            # Reached through a string.
            'import subprocess\ngetattr(subprocess, "Popen")(argv)\n',
            # A different module's run.
            "import other\nother.run(argv)\n",
        ],
    )
    def test_the_audit_misses_these_and_says_so(self, body: str) -> None:
        """The reach, executed rather than promised.

        The first two are real holes, named in `_subprocess_aliases` and
        left for a follow-up: closing them needs dataflow an AST walk does
        not do. The third is not a hole, it is the scope. Pinned together
        so the difference between "cannot see" and "does not care" stays
        written down.
        """
        assert self._spawn_sites(ast.parse(body)) == [], body

    def test_no_allowlisted_module_waits_without_a_deadline(self) -> None:
        """#309's class: the allowlist admits a module, not a discipline.

        Being on POPEN_ALLOWLIST said only that the module promised to
        manage its own deadline, and nothing checked the promise. #309 is
        what that costs: ``procgroup`` passed ``timeout=`` to
        ``subprocess.run``, satisfied the audit above, and hung anyway,
        because the wait ``run`` performs after killing the timed-out
        child has no deadline. A pinned kwarg is not a bound.

        WHAT THIS DOES NOT COVER, said plainly because a mechanism cited
        for a claim it does not carry is worse than none. It would NOT
        have caught #309: that unbounded wait was inside CPython, not in
        this tree, so no walk of ``kstrl/`` could see it. The thing that
        catches a revert to ``subprocess.run`` is the clock in
        ``tests/test_procgroup.py::TestThePsCallIsBounded``, which
        measured 60.06s against the old body. What this catches is the
        sibling the allowlist invites and nobody was checking: a
        hand-rolled ``Popen`` in one of these four files that waits on
        its child with no deadline. All 11 current sites already pass one,
        so this lands green and stays a ratchet rather than a cleanup.

        Scoped to the allowlisted files because ``.wait(...)`` outside
        them is overwhelmingly ``threading.Event.wait``, whose unbounded
        form is legitimate and common (``kstrl/commandrun.py``,
        ``kstrl/interaction.py``, ``kstrl/shutdown.py``). Inside them it
        is a child process, and there it must always name a deadline.

        The wait half is a receiver-name check, so it catches ``x.wait()``
        on anything, not only on a Popen. That is the conservative
        direction for four files that exist to manage child processes; if
        one of them ever needs an unbounded Event wait, that is a decision
        worth writing down here rather than a false positive to widen
        around.

        The three forms it rejects, and the two that #309 round 1 found
        the first version of this check passing, are in
        ``_unbounded_wait_findings``. They are pinned as planted cases
        below rather than by hand, because a gate verified once in a shell
        session is a gate nobody can re-verify.
        """
        package_root = Path(__file__).resolve().parent.parent / "kstrl"
        violations: list[str] = []
        blind: list[str] = []

        for rel in sorted(self.POPEN_ALLOWLIST):
            tree = ast.parse((package_root.parent / rel).read_text(encoding="utf-8"))
            if not _wait_calls(tree):
                blind.append(rel)
            violations += [f"{rel}:{found}" for found in _unbounded_wait_findings(tree)]

        # PER FILE, not a total. A global floor of 10 against an actual 11
        # left one site of margin, so a legitimate refactor removing one
        # wait failed CI with "the scan is broken" - a false diagnosis, and
        # the kind of gate that gets deleted. A file on this allowlist is
        # here because it manages a child process, so every one of them
        # must show at least one wait; a file showing none means the walk
        # stopped seeing that file, which is the thing worth failing on.
        assert not blind, (
            "these deadline-managed modules show no wait sites at all, so "
            "the scan is broken rather than the code clean:\n  " + "\n  ".join(blind)
        )
        assert not violations, (
            "a deadline-managed module waits on a child without a "
            "deadline, which is #309:\n  " + "\n  ".join(violations)
        )

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            # The form the first version of this gate caught.
            (
                "proc.wait()\n",
                ["1 .wait() names no timeout="],
            ),
            # #309 round 1, F4: a kwarg is present, so the first version
            # reported clean. It waits forever all the same.
            (
                "proc.wait(timeout=None)\n",
                ["1 .wait(timeout=None) is not a deadline"],
            ),
            (
                "proc.communicate(timeout=None)\n",
                ["1 .communicate(timeout=None) is not a deadline"],
            ),
            # F4 again: the wait is __exit__, so there is no Call node in
            # the tree to inspect and the first version saw nothing.
            (
                "with subprocess.Popen(argv) as proc:\n    pass\n",
                ["1 `with` on a Popen: __exit__ waits with no deadline"],
            ),
            (
                "proc = subprocess.Popen(argv)\nwith proc:\n    pass\n",
                ["2 `with` on a Popen: __exit__ waits with no deadline"],
            ),
            # Found in round 2: a bare name match reported this clean,
            # which is the under-reporting direction.
            (
                "from subprocess import Popen as Spawn\nwith Spawn(argv) as p:\n    pass\n",
                ["2 `with` on a Popen: __exit__ waits with no deadline"],
            ),
            (
                "from subprocess import Popen as Spawn\np = Spawn(argv)\nwith p:\n    pass\n",
                ["3 `with` on a Popen: __exit__ waits with no deadline"],
            ),
        ],
    )
    def test_the_gate_catches_each_planted_mutation(
        self,
        body: str,
        expected: list[str],
    ) -> None:
        """The gate's own reach, measured rather than asserted.

        #309 round 1 found the first version of this check passing two of
        the three forms it exists to stop, and the manual verification
        behind it had planted only the form it did catch. A guard that
        reports clean on the bug it was written to prevent is the failure
        this batch keeps repeating, so the planted forms live here as
        cases rather than in a shell session nobody can re-run.
        """
        assert _unbounded_wait_findings(ast.parse(body)) == expected, body

    @pytest.mark.parametrize(
        "body",
        [
            # A real deadline, in each of the shapes the tree uses.
            "proc.wait(timeout=5.0)\n",
            "proc.communicate(timeout=self._remaining())\n",
            "proc.wait(timeout=0)\n",
            # Not a child process at all, and not a `with` on one.
            "with open(path) as handle:\n    pass\n",
            "with contextlib.suppress(OSError):\n    pass\n",
            # A Popen that is never used as a context manager.
            "proc = subprocess.Popen(argv)\nproc.wait(timeout=1)\n",
        ],
    )
    def test_the_gate_stays_quiet_on_these(self, body: str) -> None:
        assert _unbounded_wait_findings(ast.parse(body)) == [], body

    def test_a_deadline_that_is_none_at_run_time_is_a_known_miss(self) -> None:
        """The reach `_unbounded_wait_findings` claims, executed.

        A docstring naming a limit is a claim; this is the measurement of
        it, so the limit cannot quietly change into something else.
        """
        assert _unbounded_wait_findings(ast.parse("proc.wait(timeout=grace)\n")) == []
