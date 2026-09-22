"""#233: `ks evolve --status` prints a verdict on the entry criterion.

The criterion names ``avg_iterations``, which is a lower bound (the last
attempt's count per component). This module tests the CLI surface that
prints the verdict computed from the journal's per-attempt readings
instead (``EvolutionJournal.iteration_criterion_lines``), refusing
rather than guessing when a run's readings are incomplete, through the
real ``click`` command end to end.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

import kstrl.cli as cli_mod
from kstrl.evolution import EXPERIMENTS_HEADER, FINDINGS_SUPERSEDED_EVENT


def _write_experiments_tsv(tmp_path: Path, rows: list[dict[str, str]]) -> None:
    """One row per dict, columns taken from ``EXPERIMENTS_HEADER`` so a
    shorter hand-written header cannot pass by tolerance alone
    (``tests/test_journal_torn_tail.py::experiments_row``'s reason)."""
    kstrl_dir = tmp_path / ".kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    columns = EXPERIMENTS_HEADER.split("\t")
    lines = [EXPERIMENTS_HEADER]
    for row in rows:
        lines.append("\t".join(row.get(col, "0") for col in columns))
    (kstrl_dir / "experiments.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_journal(tmp_path: Path, entries: list[dict[str, object]]) -> None:
    import json

    kstrl_dir = tmp_path / ".kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(e) for e in entries) + "\n"
    (kstrl_dir / "evolution.jsonl").write_text(text, encoding="utf-8")


def _invoke_status(tmp_path: Path) -> str:
    result = CliRunner().invoke(
        cli_mod.cli,
        ["evolve", "--status", "--root", str(tmp_path), "--ui", "plain", "--no-color"],
    )
    assert result.exit_code == 0, result.output
    return str(result.output)


def test_the_status_row_carries_project_and_avg_iterations(tmp_path: Path) -> None:
    _write_experiments_tsv(
        tmp_path,
        [
            {
                "run_id": "r1",
                "timestamp": "2026-08-20T00:00:00Z",
                "project": "demo",
                "components_total": "1",
                "avg_iterations": "2.50",
                "common_failure": "",
            }
        ],
    )
    output = _invoke_status(tmp_path)
    assert "project=demo" in output
    assert "avg_iterations=2.50" in output


def test_status_refuses_a_verdict_when_the_journal_has_no_per_attempt_readings(
    tmp_path: Path,
) -> None:
    _write_experiments_tsv(
        tmp_path,
        [
            {
                "run_id": "r1",
                "timestamp": "2026-08-20T00:00:00Z",
                "project": "demo",
                "components_total": "1",
                "avg_iterations": "1.00",
            }
        ],
    )
    _write_journal(
        tmp_path,
        [
            {
                "schema_version": 2,
                "timestamp": "2026-08-20T00:00:00Z",
                "run_id": "r1",
                "project": "demo",
                "component_id": "comp-a",
                "event_type": "component_result",
                "retries": 1,
                "iteration_count": 1,
            },
            {
                "schema_version": 2,
                "timestamp": "2026-08-20T00:00:00Z",
                "run_id": "r1",
                "project": "demo",
                "component_id": "comp-a",
                "event_type": FINDINGS_SUPERSEDED_EVENT,
                "attempt": 1,
            },
        ],
    )
    out = _invoke_status(tmp_path)
    assert "iterations_all_attempts=REFUSED" in out
    assert "attempt 1 carries no iteration_count" in out
    clause1 = next(line for line in out.splitlines() if "clause 1" in line)
    assert clause1.strip().startswith(
        "clause 1 (journal per-attempt iterations > 1.00 in a majority of the window): REFUSED"
    )
    assert "NOT MET" not in clause1


def test_status_prints_the_measured_verdict_on_a_complete_journal(tmp_path: Path) -> None:
    _write_experiments_tsv(
        tmp_path,
        [
            {
                "run_id": "r1",
                "timestamp": "2026-08-20T00:00:00Z",
                "project": "demo",
                "components_total": "1",
                "avg_iterations": "3.00",
            }
        ],
    )
    _write_journal(
        tmp_path,
        [
            {
                "schema_version": 2,
                "timestamp": "2026-08-20T00:00:00Z",
                "run_id": "r1",
                "project": "demo",
                "component_id": "comp-a",
                "event_type": FINDINGS_SUPERSEDED_EVENT,
                "attempt": 1,
                "iteration_count": 3,
            },
            {
                "schema_version": 2,
                "timestamp": "2026-08-20T00:00:00Z",
                "run_id": "r1",
                "project": "demo",
                "component_id": "comp-a",
                "event_type": "component_result",
                "retries": 1,
                "iteration_count": 3,
            },
        ],
    )
    out = _invoke_status(tmp_path)
    assert "iterations_all_attempts=6.00" in out
    clause1 = next(line for line in out.splitlines() if "clause 1" in line)
    assert clause1.strip() == (
        "clause 1 (journal per-attempt iterations > 1.00 in a majority of the window): "
        "MET - 1 of 1 run(s) above 1.00; a majority needs 1"
    )


def test_status_names_the_journal_it_read(tmp_path: Path) -> None:
    _write_experiments_tsv(
        tmp_path,
        [
            {
                "run_id": "r1",
                "timestamp": "2026-08-20T00:00:00Z",
                "project": "demo",
                "components_total": "1",
                "avg_iterations": "1.00",
            }
        ],
    )
    out = _invoke_status(tmp_path)
    assert str(tmp_path / ".kstrl" / "evolution.jsonl") in out


def test_status_refuses_clause_two_on_a_single_project_ledger(tmp_path: Path) -> None:
    _write_experiments_tsv(
        tmp_path,
        [
            {
                "run_id": "r1",
                "timestamp": "2026-08-20T00:00:00Z",
                "project": "demo",
                "components_total": "1",
                "avg_iterations": "3.00",
            }
        ],
    )
    _write_journal(
        tmp_path,
        [
            {
                "schema_version": 2,
                "timestamp": "2026-08-20T00:00:00Z",
                "run_id": "r1",
                "project": "demo",
                "component_id": "comp-a",
                "event_type": FINDINGS_SUPERSEDED_EVENT,
                "attempt": 1,
                "iteration_count": 3,
            },
            {
                "schema_version": 2,
                "timestamp": "2026-08-20T00:00:00Z",
                "run_id": "r1",
                "project": "demo",
                "component_id": "comp-a",
                "event_type": "component_result",
                "retries": 1,
                "iteration_count": 3,
            },
        ],
    )
    out = _invoke_status(tmp_path)
    clause2 = next(line for line in out.splitlines() if "clause 2" in line)
    assert clause2.strip() == (
        "clause 2 (at least two distinct project values): REFUSED - "
        "1 distinct project value(s) in this ledger (demo); this surface reads "
        "one project root and the criterion spans projects"
    )
    assert "reads one project root" in out
