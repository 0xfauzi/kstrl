"""The map-driving widgets of docs/lessons/pr-221.html: the walk and the loops.

Two widgets light nodes and edges on the generated system map by component
id. Neither computes a rule; each carries a table. A table that names a
component the atlas does not draw, or an edge the model does not declare,
would light nothing and teach a topology the atlas does not claim. So both
tables are checked here against ``scripts/atlas/logical_model.py``, the
same model the figure is drawn from.

The walk is the path one spec takes, in the order the brief names it. The
loops are the six from section 2 of docs/control-loop-design.md plus the
unbuilt seventh, each mapped to the components that implement its actuator,
sensor and set point. ``--json`` emits both tables so the page's copy can be
compared byte for byte.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts" / "atlas"))

from logical_model import COMPONENTS, FLOWS, REGIONS  # noqa: E402

# Each step: title, nodes to light, the component that MEASURES the step
# (empty when nothing does), the edges to light as (from, to) pairs.
WALK: list[dict[str, object]] = [
    {"title": "a labelled issue arrives",
     "nodes": ["GitHubIssues", "GitHubIntake", "WorkQueue"], "measures": ["GitHubIntake"],
     "edges": [["GitHubIssues", "GitHubIntake"], ["GitHubIntake", "WorkQueue"]]},
    {"title": "the daemon runs the admission gates",
     "nodes": ["ServeDaemon", "SpendLedger", "Inbox", "FlowControl", "WorkQueue"],
     "measures": ["SpendLedger", "Inbox", "FlowControl"],
     "edges": [["WorkQueue", "ServeDaemon"], ["SpendLedger", "ServeDaemon"], ["Inbox", "ServeDaemon"]]},
    {"title": "the architect red-teams and decomposes the spec",
     "nodes": ["ServeDaemon", "Scheduler", "Architect", "Manifest", "PRD", "Operator"],
     "measures": ["Architect"],
     "edges": [["ServeDaemon", "Scheduler"], ["Operator", "Architect"], ["Architect", "Manifest"], ["Architect", "PRD"]]},
    {"title": "a worktree is cut per component",
     "nodes": ["Scheduler", "Manifest", "Worktrees"], "measures": [],
     "edges": [["Manifest", "Scheduler"], ["Scheduler", "Worktrees"]]},
    {"title": "the engineer builds one story",
     "nodes": ["EngineerLoop", "Feedforward", "KnowledgeInjector", "RetryContext", "OperatorContext",
               "AgentAdapter", "CodingAgent", "PRD", "Breaker", "PathGuard"],
     "measures": ["Breaker", "PathGuard"],
     "edges": [["Feedforward", "EngineerLoop"], ["KnowledgeInjector", "EngineerLoop"],
               ["RetryContext", "EngineerLoop"], ["EngineerLoop", "AgentAdapter"],
               ["AgentAdapter", "CodingAgent"], ["CodingAgent", "AgentAdapter"],
               ["PRD", "EngineerLoop"], ["EngineerLoop", "PRD"],
               ["Breaker", "EngineerLoop"], ["PathGuard", "EngineerLoop"]]},
    {"title": "the sensors measure the tree",
     "nodes": ["Pipeline", "MechanicalVerifier", "PolicyEnvelope", "AdequacyGate", "FixturesOracle",
               "Reviewer", "SecurityReviewer", "Findings", "Sense"],
     "measures": ["MechanicalVerifier", "Reviewer", "SecurityReviewer", "FixturesOracle"],
     "edges": [["EngineerLoop", "Pipeline"], ["Pipeline", "MechanicalVerifier"], ["Pipeline", "Reviewer"],
               ["Pipeline", "SecurityReviewer"], ["PolicyEnvelope", "MechanicalVerifier"],
               ["AdequacyGate", "MechanicalVerifier"], ["FixturesOracle", "MechanicalVerifier"],
               ["MechanicalVerifier", "Findings"], ["Reviewer", "Findings"], ["SecurityReviewer", "Findings"]]},
    {"title": "the pipeline decides: retry, fail, or go on",
     "nodes": ["Findings", "Pipeline", "RetryContext", "AutonomyLadder"], "measures": ["Findings"],
     "edges": [["Findings", "Pipeline"], ["Pipeline", "RetryContext"], ["AutonomyLadder", "Pipeline"]]},
    {"title": "facts are distilled and the pull request merges",
     "nodes": ["Pipeline", "Distiller", "PullRequests", "GitHubPRs", "Dampener", "Inbox"],
     "measures": ["Dampener"],
     "edges": [["Pipeline", "Distiller"], ["Pipeline", "PullRequests"], ["PullRequests", "GitHubPRs"],
               ["Pipeline", "Inbox"]]},
    {"title": "contract tests run on the merged tier",
     "nodes": ["PullRequests", "ContractTester", "Scheduler"], "measures": ["ContractTester"],
     "edges": [["PullRequests", "ContractTester"], ["ContractTester", "Scheduler"]]},
    {"title": "release (planned, R8.7)",
     "nodes": ["ReleaseStage"], "measures": [], "edges": []},
    {"title": "runtime signals (planned, R8.8)",
     "nodes": ["RuntimeSignals"], "measures": ["RuntimeSignals"], "edges": []},
    {"title": "the ladder records the outcome",
     "nodes": ["Scheduler", "AutonomyLadder", "PolicyEnvelope", "Calibration", "HealthTrending", "StateDir"],
     "measures": ["PolicyEnvelope", "Calibration", "HealthTrending"],
     "edges": [["Scheduler", "AutonomyLadder"], ["PolicyEnvelope", "AutonomyLadder"],
               ["Calibration", "AutonomyLadder"], ["AutonomyLadder", "StateDir"]]},
    {"title": "the journal keeps the record",
     "nodes": ["Scheduler", "EvolutionJournal", "Proposals", "Replay", "Playbook"], "measures": ["Replay"],
     "edges": [["Scheduler", "EvolutionJournal"], ["EvolutionJournal", "Proposals"],
               ["EvolutionJournal", "Replay"]]},
]

# The loop nest, docs/control-loop-design.md section 2, mapped onto the map.
LOOPS: list[dict[str, object]] = [
    {"id": "implement", "n": 1, "rate": "seconds to minutes", "closed": "open",
     "actuator": "EngineerLoop", "sensor": "none: the prompt is re-sent unchanged each iteration",
     "setpoint": "this story's criteria",
     "nodes": ["EngineerLoop", "AgentAdapter", "CodingAgent", "PRD", "Breaker", "PathGuard"],
     "regions": ["build"]},
    {"id": "accept", "n": 2, "rate": "minutes", "closed": "closed, but winds up",
     "actuator": "Pipeline (retry with context)",
     "sensor": "MechanicalVerifier, Reviewer, SecurityReviewer", "setpoint": "the component PRD",
     "nodes": ["Pipeline", "RetryContext", "MechanicalVerifier", "Reviewer", "SecurityReviewer",
               "Findings", "PolicyEnvelope", "AdequacyGate", "FixturesOracle", "Sense", "Dampener",
               "Feedforward"],
     "regions": ["build", "measure", "decide"]},
    {"id": "integrate", "n": 3, "rate": "tens of minutes", "closed": "closed",
     "actuator": "Scheduler (schedule, merge, reset breaker)", "sensor": "ContractTester",
     "setpoint": "the manifest DAG satisfied",
     "nodes": ["Scheduler", "Manifest", "Worktrees", "PullRequests", "ContractTester", "Distiller",
               "Architect"],
     "regions": ["decide", "ship"]},
    {"id": "intake", "n": 4, "rate": "hours", "closed": "closed",
     "actuator": "ServeDaemon (queue admission)", "sensor": "SpendLedger, Inbox, FlowControl",
     "setpoint": "the queue drained",
     "nodes": ["ServeDaemon", "GitHubIntake", "WorkQueue", "SpendLedger", "Inbox", "FlowControl", "Steering"],
     "regions": ["intake"]},
    {"id": "trust", "n": 5, "rate": "days", "closed": "closed, two inputs unwired",
     "actuator": "AutonomyLadder (level change)",
     "sensor": "run outcomes, PolicyEnvelope; Calibration and HealthTrending after R10.11",
     "setpoint": "the evidence supports the level",
     "nodes": ["AutonomyLadder", "Replay", "StateDir", "SafeMode", "HealthTrending", "Calibration"],
     "regions": ["trust"]},
    {"id": "learn", "n": 6, "rate": "weeks", "closed": "open",
     "actuator": "playbook and prompt edits", "sensor": "attribution, Calibration",
     "setpoint": "the harness's own detection rates",
     "nodes": ["EvolutionJournal", "Proposals", "Playbook", "Distiller", "KnowledgeInjector",
               "OperatorContext"],
     "regions": ["learn"]},
    {"id": "operate", "n": 7, "rate": "not built", "closed": "does not exist",
     "actuator": "ReleaseStage (R8.7)", "sensor": "RuntimeSignals (R8.8)", "setpoint": "the service's SLO",
     "nodes": ["ReleaseStage", "RuntimeSignals"], "regions": ["ship", "learn"]},
    {"id": "observe", "n": 0, "rate": "every event", "closed": "not a loop",
     "actuator": "none: sinks are observability, never control flow (invariant 14)",
     "sensor": "none: it is the view every loop is read through", "setpoint": "none",
     "nodes": ["EventBus", "Reducer", "Dashboard", "CLI", "ProgressLog", "LinearMirror"],
     "regions": ["observe"]},
]

# A proposed change and the loop it belongs to.
CHANGES: list[dict[str, str]] = [
    {"change": "make the reviewer confirm passes=true (R10.3)", "loop": "accept"},
    {"change": "render only current failures on retry (R10.2)", "loop": "accept"},
    {"change": "halt hard mode when the review budget runs out (R10.5)", "loop": "accept"},
    {"change": "bound open pull requests to one (R10.7)", "loop": "intake"},
    {"change": "poll /memory and /iterate comments (R10.10)", "loop": "intake"},
    {"change": "name safe mode (R10.4)", "loop": "trust"},
    {"change": "wire the calibration-regression demotion (R10.11)", "loop": "trust"},
    {"change": "a sensor between engineer iterations (R10.12)", "loop": "implement"},
    {"change": "golden patterns and the memory file (R10.8, R10.9)", "loop": "learn"},
    {"change": "a global playbook with attribution (R9)", "loop": "learn"},
    {"change": "contract tests attribute a failure to the last merge", "loop": "integrate"},
    {"change": "deploy and roll back a release (R8.7)", "loop": "operate"},
    {"change": "a safe-mode chip in the dashboard masthead", "loop": "observe"},
]


def problems() -> list[str]:
    ids = {c["id"] for c in COMPONENTS}
    flows = {(a, b) for a, b, _art, _k in FLOWS}
    regions = {r["id"] for r in REGIONS}
    out: list[str] = []
    for i, step in enumerate(WALK, 1):
        for n in list(step["nodes"]) + list(step["measures"]):  # type: ignore[arg-type]
            if n not in ids:
                out.append(f"walk step {i}: unknown component {n}")
        for a, b in step["edges"]:  # type: ignore[union-attr]
            if (a, b) not in flows:
                out.append(f"walk step {i}: no flow {a} -> {b} in the model")
    for loop in LOOPS:
        for n in loop["nodes"]:  # type: ignore[union-attr]
            if n not in ids:
                out.append(f"loop {loop['id']}: unknown component {n}")
        for r in loop["regions"]:  # type: ignore[union-attr]
            if r not in regions:
                out.append(f"loop {loop['id']}: unknown region {r}")
    loop_ids = {loop["id"] for loop in LOOPS}
    for ch in CHANGES:
        if ch["loop"] not in loop_ids:
            out.append(f"change {ch['change']!r} names unknown loop {ch['loop']}")
    return out


def main() -> None:
    if "--json" in sys.argv:
        json.dump({"walk": WALK, "loops": LOOPS, "changes": CHANGES}, sys.stdout)
        return
    p = problems()
    ids = {c["id"] for c in COMPONENTS}
    walked = {n for s in WALK for n in s["nodes"]}  # type: ignore[union-attr]
    looped = {n for lo in LOOPS for n in lo["nodes"]}  # type: ignore[union-attr]
    print(f"walk: {len(WALK)} steps, {len(walked)} distinct components, "
          f"{sum(len(s['edges']) for s in WALK)} edges")  # type: ignore[arg-type]
    print(f"loops: {len(LOOPS)}, covering {len(looped)} of {len(ids)} components")
    print("components in no loop:", sorted(ids - looped))
    print("claim: every id and edge the two widgets light exists in logical_model ->",
          "holds" if not p else "fails")
    for line in p:
        print("  ", line)
    print("claim: every proposed change maps to exactly one loop ->",
          "holds" if all(sum(1 for lo in LOOPS if lo["id"] == ch["loop"]) == 1 for ch in CHANGES)
          else "fails")


if __name__ == "__main__":
    main()
