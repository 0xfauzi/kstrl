"""#289: a home-shell screen must name a broken config, not raise on it.

The measurement this file replaces a claim with: on 5abbc91, pushing
``EvolveScreen`` at a project whose kstrl.toml holds
``[evolution] lookback_runs = "many"`` raised

    ValueError: invalid literal for int() with base 10: 'many'

out of ``EvolveScreen.on_mount`` -> ``reload`` ->
``_load_patterns_and_trends`` -> ``EvolutionConfig.load``, which in a
real shell is an unhandled exception in a message handler and takes the
app down. ``ks evolve --root <same dir>`` printed one named line and
exited 1 for the same file. ``InboxScreen`` had the same shape on a
malformed document.

The screens are reachable because the entry check DEGRADES
``[evolution]``: it warns and lets the shell open (measured - see
``test_the_shell_opens_on_the_value_that_crashes_the_screen``), and the
evolve screen is the screen that section is about.
"""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import DataTable, Static

from kstrl.config import ConfigError
from kstrl.config_preflight import (
    collect_config_problems,
    config_problem_lines,
    load_or_report,
    preflight_config,
    raise_if_defect,
)
from kstrl.config_report import build_config_report
from kstrl.evolution import EvolutionConfig
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.launch import FactoryLaunch
from kstrl.timeout import TimeoutConfig
from kstrl.tui.config_guard import env_scrub_is_safe
from kstrl.tui.screens.inbox import InboxScreen
from kstrl.tui.screens.init_wizard import _detected_text
from kstrl.tui.session import LaunchError, start_run_session
from kstrl.tui.widgets.config_problem import ConfigProblemBanner
from tests.helpers.tui_screens import evolve_screen, home_app
from tests.spine_utils import make_manifest

BAD_KNOB = '[evolution]\nlookback_runs = "many"\n'
BAD_DOCUMENT = "[evolution\nlookback_runs = 5\n"
GOOD_KNOB = "[evolution]\nlookback_runs = 5\n"


class _Harness(App[None]):
    """A bare app: no run_context, so the env sweep is allowed."""

    def compose(self) -> ComposeResult:
        yield from ()


def _banner_text(screen: Screen[Any]) -> str:
    return str(screen.query_one(ConfigProblemBanner).render())


@asynccontextmanager
async def _inbox(tmp_path: Path) -> AsyncIterator[tuple[InboxScreen, Pilot[None]]]:
    """The inbox screen open on ``tmp_path``.

    A bare pause, mirroring ``tests/test_inbox.py``: this screen mounts
    its table directly, with no tab panes to wait for.
    """
    app = _Harness()
    async with app.run_test() as pilot:
        await app.push_screen(InboxScreen(tmp_path))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, InboxScreen)
        yield screen, pilot


# --------------------------------------------------------------------------
# Why the screen is reachable at all
# --------------------------------------------------------------------------
def test_the_shell_opens_on_the_value_that_crashes_the_screen(tmp_path: Path) -> None:
    """The entry check warns for [evolution] and lets the shell open.

    This is the whole reason #289 is not covered by #272: the seam ran,
    and classified this section as degrading.
    """
    (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
    warnings: list[str] = []
    preflight_config(tmp_path, warn=warnings.append)
    assert len(warnings) == 1
    assert "[evolution]" in warnings[0]
    assert "continuing without it" in warnings[0]


def test_ks_evolve_still_stops_on_the_same_file(tmp_path: Path) -> None:
    """The command that IS the journal keeps its fatal classification."""
    (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        preflight_config(tmp_path, warn=lambda _m: None, required=frozenset({"evolution"}))
    assert "[evolution]" in str(caught.value)


# --------------------------------------------------------------------------
# The evolve screen
# --------------------------------------------------------------------------
class TestEvolveScreen:
    async def test_a_bad_knob_is_named_instead_of_raised(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            text = _banner_text(screen)
            assert "configuration unreadable" in text
            # The section name survives: it is bracketed, and Rich reads
            # a bracketed token as markup and deletes it, so this pins
            # the banner rendering a Text rather than a str.
            assert "[evolution]" in text
            assert "invalid literal for int() with base 10: 'many'" in text
            assert "lookback_runs" in text
            assert screen.query_one(ConfigProblemBanner).display is True

    async def test_the_tables_are_empty_and_the_emptiness_is_explained(
        self,
        tmp_path: Path,
    ) -> None:
        """Not a silent degrade: no rows, and a line saying why."""
        (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert screen.query_one("#patterns-table", DataTable).row_count == 0
            assert screen.query_one("#trends-table", DataTable).row_count == 0
            assert "[evolution]" in _banner_text(screen)

    async def test_a_malformed_document_is_named_too(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            text = _banner_text(screen)
            assert "Invalid TOML" in text
            assert "line 1" in text

    async def test_the_environment_variable_is_named_when_it_is_the_cause(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "lots")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert "set by KSTRL_EVOLUTION_LOOKBACK_RUNS=lots" in _banner_text(screen)

    async def test_the_banner_is_hidden_on_a_config_that_resolves(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(GOOD_KNOB, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert screen.query_one(ConfigProblemBanner).display is False
            assert screen.query_one(ConfigProblemBanner).problem is None

    async def test_reload_clears_the_banner_once_the_file_is_repaired(
        self,
        tmp_path: Path,
    ) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(BAD_KNOB, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, pilot):
            assert screen.query_one(ConfigProblemBanner).display is True
            toml.write_text(GOOD_KNOB, encoding="utf-8")
            screen.action_reload()
            await pilot.pause(0.2)
            assert screen.query_one(ConfigProblemBanner).display is False


# --------------------------------------------------------------------------
# The inbox screen: the same shape, found by the survey #289 asked for
# --------------------------------------------------------------------------
class TestInboxScreen:
    async def test_a_malformed_document_is_named_instead_of_raised(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
        async with _inbox(tmp_path) as (screen, _pilot):
            text = _banner_text(screen)
            assert "configuration unreadable" in text
            assert "[inbox]" in text
            assert "Invalid TOML" in text

    async def test_it_never_claims_the_inbox_is_clear(self, tmp_path: Path) -> None:
        """An item IS waiting; the config is what cannot be read.

        "Inbox clear: nothing is waiting on you." from an empty list
        that only means the config failed is the silent degrade #289
        rules out, and here it would be false as well as silent.
        """
        Inbox(tmp_path, InboxConfig()).add(
            ItemKind.POLICY_EXCEPTION,
            "comp-a: denied path",
            component="comp-a",
            dedupe_key="p1",
        )
        (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
        async with _inbox(tmp_path) as (screen, _pilot):
            assert screen._items == []
            detail = str(screen.query_one("#inbox-detail", Static).render())
            assert "Inbox clear" not in detail

    async def test_a_decision_taken_after_the_file_broke_redraws(
        self,
        tmp_path: Path,
    ) -> None:
        """The list was drawn from a good file; the file then broke."""
        Inbox(tmp_path, InboxConfig()).add(
            ItemKind.POLICY_EXCEPTION,
            "comp-a: denied path",
            component="comp-a",
            dedupe_key="p1",
        )
        async with _inbox(tmp_path) as (screen, pilot):
            assert len(screen._items) == 1
            (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
            screen.action_approve()
            await pilot.pause()
            assert screen._items == []
            assert "Invalid TOML" in _banner_text(screen)
            # The decision did NOT land: refusing is the honest answer
            # when the file that configures the log will not parse.
            assert len(Inbox(tmp_path, InboxConfig()).open_items()) == 1


# --------------------------------------------------------------------------
# The shared pattern
# --------------------------------------------------------------------------
class TestSharedGuard:
    def test_the_screen_says_exactly_what_the_command_says(self, tmp_path: Path) -> None:
        """The point of #289: one file, one wording, two surfaces."""
        (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
        from_cli = collect_config_problems(
            tmp_path,
            warn=lambda _m: None,
            required=frozenset({"evolution"}),
        )
        _config, from_screen = load_or_report(
            EvolutionConfig.load,
            tmp_path,
            blame_env=True,
        )
        assert from_cli == [from_screen]

    def test_env_blame_is_skipped_while_a_run_is_in_flight(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Measuring the blame clears os.environ process-wide.

        A launched home-shell session runs the factory on another
        thread of THIS process and its subprocesses inherit the
        environment, so the variable's name is given up rather than the
        run corrupted. Everything else in the line survives.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "lots")
        _config, problem = load_or_report(EvolutionConfig.load, tmp_path, blame_env=False)
        assert problem is not None
        assert "[evolution]" in problem
        assert "invalid literal for int() with base 10: 'lots'" in problem
        assert "set by" not in problem

        _again, blamed = load_or_report(EvolutionConfig.load, tmp_path, blame_env=True)
        assert blamed is not None
        assert "set by KSTRL_EVOLUTION_LOOKBACK_RUNS=lots" in blamed

    def test_env_scrub_is_safe_reads_the_launched_session(self) -> None:
        class _Handle:
            def __init__(self, done: bool) -> None:
                self._done = done

            def done(self) -> bool:
                return self._done

        class _Ctx:
            def __init__(self, handle: object | None) -> None:
                self.handle = handle

        class _App:
            def __init__(self, ctx: object | None) -> None:
                self.run_context = ctx

        assert env_scrub_is_safe(object()) is True  # no attribute at all
        assert env_scrub_is_safe(_App(None)) is True
        assert env_scrub_is_safe(_App(_Ctx(None))) is True
        assert env_scrub_is_safe(_App(_Ctx(_Handle(True)))) is True
        assert env_scrub_is_safe(_App(_Ctx(_Handle(False)))) is False

    def test_a_clean_config_returns_the_object_and_no_problem(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[evolution]\nlookback_runs = 3\n", encoding="utf-8")
        config, problem = load_or_report(EvolutionConfig.load, tmp_path, blame_env=True)
        assert problem is None
        assert config is not None
        assert config.lookback_runs == 3

    def test_an_unenrolled_loader_is_a_defect_not_a_message(self, tmp_path: Path) -> None:
        """The label comes from config_sections(), so a screen cannot
        invent a section the entry check does not know.

        Raised on the FAILURE path only: the lookup moved into the
        except branch because `config_sections()` costs a measured
        6.2 ms on its first call and the happy path never needs a
        label. An unenrolled loader that resolves cleanly has nothing
        to mislabel.
        """

        def _rejecting_unenrolled_loader(_root: Path) -> int:
            raise ValueError("nope")

        with pytest.raises(LookupError):
            load_or_report(_rejecting_unenrolled_loader, tmp_path, blame_env=False)

        def _clean_unenrolled_loader(_root: Path) -> int:
            return 1

        assert load_or_report(_clean_unenrolled_loader, tmp_path, blame_env=False) == (1, None)

    def test_a_bare_runtimeerror_is_re_raised_not_blamed_on_the_operator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-one review, finding 10.

        SURFACE_REJECTIONS names RuntimeError for the domain errors
        that DERIVE from it, which are operator input. A bare one is a
        kstrl defect, and rendering it as "configuration unreadable"
        would blame the operator's file and eat the traceback. This is
        the same line EvolutionConfig.load_or_none draws.
        """
        import kstrl.evolution

        def _explode(root_dir: Path | None = None) -> None:
            raise RuntimeError("journal config exploded")

        monkeypatch.setattr(kstrl.evolution.EvolutionConfig, "load", _explode)
        with pytest.raises(RuntimeError, match="journal config exploded"):
            load_or_report(EvolutionConfig.load, tmp_path, blame_env=False)

    @pytest.mark.parametrize(
        "defect",
        [
            RuntimeError("bare"),
            NotImplementedError("abstract loader"),
            RecursionError("cycle in the config graph"),
        ],
        ids=["bare", "not_implemented", "recursion"],
    )
    def test_every_runtimeerror_kstrl_did_not_define_is_re_raised(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        defect: RuntimeError,
    ) -> None:
        """Round-two review, finding 2.

        The first cut of the test above wrote ``type(exc) is
        RuntimeError``, which is true only of a BARE one.
        NotImplementedError and RecursionError are direct RuntimeError
        subclasses and unambiguously defects, and both were reported to
        the operator as "configuration unreadable" with the traceback
        eaten. RecursionError is the one that hurts: a cycle in the
        config graph would have been rendered as their broken file.
        """
        import kstrl.evolution

        def _explode(root_dir: Path | None = None) -> None:
            raise defect

        monkeypatch.setattr(kstrl.evolution.EvolutionConfig, "load", _explode)
        with pytest.raises(type(defect)):
            load_or_report(EvolutionConfig.load, tmp_path, blame_env=False)

    def test_the_domain_rule_is_derived_from_kstrl_not_listed(self) -> None:
        """No ledger to go stale.

        The reviewer's suggested fix was a tuple of the four domain
        errors named in REJECTIONS' docstring. kstrl defines TEN
        RuntimeError subclasses, so that tuple would have been wrong on
        the day it was written and staler with each new one. The rule
        asks a derivable question instead - did kstrl define this class
        - and this test walks the package to check the answer holds for
        every one of them.
        """
        import ast
        import importlib

        src = Path(__file__).resolve().parent.parent / "kstrl"
        subclasses: list[type[BaseException]] = []
        for path in sorted(src.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            module = path.relative_to(src.parent).with_suffix("").as_posix().replace("/", ".")
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not any(
                    isinstance(base, ast.Name) and base.id == "RuntimeError" for base in node.bases
                ):
                    continue
                subclasses.append(getattr(importlib.import_module(module), node.name))
        assert len(subclasses) >= 10, subclasses
        for cls in subclasses:
            raise_if_defect(cls("operator input"))  # must not raise
        for defect in (RuntimeError, NotImplementedError, RecursionError):
            with pytest.raises(defect):
                raise_if_defect(defect("ours"))
        # And nothing outside RuntimeError is this rule's business.
        raise_if_defect(ValueError("a knob"))
        raise_if_defect(OSError("a file"))

    def test_a_domain_runtimeerror_is_still_reported(self, tmp_path: Path) -> None:
        """The other side of the same line: ServeError and friends are
        operator input and must still reach the banner."""
        from kstrl.serve import ServeConfig

        (tmp_path / "kstrl.toml").write_text(
            "[serve]\nmax_consecutive_poison = 0\n",
            encoding="utf-8",
        )
        _config, problem = load_or_report(ServeConfig.load, tmp_path, blame_env=False)
        assert problem is not None
        assert "[serve]" in problem

    @pytest.mark.skipif(os.name == "nt" or os.getuid() == 0, reason="root reads a 0000 file")
    def test_an_unreadable_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        """OSError is in scope for a surface, not for the entry check.

        The entry check reads the document itself before any loader
        runs; a screen re-reading minutes later has no such pass in
        front of it, and a chmod between two refreshes lands here.
        """
        toml = tmp_path / "kstrl.toml"
        toml.write_text(GOOD_KNOB, encoding="utf-8")
        toml.chmod(0o000)
        try:
            _config, problem = load_or_report(
                EvolutionConfig.load,
                tmp_path,
                blame_env=False,
            )
        finally:
            toml.chmod(stat.S_IRUSR | stat.S_IWUSR)
        assert problem is not None
        assert "[evolution]" in problem
        assert "Permission denied" in problem


# --------------------------------------------------------------------------
# The rest of the survey #289 asked for: guards NARROWER than the seam
# --------------------------------------------------------------------------
#: A toml array where a number belongs. ``float()`` and ``int()`` raise
#: TypeError for it, not ValueError, which is why the entry check's
#: REJECTIONS names TypeError and why a hand-written
#: ``(OSError, ValueError)`` guard is not equivalent to it.
ARRAY_FOR_A_NUMBER = '[timeout]\ngit_operation = ["30"]\n'
ARRAY_FOR_A_RUN_NUMBER = '[run]\nmax_iterations = ["3"]\n'


def test_the_array_case_really_is_a_typeerror(tmp_path: Path) -> None:
    """The premise, measured rather than assumed."""
    (tmp_path / "kstrl.toml").write_text(ARRAY_FOR_A_NUMBER, encoding="utf-8")
    with pytest.raises(TypeError):
        TimeoutConfig.load(tmp_path)
    assert not isinstance(TypeError(), ValueError)


def test_a_launched_run_reports_the_array_instead_of_raising(tmp_path: Path) -> None:
    """``kstrl/tui/session.py`` assembles seven sections at launch.

    Its guard was ``(OSError, ValueError)``, so this file escaped as a
    TypeError and took the shell down. Reachable only by editing
    kstrl.toml with the shell already open: the entry check would have
    refused to open it otherwise.
    """
    manifest_dir = tmp_path / "scripts" / "kstrl"
    manifest_dir.mkdir(parents=True)
    make_manifest([]).save(manifest_dir / "manifest.json")
    (tmp_path / "kstrl.toml").write_text(ARRAY_FOR_A_NUMBER, encoding="utf-8")

    with pytest.raises(LaunchError, match="failed to load configuration"):
        start_run_session(FactoryLaunch(), tmp_path)


def test_the_init_wizard_reports_the_array_instead_of_raising(tmp_path: Path) -> None:
    """The wizard is the screen an operator opens to repair a scaffold.

    Its guard was one exception narrower than the entry check too.
    """
    (tmp_path / "kstrl.toml").write_text(
        '[verify]\nself_critique_min_bullets = ["3"]\n',
        encoding="utf-8",
    )
    assert "kstrl.toml is unreadable" in _detected_text(tmp_path).plain


def test_build_config_report_raises_what_its_tui_callers_now_catch(tmp_path: Path) -> None:
    """Both TUI readers of the report caught only ValueError.

    The config screen's refresh action is the live one: it exists to
    re-read a file the operator has just edited, so it is the surface
    that meets a broken document with the shell already open. The walk
    below is what stops a third reader repeating it.
    """
    (tmp_path / "kstrl.toml").write_text(ARRAY_FOR_A_RUN_NUMBER, encoding="utf-8")
    with pytest.raises(TypeError):
        build_config_report(tmp_path)


# --------------------------------------------------------------------------
# Round-one review findings, each with the measurement that found it
# --------------------------------------------------------------------------
def test_ks_config_show_names_the_section_instead_of_a_traceback(tmp_path: Path) -> None:
    """Finding 1. `config` is preflight-EXEMPT, which is exactly why its
    own guard has to be the shared tuple: nothing catches it first."""
    from click.testing import CliRunner

    from kstrl.cli import cli

    (tmp_path / "kstrl.toml").write_text(ARRAY_FOR_A_RUN_NUMBER, encoding="utf-8")
    result = CliRunner().invoke(cli, ["config", "show", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "[run]" in result.output


def test_the_env_scrub_predicate_sees_both_launched_run_handles() -> None:
    """Round 1 finding 3, and the correction round 2 made to finding 2.

    An embedded orchestrator hangs off a different attribute than a
    home-launched session, so a predicate that reads only run_context
    is not a guard, it is a lie with a docstring. Both handles are read.

    The app's own 5-second safe-mode worker is NOT one of them, and the
    reason is the difference between refusal and serialization: a
    launched run spawns subprocesses that inherit the environment at
    exec and cannot wait on a lock, so the scrub must be refused while
    one is live; the safe-mode worker is our own bounded thread and can
    simply take the lock. Round 1 put it in the refusal condition
    instead, and the measured cost was that the flag is set for 51 to
    84 ms of every 5 s tick on an empty project, which made the config
    screen's refresh and the banner's env blame intermittent on a
    machine with no run at all.
    """

    class _Handle:
        def __init__(self, done: bool) -> None:
            self._done = done

        def done(self) -> bool:
            return self._done

    class _Ctx:
        def __init__(self, handle: object | None) -> None:
            self.handle = handle

    class _App:
        def __init__(
            self,
            ctx: object | None = None,
            orchestrator: object | None = None,
            safe_mode: bool = False,
        ) -> None:
            self.run_context = ctx
            self.orchestrator = orchestrator
            self._safe_mode_running = safe_mode

    assert env_scrub_is_safe(_App()) is True
    assert env_scrub_is_safe(_App(ctx=_Ctx(_Handle(True)))) is True
    assert env_scrub_is_safe(_App(ctx=_Ctx(_Handle(False)))) is False
    # Finding 3: EMBEDDED mode keeps the live handle here, not on
    # run_context, whose handle stays None (RunContext.observe).
    assert env_scrub_is_safe(_App(orchestrator=_Handle(False))) is False
    assert env_scrub_is_safe(_App(orchestrator=_Handle(True))) is True
    # Round 2 findings 3 and 4: the app's own safe-mode worker is
    # serialized against the scrub, not refused around it.
    assert env_scrub_is_safe(_App(safe_mode=True)) is True


def test_the_safe_mode_worker_takes_the_lock_the_scrub_holds() -> None:
    """The mechanism that replaced round 1's refusal clause.

    The worker still reads KSTRL_* every 5 seconds in every mode, so
    the race round 1 found is real. It is closed by serialization now:
    both config loads in safemode.py sit inside ``environ_lock()``, the
    same reentrant lock ``scrubbed_environ`` holds while os.environ is
    empty, so the worker waits a few milliseconds instead of the config
    screen refusing to work for the duration of the flag.
    """
    src = Path(__file__).resolve().parent.parent / "kstrl"
    safemode = (src / "safemode.py").read_text(encoding="utf-8")
    assert "AutonomyConfig.load" in safemode
    assert "PolicyConfig.load" in safemode
    assert "QueueConfig.load" in safemode
    assert safemode.count("with environ_lock():") == 2
    report = (src / "config_report.py").read_text(encoding="utf-8")
    assert "_ENVIRON_LOCK = threading.RLock()" in report
    app = (src / "tui" / "app.py").read_text(encoding="utf-8")
    assert "SAFE_MODE_INTERVAL_SECONDS" in app
    assert "self._safe_mode_running = True" in app


async def test_the_config_screen_refresh_reports_the_seam_wording(tmp_path: Path) -> None:
    """Finding 8.

    This is the screen an operator opens to look at configuration, so
    "int() argument must be a string ... not 'list'" with no section,
    key or value is the one place that answer is least useful. The
    notify now carries collect_config_problems' lines, which is the
    same traversal `ks config show` prints.
    """
    from kstrl.tui.screens.config import ConfigScreen

    (tmp_path / "kstrl.toml").write_text(ARRAY_FOR_A_RUN_NUMBER, encoding="utf-8")
    app = home_app(tmp_path)
    notices: list[str] = []
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(ConfigScreen())
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, ConfigScreen)
        app.notify = lambda message, **kw: notices.append(str(message))  # type: ignore[method-assign,assignment]
        screen.action_refresh()
        await pilot.pause(0.2)
    assert notices, "refresh reported nothing"
    assert any("[run]" in n for n in notices), notices


def test_the_config_screen_and_ks_config_show_share_one_problem_reporter() -> None:
    """Round-two review, finding 10.

    The screen carried its own ``_problem_lines`` next to the CLI's
    ``_problems``: same traversal, same fallback, two copies, and they
    had already drifted on the empty case. One function now, and the
    only thing either caller supplies is where warnings go.
    """
    src = Path(__file__).resolve().parent.parent / "kstrl"
    screen = (src / "tui" / "screens" / "config.py").read_text(encoding="utf-8")
    cli = (src / "cli.py").read_text(encoding="utf-8")
    assert "def _problem_lines" not in screen
    assert "def _problems()" not in cli
    assert "config_problem_lines(root_dir, warn=" in screen
    assert "config_problem_lines(root_dir, warn=" in cli


def test_the_entry_check_does_not_list_our_own_defect_as_a_config_problem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The seam all three reporting surfaces route through.

    ``ks config show``, the TUI config screen and the entry check
    itself all reach ``collect_config_problems``, so a RuntimeError
    kstrl never defined being listed there as the operator's broken
    file is the same defect as findings 2 and 6, one call deeper and
    reachable from all of them.
    """
    import kstrl.evolution

    def _explode(root_dir: Path | None = None) -> None:
        raise NotImplementedError("a loader we forgot to finish")

    monkeypatch.setattr(kstrl.evolution.EvolutionConfig, "load", _explode)
    with pytest.raises(NotImplementedError):
        collect_config_problems(tmp_path, warn=lambda _m: None)
    with pytest.raises(NotImplementedError):
        config_problem_lines(tmp_path, warn=lambda _m: None)


def test_the_shared_reporter_gives_the_document_error_when_nothing_parses(
    tmp_path: Path,
) -> None:
    """The case the two copies had drifted on."""
    (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
    lines = config_problem_lines(tmp_path, warn=lambda _m: None)
    assert len(lines) == 1
    assert "kstrl.toml" in lines[0]


def test_a_report_survives_a_section_whose_loader_hits_the_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-two review, finding 9.

    ``build_config_report._resolve`` caught the ENTRY set, which has no
    OSError in it, so an unreadable file met while resolving one phase
    section killed the whole report - on the one screen whose job is
    showing configuration, the same defect #272 removed for coercion
    errors. A rejected section costs its rows and is named in
    ``unresolved``; it does not cost the report.
    """
    import kstrl.config_report

    real = kstrl.config_report._phase_sections

    def _one_unreadable_section() -> list[tuple[str, object, list[str]]]:
        sections = real()
        name, _loader, knobs = sections[0]

        def _unreadable(_root: Path) -> object:
            raise PermissionError(13, "Permission denied")

        return [(name, _unreadable, knobs), *sections[1:]]

    monkeypatch.setattr(kstrl.config_report, "_phase_sections", _one_unreadable_section)
    report = build_config_report(tmp_path)
    assert report.unresolved == (real()[0][0],)
    assert report.rows, "the rest of the report was lost with the section"
