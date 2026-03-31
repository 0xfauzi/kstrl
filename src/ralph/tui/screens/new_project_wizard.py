"""New project wizard - guided setup for greenfield projects.

Combines project initialization and PRD creation into a single flow.
Branch name is auto-derived from the project name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Center, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TextArea,
)

from ralph.config import RalphConfig, save_config
from ralph.models import detect_installed_agents
from ralph.prd import PRD, create_empty_prd, create_story, save_prd
from ralph.prompt import scaffold_project
from ralph.tui.widgets.model_selector import ModelSelector

TOTAL_STEPS = 4


def _slugify(name: str) -> str:
    """Convert a project name to a branch-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "project"


class NewProjectWizardScreen(Screen):
    """Guided wizard for creating brand-new projects from scratch.

    Steps:
        1. Project name and directory
        2. Agent selection
        3. Feature description and user stories (quick PRD)
        4. Scaffold and finish
    """

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(self, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._step = 0
        self._project_name = ""
        self._target_dir = str(Path.cwd())
        self._feature_overview = ""
        self._stories: list[dict[str, str]] = []
        self._tech_stack = ""
        self._verification_commands = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center(classes="wizard-outer"):
            with Vertical(id="new-project-container"):
                yield Static("New Project", classes="title")
                yield Static(
                    self._progress_dots(),
                    id="new-project-step-label",
                )
                yield Vertical(id="new-project-step")
                with Horizontal(id="new-project-nav"):
                    yield Button("Back", id="np-back", variant="default", disabled=True)
                    yield Button("Next", id="np-next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._render_step()

    def _progress_dots(self) -> str:
        parts: list[str] = []
        for i in range(TOTAL_STEPS):
            if i == self._step:
                parts.append("[bold]()[/bold]")
            elif i < self._step:
                parts.append("[green]o[/green]")
            else:
                parts.append("[dim]o[/dim]")
        return "  ".join(parts) + f"    Step {self._step + 1} of {TOTAL_STEPS}"

    def _render_step(self) -> None:
        container = self.query_one("#new-project-step", Vertical)
        label = self.query_one("#new-project-step-label", Static)
        back_btn = self.query_one("#np-back", Button)
        next_btn = self.query_one("#np-next", Button)

        container.remove_children()
        label.update(self._progress_dots())

        back_btn.disabled = self._step == 0

        if self._step == 0:
            next_btn.label = "Next"
            self._render_name_step(container)
        elif self._step == 1:
            next_btn.label = "Next"
            self._render_agent_step(container)
        elif self._step == 2:
            next_btn.label = "Next"
            self._render_prd_step(container)
        elif self._step == 3:
            next_btn.label = "Create Project"
            self._render_review_step(container)

    # -- Step renderers --

    def _render_name_step(self, container: Vertical) -> None:
        container.mount(Label("Project name"))
        container.mount(
            Input(
                value=self._project_name,
                id="project-name",
                placeholder="my-awesome-project",
            )
        )
        container.mount(Label("Target directory"))
        container.mount(
            Input(
                value=self._target_dir,
                id="target-dir",
                placeholder="/path/to/project",
            )
        )
        container.mount(
            Static(
                "[dim]Ralph will scaffold files in this directory. "
                "The branch name will be auto-generated from the project name.[/dim]",
                classes="help-text",
            )
        )

    def _render_agent_step(self, container: Vertical) -> None:
        container.mount(Label("Select your AI agent"))
        container.mount(ModelSelector(id="np-model-selector"))

        installed = detect_installed_agents()
        if not installed:
            container.mount(
                Static(
                    "[yellow]No agents detected. "
                    "Install claude or codex first.[/yellow]"
                )
            )

    def _render_prd_step(self, container: Vertical) -> None:
        container.mount(Label("What are you building?"))
        container.mount(
            TextArea(
                self._feature_overview,
                id="np-feature-overview",
            )
        )
        container.mount(Static(""))
        container.mount(Label("User stories"))
        container.mount(
            Static(
                "[dim]Add at least one story with acceptance criteria. "
                "One criterion per line.[/dim]",
                classes="help-text",
            )
        )

        if not self._stories:
            self._stories.append({"title": "", "criteria": ""})

        for i, story in enumerate(self._stories):
            container.mount(Label(f"Story {i + 1}"))
            container.mount(
                Input(
                    value=story["title"],
                    id=f"np-story-title-{i}",
                    placeholder="Story title",
                )
            )
            container.mount(
                TextArea(
                    story["criteria"],
                    id=f"np-story-criteria-{i}",
                )
            )

        container.mount(Button("+ Add Story", id="np-add-story", variant="success"))
        container.mount(Static(""))
        container.mount(Label("Tech stack"))
        container.mount(
            TextArea(self._tech_stack, id="np-tech-stack")
        )
        container.mount(Label("Verification commands"))
        container.mount(
            Static(
                "[dim]Commands to verify correctness (e.g., pytest, mypy, npm test). "
                "Added as acceptance criteria to every story.[/dim]",
                classes="help-text",
            )
        )
        container.mount(
            TextArea(self._verification_commands, id="np-verification")
        )

    def _render_review_step(self, container: Vertical) -> None:
        slug = _slugify(self._project_name)
        branch = f"ralph/{slug}"
        prd = self._build_prd(branch)
        preview = json.dumps(prd.to_dict(), indent=2)

        container.mount(Label("Review"))
        container.mount(Static(f"  Project:   {self._project_name}"))
        container.mount(Static(f"  Directory: {self._target_dir}"))
        container.mount(Static(f"  Branch:    {branch}"))
        container.mount(Static(f"  Stories:   {prd.total_stories}"))
        container.mount(Static(""))
        container.mount(
            TextArea(preview, id="np-preview", read_only=True)
        )

    # -- Data collection --

    def _save_current_step(self) -> None:
        if self._step == 0:
            try:
                self._project_name = self.query_one("#project-name", Input).value
                self._target_dir = self.query_one("#target-dir", Input).value
            except Exception:
                pass
        elif self._step == 2:
            try:
                self._feature_overview = self.query_one(
                    "#np-feature-overview", TextArea
                ).text
            except Exception:
                pass
            for i in range(len(self._stories)):
                try:
                    self._stories[i]["title"] = self.query_one(
                        f"#np-story-title-{i}", Input
                    ).value
                    self._stories[i]["criteria"] = self.query_one(
                        f"#np-story-criteria-{i}", TextArea
                    ).text
                except Exception:
                    pass
            try:
                self._tech_stack = self.query_one("#np-tech-stack", TextArea).text
                self._verification_commands = self.query_one(
                    "#np-verification", TextArea
                ).text
            except Exception:
                pass

    def _build_prd(self, branch_name: str) -> PRD:
        prd = create_empty_prd(branch_name)
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

    # -- Button handling --

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "np-next":
            self._save_current_step()
            if self._step == TOTAL_STEPS - 1:
                self._create_project()
            else:
                self._step += 1
                self._render_step()
        elif event.button.id == "np-back":
            self._save_current_step()
            if self._step > 0:
                self._step -= 1
                self._render_step()
        elif event.button.id == "np-add-story":
            self._save_current_step()
            self._stories.append({"title": "", "criteria": ""})
            self._render_step()

    def _create_project(self) -> None:
        target = Path(self._target_dir).resolve()
        slug = _slugify(self._project_name)
        branch = f"ralph/{slug}"

        container = self.query_one("#new-project-step", Vertical)
        container.remove_children()

        container.mount(Static(f"Creating project at {target}..."))

        # Scaffold files
        messages = scaffold_project(target)
        for msg in messages:
            if msg.startswith("Created"):
                container.mount(Static(f"[green]{msg}[/green]"))
            else:
                container.mount(Static(f"[dim]{msg}[/dim]"))

        # Create ralph.toml
        config = RalphConfig()
        try:
            selector = self.query_one("#np-model-selector", ModelSelector)
            config.agent.type = selector.agent_type
            config.agent.model = selector.model
        except Exception:
            installed = detect_installed_agents()
            if "claude" in installed:
                config.agent.type = "claude"
            elif "codex" in installed:
                config.agent.type = "codex"

        toml_path = target / "ralph.toml"
        if not toml_path.exists():
            save_config(config, toml_path)
            container.mount(Static("[green]Created: ralph.toml[/green]"))

        # Save PRD
        prd = self._build_prd(branch)
        prd_path = target / "scripts" / "ralph" / "prd.json"
        prd_path.parent.mkdir(parents=True, exist_ok=True)
        save_prd(prd, prd_path)
        container.mount(
            Static(f"[green]Created: PRD with {prd.total_stories} stories[/green]")
        )

        container.mount(Static(""))
        container.mount(Static("[bold green]Project created[/bold green]"))
        container.mount(Static(
            "\nNext steps:\n"
            f"  cd {target}\n"
            f"  ralph run 25    - Run the feature loop"
        ))

        self.query_one("#np-next", Button).disabled = True
        self.query_one("#new-project-step-label", Static).update("Done")

    def action_back(self) -> None:
        self.app.pop_screen()
