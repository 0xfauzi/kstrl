"""#433 increment 2 (PR #552): defects the independent verifier measured.

Each test was measured red on the PR head and green with the fix beside
it. The families are the ones increment 1's verifier found: a claim of
"running" or "alive" from an input that no longer attests it, a join by
something weaker than the schema's key, and state a refresh throws away.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.inbox import Inbox, InboxConfig, InboxScan, ItemKind
from kstrl.reducer import ComponentState, RunState
from kstrl.tui.agent_health import UNKNOWN, agent_health
from kstrl.tui.home_data import HomeStats
from kstrl.tui.home_view import attention_line
from kstrl.tui.integration_view import FIXED, read_integration_review
from kstrl.tui.operator_queue import build_queue, newest_finished_factory
from kstrl.tui.runs import RunRef
from kstrl.tui.serve_view import read_serve_state

NOW = 1_800_000_000.0
RUN_A = "factory-20260920-000000.000000-aaaa"
RUN_B = "factory-20260925-000000.000000-bbbb"


def _dead_pid() -> int:
    """A pid this test spawned and reaped, so nothing holds it now."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


class TestIntegrationJoinIsPerFeature:
    """state.json is rewritten per FEATURE and a new feature's ids restart
    at IF-1 (``integration_state.fresh_state`` + ``next_finding_ids``), so
    the id alone does not name this run's finding once another feature
    has run. Joining by the bare id showed feature B's IF-1 (its text and
    its fix) as run A's IF-1."""

    def _write(self, root: Path, *, state_base: str) -> Path:
        run_dir = root / ".kstrl" / "runs" / RUN_A
        (run_dir / "integration").mkdir(parents=True)
        (run_dir / "integration" / "review-1.json").write_text(
            json.dumps(
                {
                    "runId": RUN_A,
                    "outcome": "open_findings",
                    "featureBaseSha": "a" * 40,
                    "opened": [{"id": "IF-1", "text": "run A's finding"}],
                }
            ),
            encoding="utf-8",
        )
        state = root / ".kstrl" / "integration" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text(
            json.dumps(
                {
                    "featureBaseSha": state_base,
                    "findings": [
                        {
                            "id": "IF-1",
                            "status": "closed",
                            "text": "feature B's finding",
                            "history": [{"runId": RUN_B, "event": "opened"}],
                        }
                    ],
                    "fixes": [{"id": "integration-fix-1", "findings": ["IF-1"]}],
                }
            ),
            encoding="utf-8",
        )
        return run_dir

    def test_another_features_state_file_is_not_this_runs_disposition(self, tmp_path: Path) -> None:
        run_dir = self._write(tmp_path, state_base="b" * 40)
        review = read_integration_review(tmp_path, run_dir)
        assert review is not None
        finding = review.findings[0]
        assert finding.disposition == UNKNOWN, finding
        assert finding.text == "run A's finding", finding

    def test_an_id_this_run_opened_but_another_run_opened_in_state_is_unknown(
        self, tmp_path: Path
    ) -> None:
        """Two features cut from the same base commit share featureBaseSha;
        the finding's own history still says which run opened it."""
        run_dir = self._write(tmp_path, state_base="a" * 40)
        review = read_integration_review(tmp_path, run_dir)
        assert review is not None
        assert review.findings[0].disposition == UNKNOWN, review.findings[0]
        assert review.count(FIXED) == 0


class TestServeItemAfterTheDaemonDied:
    """A ``kill -9`` of ks serve leaves the item in ``running/`` and nothing
    reaps it until the next daemon starts. With the daemon's flock free
    and the lease holder gone, the item is not running."""

    def _stranded(self, root: Path) -> None:
        from kstrl.workqueue import Queue

        queue = Queue(root)
        item = queue.add("spec", title="slice three")
        queue.start(queue.lease(item, pid=_dead_pid()))
        (root / ".kstrl" / "queue" / "serve.lock").write_text("1\n", encoding="utf-8")

    def test_a_stranded_item_is_not_in_flight(self, tmp_path: Path) -> None:
        self._stranded(tmp_path)
        state = read_serve_state(tmp_path, "", factory_lock_held=False)
        assert state is not None
        assert state.in_flight == (), state.items
        assert state.items[0].state != "running", state.items

    def test_home_does_not_list_it_as_running(self, tmp_path: Path) -> None:
        self._stranded(tmp_path)
        queue = build_queue(tmp_path, [], {}, {}, NOW)
        assert [row.state for row in queue.active] != ["running"], queue.active


class TestHeartbeatPidIsEvidenceOnlyWhileFresh:
    """The heartbeat pid is the WORKER that ran the engineer. In pool mode
    the worker returns before review and security run in the parent, and
    ProcessPoolExecutor keeps the worker for the next component, so the
    pid probe says "alive" about a process that no longer runs this
    component (or "exited" once the pool shuts down). The pid is evidence
    only while its heartbeat is recent."""

    def _comp(self, heartbeat_age: float) -> ComponentState:
        return ComponentState(
            component_id="api",
            status="verifying",
            heartbeat_pid=os.getpid(),
            last_heartbeat_ts=NOW - heartbeat_age,
        )

    def test_a_pid_whose_heartbeat_stopped_is_not_probed(self, tmp_path: Path) -> None:
        for alive in (True, False):
            health = agent_health(tmp_path, self._comp(600), NOW, probe=lambda _pid, a=alive: a)
            assert health.process == UNKNOWN, (alive, health)

    def test_a_fresh_heartbeat_is_still_probed(self, tmp_path: Path) -> None:
        health = agent_health(tmp_path, self._comp(5), NOW, probe=lambda _pid: True)
        assert health.process == "alive"


class TestInboxIsReadOnce:
    """The needs-you rows and the "nothing waiting" claim came from two
    separate reads of the inbox log: ``scan()`` for readability, then
    ``open_items()``, which scans again and returns [] when that second
    read fails. One read decides both."""

    def test_a_second_read_failing_does_not_count_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        Inbox(tmp_path, InboxConfig()).add(ItemKind.HALTED_RUN, "halted", dedupe_key="h")
        real_scan = Inbox.scan
        calls = {"n": 0}

        def flaky(self: Inbox) -> InboxScan:
            calls["n"] += 1
            return real_scan(self) if calls["n"] == 1 else InboxScan(unreadable=True)

        monkeypatch.setattr(Inbox, "scan", flaky)
        queue = build_queue(tmp_path, [], {}, {}, NOW)
        assert queue.decisions == 1, (queue.decisions, queue.unreadable)


# -- tests for the verifier's still-green plants --------------------------------

_REF_ROOT = Path("/nonexistent-root")


def _factory_ref(run_id: str) -> RunRef:
    run_dir = _REF_ROOT / ".kstrl" / "runs" / run_id
    return RunRef(run_id, run_dir, run_dir / "events.jsonl", NOW, False, kind="factory")


class TestPlantsTheSuiteMissed:
    def test_a_disabled_inbox_is_not_counted_as_nothing_waiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plant P1: a disabled inbox counted as zero decisions."""
        monkeypatch.delenv("KSTRL_INBOX_ENABLED", raising=False)
        (tmp_path / "kstrl.toml").write_text("[inbox]\nenabled = false\n", encoding="utf-8")
        queue = build_queue(tmp_path, [], {}, {}, NOW)
        assert queue.unreadable == ("inbox disabled",)
        assert queue.decisions is None
        stats = HomeStats(last=None, inbox_open=queue.decisions, failed_components=queue.failures)
        assert "nothing is waiting" not in attention_line(stats).plain

    def test_the_branch_probe_asks_for_a_branch_not_any_ref(self, tmp_path: Path) -> None:
        """Plant P4: without ``refs/heads/`` a TAG named like the failed
        branch answers 0, and prepare_retry would run ``git branch -D``
        on a branch that does not exist."""
        from kstrl.retry_plan import failed_branch_probe
        from tests.helpers.gitrepo import git_in, set_identity

        git_in(tmp_path, "init", "-q", "-b", "main")
        set_identity(tmp_path)
        git_in(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
        git_in(tmp_path, "tag", "kstrl/api")
        assert failed_branch_probe(tmp_path, "kstrl/api") != 0
        git_in(tmp_path, "branch", "kstrl/web")
        assert failed_branch_probe(tmp_path, "kstrl/web") == 0

    def test_a_status_kstrl_does_not_write_is_unknown_not_open(self, tmp_path: Path) -> None:
        """Plant P7: an unrecognised status read as open."""
        run_dir = tmp_path / ".kstrl" / "runs" / RUN_A
        (run_dir / "integration").mkdir(parents=True)
        (run_dir / "integration" / "review-1.json").write_text(
            json.dumps({"outcome": "open_findings", "opened": [{"id": "IF-1", "text": "t"}]}),
            encoding="utf-8",
        )
        state = tmp_path / ".kstrl" / "integration" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text(
            json.dumps({"findings": [{"id": "IF-1", "status": "dismissed"}], "fixes": []}),
            encoding="utf-8",
        )
        review = read_integration_review(tmp_path, run_dir)
        assert review is not None
        finding = review.findings[0]
        assert (finding.disposition, finding.detail) == (
            UNKNOWN,
            "status 'dismissed' is not one kstrl writes",
        )

    def test_delivery_skips_a_newer_run_that_delivered_nothing(self) -> None:
        """Plant P9: home's delivery section described the newest finished
        factory run even when an older one holds the merges."""
        newer, older = _factory_ref(RUN_B), _factory_ref(RUN_A)
        empty = RunState(finished=True)
        merged = RunState(finished=True)
        merged.components["api"] = ComponentState(
            component_id="api", status="completed", pr_state="merged", pr_number=8
        )
        chosen = newest_finished_factory([newer, older], {RUN_B: empty, RUN_A: merged})
        assert chosen is older

    def test_retry_narration_puts_warnings_first_and_raises_the_severity(self) -> None:
        """Plant P13: the #537 sweep warning shown after the plan lines, as
        an information toast that times out in 10 s."""
        from kstrl.tui.screens.retry import _notify_narration

        seen: list[tuple[str, dict[str, object]]] = []

        class _App:
            def notify(self, message: str, **kwargs: object) -> None:
                seen.append((message, kwargs))

        _notify_narration(_App(), ["Retry plan", "Component: api", "WARN: killed pid 4242"])
        message, kwargs = seen[0]
        assert message.splitlines()[0] == "WARN: killed pid 4242"
        assert kwargs["severity"] == "warning"
