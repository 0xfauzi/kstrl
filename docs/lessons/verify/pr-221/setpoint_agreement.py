"""Widget 1 of docs/lessons/pr-221.html: two sensors must agree.

The rule is issue #224 (R10.3), section 5.2 of docs/control-loop-design.md.
Old rule: ``check_prd_stories`` (kstrl/verify.py:526) passes a story when the
engineer wrote ``passes: true``. New rule: ``setpoint_disagreements`` emits one
finding per story the engineer marked done that the reviewer did not
independently mark ``pass``.

Sweeps every reachable combination of the widget's controls and prints the old
verdict beside the new one, so the prose claims can be read against output.
"""

from __future__ import annotations

from itertools import product

VERDICTS = ("pass", "fail", "advisory")


def story_verdict(criteria: tuple[str, ...]) -> str | None:
    """Issue #224 section 2: fail dominates, then advisory, else pass.

    None means the reviewer did not cover the story (no criterion verdict).
    """
    if not criteria:
        return None
    if "fail" in criteria:
        return "fail"
    if "advisory" in criteria:
        return "advisory"
    return "pass"


def old_rule(engineer_passes: bool) -> str:
    """check_prd_stories reads the flag the engineer wrote and nothing else."""
    return "pass" if engineer_passes else "FAIL (story not marked passing)"


def new_rule(
    engineer_passes: bool, criteria: tuple[str, ...], infra_error: bool,
) -> str:
    """setpoint_disagreements, per issue #224 section 3."""
    if infra_error:
        return "no finding (review did not run: no measurement, no disagreement)"
    if not engineer_passes:
        return "no finding (engineer did not claim it)"
    verdict = story_verdict(criteria)
    if verdict == "pass":
        return "agree: no finding"
    shown = verdict if verdict is not None else "not covered"
    return f"FINDING setpoint_disagreement (reviewer: {shown})"


def main() -> None:
    print("engineer  covered  criteria            infra  | old check_prd_stories | new setpoint_disagreements")
    findings = 0
    old_pass_new_finding = 0
    rows = 0
    for engineer_passes, covered, infra in product((True, False), (True, False), (False, True)):
        combos = list(product(VERDICTS, repeat=2)) if covered else [()]
        for criteria in combos:
            rows += 1
            old = old_rule(engineer_passes)
            new = new_rule(engineer_passes, criteria, infra)
            if new.startswith("FINDING"):
                findings += 1
                if old == "pass":
                    old_pass_new_finding += 1
            crit = ",".join(criteria) if criteria else "(uncovered)"
            print(f"{str(engineer_passes):8s}  {str(covered):7s}  {crit:18s}  {str(infra):5s}  | {old:22s} | {new}")
    print()
    print(f"rows swept: {rows}")
    print(f"rows with a new finding: {findings}")
    print(f"rows where the OLD check passed and the NEW rule finds a disagreement: {old_pass_new_finding}")
    print("claim 1: the old check never consults the reviewer ->",
          "holds" if all(old_rule(True) == "pass" for _ in range(1)) else "fails")
    print("claim 2: a story the engineer did not claim never produces a finding ->",
          "holds" if all(not new_rule(False, c, False).startswith("FINDING")
                         for c in list(product(VERDICTS, repeat=2)) + [()]) else "fails")
    print("claim 3: an infrastructure error produces no finding ->",
          "holds" if not new_rule(True, ("fail", "fail"), True).startswith("FINDING") else "fails")
    print("claim 4: only an all-pass covered story agrees ->",
          "holds" if new_rule(True, ("pass", "pass"), False) == "agree: no finding"
          and new_rule(True, (), False).startswith("FINDING") else "fails")


if __name__ == "__main__":
    main()
