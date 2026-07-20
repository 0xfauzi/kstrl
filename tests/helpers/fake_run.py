"""Synthetic run-directory fixture for tailer and Pilot tests (PR C).

The spike's generator (spike/tui0/fake_run.py) promoted onto the REAL
event classes, so fixtures exercise production serialization. Two
modes: ``write_fake_run`` writes a complete run at once (static
states); ``stream_fake_run`` yields after each write so tailer and
live-update tests can step the run forward between polls.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

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

    bus.emit(ev.RunStarted(project="fake-project", components=len(comps)))
    yield
    bus.emit(ev.RunPlan(
        components=tuple(
            {"id": cid, "title": f"Component {cid[-1].upper()}",
             "deps": [comps[i - 1]] if i else []}
            for i, cid in enumerate(comps)
        ),
        max_total_tokens=spec.max_total_tokens,
        max_adversarial_calls=spec.max_adversarial_calls,
    ))
    yield

    for index, cid in enumerate(comps):
        bus.emit(ev.ComponentStarted(component=cid))
        bus.emit(ev.PhaseStarted(component=cid, phase="engineer", attempt=1))
        yield
        worker = ev.EventBus(
            ev.JsonlSink(paths.engineer_events(cid)),
            run_id=run_id, source="worker", component=cid,
        )
        log_path = paths.engineer_log(cid)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            for iteration in range(1, spec.iterations + 1):
                worker.emit(ev.IterationStarted(
                    iteration=iteration, max_iterations=spec.iterations,
                ))
                log.write(f"[{cid}] editing src/{cid}/impl.py\n")
                log.write(f"[{cid}] running tests (iteration {iteration})\n")
                worker.emit(ev.IterationCompleted(
                    iteration=iteration, duration_seconds=12.5,
                    completed=iteration == spec.iterations,
                ))
                yield
            worker.emit(ev.WorkerHeartbeat(pid=40000 + index,
                                           elapsed_seconds=25.0))
        worker.close()
        bus.emit(ev.PhaseCompleted(
            component=cid, phase="engineer", passed=True,
            duration_seconds=25.0,
        ))
        bus.emit(ev.ComponentUsage(
            component=cid, phase="engineer", calls=spec.iterations,
            known_calls=(
                spec.iterations - 1 if spec.include_unreported_usage
                else spec.iterations
            ),
            unreported_calls=1 if spec.include_unreported_usage else 0,
            input_tokens=40_000 * (index + 1),
            output_tokens=8_000,
            total_tokens=48_000 * (index + 1),
            cost_usd=0.75 * (index + 1),
            duration_seconds=25.0,
        ))
        yield
        for phase in ("verify", "review", "security", "distill"):
            bus.emit(ev.PhaseStarted(component=cid, phase=phase, attempt=1))
            if phase == "review" and spec.include_findings:
                bus.emit(ev.FindingRecorded(
                    component=cid, phase="review", category="test_quality",
                    severity="advisory",
                    location=f"src/{cid}/impl.py:42",
                    explanation="Assertion could be tighter.", attempt=1,
                ))
            bus.emit(ev.PhaseCompleted(
                component=cid, phase=phase, passed=True,
                duration_seconds=8.0,
            ))
            yield
        if spec.include_checkpoint and index == len(comps) - 1:
            bus.emit(ev.CheckpointRequested(
                component=cid, kind="pr_merge",
                question=f"Approve PR creation and merge for {cid}?",
            ))
            yield
            # Left OPEN deliberately: dash renders the pending banner.
        else:
            bus.emit(ev.PrCreated(component=cid, pr_number=100 + index,
                                  pr_url=f"https://example.test/pr/{100 + index}"))
            bus.emit(ev.PrMerged(component=cid, pr_number=100 + index,
                                 pr_url=f"https://example.test/pr/{100 + index}"))
            bus.emit(ev.ComponentCompleted(
                component=cid, duration_seconds=60.0 + index,
                iterations=spec.iterations,
            ))
            yield

    if spec.complete and not spec.include_checkpoint:
        bus.emit(ev.RunCompleted(
            completed=len(comps), failed=0, skipped=0,
            duration_seconds=200.0,
        ))
        yield
    bus.close()


def write_fake_run(
    root: Path, spec: FakeRunSpec | None = None, *,
    run_id: str = DEFAULT_RUN_ID,
) -> Path:
    """Write the whole fake run at once; returns the run dir."""
    spec = spec or FakeRunSpec()
    for _ in _emit_run(root, spec, run_id):
        pass
    return root / ".ralph" / "runs" / run_id


def stream_fake_run(
    root: Path, spec: FakeRunSpec | None = None, *,
    run_id: str = DEFAULT_RUN_ID,
) -> Iterator[None]:
    """Step the fake run forward one write-batch per iteration."""
    return _emit_run(root, spec or FakeRunSpec(), run_id)
