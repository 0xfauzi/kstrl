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
    ScaffoldEntry,
    apply_agent_settings,
    detect_context,
    plan_scaffold,
)
from kstrl.tui import theme
from kstrl.tui.widgets.context_bar import ContextBar
from kstrl.tui.widgets.form import FormErrors, FormField
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig, resolve_verify_commands

if TYPE_CHECKING:
    pass

REASONING_LEVELS = ("", "low", "medium", "high", "max")


class WizardDone(Message):
    def __init__(
        self,
        exit_code: int,
        transcript: str,
        agent_note: str,
    ) -> None:
        super().__init__()
        self.exit_code = exit_code
        self.transcript = transcript
        self.agent_note = agent_note


_LABEL_WIDTH = 9


def _detected_text(root: Path) -> Text:
    """The project's language and the commands Phase 1 will run (#261).

    The commands come from the gate's own resolver, so the wizard shows
    what will actually run; it used to show init's guesses. One labelled
    line each - see the `#wizard-detected` rule in styles.tcss for why
    they cannot share a line.

    VerifyConfig.load raises ValueError on malformed TOML by design
    (config._load_toml), and this is the screen an operator opens to
    repair a broken scaffold, so it reports one rather than taking the
    app down on mount.
    """
    rows: list[tuple[str, str]] = [
        ("detected", detect_context(root).get("language", "unknown")),
    ]
    try:
        commands = resolve_verify_commands(VerifyConfig.load(root), root)
    except (ValueError, OSError):
        rows.append(("verify", "kstrl.toml is unreadable; cannot show gate commands"))
    else:
        rows += [
            ("test", commands.test),
            ("typecheck", commands.typecheck),
            ("lint", commands.lint),
        ]
    text = Text()
    for index, (label, value) in enumerate(rows):
        if index:
            text.append("\n")
        text.append(f"{label:<{_LABEL_WIDTH}}  ", style=f"bold {theme.MUTED}")
        text.append(value, style=theme.MUTED if index else "")
    return text


def _plan_row(entry: ScaffoldEntry, display: object) -> list[tuple[str, str]]:
    """The styled segments for one scaffold-preview row.

    Its own function so the four-way choice does not live inside
    ``_render_preview``, which already carries the agent-settings
    branch and sits at the cognitive-complexity ceiling.

    #286: "exists - kept" alone reads as "your scaffold is fine", and
    this preview is the surface most likely to be read that way. A
    label can only attach to a file that exists, i.e. to a "keep", so
    that row is replaced rather than competing with create or append.
    """
    if entry.stale_label:
        return [
            ("  ! older template  ", f"bold {theme.WARNING}"),
            (f"{display}", theme.WARNING),
            (f"  (shipped at {entry.stale_label})\n", theme.MUTED),
        ]
    if entry.action == "keep":
        return [
            ("  · exists - kept   ", theme.MUTED),
            (f"{display}\n", theme.MUTED),
        ]
    if entry.action == "append":
        return [
            ("  ~ append block    ", f"bold {theme.ACCENT}"),
            (f"{display}\n", ""),
        ]
    return [
        ("  + will create    ", f"bold {theme.ACCENT}"),
        (f"{display}\n", ""),
    ]


class InitWizardScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._scaffolding = False

    @property
    def navigation_blocked(self) -> bool:
        return self._scaffolding

    def compose(self) -> ComposeResult:
        yield ContextBar(
            "init",
            "rewrites nothing - files kept, .gitignore appended, lockfile staged",
        )
        with Vertical(classes="dialog-host"):
            panel = Vertical(classes="dialog-panel", id="wizard-root")
            panel.border_title = "initialize project"
            with panel:
                with Vertical(id="wizard-form"):
                    yield FormField(
                        "directory",
                        Input(id="wizard-directory"),
                        hint="root to scaffold",
                    )
                    yield FormField(
                        "agent type",
                        Select(
                            [(t, t) for t in AGENT_TYPES if t],
                            allow_blank=True,
                            prompt="auto-detect",
                            id="wizard-agent-type",
                        ),
                    )
                    yield FormField(
                        "model",
                        Input(placeholder="agent default", id="wizard-model"),
                    )
                    yield FormField(
                        "reasoning",
                        Select(
                            [(r, r) for r in REASONING_LEVELS if r],
                            allow_blank=True,
                            prompt="agent default",
                            id="wizard-reasoning",
                        ),
                    )
                    yield Static(id="wizard-detected")
                    yield FormErrors(id="wizard-errors")
                    with Horizontal(classes="wizard-buttons"):
                        yield Button("preview", id="wizard-preview-btn", classes="default-choice")
                with Vertical(id="wizard-preview"):
                    yield Static("plan", id="wizard-plan-title")
                    yield Static(id="wizard-plan")
                    with Horizontal(classes="wizard-buttons"):
                        yield Button("run init", id="wizard-run-btn", classes="default-choice")
                        yield Button("back", id="wizard-back-btn")
                with Vertical(id="wizard-result"):
                    yield Static("init transcript", id="wizard-log-title")
                    with VerticalScroll(id="wizard-log-scroll"):
                        yield Static(id="wizard-log")
                    yield Static(id="wizard-outcome")
        yield Footer()

    def on_mount(self) -> None:
        root = Path(getattr(self.app, "root_dir", Path.cwd()))
        self.query_one("#wizard-directory", Input).value = str(root)
        self.query_one("#wizard-detected", Static).update(_detected_text(root))
        self._show_stage("form")

    # -- stages --------------------------------------------------------------

    def _show_stage(self, stage: str) -> None:
        self._stage = stage
        self.query_one("#wizard-form").display = stage == "form"
        self.query_one("#wizard-preview").display = stage == "preview"
        self.query_one("#wizard-result").display = stage == "result"

    def _directory(self) -> Path:
        return Path(
            self.query_one("#wizard-directory", Input).value.strip() or ".",
        ).expanduser()

    def _agent_values(self) -> tuple[str, str, str]:
        raw_type = self.query_one("#wizard-agent-type", Select).value
        agent_type = raw_type if isinstance(raw_type, str) else ""
        model = self.query_one("#wizard-model", Input).value.strip()
        raw_reasoning = self.query_one("#wizard-reasoning", Select).value
        reasoning = raw_reasoning if isinstance(raw_reasoning, str) else ""
        return agent_type, model, reasoning

    def _render_preview(self) -> None:
        directory = self._directory()
        plan = Text()
        for entry in plan_scaffold(directory):
            try:
                display = entry.path.relative_to(directory)
            except ValueError:
                display = entry.path
            for text, style in _plan_row(entry, display):
                plan.append(text, style=style)
        agent_type, model, reasoning = self._agent_values()
        if any((agent_type, model, reasoning)):
            if (directory / "kstrl.toml").exists():
                plan.append(
                    "\n  existing kstrl.toml - agent settings will NOT be written",
                    style=theme.WARNING,
                )
            else:
                chosen = ", ".join(
                    f"{k}={v}"
                    for k, v in (
                        ("type", agent_type),
                        ("model", model),
                        ("reasoning", reasoning),
                    )
                    if v
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
            directory = self._directory()
            if not directory.exists():
                errors.append(f"directory not found: {directory}")
            elif not directory.is_dir():
                errors.append(f"not a directory: {directory}")
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
        if self._scaffolding:
            return
        self._scaffolding = True
        directory = self._directory()
        agent_type, model, reasoning = self._agent_values()
        toml_missing_before = not (directory / "kstrl.toml").exists()

        def _work() -> None:
            stream = io.StringIO()
            note = ""
            try:
                code = run_init(
                    directory,
                    PlainUI(no_color=True, file=stream),
                )
                if any((agent_type, model, reasoning)):
                    if not toml_missing_before:
                        note = (
                            "agent settings NOT written: kstrl.toml already existed before this run"
                        )
                    elif code != 0:
                        note = "agent settings skipped: init did not succeed"
                    elif apply_agent_settings(
                        directory / "kstrl.toml",
                        agent_type=agent_type,
                        model=model,
                        reasoning=reasoning,
                    ):
                        note = "agent settings written to kstrl.toml [agent]"
                    else:
                        note = (
                            "agent settings NOT written: the scaffolded "
                            "[agent] lines were not found"
                        )
            except (OSError, ValueError) as exc:
                code = 1
                stream.write(f"init failed: {exc}\n")
            self.post_message(WizardDone(code, stream.getvalue(), note))

        self._show_stage("result")
        self.query_one("#wizard-log", Static).update(
            Text("scaffolding...", style=theme.MUTED),
        )
        self.query_one("#wizard-run-btn", Button).disabled = True
        self.run_worker(_work, thread=True)

    def on_wizard_done(self, message: WizardDone) -> None:
        self._scaffolding = False
        self.query_one("#wizard-run-btn", Button).disabled = False
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
        if self._scaffolding:
            self.app.notify(
                "init is still writing project files; wait for it to finish",
                severity="warning",
            )
            return
        if getattr(self, "_stage", "form") == "preview":
            self._show_stage("form")
            return
        self.app.pop_screen()
