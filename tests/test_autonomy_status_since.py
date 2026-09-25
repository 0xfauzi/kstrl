"""`ks autonomy status` shows a ladder record only when one exists (#484).

With no state file, ``AutonomyState.load`` returns a fresh default whose
``since`` is the time of the call. Printing it made every invocation
report a different start time for evidence that was never recorded.
The command now prints ``-`` unless a record was read from disk, and
says in one sentence that nothing is recorded while the ladder is off.

``_utc_now_iso`` is bound into the dataclass as its ``default_factory``
when the class is created, so patching the module attribute does not
reach ``AutonomyState()``. The clock fixture replaces the ``datetime``
name the function reads instead, and checks that it took effect.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import warnings
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest
from click.testing import CliRunner

import kstrl.autonomy as autonomy_module
from kstrl.autonomy import AutonomyState
from kstrl.cli import cli
from kstrl.statedir import CONTROL_AUTONOMY, legacy_control_paths
from tests.helpers.gitrepo import git_in

DISABLED_NOTE = "Evidence is not recorded while [autonomy] enabled = false."
SAVED_SINCE = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    git_in(root, "init", "-q")
    return root


@pytest.fixture
def ticking_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``datetime.now`` in kstrl.autonomy is one second after the last."""
    ticks = itertools.count()

    class _Ticking(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
            return datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=next(ticks))

    monkeypatch.setattr(autonomy_module, "datetime", _Ticking)
    # Control: without this the identical-output test could pass because
    # the clock never moved, not because the default is no longer printed.
    assert AutonomyState().since != AutonomyState().since


def _status(root: Path) -> str:
    result = CliRunner().invoke(
        cli,
        ["autonomy", "status", "--root", str(root), "--ui", "plain", "--no-color"],
    )
    assert result.exit_code == 0, result.output
    return result.output


def _value(output: str, key: str) -> str:
    """The value of the one ``key: value`` line in ``output``."""
    lines = [line for line in output.splitlines() if line.strip().startswith(f"{key}:")]
    assert len(lines) == 1, output
    return lines[0].split(":", 1)[1].strip()


def _since(output: str) -> str:
    return _value(output, "since")


def test_a_fresh_repo_prints_the_same_status_twice(repo: Path, ticking_clock: None) -> None:
    first = _status(repo)
    second = _status(repo)
    assert first == second
    assert _since(first) == "-"
    assert not AutonomyState.path_for(repo).exists()


def test_the_disabled_note_appears_exactly_once(repo: Path) -> None:
    output = _status(repo)
    assert output.count(DISABLED_NOTE) == 1, output
    assert DISABLED_NOTE in [line.strip() for line in output.splitlines()], output


@pytest.mark.parametrize("source", ["env", "toml"])
def test_an_enabled_ladder_with_no_record_prints_a_dash_and_no_note(
    repo: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    if source == "env":
        monkeypatch.setenv("KSTRL_AUTONOMY_ENABLED", "1")
    else:
        (repo / "kstrl.toml").write_text("[autonomy]\nenabled = true\n", encoding="utf-8")
    output = _status(repo)
    assert _value(output, "enabled") == "yes", output
    assert _since(output) == "-"
    assert DISABLED_NOTE not in output


@pytest.mark.parametrize("enabled", [False, True])
def test_a_saved_record_prints_its_own_since(
    repo: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    assert AutonomyState(since=SAVED_SINCE).save(repo) is None
    if enabled:
        monkeypatch.setenv("KSTRL_AUTONOMY_ENABLED", "1")
    output = _status(repo)
    assert _since(output) == SAVED_SINCE
    assert output.count(DISABLED_NOTE) == (0 if enabled else 1), output


def test_a_legacy_in_tree_record_prints_its_own_since(repo: Path) -> None:
    assert AutonomyState(since=SAVED_SINCE).save(repo) is None
    legacy = legacy_control_paths(repo)[CONTROL_AUTONOMY]
    legacy.parent.mkdir(parents=True, exist_ok=True)
    AutonomyState.path_for(repo).replace(legacy)
    with pytest.warns(DeprecationWarning, match="relocated control file"):
        output = _status(repo)
    assert _since(output) == SAVED_SINCE
    assert AutonomyState.path_for(repo).exists()
    assert not legacy.exists()


def test_a_record_load_discarded_prints_a_dash(repo: Path, ticking_clock: None) -> None:
    path = AutonomyState.path_for(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        first = _status(repo)
        second = _status(repo)
    assert _since(first) == "-"
    assert first == second


def test_the_real_cli_on_a_fresh_repo_prints_a_dash_and_the_note(repo: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env.update(KSTRL_NO_TUI="1", KSTRL_AGENT_PROBE="0")
    proc = subprocess.run(
        [sys.executable, "-m", "kstrl", "autonomy", "status", "--ui", "plain"],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert _since(output) == "-"
    assert output.count(DISABLED_NOTE) == 1, output
    assert not AutonomyState.path_for(repo).exists()


def test_the_note_is_the_line_after_since_on_the_same_stream(repo: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env.update(KSTRL_NO_TUI="1", KSTRL_AGENT_PROBE="0")
    proc = subprocess.run(
        [sys.executable, "-m", "kstrl", "autonomy", "status", "--ui", "plain"],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    streams = [s for s in (proc.stdout, proc.stderr) if "since:" in s]
    assert len(streams) == 1, (proc.stdout, proc.stderr)
    lines = [line.strip() for line in streams[0].splitlines()]
    at = next(i for i, line in enumerate(lines) if line.startswith("since:"))
    assert lines[at + 1 : at + 2] == [DISABLED_NOTE], streams[0]
