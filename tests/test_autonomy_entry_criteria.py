"""`ks autonomy status` and `ks autonomy promote` read every documented entry criterion (#643).

``docs/dark-factory-roadmap.md`` R8.2 lists "calibration compare green"
for L2, "health metrics inside limits" for L3 and "deploy target exists"
for L4. Before #643 nothing read any of the three: status printed
"Criteria met" and promote succeeded with a calibration regression and a
health breach open.

Every test drives the real commands. ``status`` and ``inbox approve`` run
through click's runner, the calibration regression through
``python -m kstrl.calibration compare``'s own ``main``, and ``promote`` in
a subprocess on a real pseudo-terminal, because it refuses any caller
without one.
"""

from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl import calibration
from kstrl.autonomy import AutonomyLevel, AutonomyState
from kstrl.cli import cli
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from tests.helpers.demotion import MISSED_IN_NEW, baseline_pair
from tests.helpers.gitrepo import git_in
from tests.helpers.journal import component_result, journal_at
from tests.helpers.replay import UNDECODABLE_TSV, run_record, write_runs

ENABLED = "[autonomy]\nenabled = true\n[inbox]\nenabled = true\n"
FLAT12 = (0.10, 0.12, 0.09, 0.11, 0.10, 0.13, 0.08, 0.10, 0.11, 0.09, 0.12, 0.10)
DRIFT12 = FLAT12[:9] + (0.90, 0.92, 0.95)
RETRY_BREACH = (
    "health metric outside limits: retry_rate: 1 point beyond 3 sigma "
    "(value 0.9500 beyond limit 0.1471 over 12 run(s))"
)
DEPLOY_UNCHECKED = (
    "deploy target exists: not checked, because the release stage (#154) is not built"
)
PROMOTE_DEADLINE_S = 120.0


def _project(root: Path, level: AutonomyLevel, *, toml: str = ENABLED, merged: int = 50) -> Path:
    """A repo at ``level`` whose ladder counters meet every counter criterion."""
    git_in(root, "init", "-q")
    (root / "kstrl.toml").write_text(toml, encoding="utf-8")
    state = AutonomyState(level=int(level))
    state.decisive_runs_at_level = 8
    state.components_merged_at_level = merged
    state.clean_merges_at_level = 50
    assert state.save(root) is None
    return root


def _history(root: Path, retry_rates: tuple[float, ...]) -> None:
    """One decisive run per rate, each with a cost and a journal infra count of 0."""
    write_runs(
        root,
        [
            run_record(
                run_id=f"run-{index:02d}",
                timestamp=f"2026-09-{index + 1:02d}T00:00:00Z",
                retry_rate=rate,
                total_cost_usd=1.0,
            )
            for index, rate in enumerate(retry_rates)
        ],
    )
    journal_at(root).append_entries(
        [
            component_result(
                f"run-{index:02d}", "comp-a", findings_summary={"infrastructure_errors": 0}
            )
            for index in range(len(retry_rates))
        ]
    )


def _regress_calibration(root: Path, tmp_path: Path) -> str:
    """Open a calibration_drift item the way kstrl does, and return its short id."""
    old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)
    assert calibration.main(["compare", str(old), str(new), "--root", str(root)]) == 1
    items = [
        item
        for item in Inbox(root, InboxConfig.load(root)).open_items()
        if item.kind == ItemKind.CALIBRATION_DRIFT
    ]
    assert len(items) == 1, items
    return items[0].id[:8]


def _status(root: Path) -> str:
    result = CliRunner().invoke(
        cli, ["autonomy", "status", "--root", str(root), "--ui", "plain", "--no-color"]
    )
    assert result.exit_code == 0, result.output
    return result.output


def _blockers(status_output: str) -> list[str]:
    """The ``- `` lines under the Promotion heading, without the dash."""
    section = status_output.split("Promotion", 1)[1]
    return [line.strip()[2:] for line in section.splitlines() if line.strip().startswith("- ")]


def _promote(root: Path, *extra: str) -> tuple[int, str]:
    """Run the real ``ks autonomy promote`` with a pseudo-terminal on stdin and stdout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env.update(KSTRL_NO_TUI="1", KSTRL_AGENT_PROBE="0")
    command = [sys.executable, "-m", "kstrl", "autonomy", "promote", "--actor", "owner"]
    command += ["--ack", "evidence read", "--root", str(root), "--ui", "plain", "--no-color"]
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [*command, *extra],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    os.close(slave)
    chunks: list[bytes] = []
    deadline = time.monotonic() + PROMOTE_DEADLINE_S
    try:
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"promote did not exit within {PROMOTE_DEADLINE_S}s"
            ready, _, _ = select.select([master], [], [], remaining)
            if not ready:
                continue
            try:
                data = os.read(master, 4096)
            except OSError:  # EIO once every slave end is closed (Linux)
                break
            if not data:
                break
            chunks.append(data)
        code = proc.wait(timeout=PROMOTE_DEADLINE_S)
    finally:
        os.close(master)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    return code, b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")


def test_a_calibration_regression_blocks_l1_to_l2_until_its_item_is_decided(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, AutonomyLevel.L1_SUPERVISED)
    item = _regress_calibration(root, tmp_path)

    status = _status(root)
    assert "Criteria met" not in status
    assert _blockers(status) == [
        f"calibration compare not green: 1 undecided calibration_drift inbox item(s): {item}"
    ]
    code, output = _promote(root)
    assert code == 2, output
    assert "calibration compare not green" in output
    assert AutonomyState.load(root).level == int(AutonomyLevel.L1_SUPERVISED)

    approved = CliRunner().invoke(
        cli, ["inbox", "approve", item, "--root", str(root), "--ui", "plain", "--no-color"]
    )
    assert approved.exit_code == 0, approved.output
    assert "Criteria met" in _status(root)
    code, output = _promote(root)
    assert code == 0, output
    assert AutonomyState.load(root).level == int(AutonomyLevel.L2_GATED_MERGE)


def test_a_health_breach_blocks_l2_to_l3(tmp_path: Path) -> None:
    root = _project(tmp_path, AutonomyLevel.L2_GATED_MERGE)
    _history(root, DRIFT12)

    status = _status(root)
    assert "Criteria met" not in status
    assert _blockers(status) == [RETRY_BREACH]
    code, output = _promote(root)
    assert code == 2, output
    assert RETRY_BREACH in output.replace("\n", "")
    assert AutonomyState.load(root).level == int(AutonomyLevel.L2_GATED_MERGE)


@pytest.mark.parametrize(
    ("level", "merged", "expected"),
    [
        (AutonomyLevel.L1_SUPERVISED, 50, []),
        (AutonomyLevel.L1_SUPERVISED, 4, ["4/5 components merged at L1"]),
        (AutonomyLevel.L2_GATED_MERGE, 50, []),
    ],
    ids=["l1_all_met", "l1_counter_unmet", "l2_all_met"],
)
def test_with_both_signals_green_the_counters_decide_as_before(
    tmp_path: Path, level: AutonomyLevel, merged: int, expected: list[str]
) -> None:
    root = _project(tmp_path, level, merged=merged)
    _history(root, FLAT12)

    status = _status(root)
    assert _blockers(status) == expected
    assert ("Criteria met" in status) is (expected == [])
    code, output = _promote(root)
    assert code == (0 if expected == [] else 2), output
    promoted = int(level) + 1 if expected == [] else int(level)
    assert AutonomyState.load(root).level == promoted


def _inbox_off(root: Path) -> None:
    (root / "kstrl.toml").write_text(
        "[autonomy]\nenabled = true\n[inbox]\nenabled = false\n", encoding="utf-8"
    )


def _inbox_undecodable(root: Path) -> None:
    path = Inbox(root, InboxConfig.load(root)).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"id": "\xff\xfe"}\n')


def _autonomy_off(root: Path) -> None:
    (root / "kstrl.toml").write_text(
        "[autonomy]\nenabled = false\n[inbox]\nenabled = true\n", encoding="utf-8"
    )


def _inbox_torn_line(root: Path) -> None:
    path = Inbox(root, InboxConfig.load(root)).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not json\n")


def _history_undecodable(root: Path) -> None:
    path = root / ".kstrl" / "experiments.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(UNDECODABLE_TSV)


def _history_is_directory(root: Path) -> None:
    (root / ".kstrl" / "experiments.tsv").mkdir(parents=True)


def _no_history(root: Path) -> None:
    return None


@pytest.mark.parametrize(
    ("level", "unreadable", "expected"),
    [
        (
            AutonomyLevel.L1_SUPERVISED,
            _inbox_off,
            "calibration compare: not recorded, because a regression opens a "
            "calibration_drift item only while [autonomy] and [inbox] are both enabled",
        ),
        (
            AutonomyLevel.L1_SUPERVISED,
            _autonomy_off,
            "calibration compare: not recorded, because a regression opens a "
            "calibration_drift item only while [autonomy] and [inbox] are both enabled",
        ),
        (AutonomyLevel.L1_SUPERVISED, _inbox_undecodable, "calibration compare: the inbox at "),
        (
            AutonomyLevel.L1_SUPERVISED,
            _inbox_torn_line,
            "calibration compare: 1 line(s) of the inbox at ",
        ),
        (
            AutonomyLevel.L2_GATED_MERGE,
            _history_undecodable,
            "health metrics: the recorded run history could not be read",
        ),
        (
            AutonomyLevel.L2_GATED_MERGE,
            _history_is_directory,
            "health metrics: the recorded run history could not be read",
        ),
        (
            AutonomyLevel.L2_GATED_MERGE,
            _no_history,
            "health metric retry_rate not measured: 0 decisive run(s) record it, need 8",
        ),
    ],
    ids=[
        "inbox_disabled",
        "autonomy_disabled",
        "inbox_undecodable",
        "inbox_torn_line",
        "history_undecodable",
        "history_is_directory",
        "no_history",
    ],
)
def test_a_signal_that_cannot_be_read_refuses_the_promotion(
    tmp_path: Path, level: AutonomyLevel, unreadable: object, expected: str
) -> None:
    root = _project(tmp_path, level)
    assert callable(unreadable)
    unreadable(root)

    status = _status(root)
    assert "Criteria met" not in status
    assert any(blocker.startswith(expected) for blocker in _blockers(status)), status
    code, output = _promote(root)
    assert code == 2, output
    assert AutonomyState.load(root).level == int(level)


def test_force_records_the_unmet_signal_in_the_transition_evidence(tmp_path: Path) -> None:
    root = _project(tmp_path, AutonomyLevel.L1_SUPERVISED)
    item = _regress_calibration(root, tmp_path)

    code, output = _promote(root, "--force")
    assert code == 0, output
    assert "Recorded as a forced promotion over unmet criteria." in output
    state = AutonomyState.load(root)
    assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
    assert state.history[-1].evidence["forced_over_blockers"] == [
        f"calibration compare not green: 1 undecided calibration_drift inbox item(s): {item}"
    ]


def test_l3_to_l4_names_the_deploy_target_as_unchecked(tmp_path: Path) -> None:
    root = _project(tmp_path, AutonomyLevel.L3_ENVELOPED_AUTO)

    status = _status(root)
    assert "Criteria met" not in status
    assert _blockers(status) == [DEPLOY_UNCHECKED]
    code, output = _promote(root)
    assert code == 2, output
    assert AutonomyState.load(root).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)


def test_a_snoozed_calibration_item_still_blocks(tmp_path: Path) -> None:
    """A snooze defers the item; it does not decide it."""
    root = _project(tmp_path, AutonomyLevel.L1_SUPERVISED)
    item = _regress_calibration(root, tmp_path)
    snoozed = CliRunner().invoke(
        cli, ["inbox", "snooze", item, "--root", str(root), "--ui", "plain", "--no-color"]
    )
    assert snoozed.exit_code == 0, snoozed.output

    assert _blockers(_status(root)) == [
        f"calibration compare not green: 1 undecided calibration_drift inbox item(s): {item}"
    ]
