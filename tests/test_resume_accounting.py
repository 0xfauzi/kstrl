"""After an interrupt and a resume, every surface counts the same attempts and spend (#463).

A run that is killed before its summary writes no journal result, no
experiments.tsv row and no run total, while the manifest already carries its
retry count. The run that resumes the manifest takes the killed run's record
over: its spend enters the new run's meter, and the attempts the manifest
counts for a component the new run runs again (their ``component_retrying``
events and ``findings_superseded`` rows) are written again under the new run's
id. A run that did finish keeps what it recorded, and the next run answers
only for the attempts from ``first_attempt`` on.

The kill is a real SIGKILL of a real factory process; the graceful stop is the
same ``StopController`` the SIGINT handler sets. Every engineer is a shell
command. The engineer's spend is a fixed ``cost_usd`` per call, because the
custom agent reports calls and no cost. The approval tests drive the real
``serve_cycle`` and the real `ks inbox approve` from ``tests/test_merge_gate_park.py``.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kstrl.agents import custom
from kstrl.agents.base import UsageRecord
from kstrl.evolution import FINDINGS_SUPERSEDED_EVENT, EvolutionConfig, EvolutionJournal
from kstrl.factory import run_factory
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.serve import ServeConfig, SpendLedger, read_run_spend, serve_cycle
from kstrl.shutdown import StopController
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from kstrl.workqueue import ItemSource, Queue, QueueConfig
from tests.spine_utils import (
    base_config,
    component,
    factory_config,
    init_kstrl_repo,
    make_manifest,
    write_stub_gh,
)
from tests.test_merge_gate_park import (
    HTTP,
    ISSUE,
    REPO,
    _env,
    _ks,
    _manifest_path,
    _park_item,
    _parked_run,
    _real_factory_runner,
    _repo,
    _status,
)

COST = 0.25
COMPLETE = "echo '<promise>COMPLETE</promise>'"

#: A real factory run in its own process, with every engineer call priced
#: at COST. argv: root, manifest path, verify test command, engineer command.
_DRIVER = """
import sys
from pathlib import Path
from kstrl.agents import custom
from kstrl.agents.base import UsageRecord
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, run_factory
from kstrl.manifest import Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

_calls = custom.CustomAgent.usage_records.fget
custom.CustomAgent.usage_records = property(
    lambda self: [UsageRecord(cost_usd=0.25, total_tokens=100, source="test") for _ in _calls(self)]
)
root = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
result = run_factory(
    Manifest.load(manifest_path),
    FactoryConfig(
        use_worktrees=True, create_prs=False, max_parallel=1,
        max_retries=2, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command=sys.argv[3], typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=300.0,
        ),
    ),
    KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0, agent_cmd=sys.argv[4],
        kstrl_branch="", kstrl_branch_explicit=True,
        ui_mode="plain", no_color=True,
    ),
    PlainUI(no_color=True),
    root,
    manifest_path=manifest_path,
)
sys.exit(result.exit_code)
"""


def _price_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = custom.CustomAgent.usage_records.fget
    assert calls is not None
    monkeypatch.setattr(
        custom.CustomAgent,
        "usage_records",
        property(
            lambda self: [
                UsageRecord(cost_usd=COST, total_tokens=100, source="test") for _ in calls(self)
            ]
        ),
    )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _journal(root: Path, run_id: str) -> list[dict[str, Any]]:
    return [e for e in _jsonl(root / ".kstrl" / "evolution.jsonl") if e.get("run_id") == run_id]


def _result_row(root: Path, run_id: str, component_id: str) -> dict[str, Any]:
    (row,) = [
        e
        for e in _journal(root, run_id)
        if e.get("event_type") == "component_result" and e["component_id"] == component_id
    ]
    return row


def _tsv(root: Path) -> dict[str, dict[str, str]]:
    lines = (root / ".kstrl" / "experiments.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, line.split("\t"), strict=False)) for line in lines[1:]]
    return {row["run_id"]: row for row in rows}


def _ks_text(root: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "kstrl", *args, "--root", str(root), "--ui", "plain", "--no-color"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    return proc.stdout


def _reading(root: Path, run_id: str) -> str:
    out = _ks_text(root, "evolve", "--status")
    return next(
        line
        for line in out.splitlines()
        if line.strip().startswith(run_id) and "iterations_all_attempts" in line
    )


def _killed_run(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    """comp-z completes, comp-a fails attempt 1 and hangs in attempt 2; SIGKILL then.

    Returns the root, the manifest path, the state dir, the verify command
    and the engineer command. The engineer ran three times at $0.25 each
    and two of those calls finished, so the killed run recorded $0.50.
    """
    root = tmp_path / "repo"
    init_kstrl_repo(root, ("comp-z", "comp-a"))
    manifest_path = tmp_path / "manifest.json"
    make_manifest([component("comp-z"), component("comp-a", ["comp-z"])]).save(manifest_path)
    state = tmp_path / "state"
    state.mkdir()
    engineer = (
        'case "$(basename "$(pwd)")" in comp-a) '
        f'n=$(cat "{state}/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{state}/count"; '
        f'if [ $n -ge 2 ] && [ ! -f "{state}/resume" ]; then touch "{state}/hang"; sleep 60; fi;; '
        f"esac; {COMPLETE}"
    )
    verify = f'[ "$(basename "$(pwd)")" != comp-a ] || test -f "{state}/pass"'
    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(root), str(manifest_path), verify, engineer],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 120
        while not (state / "hang").exists():
            assert time.monotonic() < deadline, "comp-a never reached attempt 2"
            assert proc.poll() is None, proc.communicate()[0]
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
    killed = Manifest.load(manifest_path)
    assert [(c.id, c.status, c.retries) for c in killed.components] == [
        ("comp-z", ComponentStatus.COMPLETED.value, 0),
        ("comp-a", ComponentStatus.RUNNING.value, 1),
    ]
    assert killed.completed_at == ""
    return root, manifest_path, state, verify, engineer


def _resume(
    root: Path,
    manifest_path: Path,
    verify: str,
    engineer: str,
    **overrides: object,
) -> tuple[int, str]:
    config = factory_config(
        max_retries=2,
        verify_config=VerifyConfig(
            test_command=verify,
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=60.0,
        ),
        **overrides,
    )
    base = base_config(root)
    base.agent_cmd = engineer
    out = io.StringIO()
    result = run_factory(
        Manifest.load(manifest_path),
        config,
        base,
        PlainUI(no_color=True, file=out),
        root,
        manifest_path=manifest_path,
    )
    return result.exit_code, out.getvalue()


def test_a_killed_runs_attempts_readings_and_spend_go_to_the_run_that_resumes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    root, manifest_path, state, verify, engineer = _killed_run(tmp_path)
    killed_run = Manifest.load(manifest_path).run_id
    (state / "resume").touch()
    (state / "pass").touch()
    _price_every_call(monkeypatch)

    exit_code, out = _resume(root, manifest_path, verify, engineer)

    assert exit_code == 0, out
    resumed = Manifest.load(manifest_path)
    run = resumed.run_id
    comp_a = resumed.get_component("comp-a")
    assert comp_a is not None and comp_a.retries == 1
    # The #233 reading: attempt 1 ran in the killed run, attempt 2 in this one.
    assert (
        "iterations_all_attempts=2.00 (1 component(s) ran, 2 iteration(s) across 2 attempt(s))"
        in _reading(root, run)
    )
    # Spend: $0.50 recorded by the killed run plus $0.25 now, in one total.
    assert list(_tsv(root)) == [run]
    assert float(_tsv(root)[run]["total_cost_usd"]) == pytest.approx(0.75)
    journal_cost = sum(
        phase["cost_usd"]
        for row in _journal(root, run)
        if row.get("event_type") in ("component_result", "role_usage")
        for phase in row["usage"].values()
    )
    assert journal_cost == pytest.approx(0.75)
    status = _ks_text(root, "status", "--no-tui", "--manifest", str(manifest_path))
    assert "$0.7500" in next(line for line in status.splitlines() if "Run usage" in line)
    # progress.jsonl counts the retry the manifest counts, under this run.
    retrying = [
        e
        for e in _jsonl(root / ".kstrl" / "progress.jsonl")
        if e.get("run_id") == run and e.get("event") == "component_retrying"
    ]
    assert [(e["component"], e["data"]["attempt"]) for e in retrying] == [("comp-a", 1)]
    assert retrying[0]["data"]["reason"].startswith(f"carried from run {killed_run}: ")
    # The journal: the reading carried under this run names where it ran.
    superseded = [
        (e["attempt"], e["carried_from_run"])
        for e in _journal(root, run)
        if e.get("event_type") == FINDINGS_SUPERSEDED_EVENT and e["component_id"] == "comp-a"
    ]
    assert superseded == [(1, killed_run)]
    result = _result_row(root, run, "comp-a")
    assert (result["retries"], result["first_attempt"]) == (1, 1)
    assert f"Carried from interrupted run {killed_run}" in out


def test_a_resume_counts_the_killed_runs_spend_against_the_cost_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    root, manifest_path, state, verify, engineer = _killed_run(tmp_path)
    (state / "resume").touch()
    (state / "pass").touch()
    _price_every_call(monkeypatch)

    exit_code, out = _resume(root, manifest_path, verify, engineer, max_cost_usd=0.5)

    # The killed run already spent $0.50, so the ceiling holds before a call.
    assert (state / "count").read_text(encoding="utf-8").strip() == "2", out
    assert exit_code == 1, out
    run = Manifest.load(manifest_path).run_id
    # comp-a failed at scheduling without a launch: attempt 1 (carried) ran
    # one iteration, attempt 2 was killed and never ran again.
    assert (
        "iterations_all_attempts=1.00 (1 component(s) ran, 1 iteration(s) across 2 attempt(s))"
        in _reading(root, run)
    )


def test_a_stopped_run_and_its_resume_each_answer_for_their_own_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The graceful stop: comp-a is waiting to retry when the stop lands, so it
    stays PENDING with one retry while the run finishes and records itself."""
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    root = tmp_path / "repo"
    init_kstrl_repo(root, ("comp-a", "comp-b"))
    manifest_path = tmp_path / "manifest.json"
    make_manifest([component("comp-a"), component("comp-b")]).save(manifest_path)
    state = tmp_path / "state"
    state.mkdir()
    # comp-b holds its slot until released (bounded at 60s); comp-a completes.
    engineer = (
        'case "$(basename "$(pwd)")" in comp-b) '
        f'for _ in $(seq 1 600); do [ -f "{state}/release" ] && break; sleep 0.1; done;; '
        f"esac; {COMPLETE}"
    )
    verify = f'[ "$(basename "$(pwd)")" != comp-a ] || test -f "{state}/pass"'
    verify_config = VerifyConfig(
        test_command=verify,
        typecheck_command="true",
        lint_command="true",
        check_diff_scope=False,
        check_bad_patterns=False,
        subprocess_timeout=60.0,
    )
    base = base_config(root)
    base.agent_cmd = engineer
    stop = StopController()
    journal = root / ".kstrl" / "evolution.jsonl"

    def _stop_once_comp_a_is_retrying() -> None:
        # The superseded row is written just before the retry delay starts.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if journal.exists() and any(
                e.get("event_type") == FINDINGS_SUPERSEDED_EVENT
                and e.get("component_id") == "comp-a"
                for e in _jsonl(journal)
            ):
                stop.request("test stop")
                return
            time.sleep(0.05)

    stopper = threading.Thread(target=_stop_once_comp_a_is_retrying, daemon=True)
    stopper.start()
    try:
        first = run_factory(
            Manifest.load(manifest_path),
            factory_config(
                max_retries=2, max_parallel=2, retry_delay=3, verify_config=verify_config
            ),
            base,
            PlainUI(no_color=True, file=io.StringIO()),
            root,
            manifest_path=manifest_path,
            stop=stop,
        )
    finally:
        (state / "release").touch()
        stopper.join(timeout=10)
    stopped = Manifest.load(manifest_path)
    comp_a = stopped.get_component("comp-a")
    assert first.exit_code == 130
    assert comp_a is not None and (comp_a.status, comp_a.retries) == ("pending", 1)
    assert stopped.completed_at != ""
    stopped_run = stopped.run_id

    (state / "pass").touch()
    second = run_factory(
        Manifest.load(manifest_path),
        factory_config(max_retries=2, max_parallel=2, retry_delay=0, verify_config=verify_config),
        base,
        PlainUI(no_color=True, file=io.StringIO()),
        root,
        manifest_path=manifest_path,
    )
    assert second.exit_code != 130
    resumed_run = Manifest.load(manifest_path).run_id

    # The resume answers for attempt 2 only; attempt 1 is the stopped run's.
    assert (
        "iterations_all_attempts=1.00 (1 component(s) ran, 1 iteration(s) across 1 attempt(s))"
        in _reading(root, resumed_run)
    )
    # The stopped run's comp-a row is PENDING: attempt 2 never began there.
    assert (
        "iterations_all_attempts=1.00 (1 component(s) ran, 1 iteration(s) across 2 attempt(s))"
        in _reading(root, stopped_run)
    )
    assert _tsv(root)[resumed_run]["retry_rate"] == "0.00"
    result = _result_row(root, resumed_run, "comp-a")
    assert (result["retries"], result["first_attempt"]) == (1, 2)


def test_a_resume_does_not_take_over_a_component_the_killed_run_left_merge_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a component the resume runs again has its earlier attempts taken
    over. alpha had one retry and then parked MERGE_PENDING; the run was
    killed before its summary. The resume's re-poll finds the PR closed, so
    alpha fails without a launch, and its row answers from its next attempt:
    ``ks evolve --status`` measures the resume instead of refusing it for an
    attempt 1 whose reading this run never took over.

    The kill is the manifest state ``_killed_run`` asserts a SIGKILL leaves
    (the run named, ``completedAt`` empty), written by hand, because alpha
    parks only after the run has nothing left to kill it during."""
    bin_dir = tmp_path / "spine-bin"
    bin_dir.mkdir()
    write_stub_gh(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for var in ("GH_SPINE_CREATE", "GH_SPINE_MERGE", "GH_SPINE_MERGE_SHA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GH_SPINE_VIEW_STATE", "OPEN")
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    root = tmp_path / "repo"
    init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
    manifest_path = tmp_path / "manifest.json"
    make_manifest([component("alpha"), component("beta", ["alpha"])]).save(manifest_path)
    run_factory(
        Manifest.load(manifest_path),
        factory_config(create_prs=True),
        base_config(root),
        PlainUI(no_color=True, file=io.StringIO()),
        root,
        manifest_path=manifest_path,
    )
    killed = Manifest.load(manifest_path)
    alpha = killed.get_component("alpha")
    assert alpha is not None and alpha.status == ComponentStatus.MERGE_PENDING.value
    alpha.retries = 1
    killed.completed_at = ""
    killed.save(manifest_path)

    monkeypatch.setenv("GH_SPINE_VIEW_STATE", "CLOSED")
    out = io.StringIO()
    second = run_factory(
        Manifest.load(manifest_path),
        factory_config(create_prs=True),
        base_config(root),
        PlainUI(no_color=True, file=out),
        root,
        manifest_path=manifest_path,
    )

    assert second.failed == ["alpha"], out.getvalue()
    assert f"Carried from interrupted run {killed.run_id}" in out.getvalue()
    run = Manifest.load(manifest_path).run_id
    result = _result_row(root, run, "alpha")
    assert (result["retries"], result["first_attempt"]) == (0, 1)
    assert (
        "iterations_all_attempts=0.00 (0 component(s) ran, 0 iteration(s) across 2 attempt(s))"
        in _reading(root, run)
    )


@pytest.mark.usefixtures("no_open_prs")
def test_an_approval_run_is_charged_to_the_serve_ledger_that_charged_the_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, f'[intake_github]\nenabled = true\nrepo = "{REPO}"\n')
    env = _env(tmp_path)
    for key in ("PATH", "GH_LOG", "GH_COMMENTS", "GH_PUSHED", "GH_HEAD"):
        monkeypatch.setenv(key, env[key])
    queue = Queue(root, QueueConfig())
    remote = queue.add(
        "# spec\n",
        title="remote",
        source=ItemSource.GITHUB,
        source_ref=f"{REPO}#{ISSUE}",
        target_repo=REPO,
    )
    runner = _real_factory_runner(tmp_path / "manifest.template.json", env, [])
    parked = serve_cycle(root, config=ServeConfig(caffeinate=False), runner=runner)
    assert str(parked.verdict) == "awaiting_approval", parked.reason
    parked_run = Manifest.load(_manifest_path(root)).run_id
    linked = queue.get(remote.item_id)
    assert linked is not None and linked.last_run_id == parked_run
    before = SpendLedger(root).read_state().spend

    approved = _ks(
        root, env, "inbox", "approve", _park_item(root).id, "--ui", "plain", "--no-color"
    )

    out = approved.stdout + approved.stderr
    assert _status(root, HTTP) == "completed", out
    approval_run = Manifest.load(_manifest_path(root)).run_id
    spend = read_run_spend(root, approval_run)
    assert spend.usage_calls >= 1, out
    after = SpendLedger(root).read_state().spend
    assert (after.runs, after.total_calls) == (
        before.runs + 1,
        before.total_calls + spend.usage_calls,
    ), out
    assert after.spent_usd == pytest.approx(before.spent_usd + spend.cost_usd)
    assert "to the ks serve spend ledger" in out
    # The parked run reached its summary, so the approval run took over
    # nothing and the park's spend, which serve already charged, is not in it.
    assert "Carried from interrupted run" not in out
    # The dependent parked under the approval run's own id, so the item
    # still awaits approval and now points at that run (#464).
    item = queue.get(remote.item_id)
    assert item is not None and item.last_run_id == approval_run


@pytest.mark.usefixtures("no_open_prs")
def test_approving_a_park_serve_did_not_make_leaves_the_serve_ledger_alone(
    tmp_path: Path,
) -> None:
    root, env, _tip = _parked_run(tmp_path)

    approved = _ks(
        root, env, "inbox", "approve", _park_item(root).id, "--ui", "plain", "--no-color"
    )

    out = approved.stdout + approved.stderr
    assert _status(root, HTTP) == "completed", out
    spend = SpendLedger(root).read_state().spend
    assert (spend.runs, spend.total_calls) == (0, 0), out


def test_first_attempt_survives_a_save_resets_on_retry_and_refuses_a_bad_value(
    tmp_path: Path,
) -> None:
    comp: Component = component("comp-a")
    comp.status = ComponentStatus.FAILED.value
    comp.retries = 2
    comp.first_attempt = 3
    path = tmp_path / "manifest.json"
    make_manifest([comp]).save(path)
    loaded = Manifest.load(path)
    assert loaded.components[0].first_attempt == 3

    loaded.reset_for_retry("comp-a")
    assert (loaded.components[0].retries, loaded.components[0].first_attempt) == (0, 1)

    raw = json.loads(path.read_text(encoding="utf-8"))
    for bad in (0, True, "2"):
        raw["components"][0]["firstAttempt"] = bad
        assert "components[0].firstAttempt: must be an integer of at least 1" in (
            Manifest.validate_schema(raw)
        )


def test_a_resume_rewrites_only_the_attempts_the_manifest_still_counts(tmp_path: Path) -> None:
    """A component `ks retry` reset starts again at attempt 1, so the killed
    run's rows for it are not this run's; only the owed range is written."""
    config = EvolutionConfig.load(tmp_path)
    journal = EvolutionJournal(config)
    row = {"event_type": FINDINGS_SUPERSEDED_EVENT, "run_id": "old", "iteration_count": 1}
    journal.append_entries(
        [
            {**row, "component_id": "comp-a", "attempt": 1},
            {**row, "component_id": "comp-a", "attempt": 2},
            {**row, "component_id": "comp-b", "attempt": 1},
        ]
    )

    written = journal.carry_superseded("old", "new", {"comp-a": range(2, 3), "comp-b": range(1, 1)})

    assert written == 1
    carried = [e for e in _jsonl(config.journal_path) if e.get("run_id") == "new"]
    assert [(e["component_id"], e["attempt"], e["carried_from_run"]) for e in carried] == [
        ("comp-a", 2, "old")
    ]


def test_a_chain_of_killed_runs_carries_through_to_the_run_that_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 1 is killed in comp-a's attempt 2; run 2 resumes it and is killed in
    the same attempt; run 3 finishes. Run 3 answers for run 1's attempt 1 and
    spend, and the carried row names run 1, where attempt 1 ran."""
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    root, manifest_path, state, verify, engineer = _killed_run(tmp_path)
    first_run = Manifest.load(manifest_path).run_id
    (state / "hang").unlink()
    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(root), str(manifest_path), verify, engineer],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 120
        while not (state / "hang").exists():
            assert time.monotonic() < deadline, "run 2 never reached comp-a's attempt 2"
            assert proc.poll() is None, proc.communicate()[0]
            time.sleep(0.05)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    second_run = Manifest.load(manifest_path).run_id
    assert second_run != first_run
    (state / "resume").touch()
    (state / "pass").touch()
    _price_every_call(monkeypatch)

    exit_code, out = _resume(root, manifest_path, verify, engineer)

    assert exit_code == 0, out
    run = Manifest.load(manifest_path).run_id
    assert f"Carried from interrupted run {second_run}" in out
    assert (
        "iterations_all_attempts=2.00 (1 component(s) ran, 2 iteration(s) across 2 attempt(s))"
        in _reading(root, run)
    )
    assert float(_tsv(root)[run]["total_cost_usd"]) == pytest.approx(0.75)
    superseded = [
        (e["attempt"], e["carried_from_run"])
        for e in _journal(root, run)
        if e.get("event_type") == FINDINGS_SUPERSEDED_EVENT and e["component_id"] == "comp-a"
    ]
    assert superseded == [(1, first_run)]
