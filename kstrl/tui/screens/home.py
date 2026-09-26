"""Home screen: an operator queue over the project (D1, #433 increment 2).

The `ks` no-args landing surface. Below the masthead and the context
line, four sections in the order an operator needs them
(``operator_queue``):

- **needs you**: open inbox decisions and current failures a retry can
  act on. Enter opens the inbox or the failure queue.
- **active**: runs whose writer is alive, with the running agent's last
  output and process, and ``ks serve`` items in flight or queued.
- **delivery**: the newest finished factory run's integration review,
  and each merge commit's CI state from the ``ks ci poll`` ledger.
- **history**: the run browser. A failed run says whether a later run
  superseded it or it is still current.

Everything renders from disk discovery (tui.runs) and the project's own
files, read on a worker thread; opening a run delegates to the app's
open_run, which builds an observe context and pushes the kind-appropriate
stack. Returning here (escape/q) tears that context down via
on_screen_resume - no matter which path popped. The launcher column
shows only on a wide terminal; elsewhere the digit keys and ^p reach the
same commands.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, OptionList, Static
from textual.widgets.option_list import Option

from kstrl.config import resolve_config_file
from kstrl.safemode import SafeModeReason
from kstrl.tui import theme
from kstrl.tui.home_data import (
    HomeStats,
    RunSummary,
    SummaryCache,
    gather_home,
)
from kstrl.tui.home_view import (
    COMMAND_KEYS,
    NARROW_BELOW,
    SECTION_ROWS,
    active_cells,
    active_empty,
    attention_line,
    command_strip,
    delivery_text,
    fit_rows,
    history_note,
    needs_cells,
    preview_status,
    section_title,
    serve_phrase,
)
from kstrl.tui.messages import SummariesReady
from kstrl.tui.operator_queue import DECISION, OperatorQueue
from kstrl.tui.run_status import RUN_STATE_STYLE, state_word
from kstrl.tui.runs import RunRef, discover_runs
from kstrl.tui.widgets.component_table import ComponentTable
from kstrl.tui.widgets.cost_meter import at_least, cost_against_cap, format_tokens
from kstrl.tui.widgets.run_table import RunTable
from kstrl.tui.widgets.safe_mode_chip import SafeModeChip

HOME_POLL_INTERVAL = 2.0
HOME_RUN_LIMIT = 15
#: Below this height the selected run's component board is left out.
PREVIEW_BOARD_MIN_HEIGHT = 45
#: Below this width a live run's agent health drops its words ("21s · alive").
ACTIVE_SHORT_BELOW = 100


@dataclass(frozen=True)
class HomeCommand:
    command_id: str
    title: str
    description: str


# The launcher's register: digit hotkey, name, a SHORT consequence.
# Long help prose belongs in --help; a launcher names destinations.
HOME_COMMANDS: list[HomeCommand] = [
    HomeCommand("factory", "factory", "run the manifest"),
    HomeCommand("decompose", "decompose", "spec into components"),
    HomeCommand("retry", "retry", "rerun a failed component"),
    HomeCommand("dash", "dashboard", "open the newest run"),
    HomeCommand("config", "config", "resolved values + sources"),
    HomeCommand("inbox", "inbox", "decisions awaiting you"),
    HomeCommand("evolve", "evolve", "failure patterns and trends"),
    HomeCommand("init", "init", "scaffold a project"),
    HomeCommand("feature", "feature", "shell: ks feature --tui"),
    HomeCommand("understand", "understand", "shell: ks understand --tui"),
]


def _git_branch(root_dir: Path) -> str:
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root_dir,
            capture_output=True,
            encoding="utf-8",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _project_name(root_dir: Path) -> str:
    """The project is the directory the shell was opened on (#433 F3).

    The newest manifest's ``projectName`` names a RUN, not the project: a
    daemon names each run it starts ``queue-<id>``, and the home header
    showed that queue name as the project.
    """
    return root_dir.resolve().name or str(root_dir)


def _masthead(root_dir: Path, branch: str, project: str) -> Text:
    text = Text()
    text.append(" ◍ kstrl ", style=f"bold {theme.BACKGROUND} on {theme.ACCENT}")
    text.append("  ")
    text.append(project or "(no project)", style="bold")
    if branch:
        text.append(f"  {branch}", style=theme.STEEL)
    toml_path = resolve_config_file(root_dir)
    if toml_path.exists():
        text.append(f"  {toml_path.name} ✓", style=theme.MUTED)
    else:
        text.append("  no kstrl.toml - run ks init", style=theme.WARNING)
    return text


def _stats_line(stats: HomeStats, width: int = 120) -> Text:
    narrow = width < 100
    text = _last_run(stats, narrow)
    serve = serve_phrase(stats.queue.serve if stats.queue is not None else None, pid=not narrow)
    if serve.cell_len:
        text.append(" · ", style=theme.MUTED)
        text.append_text(serve)
    return text


def _last_run(stats: HomeStats, narrow: bool = False) -> Text:
    text = Text(" ")
    last = stats.last
    if last is None:
        text.append("no finished runs yet", style=theme.MUTED)
    else:
        # The same four words as the run table (#433 F4).
        glyph, color = RUN_STATE_STYLE[state_word(last.outcome)]
        if not narrow:
            # At 80 columns the words give way so the spend, its cap and
            # ks serve's state fit beside the safe-mode chip (#433 G9).
            text.append("last run ", style=theme.MUTED)
        text.append(f"{glyph} {state_word(last.outcome)}", style=f"bold {color}")
        text.append(
            f" {last.components_done}/{last.components_total}",
            style="bold",
        )
        if last.total_tokens and not narrow:
            text.append(" · ", style=theme.MUTED)
            text.append(
                f"{at_least(format_tokens(last.total_tokens), last.tokens_lower_bound)} tok"
            )
        if last.cost_usd:
            text.append(" · ", style=theme.MUTED)
            text.append_text(
                cost_against_cap(last.cost_usd, last.max_cost_usd, last.cost_lower_bound)
            )
    return text


class HomeScreen(Screen[None]):
    # History until the queue is read; then _place_focus decides once.
    AUTO_FOCUS = "#home-runs"
    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
        # One key per command, and every key is one keypress: the tenth
        # command used to be bound to "10", which no terminal sends (#433 F2).
        *[Binding(COMMAND_KEYS[n], f"command({n})", show=False) for n in range(len(HOME_COMMANDS))],
    ]

    def __init__(self) -> None:
        super().__init__()
        self._refs: dict[str, RunRef] = {}
        self._summaries: dict[str, RunSummary] = {}
        self._cache = SummaryCache()
        self._summarizing = False
        self._preview_run_id = ""
        self._queue: OperatorQueue | None = None
        self._stats: HomeStats | None = None
        self._focus_placed = False
        #: The identity of each active row on screen, in row order; the
        #: row keys are positions.
        self._active_shown: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="home-header"):
            yield Static(id="home-masthead")
            with Horizontal(id="home-status-row"):
                yield SafeModeChip(id="safe-mode-chip")
                yield Static(id="home-stats")
        yield Static(id="home-attention", classes="home-section-title")
        yield DataTable(id="home-needs", classes="home-section")
        yield Static(id="home-active-title", classes="home-section-title")
        yield DataTable(id="home-active", classes="home-section")
        yield Static(id="home-delivery")
        with Horizontal(id="home-columns"):
            with Vertical(id="home-runs-col"):
                yield Static("history", id="home-runs-title")
                yield RunTable(id="home-runs")
            with Vertical(id="home-commands-col"):
                yield Static("commands", id="home-commands-title")
                yield OptionList(id="home-commands")
        yield Static(id="home-preview-meta")
        yield Static("run preview", id="home-preview-title")
        yield ComponentTable(id="home-preview")
        yield Static(id="home-keys")
        yield Footer()

    @property
    def ready(self) -> bool:
        return next(iter(self.query(RunTable)), None) is not None

    def update_safe_mode(
        self,
        reasons: list[SafeModeReason] | None,
    ) -> None:
        """Duck-typed contract the app calls; ignored while unmounted."""
        chip = next(iter(self.query(SafeModeChip)), None)
        if chip is not None:
            chip.update_reasons(reasons)

    def on_mount(self) -> None:
        self.update_safe_mode(getattr(self.app, "_safe_mode_reasons", None))
        root_dir = self._root_dir()
        self.query_one("#home-masthead", Static).update(
            _masthead(
                root_dir,
                _git_branch(root_dir),
                _project_name(root_dir),
            )
        )
        for table_id in ("#home-needs", "#home-active"):
            table: DataTable[Text | str] = self.query_one(table_id, DataTable)
            table.cursor_type = "row"
            table.show_header = False
            table.display = False
        self._render_queue()
        commands = self.query_one(OptionList)
        for index, command in enumerate(HOME_COMMANDS):
            label = Text()
            label.append(f" {COMMAND_KEYS[index]} ", style=f"bold {theme.ACCENT}")
            label.append(f"{command.title:<11}", style="bold")
            label.append(command.description, style=theme.MUTED)
            commands.add_option(Option(label, id=command.command_id))
        self.refresh_runs()
        self.set_interval(HOME_POLL_INTERVAL, self.refresh_runs)

    def _root_dir(self) -> Path:
        return getattr(self.app, "root_dir", Path.cwd())

    def refresh_runs(self) -> None:
        if not self.ready:
            return
        root_dir = self._root_dir()
        refs = discover_runs(root_dir)[:HOME_RUN_LIMIT]
        self._refs = {ref.run_id: ref for ref in refs}
        self.query_one(RunTable).update_runs(refs, self._summaries, self._history_notes())
        title = Text("history", style=f"bold {theme.MUTED}")
        if not refs:
            title.append("  none yet - run a command below", style=theme.MUTED)
        self.query_one("#home-runs-title", Static).update(title)
        if not self._summarizing:
            # Folding every listed run is file IO + reducer work: off
            # the UI thread, with "·" cells until the message lands.
            self._summarizing = True
            self.run_worker(
                lambda: self._compute_summaries(list(refs)),
                thread=True,
            )

    def _compute_summaries(self, refs: list[RunRef]) -> None:
        # Every file and process read home makes happens here, on the
        # worker thread: the run folds, the inbox, the manifest, the serve
        # queue, the integration files, and each running agent's output
        # time and pid probe.
        try:
            summaries = self._cache.refresh(refs)
            stats = gather_home(summaries, refs, self._cache, self._root_dir(), time.time())
        except Exception:  # noqa: BLE001 - a broken run dir must not kill home
            summaries, stats = {}, HomeStats(None)
        self.post_message(SummariesReady(summaries, stats))

    def on_summaries_ready(self, message: SummariesReady) -> None:
        self._summarizing = False
        self._summaries = message.summaries
        self._queue = message.stats.queue
        if self.ready:
            self.query_one(RunTable).update_runs(
                list(self._refs.values()),
                self._summaries,
                self._history_notes(),
            )
            self._stats = message.stats
            self.query_one("#home-stats", Static).update(
                _stats_line(message.stats, self.size.width),
            )
            self.query_one("#home-attention", Static).update(attention_line(message.stats))
            self._render_queue()
            self._render_preview()
            self._place_focus()

    # -- the operator queue (#433 increment 2) -----------------------------

    def _place_focus(self) -> None:
        """Once, on the first queue read: the needs-you rows when there
        are any, otherwise history. Active rows are a tab away; later
        reads never move the operator's focus, and neither does this one
        once the operator has moved it off the history table."""
        if self._focus_placed:
            return
        self._focus_placed = True
        history = self.query_one(RunTable)
        if self.focused not in (None, history):
            return
        needs = self.query_one("#home-needs", DataTable)
        (needs if needs.row_count else history).focus()

    def _history_notes(self) -> dict[str, str]:
        return {
            run_id: history_note(run_id, summary.state, self._queue, summary.reason)
            for run_id, summary in self._summaries.items()
        }

    def _render_queue(self) -> None:
        width = self.size.width or 120
        queue = self._queue
        self._render_needs(queue, width)
        self._render_active(queue, width)
        self.query_one("#home-delivery", Static).update(delivery_text(queue, width, time.time()))

    def _render_needs(self, queue: OperatorQueue | None, width: int) -> None:
        needs: DataTable[Text | str] = self.query_one("#home-needs", DataTable)
        # The rebuild below puts the cursor on row 0; every poll runs it,
        # so the operator's row is found again by its identity.
        needs_at = _row_at_cursor(needs, [str(key.value) for key in needs.rows])
        needs.clear(columns=True)
        rows = queue.needs_you if queue is not None else ()
        if rows:
            needs.add_columns("", "what", "action")
            shown = rows[:SECTION_ROWS]
            cells = fit_rows([needs_cells(row) for row in shown], width, flex=1)
            for row, values in zip(shown, cells, strict=True):
                needs.add_row(*values, key=f"{row.kind}:{row.key}")
            _put_cursor(needs, [f"{row.kind}:{row.key}" for row in shown], needs_at)
        needs.display = bool(rows)
        # An explicit height, not auto: a table whose rows land in the
        # frame it is shown in was laid out at height 0 at 80x24.
        needs.styles.height = min(len(rows), SECTION_ROWS)
        if len(rows) > SECTION_ROWS:
            line = self.query_one("#home-attention", Static)
            line.update(
                Text.assemble(
                    attention_line_for(queue),
                    (f"  {len(rows) - SECTION_ROWS} more in the inbox or retry", theme.MUTED),
                )
            )

    def _render_active(self, queue: OperatorQueue | None, width: int) -> None:
        active: DataTable[Text | str] = self.query_one("#home-active", DataTable)
        active_at = _row_at_cursor(active, self._active_shown)
        active.clear(columns=True)
        moving = queue.active if queue is not None else ()
        self._active_shown = [f"{item.source}:{item.label}" for item in moving[:SECTION_ROWS]]
        title = (
            active_empty(queue)
            if not moving
            else section_title("active", len(moving), min(len(moving), SECTION_ROWS))
        )
        self.query_one("#home-active-title", Static).update(title)
        if moving:
            active.add_columns("", "who", "state", "detail")
            narrow = width < ACTIVE_SHORT_BELOW
            shown_rows = [active_cells(item, narrow=narrow) for item in moving[:SECTION_ROWS]]
            cells = fit_rows(shown_rows, width, flex=3)
            for index, values in enumerate(cells):
                active.add_row(*values, key=f"active-{index}")
            _put_cursor(active, self._active_shown, active_at)
        active.display = bool(moving)
        active.styles.height = min(len(moving), SECTION_ROWS)

    def on_screen_resume(self) -> None:
        # Whatever path popped back here, the observed run is done
        # with: tear its context down and re-discover.
        close_run = getattr(self.app, "close_run", None)
        if close_run is not None:
            close_run()
        self.refresh_runs()

    def action_refresh(self) -> None:
        self.refresh_runs()

    def on_resize(self) -> None:
        # The queue sections want the width; below NARROW_BELOW the
        # launcher becomes one line of keys and ^p lists every command.
        # The preview board under history needs the height of a tall
        # terminal, and is left out below it.
        narrow = self.size.width < NARROW_BELOW
        self.set_class(narrow, "narrow")
        self.set_class(self.size.height < PREVIEW_BOARD_MIN_HEIGHT, "short")
        self.set_class(self.size.height < 30, "tiny")
        keys = self.query_one("#home-keys", Static)
        keys.update(command_strip(HOME_COMMANDS, self.size.width) if narrow else "")
        self.query_one(RunTable).update_runs(
            list(self._refs.values()), self._summaries, self._history_notes()
        )
        if self._stats is not None:
            self.query_one("#home-stats", Static).update(_stats_line(self._stats, self.size.width))
        self._render_queue()
        self._render_preview()

    def palette_commands(self) -> list[tuple[str, str, str]]:
        """(title, help, command id) per command, for the ^p palette."""
        return [
            (f"{COMMAND_KEYS[index]} {command.title}", command.description, command.command_id)
            for index, command in enumerate(HOME_COMMANDS)
        ]

    def run_command(self, command_id: str) -> None:
        self._dispatch(command_id)

    # -- run preview ---------------------------------------------------------

    def _open_ref(self, run_id: str) -> None:
        ref = self._refs.get(run_id)
        if ref is None:
            return
        open_run = getattr(self.app, "open_run", None)
        if open_run is not None:
            open_run(ref)

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        if event.data_table.id != "home-runs":
            return
        row = event.cursor_row
        keys = list(event.data_table.rows)
        if row is None or not (0 <= row < len(keys)):
            return
        self._preview_run_id = str(keys[row].value)
        self._render_preview()

    def _render_preview(self) -> None:
        if not self.ready:
            return
        run_id = self._preview_run_id or next(iter(self._refs), "")
        table = self.query_one("#home-preview", ComponentTable)
        meta = self.query_one("#home-preview-meta", Static)
        title = Text("run preview", style=f"bold {theme.MUTED}")
        if not run_id:
            table.display = False
            meta.update(
                Text(
                    "nothing to preview - launch a command to record a run",
                    style=theme.MUTED,
                )
            )
            self.query_one("#home-preview-title", Static).update(title)
            return
        ref = self._refs.get(run_id)
        title.append(f"  {ref.kind if ref else 'run'} ", style=theme.STEEL)
        title.append(theme.short_run_id(run_id), style=theme.MUTED)
        self.query_one("#home-preview-title", Static).update(title)
        state = self._cache.state_for(run_id)
        if state is None:
            table.display = False
            meta.update(Text("· folding run state...", style=theme.MUTED))
            return
        # A short terminal leaves the board out (CSS .short); an inline
        # display=True would override that rule.
        table.display = not self.has_class("short")
        if getattr(self, "_preview_shown", "") != run_id:
            # Switching runs is a rebuild, not a live diff - the
            # never-clear rule guards live updates, not navigation.
            # Columns too: their widths were sized for the other run.
            table.reset()
            self._preview_shown = run_id
        table.run_dir = ref.run_dir if ref is not None else None
        table.update_state(state)
        summary = self._summaries.get(run_id)
        line = preview_status(ref, summary, state)
        if summary is not None:
            line.append(
                f" · {summary.components_done}/{summary.components_total} components",
                style=theme.MUTED,
            )
            # No reason here: run_reason above already says it in full.
            note = history_note(run_id, summary.state, self._queue)
            if note:
                line.append(f" · {note}", style=f"bold {theme.MUTED}")
        counts = state.spec_issue_counts
        if counts:
            parts = " ".join(f"{n} {sev}" for sev, n in sorted(counts.items()))
            line.append(f" · spec issues: {parts}", style=theme.WARNING)
        if self.size.width >= 100:
            line.append("  enter opens the board", style=theme.MUTED)
        meta.update(line)

    # -- dispatch ------------------------------------------------------------

    def on_data_table_row_selected(
        self,
        event: DataTable.RowSelected,
    ) -> None:
        event.stop()
        if event.data_table.id == "home-needs":
            self._open_needs(str(event.row_key.value or ""))
            return
        if event.data_table.id == "home-active":
            self._open_active(event.cursor_row)
            return
        if event.data_table.id == "home-preview":
            self._open_ref(self._preview_run_id)
            return
        if event.row_key.value is None:
            return
        self._open_ref(str(event.row_key.value))

    def _open_needs(self, key: str) -> None:
        kind, _, item_key = key.partition(":")
        if kind == DECISION:
            from kstrl.tui.screens.inbox import InboxScreen

            self.app.push_screen(InboxScreen(select=item_key))
            return
        from kstrl.tui.screens.retry import RetryScreen

        self.app.push_screen(RetryScreen(select=item_key))

    def _open_active(self, row: int) -> None:
        queue = self._queue
        if queue is None or not (0 <= row < len(queue.active)):
            return
        item = queue.active[row]
        if item.run_id and item.run_id in self._refs:
            self._open_ref(item.run_id)
        else:
            self.app.notify(f"{item.label} is {item.state}; there is no run to open yet")

    def action_command(self, index: int) -> None:
        if 0 <= index < len(HOME_COMMANDS):
            self._dispatch(HOME_COMMANDS[index].command_id)

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        event.stop()
        if event.option_id is not None:
            self._dispatch(event.option_id)

    def _dispatch(self, command_id: str) -> None:
        if command_id == "dash":
            refs = list(self._refs.values())
            if refs:
                self._open_ref(refs[0].run_id)
            else:
                self.app.notify("no runs yet", severity="warning")
        elif command_id == "config":
            from kstrl.tui.screens.config import ConfigScreen

            self.app.push_screen(ConfigScreen())
        elif command_id == "inbox":
            from kstrl.tui.screens.inbox import InboxScreen

            self.app.push_screen(InboxScreen())
        elif command_id == "evolve":
            from kstrl.tui.screens.evolve import EvolveScreen

            self.app.push_screen(EvolveScreen())
        elif command_id == "init":
            from kstrl.tui.screens.init_wizard import InitWizardScreen

            self.app.push_screen(InitWizardScreen())
        elif command_id == "factory":
            from kstrl.tui.screens.launch import FactoryLaunchForm

            self.app.push_screen(FactoryLaunchForm())
        elif command_id == "decompose":
            from kstrl.tui.screens.launch import DecomposeLaunchForm

            self.app.push_screen(DecomposeLaunchForm())
        elif command_id == "retry":
            from kstrl.tui.screens.retry import RetryScreen

            self.app.push_screen(RetryScreen())
        elif command_id in ("feature", "understand"):
            self.app.notify(
                f"run `ks {command_id} --tui` from the CLI - its "
                "argument resolution lives there and opens the same "
                "embedded dashboard",
            )


def _row_at_cursor(table: DataTable[Text | str], identities: list[str]) -> str:
    """The identity of the row under the cursor, "" when there is none."""
    row = table.cursor_row
    return identities[row] if table.row_count and 0 <= row < len(identities) else ""


def _put_cursor(table: DataTable[Text | str], identities: list[str], wanted: str) -> None:
    """Back on the row the operator was on, when it is still listed."""
    if wanted and wanted in identities:
        table.move_cursor(row=identities.index(wanted), animate=False)


def attention_line_for(queue: OperatorQueue | None) -> Text:
    """The needs-you title for a queue (the counts ``attention_line`` reads)."""
    return attention_line(
        HomeStats(
            last=None,
            inbox_open=queue.decisions if queue is not None else None,
            failed_components=queue.failures if queue is not None else None,
        )
    )
