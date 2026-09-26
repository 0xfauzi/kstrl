"""Evidence panel: where this component's artifacts live (PR E).

Joins the temporal state (PR URL/state from events) with the
authoritative manifest snapshot (branch, evidence worktree, debug dir)
- the TUI is a view; when a run breaks, these paths are where the
operator goes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from kstrl.manifest import Component
    from kstrl.reducer import ComponentState


def render_evidence(
    comp: ComponentState,
    manifest_comp: Component | None,
    *,
    show_error: bool = True,
) -> Text:
    """Where the artifacts live; the error only when no failed gate shows it.

    #433 G8: a failed gate already says why above, so repeating the
    component's error here, pinned over the footer, said it twice.
    """
    text = Text()

    def row(label: str, value: str, style: str = "") -> None:
        text.append(f"{label:>10}  ", style="dim")
        text.append(value, style=style)
        text.append("\n")

    if manifest_comp is not None and manifest_comp.branch_name:
        row("branch", manifest_comp.branch_name)
    pr_url = comp.pr_url or (manifest_comp.pr_url if manifest_comp else "")
    if pr_url:
        note = f" ({comp.pr_state})" if comp.pr_state else ""
        row("pr", f"{pr_url}{note}", "bold")
    if manifest_comp is not None:
        if manifest_comp.evidence_worktree:
            row("worktree", manifest_comp.evidence_worktree)
        if manifest_comp.evidence_debug_dir:
            row("raw dumps", manifest_comp.evidence_debug_dir)
    if comp.error and show_error:
        row("error", comp.error, "red")
    if not text.plain:
        text.append("no evidence recorded", style="dim")
    return text


class EvidencePanel(Static):
    def update_state(
        self,
        comp: ComponentState,
        manifest_comp: Component | None,
        *,
        show_error: bool = True,
    ) -> None:
        self.update(render_evidence(comp, manifest_comp, show_error=show_error))
