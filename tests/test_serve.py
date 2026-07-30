"""R8.6 PR 2: `ks serve` regression tests.

The centre of gravity here is the retry classifier and the four
backstops, because this is the module that spends money unattended. The
rule under test is not "infrastructure errors retry" but the stronger
"NOTHING retries without positive evidence that it was infrastructural",
so most of these tests assert that an ambiguous situation does NOT
retry.

No test runs a real factory. The `FactoryRunner` Protocol exists so the
whole loop is drivable with a stub - a suite that spawned the real thing
would cost dollars per assertion at a measured $1.70-2.60 per iteration.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.findings import Finding
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.serve import (
    BACKOFF_CAP_SECONDS,
    DailySpend,
    RunOutcome,
    RunSpend,
    ServeConfig,
    ServeError,
    ServeLockedError,
    SpendLedger,
    Verdict,
    backoff_seconds,
    caffeinate_prefix,
    check_budget,
    check_cost_coverage,
    check_poison_breaker,
    classify_run,
    consecutive_poison_count,
    next_local_midnight,
    reap_leases,
    resolve_merge_gate,
    serve,
    serve_cycle,
    serve_lock,
)
from kstrl.workqueue import (
    ItemSource,
    ItemState,
    MergeDisposition,
    Queue,
    QueueConfig,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _queue(root: Path, **kwargs: object) -> Queue:
    return Queue(root, QueueConfig(**kwargs))  # type: ignore[arg-type]


def _add(queue: Queue, **kwargs: object) -> object:
    return queue.add("# Spec\n\nDo the thing.\n", **kwargs)  # type: ignore[arg-type]


def _manifest(path: Path, components: list[Component], run_id: str = "r1") -> None:
    """Write a manifest through the real Manifest.save.

    Built from the real dataclasses rather than hand-written JSON: an
    earlier version of this helper invented key names and every
    classification test silently exercised the unreadable-manifest branch
    instead of the branch it claimed to test.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=components,
        run_id=run_id,
    )
    manifest.save(path)


def _component(
    comp_id: str, status: str, findings: list[Finding] | None = None,
) -> Component:
    component = Component(
        comp_id, comp_id, "", [], f"{comp_id}.json", f"branch/{comp_id}",
    )
    component.status = ComponentStatus(status)
    component.findings = list(findings or [])
    return component


def _infra_finding() -> Finding:
    return Finding.infrastructure_error("review", "agent CLI timed out")


def _spec_finding() -> Finding:
    return Finding(
        phase="review",
        category="test_quality",
        severity="fail",
        location="tests/test_x.py",
        explanation="tests assert nothing",
        tags=(),
    )


def _stub_runner(outcome: RunOutcome, calls: list[dict[str, object]] | None = None):
    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
    ) -> RunOutcome:
        if calls is not None:
            calls.append({
                "spec_path": spec_path,
                "project_name": project_name,
                "pause_before_pr_merge": pause_before_pr_merge,
                "timeout_seconds": timeout_seconds,
            })
        return outcome

    return runner


@pytest.fixture(autouse=True)
def _no_spend(monkeypatch: pytest.MonkeyPatch):
    """Default every test to a zero-cost run.

    Reading real spend needs a run dir; tests that care about cost patch
    this explicitly. Defaulting to zero keeps the accounting out of the
    way of the classification tests.
    """
    monkeypatch.setattr(
        "kstrl.serve.read_run_spend", lambda root, run_id: RunSpend(),
    )


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------


class TestClassifierRetriesOnlyWithEvidence:
    """The rule that stands between the queue and an overnight crash loop."""

    def test_exit_zero_is_success(self, tmp_path: Path) -> None:
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=0),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.SUCCESS

    def test_all_infra_failures_retry(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_infra_finding()])])
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=1), manifest_path=path,
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert "comp-a" in outcome.reason

    def test_one_judged_failure_blocks_the_retry(self, tmp_path: Path) -> None:
        """A mixed run is a SPEC failure: the spec failure is the verdict."""
        path = tmp_path / "m.json"
        _manifest(path, [
            _component("comp-a", "failed", [_infra_finding()]),
            _component("comp-b", "failed", [_spec_finding()]),
        ])
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=1), manifest_path=path,
        )
        assert outcome.verdict is Verdict.SPEC_FAILURE
        assert "comp-b" in outcome.reason

    def test_a_failure_with_no_findings_is_a_spec_failure(
        self, tmp_path: Path,
    ) -> None:
        """No findings is not evidence OF infrastructure trouble."""
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [])])
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=1), manifest_path=path,
        )
        assert outcome.verdict is Verdict.SPEC_FAILURE

    def test_an_unreadable_manifest_does_not_retry(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text("{not json")
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=1), manifest_path=path,
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE
        assert not outcome.verdict.may_retry

    def test_a_missing_manifest_does_not_retry(self, tmp_path: Path) -> None:
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=1),
            manifest_path=tmp_path / "absent.json",
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE

    def test_nonzero_exit_with_nothing_failed_does_not_retry(
        self, tmp_path: Path,
    ) -> None:
        """Merge-pending / contract failure: resumable, but not by us."""
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "completed")])
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=1), manifest_path=path,
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE
        assert "no failed component" in outcome.reason

    def test_exit_two_is_a_spec_failure(self, tmp_path: Path) -> None:
        """The architect halted on a blocker; re-running spends the same."""
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=2),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.SPEC_FAILURE
        assert "architect" in outcome.reason

    def test_a_signal_kill_retries(self, tmp_path: Path) -> None:
        """SIGKILL is evidence of an external cause, not a spec verdict."""
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=-9),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert "signal 9" in outcome.reason

    def test_a_timeout_retries(self, tmp_path: Path) -> None:
        outcome = classify_run(
            tmp_path, run=RunOutcome(returncode=-9, timed_out=True),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert "timeout" in outcome.reason or "timed" in outcome.reason.lower()

    def test_a_launch_failure_retries(self, tmp_path: Path) -> None:
        """Nothing was spent, so retrying is free."""
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=-1, launch_error="No such file"),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert "before any spend" in outcome.reason

    def test_only_retry_infra_authorizes_spending(self) -> None:
        assert Verdict.RETRY_INFRA.may_retry
        assert not Verdict.SUCCESS.may_retry
        assert not Verdict.SPEC_FAILURE.may_retry
        assert not Verdict.UNCLASSIFIABLE.may_retry

    def test_every_verdict_carries_a_reason(self, tmp_path: Path) -> None:
        """A machine decision that spends money must say why."""
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_infra_finding()])])
        for run in (
            RunOutcome(returncode=0),
            RunOutcome(returncode=1),
            RunOutcome(returncode=2),
            RunOutcome(returncode=-9),
            RunOutcome(returncode=-9, timed_out=True),
            RunOutcome(returncode=-1, launch_error="boom"),
        ):
            outcome = classify_run(tmp_path, run=run, manifest_path=path)
            assert outcome.reason.strip()

    def test_the_shared_infra_predicate_is_reused(self, tmp_path: Path) -> None:
        """Not a second copy of factory._infra_casualty.

        Two copies of this rule drifting apart is how a spec failure
        becomes retryable, so the classifier must go through
        Finding.is_infrastructure_error rather than string-matching.
        """
        from kstrl.findings import Finding
        from kstrl.serve import _infra_casualty

        class _Comp:
            def __init__(self, findings: list[Finding]) -> None:
                self.findings = findings

        assert _infra_casualty(
            _Comp([Finding.infrastructure_error("review", "cli died")])
        )
        assert not _infra_casualty(_Comp([]))


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------


class TestBackoff:
    def test_grows_exponentially(self) -> None:
        assert backoff_seconds(1) == 60.0
        assert backoff_seconds(2) == 120.0
        assert backoff_seconds(3) == 240.0

    def test_is_capped(self) -> None:
        assert backoff_seconds(50) == BACKOFF_CAP_SECONDS

    def test_zero_attempts_has_no_delay(self) -> None:
        assert backoff_seconds(0) == 0.0

    def test_an_item_inside_its_backoff_is_not_claimed(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        queue.transition(
            item, ItemState.LEASED, reason="t", not_before=future,  # type: ignore[arg-type]
        )
        queue.requeue(queue.items()[0], not_before=future)
        assert queue.next_ready() is None

    def test_a_backed_off_item_does_not_starve_the_others(
        self, tmp_path: Path,
    ) -> None:
        """A flaking item must not block the ones that would succeed."""
        queue = _queue(tmp_path)
        stuck = _add(queue, title="stuck", priority=9)
        fresh = _add(queue, title="fresh")
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        queue.requeue(
            queue.lease(stuck),  # type: ignore[arg-type]
            not_before=future,
        )
        ready = queue.next_ready()
        assert ready is not None
        assert ready.item_id == fresh.item_id  # type: ignore[attr-defined]

    def test_an_unparseable_not_before_holds_off(self, tmp_path: Path) -> None:
        """Err away from launching: the opposite of the lease default."""
        queue = _queue(tmp_path)
        item = _add(queue)
        queue.requeue(queue.lease(item), not_before="not-a-date")  # type: ignore[arg-type]
        assert queue.next_ready() is None


# --------------------------------------------------------------------------
# Spend ledger and the budget
# --------------------------------------------------------------------------


class TestSpendLedger:
    def test_a_fresh_day_starts_at_zero(self, tmp_path: Path) -> None:
        assert SpendLedger(tmp_path).read("2026-07-30").spent_usd == 0.0

    def test_charges_accumulate(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(1.50, today="2026-07-30")
        spend = ledger.charge(2.25, today="2026-07-30")
        assert spend.spent_usd == pytest.approx(3.75)
        assert spend.runs == 2

    def test_a_new_day_resets(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(10.0, today="2026-07-30")
        assert ledger.read("2026-07-31").spent_usd == 0.0

    def test_lower_bound_is_sticky_for_the_day(self, tmp_path: Path) -> None:
        """Once any run under-reported, the day's total is a floor."""
        ledger = SpendLedger(tmp_path)
        ledger.charge(1.0, lower_bound=True, uncovered_calls=2, today="d")
        spend = ledger.charge(1.0, lower_bound=False, today="d")
        assert spend.lower_bound
        assert spend.uncovered_calls == 2

    def test_negative_charges_are_ignored(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        assert ledger.charge(-5.0, today="d").spent_usd == 0.0

    def test_a_corrupt_ledger_reads_as_a_fresh_day(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.path.parent.mkdir(parents=True, exist_ok=True)
        ledger.path.write_text("{not json")
        assert ledger.read("d").spent_usd == 0.0

    def test_round_trip(self) -> None:
        spend = DailySpend("d", 1.5, 2, True, 3)
        assert DailySpend.from_dict(spend.to_dict()) == spend

    def test_non_numeric_fields_decode_to_zero(self) -> None:
        spend = DailySpend.from_dict({"date": "d", "spent_usd": "lots"})
        assert spend.spent_usd == 0.0


class TestBudgetGate:
    def test_an_unset_budget_never_blocks(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(1000.0, today="d")
        assert check_budget(ledger, ServeConfig(), today="d").allowed

    def test_under_budget_is_allowed(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(5.0, today="d")
        config = ServeConfig(daily_budget_usd=20.0)
        assert check_budget(ledger, config, today="d").allowed

    def test_at_budget_blocks_and_pauses(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(20.0, today="d")
        config = ServeConfig(daily_budget_usd=20.0)
        admission = check_budget(ledger, config, today="d")
        assert not admission.allowed
        assert admission.pause_reason
        assert admission.resume_after, "the pause must clear itself"

    def test_the_pause_targets_the_next_local_midnight(
        self, tmp_path: Path,
    ) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(20.0, today="d")
        admission = check_budget(
            ledger, ServeConfig(daily_budget_usd=20.0), today="d",
        )
        deadline = datetime.fromisoformat(admission.resume_after)
        assert deadline > datetime.now(UTC)
        assert deadline <= datetime.now(UTC) + timedelta(days=1, minutes=1)

    def test_a_floor_total_is_labelled_in_the_reason(
        self, tmp_path: Path,
    ) -> None:
        """H4: the operator must not read a floor as a measurement."""
        ledger = SpendLedger(tmp_path)
        ledger.charge(20.0, lower_bound=True, uncovered_calls=4, today="d")
        admission = check_budget(
            ledger, ServeConfig(daily_budget_usd=20.0), today="d",
        )
        assert "FLOOR" in admission.reason

    def test_next_local_midnight_is_in_the_future(self) -> None:
        assert datetime.fromisoformat(next_local_midnight()) > datetime.now(UTC)


class TestCostCoverageGate:
    """A budget over a cost-blind adapter is UNENFORCEABLE, not approximate."""

    def test_no_budget_means_no_coverage_requirement(
        self, tmp_path: Path,
    ) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(0.0, lower_bound=True, uncovered_calls=3, today="d")
        assert check_cost_coverage(ledger, ServeConfig(), today="d").allowed

    def test_a_fresh_day_with_no_runs_is_allowed(self, tmp_path: Path) -> None:
        """Absence of evidence is not evidence of a gap."""
        config = ServeConfig(daily_budget_usd=10.0)
        assert check_cost_coverage(SpendLedger(tmp_path), config, today="d").allowed

    def test_full_coverage_is_allowed(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(2.0, lower_bound=False, today="d")
        config = ServeConfig(daily_budget_usd=10.0)
        assert check_cost_coverage(ledger, config, today="d").allowed

    def test_zero_reported_cost_under_a_budget_blocks(
        self, tmp_path: Path,
    ) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(0.0, lower_bound=True, uncovered_calls=6, today="d")
        config = ServeConfig(daily_budget_usd=10.0)
        admission = check_cost_coverage(ledger, config, today="d")
        assert not admission.allowed
        assert "cannot be enforced" in admission.reason

    def test_the_reason_does_not_estimate_the_missing_spend(
        self, tmp_path: Path,
    ) -> None:
        """Never convert unreported calls into a dollar figure (H4)."""
        ledger = SpendLedger(tmp_path)
        ledger.charge(0.0, lower_bound=True, uncovered_calls=6, today="d")
        admission = check_cost_coverage(
            ledger, ServeConfig(daily_budget_usd=10.0), today="d",
        )
        assert "NOT estimated" in admission.reason

    def test_the_override_is_explicit(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(0.0, lower_bound=True, uncovered_calls=6, today="d")
        config = ServeConfig(daily_budget_usd=10.0, allow_uncovered_cost=True)
        assert check_cost_coverage(ledger, config, today="d").allowed


# --------------------------------------------------------------------------
# The poison breaker
# --------------------------------------------------------------------------


class TestPoisonBreaker:
    def test_no_poison_no_streak(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.finish_ok(queue.start(queue.lease(_add(queue))))  # type: ignore[arg-type]
        assert consecutive_poison_count(queue) == 0

    def test_counts_a_trailing_run_of_poison(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for _ in range(3):
            queue.poison(_add(queue), reason="bad spec")  # type: ignore[arg-type]
        assert consecutive_poison_count(queue) == 3

    def test_a_success_resets_the_streak(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
        queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
        queue.finish_ok(queue.start(queue.lease(_add(queue))))  # type: ignore[arg-type]
        assert consecutive_poison_count(queue) == 0

    def test_non_terminal_transitions_do_not_break_the_streak(
        self, tmp_path: Path,
    ) -> None:
        """A lease between two poisons is not a verdict."""
        queue = _queue(tmp_path)
        queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
        queue.lease(_add(queue))  # type: ignore[arg-type]
        queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
        assert consecutive_poison_count(queue) == 2

    def test_the_breaker_blocks_at_the_limit(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for _ in range(3):
            queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
        admission = check_poison_breaker(queue, ServeConfig(max_consecutive_poison=3))
        assert not admission.allowed
        assert admission.pause_reason
        assert "systemic" in admission.pause_reason

    def test_the_breaker_allows_below_the_limit(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
        assert check_poison_breaker(
            queue, ServeConfig(max_consecutive_poison=3),
        ).allowed

    def test_the_breaker_pause_does_not_auto_resume(
        self, tmp_path: Path,
    ) -> None:
        """Unlike the budget: something systemic needs a human, not a clock."""
        queue = _queue(tmp_path)
        for _ in range(3):
            queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
        admission = check_poison_breaker(queue, ServeConfig())
        assert admission.resume_after == ""


# --------------------------------------------------------------------------
# The lease reaper
# --------------------------------------------------------------------------


class TestReaper:
    def test_a_dead_leased_item_returns_to_queued_for_free(
        self, tmp_path: Path,
    ) -> None:
        """Leasing spends nothing, so recovery costs nothing."""
        queue = _queue(tmp_path)
        item = queue.lease(_add(queue), pid=999999)  # type: ignore[arg-type]
        result = reap_leases(queue)
        assert item.item_id in result.requeued
        reread = queue.items()[0]
        assert reread.state is ItemState.QUEUED
        assert reread.attempts == 0

    def test_a_live_lease_is_left_alone(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.lease(_add(queue), pid=os.getpid())  # type: ignore[arg-type]
        result = reap_leases(queue)
        assert result.requeued == ()
        assert queue.items()[0].state is ItemState.LEASED

    def test_an_expired_lease_is_reaped_even_with_a_live_pid(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, lease_ttl_seconds=1)
        queue.lease(_add(queue), pid=os.getpid())  # type: ignore[arg-type]
        future = datetime.now(UTC) + timedelta(hours=2)
        result = reap_leases(queue, now=future)
        assert len(result.requeued) == 1

    def test_a_dead_running_item_retries_with_backoff(
        self, tmp_path: Path,
    ) -> None:
        """The sleep/crash path: the attempt is spent, the cause is external."""
        queue = _queue(tmp_path, max_attempts=3)
        item = queue.start(queue.lease(_add(queue), pid=999999))  # type: ignore[arg-type]
        result = reap_leases(queue)
        assert item.item_id in result.failed_for_retry
        reread = queue.items()[0]
        assert reread.state is ItemState.QUEUED
        assert reread.attempts == 1, "the spent attempt stays charged"
        assert reread.not_before, "a reaped retry waits out a backoff"

    def test_a_dead_running_item_poisons_when_out_of_attempts(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, max_attempts=1)
        item = queue.start(queue.lease(_add(queue), pid=999999))  # type: ignore[arg-type]
        result = reap_leases(queue)
        assert item.item_id in result.poisoned
        reread = queue.items()[0]
        assert reread.state is ItemState.POISON
        assert "no attempts left" in reread.poison_reason

    def test_a_foreign_host_lease_is_not_reaped_on_pid(
        self, tmp_path: Path,
    ) -> None:
        """We cannot probe a foreign pid; the TTL is the only signal."""
        queue = _queue(tmp_path)
        queue.lease(_add(queue), pid=999999, host="some-other-machine")  # type: ignore[arg-type]
        assert reap_leases(queue).requeued == ()

    def test_a_foreign_host_lease_still_expires(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, lease_ttl_seconds=1)
        queue.lease(_add(queue), pid=999999, host="some-other-machine")  # type: ignore[arg-type]
        future = datetime.now(UTC) + timedelta(hours=2)
        assert len(reap_leases(queue, now=future).requeued) == 1

    def test_the_reaper_records_why_in_the_journal(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        item = queue.lease(_add(queue), pid=999999)  # type: ignore[arg-type]
        reap_leases(queue)
        reasons = [e["reason"] for e in queue.journal_entries(item.item_id)]
        assert any("reaped" in r for r in reasons)


# --------------------------------------------------------------------------
# The merge gate must survive continuous intake
# --------------------------------------------------------------------------


class TestMergeGate:
    def test_stop_at_pr_without_the_ladder(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue, merge_disposition=MergeDisposition.STOP_AT_PR)
        gate = resolve_merge_gate(item, tmp_path)  # type: ignore[arg-type]
        assert gate.pause_before_pr_merge
        assert not gate.refusal

    def test_auto_merge_without_the_ladder(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue, merge_disposition=MergeDisposition.AUTO_MERGE)
        gate = resolve_merge_gate(item, tmp_path)  # type: ignore[arg-type]
        assert not gate.pause_before_pr_merge

    def test_the_ladder_withholds_auto_merge_at_l1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ladder may always withhold a permission."""
        monkeypatch.setenv("KSTRL_AUTONOMY_ENABLED", "1")
        queue = _queue(tmp_path)
        item = _add(queue, merge_disposition=MergeDisposition.AUTO_MERGE)
        gate = resolve_merge_gate(item, tmp_path)  # type: ignore[arg-type]
        assert gate.pause_before_pr_merge
        assert not gate.refusal
        assert any("withholds" in note for note in gate.notes)

    def test_remote_items_default_to_stop_at_pr(self, tmp_path: Path) -> None:
        """Continuous intake must not silently delete the merge gate."""
        queue = _queue(tmp_path)
        item = _add(
            queue, source=ItemSource.GITHUB, source_ref="o/r#1",
        )
        gate = resolve_merge_gate(item, tmp_path)  # type: ignore[arg-type]
        assert gate.pause_before_pr_merge


# --------------------------------------------------------------------------
# caffeinate
# --------------------------------------------------------------------------


class TestCaffeinate:
    def test_disabled_yields_no_prefix(self) -> None:
        assert caffeinate_prefix(False) == []

    def test_non_darwin_yields_no_prefix(self) -> None:
        with patch("kstrl.serve.sys.platform", "linux"):
            assert caffeinate_prefix(True) == []

    def test_darwin_with_the_binary_uses_idle_only(self) -> None:
        with patch("kstrl.serve.sys.platform", "darwin"):
            with patch("kstrl.serve.shutil.which", return_value="/usr/bin/caffeinate"):
                assert caffeinate_prefix(True) == ["/usr/bin/caffeinate", "-i"]

    def test_a_missing_binary_degrades_rather_than_failing(self) -> None:
        with patch("kstrl.serve.sys.platform", "darwin"):
            with patch("kstrl.serve.shutil.which", return_value=None):
                assert caffeinate_prefix(True) == []


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class TestServeConfig:
    def test_defaults(self) -> None:
        config = ServeConfig()
        assert config.poll_interval_seconds == 60.0
        assert config.daily_budget_usd == 0.0
        assert config.max_consecutive_poison == 3
        assert config.caffeinate
        assert not config.allow_uncovered_cost

    @pytest.mark.parametrize("kwargs", [
        {"poll_interval_seconds": 0},
        {"daily_budget_usd": -1},
        {"max_consecutive_poison": 0},
        {"factory_timeout_seconds": -1},
    ])
    def test_invalid_values_are_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ServeError):
            ServeConfig(**kwargs)  # type: ignore[arg-type]

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_SERVE_DAILY_BUDGET_USD", "25.5")
        monkeypatch.setenv("KSTRL_SERVE_CAFFEINATE", "0")
        config = ServeConfig.from_env()
        assert config.daily_budget_usd == 25.5
        assert not config.caffeinate

    def test_load_reads_the_toml_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\ndaily_budget_usd = 12.0\nmax_consecutive_poison = 5\n"
        )
        config = ServeConfig.load(tmp_path)
        assert config.daily_budget_usd == 12.0
        assert config.max_consecutive_poison == 5

    def test_env_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[serve]\ndaily_budget_usd = 12.0\n")
        monkeypatch.setenv("KSTRL_SERVE_DAILY_BUDGET_USD", "3.0")
        assert ServeConfig.load(tmp_path).daily_budget_usd == 3.0


# --------------------------------------------------------------------------
# The singleton lock
# --------------------------------------------------------------------------


class TestServeLock:
    def test_a_second_daemon_is_refused(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        with serve_lock(tmp_path):
            with pytest.raises(ServeLockedError):
                with serve_lock(tmp_path):
                    pass

    def test_it_is_released_on_exit(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        with serve_lock(tmp_path):
            pass
        with serve_lock(tmp_path):
            pass

    def test_it_is_distinct_from_the_queue_mutex(self, tmp_path: Path) -> None:
        """`ks queue ls` must keep working while the daemon runs."""
        pytest.importorskip("fcntl")
        from kstrl.workqueue import queue_lock

        with serve_lock(tmp_path):
            with queue_lock(tmp_path):
                pass

    def test_it_records_the_holder_pid(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        from kstrl.serve import SERVE_LOCK_FILENAME
        from kstrl.workqueue import queue_root

        with serve_lock(tmp_path):
            content = (queue_root(tmp_path) / SERVE_LOCK_FILENAME).read_text()
        assert content.strip() == str(os.getpid())


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


class TestServeCycle:
    def test_an_empty_queue_does_nothing(self, tmp_path: Path) -> None:
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.ran_item == ""
        assert result.skipped == "nothing ready"

    def test_a_successful_item_finishes_done(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.verdict is Verdict.SUCCESS
        assert queue.items()[0].state is ItemState.DONE

    def test_one_item_per_cycle(self, tmp_path: Path) -> None:
        """Two factory runs on one repo is what factory.lock exists to stop."""
        queue = _queue(tmp_path)
        _add(queue)
        _add(queue)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert len(calls) == 1

    def test_a_spec_failure_poisons_without_retrying(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_spec_finding()])],
        )
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.verdict is Verdict.SPEC_FAILURE
        item = queue.items()[0]
        assert item.state is ItemState.POISON
        assert item.attempts == 1, "poisoned after ONE attempt, not three"

    def test_an_infra_failure_retries_with_backoff(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
        )
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.verdict is Verdict.RETRY_INFRA
        item = queue.items()[0]
        assert item.state is ItemState.QUEUED
        assert item.not_before, "the retry waits out a backoff"

    def test_an_infra_failure_poisons_once_attempts_run_out(
        self, tmp_path: Path,
    ) -> None:
        """Even a legitimately retryable failure is bounded."""
        queue = _queue(tmp_path, max_attempts=1)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
        )
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        item = queue.items()[0]
        assert item.state is ItemState.POISON
        assert "no attempts left" in item.poison_reason

    def test_an_unclassifiable_failure_poisons(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.verdict is Verdict.UNCLASSIFIABLE
        assert queue.items()[0].state is ItemState.POISON

    def test_the_attempt_is_charged_before_the_run(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        seen: list[int] = []

        def runner(**kwargs: object) -> RunOutcome:
            seen.append(queue.items()[0].attempts)
            return RunOutcome(0)

        serve_cycle(tmp_path, runner=runner)  # type: ignore[arg-type]
        assert seen == [1], "the attempt must be on disk before any spend"

    def test_the_merge_gate_reaches_the_runner(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue, merge_disposition=MergeDisposition.STOP_AT_PR)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert calls[0]["pause_before_pr_merge"] is True

    def test_auto_merge_reaches_the_runner(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue, merge_disposition=MergeDisposition.AUTO_MERGE)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert calls[0]["pause_before_pr_merge"] is False

    def test_a_paused_queue_runs_nothing(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        queue.pause(reason="operator")
        calls: list[dict[str, object]] = []
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert calls == []
        assert "paused" in result.skipped

    def test_an_elapsed_pause_window_resumes_itself(
        self, tmp_path: Path,
    ) -> None:
        """What makes the daily-budget stop self-healing.

        Asserts the marker is actually CLEARED, not merely that it reads
        as inactive. An elapsed `resume_after` already makes
        `is_paused()` false on its own, so the weaker assertion passed
        with the `resume()` call removed and pinned nothing.
        """
        queue = _queue(tmp_path)
        _add(queue)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        queue.pause(reason="budget", resume_after=past)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.ran_item
        assert not queue.is_paused()
        assert queue.pause_state().paused is False, "the marker must be cleared"
        assert any(
            entry["to"] == "running" and entry["reason"] == "resumed"
            for entry in queue.journal_entries()
        ), "the resume must be journaled"

    def test_an_exhausted_budget_pauses_before_spending(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        SpendLedger(tmp_path).charge(50.0)
        calls: list[dict[str, object]] = []
        result = serve_cycle(
            tmp_path,
            config=ServeConfig(daily_budget_usd=10.0, allow_uncovered_cost=True),
            runner=_stub_runner(RunOutcome(0), calls),
        )
        assert calls == [], "the budget must block BEFORE the run"
        assert result.paused
        assert queue.is_paused()

    def test_the_poison_breaker_pauses_the_queue(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for _ in range(3):
            queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
        _add(queue)
        calls: list[dict[str, object]] = []
        result = serve_cycle(
            tmp_path,
            config=ServeConfig(max_consecutive_poison=3),
            runner=_stub_runner(RunOutcome(0), calls),
        )
        assert calls == []
        assert result.paused
        assert queue.is_paused()

    def test_a_held_factory_lock_waits_without_charging(
        self, tmp_path: Path,
    ) -> None:
        """Someone else owns the repo; that is not the item's fault."""
        queue = _queue(tmp_path)
        _add(queue)
        calls: list[dict[str, object]] = []
        with patch("kstrl.serve.factory_lock_held", return_value=True):
            result = serve_cycle(
                tmp_path, runner=_stub_runner(RunOutcome(0), calls),
            )
        assert calls == []
        assert "already holds" in result.skipped
        item = queue.items()[0]
        assert item.state is ItemState.QUEUED
        assert item.attempts == 0

    def test_the_cycle_reaps_before_admitting(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stale = queue.lease(_add(queue), pid=999999)  # type: ignore[arg-type]
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert stale.item_id in result.reaped.requeued

    def test_the_cycle_sweeps_staging(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ensure_dirs()
        (queue.staging_path / "q-ghost").mkdir(parents=True)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.swept_staging == 1

    def test_spend_is_charged_even_on_a_failure(self, tmp_path: Path) -> None:
        """A classification bug must not also lose the accounting."""
        queue = _queue(tmp_path)
        _add(queue)
        with patch(
            "kstrl.serve.read_run_spend",
            return_value=RunSpend(cost_usd=2.50, cost_calls=1, usage_calls=1),
        ):
            result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.charged_usd == pytest.approx(2.50)
        assert SpendLedger(tmp_path).read().spent_usd == pytest.approx(2.50)

    def test_a_poisoned_item_files_an_inbox_item(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert any(result.inbox_items)
        from kstrl.inbox import Inbox, InboxConfig

        items = Inbox(tmp_path, InboxConfig.load(tmp_path)).open_items()
        assert any("poisoned" in item.title for item in items)

    def test_a_full_inbox_stops_admitting_work(self, tmp_path: Path) -> None:
        """R8.3 documented open_item_cap as R8.6's backstop; this uses it."""
        queue = _queue(tmp_path)
        _add(queue)
        calls: list[dict[str, object]] = []
        with patch("kstrl.serve.check_inbox_cap") as gate:
            from kstrl.serve import Admission

            gate.return_value = Admission(allowed=False, reason="inbox full")
            result = serve_cycle(
                tmp_path, runner=_stub_runner(RunOutcome(0), calls),
            )
        assert calls == []
        assert result.skipped == "inbox full"

    def test_priority_order_is_honored(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue, title="low")
        _add(queue, title="urgent", priority=9)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        done = [i for i in queue.items() if i.state is ItemState.DONE]
        assert done[0].title == "urgent"


class TestServeLoop:
    def test_once_runs_a_single_cycle(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        _add(queue)
        results = serve(tmp_path, once=True, runner=_stub_runner(RunOutcome(0)))
        assert len(results) == 1
        assert len([i for i in queue.items() if i.state is ItemState.DONE]) == 1

    def test_max_cycles_bounds_the_loop(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for _ in range(3):
            _add(queue)
        slept: list[float] = []
        results = serve(
            tmp_path,
            runner=_stub_runner(RunOutcome(0)),
            max_cycles=3,
            sleeper=slept.append,
        )
        assert len(results) == 3
        assert len([i for i in queue.items() if i.state is ItemState.DONE]) == 3

    def test_the_loop_sleeps_between_cycles(self, tmp_path: Path) -> None:
        slept: list[float] = []
        serve(
            tmp_path,
            config=ServeConfig(poll_interval_seconds=42.0),
            runner=_stub_runner(RunOutcome(0)),
            max_cycles=2,
            sleeper=slept.append,
        )
        assert slept == [42.0]

    def test_once_does_not_sleep(self, tmp_path: Path) -> None:
        slept: list[float] = []
        serve(
            tmp_path, once=True, runner=_stub_runner(RunOutcome(0)),
            sleeper=slept.append,
        )
        assert slept == []

    def test_the_loop_holds_the_singleton_lock(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        with serve_lock(tmp_path):
            with pytest.raises(ServeLockedError):
                serve(tmp_path, once=True, runner=_stub_runner(RunOutcome(0)))
