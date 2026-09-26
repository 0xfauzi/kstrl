"""#433 increment 4: each merge commit's CI, the retry's dropped flags, r's wait, spend against cap.

G11 of the #433 round-3 audit (advice-r2 section 2 item 1): home's
delivery section and the run overview's show, per merge commit, the CI
state ``ks ci poll`` recorded (#553), how long ago it was read, and the
reason when it is unknown. The ledger is written by kstrl's own writer:
``poll_ci`` spawning a fake ``gh`` found on PATH that answers with the
replies captured in ``tests/fixtures/ci/``, or, where a reading must be
old, ``CiReading.to_dict`` through ``append_records`` as ``poll_ci``
does. The PR #557/#561 handoffs: the retry scope names each recorded
flag it does not replay (``plan.dropped``), r waits for the carry, and
the history cost column is spend against the run's recorded cap.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from rich.text import Text
from textual.widgets import DataTable, Static

from kstrl import events as ev
from kstrl.appendio import append_records
from kstrl.ci_state import CiReading, CiState, poll_ci
from kstrl.factory import FactoryConfig
from kstrl.launch_record import FlagValue, run_limits, write_launch_record
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.statedir import CONTROL_CI_CHECKS, control_file, ensure_control_state
from kstrl.timeout import TimeoutConfig
from kstrl.tui import theme
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.delivery import NO_CI_READING
from kstrl.tui.dispatch import initial_screens_for_kind
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.screens.retry import RetryScreen
from tests.helpers.fakegh import put_gh_on_path
from tests.helpers.settle import drained, mounted, settled
from tests.test_ci_state import FAKE_GH, FIXTURES

SHIP = "factory-20260925-080000.000000-ship01"
FAILED_RUN = "factory-20260926-072240.147891-fail01"
PRICED = "factory-20260924-080000.000000-cost01"
FREE = "factory-20260924-070000.000000-free01"
SHA_PASSED = "b057de21b57bb8543db8bb0b2e1569e0a98e84e5"
SHA_FAILED = "0aa55d980241dd702f6f56d661ea0fc8bad04a8f"
SHA_UNKNOWN = "6cabef5547ce47529addcbde62e6c865e89d5aff"
SHA_UNREAD = "1" * 40
GH_REFUSAL = "HTTP 401: Bad credentials (https://api.github.com/graphql)"
LIMIT_ENV = (
    "KSTRL_FACTORY_MAX_COST_USD",
    "KSTRL_FACTORY_MAX_TOTAL_TOKENS",
    "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS",
    "KSTRL_TIMEOUT_AGENT_ITERATION",
    "KSTRL_TIMEOUT_COMPONENT",
)
SIZES = [(120, 36), (80, 24)]
DROPPED = "--verify-command, removed in #539"


def _home(root: Path) -> KstrlTuiApp:
    return KstrlTuiApp(root_dir=root, mode=Mode.HOME, poll_interval=0.05)


def _dash(root: Path, run_id: str) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=root / ".kstrl" / "runs" / run_id,
        root_dir=root,
        mode=Mode.DASH,
        poll_interval=0.05,
        screen_factory=initial_screens_for_kind("factory", observe_only=True),
    )


def _backdate(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _shipped(root: Path, shas: tuple[str, ...]) -> None:
    """A finished factory run that merged one PR per sha, PR #1 first."""
    paths = ev.RunPaths.for_run(root, SHIP)
    bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=SHIP)
    components = tuple({"id": f"c{n}", "title": f"C{n}", "deps": []} for n in range(len(shas)))
    bus.emit(ev.RunStarted(project="demo", components=len(shas)))
    bus.emit(ev.RunPlan(components=components))
    for number, sha in enumerate(shas, start=1):
        cid = f"c{number - 1}"
        bus.emit(ev.ComponentStarted(component=cid))
        bus.emit(ev.PrMerged(component=cid, pr_number=number, merge_sha=sha))
        bus.emit(ev.ComponentCompleted(component=cid, duration_seconds=1.0, iterations=1))
    bus.emit(
        ev.RunCompleted(
            completed=len(shas), failed=0, skipped=0, duration_seconds=1.0, release_ref=shas[-1]
        )
    )
    bus.close()
    _backdate(paths.events_file, 7200)


@pytest.fixture
def polled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Four merges; ``poll_ci`` read three of them through a fake gh:
    passed, failed, and one gh refused (unknown). The fourth was never read."""
    put_gh_on_path(tmp_path, monkeypatch, FAKE_GH)
    replies = tmp_path / "gh-replies"
    replies.mkdir()
    monkeypatch.setenv("FAKE_GH_DIR", str(replies))
    for sha, name in ((SHA_PASSED, "passed.json"), (SHA_FAILED, "failed.json")):
        # gh prints the array of pages under --paginate --slurp (#570).
        page = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        (replies / f"{sha}.json").write_text(json.dumps([page]), encoding="utf-8")
    (replies / f"{SHA_UNKNOWN}.fail").write_text(GH_REFUSAL, encoding="utf-8")
    root = tmp_path / "project"
    root.mkdir()
    _shipped(root, (SHA_PASSED, SHA_FAILED, SHA_UNKNOWN, SHA_UNREAD))
    states = [reading.state for reading in poll_ci(root, [SHA_PASSED, SHA_FAILED, SHA_UNKNOWN])]
    assert states == [CiState.PASSED, CiState.FAILED, CiState.UNKNOWN]
    return root


def _ledger(root: Path, readings: list[tuple[str, CiState, float]]) -> None:
    """Ledger lines as ``poll_ci`` writes them: ``CiReading.to_dict``
    through ``append_records``, each taken ``age`` seconds ago."""
    ensure_control_state(root)
    payload = ""
    for sha, state, age in readings:
        taken = datetime.fromtimestamp(time.time() - age, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload += json.dumps(CiReading(sha, state, f"{state} reason", taken, 1).to_dict()) + "\n"
    append_records(control_file(root, CONTROL_CI_CHECKS), payload, repair="", lock=True)


def _styles_at(text: Text, needle: str) -> str:
    """Every style applied where ``needle`` starts in ``text``."""
    start = text.plain.index(needle)
    return " ".join(str(span.style) for span in text.spans if span.start <= start < span.end)


def _assert_ci_lines(widget: Static, lines: list[str]) -> None:
    """The four merge lines, in PR order, each whole on its own line; only
    the passed commit is green."""
    content = cast(Text, widget.content)
    merged = [line for line in lines if line.startswith("  merged ")]
    assert [line.split("  ")[1][:20] for line in merged] == [
        "merged PR #1 b057de2",
        "merged PR #2 0aa55d9",
        "merged PR #3 6cabef5",
        "merged PR #4 1111111",
    ], lines
    assert merged[0].startswith("  merged PR #1 b057de2  CI passed · read "), merged[0]
    assert "ago · 7 checks passed" in merged[0], merged[0]
    assert merged[1].startswith("  merged PR #2 0aa55d9  CI failed · read "), merged[1]
    assert merged[2].startswith("  merged PR #3 6cabef5  CI unknown · read "), merged[2]
    assert "ago · gh api failed (4)" in merged[2], merged[2]
    assert merged[3] == f"  merged PR #4 1111111  {NO_CI_READING}", merged[3]
    assert theme.SUCCESS in _styles_at(content, "CI passed")
    for never_green in ("CI failed", "CI unknown", NO_CI_READING):
        assert theme.SUCCESS not in _styles_at(content, never_green), never_green
    width = widget.content_region.width
    assert max(len(line) for line in lines) <= width, (width, lines)


class TestCiOnDelivery:
    @pytest.mark.parametrize("size", SIZES)
    async def test_home_shows_each_merge_commits_ci_state(
        self, polled: Path, size: tuple[int, int]
    ) -> None:
        """G11: "CI unknown (kstrl records no CI check state)" beside every run."""
        app = _home(polled)
        async with app.run_test(size=size) as pilot:
            delivery = cast(Static, await mounted(pilot, lambda: app.screen, "#home-delivery"))
            await settled(
                pilot, lambda: "merged PR #4" in str(delivery.content), what="the delivery lines"
            )
            await settled(pilot, lambda: delivery.region.height == 6, what="six lines laid out")
            lines = str(delivery.content).splitlines()
            assert lines[0] == "delivery  run ship01 · release ref 1111111", lines
            assert len(lines) == 6 and delivery.region.height == 6, (lines, delivery.region)
            _assert_ci_lines(delivery, lines)

    @pytest.mark.parametrize("size", SIZES)
    async def test_the_run_overview_shows_each_merge_commits_ci_state(
        self, polled: Path, size: tuple[int, int]
    ) -> None:
        app = _dash(polled, SHIP)
        async with app.run_test(size=size) as pilot:
            row = cast(Static, await mounted(pilot, lambda: app.screen, "#delivery-row"))
            await settled(
                pilot,
                lambda: row.display and "merged PR #4" in str(row.content),
                what="the delivery lines",
            )
            await settled(pilot, lambda: row.region.height == 6, what="six lines laid out")
            lines = str(row.content).splitlines()
            assert lines[0] == "delivery · release ref 1111111", lines
            assert len(lines) == 6 and row.region.height == 6, (lines, row.region)
            _assert_ci_lines(row, lines)

    @pytest.mark.parametrize("screen", ["home", "overview"])
    async def test_an_old_reading_says_how_old_it_is(self, tmp_path: Path, screen: str) -> None:
        """A passed read five minutes ago is shown as five minutes old, on
        home and on the run overview, so a stale green is visible."""
        _shipped(tmp_path, (SHA_PASSED,))
        taken = datetime.fromtimestamp(time.time() - 300, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ensure_control_state(tmp_path)
        reading = CiReading(SHA_PASSED, CiState.PASSED, "7 checks passed", taken, 7)
        payload = json.dumps(reading.to_dict()) + "\n"
        append_records(control_file(tmp_path, CONTROL_CI_CHECKS), payload, repair="", lock=True)
        app = _home(tmp_path) if screen == "home" else _dash(tmp_path, SHIP)
        selector = "#home-delivery" if screen == "home" else "#delivery-row"
        async with app.run_test(size=(120, 36)) as pilot:
            delivery = cast(Static, await mounted(pilot, lambda: app.screen, selector))
            await settled(
                pilot, lambda: "merged PR #1" in str(delivery.content), what="the delivery line"
            )
            assert "merged PR #1 b057de2  CI passed · read 5m ago · 7 checks passed" in str(
                delivery.content
            ), str(delivery.content)

    @pytest.mark.parametrize(
        ("screen", "error"),
        [
            ("home", "UnicodeDecodeError"),
            ("overview", "UnicodeDecodeError"),
            ("home", "IsADirectoryError"),
        ],
    )
    async def test_an_unreadable_ledger_is_unknown_with_the_reason(
        self, tmp_path: Path, screen: str, error: str
    ) -> None:
        """Fail closed: a ledger that does not decode, or cannot be read at
        all, is every commit's unknown, named, and the rest still renders."""
        _shipped(tmp_path, (SHA_PASSED, SHA_FAILED))
        ensure_control_state(tmp_path)
        ledger = control_file(tmp_path, CONTROL_CI_CHECKS)
        if error == "IsADirectoryError":
            ledger.mkdir()
        else:
            ledger.write_bytes(b"\xff\xfe not utf-8\n")
        app = _home(tmp_path) if screen == "home" else _dash(tmp_path, SHIP)
        selector = "#home-delivery" if screen == "home" else "#delivery-row"
        async with app.run_test(size=(120, 36)) as pilot:
            widget = cast(Static, await mounted(pilot, lambda: app.screen, selector))
            await settled(
                pilot, lambda: "merged PR #2" in str(widget.content), what="the delivery lines"
            )
            text = str(widget.content)
            unknown = f"CI unknown · the CI ledger could not be read: {error}"
            assert text.count(unknown) == 2, text
            assert "passed" not in text and "failed" not in text, text
            assert "integration no review recorded for this run" in text, text

    async def test_more_merges_than_lines_are_counted_not_dropped(self, tmp_path: Path) -> None:
        """Six merges on home: three lines, then the other three counted by
        state, so the failed one past the cut is still named. A running
        reading is not green either."""
        shas = tuple(f"{n}" * 40 for n in range(1, 7))
        _shipped(tmp_path, shas)
        states = [CiState.PASSED, CiState.RUNNING, CiState.UNKNOWN, CiState.PASSED, CiState.FAILED]
        _ledger(tmp_path, [(sha, state, 0.0) for sha, state in zip(shas, states, strict=False)])
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            delivery = cast(Static, await mounted(pilot, lambda: app.screen, "#home-delivery"))
            await settled(
                pilot, lambda: "+3 more merged" in str(delivery.content), what="the counted line"
            )
            lines = str(delivery.content).splitlines()
            assert len(lines) == 6, lines
            assert lines[5] == (f"  +3 more merged: 1 CI passed, 1 CI failed, 1 {NO_CI_READING}"), (
                lines
            )
            assert "CI running · read " in lines[3], lines
            content = cast(Text, delivery.content)
            assert theme.SUCCESS not in _styles_at(content, "CI running"), lines

    @pytest.mark.parametrize("screen", ["home", "overview"])
    async def test_the_ledger_is_read_off_the_event_loop(
        self, polled: Path, screen: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every read of the ledger file happens on a worker thread, never on
        the thread the Textual event loop runs on."""
        import kstrl.ci_state as ci_state

        on_loop: list[bool] = []

        def spy(root_dir: Path, name: str) -> Path:
            # read_ci_ledger names its file through ci_state's control_file.
            if name == CONTROL_CI_CHECKS:
                on_loop.append(threading.current_thread() is threading.main_thread())
            return control_file(root_dir, name)

        monkeypatch.setattr(ci_state, "control_file", spy)
        app = _home(polled) if screen == "home" else _dash(polled, SHIP)
        selector = "#home-delivery" if screen == "home" else "#delivery-row"
        async with app.run_test(size=(120, 36)) as pilot:
            widget = cast(Static, await mounted(pilot, lambda: app.screen, selector))
            await settled(
                pilot, lambda: "merged PR #4" in str(widget.content), what="the delivery lines"
            )
        assert on_loop and not any(on_loop), on_loop


def _failed_manifest(root: Path, flags: tuple[tuple[str, FlagValue], ...]) -> None:
    """comp-a FAILED in the manifest, the run that failed it, and that run's
    launch record with ``flags`` and every run limit off."""
    paths = ev.RunPaths.for_run(root, FAILED_RUN)
    bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=FAILED_RUN)
    bus.emit(ev.RunStarted(project="demo", components=1))
    bus.emit(ev.RunPlan(components=({"id": "comp-a", "title": "A", "deps": []},)))
    bus.emit(ev.ComponentStarted(component="comp-a"))
    bus.emit(ev.ComponentFailed(component="comp-a", error="Mechanical verification failed"))
    bus.emit(ev.RunCompleted(completed=0, failed=1, skipped=0, duration_seconds=1.0))
    bus.close()
    manifest_file = root / "scripts" / "kstrl" / "manifest.json"
    manifest_file.parent.mkdir(parents=True)
    Manifest(
        version="1",
        spec_file="s",
        project_name="demo",
        base_branch="main",
        single_pr=False,
        run_id=FAILED_RUN,
        components=[
            Component(
                id="comp-a",
                title="A",
                description="",
                dependencies=[],
                prd_path="p.json",
                branch_name="kstrl/comp-a",
                status=ComponentStatus.FAILED.value,
            )
        ],
    ).save(manifest_file)
    limits = run_limits(FactoryConfig(), TimeoutConfig())
    assert write_launch_record(root, FAILED_RUN, manifest_file, flags, limits) == []


@pytest.fixture
def no_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIMIT_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def slow_carry() -> Iterator[threading.Event]:
    """Hold the failure queue's carry read until the test sets the event.
    Bounded, so a test that never sets it cannot hang the worker."""
    import kstrl.tui.screens.retry as retry_screen

    gate = threading.Event()
    real = retry_screen._read_queue_carry

    def held(root: Path, entries: Any) -> Any:
        gate.wait(10)
        return real(root, entries)

    with patch.object(retry_screen, "_read_queue_carry", held):
        try:
            yield gate
        finally:
            gate.set()


class TestRetry:
    @pytest.mark.parametrize("size", SIZES)
    async def test_r_waits_for_the_carry_and_says_so(
        self, tmp_path: Path, no_limit_env: None, slow_carry: threading.Event, size: tuple[int, int]
    ) -> None:
        """PR #561's verifier: r was offered before the queue knew whether this
        screen could carry the retry."""
        _failed_manifest(tmp_path, ())
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(RetryScreen())
            detail = cast(Static, await mounted(pilot, lambda: app.screen, "#retry-detail"))
            await settled(
                pilot, lambda: "reading scope..." in str(detail.content), what="the unread carry"
            )
            screen = app.screen
            offered = screen.check_action("retry_selected", ())
            await pilot.press("r")
            await drained(pilot, screen, what="r to be handled")
            left = app.screen is not screen or "reading scope..." not in str(detail.content)
            # Released before any assertion, so a failure does not hold the worker.
            slow_carry.set()
            assert offered is False and not left, (offered, left)
            await settled(
                pilot,
                lambda: "r works out what a retry would do" in str(detail.content),
                what="the carry to be read",
            )
            assert screen.check_action("retry_selected", ()) is True

    async def test_r_is_offered_when_the_runs_cannot_be_read(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """A failed read of the runs keeps the manifest's rows and still reads
        the carry, so r is not withheld for ever."""
        _failed_manifest(tmp_path, ())
        app = _home(tmp_path)
        with patch("kstrl.tui.screens.retry._read_failure_queue", side_effect=RuntimeError("boom")):
            async with app.run_test(size=(120, 36)) as pilot:
                await mounted(pilot, lambda: app.screen, "#home-runs")
                app.push_screen(RetryScreen())
                detail = cast(Static, await mounted(pilot, lambda: app.screen, "#retry-detail"))
                await settled(
                    pilot,
                    lambda: "r works out what a retry would do" in str(detail.content),
                    what="the carry to be read after the runs read failed",
                )
                assert app.screen.check_action("retry_selected", ()) is True

    async def test_the_scope_names_a_recorded_flag_it_does_not_replay(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """PR #557/#551: `ks retry` prints "Not replayed from run ..." for a
        removed option; the TUI's scope and its confirmation said nothing."""
        _failed_manifest(tmp_path, (("verify_command", "uv run pytest"),))
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(RetryScreen())
            detail = cast(Static, await mounted(pilot, lambda: app.screen, "#retry-detail"))
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered once the carry is read",
            )
            await pilot.press("r")
            await settled(
                pilot, lambda: isinstance(app.screen, OptionsModal), what="the confirmation"
            )
            header = cast(OptionsModal, app.screen).request.header
            assert f"not replayed: {DROPPED}" in header, header
            assert "--verify-command" not in header.split("runs under:")[1].splitlines()[0]
            await pilot.press("escape")
            await settled(pilot, lambda: app.screen.query("#retry-detail"), what="the queue")
            assert DROPPED in str(detail.content), str(detail.content)

    async def test_the_cli_command_names_a_recorded_flag_it_does_not_replay(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """The same line when only `ks retry` can carry the retry: a recorded
        --max-parallel is a flag this screen cannot carry."""
        _failed_manifest(tmp_path, (("verify_command", "uv run pytest"), ("max_parallel", 1)))
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(RetryScreen())
            detail = cast(Static, await mounted(pilot, lambda: app.screen, "#retry-detail"))
            await settled(
                pilot, lambda: "ks retry comp-a" in str(detail.content), what="the CLI command"
            )
            assert f"not replayed: {DROPPED}" in str(detail.content), str(detail.content)
            assert app.screen.check_action("retry_selected", ()) is False


def _spent(root: Path, run_id: str, cost: float, cap: float) -> None:
    """A finished run that spent ``cost`` under a recorded cap of ``cap``."""
    paths = ev.RunPaths.for_run(root, run_id)
    bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=run_id)
    bus.emit(ev.RunStarted(project="demo", components=1))
    bus.emit(ev.RunPlan(components=({"id": "api", "title": "API", "deps": []},), max_cost_usd=cap))
    bus.emit(
        ev.ComponentUsage(
            component="api",
            phase="engineer",
            calls=1,
            known_calls=1,
            token_calls=1,
            cost_calls=1,
            total_tokens=1_000,
            cost_usd=cost,
        )
    )
    bus.emit(ev.RunCompleted(completed=1, failed=0, skipped=0, duration_seconds=1.0))
    bus.close()
    _backdate(paths.events_file, 7200)


class TestHistoryCost:
    @pytest.mark.parametrize("size", [(120, 36), (110, 36), (80, 24)])
    async def test_the_cost_column_is_spend_against_the_recorded_cap(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """PR #561's verifier: history showed "$19.24" beside a run whose cap
        was recorded. cap_percent's rule; a run with no cap is not given one."""
        _spent(tmp_path, PRICED, 19.24, 78.0)
        _spent(tmp_path, FREE, 1.47, 0.0)
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            runs = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#home-runs"))
            await settled(
                pilot,
                lambda: (
                    PRICED in [str(k.value) for k in runs.rows]
                    and "completed" in str(runs.get_cell(PRICED, "state"))
                    and not runs._updated_cells
                    and not runs._require_update_dimensions
                ),
                what="the summaries to land and the widths to be applied",
            )
            columns = [str(column.key.value) for column in runs.ordered_columns]
            if size[0] >= 110:
                assert str(runs.get_cell(PRICED, "cost")) == "$19.24 of $78.00 cap 25%"
                assert str(runs.get_cell(FREE, "cost")) == "$1.47 · no cap"
            else:
                assert "cost" not in columns, columns
            assert runs.virtual_size.width <= runs.region.width, (runs.virtual_size, runs.region)
