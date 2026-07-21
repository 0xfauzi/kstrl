"""Launcher forms: factory and decompose (TUI surface D6).

Headline options only - everything else resolves env > kstrl.toml >
defaults, exactly like the CLI invoked with just these flags (the
LaunchSpec contract from B1). Submitting hands the spec to
app.launch, which starts the session and replaces the form with the
run's board.

Feature and understand keep their full arg-resolution stacks in the
CLI (where `--tui` already gives them the same embedded experience);
the home launcher points at them honestly instead of duplicating
that resolution here.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Select, Switch

from kstrl.launch import DecomposeLaunch, FactoryLaunch
from kstrl.tui.widgets.context_bar import ContextBar
from kstrl.tui.widgets.form import FormErrors, FormField

REVIEW_MODES = ("", "hard", "advisory", "skip")


class FactoryLaunchForm(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield ContextBar(
            "launch", "everything unset resolves env > kstrl.toml > defaults",
        )
        with Vertical(classes="dialog-host"):
            panel = Vertical(classes="dialog-panel", id="launch-root")
            panel.border_title = "launch factory"
            with panel:
                yield FormField(
                "manifest",
                Input(id="factory-manifest",
                      placeholder="scripts/kstrl/manifest.json"),
                    hint="decompose writes it",
                )
                yield FormField(
                    "max parallel",
                    Input(id="factory-parallel", placeholder="from config"),
                )
                yield FormField(
                    "review mode",
                    Select(
                        [(m, m) for m in REVIEW_MODES if m],
                        allow_blank=True, prompt="from config",
                        id="factory-review",
                    ),
                )
                yield FormErrors(id="launch-errors")
                with Horizontal(classes="wizard-buttons"):
                    yield Button("start factory", id="factory-start",
                                 classes="default-choice")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "factory-start":
            return
        root_dir = getattr(self.app, "root_dir", Path.cwd())
        raw_manifest = self.query_one("#factory-manifest", Input).value.strip()
        manifest_path = (
            (root_dir / raw_manifest if not Path(raw_manifest).is_absolute()
             else Path(raw_manifest))
            if raw_manifest else None
        )
        raw_parallel = self.query_one("#factory-parallel", Input).value.strip()
        errors: list[str] = []
        max_parallel: int | None = None
        if raw_parallel:
            try:
                max_parallel = int(raw_parallel)
            except ValueError:
                errors.append(f"max parallel must be an integer: {raw_parallel}")
            else:
                if max_parallel < 1:
                    errors.append("max parallel must be >= 1")
        default_manifest = root_dir / "scripts" / "kstrl" / "manifest.json"
        if not (manifest_path or default_manifest).exists():
            errors.append(
                f"no manifest at {manifest_path or default_manifest} - "
                "decompose a spec first",
            )
        self.query_one(FormErrors).show(errors)
        if errors:
            return
        review_value = self.query_one("#factory-review", Select).value
        review = review_value if isinstance(review_value, str) else ""
        launch = getattr(self.app, "launch", None)
        if launch is not None:
            launch(FactoryLaunch(
                manifest_path=manifest_path,
                max_parallel=max_parallel,
                review_mode=review or None,
            ))


class DecomposeLaunchForm(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield ContextBar(
            "launch", "everything unset resolves env > kstrl.toml > defaults",
        )
        with Vertical(classes="dialog-host"):
            panel = Vertical(classes="dialog-panel", id="launch-root")
            panel.border_title = "launch decompose"
            with panel:
                yield FormField(
                    "spec",
                    Input(id="decompose-spec",
                          placeholder="scripts/kstrl/spec.md"),
                    hint="markdown or SpecKit dir",
                )
                yield FormField(
                    "project name",
                    Input(id="decompose-project"),
                )
                yield FormField(
                    "base branch",
                    Input(value="main", id="decompose-branch"),
                )
                yield FormField(
                    "single PR",
                    Switch(value=False, id="decompose-single-pr"),
                    hint="one branch for all",
                )
                yield FormErrors(id="launch-errors")
                with Horizontal(classes="wizard-buttons"):
                    yield Button("start decompose", id="decompose-start",
                                 classes="default-choice")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "decompose-start":
            return
        root_dir = getattr(self.app, "root_dir", Path.cwd())
        raw_spec = self.query_one("#decompose-spec", Input).value.strip()
        project = self.query_one("#decompose-project", Input).value.strip()
        branch = (
            self.query_one("#decompose-branch", Input).value.strip() or "main"
        )
        errors: list[str] = []
        spec_path = (
            Path(raw_spec) if Path(raw_spec).is_absolute()
            else root_dir / raw_spec
        ) if raw_spec else None
        if spec_path is None:
            errors.append("spec path is required")
        elif not spec_path.exists():
            errors.append(f"spec not found: {spec_path}")
        if not project:
            errors.append("project name is required")
        self.query_one(FormErrors).show(errors)
        if errors or spec_path is None:
            return
        launch = getattr(self.app, "launch", None)
        if launch is not None:
            launch(DecomposeLaunch(
                spec_path=spec_path,
                project_name=project,
                base_branch=branch,
                single_pr=self.query_one("#decompose-single-pr", Switch).value,
            ))
