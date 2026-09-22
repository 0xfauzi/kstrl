"""#447: a run journals only the components it touched.

``EvolutionJournal.record_run`` used to write one ``component_result`` row
per manifest component from the manifest's CARRIED state, so a retry run
re-journaled every earlier component's result as its own under a new
``run_id``. These tests drive the real factory and the real CLI and assert
on the journal, ``experiments.tsv`` and the printed output.

The fixture under ``tests/fixtures/journal_447/`` is the #432 dogfood
journal (22 rows, 4 runs, 16 ``component_result`` rows) and its
``experiments.tsv``, with three free-text fields blanked that no reader
under test consumes: ``test_output``, ``findings`` (``findings_summary``
is kept) and ``spec_issues.issues``. It is history and is never rewritten,
so the readers must tolerate the carried rows already in it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kstrl.autonomy_replay import load_runs
from kstrl.evolution import EvolutionConfig
from kstrl.factory import FactoryConfig, run_factory
from kstrl.health import readings_from
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests import spine_utils
from tests.helpers import gitrepo
from tests.test_event_stream import (
    _component,
    _make_base_config,
    _make_manifest,
    _setup_project,
)

FIXTURE = Path(__file__).parent / "fixtures" / "journal_447"
COMPLETE = "<promise>COMPLETE</promise>"
RUN_3 = "factory-20260922-060733.849007-9f3722"
RUN_4 = "factory-20260922-063652.559123-d658d2"


def _journal(root: Path) -> list[dict[str, Any]]:
    path = root / ".kstrl" / "evolution.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _results(root: Path) -> list[dict[str, Any]]:
    return [e for e in _journal(root) if e.get("event_type") == "component_result"]


def _tsv_rows(root: Path) -> list[dict[str, str]]:
    lines = (root / ".kstrl" / "experiments.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"), strict=False)) for line in lines[1:]]


def _git_project(root: Path, ids: list[str]) -> Path:
    project = _setup_project(root, ids)
    gitrepo.git_in(project, "init", "-q", "-b", "main")
    gitrepo.set_identity(project)
    gitrepo.git_in(project, "add", "-A")
    gitrepo.git_in(project, "commit", "-qm", "base")
    return project


def _config(root: Path, *, test_command: str, max_retries: int) -> FactoryConfig:
    return FactoryConfig(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=max_retries,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command=test_command,
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
        progress_log_path=root / "progress.jsonl",
    )


def _evolve(root: Path, *extra: str) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kstrl",
            "evolve",
            *extra,
            "--root",
            str(root),
            "--ui",
            "plain",
            "--no-color",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    return proc.stdout


def _copy_fixture(tmp_path: Path) -> Path:
    kstrl_dir = tmp_path / ".kstrl"
    kstrl_dir.mkdir(parents=True)
    shutil.copy(FIXTURE / "evolution.jsonl", kstrl_dir / "evolution.jsonl")
    shutil.copy(FIXTURE / "experiments.tsv", kstrl_dir / "experiments.tsv")
    return tmp_path


def _line_for(output: str, run_id: str) -> str:
    return next(
        line
        for line in output.splitlines()
        if line.strip().startswith(run_id) and "iterations_all_attempts" in line
    )


# --- writer -----------------------------------------------------------------


def test_a_resumed_run_journals_only_the_component_it_ran(tmp_path: Path) -> None:
    """Plant 1: comp-a COMPLETED in an earlier run, comp-b runs now."""
    root = _git_project(tmp_path, ["comp-a", "comp-b"])
    carried = _component("comp-a")
    carried.status = ComponentStatus.COMPLETED.value
    carried.iteration_count = 4
    carried.duration_seconds = 12.5
    manifest = _make_manifest([carried, _component("comp-b")])
    base = _make_base_config(root)
    base.agent_cmd = f"echo '{COMPLETE}'"

    result = run_factory(
        manifest,
        _config(root, test_command="true", max_retries=0),
        base,
        PlainUI(no_color=True),
        root,
    )

    assert result.completed == ["comp-b"]
    assert [e["component_id"] for e in _results(root)] == ["comp-b"]
    (row,) = _tsv_rows(root)
    assert row["components_total"] == "1"
    assert row["avg_iterations"] == "1.00"


def _two_runs_retrying_one(tmp_path: Path) -> Path:
    """Run 1: comp-a and comp-b both fail after 2 attempts x 3 iterations.
    Run 2: ``reset_for_retry('comp-b')`` (what ``ks retry`` does), and
    comp-b completes in 1 iteration. comp-a is carried FAILED, retries 1."""
    root = _git_project(tmp_path, ["comp-a", "comp-b"])
    manifest: Manifest = _make_manifest([_component("comp-a"), _component("comp-b")])
    base = _make_base_config(root)
    base.agent_cmd = "echo working"
    base.max_iterations = 3
    run_factory(
        manifest,
        _config(root, test_command="false", max_retries=1),
        base,
        PlainUI(no_color=True),
        root,
    )
    assert [c.status for c in manifest.components] == ["failed", "failed"]

    manifest.reset_for_retry("comp-b")
    base.agent_cmd = f"echo '{COMPLETE}'"
    run_factory(
        manifest,
        _config(root, test_command="true", max_retries=1),
        base,
        PlainUI(no_color=True),
        root,
    )
    return root


def test_evolve_status_measures_a_run_that_retried_one_component(tmp_path: Path) -> None:
    """Plant 2: run 2's reading is measured, not REFUSED on comp-a's carried retries."""
    root = _two_runs_retrying_one(tmp_path)
    first, second = (row["run_id"] for row in _tsv_rows(root))

    out = _evolve(root, "--status")

    assert (
        "iterations_all_attempts=6.00 (2 component(s) ran, 12 iteration(s) across 4 attempt(s))"
        in _line_for(out, first)
    )
    assert (
        "iterations_all_attempts=1.00 (1 component(s) ran, 1 iteration(s) across 1 attempt(s))"
        in _line_for(out, second)
    )


def test_evolve_readiness_counts_each_component_build_once(tmp_path: Path) -> None:
    """Plant 3: two runs, three component results (a, b, then b again)."""
    root = _two_runs_retrying_one(tmp_path)

    assert [
        (e["run_id"] == _tsv_rows(root)[1]["run_id"], e["component_id"]) for e in _results(root)
    ] == [
        (False, "comp-a"),
        (False, "comp-b"),
        (True, "comp-b"),
    ]
    out = _evolve(root)
    assert "concern hit rate: 0 of 3 components" in out


# --- readers over the #432 journal (history, never rewritten) ---------------


def test_the_fixture_is_the_432_journal_with_every_row_kept() -> None:
    rows = [
        json.loads(line)
        for line in (FIXTURE / "evolution.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    results = [r for r in rows if r.get("event_type") == "component_result"]
    assert len(rows) == 22
    assert len(results) == 16
    assert len({r["run_id"] for r in results}) == 4


def test_evolve_status_measures_runs_3_and_4_of_the_432_journal(tmp_path: Path) -> None:
    out = _evolve(_copy_fixture(tmp_path), "--status")

    assert (
        "iterations_all_attempts=4.00 (1 component(s) ran, 4 iteration(s) across 1 attempt(s))"
        in _line_for(out, RUN_3)
    )
    assert (
        "iterations_all_attempts=4.00 (1 component(s) ran, 4 iteration(s) across 1 attempt(s))"
        in _line_for(out, RUN_4)
    )


def test_evolve_readiness_on_the_432_journal_counts_no_carried_row(tmp_path: Path) -> None:
    out = _evolve(_copy_fixture(tmp_path))

    assert "concern hit rate: 4 of 9 components" in out
    assert "fact utilization: measured 4, unmeasured 5" in out


def test_evolve_readiness_drops_a_copy_whose_previous_row_is_outside_the_lookback_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carried-row filter must run over the whole file before the
    lookback window is applied. Run the filter after the window instead,
    and a copy loses the earlier row that marks it a copy the moment that
    earlier row falls outside the window: with the window narrowed to 2
    runs, run 3's copy of link-rules and run 4's copies of link-rules and
    storage would count as fresh results, changing both readings below."""
    monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")

    out = _evolve(_copy_fixture(tmp_path))

    assert "concern hit rate: 2 of 2 components" in out
    assert "fact utilization: measured 2, unmeasured 0" in out


def test_health_infra_rate_on_the_432_journal_reads_no_carried_row(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    config = EvolutionConfig.load(root)
    readings = {
        r.metric: r for r in readings_from(load_runs(config.experiments_path), config.journal_path)
    }
    assert readings["infrastructure_error_rate"].values == (0.0, 0.0, 0.0)


def test_the_carried_rows_of_the_432_journal_are_the_seven_copies() -> None:
    """The copies the issue lists, and what is left per run is exactly what
    that run's experiments.tsv row says it completed, failed or skipped."""
    from kstrl.evolution import carried_result_indices  # new in #447; red, not a collection error

    rows = [
        json.loads(line)
        for line in (FIXTURE / "evolution.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    runs = list(dict.fromkeys(r["run_id"] for r in rows if r.get("run_id")))
    carried = carried_result_indices(rows)

    assert sorted(
        (runs.index(rows[i]["run_id"]) + 1, rows[i]["component_id"]) for i in carried
    ) == [
        (2, "link-rules"),
        (3, "cli"),
        (3, "link-rules"),
        (3, "storage"),
        (4, "http-api"),
        (4, "link-rules"),
        (4, "storage"),
    ]
    kept = [
        r
        for i, r in enumerate(rows)
        if r.get("event_type") == "component_result" and i not in carried
    ]
    tsv = (FIXTURE / "experiments.tsv").read_text(encoding="utf-8").splitlines()
    header = tsv[0].split("\t")
    touched = [
        sum(
            int(dict(zip(header, line.split("\t"), strict=False))[k])
            for k in ("completed", "failed", "skipped")
        )
        for line in tsv[1:]
    ]
    assert touched == [4, 3, 1, 1]
    assert [sum(1 for r in kept if r["run_id"] == run) for run in runs] == touched


def _legacy_row(run_id: str, duration: float, version: int) -> dict[str, Any]:
    return {
        "schema_version": version,
        "run_id": run_id,
        "component_id": "comp-a",
        "event_type": "component_result",
        "status": "completed",
        "retries": 0,
        "iteration_count": 3,
        "duration_seconds": duration,
    }


@pytest.mark.parametrize(
    ("version", "second_duration", "components"),
    [
        (2, 10.5, 1),  # pre-#447 row repeating status, retries, iterations AND duration: a copy
        (2, 11.25, 2),  # same three fields, new duration: the component ran again
        (3, 10.5, 2),  # a v3 row is never a copy, even when all four fields repeat
        (True, 10.5, 2),  # a bool is not a version (True is an int): not provably pre-#447
    ],
)
def test_ks_evolve_drops_a_row_only_when_it_is_pre_447_and_repeats_the_previous_duration(
    tmp_path: Path, version: int, second_duration: float, components: int
) -> None:
    kstrl_dir = tmp_path / ".kstrl"
    kstrl_dir.mkdir()
    rows = [_legacy_row("r1", 10.5, version), _legacy_row("r2", second_duration, version)]
    (kstrl_dir / "evolution.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    out = _evolve(tmp_path)

    assert f"concern hit rate: 0 of {components} components" in out


# --- a component that ends MERGE_PENDING is journaled in the run that parked it


@pytest.mark.spine
def test_a_component_parked_merge_pending_is_journaled_in_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """alpha runs, its PR is never confirmed merged, so it ends MERGE_PENDING:
    in ``scheduled`` but in none of completed, failed or skipped. beta
    depends on alpha and never runs. Run 1 touched alpha only; run 2
    re-polls alpha, finds it still pending, and touched nothing. Run 3's
    re-poll finds the PR merged, so alpha completes without a launch and
    beta runs: alpha's one iteration was run 1's work, so run 3 journals
    beta only and ``ks evolve --status`` reads one component for run 3."""
    bin_dir = tmp_path / "spine-bin"
    bin_dir.mkdir()
    spine_utils.write_stub_gh(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for var in ("GH_SPINE_CREATE", "GH_SPINE_MERGE", "GH_SPINE_MERGE_SHA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GH_SPINE_VIEW_STATE", "OPEN")
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    root = tmp_path / "repo"
    spine_utils.init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
    manifest = spine_utils.make_manifest(
        [spine_utils.component("alpha"), spine_utils.component("beta", ["alpha"])]
    )

    result = run_factory(
        manifest,
        spine_utils.factory_config(create_prs=True),
        spine_utils.base_config(root),
        PlainUI(no_color=True),
        root,
    )

    assert result.merge_pending == ["alpha"]
    assert (result.completed, result.failed, result.skipped) == ([], [], [])
    assert [e["component_id"] for e in _results(root)] == ["alpha"]
    (row,) = _tsv_rows(root)
    assert row["components_total"] == "1"

    # Run 2: the re-poll finds the PR still open. alpha stays MERGE_PENDING
    # (it is in result.merge_pending, rebuilt from the manifest) and nothing
    # else can run, so run 2 touched nothing and journals no row.
    again = run_factory(
        manifest,
        spine_utils.factory_config(create_prs=True),
        spine_utils.base_config(root),
        PlainUI(no_color=True),
        root,
    )

    assert again.merge_pending == ["alpha"]
    assert again.scheduled == []
    assert [e["component_id"] for e in _results(root)] == ["alpha"]
    assert [r["components_total"] for r in _tsv_rows(root)] == ["1", "0"]

    # Run 3: the re-poll confirms the merge. alpha completes without a
    # launch; beta is unblocked and runs.
    monkeypatch.setenv("GH_SPINE_VIEW_STATE", "MERGED")
    third = run_factory(
        manifest,
        spine_utils.factory_config(create_prs=True),
        spine_utils.base_config(root),
        PlainUI(no_color=True),
        root,
    )

    assert third.completed == ["alpha", "beta"]
    assert third.scheduled == ["beta"]
    run_3 = _tsv_rows(root)[2]["run_id"]
    assert [(e["run_id"] == run_3, e["component_id"]) for e in _results(root)] == [
        (False, "alpha"),
        (True, "beta"),
    ]
    assert [r["components_total"] for r in _tsv_rows(root)] == ["1", "0", "1"]
    assert "(1 component(s) ran, 1 iteration(s) across 1 attempt(s))" in _line_for(
        _evolve(root, "--status"), run_3
    )
