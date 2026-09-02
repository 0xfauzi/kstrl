"""#289, the narrower half: guards TIGHTER than the screen seam.

Split out of ``tests/test_tui_config_guard.py``, which the #342 merge
took to 827 lines against this repo's 800-line ratchet. Neither branch
crossed it alone: the base was 723, `main` added 77 to land exactly ON
800, and this branch's 27 pushed it over. The seam is the one the
original file already drew in a section banner, so the split is where
the file said it wanted to be split rather than wherever 800 fell.

The file it came from is about the SCREENS: a home-shell screen must
name a broken config rather than raise on it. This half is about the
guards underneath them - the array-for-a-number case that raises
TypeError rather than ValueError, the env scrub, the shared reporter
that ``ks config show`` and ``ConfigScreen`` both go through, and the
entry check's own defect list.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from textual.widgets import DataTable

from kstrl.config_preflight import collect_config_problems, config_problem_lines
from kstrl.config_report import build_config_report
from kstrl.launch import FactoryLaunch
from kstrl.timeout import TimeoutConfig
from kstrl.tui.config_guard import env_scrub_is_safe
from kstrl.tui.screens.init_wizard import _detected_text
from kstrl.tui.session import LaunchError, start_run_session
from tests.helpers import astwalk
from tests.helpers.settle import mounted, settled
from tests.helpers.tui_screens import home_app
from tests.spine_utils import make_manifest

#: A malformed document, as opposed to a bad value inside a good one.
BAD_DOCUMENT = "[evolution\nlookback_runs = 5\n"

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
    src = astwalk.KSTRL_PACKAGE
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
        # The app installs its home screen from its own on_mount, so a
        # screen pushed before that lands under it and never becomes
        # active. Then ConfigScreen.on_mount adds the columns and
        # renders the report in one synchronous call, so a table with
        # columns is a screen that has finished loading.
        await mounted(pilot, lambda: app.screen, "#home-commands")
        app.push_screen(ConfigScreen())
        table = await mounted(pilot, lambda: app.screen, "#config-table")
        await settled(
            pilot,
            lambda: cast(DataTable, table).columns,
            what="the config screen's on_mount to fill the table",
        )
        screen = app.screen
        assert isinstance(screen, ConfigScreen)
        app.notify = lambda message, **kw: notices.append(str(message))  # type: ignore[method-assign,assignment]
        # action_refresh notifies from inside the call, so `notices`
        # is already final when it returns.
        screen.action_refresh()
    assert notices, "refresh reported nothing"
    assert any("[run]" in n for n in notices), notices


def test_the_config_screen_and_ks_config_show_share_one_problem_reporter() -> None:
    """Round-two review, finding 10.

    The screen carried its own ``_problem_lines`` next to the CLI's
    ``_problems``: same traversal, same fallback, two copies, and they
    had already drifted on the empty case. One function now, and the
    only thing either caller supplies is where warnings go.
    """
    src = astwalk.KSTRL_PACKAGE
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
