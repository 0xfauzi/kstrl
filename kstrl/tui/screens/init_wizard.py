"""Init wizard: form -> preview -> scaffold (TUI surface D5).

One screen, three progressively revealed sections. All content comes
from run_init's existing scaffold functions - zero template drift by
construction (this module never renders a template). The only wizard-
own write is init_wizard.apply_agent_settings, and only when THIS run
created the kstrl.toml and the user actually picked agent values; its
outcome is reported honestly either way.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Select, Static

from kstrl.init_cmd import run_init
from kstrl.init_wizard import (
    AGENT_TYPES,
    apply_agent_settings,
    detect_context,
    plan_scaffold,
)
from kstrl.tui import theme
from kstrl.tui.widgets.form import FormErrors, FormField
from kstrl.ui.plain import PlainUI

if TYPE_CHECKING:
    pass

REASONING_LEVELS = ("", "low", "medium", "high", "max")


class WizardDone(Message):
    def __init__(
        self, exit_code: int, transcript: str, agent_note: str,
    ) -> None:
        super().__init__()
        self.exit_code = exit_code
        self.transcript = transcript
        self.agent_note = agent_note


class InitWizardScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back", "Back"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard-root"):
            yield Static("initialize project", id="wizard-title")
            with Vertical(id="wizard-form"):
                yield FormField(
                    "directory",
                    Input(id="wizard-directory"),
                    hint="project root to scaffold",
                )
                yield FormField(
                    "agent type",
                    Select(
                        [(t or "auto-detect", t) for t in AGENT_TYPES],
                        value="", allow_blank=False, id="wizard-agent-type",
                    ),
                )
                yield FormField(
                    "model",
                    Input(placeholder="agent default", id="wizard-model"),
                )
                yield FormField(
                    "reasoning",
                    Select(
                        [(r or "agent default", r) for r in REASONING_LEVELS],
                        value="", allow_blank=False, id="wizard-reasoning",
                    ),
                )
                yield Static(id="wizard-detected")
                yield FormErrors(id="wizard-errors")
                with Horizontal(classes="wizard-buttons"):
                    yield Button("preview", id="wizard-preview-btn",
                                 classes="default-choice")
            with Vertical(id="wizard-preview"):
                yield Static("plan", id="wizard-plan-title")
                yield Static(id="wizard-plan")
                with Horizontal(classes="wizard-buttons"):
                    yield Button("run init", id="wizard-run-btn",
                                 classes="default-choice")
                    yield Button("back", id="wizard-back-btn")
            with Vertical(id="wizard-result"):
                yield Static("init transcript", id="wizard-log-title")
                with VerticalScroll(id="wizard-log-scroll"):
                    yield Static(id="wizard-log")
                yield Static(id="wizard-outcome")
        yield Footer()

    def on_mount(self) -> None:
        root = getattr(self.app, "root_dir", Path.cwd())
        self.query_one("#wizard-directory", Input).value = str(root)
        context = detect_context(Path(root))
        detected = Text()
        detected.append("detected  ", style=f"bold {theme.MUTED}")
        detected.append(context.get("language", "unknown"))
        for key in ("test_cmd", "typecheck_cmd", "lint_cmd"):
            if context.get(key):
                detected.append(f"  ·  {context[key]}", style=theme.MUTED)
        self.query_one("#wizard-detected", Static).update(detected)
        self._show_stage("form")

    # -- stages --------------------------------------------------------------

    def _show_stage(self, stage: str) -> None:
        self._stage = stage
        self.query_one("#wizard-form").display = stage == "form"
        self.query_one("#wizard-preview").display = stage == "preview"
        self.query_one("#wizard-result").display = stage == "result"

    def _directory(self) -> Path:
        return Path(
            self.query_one("#wizard-directory", Input).value.strip()
            or ".",
        ).expanduser()

    def _agent_values(self) -> tuple[str, str, str]:
        agent_type = str(
            self.query_one("#wizard-agent-type", Select).value or "",
        )
        model = self.query_one("#wizard-model", Input).value.strip()
        reasoning = str(
            self.query_one("#wizard-reasoning", Select).value or "",
        )
        return agent_type, model, reasoning

    def _render_preview(self) -> None:
        directory = self._directory()
        plan = Text()
        for entry in plan_scaffold(directory):
            try:
                display = entry.path.relative_to(directory)
            except ValueError:
                display = entry.path
            if entry.exists:
                plan.append("  · exists - kept   ", style=theme.MUTED)
                plan.append(f"{display}\n", style=theme.MUTED)
            else:
                plan.append("  + will create    ", style=f"bold {theme.ACCENT}")
                plan.append(f"{display}\n")
        agent_type, model, reasoning = self._agent_values()
        if any((agent_type, model, reasoning)):
            if (directory / "kstrl.toml").exists():
                plan.append(
                    "\n  existing kstrl.toml - agent settings will NOT "
                    "be written",
                    style=theme.WARNING,
                )
            else:
                chosen = ", ".join(
                    f"{k}={v}" for k, v in (
                        ("type", agent_type), ("model", model),
                        ("reasoning", reasoning),
                    ) if v
                )
                plan.append(
                    f"\n  [agent] will be set: {chosen}",
                    style=theme.STEEL,
                )
        self.query_one("#wizard-plan", Static).update(plan)

    # -- events --------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "wizard-preview-btn":
            errors: list[str] = []
            if not self._directory().exists():
                errors.append(f"directory not found: {self._directory()}")
            self.query_one(FormErrors).show(errors)
            if errors:
                return
            self._render_preview()
            self._show_stage("preview")
        elif button_id == "wizard-back-btn":
            self._show_stage("form")
        elif button_id == "wizard-run-btn":
            self._run_scaffold()

    def _run_scaffold(self) -> None:
        directory = self._directory()
        agent_type, model, reasoning = self._agent_values()
        toml_missing_before = not (directory / "kstrl.toml").exists()

        def _work() -> None:
            stream = io.StringIO()
            code = run_init(directory, PlainUI(no_color=True, file=stream))
            note = ""
            if any((agent_type, model, reasoning)):
                if not toml_missing_before:
                    note = (
                        "agent settings NOT written: kstrl.toml already "
                        "existed before this run"
                    )
                elif code != 0:
                    note = "agent settings skipped: init did not succeed"
                elif apply_agent_settings(
                    directory / "kstrl.toml",
                    agent_type=agent_type, model=model, reasoning=reasoning,
                ):
                    note = "agent settings written to kstrl.toml [agent]"
                else:
                    note = (
                        "agent settings NOT written: the scaffolded "
                        "[agent] lines were not found"
                    )
            self.post_message(WizardDone(code, stream.getvalue(), note))

        self._show_stage("result")
        self.query_one("#wizard-log", Static).update(
            Text("scaffolding...", style=theme.MUTED),
        )
        self.run_worker(_work, thread=True)

    def on_wizard_done(self, message: WizardDone) -> None:
        self.query_one("#wizard-log", Static).update(
            Text(message.transcript or "(no output)"),
        )
        outcome = Text()
        if message.exit_code == 0:
            outcome.append("✓ init complete", style=f"bold {theme.SUCCESS}")
        else:
            outcome.append(
                f"✗ init exited {message.exit_code}",
                style=f"bold {theme.ERROR}",
            )
        if message.agent_note:
            outcome.append(f"  ·  {message.agent_note}", style=theme.MUTED)
        self.query_one("#wizard-outcome", Static).update(outcome)

    def action_back(self) -> None:
        if getattr(self, "_stage", "form") == "preview":
            self._show_stage("form")
            return
        self.app.pop_screen()
