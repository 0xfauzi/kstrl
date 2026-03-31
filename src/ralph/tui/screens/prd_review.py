"""PRD review screen - review, edit, and launch execution.

Shown after the interactive conversation generates a PRD. The user
can review each story, edit the raw JSON, then launch the feature loop.
"""

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
    Label,
    Static,
    TextArea,
)

from ralph.prd import PRD, save_prd, validate_prd


class PRDReviewScreen(Screen):
    """Review a generated PRD, optionally edit, then launch the feature loop."""

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(
        self,
        prd: PRD,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self._prd = prd
        self._editing = False
        self._error = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center(classes="wizard-outer"):
            with Vertical(id="new-project-container"):
                yield Static("Review PRD", classes="title")
                yield VerticalScroll(id="prd-review-body")
                with Horizontal(id="new-project-nav"):
                    yield Button("Back", id="review-back", variant="default")
                    yield Button(
                        "Edit JSON", id="review-edit", variant="default",
                    )
                    yield Button(
                        "Run Feature Loop",
                        id="review-run",
                        variant="primary",
                    )
        yield Footer()

    def on_mount(self) -> None:
        self._render_review()

    def _render_review(self) -> None:
        self.run_worker(self._render_review_async(), exclusive=True)

    async def _render_review_async(self) -> None:
        body = self.query_one("#prd-review-body", VerticalScroll)
        await body.remove_children()

        if self._editing:
            self._render_edit_mode(body)
        else:
            self._render_view_mode(body)

    def _render_view_mode(self, body: VerticalScroll) -> None:
        prd = self._prd

        body.mount(Static(
            f"  Branch:  [bold]{prd.branch_name}[/bold]\n"
            f"  Stories: [bold]{prd.total_stories}[/bold]"
        ))
        body.mount(Static(""))

        for story in sorted(prd.user_stories, key=lambda s: s.priority):
            body.mount(Label(
                f"[bold]{story.id}[/bold]  {story.title}"
            ))
            for criterion in story.acceptance_criteria:
                body.mount(Static(f"    - {criterion}"))
            body.mount(Static(""))

        if self._error:
            body.mount(Static(f"[red]{self._error}[/red]"))

    def _render_edit_mode(self, body: VerticalScroll) -> None:
        preview = json.dumps(self._prd.to_dict(), indent=2)
        body.mount(Label("Edit the PRD JSON below"))
        body.mount(
            Static(
                "[dim]Modify stories, criteria, or branch name. "
                "Click 'Apply' to validate and update.[/dim]",
                classes="help-text",
            )
        )
        body.mount(
            TextArea(preview, id="prd-edit-json")
        )
        body.mount(
            Button("Apply Changes", id="review-apply", variant="primary")
        )
        if self._error:
            body.mount(Static(f"[red]{self._error}[/red]"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "review-back":
            self.app.pop_screen()
        elif event.button.id == "review-edit":
            self._editing = not self._editing
            edit_btn = self.query_one("#review-edit", Button)
            edit_btn.label = "View Summary" if self._editing else "Edit JSON"
            self._error = ""
            self._render_review()
        elif event.button.id == "review-apply":
            self._apply_edits()
        elif event.button.id == "review-run":
            self._save_and_run()

    def _apply_edits(self) -> None:
        """Parse edited JSON and update the PRD."""
        try:
            raw = self.query_one("#prd-edit-json", TextArea).text
        except Exception:
            self._error = "Could not read the editor"
            self._render_review()
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._error = f"Invalid JSON: {e}"
            self._render_review()
            return

        errors = validate_prd(data)
        if errors:
            self._error = "Validation errors:\n" + "\n".join(
                f"  - {e}" for e in errors
            )
            self._render_review()
            return

        self._prd = PRD.from_dict(data)
        self._error = ""
        self._editing = False
        edit_btn = self.query_one("#review-edit", Button)
        edit_btn.label = "Edit JSON"
        self._render_review()

    def _save_and_run(self) -> None:
        """Save PRD to disk and launch the feature loop."""
        output_path = Path.cwd() / "scripts" / "ralph" / "prd.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        save_prd(self._prd, output_path)

        from ralph.tui.screens.run_dashboard import RunDashboardScreen

        # Pop review and conversation screens, push dashboard
        self.app.pop_screen()  # review
        self.app.pop_screen()  # conversation

        self.app.push_screen(
            RunDashboardScreen(understand_mode=False)
        )

    def action_back(self) -> None:
        self.app.pop_screen()
