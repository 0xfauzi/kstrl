"""Synthetic Ralph run generator for the TUI spike (Stage 0).

Writes a plausible schema-v2 run directory:

    <out>/.ralph/runs/<run_id>/events.jsonl
    <out>/.ralph/runs/<run_id>/components/<comp_id>/engineer.jsonl
    <out>/.ralph/runs/<run_id>/components/<comp_id>/engineer.log

Every record carries ``t_emit`` (time.time() at write) so a tailer can
measure end-to-end latency. ``--rate`` scales event frequency (1.0 =
realistic, 10.0 = storm). ``--torn-tail-every N`` writes a partial JSON
line (no newline), flushes, sleeps 200ms, then completes it - the tailer
must never crash or drop records on this.

This is a spike artifact: stdlib only, no ralph_py imports.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

PHASES = ["engineer", "verify", "review", "security", "distill", "pr"]
SEVERITIES = ["critical", "high", "medium", "low", "advisory"]
CATEGORIES = ["scope_creep", "security_concern", "test_quality", "missing_error_handling"]

TRANSCRIPT_LINES = [
    "Reading PRD stories from scripts/ralph/feature/{c}/prd.json",
    "Running: uv run pytest tests/ -x -q",
    "  12 passed in 3.41s",
    "Editing src/{c}/handler.py: add retry wrapper around fetch()",
    "TOOL git diff --stat",
    " src/{c}/handler.py | 34 ++++++++++++++----",
    "## Self-Critique",
    "- Assumed the retry cap of 3 is acceptable; PRD does not state one.",
    "Iteration complete; 2 stories remain.",
]


class RunWriter:
    def __init__(self, out: Path, run_id: str, torn_every: int, seed: int) -> None:
        self.run_dir = out / ".ralph" / "runs" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.events_path = self.run_dir / "events.jsonl"
        self.events_fh = open(self.events_path, "a", buffering=1)
        self.comp_fhs: dict[str, object] = {}
        self.log_fhs: dict[str, object] = {}
        self.torn_every = torn_every
        self.lines_written = 0
        self.seq = 0
        self.rng = random.Random(seed)

    def component_files(self, comp: str):
        if comp not in self.comp_fhs:
            d = self.run_dir / "components" / comp
            d.mkdir(parents=True, exist_ok=True)
            self.comp_fhs[comp] = open(d / "engineer.jsonl", "a", buffering=1)
            self.log_fhs[comp] = open(d / "engineer.log", "a", buffering=1)
        return self.comp_fhs[comp], self.log_fhs[comp]

    def emit(self, event: str, component: str = "", source: str = "orchestrator",
             **data) -> None:
        self.seq += 1
        rec = {
            "schema": 2, "event": event, "ts": time.time(), "t_emit": time.time(),
            "run_id": self.run_id, "component": component, "source": source,
            "seq": self.seq, "data": data,
        }
        line = json.dumps(rec, separators=(",", ":"))
        if source == "worker" and component:
            fh, _ = self.component_files(component)
        else:
            fh = self.events_fh
        self.lines_written += 1
        if self.torn_every and self.lines_written % self.torn_every == 0:
            cut = max(1, len(line) // 2)
            fh.write(line[:cut])
            fh.flush()
            time.sleep(0.2)
            fh.write(line[cut:] + "\n")
        else:
            fh.write(line + "\n")

    def transcript(self, comp: str, n_lines: int) -> None:
        _, log_fh = self.component_files(comp)
        for _ in range(n_lines):
            tpl = self.rng.choice(TRANSCRIPT_LINES)
            log_fh.write(tpl.format(c=comp) + "\n")


def generate(out: Path, components: int, rate: float, duration: float, seed: int,
             torn_every: int, pause_after: float, checkpoint: bool,
             churn: bool = False) -> None:
    rng = random.Random(seed)
    run_id = f"factory-{time.strftime('%Y%m%d-%H%M%S')}.000000-spike"
    w = RunWriter(out, run_id, torn_every, seed)
    comps = [f"comp-{chr(ord('a') + i)}" for i in range(components)]

    w.emit("factory_started", project="spike-project", components=len(comps))
    w.emit("run_plan", components=[
        {"id": c, "title": f"Component {c.split('-')[1].upper()}",
         "deps": [comps[i - 1]] if i else []}
        for i, c in enumerate(comps)
    ], max_total_tokens=5_000_000, max_adversarial_calls=40)

    start = time.monotonic()
    comp_state = {c: {"phase_i": 0, "iteration": 0, "started": False} for c in comps}
    last_heartbeat: dict[str, float] = {}
    finished: set[str] = set()
    tick = 0.5 / rate

    while time.monotonic() - start < duration:
        now = time.monotonic()
        if pause_after and now - start > pause_after:
            time.sleep(duration - (now - start))
            break
        for c in comps:
            st = comp_state[c]
            if c in finished:
                continue
            if not st["started"]:
                if rng.random() < 0.3:
                    st["started"] = True
                    w.emit("component_started", component=c)
                    w.emit("phase_started", component=c, phase="engineer", attempt=1)
                continue
            # heartbeat every ~15s/rate
            if now - last_heartbeat.get(c, 0) > 15.0 / rate:
                last_heartbeat[c] = now
                w.emit("worker_heartbeat", component=c, source="worker",
                       pid=10000 + hash(c) % 1000, elapsed_seconds=round(now - start, 1))
            r = rng.random()
            if r < 0.35:
                st["iteration"] += 1
                w.emit("iteration_started", component=c, source="worker",
                       iteration=st["iteration"], max_iterations=10)
                w.transcript(c, rng.randint(2, 5))
                w.emit("iteration_completed", component=c, source="worker",
                       iteration=st["iteration"],
                       duration_seconds=round(rng.uniform(5, 40), 1),
                       completed=False, timed_out=False)
            elif r < 0.55:
                w.emit("component_usage", component=c, phase=rng.choice(PHASES[:3]),
                       calls=1, known_calls=rng.choice([0, 1]), unreported_calls=0,
                       input_tokens=rng.randint(1000, 90000),
                       output_tokens=rng.randint(200, 9000),
                       total_tokens=rng.randint(1200, 99000),
                       cost_usd=round(rng.uniform(0.01, 0.9), 4),
                       duration_seconds=round(rng.uniform(2, 60), 2))
            elif r < 0.65:
                w.emit("finding_recorded", component=c, phase="review",
                       category=rng.choice(CATEGORIES),
                       severity=rng.choice(SEVERITIES),
                       location=f"src/{c}/handler.py:{rng.randint(10, 300)}",
                       explanation="Retry loop can spin without backoff cap.",
                       attempt=1)
            elif r < 0.72:
                w.emit("log", component=c, severity=rng.choice(["info", "warn"]),
                       kind="line", text=f"Phase note for {c} at t={round(now - start, 1)}")
            elif r < 0.82:
                phase = PHASES[st["phase_i"]]
                w.emit("phase_completed", component=c, phase=phase,
                       passed=True, detail="", duration_seconds=round(rng.uniform(5, 120), 1))
                st["phase_i"] += 1
                if st["phase_i"] >= len(PHASES):
                    if churn:
                        # Storm mode: never finish - cycle back through the
                        # phase chain as a new attempt so event flow stays
                        # at the configured rate for the whole duration.
                        st["phase_i"] = 0
                        w.emit("component_retrying", component=c,
                               attempt=st["iteration"], reason="spike churn")
                        w.emit("phase_started", component=c, phase=PHASES[0], attempt=2)
                        continue
                    w.emit("component_completed", component=c,
                           duration_seconds=round(now - start, 1),
                           iterations=st["iteration"])
                    finished.add(c)
                else:
                    nxt = PHASES[st["phase_i"]]
                    if checkpoint and nxt == "pr" and rng.random() < 0.5:
                        w.emit("checkpoint_requested", component=c, kind="checkpoint",
                               question=f"Approve PR creation and merge for {c}?")
                    w.emit("phase_started", component=c, phase=nxt, attempt=1)
        time.sleep(tick)

    for c in comps:
        if c not in finished and comp_state[c]["started"]:
            w.emit("component_failed", component=c, error="spike ended before completion")
    w.emit("factory_completed", completed=len(finished),
           failed=len(comps) - len(finished), skipped=0,
           duration_seconds=round(time.monotonic() - start, 1))
    print(f"run_id={run_id} events={w.lines_written} dir={w.run_dir}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--components", type=int, default=4)
    p.add_argument("--rate", type=float, default=1.0)
    p.add_argument("--duration", type=float, default=120.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--torn-tail-every", type=int, default=50)
    p.add_argument("--pause-after", type=float, default=0.0)
    p.add_argument("--checkpoint", action="store_true")
    p.add_argument("--churn", action="store_true",
                   help="components never complete; sustained event flow")
    a = p.parse_args()
    generate(a.out, a.components, a.rate, a.duration, a.seed,
             a.torn_tail_every, a.pause_after, a.checkpoint, a.churn)


if __name__ == "__main__":
    main()
