"""The first `ks serve` reads the cost evidence a manual run left (#464).

The defect: `check_cost_coverage` allowed a daily budget only when the
serve ledger's ``cost_coverage_seen`` flag was set, and only a run the
daemon itself launched sets it. So the first `ks serve` on any repo
refused every positive ``daily_budget_usd`` with "no call has ever
reported a cost figure on this repo", including a repo whose
``.kstrl/progress.jsonl`` recorded 43 calls reporting $42.35 from a
manual `ks factory` the same day.

The gate now also reads the repo's progress log. It refuses only when
that log and the ledger both lack a call that reported a cost, and the
refusal names the log it read. Every test drives a real entry point:
the real ``serve_cycle`` against a real queue, ledger and log, or the
real `ks serve --dry-run` in a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.serve import RunOutcome, ServeConfig, SpendLedger, serve_cycle
from kstrl.workqueue import ItemState, Queue, QueueConfig
from tests.helpers.executables import write_executable

pytestmark = pytest.mark.usefixtures("no_open_prs")

#: One `component_usage` line as `ks factory` writes it to
#: `.kstrl/progress.jsonl` (shape copied from a real run's log).
COST_REPORTING = {
    "ts": "2026-09-24T10:00:00Z",
    "event": "component_usage",
    "run_id": "factory-20260924-100000.000000-abcdef",
    "component": "http",
    "data": {
        "phase": "engineer",
        "calls": 43,
        "known_calls": 43,
        "token_calls": 43,
        "cost_calls": 43,
        "unreported_calls": 0,
        "total_tokens": 2910902,
        "cost_usd": 42.35,
        "duration_seconds": 757.74,
    },
}

#: The same line from an agent that reports tokens and no cost (codex).
TOKENS_ONLY = {
    **COST_REPORTING,
    "data": {**COST_REPORTING["data"], "calls": 4, "cost_calls": 0, "cost_usd": 0.0},
}

#: A payload written before `cost_calls` existed: a dollar figure and no count.
LEGACY_WITH_COST = {
    **COST_REPORTING,
    "data": {
        "phase": "engineer",
        "calls": 1,
        "known_calls": 1,
        "unreported_calls": 0,
        "cost_usd": 4.269656,
    },
}


def _log(root: Path, *events: dict[str, object]) -> Path:
    path = root / ".kstrl" / "progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _runner(calls: list[str]):
    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: object = None,
    ) -> RunOutcome:
        calls.append(project_name)
        return RunOutcome(returncode=0)

    return runner


def _cycle(root: Path) -> tuple[list[str], object, Queue]:
    queue = Queue(root, QueueConfig())
    queue.add("# spec\n", title="first")
    calls: list[str] = []
    result = serve_cycle(
        root, config=ServeConfig(daily_budget_usd=50.0, caffeinate=False), runner=_runner(calls)
    )
    return calls, result, queue


class TestTheFirstServeReadsRecordedCost:
    def test_the_first_serve_with_recorded_cost_runs_the_item(self, tmp_path: Path) -> None:
        _log(tmp_path, COST_REPORTING)
        assert SpendLedger(tmp_path).read_state().cost_coverage_seen is False

        calls, result, queue = _cycle(tmp_path)

        assert len(calls) == 1, result.skipped
        assert result.ran_item != "", result.skipped
        assert not queue.pause_state().paused
        assert [item.state for item in queue.items()] == [ItemState.DONE]

    def test_calls_that_reported_no_cost_still_refuse_and_say_where_they_looked(
        self, tmp_path: Path
    ) -> None:
        log = _log(tmp_path, TOKENS_ONLY)

        calls, result, queue = _cycle(tmp_path)

        assert calls == []
        assert queue.pause_state().paused
        assert "none of the 4 agent call(s) recorded in" in result.skipped, result.skipped
        assert str(log) in result.skipped
        assert "can never fire" in result.skipped
        assert "no call has ever reported" not in result.skipped

    def test_a_repo_with_no_recorded_call_is_told_to_record_one(self, tmp_path: Path) -> None:
        calls, result, _queue = _cycle(tmp_path)

        assert calls == []
        assert "no agent call is recorded on this repo yet" in result.skipped, result.skipped
        assert "ks factory" in result.skipped
        assert "ks queue resume" in result.skipped
        assert "allow_uncovered_cost" not in result.skipped
        assert "NOT estimated" in result.skipped

    def test_a_legacy_event_with_a_dollar_figure_counts_as_reported(self, tmp_path: Path) -> None:
        _log(tmp_path, LEGACY_WITH_COST)

        calls, result, queue = _cycle(tmp_path)

        assert len(calls) == 1, result.skipped
        assert [item.state for item in queue.items()] == [ItemState.DONE]


class TestTheDryRunAgrees:
    def test_the_dry_run_passes_the_cost_gate_on_recorded_cost(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "kstrl.toml").write_text("[serve]\ndaily_budget_usd = 50.0\n", encoding="utf-8")
        _log(root, COST_REPORTING)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        write_executable(bindir / "gh", "#!/bin/sh\necho '[]'\nexit 0\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("KSTRL_")}
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"

        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "kstrl",
                "serve",
                "--dry-run",
                "--ui",
                "plain",
                "--no-color",
                "--root",
                str(root),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        out = done.stdout + done.stderr

        assert "gate cost coverage: ok" in out, out
