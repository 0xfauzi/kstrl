"""Widget 6 of docs/lessons/pr-221.html: what happens when the review budget runs out.

Old rule: kstrl/pipeline.py:2465-2478. In hard mode with an unchunked review and
an exhausted ``max_adversarial_calls``, review_mode is downgraded to SKIP and the
component proceeds on mechanical checks alone. New rule: issue #226 (R10.5),
section 5.9 of the design. Hard mode fails closed with an infrastructure_error
finding and check ``adversarial_budget``; advisory mode keeps the skip.

A cap of 0 means unbounded. Each review that runs consumes one call. Security
review has the same shape (pipeline.py:2696-2701) and is left out here: one
rule per widget.

Sweeps cap x component count x mode, old rule and new rule side by side.
"""

from __future__ import annotations

import json
import sys
from itertools import product


def run(cap: int, components: int, mode: str, new_rule: bool) -> list[str]:
    used = 0
    out: list[str] = []
    for _ in range(components):
        exhausted = cap > 0 and used >= cap
        if mode == "advisory":
            if exhausted:
                out.append("skipped, merged on mechanical checks (phase_skipped recorded)")
            else:
                used += 1
                out.append("reviewed")
            continue
        # hard mode
        if not exhausted:
            used += 1
            out.append("reviewed")
        elif new_rule:
            out.append("HALTED: infrastructure_error, check=adversarial_budget")
        else:
            out.append("skipped, merged on mechanical checks (phase_skipped recorded)")
    return out


def sweep() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cap, n, mode, new in product(range(0, 6), range(1, 7), ("hard", "advisory"), (False, True)):
        out = run(cap, n, mode, new)
        rows.append(
            {
                "cap": cap,
                "n": n,
                "mode": mode,
                "new_rule": new,
                "outcome": out,
                "unreviewed": sum(1 for o in out if o.startswith("skipped")),
                "halted": sum(1 for o in out if o.startswith("HALTED")),
            }
        )
    return rows


def main() -> None:
    if "--json" in sys.argv:
        json.dump(sweep(), sys.stdout)
        return
    print("cap  n  mode      rule | outcome per component")
    unreviewed_merges_old = 0
    unreviewed_merges_new_hard = 0
    for cap, n, mode in product(range(0, 4), range(1, 5), ("hard", "advisory")):
        for label, new in (("old", False), ("new", True)):
            outcome = run(cap, n, mode, new)
            merged_unreviewed = sum(1 for o in outcome if o.startswith("skipped"))
            if label == "old":
                unreviewed_merges_old += merged_unreviewed
            elif mode == "hard":
                unreviewed_merges_new_hard += merged_unreviewed
            short = [o.split(":")[0].split(",")[0] for o in outcome]
            print(f"{cap:3d}  {n}  {mode:9s} {label:4s} | {short}")
    print()
    print(f"components merged unreviewed under the old rule, all modes: {unreviewed_merges_old}")
    print(
        "components merged unreviewed under the new rule in hard mode: "
        f"{unreviewed_merges_new_hard}"
    )
    print(
        "claim: with the default cap (0) nothing changes ->",
        "holds"
        if all(
            run(0, n, m, False) == run(0, n, m, True)
            for n in range(1, 5)
            for m in ("hard", "advisory")
        )
        else "fails",
    )
    print(
        "claim: advisory mode is byte-identical old and new ->",
        "holds"
        if all(
            run(c, n, "advisory", False) == run(c, n, "advisory", True)
            for c in range(4)
            for n in range(1, 5)
        )
        else "fails",
    )
    print(
        "claim: issue #226 test 1 (cap 1, two components, hard) halts the second ->",
        "holds"
        if run(1, 2, "hard", True)
        == ["reviewed", "HALTED: infrastructure_error, check=adversarial_budget"]
        else "fails",
    )
    print(
        "claim: the new hard rule never merges an unreviewed component ->",
        "holds" if unreviewed_merges_new_hard == 0 else "fails",
    )


if __name__ == "__main__":
    main()
