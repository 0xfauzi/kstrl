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

import pytest

from kstrl.cli.config_preflight import collect_config_problems, config_problem_lines
from kstrl.cli.config_report import build_config_report
from kstrl.proc.timeout import TimeoutConfig
from tests.helpers import astwalk

#: A malformed document, as opposed to a bad value inside a good one.
BAD_DOCUMENT = "[evolution\nlookback_runs = 5\n"

#: A toml array where a number belongs. ``float()`` and ``int()`` raise
#: TypeError for it, not ValueError, which is why the entry check's
#: REJECTIONS names TypeError and why a hand-written
#: ``(OSError, ValueError)`` guard is not equivalent to it.
ARRAY_FOR_A_NUMBER = '[timeout]\nagent_iteration = ["30"]\n'
ARRAY_FOR_A_RUN_NUMBER = '[run]\nmax_iterations = ["3"]\n'


def test_the_array_case_really_is_a_typeerror(tmp_path: Path) -> None:
    """The premise, measured rather than assumed."""
    (tmp_path / "kstrl.toml").write_text(ARRAY_FOR_A_NUMBER, encoding="utf-8")
    with pytest.raises(TypeError):
        TimeoutConfig.load(tmp_path)
    assert not isinstance(TypeError(), ValueError)


def test_build_config_report_raises_what_its_callers_catch(tmp_path: Path) -> None:
    """The two TUI readers of the report caught only ValueError.

    They went with the TUI (#863 A1). The walk below is what stops a
    new reader repeating it.
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

    from kstrl.cli.cli import cli

    (tmp_path / "kstrl.toml").write_text(ARRAY_FOR_A_RUN_NUMBER, encoding="utf-8")
    result = CliRunner().invoke(cli, ["config", "show", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "[run]" in result.output


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
    safemode = (src / "cli/safemode.py").read_text(encoding="utf-8")
    assert "AutonomyConfig.load" in safemode
    assert "PolicyConfig.load" in safemode
    assert "QueueConfig.load" in safemode
    assert safemode.count("with environ_lock():") == 2
    resolve = (src / "config/config_resolve.py").read_text(encoding="utf-8")
    assert "_ENVIRON_LOCK = threading.RLock()" in resolve


def test_ks_config_show_has_one_problem_reporter() -> None:
    """Round-two review, finding 10.

    ``ks config show`` carried its own ``_problems`` next to a second
    surface's ``_problem_lines``: same traversal, same fallback, two
    copies, and they had already drifted on the empty case. One function
    now, and the only thing a caller supplies is where warnings go.
    """
    cli = (astwalk.KSTRL_PACKAGE / "cli/cli_report.py").read_text(encoding="utf-8")
    assert "def _problems()" not in cli
    assert "config_problem_lines(root_dir, warn=" in cli


def test_the_entry_check_does_not_list_our_own_defect_as_a_config_problem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The seam both reporting surfaces route through.

    ``ks config show`` and the entry check itself both reach
    ``collect_config_problems``, so a RuntimeError
    kstrl never defined being listed there as the operator's broken
    file is the same defect as findings 2 and 6, one call deeper and
    reachable from all of them.
    """
    import kstrl.learning.evolution_config

    def _explode(root_dir: Path | None = None) -> None:
        raise NotImplementedError("a loader we forgot to finish")

    monkeypatch.setattr(kstrl.learning.evolution_config.EvolutionConfig, "load", _explode)
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
    import kstrl.cli.config_report

    real = kstrl.cli.config_report._phase_sections

    def _one_unreadable_section() -> list[tuple[str, object]]:
        sections = real()
        name, _loader = sections[0]

        def _unreadable(_root: Path) -> object:
            raise PermissionError(13, "Permission denied")

        return [(name, _unreadable), *sections[1:]]

    monkeypatch.setattr(kstrl.cli.config_report, "_phase_sections", _one_unreadable_section)
    report = build_config_report(tmp_path)
    assert report.unresolved == (real()[0][0],)
    assert report.rows, "the rest of the report was lost with the section"
