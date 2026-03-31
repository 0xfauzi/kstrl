"""PRD wizard screen - guided PRD creation, import, and editing."""

from __future__ import annotations

import json
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
    TextArea,
)

from ralph.prd import PRD, create_empty_prd, create_story, parse_markdown_to_stories, save_prd

# Step indices after the mode-selection step
_STEP_MODE = 0
_STEP_FEATURE = 1
_STEP_BRANCH = 2
_STEP_STORIES = 3
_STEP_TECH = 4
_STEP_REVIEW = 5
_TOTAL_STEPS = 6


class PRDWizardScreen(Screen):
    """Multi-step PRD creation wizard with markdown import support."""

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(self, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._step = _STEP_MODE
        self._mode = "scratch"  # "scratch" or "import"
        self._import_path = ""
        self._import_error = ""
        self._branch_name = "ralph/feature"
        self._feature_overview = ""
        self._stories: list[dict[str, str]] = []
        self._tech_stack = ""
        self._verification_commands = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center(classes="wizard-outer"):
            with Vertical(id="wizard-container"):
                yield Static("PRD Wizard", classes="title")
                yield Static(
                    self._progress_dots(),
                    id="wizard-step-label",
                )
                yield VerticalScroll(id="wizard-step")
                with Horizontal(id="wizard-nav"):
                    yield Button("Back", id="wizard-back", variant="default")
                    yield Button("Next", id="wizard-next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._render_step()

    def _progress_dots(self) -> str:
        total = _TOTAL_STEPS
        parts: list[str] = []
        for i in range(total):
            if i == self._step:
                parts.append("[bold]()[/bold]")
            elif i < self._step:
                parts.append("[green]o[/green]")
            else:
                parts.append("[dim]o[/dim]")
        return "  ".join(parts)

    def _render_step(self) -> None:
        step_container = self.query_one("#wizard-step", VerticalScroll)
        step_label = self.query_one("#wizard-step-label", Static)
        back_btn = self.query_one("#wizard-back", Button)
        next_btn = self.query_one("#wizard-next", Button)

        step_container.remove_children()
        step_label.update(self._progress_dots())

        back_btn.disabled = self._step == _STEP_MODE

        if self._step == _STEP_MODE:
            next_btn.label = "Next"
            self._render_mode_step(step_container)
        elif self._step == _STEP_FEATURE:
            next_btn.label = "Next"
            self._render_feature_step(step_container)
        elif self._step == _STEP_BRANCH:
            next_btn.label = "Next"
            self._render_branch_step(step_container)
        elif self._step == _STEP_STORIES:
            next_btn.label = "Next"
            self._render_stories_step(step_container)
        elif self._step == _STEP_TECH:
            next_btn.label = "Next"
            self._render_tech_step(step_container)
        elif self._step == _STEP_REVIEW:
            next_btn.label = "Save"
            self._render_review_step(step_container)

    # -- Step renderers --

    def _render_mode_step(self, container: VerticalScroll) -> None:
        container.mount(Label("How would you like to create your PRD?"))
        container.mount(Static(""))
        container.mount(
            RadioSet(
                RadioButton(
                    "Start from scratch",
                    value=self._mode == "scratch",
                ),
                RadioButton(
                    "Import from a markdown file",
                    value=self._mode == "import",
                ),
                id="prd-mode",
            )
        )

        if self._mode == "import":
            container.mount(Static(""))
            container.mount(Label("Path to markdown file"))
            container.mount(
                Input(
                    value=self._import_path,
                    id="import-path",
                    placeholder="/path/to/spec.md",
                )
            )
            container.mount(
                Static(
                    "[dim]The file will be parsed into user stories. "
                    "Headings become story titles, bullets become acceptance criteria. "
                    "You can review and edit everything before saving.[/dim]",
                    classes="help-text",
                )
            )
            if self._import_error:
                container.mount(
                    Static(f"[red]{self._import_error}[/red]")
                )

    def _render_feature_step(self, container: VerticalScroll) -> None:
        container.mount(Label("Describe the feature you're building"))
        container.mount(
            TextArea(
                self._feature_overview,
                id="feature-overview",
            )
        )

    def _render_branch_step(self, container: VerticalScroll) -> None:
        container.mount(Label("Git branch name for this work"))
        container.mount(Input(
            value=self._branch_name,
            id="branch-name",
            placeholder="ralph/my-feature",
        ))
        container.mount(
            Static(
                "[dim]The agent will check out this branch before starting work.[/dim]",
                classes="help-text",
            )
        )

    def _render_stories_step(self, container: VerticalScroll) -> None:
        container.mount(Label("User stories"))
        container.mount(
            Static(
                "[dim]Each story needs a title and testable acceptance criteria, "
                "one per line.[/dim]",
                classes="help-text",
            )
        )

        if not self._stories:
            self._stories.append({"title": "", "criteria": ""})

        for i, story in enumerate(self._stories):
            container.mount(Label(f"Story {i + 1}"))
            container.mount(Input(
                value=story["title"],
                id=f"story-title-{i}",
                placeholder="Story title",
            ))
            container.mount(
                TextArea(
                    story["criteria"],
                    id=f"story-criteria-{i}",
                )
            )

        container.mount(Button("+ Add Story", id="add-story", variant="success"))

    def _render_tech_step(self, container: VerticalScroll) -> None:
        container.mount(Label("Tech stack"))
        container.mount(
            Static(
                "[dim]Language, framework, testing tools, etc.[/dim]",
                classes="help-text",
            )
        )
        container.mount(
            TextArea(self._tech_stack, id="tech-stack")
        )
        container.mount(Label("Verification commands"))
        container.mount(
            Static(
                "[dim]Commands to verify correctness (e.g., pytest, mypy, npm test). "
                "These are added as acceptance criteria to every story.[/dim]",
                classes="help-text",
            )
        )
        container.mount(
            TextArea(self._verification_commands, id="verification-commands")
        )

    def _render_review_step(self, container: VerticalScroll) -> None:
        prd = self._build_prd()
        preview = json.dumps(prd.to_dict(), indent=2)
        container.mount(Label("Review your PRD"))
        container.mount(Static(
            f"  Branch:  {prd.branch_name}\n"
            f"  Stories: {prd.total_stories}"
        ))
        container.mount(Static(""))
        container.mount(
            TextArea(preview, id="prd-preview", read_only=True)
        )

    # -- Data collection --

    def _save_current_step(self) -> None:
        """Save form data from the current step before navigating."""
        if self._step == _STEP_MODE:
            try:
                radio_set = self.query_one("#prd-mode", RadioSet)
                self._mode = "import" if radio_set.pressed_index == 1 else "scratch"
            except Exception:
                pass
            if self._mode == "import":
                try:
                    self._import_path = self.query_one("#import-path", Input).value
                except Exception:
                    pass
        elif self._step == _STEP_FEATURE:
            try:
                ta = self.query_one("#feature-overview", TextArea)
                self._feature_overview = ta.text
            except Exception:
                pass
        elif self._step == _STEP_BRANCH:
            try:
                inp = self.query_one("#branch-name", Input)
                self._branch_name = inp.value
            except Exception:
                pass
        elif self._step == _STEP_STORIES:
            for i in range(len(self._stories)):
                try:
                    title_inp = self.query_one(f"#story-title-{i}", Input)
                    criteria_ta = self.query_one(f"#story-criteria-{i}", TextArea)
                    self._stories[i]["title"] = title_inp.value
                    self._stories[i]["criteria"] = criteria_ta.text
                except Exception:
                    pass
        elif self._step == _STEP_TECH:
            try:
                self._tech_stack = self.query_one("#tech-stack", TextArea).text
                ta = self.query_one("#verification-commands", TextArea)
                self._verification_commands = ta.text
            except Exception:
                pass

    def _try_import_markdown(self) -> bool:
        """Attempt to import a markdown file. Returns True on success."""
        path = Path(self._import_path).expanduser().resolve()
        if not path.exists():
            self._import_error = f"File not found: {path}"
            return False
        if not path.is_file():
            self._import_error = f"Not a file: {path}"
            return False

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            self._import_error = f"Could not read file: {e}"
            return False

        if not content.strip():
            self._import_error = "File is empty"
            return False

        parsed = parse_markdown_to_stories(content)
        self._feature_overview = parsed.feature_overview
        self._stories = parsed.stories if parsed.stories else [{"title": "", "criteria": ""}]
        self._tech_stack = parsed.tech_stack
        self._verification_commands = parsed.verification_commands
        self._import_error = ""
        return True

    def _build_prd(self) -> PRD:
        """Build a PRD from the wizard data."""
        prd = create_empty_prd(self._branch_name)
        for i, story_data in enumerate(self._stories):
            title = story_data["title"].strip()
            if not title:
                continue
            criteria_lines = [
                line.strip()
                for line in story_data["criteria"].splitlines()
                if line.strip()
            ]
            if self._verification_commands.strip():
                for cmd_line in self._verification_commands.strip().splitlines():
                    cmd_line = cmd_line.strip()
                    if cmd_line and cmd_line not in criteria_lines:
                        criteria_lines.append(cmd_line)

            story = create_story(
                story_id=f"US-{i + 1:03d}",
                title=title,
                acceptance_criteria=criteria_lines or ["Typecheck passes", "Tests pass"],
                priority=i + 1,
            )
            prd.user_stories.append(story)
        return prd

    # -- Navigation --

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "wizard-next":
            self._save_current_step()

            # Handle import on mode step
            if self._step == _STEP_MODE and self._mode == "import":
                if not self._try_import_markdown():
                    self._render_step()  # Re-render to show error
                    return
                # Skip feature overview step (already populated from file)
                self._step = _STEP_BRANCH
                self._render_step()
                return

            if self._step == _STEP_REVIEW:
                self._save_prd()
            else:
                self._step += 1
                self._render_step()
        elif event.button.id == "wizard-back":
            self._save_current_step()
            if self._step > _STEP_MODE:
                self._step -= 1
                self._render_step()
        elif event.button.id == "add-story":
            self._save_current_step()
            self._stories.append({"title": "", "criteria": ""})
            self._render_step()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Show/hide import fields when mode changes."""
        if event.radio_set.id == "prd-mode":
            self._mode = "import" if event.index == 1 else "scratch"
            if self._mode == "scratch":
                self._import_error = ""
            self._render_step()

    def _save_prd(self) -> None:
        prd = self._build_prd()
        output_path = Path.cwd() / "scripts" / "ralph" / "prd.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        save_prd(prd, output_path)

        step_container = self.query_one("#wizard-step", VerticalScroll)
        step_container.remove_children()
        step_container.mount(Static(f"[green]PRD saved to {output_path}[/green]"))
        step_container.mount(Static(
            f"  Branch:  {prd.branch_name}\n"
            f"  Stories: {prd.total_stories}"
        ))
        step_container.mount(Static("\nYou can now run: ralph run"))
        self.query_one("#wizard-next", Button).disabled = True

    def action_back(self) -> None:
        self.app.pop_screen()
