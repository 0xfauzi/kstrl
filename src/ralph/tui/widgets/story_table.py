"""Story status table widget for displaying PRD story progress."""

from __future__ import annotations

from textual.widgets import DataTable

from ralph.prd import PRD


class StoryTableWidget(DataTable):
    """DataTable showing user story status with pass/fail indicators."""

    DEFAULT_CSS = """
    StoryTableWidget {
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.add_column("ID", key="id", width=8)
        self.add_column("Title", key="title")
        self.add_column("P", key="priority", width=3)
        self.add_column("Status", key="status", width=6)
        self.cursor_type = "row"
        self.zebra_stripes = True

    def update_stories(self, prd: PRD | None, current_story_id: str = "") -> None:
        """Refresh the table with current PRD data."""
        self.clear()
        if prd is None:
            return

        for story in sorted(prd.user_stories, key=lambda s: s.priority):
            status = "[green]PASS[/green]" if story.passes else "[red]FAIL[/red]"
            # Highlight current story
            story_id = story.id
            if story.id == current_story_id:
                story_id = f"[bold]{story.id}[/bold] <-"

            self.add_row(
                story_id,
                story.title[:30],
                str(story.priority),
                status,
                key=story.id,
            )
