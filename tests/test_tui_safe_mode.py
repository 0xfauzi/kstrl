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
from tests.helpers.settle import mounted, settled


def _app(root: Path, run_dir: Path) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=run_dir,
        root_dir=root,
        mode=Mode.DASH,
        poll_interval=0.05,
    )


def _reason(source: str = "queue", detail: str = "paused") -> SafeModeReason:
    return SafeModeReason(
        source=source,
        detail=detail,
        recovery=RECOVERY[source],
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
        banner = render_banner(
            [
                _reason("queue", "paused"),
                _reason("autonomy", "clamped"),
            ]
        )

        assert "queue" in banner
        assert "autonomy" in banner
        assert "press f2" in banner

    def test_the_banner_names_repeated_sources_once(self) -> None:
        """Two skipped phases are one story on a one-line banner."""
        banner = render_banner(
            [
                _reason("adversarial_skipped", "review did not run"),
                _reason("adversarial_skipped", "security did not run"),
            ]
        )

        assert banner.count("adversarial_skipped") == 1
        assert "2 reason(s)" in banner


class TestDashboardSurface:
    async def test_the_chip_reaches_the_run_masthead(
        self,
        tmp_path: Path,
    ) -> None:
        """The gap itself: this is where `ks status` and `ks dash` land."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=2))
        Queue(tmp_path, QueueConfig()).pause(
            reason="daily budget exhausted",
            actor="test",
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
        assert "press f2" in text

    async def test_a_clean_root_hides_the_banner(
        self,
        tmp_path: Path,
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
        self,
        tmp_path: Path,
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
        self,
        tmp_path: Path,
    ) -> None:
        """The chip can only say how many. The panel says what, in the
        signal's own words, with the runbook anchor that recovers it."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        Queue(tmp_path, QueueConfig()).pause(
            reason="poison breaker tripped",
            actor="test",
        )
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons:
                    break
                await pilot.pause(0.05)
            await pilot.press("f2")
            await pilot.pause()
            assert isinstance(app.screen, SafeModePanel)
            body = str(app.screen.query_one("#safemode-body").render())

        assert "poison breaker tripped" in body
        assert RECOVERY["queue"] in body

    async def test_escape_closes_the_panel(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("f2")
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.tui.widgets.safe_mode_chip import SafeModeChip

        Queue(tmp_path, QueueConfig()).pause(
            reason="daily budget exhausted",
            actor="test",
        )
        app = KstrlTuiApp(
            root_dir=tmp_path,
            mode=Mode.HOME,
            poll_interval=0.05,
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
        self,
        tmp_path: Path,
    ) -> None:
        """Where there IS room, the clean state is stated rather than
        left as an absence."""
        from kstrl.tui.widgets.safe_mode_chip import SafeModeChip

        app = KstrlTuiApp(
            root_dir=tmp_path,
            mode=Mode.HOME,
            poll_interval=0.05,
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


class TestReviewFindings:
    """One test per defect the post-merge review reproduced. Each fails
    on the merged implementation."""

    async def test_the_banners_do_not_overlap_each_other_or_the_topbar(
        self,
        tmp_path: Path,
    ) -> None:
        """P1. `dock: top` siblings ALL reserve row zero and paint over
        each other, so the checkpoint banner hid the safe-mode warning
        and the safe-mode warning hid the run header. Measured y=0,0,0
        before the fix. This also repairs a pre-existing bug: the
        checkpoint banner has always covered the topbar.

        The two pauses this used to count are what made it flaky: on a
        loaded CI runner the banner had not been laid out when
        `region.y` was read, so the zero region answered and the rows
        came back `[0, 0, 1]`. The wait is now on the layout itself. It
        is deliberately weaker than the assertion - "all three have been
        laid out", not "their rows differ" - so that the defect this
        test names still reaches the assertion below and fails there,
        with its own message, rather than timing out here.
        """
        from kstrl.tui.screens.overview import CheckpointBanner

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=2))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            banner = await mounted(pilot, lambda: app.screen, SafeModeBanner)
            checkpoint = await mounted(pilot, lambda: app.screen, CheckpointBanner)
            topbar = await mounted(pilot, lambda: app.screen, "#topbar")
            banner.display = True
            checkpoint.display = True
            await settled(
                pilot,
                lambda: all(w.region.height for w in (topbar, banner, checkpoint)),
                what="the topbar and both banners to be laid out once shown",
            )
            rows = [topbar.region.y, banner.region.y, checkpoint.region.y]

        assert len(set(rows)) == 3, f"widgets share a row: {rows}"
        assert rows == sorted(rows)

    async def test_a_focused_text_input_does_not_swallow_the_key(
        self,
        tmp_path: Path,
    ) -> None:
        """P2, and the weak first attempt at this test. Asserting
        `len(key) > 1` passed for "slash", which Textual emits as the
        printable `/` that an Input consumes - the test permitted the
        exact regression it named. So drive the real thing: focus an
        Input, press the key, and check both that the panel opened and
        that nothing was typed."""
        from textual.widgets import Input

        from kstrl.tui.app import KstrlTuiApp as App
        from kstrl.tui.screens.safemode import SafeModePanel as Panel

        keys = [b.key for b in App.BINDINGS if getattr(b, "action", "") == "safe_mode"]
        assert keys, "no safe-mode binding at all"

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            probe = Input(id="probe-input")
            await app.screen.mount(probe)
            probe.focus()
            await pilot.pause()
            await pilot.press(keys[0])
            await pilot.pause()
            opened = isinstance(app.screen, Panel)
            typed = probe.value

        assert opened, f"{keys[0]!r} did not open the panel from an Input"
        assert typed == "", f"{keys[0]!r} was typed into the field: {typed!r}"

    async def test_a_late_check_does_not_overwrite_a_newer_one(
        self,
        tmp_path: Path,
    ) -> None:
        """P2. exclusive=True cancels the asyncio wrapper, never the
        thread, so a superseded check still posts. A slow NOMINAL result
        landing after a fast DEGRADED one would clear the warning."""
        from kstrl.tui.messages import SafeModeChecked

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons is not None:
                    break
                await pilot.pause(0.05)
            # A fresh degraded result, then a STALE nominal one.
            app.post_message(SafeModeChecked([_reason()], seq=99))
            await pilot.pause()
            app.post_message(SafeModeChecked([], seq=98))
            await pilot.pause()
            after = app._safe_mode_reasons

        assert after, "a superseded check cleared a newer degradation"

    async def test_the_open_panel_updates_when_a_check_lands(
        self,
        tmp_path: Path,
    ) -> None:
        """P2. The panel took its reasons at construction and never
        looked again, so opening it before the first check finished left
        it reading "not checked yet" for the life of the session."""
        from kstrl.tui.messages import SafeModeChecked
        from kstrl.tui.screens.safemode import SafeModePanel as Panel

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        Queue(tmp_path, QueueConfig()).pause(reason="paused now", actor="t")
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons:
                    break
                await pilot.pause(0.05)
            # Constructed with None, as it is when opened before the
            # first check reports.
            app.push_screen(Panel(None))
            await pilot.pause()
            assert isinstance(app.screen, Panel)
            on_mount_body = str(
                app.screen.query_one("#safemode-body").render(),
            )
            # And the broadcast path, for a check that lands while open.
            app.post_message(
                SafeModeChecked([_reason("autonomy", "clamped later")], seq=99),
            )
            await pilot.pause()
            await pilot.pause()
            broadcast_body = str(
                app.screen.query_one("#safemode-body").render(),
            )

        assert "paused now" in on_mount_body  # replayed on mount
        assert "clamped later" in broadcast_body  # updated in place

    async def test_a_screen_mounted_later_shows_the_active_warning(
        self,
        tmp_path: Path,
    ) -> None:
        """P2. on_mount hid the banner and nothing replayed the last
        completed check, so navigating home -> run made an active
        warning vanish for up to five seconds."""
        from kstrl.tui.screens.overview import OverviewScreen

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        Queue(tmp_path, QueueConfig()).pause(reason="paused", actor="t")
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons:
                    break
                await pilot.pause(0.05)
            # A screen built AFTER the check already reported.
            app.push_screen(OverviewScreen(observe_only=True))
            await pilot.pause()
            await pilot.pause()
            shown = app.screen.query_one(SafeModeBanner).display

        assert shown, "a freshly mounted screen hid an active warning"

    async def test_the_panel_dialog_has_a_border_so_its_title_renders(
        self,
        tmp_path: Path,
    ) -> None:
        """P3. border_title renders only with a border, and there was no
        #safemode-dialog rule at all, so the modal filled the screen
        with no visible title."""
        from kstrl.tui.screens.safemode import SafeModePanel as Panel

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            app.push_screen(Panel([_reason()]))
            await pilot.pause()
            dialog = app.screen.query_one("#safemode-dialog")
            has_border = dialog.styles.border.top[0] not in ("", None)
            width = dialog.region.width

        assert has_border, "no border, so border_title never renders"
        assert width < 120, "the dialog fills the whole screen"


class TestGatingReviewFindings:
    """The review of the fix itself. One of these is a defect the
    previous round's own fix introduced, which is now the third time
    that has happened on this repository."""

    async def test_a_dropped_tick_reruns_instead_of_being_lost(
        self,
        tmp_path: Path,
    ) -> None:
        """P2, introduced by the previous round's in-flight guard.
        safemode reads the queue BEFORE the expensive event stream, so a
        check can sample a nominal queue, spend seconds on the stream,
        and have that stale answer stay authoritative while the tick
        that would have seen a new pause was simply dropped."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons is not None:
                    break
                await pilot.pause(0.05)
            # A check is in flight; a tick arrives and must not vanish.
            app._safe_mode_running = True
            app._check_safe_mode()
            requested = app._safe_mode_rerun

        assert requested, "the tick was dropped, not remembered"

    async def test_the_binding_reaches_system_modals(
        self,
        tmp_path: Path,
    ) -> None:
        """P2. Textual's command palette is a SystemModalScreen and
        excludes non-priority app bindings, so the key did nothing there
        while the runbook promised the panel from any screen."""
        from kstrl.tui.app import KstrlTuiApp as App

        safe_mode = [b for b in App.BINDINGS if getattr(b, "action", "") == "safe_mode"]

        assert safe_mode
        for binding in safe_mode:
            assert getattr(binding, "priority", False), (
                "a non-priority app binding is excluded from system "
                "modal screens such as the command palette"
            )

    async def test_every_reason_is_reachable_in_a_short_terminal(
        self,
        tmp_path: Path,
    ) -> None:
        """P2. The scroller laid out taller than the dialog, so the
        dialog clipped the overflow while max_scroll_y stayed 0: the
        extra reasons were invisible AND unreachable. Measured at 80x24
        with four reasons before the fix: 15 content rows, 4 visible,
        max_scroll_y 0."""
        from kstrl.tui.screens.safemode import SafeModePanel as Panel

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        reasons = [_reason("queue", f"reason number {index}") for index in range(4)]

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(Panel(reasons))
            await pilot.pause()
            await pilot.pause()
            body = app.screen.query_one("#safemode-body")
            scroll = app.screen.query_one("#safemode-scroll")
            hidden = body.region.height - scroll.region.height
            reachable = scroll.max_scroll_y

        assert hidden > 0, "the fixture no longer overflows; widen it"
        assert reachable >= hidden, f"{hidden} rows overflow but only {reachable} are scrollable"

    async def test_an_explicit_panel_keeps_the_reasons_it_was_given(
        self,
        tmp_path: Path,
    ) -> None:
        """Found while measuring the one above, not by the review. The
        previous round's replay-on-mount was unconditional, so a panel
        constructed with real findings rendered the app's nominal state
        instead. It made the clipping measurement lie."""
        from kstrl.tui.screens.safemode import SafeModePanel as Panel

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            for _ in range(40):
                if app._safe_mode_reasons is not None:
                    break
                await pilot.pause(0.05)
            assert app._safe_mode_reasons == []  # the app is nominal
            app.push_screen(Panel([_reason("queue", "explicitly passed")]))
            await pilot.pause()
            body = str(app.screen.query_one("#safemode-body").render())

        assert "explicitly passed" in body
