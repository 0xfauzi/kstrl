"""Widget 1 of docs/lessons/pr-221.html: who is allowed to say a story is done.

Old rule: ``check_prd_stories`` (kstrl/verify.py:526) passes a story when the
engineer wrote ``passes: true`` and fails the component in Phase 1 when any
story is still false. It never consults the reviewer.

New rule: issue #224 (R10.3), section 5.2 of docs/control-loop-design.md.
``setpoint_disagreements`` emits one finding per story the engineer marked
done that the reviewer did not independently mark ``pass``. The blocking rule
mirrors ``layer0_blocks`` (kstrl/adequacy.py:721-737): the gate blocks when
``[factory] setpoint_agreement`` is ``block`` OR the resolved autonomy level
is 1 or higher. Autonomy may tighten a gate and never loosen one.

Sweeps every reachable combination of the widget's controls. ``--json``
emits the sweep so the page's inline script can be checked against it.
"""

from __future__ import annotations

import json
import sys
from itertools import product

VERDICTS = ("pass", "fail", "advisory", "uncovered")
REVIEW_STATES = ("ran", "infra", "skip")
MODES = ("advisory", "block")
LEVELS = (0, 1, 2, 3, 4)


def story_verdict(criteria: tuple[str, ...]) -> str | None:
    """Issue #224 section 2: fail dominates, then advisory, else pass.

    A criterion marked ``uncovered`` has no verdict and is ignored. When no
    criterion has a verdict the story was not covered: None.
    """
    covered = [c for c in criteria if c != "uncovered"]
    if not covered:
        return None
    if "fail" in covered:
        return "fail"
    if "advisory" in covered:
        return "advisory"
    return "pass"


def blocks(mode: str, level: int) -> bool:
    return mode == "block" or level >= 1


def old_rule(engineer_passes: bool) -> str:
    """check_prd_stories reads the flag the engineer wrote and nothing else."""
    if engineer_passes:
        return "phase 1 passes; the flag is the verdict"
    return "phase 1 FAILS: story not marked passing; retry before review"


def new_rule(
    engineer_passes: bool,
    review: str,
    criteria: tuple[str, ...],
    mode: str,
    level: int,
) -> dict[str, object]:
    """setpoint_disagreements plus setpoint_blocks, per issue #224 sections 3 to 5.

    Returns an outcome key (what the page prints a sentence for), whether a
    finding exists, its severity, and the reviewer's story verdict.
    """
    verdict = story_verdict(criteria)
    shown = verdict if verdict is not None else "not covered"
    if not engineer_passes:
        return {"key": "unclaimed", "finding": False, "severity": "", "verdict": shown}
    if review == "skip":
        return {"key": "skip", "finding": False, "severity": "", "verdict": shown}
    if review == "infra":
        return {"key": "infra", "finding": False, "severity": "", "verdict": shown}
    if verdict == "pass":
        return {"key": "agree", "finding": False, "severity": "", "verdict": shown}
    if blocks(mode, level):
        return {"key": "block", "finding": True, "severity": "fail", "verdict": shown}
    return {"key": "advisory", "finding": True, "severity": "advisory", "verdict": shown}


def sweep() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for eng, rev, c1, c2, mode, level in product(
        (True, False), REVIEW_STATES, VERDICTS, VERDICTS, MODES, LEVELS,
    ):
        out = new_rule(eng, rev, (c1, c2), mode, level)
        rows.append({
            "engineer_passes": eng, "review": rev, "c1": c1, "c2": c2,
            "mode": mode, "level": level, "old": old_rule(eng), **out,
        })
    return rows


def main() -> None:
    rows = sweep()
    if "--json" in sys.argv:
        json.dump(rows, sys.stdout)
        return
    findings = [r for r in rows if r["finding"]]
    old_pass_new_finding = [r for r in findings if r["engineer_passes"]]
    blocking = [r for r in findings if r["key"] == "block"]
    print(f"rows swept: {len(rows)}")
    print(f"rows with a finding: {len(findings)}")
    print(f"rows where the OLD check passed and the NEW rule files a finding: "
          f"{len(old_pass_new_finding)}")
    print(f"rows that block (revert the flag and retry): {len(blocking)}")
    print()
    for key in ("unclaimed", "skip", "infra", "agree", "advisory", "block"):
        print(f"  outcome {key:9s}: {sum(1 for r in rows if r['key'] == key)} rows")
    print()
    print("claim 1: the old check never consults the reviewer ->",
          "holds" if all(r["old"] == old_rule(bool(r["engineer_passes"])) for r in rows)
          else "fails")
    print("claim 2: a story the engineer did not claim never produces a finding ->",
          "holds" if not any(r["finding"] for r in rows if not r["engineer_passes"])
          else "fails")
    print("claim 3: no measurement (infra error or skip) never produces a finding ->",
          "holds" if not any(r["finding"] for r in rows if r["review"] != "ran")
          else "fails")
    print("claim 4: a finding exists exactly when the engineer claimed, the review "
          "ran, and the story verdict is not pass ->",
          "holds" if all(
              r["finding"] == (r["engineer_passes"] and r["review"] == "ran"
                               and r["verdict"] != "pass")
              for r in rows) else "fails")
    print("claim 5: one uncovered criterion beside a pass still reads as pass ->",
          "holds" if story_verdict(("pass", "uncovered")) == "pass" else "fails")
    print("claim 6: blocks exactly when mode is block or level >= 1 (issue #224 test 10) ->",
          "holds" if [blocks("advisory", lv) for lv in LEVELS] == [False, True, True, True, True]
          and all(blocks("block", lv) for lv in LEVELS) else "fails")
    print("claim 7: with the default config (advisory, ladder off) nothing blocks ->",
          "holds" if not any(r["key"] == "block" for r in rows
                             if r["mode"] == "advisory" and r["level"] == 0) else "fails")
    print("claim 8: a fail verdict on one criterion dominates a pass on the other ->",
          "holds" if story_verdict(("pass", "fail")) == "fail"
          and story_verdict(("advisory", "pass")) == "advisory" else "fails")


if __name__ == "__main__":
    main()
