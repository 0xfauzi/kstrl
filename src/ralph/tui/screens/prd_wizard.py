"""PRD wizard screen - guided PRD creation, import, and editing."""

from __future__ import annotations

import json
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

from ralph.prd import PRD, create_empty_prd, create_story, save_prd


class PRDWizardScreen(Screen):
    """Multi-step PRD creation wizard."""

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(self, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._step = 0
        self._branch_name = "ralph/feature"
        self._feature_overview = ""
        self._stories: list[dict[str, str]] = []
        self._tech_stack = ""
        self._verification_commands = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center():
            with Vertical(id="wizard-container"):
                yield Static("PRD Wizard", classes="title")
                yield Static("Step 1 of 5: Feature Overview", id="wizard-step-label")
                yield Vertical(id="wizard-step")
                with Horizontal(id="wizard-nav"):
                    yield Button("Back", id="wizard-back", variant="default")
                    yield Button("Next", id="wizard-next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._render_step()

    def _render_step(self) -> None:
        step_container = self.query_one("#wizard-step", Vertical)
        step_label = self.query_one("#wizard-step-label", Static)
        back_btn = self.query_one("#wizard-back", Button)
        next_btn = self.query_one("#wizard-next", Button)

        # Clear previous content
        step_container.remove_children()

        back_btn.disabled = self._step == 0
        next_btn.label = "Save" if self._step == 4 else "Next"

        step_titles = [
            "Step 1 of 5: Feature Overview",
            "Step 2 of 5: Branch Name",
            "Step 3 of 5: User Stories",
            "Step 4 of 5: Tech Stack & Verification",
            "Step 5 of 5: Review & Save",
        ]
        step_label.update(step_titles[self._step])

        if self._step == 0:
            self._render_feature_step(step_container)
        elif self._step == 1:
            self._render_branch_step(step_container)
        elif self._step == 2:
            self._render_stories_step(step_container)
        elif self._step == 3:
            self._render_tech_step(step_container)
        elif self._step == 4:
            self._render_review_step(step_container)

    def _render_feature_step(self, container: Vertical) -> None:
        container.mount(Label("Describe the feature you're building:"))
        container.mount(
            TextArea(
                self._feature_overview,
                id="feature-overview",
            )
        )

    def _render_branch_step(self, container: Vertical) -> None:
        container.mount(Label("Git branch name for this work:"))
        container.mount(Input(
            value=self._branch_name,
            id="branch-name",
            placeholder="ralph/my-feature",
        ))

    def _render_stories_step(self, container: Vertical) -> None:
        container.mount(Label("User stories (one per section):"))
        container.mount(Static("Add stories with a title and acceptance criteria."))
        container.mount(Static("Criteria: one per line, should be testable."))
        container.mount(Static(""))

        if not self._stories:
            self._stories.append({"title": "", "criteria": ""})

        for i, story in enumerate(self._stories):
            container.mount(Label(f"Story {i + 1}:"))
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

    def _render_tech_step(self, container: Vertical) -> None:
        container.mount(Label("Tech stack (language, framework, testing tools):"))
        container.mount(
            TextArea(self._tech_stack, id="tech-stack")
        )
        container.mount(Label("Verification commands (typecheck, test, lint):"))
        container.mount(
            TextArea(self._verification_commands, id="verification-commands")
        )

    def _render_review_step(self, container: Vertical) -> None:
        prd = self._build_prd()
        preview = json.dumps(prd.to_dict(), indent=2)
        container.mount(Label("Review your PRD:"))
        container.mount(
            TextArea(preview, id="prd-preview", read_only=True)
        )
        container.mount(Static(
            f"Branch: {prd.branch_name}  |  "
            f"Stories: {prd.total_stories}"
        ))

    def _save_current_step(self) -> None:
        """Save form data from the current step before navigating."""
        if self._step == 0:
            try:
                ta = self.query_one("#feature-overview", TextArea)
                self._feature_overview = ta.text
            except Exception:
                pass
        elif self._step == 1:
            try:
                inp = self.query_one("#branch-name", Input)
                self._branch_name = inp.value
            except Exception:
                pass
        elif self._step == 2:
            for i in range(len(self._stories)):
                try:
                    title_inp = self.query_one(f"#story-title-{i}", Input)
                    criteria_ta = self.query_one(f"#story-criteria-{i}", TextArea)
                    self._stories[i]["title"] = title_inp.value
                    self._stories[i]["criteria"] = criteria_ta.text
                except Exception:
                    pass
        elif self._step == 3:
            try:
                self._tech_stack = self.query_one("#tech-stack", TextArea).text
                ta = self.query_one("#verification-commands", TextArea)
                self._verification_commands = ta.text
            except Exception:
                pass

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
            # Add typecheck/test criteria from verification commands if present
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "wizard-next":
            self._save_current_step()
            if self._step == 4:
                self._save_prd()
            else:
                self._step += 1
                self._render_step()
        elif event.button.id == "wizard-back":
            self._save_current_step()
            if self._step > 0:
                self._step -= 1
                self._render_step()
        elif event.button.id == "add-story":
            self._save_current_step()
            self._stories.append({"title": "", "criteria": ""})
            self._render_step()

    def _save_prd(self) -> None:
        prd = self._build_prd()
        output_path = Path.cwd() / "scripts" / "ralph" / "prd.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        save_prd(prd, output_path)

        step_container = self.query_one("#wizard-step", Vertical)
        step_container.remove_children()
        step_container.mount(Static(f"[green]PRD saved to {output_path}[/green]"))
        step_container.mount(Static(
            f"Branch: {prd.branch_name}  |  Stories: {prd.total_stories}"
        ))
        step_container.mount(Static("\nYou can now run: ralph run"))
        self.query_one("#wizard-next", Button).disabled = True

    def action_back(self) -> None:
        self.app.pop_screen()
