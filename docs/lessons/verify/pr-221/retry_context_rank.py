"""Widget 2 of docs/lessons/pr-221.html: the level-triggered retry context.

Old rule: ``IterationContext.format_for_prompt`` (kstrl/context.py:48-88)
renders every accumulated entry and ends with "Fix ALL issues listed above
before completing." The old renderer here is a port of that function; the
list each phase lands in today follows the call sites issue #223 tabulates
(engineer-phase entries go through add_review_finding at pipeline.py:2036 and
:2417, verification through add_verification_failure, review and security
through add_review_finding, contract through add_contract_failure).

New rule: issue #223 (R10.2), section 5.3 of the design. Each entry carries
the attempt it was measured in and the phase that measured it. N is the
latest attempt, Q the highest phase rank failing in N. Entries from earlier
attempts with rank above Q were not re-measured; rank at or below Q is
resolved or superseded and is not rendered. Legacy entries (attempt 0, from
a context serialised by today's code) always render under Not re-measured;
the issue special-cases them after a sweep of an earlier version of this
script showed the rank rule would otherwise drop findings of unknown age.

Failure texts are invented and of fixed length, so the rendered sizes the
page quotes are comparable, not real.

Sweeps every sequence of one to four attempts over the five phases, with and
without a legacy entry. ``--json`` emits the sweep.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from itertools import product

PHASE_RANK: dict[str, int] = {
    "engineer": 0,
    "verification": 1,
    "review": 2,
    "security": 3,
    "contract": 4,
}
PHASES = list(PHASE_RANK)

# Invented. Every failure text is this long so sizes compare like with like.
TEXT_LEN = 90
LEGACY_TEXT = "criterion 2 not met: the reviewer found the endpoint returns 200 on a bad id"


@dataclass(frozen=True)
class Entry:
    attempt: int
    phase: str
    text: str


def entry_text(attempt: int, phase: str) -> str:
    body = f"{phase} failure measured in attempt {attempt}: "
    return (body + "x" * TEXT_LEN)[:TEXT_LEN]


def build(sequence: list[str], legacy: bool) -> list[Entry]:
    entries: list[Entry] = []
    if legacy:
        entries.append(Entry(0, "review", LEGACY_TEXT))
    for i, phase in enumerate(sequence, start=1):
        entries.append(Entry(i, phase, entry_text(i, phase)))
    return entries


def buckets(entries: list[Entry]) -> dict[str, list[Entry]]:
    """The three buckets the new format_for_prompt renders."""
    out: dict[str, list[Entry]] = {"current": [], "not_remeasured": [], "resolved": []}
    live = [e for e in entries if e.attempt > 0]
    if not live:
        out["not_remeasured"] = [e for e in entries if e.attempt == 0]
        return out
    n = max(e.attempt for e in live)
    q = max(PHASE_RANK[e.phase] for e in live if e.attempt == n)
    for e in entries:
        if e.attempt == 0:
            # Legacy: age unknown, so "this phase ran and passed in attempt N"
            # cannot be inferred. Issue #223 renders these under Not re-measured.
            out["not_remeasured"].append(e)
        elif e.attempt == n:
            out["current"].append(e)
        elif PHASE_RANK[e.phase] > q:
            out["not_remeasured"].append(e)
        else:
            out["resolved"].append(e)
    return out


def render_new(entries: list[Entry]) -> str:
    """Issue #223's format_for_prompt, headings verbatim."""
    b = buckets(entries)
    live = [e for e in entries if e.attempt > 0]
    n = max((e.attempt for e in live), default=0)
    lines = [f"=== PREVIOUS ATTEMPT CONTEXT (Attempt {n + 1}) ===", ""]
    if b["current"]:
        phase = max((e for e in b["current"]), key=lambda e: PHASE_RANK[e.phase]).phase
        lines.append(f"## Current failures (measured in attempt {n}, {phase})")
        lines.extend(e.text for e in b["current"])
        lines.append("")
    if b["not_remeasured"]:
        k = min(e.attempt for e in b["not_remeasured"])
        lines.append(f"## Not re-measured since attempt {k}")
        for e in b["not_remeasured"]:
            label = "attempt unknown" if e.attempt == 0 else f"attempt {e.attempt}"
            lines.append(f"({label}, {e.phase}) {e.text}")
        lines.append("")
    if b["resolved"]:
        names = ", ".join(sorted({e.phase for e in b["resolved"]}, key=lambda p: PHASE_RANK[p]))
        lines.append("## Resolved or superseded")
        lines.append(
            f"{len(b['resolved'])} earlier finding(s) from {names} passed or were "
            f"re-measured in attempt {n} and are omitted."
        )
        lines.append("")
    if live:
        lines.append("## Attempt history")
        for i in range(1, n + 1):
            phase = next(e.phase for e in live if e.attempt == i)
            lines.append(f"- Attempt {i}: FAILED - {phase} failed")
        lines.append("")
    lines.append("Fix the current failures. Re-check the not-re-measured items yourself; "
                 "do not assume they still apply.")
    lines.append("=== END PREVIOUS CONTEXT ===")
    return "\n".join(lines)


def render_old(entries: list[Entry]) -> str:
    """Today's format_for_prompt (kstrl/context.py:48-88), ported."""
    live = [e for e in entries if e.attempt > 0]
    n = max((e.attempt for e in live), default=0)
    verification = [e.text for e in entries if e.phase == "verification"]
    review = [e.text for e in entries if e.phase in ("engineer", "review", "security")]
    contract = [e.text for e in entries if e.phase == "contract"]
    sections = [f"=== PREVIOUS ATTEMPT CONTEXT (Attempt {n + 1}) ==="]
    if live:
        sections.append("")
        sections.append("## Iteration History")
        for i in range(1, n + 1):
            phase = next(e.phase for e in live if e.attempt == i)
            sections.append(f"- Iteration {i}: FAILED - {phase} failed")
    if verification:
        sections.append("")
        sections.append("## Verification Failures")
        sections.extend(verification)
    if review:
        sections.append("")
        sections.append("## Review Findings")
        sections.extend(review)
    if contract:
        sections.append("")
        sections.append("## Contract Test Failures")
        sections.extend(contract)
    sections.append("")
    sections.append("Fix ALL issues listed above before completing.")
    sections.append("=== END PREVIOUS CONTEXT ===")
    return "\n".join(sections)


def evaluate(sequence: list[str], legacy: bool) -> dict[str, object]:
    entries = build(sequence, legacy)
    b = buckets(entries)
    return {
        "sequence": sequence,
        "legacy": legacy,
        "current": [e.text for e in b["current"]],
        "not_remeasured": [e.text for e in b["not_remeasured"]],
        "resolved": len(b["resolved"]),
        "new_chars": len(render_new(entries)),
        "old_chars": len(render_old(entries)),
        "old_entries": len(entries),
    }


def sweep() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for k in range(1, 5):
        for seq in product(PHASES, repeat=k):
            for legacy in (False, True):
                rows.append(evaluate(list(seq), legacy))
    return rows


def main() -> None:
    rows = sweep()
    if "--json" in sys.argv:
        json.dump(rows, sys.stdout)
        return
    print(f"sequences swept: {len(rows)} (lengths 1 to 4 over {len(PHASES)} phases, "
          f"with and without a legacy entry)")

    ok = True
    for r in rows:
        seq = list(r["sequence"])  # type: ignore[arg-type]
        n = len(seq)
        entries = build(seq, bool(r["legacy"]))
        b = buckets(entries)
        # every entry lands in exactly one bucket
        if len(b["current"]) + len(b["not_remeasured"]) + len(b["resolved"]) != len(entries):
            ok = False
        # current is exactly the latest attempt's entries
        if [e.attempt for e in b["current"]] != [n]:
            ok = False
        q = PHASE_RANK[seq[-1]]
        for e in b["not_remeasured"]:
            if e.attempt != 0 and not (e.attempt < n and PHASE_RANK[e.phase] > q):
                ok = False
        for e in b["resolved"]:
            if not (0 < e.attempt < n and PHASE_RANK[e.phase] <= q):
                ok = False
    print("claim 1: every entry lands in exactly one bucket, current holds only the "
          "latest attempt, and the rank rule decides the rest ->", "holds" if ok else "fails")

    same = [len(buckets(build(["verification"] * k, False))["current"]) for k in range(1, 5)]
    old = [len(build(["verification"] * k, False)) for k in range(1, 5)]
    print(f"k consecutive verification failures, k = 1..4: current section entries "
          f"{same}; old rendering entries {old}")
    print("claim 2: the current section does not grow with k; the old rendering does ->",
          "holds" if same == [1, 1, 1, 1] and old == [1, 2, 3, 4] else "fails")

    e1 = evaluate(["verification", "review"], False)
    print("claim 3: issue example 1 (verification, then review): E501 resolved, "
          "criterion current ->",
          "holds" if e1["resolved"] == 1 and len(e1["current"]) == 1  # type: ignore[arg-type]
          and not e1["not_remeasured"] else "fails")
    e2 = evaluate(["review", "verification"], False)
    print("claim 4: issue example 2 (review, then verification): review entry not "
          "re-measured ->",
          "holds" if e2["resolved"] == 0 and len(e2["not_remeasured"]) == 1  # type: ignore[arg-type]
          else "fails")

    legacy_ok = all(
        LEGACY_TEXT in evaluate([p], True)["not_remeasured"]  # type: ignore[operator]
        for p in PHASES
    )
    print("claim 5: a legacy entry stays under Not re-measured whatever attempt 1 fails at "
          "(issue #223 test 6, five sub-cases) ->", "holds" if legacy_ok else "fails")

    sizes_old = [int(evaluate(["verification"] * k, False)["old_chars"]) for k in range(1, 6)]
    sizes_new = [int(evaluate(["verification"] * k, False)["new_chars"]) for k in range(1, 6)]
    d_old = [b - a for a, b in zip(sizes_old, sizes_old[1:], strict=False)]
    d_new = [b - a for a, b in zip(sizes_new[1:], sizes_new[2:], strict=False)]
    print(f"per-attempt growth, same phase: old {d_old} chars; new (from k=2) {d_new} chars")
    print("claim 6: from the second attempt on, each further attempt adds a full failure "
          "text to the old rendering and only a history line to the new one ->",
          "holds" if all(d >= TEXT_LEN for d in d_old) and all(0 < d < TEXT_LEN for d in d_new)
          else "fails")
    crossover = next((k for k in range(1, 6) if sizes_new[k - 1] < sizes_old[k - 1]), None)
    print(f"observation: the new rendering is LARGER than the old for the first attempts "
          f"(longer headings) and smaller from k = {crossover}; it is bounded, not small.")

    entries = build(["verification", "review"], False)
    print("claim 7: the closing instruction changed ->",
          "holds" if "Fix ALL issues listed above" in render_old(entries)
          and "Fix ALL issues listed above" not in render_new(entries) else "fails")

    print()
    print("sizes, invented texts of fixed length:")
    for seq in (["verification"], ["verification"] * 2, ["verification"] * 3,
                ["verification"] * 4, ["review", "verification"], ["verification", "review"]):
        r = evaluate(seq, False)
        print(f"  {' -> '.join(seq):55s} old {r['old_chars']:5d} chars, new {r['new_chars']:5d} chars")


if __name__ == "__main__":
    main()
