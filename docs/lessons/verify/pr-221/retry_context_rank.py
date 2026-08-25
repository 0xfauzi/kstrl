"""Widget 2 of docs/lessons/pr-221.html: the level-triggered retry context.

The rule is issue #223 (R10.2), section 5.3 of docs/control-loop-design.md.
Each failure entry carries the attempt it was measured in and the phase that
measured it. At render time N is the latest attempt and Q the highest phase
rank that failed in attempt N. Entries from earlier attempts with rank above Q
were not re-measured; entries with rank at or below Q are resolved or
superseded and are not rendered.

Sweeps every (phase of attempt 1, phase of attempt 2) pair, then k consecutive
same-phase failures, then the two worked examples from the issue, then the
legacy attempt-0 case the issue describes in words.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

PHASE_RANK: dict[str, int] = {
    "engineer": 0,
    "verification": 1,
    "review": 2,
    "security": 3,
    "contract": 4,
}


@dataclass(frozen=True)
class Entry:
    attempt: int
    phase: str
    text: str


def render(entries: list[Entry]) -> dict[str, list[Entry]]:
    """Return the three buckets the new format_for_prompt renders."""
    if not entries:
        return {"current": [], "not_remeasured": [], "resolved": []}
    n = max(e.attempt for e in entries)
    q = max(PHASE_RANK[e.phase] for e in entries if e.attempt == n)
    out: dict[str, list[Entry]] = {"current": [], "not_remeasured": [], "resolved": []}
    for e in entries:
        if e.attempt == n:
            out["current"].append(e)
        elif PHASE_RANK[e.phase] > q:
            out["not_remeasured"].append(e)
        else:
            out["resolved"].append(e)
    return out


def old_render(entries: list[Entry]) -> list[Entry]:
    """Today's format_for_prompt: every entry, then 'Fix ALL issues listed above'."""
    return list(entries)


def main() -> None:
    phases = list(PHASE_RANK)
    print("attempt-1 phase   attempt-2 phase   | where attempt 1's entry lands")
    counts = {"not_remeasured": 0, "resolved": 0}
    for p1, p2 in product(phases, repeat=2):
        buckets = render([Entry(1, p1, "a1"), Entry(2, p2, "a2")])
        where = "not_remeasured" if buckets["not_remeasured"] else "resolved"
        counts[where] += 1
        print(f"{p1:16s}  {p2:16s}  | {where}   (current = {[e.text for e in buckets['current']]})")
    print(f"\npairs swept: {len(phases) ** 2}; not_remeasured: {counts['not_remeasured']}; resolved: {counts['resolved']}")
    print("rule check: attempt 1 is not re-measured exactly when rank(p1) > rank(p2) ->",
          "holds" if counts["not_remeasured"] == sum(
              1 for a, b in product(phases, repeat=2) if PHASE_RANK[a] > PHASE_RANK[b]) else "fails")

    print("\nk consecutive verification failures: size of each bucket, old rendering size")
    for k in range(1, 6):
        entries = [Entry(i, "verification", f"v{i}") for i in range(1, k + 1)]
        b = render(entries)
        print(f"k={k}: current={len(b['current'])} not_remeasured={len(b['not_remeasured'])} "
              f"resolved={len(b['resolved'])} | old rendering carries {len(old_render(entries))} entries")

    print("\nworked example 1 (issue #223): a1 verification E501, a2 review criterion X")
    b = render([Entry(1, "verification", "linter: E501"), Entry(2, "review", "criterion X")])
    print("  current:", [e.text for e in b["current"]], "| resolved:", [e.text for e in b["resolved"]],
          "| not re-measured:", [e.text for e in b["not_remeasured"]])
    print("  claim: E501 appears nowhere in the rendered prompt ->",
          "holds" if all(e.text != "linter: E501" for e in b["current"] + b["not_remeasured"]) else "fails")

    print("\nworked example 2 (issue #223): a1 review criterion X, a2 verification arg-type")
    b = render([Entry(1, "review", "criterion X"), Entry(2, "verification", "typecheck: arg-type")])
    print("  current:", [e.text for e in b["current"]], "| resolved:", [e.text for e in b["resolved"]],
          "| not re-measured:", [e.text for e in b["not_remeasured"]])

    print("\nlegacy entries (attempt 0, from a context serialised by today's code)")
    for later in ("engineer", "verification", "review", "security", "contract"):
        b = render([Entry(0, "review", "old review text"), Entry(1, later, "new")])
        where = "not_remeasured" if b["not_remeasured"] else "resolved"
        print(f"  legacy review entry + attempt-1 {later:12s} -> rank rule puts it under: {where}")
    print("  the issue's prose says legacy entries 'always render under Not re-measured';"
          " the rank rule as written disagrees when the later failure ranks at or above review.")


if __name__ == "__main__":
    main()
