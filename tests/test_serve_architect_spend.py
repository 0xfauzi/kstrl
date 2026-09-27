"""#587: `ks serve` counts the architect of `ks factory --spec` once.

Since #567 `ks factory --spec` runs its architect as a decompose run of its
own, created before the factory run. A launch that stops after the architect
(a blocker halt, a failed decompose, a refused lock) leaves only that run,
and `ks serve` charged factory-kind runs only, so the day's total left out an
architect's spend that is on disk and the cap admitted the next item against
it. A launch that goes on to execute carries the architect's spend in its
factory run too, so charging both runs would count it twice.

Every test here drives the real `ks serve` cycle over the real `ks factory`
CLI. The agent is a fake ``claude`` on PATH that answers each call with a
stream-json result event carrying a cost, which is what the real adapter
parses, so the architect reports usage the way a paid one does. No paid CLI
runs: ``codex`` is a stub that exits 1 and ``claude`` is the fake.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl import events as ev
from kstrl.cli import cli
from kstrl.reducer import load_run_state
from kstrl.runid import mint_run_id, run_kind
from kstrl.serve import (
    SPAWNED_RUN_KIND,
    RunOutcome,
    ServeConfig,
    SpendLedger,
    serve_cycle,
    subprocess_factory_runner,
)
from kstrl.workqueue import Queue, QueueConfig
from tests.helpers import procs
from tests.helpers.executables import write_executable
from tests.test_prompt_record import COMPLETE, ONE_COMPONENT, _spec_project

pytestmark = pytest.mark.usefixtures("no_open_prs")

#: What the fake architect reports for one call.
ARCHITECT_COST = 1.5

#: An architect answer that escalates a blocker, so `ks factory` halts after
#: the architect and before any factory run exists (#260).
BLOCKER = {
    "components": [],
    "spec_issues": [
        {"id": "too-vague", "severity": "blocker", "kind": "ambiguity", "summary": "too vague"}
    ],
    "decisions": [
        {
            "issue": "too-vague",
            "question": "which product ships first",
            "disposition": "escalated",
            "resolution": "the owner must name the smallest slice",
        }
    ],
}


def _result_event(text: str, cost: float) -> str:
    return json.dumps(
        {
            "type": "result",
            "result": text,
            "total_cost_usd": cost,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )


def _priced_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    architect: dict[str, object],
    *,
    then: str | None = None,
) -> Path:
    """A fake ``claude`` first on PATH; returns the file it logs each call to.

    The first call answers ``architect``, and so does every later one
    unless ``then`` is given, in which case later calls answer ``then``.
    Each call costs ``ARCHITECT_COST``. Set in the environment, so a child
    `ks factory` that `ks serve` spawns finds it too.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    first = tmp_path / "architect.jsonl"
    first.write_text(_result_event(json.dumps(architect), ARCHITECT_COST) + "\n", encoding="utf-8")
    later = tmp_path / "engineer.jsonl"
    later_text = json.dumps(architect) if then is None else then
    later.write_text(_result_event(later_text, ARCHITECT_COST) + "\n", encoding="utf-8")
    calls = tmp_path / "claude-calls"
    write_executable(
        bindir / "claude",
        "#!/bin/sh\n"
        'case "$*" in *--help*|*--version*) exit 0 ;; esac\n'
        "cat > /dev/null\n"
        f"if [ -f '{calls}' ]; then echo call >> '{calls}'; cat '{later}'; exit 0; fi\n"
        f"echo call >> '{calls}'\n"
        f"cat '{first}'\n",
    )
    write_executable(bindir / "codex", "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("KSTRL_AGENT_PROBE", "0")
    monkeypatch.setenv("KSTRL_AGENT_TYPE", "claude-code")
    return calls


def _calls(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def _queue(root: Path, *titles: str) -> Queue:
    queue = Queue(root, QueueConfig())
    for title in titles:
        queue.add("# Spec\n\nBuild a thing.\n", title=title, project_name="demo")
    return queue


def _config(**overrides: object) -> ServeConfig:
    fields: dict[str, object] = {"caffeinate": False, "factory_timeout_seconds": 120.0}
    return ServeConfig(**(fields | overrides))  # type: ignore[arg-type]


def _runs(root: Path) -> dict[str, list[str]]:
    by_kind: dict[str, list[str]] = {}
    for entry in sorted((root / ".kstrl" / "runs").iterdir()):
        by_kind.setdefault(run_kind(entry.name), []).append(entry.name)
    return by_kind


def _in_process_factory(*extra: str) -> Callable[..., RunOutcome]:
    """The `ks factory --spec` command `ks serve` spawns, run in this process.

    The argv is the one ``subprocess_factory_runner`` builds, plus
    ``extra``; the launch's process is this one, so ``on_spawn`` gets this
    pid, which is what a decompose run opened here records.
    """

    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: Callable[[int], None] | None = None,
    ) -> RunOutcome:
        if on_spawn is not None:
            on_spawn(os.getpid())
        gate = "--pause-before-pr-merge" if pause_before_pr_merge else "--no-pause-before-pr-merge"
        result = CliRunner().invoke(
            cli,
            [
                "factory",
                "--spec",
                str(spec_path),
                "--project-name",
                project_name,
                "--root",
                str(root_dir),
                "--yes",
                "--no-tui",
                "--ui",
                "plain",
                "--no-color",
                gate,
                *extra,
            ],
        )
        return RunOutcome(returncode=result.exit_code, output_tail=result.output)

    return runner


class TestAHaltedArchitectIsCharged:
    """The launch stops after the architect: only the decompose run exists."""

    @pytest.mark.parametrize(
        "caffeinate",
        [
            pytest.param(False, id="bare"),
            pytest.param(True, marks=procs.NEEDS_CAFFEINATE, id="caffeinate"),
        ],
    )
    def test_the_day_total_counts_what_the_decompose_run_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caffeinate: bool
    ) -> None:
        """With ``caffeinate`` on (the macOS default) the pid ``Popen``
        returns must still be the `ks factory` process that writes the
        decompose run, or the launch's architect is never charged."""
        calls = _priced_claude(tmp_path, monkeypatch, BLOCKER)
        root = _spec_project(tmp_path)
        _queue(root, "halts")

        result = serve_cycle(root, config=_config(caffeinate=caffeinate))

        assert list(_runs(root)) == ["decompose"], result.reason
        assert _calls(calls) == 1, "the architect ran once and nothing after it"
        spend = SpendLedger(root).read()
        assert spend.spent_usd == pytest.approx(ARCHITECT_COST), spend
        assert (spend.covered_calls, spend.total_calls, spend.runs) == (1, 1, 1), spend
        assert spend.unmetered_phases == (), "a recorded architect is not unmetered"
        assert not spend.lower_bound, spend
        assert result.charged_usd == pytest.approx(ARCHITECT_COST)

    def test_the_cap_refuses_the_next_item_once_the_architect_reaches_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _priced_claude(tmp_path, monkeypatch, BLOCKER)
        root = _spec_project(tmp_path)
        _queue(root, "halts", "waits")
        config = _config(daily_budget_usd=1.0, allow_uncovered_cost=True)

        serve_cycle(root, config=config)
        second = serve_cycle(root, config=config)

        assert second.ran_item == "", second.reason
        assert second.skipped.startswith("daily budget reached: $1.50 of $1.00"), second.skipped
        assert _calls(calls) == 1, "no second architect may run past the cap"

    def test_an_operator_s_decompose_in_the_window_is_not_charged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator's `ks decompose` on the same repo, inside the launch
        window, spends through the same fake and halts the same way. Its run
        names its own process, so only the launch's architect is charged."""
        _priced_claude(tmp_path, monkeypatch, BLOCKER)
        root = _spec_project(tmp_path)
        _queue(root, "halts")

        def with_an_operator_decompose(**kwargs: object) -> RunOutcome:
            operator = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kstrl",
                    "decompose",
                    "--spec",
                    str(root / "spec.md"),
                    "--project-name",
                    "operator",
                    "--root",
                    str(root),
                    "--ui",
                    "plain",
                    "--no-color",
                    "--no-tui",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert operator.returncode == 2, operator.stdout + operator.stderr
            return subprocess_factory_runner(**kwargs, caffeinate=False)  # type: ignore[arg-type]

        serve_cycle(root, config=_config(), runner=with_an_operator_decompose)

        decompose_runs = _runs(root)["decompose"]
        assert len(decompose_runs) == 2
        recorded = [load_run_state(root, rid)[0].cost_usd for rid in decompose_runs]
        assert recorded == [ARCHITECT_COST, ARCHITECT_COST]
        assert SpendLedger(root).read().spent_usd == pytest.approx(ARCHITECT_COST)

    def test_a_foreign_factory_run_in_the_window_does_not_hide_the_architect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A factory run from another process lands in the launch window and
        names no architect run. The launch's own architect still halted, so
        its decompose run is still charged: what leaves a decompose run
        uncharged is a factory run NAMING it, not any factory run existing."""
        _priced_claude(tmp_path, monkeypatch, BLOCKER)
        root = _spec_project(tmp_path)
        _queue(root, "halts")

        def with_a_foreign_factory_run(**kwargs: object) -> RunOutcome:
            run_id = mint_run_id(SPAWNED_RUN_KIND)
            run_dir = root / ".kstrl" / "runs" / run_id
            run_dir.mkdir(parents=True)
            bus = ev.EventBus(ev.JsonlSink(run_dir / "events.jsonl"), run_id=run_id)
            bus.emit(ev.RunStarted(project="other", components=1, pid=os.getpid()))
            bus.close()
            return subprocess_factory_runner(**kwargs, caffeinate=False)  # type: ignore[arg-type]

        serve_cycle(root, config=_config(), runner=with_a_foreign_factory_run)

        runs = _runs(root)
        assert (len(runs["factory"]), len(runs["decompose"])) == (1, 1), runs
        assert SpendLedger(root).read().spent_usd == pytest.approx(ARCHITECT_COST)


class TestARunThatCarriesItsArchitect:
    """The launch executes: the factory run carries the architect's spend."""

    def test_the_decompose_run_is_named_and_not_charged_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _priced_claude(tmp_path, monkeypatch, ONE_COMPONENT, then=COMPLETE)
        root = _spec_project(tmp_path, initialised=True)
        _queue(root, "executes")

        serve_cycle(
            root,
            config=_config(),
            runner=_in_process_factory("--no-verify", "--no-prs", "--max-retries", "0"),
        )

        runs = _runs(root)
        (architect_run,) = runs["decompose"]
        (factory_run,) = runs["factory"]
        factory_state = load_run_state(root, factory_run)[0]
        assert factory_state.architect_run_id == architect_run
        assert load_run_state(root, architect_run)[0].cost_usd == pytest.approx(ARCHITECT_COST)
        spend = SpendLedger(root).read()
        assert spend.spent_usd == pytest.approx(factory_state.cost_usd), (
            "the factory run already carries the architect; its decompose run "
            "must not be charged on top of it"
        )
        assert spend.unmetered_phases == ()

    def test_ks_status_names_the_run_that_holds_the_architect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _priced_claude(tmp_path, monkeypatch, ONE_COMPONENT, then=COMPLETE)
        root = _spec_project(tmp_path, initialised=True)
        _queue(root, "executes")
        serve_cycle(
            root,
            config=_config(),
            runner=_in_process_factory("--no-verify", "--no-prs", "--max-retries", "0"),
        )
        (architect_run,) = _runs(root)["decompose"]

        status = CliRunner().invoke(
            cli, ["status", "--root", str(root), "--no-tui", "--ui", "plain", "--no-color"]
        )

        assert status.exit_code == 0, status.output
        assert f"Architect run: {architect_run}" in " ".join(status.output.split()), status.output
