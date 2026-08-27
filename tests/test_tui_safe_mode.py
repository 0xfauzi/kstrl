"""R10.4 follow-up: safe mode on the dashboard.

The predicate shipped on the plain `ks status` report and on `ks serve
--dry-run`, and reached neither surface a person looks at: on a terminal
`ks status` opens this dashboard whenever a run directory exists, so the
default interactive path showed nothing. These tests drive the real app
headless through Textual's Pilot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from kstrl.safemode import RECOVERY, SafeModeReason
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.safemode import SafeModePanel
from kstrl.tui.widgets.safe_mode_chip import (
    SafeModeBanner,
    render_banner,
    render_chip,
)
from kstrl.workqueue import Queue, QueueConfig
from tests.helpers.fake_run import FakeRunSpec, write_fake_run


def _app(root: Path, run_dir: Path) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=run_dir, root_dir=root, mode=Mode.DASH, poll_interval=0.05,
    )


def _reason(source: str = "queue", detail: str = "paused") -> SafeModeReason:
    return SafeModeReason(
        source=source, detail=detail, recovery=RECOVERY[source],
    )


class TestChipRendering:
    def test_three_states_are_visually_distinct(self) -> None:
        """Not checked, checked-clean and degraded are three different
        facts and must not share a rendering."""
        unchecked = render_chip(None).plain
        nominal = render_chip([]).plain
        degraded = render_chip([_reason()]).plain

        assert len({unchecked, nominal, degraded}) == 3
        assert "?" in unchecked
        assert "ok" in nominal
        assert "1" in degraded

    def test_the_clean_state_is_rendered_not_hidden(self) -> None:
        """Hiding the chip while nominal would be calmer and would make
        a missing chip and a clean one look identical - the exact fault
        the predicate exists to prevent."""
        assert render_chip([]).plain.strip() != ""

    def test_the_chip_stays_narrow_in_every_state(self) -> None:
        """Measured, not assumed: at 120 columns a 33-cell chip pushed
        the run's own state label from "✓ finished" down to "✓". The
        topbar is one line and the header owns the hierarchy."""
        many = [_reason("queue", f"reason {i}") for i in range(12)]
        for reasons in (None, [], [_reason()], many):
            assert len(render_chip(reasons).plain) <= 6, reasons

    def test_the_banner_names_the_sources(self) -> None:
        banner = render_banner([
            _reason("queue", "paused"),
            _reason("autonomy", "clamped"),
        ])

        assert "queue" in banner
        assert "autonomy" in banner
        assert "press m" in banner

    def test_the_banner_names_repeated_sources_once(self) -> None:
        """Two skipped phases are one story on a one-line banner."""
        banner = render_banner([
            _reason("adversarial_skipped", "review did not run"),
            _reason("adversarial_skipped", "security did not run"),
        ])

        assert banner.count("adversarial_skipped") == 1
        assert "2 reason(s)" in banner


class TestDashboardSurface:
    async def test_the_chip_reaches_the_run_masthead(
        self, tmp_path: Path,
    ) -> None:
        """The gap itself: this is where `ks status` and `ks dash` land."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=2))
        Queue(tmp_path, QueueConfig()).pause(
            reason="daily budget exhausted", actor="test",
        )
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons:
                    break
                await pilot.pause(0.05)
            banner_widget = app.screen.query_one(SafeModeBanner)
            text = str(banner_widget.render())
            banner_shown = banner_widget.display

        assert app._safe_mode_reasons is not None
        assert banner_shown
        assert "safe mode: 1 reason(s)" in text
        assert "queue" in text
        assert "press m" in text

    async def test_a_clean_root_hides_the_banner(
        self, tmp_path: Path,
    ) -> None:
        """Hiding it is safe here and only here: `m` is in the footer on
        every screen and the panel says which of the three states this
        is, so "no banner" never has to carry a meaning on its own."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons is not None:
                    break
                await pilot.pause(0.05)
            shown = app.screen.query_one(SafeModeBanner).display

        assert app._safe_mode_reasons == []
        assert not shown

    def test_the_panel_separates_nominal_from_not_checked(self) -> None:
        """The banner is hidden while nominal, so the panel is where the
        three states must stay distinct."""
        from kstrl.tui.screens.safemode import panel_title

        titles = [panel_title(None), panel_title([]), panel_title([_reason()])]

        assert len(set(titles)) == 3
        assert "not checked yet" in titles[0]
        assert "nominal" in titles[1]
        assert "1 reason(s)" in titles[2]

    async def test_the_run_topbar_keeps_the_run_state_label(
        self, tmp_path: Path,
    ) -> None:
        """Measured: header 41 cells + cost meter 79 already want 126 of
        120, so anything added to the topbar costs the run its own
        state word. This pins that nothing was added."""
        from kstrl.tui.screens.overview import OverviewScreen

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=2))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            topbar = app.screen.query_one("#topbar")
            children = [child.id for child in topbar.children]

        assert children == ["run-header", "cost-meter"]

    async def test_the_key_opens_the_panel_with_the_reasons(
        self, tmp_path: Path,
    ) -> None:
        """The chip can only say how many. The panel says what, in the
        signal's own words, with the runbook anchor that recovers it."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        Queue(tmp_path, QueueConfig()).pause(
            reason="poison breaker tripped", actor="test",
        )
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons:
                    break
                await pilot.pause(0.05)
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen, SafeModePanel)
            body = "\n".join(
                str(node.render()) for node in app.screen.query(Static)
            )

        assert "poison breaker tripped" in body
        assert RECOVERY["queue"] in body

    async def test_escape_closes_the_panel(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen, SafeModePanel)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SafeModePanel)


class TestTheCheckDoesNotRunOnThePollTimer:
    def test_the_interval_is_far_slower_than_the_poll(self) -> None:
        """safe_mode_reasons stats the control directory and reads a
        run's whole events.jsonl. On the 0.2s poll timer a long stream
        would hitch every frame."""
        from kstrl.tui.app import DEFAULT_POLL_INTERVAL, SAFE_MODE_INTERVAL_SECONDS

        assert SAFE_MODE_INTERVAL_SECONDS >= DEFAULT_POLL_INTERVAL * 20

    async def test_a_worker_failure_does_not_strand_the_chip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A chip stuck on "checking" forever is the ambiguity this
        feature exists to remove, so the worker reports its own failure
        rather than dying quietly."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))

        def boom(root: Path) -> list[SafeModeReason]:
            raise RuntimeError("sensor exploded")

        monkeypatch.setattr("kstrl.safemode.safe_mode_reasons", boom)
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons:
                    break
                await pilot.pause(0.05)

        assert app._safe_mode_reasons
        assert "could not evaluate" in app._safe_mode_reasons[0].detail


class TestHomeShell:
    """The home masthead is a Vertical with room, so it carries the chip
    the run topbar has no width for."""

    async def test_the_chip_reaches_the_home_masthead(
        self, tmp_path: Path,
    ) -> None:
        from kstrl.tui.widgets.safe_mode_chip import SafeModeChip

        Queue(tmp_path, QueueConfig()).pause(
            reason="daily budget exhausted", actor="test",
        )
        app = KstrlTuiApp(
            root_dir=tmp_path, mode=Mode.HOME, poll_interval=0.05,
        )

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons:
                    break
                await pilot.pause(0.05)
            text = str(app.screen.query_one(SafeModeChip).render())

        assert app._safe_mode_reasons is not None
        assert "1" in text

    async def test_the_home_chip_is_rendered_while_clean(
        self, tmp_path: Path,
    ) -> None:
        """Where there IS room, the clean state is stated rather than
        left as an absence."""
        from kstrl.tui.widgets.safe_mode_chip import SafeModeChip

        app = KstrlTuiApp(
            root_dir=tmp_path, mode=Mode.HOME, poll_interval=0.05,
        )

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons is not None:
                    break
                await pilot.pause(0.05)
            text = str(app.screen.query_one(SafeModeChip).render())

        assert app._safe_mode_reasons == []
        assert "ok" in text
