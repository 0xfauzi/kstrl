"""Tests for the no-progress circuit breaker (R7.5).

Covers the config loader, the worktree fingerprint, the test-signature
probe, the run_loop integration (a stalled fake agent trips the breaker;
a progressing one does not), and the pipeline routing (a tripped breaker
is a direct FAILED transition with a distinct journal event).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from ralph_py.breaker import (
    NO_TEST_COMMAND_SIGNATURE,
    BreakerConfig,
    NoProgressBreaker,
    compute_diff_hash,
    compute_test_signature,
)
from ralph_py.config import RalphConfig
from ralph_py.factory import ComponentResult
from ralph_py.loop import run_loop
from ralph_py.manifest import ComponentStatus
from ralph_py.ui.plain import PlainUI


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, timeout=30,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("seed\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "seed"], path)


class TestBreakerConfig:
    def test_defaults(self) -> None:
        config = BreakerConfig()
        assert config.no_progress_iterations == 3
        assert config.test_command is None
        assert config.test_timeout == 300.0

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RALPH_BREAKER_ITERATIONS", "5")
        monkeypatch.setenv("RALPH_BREAKER_TEST_CMD", "pytest -q")
        monkeypatch.setenv("RALPH_BREAKER_TEST_TIMEOUT", "60")
        config = BreakerConfig.from_env()
        assert config.no_progress_iterations == 5
        assert config.test_command == "pytest -q"
        assert config.test_timeout == 60.0

    def test_load_toml_and_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "ralph.toml").write_text(
            "[breaker]\n"
            "no_progress_iterations = 7\n"
            'test_command = "toml-cmd"\n'
            "test_timeout = 120\n"
        )
        config = BreakerConfig.load(tmp_path)
        assert config.no_progress_iterations == 7
        assert config.test_command == "toml-cmd"
        assert config.test_timeout == 120.0
        monkeypatch.setenv("RALPH_BREAKER_ITERATIONS", "2")
        config = BreakerConfig.load(tmp_path)
        assert config.no_progress_iterations == 2
        assert config.test_command == "toml-cmd"

    def test_zero_disables(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        breaker = NoProgressBreaker(
            tmp_path, BreakerConfig(no_progress_iterations=0),
        )
        assert breaker.enabled is False
        assert breaker.record_iteration() is False


class TestComputeDiffHash:
    def test_stable_on_unchanged_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert compute_diff_hash(tmp_path) == compute_diff_hash(tmp_path)

    def test_changes_on_tracked_edit(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        before = compute_diff_hash(tmp_path)
        (tmp_path / "README.md").write_text("changed\n")
        assert compute_diff_hash(tmp_path) != before

    def test_changes_on_untracked_file_added(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        before = compute_diff_hash(tmp_path)
        (tmp_path / "new.txt").write_text("a\n")
        assert compute_diff_hash(tmp_path) != before

    def test_changes_on_untracked_content_edit(self, tmp_path: Path) -> None:
        """The status line alone would miss this: the untracked file
        list is identical, only the CONTENT of one file changed."""
        _init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("a\n")
        before = compute_diff_hash(tmp_path)
        (tmp_path / "new.txt").write_text("b\n")
        assert compute_diff_hash(tmp_path) != before

    def test_changes_on_commit(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        baseline = compute_diff_hash(tmp_path)
        (tmp_path / "README.md").write_text("changed\n")
        dirty = compute_diff_hash(tmp_path)
        _git(["add", "."], tmp_path)
        _git(["commit", "-q", "-m", "change"], tmp_path)
        committed = compute_diff_hash(tmp_path)
        assert committed != dirty
        assert committed != baseline

    def test_none_outside_git_repo(self, tmp_path: Path) -> None:
        assert compute_diff_hash(tmp_path) is None


class TestComputeTestSignature:
    def test_no_command_is_constant(self, tmp_path: Path) -> None:
        config = BreakerConfig(test_command=None)
        assert compute_test_signature(tmp_path, config) == (
            NO_TEST_COMMAND_SIGNATURE
        )

    def test_masks_durations(self, tmp_path: Path) -> None:
        """Two runs of the same failing suite differ only in timings;
        the signature must not."""
        fast = BreakerConfig(
            test_command='echo "FAILED test_x in 0.12s"; exit 1',
        )
        slow = BreakerConfig(
            test_command='echo "FAILED test_x in 4.56s"; exit 1',
        )
        assert compute_test_signature(tmp_path, fast) == (
            compute_test_signature(tmp_path, slow)
        )

    def test_distinguishes_failures(self, tmp_path: Path) -> None:
        sig_x = compute_test_signature(
            tmp_path, BreakerConfig(test_command='echo "FAILED test_x"; exit 1'),
        )
        sig_y = compute_test_signature(
            tmp_path, BreakerConfig(test_command='echo "FAILED test_y"; exit 1'),
        )
        assert sig_x != sig_y

    def test_distinguishes_return_codes(self, tmp_path: Path) -> None:
        sig_pass = compute_test_signature(
            tmp_path, BreakerConfig(test_command="exit 0"),
        )
        sig_fail = compute_test_signature(
            tmp_path, BreakerConfig(test_command="exit 1"),
        )
        assert sig_pass != sig_fail


class _ScriptedAgent:
    """Fake agent whose per-iteration side effect is injected."""

    def __init__(self, side_effect: object = None) -> None:
        self._side_effect = side_effect
        self.calls = 0

    @property
    def name(self) -> str:
        return "scripted"

    def run(
        self, prompt: str, cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        self.calls += 1
        if callable(self._side_effect):
            self._side_effect(self.calls, cwd)
        yield f"iteration {self.calls}"

    @property
    def final_message(self) -> str | None:
        return None


def _loop_config(root: Path, max_iterations: int) -> RalphConfig:
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    return RalphConfig(
        max_iterations=max_iterations,
        prompt_file=ralph_dir / "prompt.md",
        prd_file=ralph_dir / "prd.json",
        sleep_seconds=0,
        ralph_branch="",
        ralph_branch_explicit=True,
    )


class TestRunLoopBreakerIntegration:
    def test_stalled_agent_trips_breaker(self, tmp_path: Path) -> None:
        """An agent that changes nothing trips after N iterations."""
        _init_repo(tmp_path)
        config = _loop_config(tmp_path, max_iterations=10)
        agent = _ScriptedAgent()
        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path,
            breaker_config=BreakerConfig(no_progress_iterations=3),
        )
        assert result.no_progress is True
        assert result.completed is False
        assert result.exit_code == 1
        assert result.iterations == 3

    def test_identical_diff_agent_trips_breaker(self, tmp_path: Path) -> None:
        """An agent that rewrites the SAME content every iteration
        changes the tree once (vs baseline), then stalls: the trip
        comes N iterations after the last real change."""
        _init_repo(tmp_path)
        config = _loop_config(tmp_path, max_iterations=10)

        def write_same(call: int, cwd: Path | None) -> None:
            assert cwd is not None
            (cwd / "work.txt").write_text("identical content\n")

        agent = _ScriptedAgent(write_same)
        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path,
            breaker_config=BreakerConfig(no_progress_iterations=3),
        )
        assert result.no_progress is True
        # Iteration 1 made progress (file appeared); 2, 3, 4 stalled.
        assert result.iterations == 4

    def test_progressing_agent_does_not_trip(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config = _loop_config(tmp_path, max_iterations=5)

        def write_progress(call: int, cwd: Path | None) -> None:
            assert cwd is not None
            (cwd / "work.txt").write_text(f"iteration {call}\n")

        agent = _ScriptedAgent(write_progress)
        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path,
            breaker_config=BreakerConfig(no_progress_iterations=3),
        )
        assert result.no_progress is False
        assert result.iterations == 5  # ordinary max-iterations exit
        assert result.exit_code == 1

    def test_changing_test_signature_resets_streak(
        self, tmp_path: Path,
    ) -> None:
        """Same tree but a different test outcome each probe (flaky or
        externally-progressing suite): the streak restarts, so the
        breaker never trips (fails open)."""
        _init_repo(tmp_path)
        config = _loop_config(tmp_path, max_iterations=4)
        counter = tmp_path.parent / "probe-counter"
        counter.write_text("0")
        # Outside the repo tree on purpose: the probe's own state must
        # not change the diff hash.
        probe = (
            f'n=$(cat "{counter}"); n=$((n+1)); echo "$n" > "{counter}"; '
            'echo "ERROR flaky-$n"; exit 1'
        )
        agent = _ScriptedAgent()
        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path,
            breaker_config=BreakerConfig(
                no_progress_iterations=2, test_command=probe,
            ),
        )
        assert result.no_progress is False
        assert result.iterations == 4

    def test_stable_test_signature_trips(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config = _loop_config(tmp_path, max_iterations=10)
        agent = _ScriptedAgent()
        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path,
            breaker_config=BreakerConfig(
                no_progress_iterations=2,
                test_command='echo "FAILED test_stall"; exit 1',
            ),
        )
        assert result.no_progress is True
        assert result.iterations == 2

    def test_inert_outside_git_repo(self, tmp_path: Path) -> None:
        """No repo, nothing to fingerprint: the loop runs to its
        ordinary max-iterations exit instead of tripping."""
        config = _loop_config(tmp_path, max_iterations=4)
        agent = _ScriptedAgent()
        result = run_loop(
            config, PlainUI(no_color=True), agent, tmp_path,
            breaker_config=BreakerConfig(no_progress_iterations=2),
        )
        assert result.no_progress is False
        assert result.iterations == 4


class TestPipelineRouting:
    def test_breaker_trip_fails_without_retry(self, tmp_path: Path) -> None:
        """A tripped breaker must FAIL the component directly (no retry
        burn) and leave the distinct event in the progress log plus the
        structured signature for the evolution journal."""
        from tests.test_pipeline import _make_pipeline

        pipeline, manifest, factory_result, _ = _make_pipeline(tmp_path)
        result = ComponentResult(
            "comp-a", success=False, iterations=3,
            error="no-progress circuit breaker tripped: 3 consecutive ...",
            no_progress=True,
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", result)
        assert outcome is not None
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.retries == 0  # never retried
        assert comp.failed_check == "no_progress_breaker"
        assert "comp-a" in factory_result.failed
        assert pipeline.component_failure_signatures["comp-a"] == [
            "engineer:no-progress-stall",
        ]
        events = pipeline.progress_log.read_events()
        tripped = [
            e for e in events if e["event"] == "circuit_breaker_tripped"
        ]
        assert len(tripped) == 1
        assert tripped[0]["component"] == "comp-a"
        assert tripped[0]["data"]["iterations"] == 3
        failed = [e for e in events if e["event"] == "component_failed"]
        assert len(failed) == 1
