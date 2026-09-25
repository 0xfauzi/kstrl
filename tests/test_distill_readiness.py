"""#495: the ``ks evolve`` readiness line that counts distiller replies
that did not parse, read from ``DistillResult.parse_failed`` on the
event stream."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kstrl import distill_readiness
from kstrl import events as ev

OLDER = "factory-20260101-120000.000000-aaaaaa"
NEWER = "factory-20260201-120000.000000-bbbbbb"


def _write_run(root: Path, run_id: str, *parse_failed: bool) -> None:
    """A run directory whose events.jsonl holds one DistillResult per flag,
    written through the real sink."""
    run_dir = root / ".kstrl" / "runs" / run_id
    run_dir.mkdir(parents=True)
    bus = ev.EventBus(ev.JsonlSink(run_dir / "events.jsonl"), run_id=run_id)
    for flag in parse_failed:
        bus.emit(ev.DistillResult(component="comp-a", parse_failed=flag))
    bus.close()


def test_the_window_is_the_newest_run_directories(tmp_path: Path) -> None:
    _write_run(tmp_path, OLDER, True, True)
    _write_run(tmp_path, NEWER, True, False)

    assert distill_readiness.distill_parse_failure_line(tmp_path, 1) == (
        "  distill replies that did not parse: 1 of 2 distill(s) in the last 1 run(s)"
    )
    assert distill_readiness.distill_parse_failure_line(tmp_path, 10) == (
        "  distill replies that did not parse: 3 of 4 distill(s) in the last 2 run(s)"
    )


def test_unreadable_runs_directory_is_not_counted_as_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(root_dir: Path) -> list[Path]:
        raise PermissionError(13, "Permission denied", str(root_dir))

    monkeypatch.setattr(distill_readiness, "run_dirs_newest_first", refuse)

    line = distill_readiness.distill_parse_failure_line(tmp_path, 10)

    assert line.startswith("  distill replies that did not parse: not counted, ")
    assert " 0 of " not in line


def test_ks_evolve_uses_the_configured_lookback_runs(tmp_path: Path) -> None:
    """The window is ``[evolution] lookback_runs`` read by the real
    ``ks evolve``, not a constant that happens to equal the default 10."""
    _write_run(tmp_path, OLDER, True, True)
    _write_run(tmp_path, NEWER, True, False)
    (tmp_path / "kstrl.toml").write_text("[evolution]\nlookback_runs = 1\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "kstrl", "evolve", "--root", str(tmp_path), "--ui", "plain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout
    assert (
        "distill replies that did not parse: 1 of 2 distill(s) in the last 1 run(s)" in proc.stdout
    )
