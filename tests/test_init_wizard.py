"""TUI surface D5: init wizard logic + screen."""

from __future__ import annotations

import tomllib
from pathlib import Path
from threading import Event
from typing import cast
from unittest.mock import patch

import pytest

from kstrl.init_cmd import DEFAULT_KSTRL_TOML
from kstrl.init_wizard import (
    apply_agent_settings,
    plan_scaffold,
)
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.home import HomeScreen
from kstrl.tui.screens.init_wizard import InitWizardScreen
from kstrl.verify import (
    DEFAULT_LINT_COMMAND,
    DEFAULT_TEST_COMMAND,
    DEFAULT_TYPECHECK_COMMAND,
)
from tests.helpers.settle import drained, mounted, settled


class TestPlanScaffold:
    def test_markers_flip_after_init(self, tmp_path: Path) -> None:
        before = plan_scaffold(tmp_path)
        assert all(entry.action == "create" for entry in before)
        names = [entry.path.name for entry in before]
        assert "kstrl.toml" in names
        assert "prompt.md" in names
        assert "CLAUDE.md" in names
        assert ".gitignore" in names

        (tmp_path / "kstrl.toml").write_text("")
        after = plan_scaffold(tmp_path)
        by_name = {e.path.name: e for e in after}
        assert by_name["kstrl.toml"].action == "keep"
        assert by_name["prd.json"].action == "create"

    def test_existing_gitignore_is_planned_as_an_append(self, tmp_path: Path) -> None:
        """An existing .gitignore is added to, not kept as-is (#201)."""
        (tmp_path / ".gitignore").write_text("secrets.env\n")

        planned = {e.path.name: e for e in plan_scaffold(tmp_path)}
        assert planned[".gitignore"].action == "append"

    def test_an_unreadable_gitignore_is_planned_as_kept(self, tmp_path: Path) -> None:
        """The preview must not promise a write init will not make: a
        .gitignore it cannot read is left alone, and reading it used to
        crash the preview with UnicodeDecodeError (#201 review)."""
        (tmp_path / ".gitignore").write_bytes(b"build\xff/\n")

        planned = {e.path.name: e for e in plan_scaffold(tmp_path)}
        assert planned[".gitignore"].action == "keep"

    def test_only_gitignore_is_ever_planned_as_an_append(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("secrets.env\n")
        (tmp_path / "kstrl.toml").write_text("")

        appending = [e.path.name for e in plan_scaffold(tmp_path) if e.action == "append"]
        assert appending == [".gitignore"]


class TestApplyAgentSettings:
    def test_substitutes_stock_lines(self, tmp_path: Path) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(DEFAULT_KSTRL_TOML)
        assert apply_agent_settings(
            toml,
            agent_type="codex",
            model="gpt-5",
            reasoning="high",
        )
        content = toml.read_text()
        assert 'type = "codex"' in content
        assert 'model = "gpt-5"' in content
        assert 'reasoning_effort = "high"' in content
        assert '# type = ""' not in content
        # Untouched sections stay byte-identical.
        assert "# max_iterations = 10" in content

    def test_escapes_free_form_values_as_toml_strings(
        self,
        tmp_path: Path,
    ) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(DEFAULT_KSTRL_TOML)
        hostile = 'gpt"\n[factory]\nmax_parallel = 99'
        assert apply_agent_settings(toml, model=hostile)
        parsed = tomllib.loads(toml.read_text())
        assert parsed["agent"]["model"] == hostile
        assert "max_parallel" not in parsed["factory"]

    def test_refuses_user_edited_files_without_writing(
        self,
        tmp_path: Path,
    ) -> None:
        toml = tmp_path / "kstrl.toml"
        edited = DEFAULT_KSTRL_TOML.replace('# model = ""', 'model = "opus"')
        toml.write_text(edited)
        assert not apply_agent_settings(
            toml,
            agent_type="codex",
            model="gpt-5",
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
        self,
        tmp_path: Path,
        *,
        agent_type: str = "",
    ) -> tuple[KstrlTuiApp, InitWizardScreen]:
        app = KstrlTuiApp(root_dir=tmp_path, mode=Mode.HOME, poll_interval=0.05)
        pilot_ctx = app.run_test(size=(120, 45))
        pilot = await pilot_ctx.__aenter__()
        # Stashed BEFORE the first wait, not after: every test in this
        # class closes the pilot in a `finally` that reads
        # `self._pilot_ctx`, so a settle that fails above the assignment
        # would be reported as an AttributeError in the teardown instead
        # of as the thing that actually went wrong.
        self._pilot_ctx = pilot_ctx
        self._pilot = pilot
        # The app's own on_mount pushes the home screen, so the wizard
        # has to go on top of that rather than on top of the screen the
        # home screen is replacing.
        await settled(
            pilot,
            lambda: isinstance(app.screen, HomeScreen),
            what="the home screen to be pushed by the app's on_mount",
        )
        app.push_screen(InitWizardScreen())
        await settled(
            pilot,
            lambda: isinstance(app.screen, InitWizardScreen),
            what="the init wizard to become the active screen",
        )
        screen = cast(InitWizardScreen, app.screen)
        await mounted(pilot, lambda: app.screen, "#wizard-agent-type")
        # compose makes the form queryable BEFORE the screen's own
        # on_mount fills the directory field and the detected line, so
        # the mount above is not enough on its own. Textual dispatches
        # Compose and Mount at the head of the screen's message loop,
        # ahead of anything on its queue, so one hop on that queue is
        # proof that on_mount has run.
        await drained(pilot, screen, what="the wizard's on_mount to fill the form")
        if agent_type:
            from textual.widgets import Select

            screen.query_one("#wizard-agent-type", Select).value = agent_type
        return app, screen

    def _rendered(self, app: KstrlTuiApp) -> str:
        """Text actually painted on the terminal.

        Asserting on the Text object is what let #261's first attempt
        through: it built a two-line Text while styles.tcss pinned
        `#wizard-detected` to `height: 1`, so the second line rendered
        nowhere. The screenshot encodes spaces as `&#160;`, so normalize
        before matching.
        """
        return app.export_screenshot().replace("&#160;", " ")

    async def test_detected_line_renders_the_resolved_gate_commands(
        self,
        tmp_path: Path,
    ) -> None:
        """#261: the wizard shows what Phase 1 will actually run, and it
        has to be visible, not merely constructed.

        The screenshot comes off the compositor, so the form has to have
        been laid out before it is read. Waiting on the FORM's region is
        deliberately weaker than either assertion below: a detected
        block squashed back to height 1, which is the defect this test
        names, still lays the form out and so still reaches the
        assertions and fails there.
        """
        app, screen = await self._run_wizard(tmp_path)
        try:
            form = await mounted(self._pilot, lambda: app.screen, "#wizard-form")
            await settled(
                self._pilot,
                lambda: form.region.height,
                what="the wizard form to be laid out",
            )
            rendered = self._rendered(app)
            assert "detected" in rendered
            for command in (
                DEFAULT_TEST_COMMAND,
                DEFAULT_TYPECHECK_COMMAND,
                DEFAULT_LINT_COMMAND,
            ):
                assert command in rendered, f"{command!r} not painted"
            assert screen.query_one("#wizard-detected").size.height >= 4
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_configured_commands_are_the_ones_shown(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[verify]\nlint_command = "npx eslint ."\n',
        )
        app, _ = await self._run_wizard(tmp_path)
        try:
            # The form's layout, not the text: the assertions own the text.
            form = await mounted(self._pilot, lambda: app.screen, "#wizard-form")
            await settled(
                self._pilot,
                lambda: form.region.height,
                what="the wizard form to be laid out",
            )
            rendered = self._rendered(app)
            assert "npx eslint ." in rendered
            assert DEFAULT_LINT_COMMAND not in rendered
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_a_malformed_kstrl_toml_does_not_take_the_app_down(
        self,
        tmp_path: Path,
    ) -> None:
        """VerifyConfig.load raises ValueError on bad TOML by design.
        The wizard is the screen an operator opens to repair a broken
        scaffold, so it must survive one and say so."""
        (tmp_path / "kstrl.toml").write_text("[verify\ntest_command = broken")
        app, _ = await self._run_wizard(tmp_path)
        try:
            # The form's layout, not the text: the assertions own the text.
            form = await mounted(self._pilot, lambda: app.screen, "#wizard-form")
            await settled(
                self._pilot,
                lambda: form.region.height,
                what="the wizard form to be laid out",
            )
            rendered = self._rendered(app)
            assert "unreadable" in rendered
            assert DEFAULT_TEST_COMMAND not in rendered
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_happy_path_scaffolds_and_writes_agent(
        self,
        tmp_path: Path,
    ) -> None:
        app, screen = await self._run_wizard(tmp_path, agent_type="codex")
        try:
            from textual.widgets import Button, Static

            preview_stage = await mounted(
                self._pilot,
                lambda: app.screen,
                "#wizard-preview",
            )
            outcome_widget = await mounted(
                self._pilot,
                lambda: app.screen,
                "#wizard-outcome",
            )
            screen.query_one("#wizard-preview-btn", Button).press()
            # The stage flip, not the plan text: on_button_pressed
            # renders the plan and only then reveals the stage, so this
            # is a real observation of the press being handled without
            # asserting anything about what the plan says.
            await settled(
                self._pilot,
                lambda: preview_stage.display,
                what="the preview button to reveal the plan stage",
            )
            plan = str(screen.query_one("#wizard-plan", Static).content)
            assert "will create" in plan
            assert "kstrl.toml" in plan
            assert "type=codex" in plan
            screen.query_one("#wizard-run-btn", Button).press()
            # An outcome at all, which is weaker than the two assertions
            # below: a wrong outcome still reaches them and fails with
            # its own message. Only "no outcome ever" times out here,
            # and that is what the loop this replaces reported as
            # "wizard never finished". run_init writes real files on a
            # worker thread, so the 10s budget is kept.
            await settled(
                self._pilot,
                lambda: str(outcome_widget.content),
                what="the scaffold worker to report an outcome",
                timeout=10.0,
            )
            outcome = str(screen.query_one("#wizard-outcome", Static).content)
            assert "✓ init complete" in outcome
            assert "agent settings written" in outcome
            assert (tmp_path / "scripts" / "kstrl" / "prompt.md").exists()
            content = (tmp_path / "kstrl.toml").read_text()
            assert 'type = "codex"' in content
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_preview_flags_a_stale_prompt_instead_of_kept(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#286: "exists - kept" on its own reads as "your scaffold is
        fine", and this preview is the surface most likely to be read
        that way. A prompt that is an unedited OLDER kstrl template says
        so here, with the label it shipped under."""
        import hashlib

        from kstrl import init_cmd
        from kstrl.init_cmd import ScaffoldedTemplate

        old_body = "# old engineer instructions\n"
        new_body = "# new engineer instructions\n"

        def digest(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        monkeypatch.setattr(
            init_cmd,
            "SCAFFOLDED_TEMPLATES",
            (
                ScaffoldedTemplate(
                    filename="prompt.md",
                    constant_name="DEFAULT_PROMPT",
                    body=new_body,
                    history=((digest(old_body), "9.0.0"), (digest(new_body), "9.1.0")),
                ),
            ),
        )
        prompt = tmp_path / "scripts" / "kstrl" / "prompt.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text(old_body)

        app, screen = await self._run_wizard(tmp_path)
        try:
            from textual.widgets import Button, Static

            preview_stage = await mounted(
                self._pilot,
                lambda: app.screen,
                "#wizard-preview",
            )
            screen.query_one("#wizard-preview-btn", Button).press()
            await settled(
                self._pilot,
                lambda: preview_stage.display,
                what="the preview button to reveal the plan stage",
            )
            plan = str(screen.query_one("#wizard-plan", Static).content)
            assert "older template" in plan
            assert "shipped at 9.0.0" in plan
            # The row is the prompt's, and only the prompt's.
            stale_rows = [line for line in plan.splitlines() if "older template" in line]
            assert len(stale_rows) == 1
            assert "prompt.md" in stale_rows[0]
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_existing_toml_keeps_agent_settings_out(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("# user file\n")
        app, screen = await self._run_wizard(tmp_path, agent_type="codex")
        try:
            from textual.widgets import Button, Static

            preview_stage = await mounted(
                self._pilot,
                lambda: app.screen,
                "#wizard-preview",
            )
            outcome_widget = await mounted(
                self._pilot,
                lambda: app.screen,
                "#wizard-outcome",
            )
            screen.query_one("#wizard-preview-btn", Button).press()
            await settled(
                self._pilot,
                lambda: preview_stage.display,
                what="the preview button to reveal the plan stage",
            )
            plan = str(screen.query_one("#wizard-plan", Static).content)
            assert "exists - kept" in plan
            assert "will NOT be written" in plan
            screen.query_one("#wizard-run-btn", Button).press()
            # Any outcome at all, not the one asserted below, and the
            # 10s budget the hand-rolled loop had. A wrong outcome
            # fails at the assertion; only silence times out here.
            await settled(
                self._pilot,
                lambda: str(outcome_widget.content),
                what="the scaffold worker to report an outcome",
                timeout=10.0,
            )
            outcome = str(screen.query_one("#wizard-outcome", Static).content)
            assert "NOT written" in outcome
            assert (tmp_path / "kstrl.toml").read_text() == "# user file\n"
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_bad_directory_blocks_preview(
        self,
        tmp_path: Path,
    ) -> None:
        app, screen = await self._run_wizard(tmp_path)
        try:
            from textual.widgets import Button, Input

            errors_widget = await mounted(
                self._pilot,
                lambda: app.screen,
                "#wizard-errors",
            )
            screen.query_one("#wizard-directory", Input).value = str(
                tmp_path / "nope",
            )
            screen.query_one("#wizard-preview-btn", Button).press()
            # An error strip with anything in it. Which error it names,
            # and whether the form stayed up, are the assertions.
            await settled(
                self._pilot,
                lambda: str(errors_widget.content),
                what="the preview button to report a validation error",
            )
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

            errors_widget = await mounted(
                self._pilot,
                lambda: app.screen,
                "#wizard-errors",
            )
            screen.query_one("#wizard-directory", Input).value = str(target)
            screen.query_one("#wizard-preview-btn", Button).press()
            # An error strip with anything in it. Which error it names,
            # and whether the form stayed up, are the assertions.
            await settled(
                self._pilot,
                lambda: str(errors_widget.content),
                what="the preview button to report a validation error",
            )
            errors = str(screen.query_one("#wizard-errors").content)
            assert "not a directory" in errors
            assert screen.query_one("#wizard-form").display
        finally:
            await self._pilot_ctx.__aexit__(None, None, None)

    async def test_worker_error_is_terminal_and_navigation_waits(
        self,
        tmp_path: Path,
    ) -> None:
        app, screen = await self._run_wizard(tmp_path)
        try:
            from textual.widgets import Button, Static

            preview_stage = await mounted(
                self._pilot,
                lambda: app.screen,
                "#wizard-preview",
            )
            screen.query_one("#wizard-preview-btn", Button).press()
            await settled(
                self._pilot,
                lambda: preview_stage.display,
                what="the preview button to reveal the plan stage",
            )
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
                await settled(
                    self._pilot,
                    lambda: screen.navigation_blocked,
                    what="the run button to start the scaffold worker",
                )
                screen.action_back()
                assert isinstance(app.screen, InitWizardScreen)
                release.set()
                # on_wizard_done clears the flag and then writes both
                # panes in the same synchronous handler, so this is one
                # observation for all three reads and it asserts none of
                # what they say.
                await settled(
                    self._pilot,
                    lambda: not screen.navigation_blocked,
                    what="the failed worker to release navigation",
                )
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
