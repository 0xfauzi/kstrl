"""TUI surface D5: init wizard logic + screen."""

from __future__ import annotations

import time
import tomllib
from pathlib import Path
from threading import Event
from typing import cast
from unittest.mock import patch

from kstrl.init_cmd import DEFAULT_KSTRL_TOML
from kstrl.init_wizard import (
    apply_agent_settings,
    plan_scaffold,
)
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.init_wizard import InitWizardScreen


class TestPlanScaffold:
    def test_markers_flip_after_init(self, tmp_path: Path) -> None:
        before = plan_scaffold(tmp_path)
        assert all(not entry.exists for entry in before)
        names = [entry.path.name for entry in before]
        assert "kstrl.toml" in names
        assert "prompt.md" in names
        assert "CLAUDE.md" in names

        (tmp_path / "kstrl.toml").write_text("")
        after = plan_scaffold(tmp_path)
        by_name = {e.path.name: e.exists for e in after}
        assert by_name["kstrl.toml"] is True
        assert by_name["prd.json"] is False


class TestApplyAgentSettings:
    def test_substitutes_stock_lines(self, tmp_path: Path) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(DEFAULT_KSTRL_TOML)
        assert apply_agent_settings(
            toml, agent_type="codex", model="gpt-5", reasoning="high",
        )
        content = toml.read_text()
        assert 'type = "codex"' in content
        assert 'model = "gpt-5"' in content
        assert 'reasoning_effort = "high"' in content
        assert '# type = ""' not in content
        # Untouched sections stay byte-identical.
        assert "# max_iterations = 10" in content

    def test_escapes_free_form_values_as_toml_strings(
        self, tmp_path: Path,
    ) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(DEFAULT_KSTRL_TOML)
        hostile = 'gpt"\n[factory]\nmax_parallel = 99'
        assert apply_agent_settings(toml, model=hostile)
        parsed = tomllib.loads(toml.read_text())
        assert parsed["agent"]["model"] == hostile
        assert "max_parallel" not in parsed["factory"]

    def test_refuses_user_edited_files_without_writing(
        self, tmp_path: Path,
    ) -> None:
        toml = tmp_path / "kstrl.toml"
        edited = DEFAULT_KSTRL_TOML.replace('# model = ""', 'model = "opus"')
        toml.write_text(edited)
        assert not apply_agent_settings(
            toml, agent_type="codex", model="gpt-5",
        )
        assert toml.read_text() == edited  # all-or-nothing: no write

    def test_empty_values_are_a_noop(self, tmp_path: Path) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(DEFAULT_KSTRL_TOML)
        assert not apply_agent_settings(toml)
        assert toml.read_text() == DEFAULT_KSTRL_TOML

    def test_not_idempotent_reapply_refused(self, tmp_path: Path) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(DEFAULT_KSTRL_TOML)
        assert apply_agent_settings(toml, agent_type="codex")
        # The stock line is gone now; a second apply refuses.
        assert not apply_agent_settings(toml, agent_type="claude-code")
        assert 'type = "codex"' in toml.read_text()


class TestWizardScreen:
    async def _run_wizard(
        self, tmp_path: Path, *, agent_type: str = "",
    ) -> tuple[KstrlTuiApp, InitWizardScreen]:
        app = KstrlTuiApp(root_dir=tmp_path, mode=Mode.HOME,
                          poll_interval=0.05)
        pilot_ctx = app.run_test(size=(120, 45))
        pilot = await pilot_ctx.__aenter__()
        self._pilot_ctx = pilot_ctx
        self._pilot = pilot
        await pilot.pause(0.2)
        app.push_screen(InitWizardScreen())
        await pilot.pause(0.2)
        screen = cast(InitWizardScreen, app.screen)
        if agent_type:
            from textual.widgets import Select

            screen.query_one("#wizard-agent-type", Select).value = agent_type
        return app, screen

    async def test_happy_path_scaffolds_and_writes_agent(
        self, tmp_path: Path,
    ) -> None:
        app, screen = await self._run_wizard(tmp_path, agent_type="codex")
        try:
            from textual.widgets import Button, Static

            screen.query_one("#wizard-preview-btn", Button).press()
            await self._pilot.pause(0.2)
            plan = str(screen.query_one("#wizard-plan", Static).content)
            assert "will create" in plan
            assert "kstrl.toml" in plan
            assert "type=codex" in plan
            screen.query_one("#wizard-run-btn", Button).press()
            deadline = time.monotonic() + 10
            while True:
                await self._pilot.pause(0.1)
                outcome = str(
                    screen.query_one("#wizard-outcome", Static).content,
                )
                if outcome:
                    break
                assert time.monotonic() < deadline, "wizard never finished"
            assert "✓ init complete" in outcome
            assert "agent settings written" in outcome
            assert (tmp_path / "scripts" / "kstrl" / "prompt.md").exists()
            content = (tmp_path / "kstrl.toml").read_text()
            assert 'type = "codex"' in content
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_existing_toml_keeps_agent_settings_out(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("# user file\n")
        app, screen = await self._run_wizard(tmp_path, agent_type="codex")
        try:
            from textual.widgets import Button, Static

            screen.query_one("#wizard-preview-btn", Button).press()
            await self._pilot.pause(0.2)
            plan = str(screen.query_one("#wizard-plan", Static).content)
            assert "exists - kept" in plan
            assert "will NOT be written" in plan
            screen.query_one("#wizard-run-btn", Button).press()
            deadline = time.monotonic() + 10
            while True:
                await self._pilot.pause(0.1)
                outcome = str(
                    screen.query_one("#wizard-outcome", Static).content,
                )
                if outcome:
                    break
                assert time.monotonic() < deadline, "wizard never finished"
            assert "NOT written" in outcome
            assert (tmp_path / "kstrl.toml").read_text() == "# user file\n"
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_bad_directory_blocks_preview(
        self, tmp_path: Path,
    ) -> None:
        app, screen = await self._run_wizard(tmp_path)
        try:
            from textual.widgets import Button, Input

            screen.query_one("#wizard-directory", Input).value = str(
                tmp_path / "nope",
            )
            screen.query_one("#wizard-preview-btn", Button).press()
            await self._pilot.pause(0.2)
            errors = str(screen.query_one("#wizard-errors").content)
            assert "not found" in errors
            assert screen.query_one("#wizard-form").display
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_file_target_blocks_preview(self, tmp_path: Path) -> None:
        target = tmp_path / "not-a-directory"
        target.write_text("data")
        app, screen = await self._run_wizard(tmp_path)
        try:
            from textual.widgets import Button, Input

            screen.query_one("#wizard-directory", Input).value = str(target)
            screen.query_one("#wizard-preview-btn", Button).press()
            await self._pilot.pause(0.2)
            errors = str(screen.query_one("#wizard-errors").content)
            assert "not a directory" in errors
            assert screen.query_one("#wizard-form").display
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_worker_error_is_terminal_and_navigation_waits(
        self, tmp_path: Path,
    ) -> None:
        app, screen = await self._run_wizard(tmp_path)
        try:
            from textual.widgets import Button, Static

            screen.query_one("#wizard-preview-btn", Button).press()
            await self._pilot.pause(0.2)
            release = Event()

            def fail_init(*args: object) -> int:
                del args
                assert release.wait(timeout=5)
                raise OSError("disk unavailable")

            with patch(
                "kstrl.tui.screens.init_wizard.run_init",
                side_effect=fail_init,
            ):
                screen.query_one("#wizard-run-btn", Button).press()
                deadline = time.monotonic() + 5
                while not screen.navigation_blocked:
                    await self._pilot.pause(0.05)
                    assert time.monotonic() < deadline
                screen.action_back()
                assert isinstance(app.screen, InitWizardScreen)
                release.set()
                deadline = time.monotonic() + 5
                while screen.navigation_blocked:
                    await self._pilot.pause(0.1)
                    assert time.monotonic() < deadline
            outcome = str(
                screen.query_one("#wizard-outcome", Static).content,
            )
            transcript = str(
                screen.query_one("#wizard-log", Static).content,
            )
            assert "exited 1" in outcome
            assert "disk unavailable" in transcript
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)
