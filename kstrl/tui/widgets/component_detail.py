"""What the component detail screen says above its tables (#433).

- ``render_component_header``: the component's state and, while it is
  moving, its phase, iteration and how long ago it last wrote an event
  (Q1, Q6); once it has stopped, how long it took.
- ``render_failure_detail``: every failed phase with its cause, and for
  the most recent one the path of the gate's stored output and the last
  lines of it (F7). The timeline said ``verify ✗ 1s`` and nothing else,
  although the pipeline has written the failing gate's output to disk
  since #462 and names the file in ``verification_result.gate_logs``.

The gate log is read from the path the event names, bounded to its last
``_EXCERPT_BYTES``, and cached by the file's size and mtime so a poll
does not re-read an unchanged file.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.text import Text

from kstrl.tui import theme
from kstrl.tui.run_status import age_phrase, component_took

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState
    from kstrl.tui.agent_health import AgentHealth

#: The most output lines shown under a failed gate.
EXCERPT_LINES = 8
_EXCERPT_BYTES = 16384
_MOVING = ("running", "verifying")

_excerpts: dict[str, tuple[tuple[int, int], list[str]]] = {}


def read_tail(path: str, max_bytes: int) -> tuple[str, int]:
    """The last ``max_bytes`` of a gate log, decoded, and the file's size.

    The one reader of a stored gate log (#462): the excerpt here and the
    whole-output screen (``screens.gate_log``) both read through it.
    Raises ``OSError``; undecodable bytes are replaced, never raised.
    """
    size = os.stat(path).st_size
    with open(path, "rb") as handle:
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace"), size


def gate_log_excerpt(path: str, lines: int = EXCERPT_LINES) -> list[str] | None:
    """The last ``lines`` non-blank lines of a gate log; None if unreadable."""
    try:
        stat = os.stat(path)
        key = (stat.st_size, stat.st_mtime_ns)
        cached = _excerpts.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        tail, _ = read_tail(path, _EXCERPT_BYTES)
    except OSError:
        return None
    kept = [line.rstrip() for line in tail.splitlines() if line.strip()][-lines:]
    _excerpts[path] = (key, kept)
    return kept


def _moving_detail(comp: ComponentState, now: float, health: AgentHealth | None) -> list[str]:
    parts = [comp.phase] if comp.phase else []
    if comp.iteration:
        limit = f"/{comp.max_iterations}" if comp.max_iterations else ""
        parts.append(f"iteration {comp.iteration}{limit}")
    parts.append(f"attempt {max(comp.attempt, 1)}")
    if comp.last_event_ts:
        parts.append(f"last event {age_phrase(now - comp.last_event_ts)} ago")
    if health is not None:
        # Q6 (#433 M2): the agent's own output and its process, apart
        # from the event age above.
        parts.append(health.text(threshold=True))
    return parts


def _stopped_detail(comp: ComponentState) -> list[str]:
    parts = [f"attempt {comp.attempt}"] if comp.attempt > 1 else []
    if comp.carried and comp.status != "pending":
        parts.append("carried from an earlier run; not run in this one")
    elif comp.started_ts and comp.last_event_ts:
        parts.append(f"took {age_phrase(component_took(comp))}")
    return parts


def render_component_header(
    comp: ComponentState,
    now: float | None = None,
    health: AgentHealth | None = None,
) -> Text:
    clock = time.time() if now is None else now
    glyph, color = theme.status_glyph(comp.status)
    header = Text()
    header.append(f" {comp.component_id} ", style=f"bold {theme.BACKGROUND} on {theme.ACCENT}")
    if comp.title:
        header.append(f"  {comp.title}", style="bold")
    header.append(f"  {glyph} {comp.status}", style=f"bold {color}")
    moving = comp.status in _MOVING
    parts = _moving_detail(comp, clock, health) if moving else _stopped_detail(comp)
    if parts:
        header.append("  " + " · ".join(parts), style=theme.MUTED)
    return header


def _cause(entry: dict[str, Any]) -> str:
    failures = entry.get("failures") or []
    if failures:
        return "; ".join(str(item) for item in failures)
    return str(entry.get("detail") or "no cause recorded")


def shown_path(path: str, root_dir: Path | None) -> str:
    """``path`` relative to the project when it is inside it."""
    if root_dir is None:
        return path
    try:
        return str(Path(path).resolve().relative_to(root_dir.resolve()))
    except (OSError, ValueError):
        return path


def _indented(text: Text, cells: int) -> Padding:
    """``text`` indented, wrapped lines included (a hanging indent)."""
    return Padding(text, (0, 0, 0, cells))


def _gate_output(entry: dict[str, Any], root_dir: Path | None) -> list[RenderableType]:
    parts: list[RenderableType] = []
    for path in entry.get("gate_logs") or []:
        parts.append(_indented(Text("output  o opens it whole", style=theme.MUTED), 2))
        parts.append(_indented(Text(shown_path(str(path), root_dir)), 4))
        excerpt = gate_log_excerpt(str(path))
        if excerpt is None:
            parts.append(_indented(Text("(file not readable)", style=theme.WARNING), 4))
            continue
        parts.extend(_indented(Text(line, style=theme.MUTED), 4) for line in excerpt)
    return parts


def _failure_line(entry: dict[str, Any]) -> Text:
    text = Text()
    text.append("✗ ", style=f"bold {theme.ERROR}")
    text.append(f"{entry.get('phase', '?')} failed", style=f"bold {theme.ERROR}")
    text.append(f" (attempt {entry.get('attempt', 1)})", style=theme.MUTED)
    text.append(f"  {_cause(entry)}")
    return text


def render_failure_detail(
    comp: ComponentState, root_dir: Path | None = None, route: RenderableType | None = None
) -> Group | None:
    """Each failed phase and its cause, newest first; output only for the newest.

    The output path and lines keep their indent when they wrap, so the
    excerpt does not run back under the failure line it belongs to.
    ``route`` goes right under the newest failure, above its output, so a
    long excerpt cannot push it out of the pane (#433 H12).
    """
    failed = [entry for entry in comp.phase_history if not entry.get("passed")]
    if not failed:
        return None
    newest, *older = reversed(failed)
    parts: list[RenderableType] = [
        _failure_line(newest),
        *([route] if route is not None else []),
        *_gate_output(newest, root_dir),
    ]
    parts.extend(_failure_line(entry) for entry in older)
    return Group(*parts)


def newest_gate_log(comp: ComponentState) -> str:
    """The stored output of the newest failed phase, "" when it kept none."""
    failed = [entry for entry in comp.phase_history if not entry.get("passed")]
    logs = failed[-1].get("gate_logs") if failed else None
    return str(logs[0]) if logs else ""


def transcript_path(run_dir: Path | None, component_id: str) -> Path | None:
    if run_dir is None:
        return None
    return run_dir / "components" / component_id / "engineer.log"


def transcript_written(path: Path | None) -> bool:
    try:
        return path is not None and path.stat().st_size > 0
    except OSError:
        return False
