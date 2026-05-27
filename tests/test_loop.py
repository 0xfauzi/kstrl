"""Tests for loop module."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ralph_py.config import RalphConfig
from ralph_py.loop import COMPLETION_MARKER, run_loop
from ralph_py.ui.plain import PlainUI


class MockAgent:
    """Mock agent for testing."""

    def __init__(self, output: list[str]):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        yield from self._output
        if self._output:
            self._final_message = self._output[-1]

    @property
    def final_message(self) -> str | None:
        return self._final_message


class TestRunLoop:
    """Tests for run_loop."""

    def test_completes_on_marker(self, tmp_path: Path) -> None:
        """Loop exits with code 0 when completion marker found."""
        # Setup
        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test prompt")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        config = RalphConfig(
            max_iterations=5,
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent(["working...", COMPLETION_MARKER])

        # Execute
        result = run_loop(config, ui, agent, tmp_path)

        # Verify
        assert result.completed is True
        assert result.exit_code == 0
        assert result.iterations == 1

    def test_max_iterations_without_completion(self, tmp_path: Path) -> None:
        """Loop exits with code 1 when max iterations reached."""
        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test prompt")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        config = RalphConfig(
            max_iterations=3,
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent(["still working"])

        result = run_loop(config, ui, agent, tmp_path)

        assert result.completed is False
        assert result.exit_code == 1
        assert result.iterations == 3

    def test_missing_prompt_file_falls_back_to_default(
        self, tmp_path: Path,
    ) -> None:
        """Gap 1 fix: when the configured prompt file does not exist,
        run_loop falls back to the H3-protected DEFAULT_PROMPT from
        init_cmd.py rather than failing.

        Pre-fix behavior was exit_code=1 / iterations=0; the factory
        validation run on 2026-05-27 surfaced that this blocked any
        ``ralph factory --spec X`` invocation on a project that had
        not been ``ralph init``'d, even though the harness ships its
        own engineer prompt that should be used as the default.
        """
        from ralph_py.init_cmd import DEFAULT_PROMPT

        captured_prompts: list[str] = []

        class _PromptCapturingAgent:
            name = "capture"
            final_message: str | None = None

            def run(
                self, prompt: str,
                cwd: Path | None = None,
                timeout: float | None = None,
            ) -> Iterator[str]:
                captured_prompts.append(prompt)
                yield "starting"
                yield COMPLETION_MARKER

        config = RalphConfig(
            max_iterations=2,
            prompt_file=tmp_path / "nonexistent.md",
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)

        result = run_loop(config, ui, _PromptCapturingAgent(), tmp_path)  # type: ignore[arg-type]

        # Loop ran (didn't bail on the missing file) and completed via
        # the marker emitted by the mock agent.
        assert result.completed is True
        assert result.iterations == 1
        # The fallback prompt content is what the agent saw -- the
        # first lines of DEFAULT_PROMPT are stable per H3 snapshot.
        assert captured_prompts
        assert DEFAULT_PROMPT.splitlines()[0] in captured_prompts[0]

    def test_completion_marker_in_middle_of_output(self, tmp_path: Path) -> None:
        """Completion marker found even when not at end of output."""
        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        config = RalphConfig(
            max_iterations=5,
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent(["start", COMPLETION_MARKER, "more output"])

        result = run_loop(config, ui, agent, tmp_path)

        assert result.completed is True
        assert result.exit_code == 0

    def test_auto_checkout_false_skips_branch_checkout(
        self, tmp_path: Path,
    ) -> None:
        """When config.auto_checkout is False, run_loop must skip both the
        branch resolution AND the checkout call, even if a non-empty
        branch is configured. Otherwise the documented [git].auto_checkout
        setting has no effect."""
        import subprocess

        # Real git repo so the is_git_repo check passes
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(tmp_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "t"],
            check=True, capture_output=True,
        )
        (tmp_path / "stub").write_text("stub")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "stub"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
            check=True, capture_output=True,
        )

        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("hi")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "feature/should-not-checkout", "userStories": []}'
        )

        config = RalphConfig(
            max_iterations=1,
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            auto_checkout=False,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent([COMPLETION_MARKER])

        result = run_loop(config, ui, agent, tmp_path)
        assert result.completed is True

        current = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Did not switch to feature/should-not-checkout
        assert current == "main"

    def test_inline_marker_does_not_trigger_completion(self, tmp_path: Path) -> None:
        """Inline marker should not end the loop."""
        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("hello")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        config = RalphConfig(
            max_iterations=1,
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent(
            [
                "User",
                "hello",
                f"marker inside line {COMPLETION_MARKER} and more",
                "Assistant",
                "working...",
            ]
        )

        result = run_loop(config, ui, agent, tmp_path)

        assert result.completed is False
        assert result.exit_code == 1
        assert result.iterations == 1
