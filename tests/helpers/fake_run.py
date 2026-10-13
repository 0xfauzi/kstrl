"""Synthetic run-directory fixture.

Built on the REAL event classes, so fixtures exercise production
serialization. Two modes: ``write_fake_run`` writes a complete run at once (static
states).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from kstrl import event_catalog
from kstrl import events as ev

DEFAULT_RUN_ID = "factory-20260720-120000.000000-fake"


@dataclass(frozen=True)
class FakeRunSpec:
    components: int = 3
    iterations: int = 2
    include_checkpoint: bool = False
    include_unreported_usage: bool = True
    include_findings: bool = True
    complete: bool = True
    max_total_tokens: int = 5_000_000
    max_adversarial_calls: int = 40


def _component_ids(spec: FakeRunSpec) -> list[str]:
    return [f"comp-{chr(ord('a') + i)}" for i in range(spec.components)]


def _emit_run(root: Path, spec: FakeRunSpec, run_id: str) -> Iterator[None]:
    paths = ev.RunPaths.for_run(root, run_id)
    bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=run_id)
    comps = _component_ids(spec)

    bus.emit(event_catalog.RunStarted(project="fake-project", components=len(comps)))
    yield
    bus.emit(
        event_catalog.RunPlan(
            components=tuple(
                {
                    "id": cid,
                    "title": f"Component {cid[-1].upper()}",
                    "deps": [comps[i - 1]] if i else [],
                }
                for i, cid in enumerate(comps)
            ),
            max_total_tokens=spec.max_total_tokens,
            max_adversarial_calls=spec.max_adversarial_calls,
        )
    )
    yield

    for index, cid in enumerate(comps):
        bus.emit(event_catalog.ComponentStarted(component=cid))
        bus.emit(event_catalog.PhaseStarted(component=cid, phase="engineer", attempt=1))
        yield
        worker = ev.EventBus(
            ev.JsonlSink(paths.engineer_events(cid)),
            run_id=run_id,
            source="worker",
            component=cid,
        )
        log_path = paths.engineer_log(cid)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            for iteration in range(1, spec.iterations + 1):
                worker.emit(
                    event_catalog.IterationStarted(
                        iteration=iteration,
                        max_iterations=spec.iterations,
                    )
                )
                log.write(f"[{cid}] editing src/{cid}/impl.py\n")
                log.write(f"[{cid}] running tests (iteration {iteration})\n")
                worker.emit(
                    event_catalog.IterationCompleted(
                        iteration=iteration,
                        duration_seconds=12.5,
                        completed=iteration == spec.iterations,
                    )
                )
                yield
            worker.emit(event_catalog.WorkerHeartbeat(pid=40000 + index, elapsed_seconds=25.0))
        worker.close()
        bus.emit(
            event_catalog.PhaseCompleted(
                component=cid,
                phase="engineer",
                passed=True,
                duration_seconds=25.0,
            )
        )
        # A claude-style engineer: every call that reported anything
        # reported BOTH figures, so token_calls == cost_calls ==
        # known_calls. Emitted explicitly because a payload with
        # known_calls > 0 and both axis counters at 0 is the signature
        # of a pre-R8 log, and the reducer reads it as "coverage not
        # measured" - which is not what this fixture means.
        known = spec.iterations - 1 if spec.include_unreported_usage else spec.iterations
        bus.emit(
            event_catalog.ComponentUsage(
                component=cid,
                phase="engineer",
                calls=spec.iterations,
                known_calls=known,
                token_calls=known,
                cost_calls=known,
                unreported_calls=1 if spec.include_unreported_usage else 0,
                input_tokens=40_000 * (index + 1),
                output_tokens=8_000,
                total_tokens=48_000 * (index + 1),
                cost_usd=0.75 * (index + 1),
                duration_seconds=25.0,
            )
        )
        yield
        for phase in ("verify", "review", "security", "distill"):
            bus.emit(event_catalog.PhaseStarted(component=cid, phase=phase, attempt=1))
            if phase == "review" and spec.include_findings:
                bus.emit(
                    event_catalog.FindingRecorded(
                        component=cid,
                        phase="review",
                        category="test_quality",
                        severity="advisory",
                        location=f"src/{cid}/impl.py:42",
                        explanation="Assertion could be tighter.",
                        attempt=1,
                    )
                )
            bus.emit(
                event_catalog.PhaseCompleted(
                    component=cid,
                    phase=phase,
                    passed=True,
                    duration_seconds=8.0,
                )
            )
            yield
        if spec.include_checkpoint and index == len(comps) - 1:
            bus.emit(
                event_catalog.CheckpointRequested(
                    component=cid,
                    kind="pr_merge",
                    question=f"Approve PR creation and merge for {cid}?",
                )
            )
            yield
            # Left OPEN deliberately: the checkpoint stays pending.
        else:
            bus.emit(
                event_catalog.PrCreated(
                    component=cid,
                    pr_number=100 + index,
                    pr_url=f"https://example.test/pr/{100 + index}",
                )
            )
            bus.emit(
                event_catalog.PrMerged(
                    component=cid,
                    pr_number=100 + index,
                    pr_url=f"https://example.test/pr/{100 + index}",
                )
            )
            bus.emit(
                event_catalog.ComponentCompleted(
                    component=cid,
                    duration_seconds=60.0 + index,
                    iterations=spec.iterations,
                )
            )
            yield

    if spec.complete and not spec.include_checkpoint:
        bus.emit(
            event_catalog.RunCompleted(
                completed=len(comps),
                failed=0,
                skipped=0,
                duration_seconds=200.0,
            )
        )
        yield
    bus.close()


def write_fake_run(
    root: Path,
    spec: FakeRunSpec | None = None,
    *,
    run_id: str = DEFAULT_RUN_ID,
) -> Path:
    """Write the whole fake run at once; returns the run dir."""
    spec = spec or FakeRunSpec()
    for _ in _emit_run(root, spec, run_id):
        pass
    return root / ".kstrl" / "runs" / run_id
