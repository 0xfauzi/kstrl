"""R8.4 (#151): factory health trending, advisory only.

The seam #232 shipped (``kstrl/factory.py::_record_health_breaches``) has
read a module named ``kstrl.health`` since it landed, and nothing supplied
it. This file drives the rule arithmetic on synthetic series, the two
report surfaces (``ks health`` and ``ks autonomy replay``), and the real
factory seam end to end, with no monkeypatching of ``sys.modules`` for the
seam test: that is the one test that proves #232 and #151 actually meet.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers.journal import component_result, journal_at
from tests.helpers.replay import UNDECODABLE_TSV, run_record, write_runs

FLAT9 = (0.10, 0.12, 0.09, 0.11, 0.10, 0.13, 0.08, 0.10, 0.11)
DRIFT12 = FLAT9 + (0.90, 0.92, 0.95)
DRIFT12_LINE = (
    "  - retry_rate: 1 point beyond 3 sigma (value 0.9500 beyond limit 0.1471 over 12 run(s))"
)


def write_history(
    root: Path,
    retry_rates: Sequence[float],
    *,
    cost: str = "1.00",
    common_failures: Sequence[str] | None = None,
) -> None:
    """Write .kstrl/experiments.tsv with one run per rate, run-NN ids.

    Through the shared builders (``tests/helpers/replay.py``) rather than
    a hand-rolled writer: ``run_record`` for the row, ``write_runs`` for
    the file, both already serialising under ``EXPERIMENTS_HEADER``.
    """
    failures = tuple(common_failures or ("",) * len(retry_rates))
    assert len(failures) == len(retry_rates)
    total_cost = None if cost == "" else float(cost)
    records = [
        run_record(
            run_id=f"run-{index:02d}",
            timestamp=f"2026-09-{index + 1:02d}T00:00:00Z",
            project="proj",
            components_total=2,
            retry_rate=rate,
            common_failure=failures[index],
            total_cost_usd=total_cost,
        )
        for index, rate in enumerate(retry_rates)
    ]
    write_runs(root, records)


def write_journal(root: Path, infra_counts: Sequence[int | None]) -> None:
    """One ``component_result`` entry per run, run-NN aligned with write_history.

    ``None`` omits ``findings_summary`` entirely, which is the
    missing-measurement case: a missing key is not a measured zero.
    """
    entries = [
        component_result(
            f"run-{index:02d}",
            "comp-a",
            findings_summary=(
                None if count is None else {"total": 1, "infrastructure_errors": count}
            ),
        )
        for index, count in enumerate(infra_counts)
    ]
    journal_at(root).append_entries(entries)


def metric_line(text: str, metric: str) -> str:
    """The ONE report line for this metric, or an assertion failure.

    Every ``n=`` and ``need`` assertion in this file goes through here
    rather than searching the whole report, and the reason is measured:
    the report carries three metric lines, so ``"n=0" in summary`` is
    satisfied by whichever of the three happens to be empty. In the
    blank-cost fixture ``infrastructure_error_rate`` is n=0, so
    ``"n=0" in summary`` passes with the COST series at n=12, which is
    exactly the defect (a blank cost column read as 0.0) that assertion
    exists to catch. Plant 5 below is that mutation and it would have
    stayed green.
    """
    found = [line for line in text.splitlines() if line.startswith(metric)]
    assert len(found) == 1, f"expected one {metric} line, got {found}"
    return found[0]


def test_ks_health_reports_a_drifting_retry_rate(tmp_path: Path) -> None:
    write_history(tmp_path, DRIFT12)
    result = CliRunner().invoke(cli, ["health", "--root", str(tmp_path), "--no-color"])

    assert result.exit_code == 1, result.output
    assert DRIFT12_LINE in result.output
    assert "n=12" in metric_line(result.output, "retry_rate")
    assert "Advisory only." in result.output


@pytest.mark.parametrize(
    ("values", "expected_exit_code", "fragment"),
    [
        (FLAT9 + (0.09, 0.12, 0.10), 0, "No breaches."),
        ((0.10, 0.12, 0.09, 0.11, 0.10, 0.90, 0.92), 0, "need 8"),
        (
            (0.10, 0.12, 0.09, 0.11, 0.10, 0.90, 0.92, 0.95),
            1,
            "retry_rate: 1 point beyond 3 sigma (value 0.9500 beyond limit 0.1346 over 8 run(s))",
        ),
    ],
    ids=["flat_history", "seven_runs_below_the_floor", "eight_runs_at_the_floor"],
)
def test_ks_health_exit_code_and_report(
    tmp_path: Path,
    values: tuple[float, ...],
    expected_exit_code: int,
    fragment: str,
) -> None:
    write_history(tmp_path, values)
    result = CliRunner().invoke(cli, ["health", "--root", str(tmp_path), "--no-color"])

    assert result.exit_code == expected_exit_code, result.output
    assert fragment in result.output


def test_the_factory_seam_opens_an_inbox_item_from_the_real_module(tmp_path: Path) -> None:
    """No monkeypatching of ``sys.modules``: #232's seam meets #151's module."""
    from kstrl.autonomy import AutonomyLevel, AutonomyState
    from kstrl.inbox import ItemKind
    from tests.helpers.demotion import inbox_items, run_outcome, write_config

    write_history(tmp_path, DRIFT12)
    write_config(tmp_path, demote_on_health=False)
    run_outcome(tmp_path)

    items = inbox_items(tmp_path, ItemKind.HEALTH_BREACH)
    assert len(items) == 1
    assert items[0].title == "Health breach: retry_rate 1 point beyond 3 sigma"
    assert items[0].evidence["metric"] == "retry_rate"
    assert items[0].evidence["rule"] == "1 point beyond 3 sigma"
    assert items[0].evidence["window_runs"] == 12
    assert items[0].evidence["value"] == pytest.approx(0.95)
    assert items[0].evidence["limit"] == pytest.approx(0.147132, abs=1e-6)
    assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L1_SUPERVISED)


def test_autonomy_replay_reports_health_breaches_without_mutating(tmp_path: Path) -> None:
    from kstrl.autonomy import AutonomyLevel, AutonomyState

    write_history(tmp_path, DRIFT12)
    AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
    result = CliRunner().invoke(cli, ["autonomy", "replay", "--root", str(tmp_path), "--no-color"])

    assert result.exit_code == 0, result.output
    assert "R8.4 health rules (advisory)" in result.output
    assert DRIFT12_LINE in result.output
    assert "Autonomy threshold replay" in result.output
    assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)


def test_a_malformed_evolution_key_is_a_named_refusal_not_a_traceback(
    tmp_path: Path,
) -> None:
    """``EvolutionConfig.load`` raises ``TypeError`` for a toml array where
    a number belongs. Both commands must refuse at exit 2 with the history
    line, because ``ks health`` documents exit 1 as a breach and
    ``ks autonomy replay`` never read ``[evolution]`` before this PR."""
    write_history(tmp_path, DRIFT12)
    # After the history: the helper places the file through the same loader.
    (tmp_path / "kstrl.toml").write_text(
        "[evolution]\nmin_pattern_frequency = [1]\n", encoding="utf-8"
    )
    for args in (["health"], ["autonomy", "replay"]):
        result = CliRunner().invoke(cli, [*args, "--root", str(tmp_path), "--no-color"])
        assert result.exit_code == 2, (args, result.output)
        assert "could not read the recorded run history" in result.output, (args, result.output)
        assert "Traceback" not in result.output, (args, result.output)


def test_replay_and_health_agree_when_experiments_path_moves(tmp_path: Path) -> None:
    """``[evolution] experiments_path`` must move BOTH report halves of
    ``ks autonomy replay``, not just its health-breach half.

    Before #151's simplify pass, ``replay_file`` resolved its default
    through ``DEFAULT_EXPERIMENTS_PATH`` while ``health_breaches``
    (called by the SAME command, for the second half of its own report)
    resolved through ``EvolutionConfig.load``: moving the file in
    ``kstrl.toml`` moved one half of one command's own output and left
    the other reading an empty default.
    """
    (tmp_path / "kstrl.toml").write_text(
        '[evolution]\nexperiments_path = "custom/history.tsv"\n', encoding="utf-8"
    )
    write_history(tmp_path, DRIFT12)
    assert not (tmp_path / ".kstrl" / "experiments.tsv").exists()
    assert (tmp_path / "custom" / "history.tsv").exists()

    replay_result = CliRunner().invoke(
        cli, ["autonomy", "replay", "--root", str(tmp_path), "--no-color"]
    )
    health_result = CliRunner().invoke(cli, ["health", "--root", str(tmp_path), "--no-color"])

    assert "Runs recorded:        12" in replay_result.output, replay_result.output
    assert DRIFT12_LINE in replay_result.output, replay_result.output
    assert DRIFT12_LINE in health_result.output, health_result.output


def test_the_process_exit_code_is_one_when_a_metric_breaches(tmp_path: Path) -> None:
    write_history(tmp_path, DRIFT12)
    proc = subprocess.run(
        [sys.executable, "-m", "kstrl", "health", "--root", str(tmp_path), "--no-color"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    # Measured: `_autonomy_ui` resolves to `PlainUI`, whose default `file`
    # is `sys.stderr` (matching every other _autonomy_ui-based command,
    # e.g. `ks autonomy replay`), so the report lands on stderr here, not
    # stdout. `proc.stdout` is empty; checked against the real process
    # rather than assumed from the CliRunner tests above, where `result.output`
    # merges both streams and hides the distinction.
    assert "1 point beyond 3 sigma" in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("values", "expected_rule", "expected_value", "expected_limit", "expected_window"),
    [
        (DRIFT12, "1 point beyond 3 sigma", 0.95, 0.147132, 12),
        (FLAT9 + (0.10, 0.140, 0.145), "2 of 3 beyond 2 sigma", 0.145, 0.132903, 12),
        (FLAT9 + (0.10, 0.10, 0.145), None, None, None, None),
        (FLAT9 + (0.166,) * 5, "EWMA(0.2) beyond 3 sigma", 0.146202, 0.142642, 14),
        (FLAT9 + (0.09, 0.12, 0.10), None, None, None, None),
        # Pins the monitored window at MONITORED_RUNS points: in control with the
        # 3-point window, and a 4-point window fires 2 of 3 beyond 2 sigma at
        # 0.145000 against 0.144846.
        (FLAT9 + (0.145, 0.10, 0.10, 0.145), None, None, None, None),
        ((0.10, 0.12, 0.09, 0.11, 0.10, 0.90, 0.92), None, None, None, None),
        (
            (0.10, 0.12, 0.09, 0.11, 0.10, 0.90, 0.92, 0.95),
            "1 point beyond 3 sigma",
            0.95,
            0.134594,
            8,
        ),
        (
            (0.88, 0.92, 0.89, 0.91, 0.90, 0.93, 0.88, 0.90, 0.91, 0.10, 0.08, 0.11),
            None,
            None,
            None,
            None,
        ),
        ((0.5,) * 12, None, None, None, None),
    ],
)
def test_the_rules_on_synthetic_series(
    values: tuple[float, ...],
    expected_rule: str | None,
    expected_value: float | None,
    expected_limit: float | None,
    expected_window: int | None,
) -> None:
    from kstrl.health import _reading

    reading = _reading("m", values)
    if expected_rule is None:
        assert reading.breach is None
    else:
        assert reading.breach is not None
        assert reading.breach.rule == expected_rule
        assert reading.breach.value == pytest.approx(expected_value, abs=1e-6)
        assert reading.breach.limit == pytest.approx(expected_limit, abs=1e-6)
        assert reading.breach.window_runs == expected_window


def test_ewma_smooths_towards_the_new_level() -> None:
    from kstrl.health import ewma

    assert ewma((1.0, 1.0, 1.0), start=0.0) == pytest.approx(0.488)


def test_cost_per_merged_component_drops_runs_with_no_recorded_cost(tmp_path: Path) -> None:
    import kstrl.health

    write_history(tmp_path, (0.1,) * 12, cost="")
    summary, breaches = kstrl.health.health_status(tmp_path)

    assert breaches == []
    assert "n=0" in metric_line(summary, "cost_per_merged_component")


def test_an_infra_aborted_run_is_not_in_the_series(tmp_path: Path) -> None:
    import kstrl.health

    write_history(
        tmp_path,
        (0.10, 0.12, 0.09, 0.11, 0.10, 0.13, 0.08, 0.10, 0.11, 0.95, 0.95, 0.95),
        common_failures=("",) * 9 + ("pr:push-of-x-failed",) * 3,
    )
    summary, breaches = kstrl.health.health_status(tmp_path)

    assert breaches == []
    assert "n=9" in metric_line(summary, "retry_rate")


def test_the_infrastructure_error_rate_comes_from_the_journal(tmp_path: Path) -> None:
    import kstrl.health

    write_history(tmp_path, (0.1,) * 12)
    write_journal(tmp_path, [0, 1, 0, 1, 0, 0, 1, 0, 1, 5, 5, 5])
    summary, breaches = kstrl.health.health_status(tmp_path)

    matching = [b for b in breaches if b.metric == "infrastructure_error_rate"]
    assert len(matching) == 1
    breach = matching[0]
    assert breach.rule == "1 point beyond 3 sigma"
    assert breach.value == pytest.approx(5.0)
    assert breach.limit == pytest.approx(1.935156, abs=1e-6)
    assert breach.window_runs == 12


def test_ks_health_reports_the_journal_series(tmp_path: Path) -> None:
    """Drives the journal-derived series through the real CLI, not ``health_status``.

    The five UNIT-3 tests above all call ``kstrl.health.health_status``
    directly. #387's blocker 1 found that no production code called it any
    more: ``ks health`` (``kstrl/cli.py::health_cmd``) had its own second
    copy of the render/breach logic, reachable through the private names
    ``_breaches`` and ``_render``, so the CLI path these tests were meant
    to stand in for was untested. This test closes that gap by going
    through ``CliRunner`` and the ``health`` command exactly as an operator
    would.
    """
    write_history(tmp_path, (0.1,) * 12)
    write_journal(tmp_path, [0, 1, 0, 1, 0, 0, 1, 0, 1, 5, 5, 5])
    result = CliRunner().invoke(cli, ["health", "--root", str(tmp_path), "--no-color"])

    assert result.exit_code == 1, result.output
    assert "n=12" in metric_line(result.output, "infrastructure_error_rate")
    assert (
        "  - infrastructure_error_rate: 1 point beyond 3 sigma "
        "(value 5.0000 beyond limit 1.9352 over 12 run(s))"
    ) in result.output


def test_a_component_entry_without_a_findings_summary_drops_its_run(tmp_path: Path) -> None:
    import kstrl.health

    write_history(tmp_path, (0.1,) * 12)
    write_journal(tmp_path, [None, 1, 0, 1, 0, 0, 1, 0, 1, 5, 5, 5])
    summary, breaches = kstrl.health.health_status(tmp_path)

    assert "n=11" in metric_line(summary, "infrastructure_error_rate")
    matching = [b for b in breaches if b.metric == "infrastructure_error_rate"]
    assert len(matching) == 1
    breach = matching[0]
    assert breach.limit == pytest.approx(2.0)
    assert breach.window_runs == 11


def test_a_boolean_infrastructure_errors_count_drops_its_run(tmp_path: Path) -> None:
    """``isinstance(True, int)`` is ``True``; a bool is not a measured count.

    Added by #151's simplify pass: PS5 (the grouping rewrite keeping a
    run whose count is a bool) left every OTHER fixture in this file
    green, because none of them used a bool value, so the exclusion had
    no test actually depending on it.
    """
    import kstrl.health

    write_history(tmp_path, (0.1,) * 12)
    write_journal(tmp_path, [True, 1, 0, 1, 0, 0, 1, 0, 1, 5, 5, 5])
    summary, breaches = kstrl.health.health_status(tmp_path)

    assert "n=11" in metric_line(summary, "infrastructure_error_rate")


def test_an_undecodable_experiments_file_is_a_refusal(tmp_path: Path) -> None:
    state = tmp_path / ".kstrl"
    state.mkdir(parents=True, exist_ok=True)
    (state / "experiments.tsv").write_bytes(UNDECODABLE_TSV)

    result = CliRunner().invoke(cli, ["health", "--root", str(tmp_path), "--no-color"])

    assert result.exit_code == 2, result.output
    assert "could not read the recorded run history" in result.output
    assert "No breaches." not in result.output
