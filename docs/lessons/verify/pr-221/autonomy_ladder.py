"""Widget 5 of docs/lessons/pr-221.html: how the factory earns and loses autonomy.

The rule is kstrl/autonomy.py on the merge commit: ``AutonomyState.promote``
(needs an actor, an ack, and no blockers), ``demote`` (one level, cool-down
of DEMOTION_COOLDOWN_RUNS, counters reset, no-op at L1), the counters,
``promotion_blockers``, ``flag_bundle_for``, ``resolve_runtime_level`` (the
lowest of max_level, the envelope ceiling, and the control-state gate wins)
and ``manual_override_notes`` (a config flag that contradicts the bundle is
recorded, never honoured).

Issue #232 (R10.11) adds two triggers that fire from outside a run: a
calibration regression always opens an inbox item and demotes only when
``demote_on_calibration_regression`` is true; a health breach likewise, once
kstrl/health.py exists (R8.4 #151).

Every threshold constant is copied from autonomy.py:107-124 and is, in that
file's own words, an unmeasured placeholder. The widget teaches the shape of
the ladder, not the numbers.

Runs a fixed list of action sequences and a property sweep. ``--json`` emits
the sequences' traces so the page's inline script can be compared.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from itertools import product

L2_MERGED_COMPONENTS_REQUIRED = 5
L3_CLEAN_MERGES_REQUIRED = 15
L4_MERGED_COMPONENTS_REQUIRED = 30
DEMOTION_COOLDOWN_RUNS = 10
MIN_DECISIVE_RUNS = 8

LABELS = {1: "L1 Supervised", 2: "L2 Gated-merge", 3: "L3 Enveloped auto-merge", 4: "L4 Deploy"}


def flag_bundle(level: int) -> dict[str, object]:
    return {
        "pause_before_pr_merge": level <= 2,
        "review_mode": "hard",
        "auto_accept_plan": level >= 2,
        "deps_allow_new_permitted": level >= 3,
        "auto_merge_when_green": level >= 3,
        "deploy_permitted": level >= 4,
    }


@dataclass
class State:
    level: int = 1
    components_merged: int = 0
    clean_merges: int = 0
    policy_violations: int = 0
    decisive_runs: int = 0
    cooldown: int = 0
    history: list[str] = field(default_factory=list)

    def reset_counters(self) -> None:
        self.components_merged = 0
        self.clean_merges = 0
        self.policy_violations = 0
        self.decisive_runs = 0

    def blockers(self) -> list[str]:
        if self.level >= 4:
            return ["already at L4 Deploy"]
        target = self.level + 1
        out: list[str] = []
        if self.cooldown > 0:
            out.append(f"demotion cool-down active: {self.cooldown} more decisive run(s) required")
        if self.decisive_runs < MIN_DECISIVE_RUNS:
            out.append(
                f"insufficient evidence: {self.decisive_runs} decisive run(s), "
                f"need {MIN_DECISIVE_RUNS}"
            )
        if self.policy_violations:
            out.append(f"{self.policy_violations} policy violation(s) at this level; need zero")
        if target == 2 and self.components_merged < L2_MERGED_COMPONENTS_REQUIRED:
            out.append(
                f"{self.components_merged}/{L2_MERGED_COMPONENTS_REQUIRED} components merged at L1"
            )
        elif target == 3 and self.clean_merges < L3_CLEAN_MERGES_REQUIRED:
            out.append(
                f"{self.clean_merges}/{L3_CLEAN_MERGES_REQUIRED} consecutive merges "
                "approved without edits"
            )
        elif target == 4 and self.components_merged < L4_MERGED_COMPONENTS_REQUIRED:
            out.append(
                f"{self.components_merged}/{L4_MERGED_COMPONENTS_REQUIRED} components "
                "merged while holding L3"
            )
        return out

    def promote(self, ack: bool) -> str:
        if not ack:
            return (
                "refused: promotion requires an explicit acknowledgement (and a named human actor)"
            )
        if self.level >= 4:
            return "refused: already at the highest level (L4 Deploy)"
        blockers = self.blockers()
        if blockers:
            return "refused: " + "; ".join(blockers)
        self.level += 1
        self.reset_counters()
        self.history.append(f"promote -> {LABELS[self.level]}")
        return f"promoted to {LABELS[self.level]}; counters reset"

    def demote(self, trigger: str) -> str:
        if self.level == 1:
            return (
                f"{trigger}: already at L1, the floor; nothing to revoke (violation still counted)"
            )
        self.level -= 1
        self.reset_counters()
        self.cooldown = DEMOTION_COOLDOWN_RUNS
        self.history.append(f"demote ({trigger}) -> {LABELS[self.level]}")
        return (
            f"{trigger}: demoted to {LABELS[self.level]}; counters reset; "
            f"cool-down {DEMOTION_COOLDOWN_RUNS} decisive runs"
        )

    def act(self, action: str, *, demote_on_calibration: bool = False) -> str:
        if action == "run":
            self.decisive_runs += 1
            if self.cooldown > 0:
                self.cooldown -= 1
            return "decisive run recorded" + (
                f"; cool-down now {self.cooldown}" if self.cooldown else ""
            )
        if action == "merge":
            self.components_merged += 1
            self.clean_merges += 1
            return "merged component recorded, clean"
        if action == "merge_edited":
            self.components_merged += 1
            self.clean_merges = 0
            return "merged component recorded with human edits; clean streak reset to 0"
        if action == "violation":
            self.policy_violations += 1
            return self.demote("policy violation")
        if action == "calibration":
            if demote_on_calibration:
                return "calibration regression: inbox item opened; " + self.demote(
                    "calibration regression"
                )
            return (
                "calibration regression: inbox item opened (calibration_drift); "
                "level unchanged (advisory)"
            )
        if action == "promote":
            return self.promote(ack=True)
        if action == "promote_noack":
            return self.promote(ack=False)
        raise ValueError(action)


def runtime_level(
    level: int, max_level: int, policy_enabled: bool, control_external: bool
) -> tuple[int, list[str]]:
    notes: list[str] = []
    out = level
    if out > max_level:
        out = max_level
        notes.append(f"clamped to L{max_level} by [autonomy] max_level")
    ceiling = 4 if policy_enabled else 2
    if out > ceiling:
        out = ceiling
        notes.append("clamped to L2: no policy envelope to auto-merge inside")
    if out >= 3 and not control_external:
        out = 2
        notes.append("clamped to L2: control state is not outside the tree")
    return out, notes


def override_note(level: int, configured_merge_gate: bool | None) -> str:
    bundle = flag_bundle(level)
    if configured_merge_gate is None or configured_merge_gate == bundle["pause_before_pr_merge"]:
        return ""
    return (
        f"[factory] pause_before_pr_merge={configured_merge_gate} contradicts "
        f"{LABELS[level]} (bundle: {bundle['pause_before_pr_merge']}); bundle wins"
    )


SEQUENCES: list[tuple[str, list[str], bool]] = [
    ("fresh state, promote at once", ["promote"], False),
    ("eight runs, no merges", ["run"] * 8 + ["promote"], False),
    ("eight runs, five merges", ["run"] * 8 + ["merge"] * 5 + ["promote"], False),
    ("earn L2 then a violation", ["run"] * 8 + ["merge"] * 5 + ["promote", "violation"], False),
    (
        "cool-down burns down",
        ["run"] * 8
        + ["merge"] * 5
        + ["promote", "violation"]
        + ["run"] * 10
        + ["merge"] * 5
        + ["promote"],
        False,
    ),
    ("promote without an ack", ["run"] * 8 + ["merge"] * 5 + ["promote_noack"], False),
    (
        "calibration regression, advisory",
        ["run"] * 8 + ["merge"] * 5 + ["promote", "calibration"],
        False,
    ),
    (
        "calibration regression, demotion on",
        ["run"] * 8 + ["merge"] * 5 + ["promote", "calibration"],
        True,
    ),
    ("violation at the floor", ["violation", "violation"], False),
    (
        "edited merge resets the streak",
        ["run"] * 8
        + ["merge"] * 5
        + ["promote"]
        + ["run"] * 8
        + ["merge"] * 14
        + ["merge_edited", "promote"],
        False,
    ),
    (
        "L3 earned",
        ["run"] * 8 + ["merge"] * 5 + ["promote"] + ["run"] * 8 + ["merge"] * 15 + ["promote"],
        False,
    ),
]


def trace(name: str, actions: list[str], demote_on_calibration: bool) -> dict[str, object]:
    s = State()
    lines = [s.act(a, demote_on_calibration=demote_on_calibration) for a in actions]
    return {
        "name": name,
        "actions": actions,
        "demote_on_calibration": demote_on_calibration,
        "lines": lines,
        "level": s.level,
        "cooldown": s.cooldown,
        "counters": [s.decisive_runs, s.components_merged, s.clean_merges, s.policy_violations],
        "bundle": flag_bundle(s.level),
        "blockers": s.blockers(),
    }


def sweep() -> dict[str, object]:
    clamps = []
    for level, max_level, policy, external in product(
        (1, 2, 3, 4), (1, 2, 3, 4), (True, False), (True, False)
    ):
        rl, notes = runtime_level(level, max_level, policy, external)
        clamps.append(
            {
                "level": level,
                "max_level": max_level,
                "policy": policy,
                "external": external,
                "runtime": rl,
                "notes": notes,
            }
        )
    return {"traces": [trace(*s) for s in SEQUENCES], "clamps": clamps}


def main() -> None:
    data = sweep()
    if "--json" in sys.argv:
        json.dump(data, sys.stdout)
        return
    for t in data["traces"]:  # type: ignore[union-attr]
        print(
            f"{t['name']}: ends at {LABELS[t['level']]}, cool-down {t['cooldown']}, "  # type: ignore[index]
            f"counters {t['counters']}"
        )  # type: ignore[index]
        print(f"   last line: {t['lines'][-1]}")  # type: ignore[index]
    traces = {t["name"]: t for t in data["traces"]}  # type: ignore[union-attr]
    print()
    print(
        "claim 1: a fresh state cannot promote (evidence first) ->",
        "holds" if traces["fresh state, promote at once"]["level"] == 1 else "fails",
    )
    print(
        "claim 2: eight decisive runs and five merges earn L2; eight runs alone do not ->",
        "holds"
        if traces["eight runs, five merges"]["level"] == 2
        and traces["eight runs, no merges"]["level"] == 1
        else "fails",
    )
    print(
        "claim 3: a policy violation demotes one level, resets counters, "
        "starts a 10-run cool-down ->",
        "holds"
        if traces["earn L2 then a violation"]["level"] == 1
        and traces["earn L2 then a violation"]["cooldown"] == 10
        and traces["earn L2 then a violation"]["counters"] == [0, 0, 0, 0]
        else "fails",
    )
    print(
        "claim 4: after the cool-down elapses and the evidence is re-earned, "
        "L2 is offered again ->",
        "holds" if traces["cool-down burns down"]["level"] == 2 else "fails",
    )
    print(
        "claim 5: no ack, no promotion, whatever the evidence ->",
        "holds" if traces["promote without an ack"]["level"] == 1 else "fails",
    )
    print(
        "claim 6: a calibration regression is advisory by default and demotes "
        "only when switched on ->",
        "holds"
        if traces["calibration regression, advisory"]["level"] == 2
        and traces["calibration regression, demotion on"]["level"] == 1
        else "fails",
    )
    print(
        "claim 7: demotion at L1 is a no-op ->",
        "holds" if traces["violation at the floor"]["level"] == 1 else "fails",
    )
    print(
        "claim 8: one human-edited merge resets the clean streak, so L3 is refused ->",
        "holds" if traces["edited merge resets the streak"]["level"] == 2 else "fails",
    )
    print(
        "claim 9: L3 is earned by fifteen clean merges after eight runs at L2 ->",
        "holds" if traces["L3 earned"]["level"] == 3 else "fails",
    )

    def permissions(lv: int) -> list[bool]:
        b = flag_bundle(lv)
        # pause_before_pr_merge is a restriction; its absence is the permission.
        return [
            not b["pause_before_pr_merge"],
            bool(b["auto_accept_plan"]),
            bool(b["deps_allow_new_permitted"]),
            bool(b["auto_merge_when_green"]),
            bool(b["deploy_permitted"]),
        ]

    monotone = all(
        all(a <= b for a, b in zip(permissions(lv), permissions(lv + 1), strict=True))
        for lv in (1, 2, 3)
    )
    print(
        "claim 10: every permission in the bundle is monotone in the level; review stays hard ->",
        "holds"
        if monotone and all(flag_bundle(lv)["review_mode"] == "hard" for lv in (1, 2, 3, 4))
        else "fails",
    )
    clamps = data["clamps"]  # type: ignore[assignment]
    print(
        "claim 11: the runtime level never exceeds the earned level (clamps only withhold) ->",
        "holds" if all(c["runtime"] <= c["level"] for c in clamps) else "fails",
    )  # type: ignore[index]
    print(
        "claim 12: without a policy envelope or with control state in the tree, "
        "L3 and L4 run as L2 ->",
        "holds"
        if all(
            c["runtime"] <= 2
            for c in clamps  # type: ignore[index]
            if not c["policy"] or not c["external"]
        )
        else "fails",
    )  # type: ignore[index]
    print(
        "claim 13: a config that switches the merge gate off at L1 is recorded, not honoured ->",
        "holds"
        if "bundle wins" in override_note(1, False) and override_note(3, False) == ""
        else "fails",
    )


if __name__ == "__main__":
    main()
