"""Minimal Textual tailer app for the TUI spike.

Tails the newest run under <root>/.kstrl/runs/, folds events with a
30-line inline reducer, renders a component DataTable + transcript
RichLog + a latency/event-count header.

Flags exercise the risky mechanisms the real TUI will rely on:
  --chatter          background thread printing to stdout/stderr + logging
                     (must be captured by Textual, never corrupt the screen)
  --subproc-chatter  spawn a subprocess inheriting fd 2 for ~2s (simulates
                     notify hooks; EXPECTED to corrupt - documents why they
                     must be redirected in embedded mode)
  --prompt-demo      background thread opens a modal via call_from_thread
                     every 3s and blocks on the answer (the PR F bridge
                     mechanism, measured round-trip)
  --crash-after N    raise inside the app after N seconds (terminal restore)

Run: uv run --with 'textual>=3,<6' python spike/tui0/app.py --root DIR
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).parent
sys.path.insert(0, str(SPIKE_DIR))

from tailer import RunTailer, TextTailer  # noqa: E402

from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Vertical  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import Button, DataTable, Footer, Label, RichLog, Static  # noqa: E402


def newest_run_dir(root: Path) -> Path:
    runs = sorted((root / ".kstrl" / "runs").iterdir())
    if not runs:
        raise SystemExit(f"no runs under {root}/.kstrl/runs")
    return runs[-1]


class PromptModal(ModalScreen[int]):
    DEFAULT_CSS = """
    PromptModal { align: center middle; }
    PromptModal > Vertical { width: 60; height: auto; border: thick $accent;
                             background: $surface; padding: 1 2; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.question)
            yield Button("Approve", id="approve", variant="success")
            yield Button("Reject", id="reject", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(0 if event.button.id == "approve" else 1)


class SpikeApp(App[int]):
    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        # Raw mode delivers Ctrl-C as a KEY, not SIGINT (measured in the
        # pty test) - it must be bound explicitly or it does nothing.
        Binding("ctrl+c", "quit_app", show=False),
    ]
    CSS = """
    #header { height: 3; background: $panel; padding: 0 1; }
    #table { height: 1fr; }
    #transcript { height: 12; border-top: solid $accent; }
    """

    def __init__(self, root: Path, poll: float, chatter: bool, subproc_chatter: bool,
                 prompt_demo: bool, crash_after: float) -> None:
        super().__init__()
        self.run_dir = newest_run_dir(root)
        self.tailer = RunTailer(self.run_dir)
        self.transcript_tailers: dict[str, TextTailer] = {}
        self.poll_interval = poll
        self.chatter = chatter
        self.subproc_chatter = subproc_chatter
        self.prompt_demo = prompt_demo
        self.crash_after = crash_after
        self.started = time.monotonic()
        self.event_count = 0
        self.latencies: list[float] = []
        self.components: dict[str, dict] = {}
        self.prompt_roundtrips: list[float] = []
        self._stop_threads = threading.Event()

    def compose(self) -> ComposeResult:
        yield Static("starting...", id="header")
        yield DataTable(id="table", cursor_type="row")
        yield RichLog(id="transcript", max_lines=1000, wrap=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("component", "phase", "iter", "events", "last age")
        self.set_interval(self.poll_interval, self._poll)
        self.set_interval(1.0, self._tick_header)
        if self.chatter:
            threading.Thread(target=self._chatter_thread, daemon=True).start()
        if self.subproc_chatter:
            self.set_timer(3.0, self._spawn_subproc_chatter)
        if self.prompt_demo:
            threading.Thread(target=self._prompt_thread, daemon=True).start()
        if self.crash_after:
            self.set_timer(self.crash_after, self._crash)

    def _crash(self) -> None:
        raise RuntimeError("spike-induced crash (--crash-after)")

    def _chatter_thread(self) -> None:
        logging.basicConfig(level=logging.INFO)
        log = logging.getLogger("spike.chatter")
        i = 0
        while not self._stop_threads.is_set():
            print(f"stray stdout line {i}")
            print(f"stray stderr line {i}", file=sys.stderr)
            log.info("stray logging line %d", i)
            i += 1
            time.sleep(0.1)

    def _spawn_subproc_chatter(self) -> None:
        # Inherits our fds like a notify hook would: expected to corrupt.
        subprocess.Popen(["sh", "-c", "for i in 1 2 3 4 5; do echo SUBPROC-$i >&2; sleep 0.3; done"])

    def _prompt_thread(self) -> None:
        while not self._stop_threads.is_set():
            time.sleep(3.0)
            t0 = time.monotonic()
            done = threading.Event()
            result: list[int | None] = [None]

            def _open() -> None:
                def _cb(choice: int | None) -> None:
                    result[0] = choice
                    done.set()
                self.push_screen(PromptModal("Approve component (spike)?"), _cb)

            try:
                self.call_from_thread(_open)
            except Exception:
                return
            # auto-answer after 0.5s so the soak needs no human
            time.sleep(0.5)
            try:
                self.call_from_thread(self._auto_answer)
            except Exception:
                return
            done.wait(timeout=5.0)
            self.prompt_roundtrips.append(time.monotonic() - t0)

    def _auto_answer(self) -> None:
        if isinstance(self.screen, PromptModal):
            self.screen.dismiss(0)

    def _poll(self) -> None:
        records = self.tailer.poll_events()
        now = time.time()
        if not records:
            return
        for rec in records:
            self.event_count += 1
            t_emit = rec.get("t_emit")
            if isinstance(t_emit, (int, float)):
                self.latencies.append(now - t_emit)
            comp = rec.get("component") or ""
            if comp:
                st = self.components.setdefault(
                    comp, {"phase": "", "iter": 0, "events": 0, "last_ts": 0.0})
                st["events"] += 1
                st["last_ts"] = rec.get("ts", now)
                ev = rec.get("event")
                data = rec.get("data", {})
                if ev == "phase_started":
                    st["phase"] = data.get("phase", "")
                elif ev == "iteration_started":
                    st["iter"] = data.get("iteration", st["iter"])
                elif ev == "component_completed":
                    st["phase"] = "done"
                elif ev == "component_failed":
                    st["phase"] = "FAILED"
        self._refresh_table()
        self._refresh_transcript()

    def _refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        now = time.time()
        table.clear()
        for cid in sorted(self.components):
            st = self.components[cid]
            age = f"{now - st['last_ts']:.0f}s" if st["last_ts"] else "-"
            table.add_row(cid, st["phase"], str(st["iter"]), str(st["events"]), age)

    def _refresh_transcript(self) -> None:
        log = self.query_one("#transcript", RichLog)
        comp_root = self.run_dir / "components"
        if not comp_root.is_dir():
            return
        for d in comp_root.iterdir():
            t = self.transcript_tailers.setdefault(
                d.name, TextTailer(d / "engineer.log"))
            for line in t.poll():
                log.write(f"[{d.name}] {line}")

    def _tick_header(self) -> None:
        hdr = self.query_one("#header", Static)
        lat = sorted(self.latencies[-500:])
        p50 = f"{lat[len(lat) // 2] * 1000:.0f}ms" if lat else "-"
        p95 = f"{lat[min(len(lat) - 1, int(0.95 * len(lat)))] * 1000:.0f}ms" if lat else "-"
        rt = ""
        if self.prompt_roundtrips:
            rt = f"  prompt-rt max {max(self.prompt_roundtrips) * 1000:.0f}ms"
        hdr.update(
            f"spike  run={self.run_dir.name}  events={self.event_count}  "
            f"tail p50={p50} p95={p95}  up={time.monotonic() - self.started:.0f}s{rt}"
        )

    def action_quit_app(self) -> None:
        self._stop_threads.set()
        self.exit(0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--poll", type=float, default=0.2)
    p.add_argument("--chatter", action="store_true")
    p.add_argument("--subproc-chatter", action="store_true")
    p.add_argument("--prompt-demo", action="store_true")
    p.add_argument("--crash-after", type=float, default=0.0)
    p.add_argument("--no-transcript", action="store_true",
                   help="disable transcript pane rendering (isolation test)")
    a = p.parse_args()
    app = SpikeApp(a.root, a.poll, a.chatter, a.subproc_chatter,
                   a.prompt_demo, a.crash_after)
    if a.no_transcript:
        app._refresh_transcript = lambda: None  # type: ignore[method-assign]
    try:
        code = app.run() or 0
    finally:
        # Belt-and-braces terminal restore (the PR F fallback will do this too)
        sys.stdout.write("\x1b[?1049l\x1b[?25h\x1b[0m")
        sys.stdout.flush()
    sys.exit(code)


if __name__ == "__main__":
    main()
