"""TUI surface D6: the retry screen (#436).

Split out of test_launch_session.py when the file-length ratchet fired,
the same way test_tui_config_walk.py split from test_tui_config_guard.py:
that file drives the launch seam and forms, this one drives RetryScreen.
Shares its `_home_app`/`FakeSession`/`_notified` fixtures rather than
duplicating them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.factory import FactoryConfig
from kstrl.launch import FactoryLaunch
from kstrl.launch_record import run_limits, write_launch_record
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.retry_plan import RESUME_REFUSAL
from kstrl.timeout import TimeoutConfig
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.screens.retry import RetryScreen
from tests.helpers.settle import drained, mounted, settled
from tests.test_launch_session import FakeSession, _home_app, _notified


def _limits(max_cost_usd: float) -> dict[str, float]:
    """Every run limit off except the cost ceiling (#526)."""
    return {**run_limits(FactoryConfig(), TimeoutConfig()), "max_cost_usd": max_cost_usd}


class TestRetryScreen:
    def _failed_manifest(self, tmp_path: Path, *, run_id: str = "") -> Path:
        manifest_dir = tmp_path / "scripts" / "kstrl"
        manifest_dir.mkdir(parents=True)
        manifest = Manifest(
            version="1",
            spec_file="s",
            project_name="demo",
            base_branch="main",
            single_pr=False,
            run_id=run_id,
            components=[
                Component(
                    id="comp-a",
                    title="A",
                    description="",
                    dependencies=[],
                    prd_path="p.json",
                    branch_name="kstrl/comp-a",
                    status=ComponentStatus.FAILED.value,
                    failed_phase="review",
                    failed_check="criteria",
                    error="review found blocking issues",
                ),
                Component(
                    id="comp-b",
                    title="B",
                    description="",
                    dependencies=[],
                    prd_path="p.json",
                    branch_name="kstrl/comp-b",
                    status=ComponentStatus.COMPLETED.value,
                ),
            ],
        )
        manifest.save(manifest_dir / "manifest.json")
        return manifest_dir / "manifest.json"

    async def test_lists_failed_and_launches_after_confirm(
        self,
        tmp_path: Path,
    ) -> None:
        # A run_id + an uncapped ($0) launch record: plan_resume replays
        # nothing and needs no ceiling FactoryLaunch cannot carry (#436).
        run_id = "factory-20260101-000000.000000-fake"
        manifest_file = self._failed_manifest(tmp_path, run_id=run_id)
        assert write_launch_record(tmp_path, run_id, manifest_file, (), _limits(0.0)) == []
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            app.push_screen(RetryScreen())
            table = await mounted(pilot, lambda: app.screen, "#retry-table")
            detail_widget = await mounted(pilot, lambda: app.screen, "#retry-detail")
            # compose and on_mount both run before a screen takes
            # anything off its own queue, so a callback on that queue
            # is proof the manifest has been read into the table. That
            # is weaker than the two assertions, which are about WHAT
            # it read.
            await drained(
                pilot,
                app.screen,
                what="the retry screen's on_mount to run",
            )
            assert table.row_count == 1  # type: ignore[attr-defined]
            detail = str(detail_widget.content)
            assert "review found blocking issues" in detail
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered once the carry is read",
            )
            await pilot.press("r")
            # Weaker than the assertion: r handed over to some other
            # screen, not specifically to the confirm modal.
            await settled(
                pilot,
                lambda: not isinstance(app.screen, RetryScreen),
                what="r to open the retry confirmation",
            )
            assert isinstance(app.screen, OptionsModal)
            assert "comp-a" in app.screen.request.header
            await pilot.press("1")  # Start retry
            # Either outcome of the confirmation, so a wrongly refused
            # retry fails on the assertion below and not here.
            await settled(
                pilot,
                lambda: specs or _notified(app, "retry"),
                what="the confirmation to launch the retry or refuse it",
            )
            assert len(specs) == 1
            assert isinstance(specs[0], FactoryLaunch)
            assert specs[0].manifest_path == manifest_file
            # prepare_retry really ran: the component is pending again.
            reloaded = Manifest.load(manifest_file)
            comp = reloaded.get_component("comp-a")
            assert comp is not None
            assert comp.status == ComponentStatus.PENDING.value

    async def test_empty_state(self, tmp_path: Path) -> None:
        app = _home_app(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            app.push_screen(RetryScreen())
            detail_widget = await mounted(pilot, lambda: app.screen, "#retry-detail")
            await drained(
                pilot,
                app.screen,
                what="the retry screen's on_mount to run",
            )
            detail = str(detail_widget.content)
            assert "Nothing to retry" in detail

    async def test_confirmation_does_not_overwrite_changed_manifest(
        self,
        tmp_path: Path,
    ) -> None:
        # An uncapped launch record, so the scope is known and offered.
        run_id = "factory-20260101-000000.000000-changed"
        manifest_file = self._failed_manifest(tmp_path, run_id=run_id)
        assert write_launch_record(tmp_path, run_id, manifest_file, (), _limits(0.0)) == []
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            app.push_screen(RetryScreen())
            await mounted(pilot, lambda: app.screen, "#retry-table")
            await drained(
                pilot,
                app.screen,
                what="the retry screen's on_mount to run",
            )
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered once the carry is read",
            )
            await pilot.press("r")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, RetryScreen),
                what="r to open the retry confirmation",
            )
            assert isinstance(app.screen, OptionsModal)

            changed = Manifest.load(manifest_file)
            comp = changed.get_component("comp-a")
            assert comp is not None
            comp.status = ComponentStatus.COMPLETED.value
            changed.save(manifest_file)

            await pilot.press("1")
            # A refusal writes nothing, so its warning is the only
            # trace; the OR covers the defect, where the confirmation
            # goes through and `specs` grows, so the assertions below
            # fail with their own messages instead of timing out here.
            await settled(
                pilot,
                lambda: _notified(app, "retry plan changed") or specs,
                what="the confirmation to act on the changed manifest",
            )

        persisted = Manifest.load(manifest_file).get_component("comp-a")
        assert persisted is not None
        assert persisted.status == ComponentStatus.COMPLETED.value
        assert specs == []

    async def test_refuses_a_retry_it_cannot_carry_the_ceiling_of(
        self,
        tmp_path: Path,
    ) -> None:
        """B1: FactoryLaunch has no field for a cost ceiling (#436).

        A $5 ceiling from kstrl.toml/env, not a flag: the record names
        it but no flag replays it, so the TUI must refuse rather than
        relaunch through FactoryLaunch(manifest_path=...) alone.
        """
        run_id = "factory-20260101-000000.000000-capped"
        manifest_file = self._failed_manifest(tmp_path, run_id=run_id)
        assert write_launch_record(tmp_path, run_id, manifest_file, (), _limits(5.0)) == []
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        with patch("kstrl.tui.screens.retry.prepare_retry") as mock_prepare:
            async with app.run_test(size=(130, 40)) as pilot:
                app.push_screen(RetryScreen())
                await mounted(pilot, lambda: app.screen, "#retry-table")
                detail = await mounted(pilot, lambda: app.screen, "#retry-detail")
                # Increment 3 (#433 G1): the failure queue reads the resume
                # plan when it reads the runs, so the row says the CLI carries
                # it, names the command, and r is not offered at all.
                await settled(
                    pilot,
                    lambda: "ks retry comp-a --max-cost-usd 5" in str(detail.content),  # type: ignore[attr-defined]
                    what="the queue to name the command that carries the ceiling",
                )
                assert app.screen.check_action("retry_selected", ()) is False
                await pilot.press("r")
                await drained(pilot, app.screen, what="r to be handled")
                assert isinstance(app.screen, RetryScreen)
                assert specs == []
            mock_prepare.assert_not_called()

        persisted = Manifest.load(manifest_file).get_component("comp-a")
        assert persisted is not None
        assert persisted.status == ComponentStatus.FAILED.value

    async def test_launches_under_a_ceiling_from_kstrl_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B1: a kstrl.toml ceiling is not a recorded flag - the TUI's own

        launch path (session.py -> launch.py -> FactoryConfig.load) loads
        it the same way plan_resume did, so FactoryLaunch(manifest_path=...)
        alone still runs under it and the TUI must not refuse.
        """
        monkeypatch.delenv("KSTRL_FACTORY_MAX_COST_USD", raising=False)
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_cost_usd = 5\n", encoding="utf-8")
        run_id = "factory-20260101-000000.000000-toml"
        manifest_file = self._failed_manifest(tmp_path, run_id=run_id)
        assert write_launch_record(tmp_path, run_id, manifest_file, (), _limits(5.0)) == []
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            app.push_screen(RetryScreen())
            await mounted(pilot, lambda: app.screen, "#retry-table")
            await drained(pilot, app.screen, what="on_mount to run")
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered once the carry is read",
            )
            await pilot.press("r")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, RetryScreen),
                what="r to open the retry confirmation",
            )
            assert isinstance(app.screen, OptionsModal)
            await pilot.press("1")  # Start retry
            await settled(
                pilot,
                lambda: specs or _notified(app, RESUME_REFUSAL),
                what="the confirmation to launch under the toml ceiling",
            )
            assert len(specs) == 1
            assert isinstance(specs[0], FactoryLaunch)

    async def test_refuses_a_retry_with_recorded_flags(
        self,
        tmp_path: Path,
    ) -> None:
        """B1: a recorded flag (not just a ceiling) has no FactoryLaunch field.

        The run carried --max-parallel 1 with no cost ceiling; FactoryLaunch
        cannot replay that flag, so the TUI must refuse.
        """
        run_id = "factory-20260101-000000.000000-flags"
        manifest_file = self._failed_manifest(tmp_path, run_id=run_id)
        flags = (("max_parallel", 1),)
        assert write_launch_record(tmp_path, run_id, manifest_file, flags, _limits(0.0)) == []
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        with patch("kstrl.tui.screens.retry.prepare_retry") as mock_prepare:
            async with app.run_test(size=(130, 40)) as pilot:
                app.push_screen(RetryScreen())
                await mounted(pilot, lambda: app.screen, "#retry-table")
                detail = await mounted(pilot, lambda: app.screen, "#retry-detail")
                # Increment 3 (#433 G1): withheld before r, not refused after.
                await settled(
                    pilot,
                    lambda: "cannot be carried through the TUI" in str(detail.content),  # type: ignore[attr-defined]
                    what="the queue to say the recorded flags need the CLI",
                )
                assert "ks retry comp-a" in str(detail.content)  # type: ignore[attr-defined]
                assert app.screen.check_action("retry_selected", ()) is False
                await pilot.press("r")
                await drained(pilot, app.screen, what="r to be handled")
                assert isinstance(app.screen, RetryScreen)
                assert specs == []
            mock_prepare.assert_not_called()

        persisted = Manifest.load(manifest_file).get_component("comp-a")
        assert persisted is not None
        assert persisted.status == ComponentStatus.FAILED.value


async def test_the_confirmation_names_the_failed_component_it_leaves_out(tmp_path: Path) -> None:
    """#485: http-api and cli both FAILED; retrying http-api names cli and its command."""
    manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
    manifest_file.parent.mkdir(parents=True)
    Manifest(
        version="1",
        spec_file="s",
        project_name="demo",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=cid,
                title=cid,
                description="",
                dependencies=deps,
                prd_path="p.json",
                branch_name=f"kstrl/{cid}",
                status=status.value,
            )
            for cid, deps, status in (
                ("storage", [], ComponentStatus.COMPLETED),
                ("http-api", ["storage"], ComponentStatus.FAILED),
                ("cli", ["storage"], ComponentStatus.FAILED),
            )
        ],
    ).save(manifest_file)
    # An uncapped launch record, so the scope is known and offered (#433).
    run_id = "factory-20260101-000000.000000-leave"
    manifest = Manifest.load(manifest_file)
    manifest.run_id = run_id
    manifest.save(manifest_file)
    assert write_launch_record(tmp_path, run_id, manifest_file, (), _limits(0.0)) == []
    app = _home_app(tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        app.push_screen(RetryScreen())
        table = await mounted(pilot, lambda: app.screen, "#retry-table")
        await drained(pilot, app.screen, what="the retry screen's on_mount to run")
        assert table.row_count == 2  # type: ignore[attr-defined]
        await settled(
            pilot,
            lambda: app.screen.check_action("retry_selected", ()) is True,
            what="r to be offered once the carry is read",
        )
        await pilot.press("r")
        await settled(
            pilot,
            lambda: not isinstance(app.screen, RetryScreen),
            what="r to open the retry confirmation",
        )
        assert isinstance(app.screen, OptionsModal)
        header = app.screen.request.header
        assert header.startswith("Retry 'http-api'?"), header
        assert "Not in this retry" in header, header
        assert "cli: FAILED; retry it after this run with ks retry cli" in header, header
        assert "ks retry http-api" not in header, header
