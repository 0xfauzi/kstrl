"""Chunk 1 (TUI rewrite): schema-v2 event model, sinks and run layout, on disk.

The load-bearing test here is golden parity: V1CompatSink fed stamped v2
events must produce byte-equivalent progress.jsonl lines (modulo ts) to
calling the real ProgressLog convenience methods directly. That parity
is what lets the whole migration keep .kstrl/progress.jsonl consumers
(ks status v1 arm, the Linear ProgressSink) untouched. Beside it: reading
a missing or torn events.jsonl, a seeded replay of every registered type
through JsonlSink, a sink added late, the run directory layout, append and
reopen, and the budget halt and coverage events reaching both sinks with
every field (the divergence a green suite once shipped). The registry
census stays so a new event type without a sample fails loudly. The
in-memory round-trip, tolerant-decode and bus-envelope tests were removed;
``tests/test_event_stream.py`` drives the dual write through run_factory.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from kstrl import event_catalog
from kstrl import events as ev
from kstrl.observability import ProgressLog, read_progress_events


def _sample_events() -> list[event_catalog.Event]:
    """One instance of every registered concrete type, non-default payloads."""
    return [
        event_catalog.RunStarted(project="proj", components=3),
        event_catalog.ComponentStarted(component="comp-a"),
        event_catalog.ComponentCompleted(component="comp-a", duration_seconds=12.34, iterations=4),
        event_catalog.ComponentFailed(component="comp-b", error="boom"),
        event_catalog.ComponentSkipped(component="comp-c", reason="operator stopped"),
        event_catalog.CircuitBreakerTripped(component="comp-b", iterations=5, error="no progress"),
        event_catalog.ComponentRetrying(component="comp-b", attempt=2, reason="verify failed"),
        event_catalog.VerificationResultEvent(
            component="comp-a",
            passed=True,
            checks=("tests", "lint"),
            failures=(),
            duration_seconds=3.5,
        ),
        event_catalog.ReviewResultEvent(
            component="comp-a",
            passed=False,
            mode="hard",
            fail_count=2,
            advisory_count=1,
            duration_seconds=60.0,
        ),
        event_catalog.ComponentUsage(
            component="comp-a",
            phase="engineer",
            calls=3,
            known_calls=2,
            unreported_calls=1,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_creation_tokens=5,
            total_tokens=165,
            cost_usd=0.123456,
            duration_seconds=42.0,
        ),
        event_catalog.BudgetExceeded(
            component="comp-a",
            total_tokens=100,
            max_total_tokens=50,
            coverage=({"ceiling": "max_cost_usd", "covered_calls": 8},),
        ),
        event_catalog.BudgetCoverage(
            ceiling="max_cost_usd",
            axis="cost",
            calls=13,
            covered_calls=8,
            uncovered_calls=5,
            uncovered_tokens=193633,
            uncovered_roles=("review",),
            detail="cost coverage is PARTIAL",
        ),
        event_catalog.ContractResult(tier=1, passed=False, breaker="comp-a", duration_seconds=9.9),
        event_catalog.RunCompleted(
            completed=2,
            failed=1,
            skipped=0,
            duration_seconds=100.0,
            release_ref="a1b2c3d4" * 5,
            release_ref_rule="last_merge_by_completed_at",
            release_withheld="no_driver",
        ),
        event_catalog.MergePendingV1(
            component="comp-a", pr_url="http://pr/1", error="not confirmed"
        ),
        event_catalog.PhaseSkipped(component="comp-a", phase="security", reason="budget"),
        event_catalog.DiffFetchFailed(component="comp-a", error="git failed"),
        event_catalog.ReviewDivergence(
            component="comp-a",
            attempts=(1, 2, 3),
            lines_changed=(612, 1408, 2907),
            files_changed=(4, 6, 7),
            blocking_findings=(6, 1, 10),
            blocked=True,
        ),
        event_catalog.AdversarialAgentSelected(
            phase="review",
            agent_source="config",
            identity="codex (gpt-5)",
            agent_type="codex",
            model="gpt-5",
            homogeneous=True,
        ),
        event_catalog.RunPlan(
            components=({"id": "comp-a", "title": "A", "deps": []},),
            max_total_tokens=1000,
            max_adversarial_calls=10,
        ),
        event_catalog.ComponentScopeResolved(
            component="comp-a",
            scope_source="component_prd",
            origin="scripts/kstrl/feature/comp-a/prd.json",
            allowed_paths=("src/", "tests/"),
            harness_paths=("scripts/kstrl/codebase_map.md",),
            manifest_status="completed",
        ),
        event_catalog.PhaseStarted(component="comp-a", phase="review", attempt=1),
        event_catalog.PhaseCompleted(
            component="comp-a", phase="review", passed=True, detail="", duration_seconds=30.0
        ),
        event_catalog.IterationStarted(component="comp-a", iteration=1, max_iterations=10),
        event_catalog.IterationCompleted(
            component="comp-a", iteration=1, duration_seconds=20.0, completed=False, timed_out=False
        ),
        event_catalog.WorkerHeartbeat(component="comp-a", pid=123, elapsed_seconds=45.0),
        event_catalog.CheckpointRequested(
            component="comp-a", kind="checkpoint", question="Approve?"
        ),
        event_catalog.CheckpointResolved(
            component="comp-a", kind="checkpoint", decision="approved", decided_by="operator"
        ),
        event_catalog.PrCreated(component="comp-a", pr_number=7, pr_url="http://pr/7"),
        event_catalog.PrMerged(
            component="comp-a", pr_number=7, pr_url="http://pr/7", merge_sha="a" * 40
        ),
        event_catalog.PrMergePending(component="comp-a", pr_url="http://pr/7", error="pending"),
        event_catalog.DistillResult(
            component="comp-a", facts_written=3, duration_seconds=12.0, parse_failed=True
        ),
        event_catalog.FactUtilizationMeasured(
            component="comp-a",
            measured=True,
            injected=5,
            referenced=2,
            reason="",
            core_injected=2,
            core_referenced=2,
            dependency_injected=1,
            dependency_referenced=0,
            sibling_injected=2,
            sibling_referenced=0,
        ),
        event_catalog.FindingRecorded(
            component="comp-a",
            phase="review",
            category="test_quality",
            severity="fail",
            location="a.py:10",
            explanation="weak assert",
            attempt=1,
        ),
        event_catalog.SpecIssueRecorded(
            severity="blocker",
            kind="ambiguity",
            summary="Spec contradicts itself",
            location="spec.md:12",
            suggestion="pick one",
        ),
        event_catalog.ArtifactWritten(
            component="comp-a", label="prd", path="scripts/kstrl/feature/comp-a/prd.json"
        ),
        event_catalog.Log(severity="warn", kind="kv", key="Root", text="/tmp/x"),
        event_catalog.AutonomyTransition(
            direction="demote",
            from_level=3,
            to_level=2,
            actor="system",
            trigger="policy_violation",
            reason="envelope breach",
        ),
        event_catalog.AutonomyLevelApplied(
            level=1,
            label="L1 Supervised",
            flags=("merge gate: ON (human approves)",),
            overrides=("[factory] pause_before_pr_merge=False contradicts L1",),
        ),
        event_catalog.JournalRepaired(detail="the preceding line was not newline-terminated"),
    ]


class TestRoundTrip:
    def test_sample_covers_registry(self) -> None:
        """Every registered type except the UnknownEvent fallback is in
        the pool the seeded replay draws from; a new event added without
        a sample here fails loudly."""
        sampled = {type(e).type for e in _sample_events()}
        registered = set(event_catalog._REGISTRY) - {"unknown"}
        assert sampled == registered


class TestReadEvents:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert ev.read_events(tmp_path / "nope.jsonl") == []

    def test_torn_tail_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        good = event_catalog.Log(text="hello").to_json_line()
        p.write_text(good + "\n" + good[: len(good) // 2])
        events = ev.read_events(p)
        assert len(events) == 1
        assert isinstance(events[0], event_catalog.Log)

    def test_seeded_replay_through_jsonl_sink(self, tmp_path: Path) -> None:
        rng = random.Random(0)
        pool = _sample_events()
        chosen = [pool[rng.randrange(len(pool))] for _ in range(200)]
        sink = ev.JsonlSink(tmp_path / "events.jsonl")
        bus = ev.EventBus(sink, run_id="replay")
        for event in chosen:
            bus.emit(event)
        bus.close()
        back = ev.read_events(tmp_path / "events.jsonl")
        assert len(back) == len(chosen)
        assert [type(e) for e in back] == [type(e) for e in chosen]
        assert [e.seq for e in back] == list(range(1, len(chosen) + 1))


class TestEventBus:
    def test_add_sink_late(self, tmp_path: Path) -> None:
        bus = ev.EventBus()
        bus.emit(event_catalog.Log(text="before"))
        sink = ev.JsonlSink(tmp_path / "late.jsonl")
        bus.add_sink(sink)
        bus.emit(event_catalog.Log(text="after"))
        bus.close()
        back = ev.read_events(tmp_path / "late.jsonl")
        assert [e.to_dict()["data"]["text"] for e in back] == ["after"]


def _strip_ts(event_dict: dict[str, Any]) -> dict[str, Any]:
    out = dict(event_dict)
    out.pop("ts", None)
    return out


class TestV1CompatGoldenParity:
    """V1CompatSink(ProgressLog) output == direct ProgressLog calls."""

    def test_named_methods_parity(self, tmp_path: Path) -> None:
        direct_path = tmp_path / "direct.jsonl"
        compat_path = tmp_path / "compat.jsonl"
        direct = ProgressLog(direct_path, run_id="run-1")
        compat = ProgressLog(compat_path, run_id="run-1")
        bus = ev.EventBus(ev.V1CompatSink(compat), run_id="run-1")

        direct.factory_started("proj", 3)
        bus.emit(event_catalog.RunStarted(project="proj", components=3))

        direct.component_started("comp-a")
        bus.emit(event_catalog.ComponentStarted(component="comp-a"))

        direct.component_completed("comp-a", 12.339, 4)
        bus.emit(
            event_catalog.ComponentCompleted(
                component="comp-a", duration_seconds=12.339, iterations=4
            )
        )

        direct.component_failed("comp-b", "boom")
        bus.emit(event_catalog.ComponentFailed(component="comp-b", error="boom"))

        direct.circuit_breaker_tripped("comp-b", 5, "stall")
        bus.emit(
            event_catalog.CircuitBreakerTripped(component="comp-b", iterations=5, error="stall")
        )

        direct.component_retrying("comp-b", 2, "verify failed")
        bus.emit(
            event_catalog.ComponentRetrying(component="comp-b", attempt=2, reason="verify failed")
        )

        direct.verification_result("comp-a", True, ["tests"], [], 3.456)
        bus.emit(
            event_catalog.VerificationResultEvent(
                component="comp-a",
                passed=True,
                checks=("tests",),
                failures=(),
                duration_seconds=3.456,
            )
        )

        direct.review_result("comp-a", False, "hard", 2, 1, 60.0)
        bus.emit(
            event_catalog.ReviewResultEvent(
                component="comp-a",
                passed=False,
                mode="hard",
                fail_count=2,
                advisory_count=1,
                duration_seconds=60.0,
            )
        )

        # Mirrors UsageTotals.to_dict() verbatim, which is the documented
        # contract of ProgressLog.component_usage. R8 added token_calls
        # (calls that reported a TOKEN figure, as opposed to cost alone)
        # and the cost ceiling added cost_calls (its mirror), so both
        # sides of the parity carry both. Every reader of this payload
        # looks keys up defensively, so older files simply lack them.
        usage = {
            "calls": 3,
            "known_calls": 2,
            "token_calls": 1,
            "cost_calls": 2,
            "unreported_calls": 1,
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 10,
            "cache_creation_tokens": 5,
            "total_tokens": 165,
            "cost_usd": 0.123456,
            "duration_seconds": 42.0,
        }
        direct.component_usage("comp-a", "engineer", dict(usage))
        bus.emit(event_catalog.ComponentUsage(component="comp-a", phase="engineer", **usage))

        direct.budget_exceeded("comp-a", 100, 50)
        bus.emit(
            event_catalog.BudgetExceeded(component="comp-a", total_tokens=100, max_total_tokens=50)
        )

        coverage_kwargs: dict[str, Any] = {
            "ceiling": "max_cost_usd",
            "axis": "cost",
            "calls": 13,
            "covered_calls": 8,
            "uncovered_calls": 5,
            "uncovered_tokens": 193633,
            "detail": "cost coverage is PARTIAL",
        }
        direct.budget_coverage(uncovered_roles=["review"], **coverage_kwargs)
        bus.emit(
            event_catalog.BudgetCoverage(
                uncovered_roles=("review",),
                **coverage_kwargs,
            )
        )

        direct.contract_result(1, False, "comp-a", 9.876)
        bus.emit(
            event_catalog.ContractResult(
                tier=1, passed=False, breaker="comp-a", duration_seconds=9.876
            )
        )

        direct.factory_completed(2, 1, 0, 100.0)
        bus.emit(
            event_catalog.RunCompleted(completed=2, failed=1, skipped=0, duration_seconds=100.0)
        )

        direct.emit("merge_pending", "comp-a", {"pr_url": "http://pr/1", "error": "not confirmed"})
        bus.emit(
            event_catalog.MergePendingV1(
                component="comp-a", pr_url="http://pr/1", error="not confirmed"
            )
        )

        direct.emit("phase_skipped", "comp-a", {"phase": "security", "reason": "budget"})
        bus.emit(event_catalog.PhaseSkipped(component="comp-a", phase="security", reason="budget"))

        direct.emit("diff_fetch_failed", "comp-a", {"error": "git failed"})
        bus.emit(event_catalog.DiffFetchFailed(component="comp-a", error="git failed"))

        direct.emit(
            "adversarial_agent_selected",
            data={
                "phase": "review",
                "source": "config",
                "identity": "codex (gpt-5)",
                "agent_type": "codex",
                "model": "gpt-5",
                "homogeneous": True,
            },
        )
        bus.emit(
            event_catalog.AdversarialAgentSelected(
                phase="review",
                agent_source="config",
                identity="codex (gpt-5)",
                agent_type="codex",
                model="gpt-5",
                homogeneous=True,
            )
        )

        direct_lines = [_strip_ts(e) for e in read_progress_events(direct_path)]
        compat_lines = [_strip_ts(e) for e in read_progress_events(compat_path)]
        assert compat_lines == direct_lines
        assert len(direct_lines) == 17  # every v1-named event type exercised

    def test_v2_only_events_are_dropped(self, tmp_path: Path) -> None:
        compat_path = tmp_path / "compat.jsonl"
        bus = ev.EventBus(ev.V1CompatSink(ProgressLog(compat_path, run_id="r")), run_id="r")
        bus.emit(event_catalog.RunPlan(components=({"id": "a", "title": "A", "deps": []},)))
        bus.emit(event_catalog.PhaseStarted(component="a", phase="verify", attempt=1))
        bus.emit(event_catalog.WorkerHeartbeat(component="a", pid=1, elapsed_seconds=1.0))
        bus.emit(event_catalog.Log(text="narration"))
        bus.emit(event_catalog.PrMerged(component="a", pr_number=1, pr_url="u"))
        bus.emit(event_catalog.SpecIssueRecorded(severity="blocker", summary="s"))
        bus.emit(event_catalog.ArtifactWritten(label="manifest", path="m.json"))
        assert read_progress_events(compat_path) == []

    def test_progress_sinks_still_fed(self, tmp_path: Path) -> None:
        """R7.4 ProgressSink observers (e.g. Linear) attached to the
        wrapped ProgressLog receive events emitted through the bus."""
        seen: list[dict[str, Any]] = []

        class Recorder:
            def handle_event(self, event: dict[str, Any]) -> None:
                seen.append(event)

        log = ProgressLog(tmp_path / "p.jsonl", run_id="r")
        log.attach_sink(Recorder())
        bus = ev.EventBus(ev.V1CompatSink(log), run_id="r")
        bus.emit(event_catalog.ComponentStarted(component="comp-a"))
        bus.emit(event_catalog.Log(text="dropped for v1"))
        assert [e["event"] for e in seen] == ["component_started"]


class TestRunPaths:
    def test_layout(self, tmp_path: Path) -> None:
        rp = ev.RunPaths.for_run(tmp_path, "run-42")
        assert rp.events_file == tmp_path / ".kstrl" / "runs" / "run-42" / "events.jsonl"
        assert rp.engineer_events("c1").name == "engineer.jsonl"
        assert rp.engineer_log("c1").parent == rp.component_dir("c1")
        assert rp.phase_log("c1", "review").name == "review.log"


class TestJsonlSink:
    def test_append_and_reopen(self, tmp_path: Path) -> None:
        p = tmp_path / "s.jsonl"
        sink = ev.JsonlSink(p)
        sink.emit(ev.EventBus().emit(event_catalog.Log(text="one")))
        sink.close()
        sink2 = ev.JsonlSink(p)
        sink2.emit(ev.EventBus().emit(event_catalog.Log(text="two")))
        sink2.close()
        texts = [json.loads(line)["data"]["text"] for line in p.read_text().splitlines()]
        assert texts == ["one", "two"]


class TestBothSinksCarryTheSameBudgetHalt:
    """The durable sink and the progress log must record the same facts.

    Found by a real factory run, not by this suite: `JsonlSink` writes
    the event via `to_json_line`, so it picked up `condition`/`ceilings`
    automatically, while `V1CompatSink` delegates to a `ProgressLog`
    method with a HAND-WRITTEN field list that was never updated. The
    durable events.jsonl carried both fields; progress.jsonl silently
    dropped them.

    Nothing asserted the two agreed, which is why a whole-field
    divergence survived a green suite. These tests fix that, and are
    written to fail for ANY future field added to one sink and not the
    other - not just this pair.
    """

    @staticmethod
    def _emit_both(
        tmp_path: Path,
        event: event_catalog.BudgetExceeded,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from kstrl.observability import ProgressLog

        durable_path = tmp_path / "events.jsonl"
        progress_path = tmp_path / "progress.jsonl"
        ev.JsonlSink(durable_path).emit(event)
        ev.V1CompatSink(ProgressLog(progress_path)).emit(event)

        def payload(path: Path) -> dict[str, Any]:
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("event") == "budget_exceeded":
                    return dict(row["data"])
            raise AssertionError(f"no budget_exceeded line in {path}")

        return payload(durable_path), payload(progress_path)

    def test_a_breach_reaches_both_sinks_identically(
        self,
        tmp_path: Path,
    ) -> None:
        durable, progress = self._emit_both(
            tmp_path,
            event_catalog.BudgetExceeded(
                component="comp-a",
                total_tokens=4017316,
                max_total_tokens=0,
                cost_usd=5.123713,
                max_cost_usd=5.0,
                ceiling="max_cost_usd",
                condition="breached",
                ceilings=("max_cost_usd",),
            ),
        )
        assert durable == progress

    def test_an_unenforceable_halt_reaches_both_sinks_identically(
        self,
        tmp_path: Path,
    ) -> None:
        """The multi-ceiling case is where a single-value field loses
        the most, so it is the one most worth pinning."""
        durable, progress = self._emit_both(
            tmp_path,
            event_catalog.BudgetExceeded(
                component="comp-a",
                total_tokens=0,
                max_total_tokens=500,
                cost_usd=0.0,
                max_cost_usd=100.0,
                ceiling="max_total_tokens, max_cost_usd",
                condition="unenforceable",
                ceilings=("max_total_tokens", "max_cost_usd"),
            ),
        )
        assert durable == progress
        assert progress["ceilings"] == ["max_total_tokens", "max_cost_usd"]
        assert progress["condition"] == "unenforceable"

    def test_a_partially_covered_halt_reaches_both_sinks_identically(
        self,
        tmp_path: Path,
    ) -> None:
        """R8: the coverage a ceiling had when it halted is part of the
        halt record, on BOTH sinks. Payload is the measured run's."""
        durable, progress = self._emit_both(
            tmp_path,
            event_catalog.BudgetExceeded(
                component="comp-a",
                total_tokens=26522034,
                max_total_tokens=0,
                cost_usd=28.7545,
                max_cost_usd=25.0,
                ceiling="max_cost_usd",
                condition="breached",
                ceilings=("max_cost_usd",),
                coverage=(
                    {
                        "ceiling": "max_cost_usd",
                        "axis": "cost",
                        "calls": 13,
                        "covered_calls": 8,
                        "uncovered_calls": 5,
                        "uncovered_tokens": 193633,
                        "uncovered_roles": ["review"],
                        "roles": [],
                    },
                ),
            ),
        )
        assert durable == progress
        assert progress["coverage"][0]["uncovered_roles"] == ["review"]

    def test_every_coverage_field_survives_the_progress_log(
        self,
        tmp_path: Path,
    ) -> None:
        """The same generic guard for the new run-scoped event."""
        import dataclasses

        from kstrl.observability import ProgressLog

        event = event_catalog.BudgetCoverage(
            ceiling="max_cost_usd",
            axis="cost",
            calls=13,
            covered_calls=8,
            uncovered_calls=5,
            uncovered_tokens=193633,
            uncovered_roles=("review",),
            detail="cost coverage is PARTIAL",
        )
        durable_path = tmp_path / "events.jsonl"
        progress_path = tmp_path / "progress.jsonl"
        ev.JsonlSink(durable_path).emit(event)
        ev.V1CompatSink(ProgressLog(progress_path)).emit(event)

        def payload(path: Path) -> dict[str, Any]:
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row.get("event") == "budget_coverage":
                    return dict(row["data"])
            raise AssertionError(f"no budget_coverage line in {path}")

        durable, progress = payload(durable_path), payload(progress_path)
        assert durable == progress
        base = {f.name for f in dataclasses.fields(event_catalog.Event)}
        payload_fields = {f.name for f in dataclasses.fields(event)} - base - {"type"}
        missing = payload_fields - progress.keys()
        assert not missing, (
            f"BudgetCoverage fields missing from progress.jsonl: {missing}. "
            "Add them to ProgressLog.budget_coverage and the V1CompatSink "
            "bridge."
        )

    def test_every_event_field_survives_the_progress_log(
        self,
        tmp_path: Path,
    ) -> None:
        """Generic guard: any field added to BudgetExceeded later must
        appear in BOTH sinks. This is the assertion whose absence let a
        two-field divergence ship."""
        import dataclasses

        event = event_catalog.BudgetExceeded(
            component="comp-a",
            total_tokens=1,
            max_total_tokens=2,
            cost_usd=3.0,
            max_cost_usd=4.0,
            ceiling="max_cost_usd",
            condition="breached",
            ceilings=("max_cost_usd",),
            coverage=({"ceiling": "max_cost_usd", "covered_calls": 8},),
        )
        durable, progress = self._emit_both(tmp_path, event)

        base = {f.name for f in dataclasses.fields(event_catalog.Event)}
        payload_fields = {f.name for f in dataclasses.fields(event)} - base - {"type"}
        missing = payload_fields - progress.keys()
        assert not missing, (
            f"BudgetExceeded fields missing from progress.jsonl: {missing}. "
            "Add them to ProgressLog.budget_exceeded and the V1CompatSink "
            "bridge."
        )
        assert payload_fields <= durable.keys()
