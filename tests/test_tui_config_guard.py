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

import ast
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
from kstrl.config_preflight import collect_config_problems, load_or_report, preflight_config
from kstrl.config_report import build_config_report
from kstrl.evolution import EvolutionConfig
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.launch import FactoryLaunch
from kstrl.timeout import TimeoutConfig
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.config_guard import env_scrub_is_safe
from kstrl.tui.screens.evolve import EvolveScreen
from kstrl.tui.screens.inbox import InboxScreen
from kstrl.tui.screens.init_wizard import _detected_text
from kstrl.tui.session import LaunchError, start_run_session
from kstrl.tui.widgets.config_problem import ConfigProblemBanner
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


def _home_app(tmp_path: Path) -> KstrlTuiApp:
    return KstrlTuiApp(root_dir=tmp_path, mode=Mode.HOME, poll_interval=0.05)


@asynccontextmanager
async def _evolve(tmp_path: Path) -> AsyncIterator[tuple[EvolveScreen, Pilot[None]]]:
    """The evolve screen open on ``tmp_path``.

    The 0.2s pauses mirror ``tests/test_evolve_screen.py``, which is
    the empirical precedent for THIS screen: it composes a
    TabbedContent whose panes mount on a later frame, and every test
    there waits the same way.
    """
    app = _home_app(tmp_path)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(EvolveScreen())
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, EvolveScreen)
        yield screen, pilot


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
        async with _evolve(tmp_path) as (screen, _pilot):
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
        async with _evolve(tmp_path) as (screen, _pilot):
            assert screen.query_one("#patterns-table", DataTable).row_count == 0
            assert screen.query_one("#trends-table", DataTable).row_count == 0
            assert "[evolution]" in _banner_text(screen)

    async def test_a_malformed_document_is_named_too(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
        async with _evolve(tmp_path) as (screen, _pilot):
            text = _banner_text(screen)
            assert "Invalid TOML" in text
            assert "line 1" in text

    async def test_the_environment_variable_is_named_when_it_is_the_cause(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "lots")
        async with _evolve(tmp_path) as (screen, _pilot):
            assert "set by KSTRL_EVOLUTION_LOOKBACK_RUNS=lots" in _banner_text(screen)

    async def test_the_banner_is_hidden_on_a_config_that_resolves(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(GOOD_KNOB, encoding="utf-8")
        async with _evolve(tmp_path) as (screen, _pilot):
            assert screen.query_one(ConfigProblemBanner).display is False
            assert screen.query_one(ConfigProblemBanner).problem is None

    async def test_reload_clears_the_banner_once_the_file_is_repaired(
        self,
        tmp_path: Path,
    ) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(BAD_KNOB, encoding="utf-8")
        async with _evolve(tmp_path) as (screen, pilot):
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
        invent a section the entry check does not know."""

        def _not_a_registered_loader(_root: Path) -> int:
            return 1

        with pytest.raises(LookupError):
            load_or_report(_not_a_registered_loader, tmp_path, blame_env=False)

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
# The mechanism, not the memory
# --------------------------------------------------------------------------
#: Sites that resolve config in kstrl/tui/ and are deliberately not
#: routed through the banner, keyed by (file, enclosing function). Each
#: still has to catch SURFACE_REJECTIONS; this only records that the
#: banner is not the right renderer for it.
_NOT_THE_BANNER = frozenset(
    {
        # Reads pyproject.toml through resolve_verify_commands, and
        # renders its own labelled row inside a block with no space for
        # a section and a value.
        ("kstrl/tui/screens/init_wizard.py", "_detected_text"),
    }
)


def _guarded_by_the_shared_tuple(node: ast.Try) -> bool:
    for handler in node.handlers:
        if handler.type is None:
            continue
        for name in ast.walk(handler.type):
            if isinstance(name, ast.Name) and name.id == "SURFACE_REJECTIONS":
                return True
    return False


def _config_loads(tree: ast.AST) -> list[ast.Call]:
    """Calls that resolve a kstrl.toml section from disk."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "build_config_report":
            found.append(node)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "load"
            and isinstance(func.value, ast.Name)
            and func.value.id.endswith("Config")
        ):
            found.append(node)
    return found


def _guarded_call_ids(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Ids of the config loads inside a try that catches the tuple."""
    guarded: set[int] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Try) and _guarded_by_the_shared_tuple(node):
            guarded.update(id(call) for call in _config_loads(node))
    return guarded


def _unguarded_config_loads(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, enclosing function) for every unguarded config load."""
    found: list[tuple[int, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        guarded = _guarded_call_ids(function)
        found += [
            (call.lineno, function.name)
            for call in _config_loads(function)
            if id(call) not in guarded
        ]
    return found


def test_no_tui_surface_loads_config_behind_a_hand_written_guard() -> None:
    """The house pattern is a walk, not a memory (CLAUDE.md).

    Every site the #289 survey found was a hand-written exception tuple
    that had drifted one exception narrower than the entry check, and
    two of them were missed on the first pass of this very fix. So the
    rule is enforced rather than remembered: inside kstrl/tui/, a call
    that resolves a config section must either go through the banner's
    ``load`` (which routes to ``config_preflight.load_or_report``) or
    sit inside a ``try`` that catches ``SURFACE_REJECTIONS``.
    """
    root = Path(__file__).resolve().parent.parent
    offenders = [
        f"{path.relative_to(root).as_posix()}:{lineno} in {name}()"
        for path in sorted((root / "kstrl" / "tui").rglob("*.py"))
        for lineno, name in _unguarded_config_loads(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == [], (
        "config resolved in kstrl/tui/ without SURFACE_REJECTIONS or the banner: "
        + ", ".join(offenders)
    )


def test_every_named_exception_to_the_banner_still_exists() -> None:
    """An allow-list nobody prunes is a lie. These are guarded by
    SURFACE_REJECTIONS and render their own message instead."""
    root = Path(__file__).resolve().parent.parent
    for rel, function in _NOT_THE_BANNER:
        source = (root / rel).read_text(encoding="utf-8")
        assert f"def {function}(" in source, f"{rel} no longer defines {function}"
        assert "SURFACE_REJECTIONS" in source, f"{rel} no longer imports the shared tuple"


def test_the_walk_would_have_caught_the_original_defect() -> None:
    """The walk is only worth having if it fails on the bug."""
    unguarded = ast.parse(
        "def reload(self):\n    journal = EvolutionJournal(EvolutionConfig.load(root_dir))\n"
    ).body[0]
    assert isinstance(unguarded, ast.FunctionDef)
    assert len(_config_loads(unguarded)) == 1

    guarded = ast.parse(
        "def reload(self):\n"
        "    try:\n"
        "        return EvolutionConfig.load(root_dir)\n"
        "    except SURFACE_REJECTIONS:\n"
        "        return None\n"
    ).body[0]
    assert isinstance(guarded, ast.FunctionDef)
    tries = [n for n in ast.walk(guarded) if isinstance(n, ast.Try)]
    assert _guarded_by_the_shared_tuple(tries[0]) is True
    assert len(_config_loads(tries[0])) == 1
