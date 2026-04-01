"""Chat log widget.

Minimal but with clear information hierarchy:
- User turns marked with green '>'
- PM turns use rendered markdown
- Activity is a single dim summary line
- System messages are dim
"""

from __future__ import annotations

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.widgets import RichLog

from ralph.agent import LineRole


class ChatLogWidget(RichLog):
    """Conversation log with markdown rendering."""

    def _separator(self) -> None:
        self.write(Text(""))
        self.write(Text("  -------", style="dim"))
        self.write(Text(""))

    def write_user_message(self, content: str) -> None:
        self._separator()
        lines = content.split("\n")
        first = Text()
        first.append("  > ", style="bold green")
        first.append(lines[0])
        self.write(first)
        for line in lines[1:]:
            self.write(Text(f"    {line}"))

    def write_pm_response(self, content: str) -> None:
        self.write(Text(""))
        if content.strip():
            try:
                self.write(RichMarkdown(content, code_theme="monokai"))
            except Exception:
                for line in content.split("\n"):
                    self.write(Text(f"  {line}"))

    def write_activity_summary(
        self, lines: list[tuple[str, LineRole]],
    ) -> None:
        if not lines:
            return
        seen: list[str] = []
        for raw, _ in lines:
            short = _shorten_activity(raw)
            if short and short not in seen:
                seen.append(short)
        if not seen:
            return
        summary = ", ".join(seen[-8:])
        self.write(Text(f"    {summary}", style="dim"))

    def write_system(self, content: str) -> None:
        self.write(Text(f"  {content}", style="dim"))


def _shorten_activity(line: str) -> str:
    line = line.strip()
    if not line or line in ("Agent",):
        return ""
    for prefix in ("Read ", "Glob ", "Grep "):
        if line.startswith(prefix):
            rest = line[len(prefix):]
            if "/" in rest:
                return rest.rsplit("/", 1)[-1][:40]
            return rest[:40]
    if line.startswith("$ "):
        parts = line[2:].split()
        if parts:
            return parts[0][:20]
    return line[:30]
