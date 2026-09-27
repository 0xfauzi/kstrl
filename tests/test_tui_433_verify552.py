"""#433 increment 2 (PR #552): defects the independent verifier measured,
kept where they reach a real artifact.

Each test was measured red on the PR head and green with the fix beside
it. What remains reads real files under a temporary root: the
integration review joined per feature (another feature's ``state.json``
is not this run's disposition, and a status kstrl never writes is
unknown rather than open); a serve item stranded by a ``kill -9`` of the
daemon, leased to a pid this test spawned and reaped, is not in flight
and home does not list it as running; a disabled inbox is not counted as
nothing waiting; and the failed-branch probe on a real repository asks
for a branch, not any ref, so a tag named like the branch answers no.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.tui.agent_health import UNKNOWN
from kstrl.tui.home_data import HomeStats
from kstrl.tui.home_view import attention_line
from kstrl.tui.integration_view import FIXED, read_integration_review
from kstrl.tui.operator_queue import build_queue
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
