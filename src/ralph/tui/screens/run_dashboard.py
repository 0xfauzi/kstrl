"""Run dashboard screen - live agent execution view with output and story progress."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header
from textual.worker import Worker

from ralph.agent import AgentOutput
from ralph.config import RalphConfig, load_config
from ralph.loop import LoopControl, LoopResult, run_loop
from ralph.prd import PRD, load_prd
from ralph.tui.widgets.agent_log import AgentLogWidget
from ralph.tui.widgets.header import RunHeader
from ralph.tui.widgets.story_table import StoryTableWidget


class DashboardCallbacks:
    """LoopCallbacks that post messages to the TUI."""

    def __init__(self, screen: RunDashboardScreen) -> None:
        self.screen = screen

    def on_loop_start(self, config: RalphConfig, prd: PRD | None) -> None:
        self.screen.call_from_thread(self.screen._on_loop_start, config, prd)

    def on_branch_status(self, message: str) -> None:
        self.screen.call_from_thread(self.screen._on_info, message)

    def on_iteration_start(self, iteration: int, max_iterations: int) -> None:
        self.screen.call_from_thread(self.screen._on_iteration_start, iteration, max_iterations)

    def on_agent_line(self, output: AgentOutput) -> None:
        self.screen.call_from_thread(self.screen._on_agent_line, output)

    def on_iteration_end(self, iteration: int, elapsed_seconds: float) -> None:
        self.screen.call_from_thread(self.screen._on_iteration_end, iteration, elapsed_seconds)

    def on_guard_violation(self, disallowed: list[str]) -> None:
        self.screen.call_from_thread(self.screen._on_guard_violation, disallowed)

    def on_guard_reverted(self, messages: list[str]) -> None:
        for msg in messages:
            self.screen.call_from_thread(self.screen._on_info, msg)

    def on_complete(self, success: bool, iterations_used: int) -> None:
        self.screen.call_from_thread(self.screen._on_complete, success, iterations_used)

    def on_info(self, message: str) -> None:
        self.screen.call_from_thread(self.screen._on_info, message)

    def on_error(self, message: str) -> None:
        self.screen.call_from_thread(self.screen._on_error, message)


class RunDashboardScreen(Screen):
    """Live run dashboard with agent output panel and story progress table."""

    BINDINGS = [
        ("p", "toggle_pause", "Pause/Resume"),
        ("s", "stop", "Stop"),
        ("escape", "back", "Back"),
    ]

    def __init__(
        self,
        understand_mode: bool = False,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self.understand_mode = understand_mode
        self.control = LoopControl()
        self._prd: PRD | None = None
        self._config: RalphConfig | None = None
        self._worker: Worker[LoopResult] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RunHeader(id="run-header")
        with Horizontal(id="run-body"):
            yield AgentLogWidget(id="agent-log", wrap=True, highlight=True, markup=True)
            yield StoryTableWidget(id="story-panel")
        yield Footer()

    def on_mount(self) -> None:
        self._start_run()

    def _start_run(self) -> None:
        """Start the agentic loop in a worker thread."""
        config = load_config()

        if self.understand_mode:
            config.paths.prompt = "scripts/ralph/understand_prompt.md"
            config.paths.allowed = ["scripts/ralph/codebase_map.md"]
            if not config.git.branch:
                config.git.branch = "ralph/understanding"

        self._config = config
        self.control = LoopControl()

        # Load PRD for story display
        try:
            self._prd = load_prd(Path.cwd() / config.paths.prd)
            story_table = self.query_one("#story-panel", StoryTableWidget)
            story_table.update_stories(self._prd)
        except Exception:
            pass

        self._worker = self.run_worker(
            self._run_loop_worker(config),
            name="ralph-loop",
            thread=True,
        )

    async def _run_loop_worker(self, config: RalphConfig) -> LoopResult:
        """Worker method that runs the loop."""
        callbacks = DashboardCallbacks(self)
        return await run_loop(config, Path.cwd(), callbacks, self.control)

    # -- Callback handlers (called from worker thread via call_from_thread) --

    def _on_loop_start(self, config: RalphConfig, prd: PRD | None) -> None:
        log = self.query_one("#agent-log", AgentLogWidget)
        mode = "Understanding" if self.understand_mode else "Feature"
        log.write_info(f"Starting {mode} loop")

    def _on_iteration_start(self, iteration: int, max_iterations: int) -> None:
        header = self.query_one("#run-header", RunHeader)
        header.set_iteration(iteration, max_iterations)

        log = self.query_one("#agent-log", AgentLogWidget)
        log.write_info(f"--- Iteration {iteration} / {max_iterations} ---")

        # Refresh PRD to get current story
        if self._config:
            try:
                self._prd = load_prd(Path.cwd() / self._config.paths.prd)
                next_story = self._prd.next_story()
                if next_story:
                    header.set_story(next_story.id)
                    story_table = self.query_one("#story-panel", StoryTableWidget)
                    story_table.update_stories(self._prd, next_story.id)
            except Exception:
                pass

    def _on_agent_line(self, output: AgentOutput) -> None:
        log = self.query_one("#agent-log", AgentLogWidget)
        log.write_agent_line(output)

    def _on_iteration_end(self, iteration: int, elapsed_seconds: float) -> None:
        log = self.query_one("#agent-log", AgentLogWidget)
        log.write_info(f"Iteration {iteration} completed in {elapsed_seconds:.1f}s")

        # Refresh story table
        if self._config:
            try:
                self._prd = load_prd(Path.cwd() / self._config.paths.prd)
                story_table = self.query_one("#story-panel", StoryTableWidget)
                story_table.update_stories(self._prd)
            except Exception:
                pass

    def _on_guard_violation(self, disallowed: list[str]) -> None:
        log = self.query_one("#agent-log", AgentLogWidget)
        log.write_error("Disallowed changes detected:")
        for f in disallowed:
            log.write_error(f"  - {f}")

    def _on_complete(self, success: bool, iterations_used: int) -> None:
        log = self.query_one("#agent-log", AgentLogWidget)
        if success:
            log.write_success(f"Completed in {iterations_used} iterations")
        else:
            log.write_error(f"Max iterations reached ({iterations_used})")

        # Final story table refresh
        if self._config:
            try:
                self._prd = load_prd(Path.cwd() / self._config.paths.prd)
                story_table = self.query_one("#story-panel", StoryTableWidget)
                story_table.update_stories(self._prd)
            except Exception:
                pass

    def _on_info(self, message: str) -> None:
        log = self.query_one("#agent-log", AgentLogWidget)
        log.write_info(message)

    def _on_error(self, message: str) -> None:
        log = self.query_one("#agent-log", AgentLogWidget)
        log.write_error(message)

    # -- Actions --

    def action_toggle_pause(self) -> None:
        if self.control.pause_requested:
            self.control.resume()
            log = self.query_one("#agent-log", AgentLogWidget)
            log.write_info("Resumed")
        else:
            self.control.request_pause()
            log = self.query_one("#agent-log", AgentLogWidget)
            log.write_info("Paused - press 'p' to resume")

    def action_stop(self) -> None:
        self.control.request_stop()
        log = self.query_one("#agent-log", AgentLogWidget)
        log.write_info("Stopping after current operation...")

    def action_back(self) -> None:
        self.control.request_stop()
        self.app.pop_screen()
