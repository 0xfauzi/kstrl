"""Widget 6 of docs/lessons/pr-221.html: the order the agent's context is assembled.

Today (kstrl/factory.py:1439-1450) the worker joins ``parts`` in this order:
knowledge prefix, feedforward prefix, retry context. ``run_loop``
(kstrl/loop.py:529-541) then prepends CLAUDE.md, headed "# Project Context
(from CLAUDE.md)", to the templated prompt, and prepends the whole context
prefix in front of that. So the agent reads, in order: knowledge,
feedforward, retry context, CLAUDE.md, the instructions.

Issue #229 (R10.8) inserts golden patterns after knowledge and before
feedforward. Issue #230 (R10.9) appends the memory file as the LAST part of
the prefix, immediately after the retry context, so a standing correction is
read right after the controller's output it corrects.

Sweeps every presence combination in both states. ``--json`` emits it.
"""

from __future__ import annotations

import json
import sys
from itertools import product

PARTS = ["knowledge", "golden", "feedforward", "retry", "memory", "claude_md"]
NAMES = {
    "knowledge": "knowledge (distilled facts)",
    "golden": "golden patterns (R10.8)",
    "feedforward": "feedforward (Phase 0)",
    "retry": "retry context (the controller's output)",
    "memory": "memory file (R10.9)",
    "claude_md": "CLAUDE.md (project context)",
    "template": "the instructions (prompt.md)",
}


def order(state: str, present: dict[str, bool]) -> list[str]:
    """The prompt's parts in reading order. state is 'today' or 'end'."""
    prefix = ["knowledge", "feedforward", "retry"]
    if state == "end":
        prefix = ["knowledge", "golden", "feedforward", "retry", "memory"]
    out = [p for p in prefix if present.get(p)]
    if present.get("claude_md"):
        out.append("claude_md")
    out.append("template")
    return out


def after_retry(seq: list[str]) -> str:
    if "retry" not in seq:
        return ""
    return seq[seq.index("retry") + 1]


def sweep() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for state in ("today", "end"):
        for values in product((True, False), repeat=len(PARTS)):
            present = dict(zip(PARTS, values, strict=True))
            if state == "today" and (present["golden"] or present["memory"]):
                continue
            seq = order(state, present)
            rows.append(
                {
                    "state": state,
                    **present,
                    "order": seq,
                    "after_retry": after_retry(seq),
                    "last_before_template": seq[-2] if len(seq) > 1 else "",
                }
            )
    return rows


def main() -> None:
    rows = sweep()
    if "--json" in sys.argv:
        json.dump(rows, sys.stdout)
        return
    print(f"rows swept: {len(rows)}")
    print(
        "claim 1: the instructions are always last ->",
        "holds" if all(r["order"][-1] == "template" for r in rows) else "fails",
    )  # type: ignore[index]
    print(
        "claim 2: CLAUDE.md, when present, sits immediately before the instructions ->",
        "holds" if all(r["order"][-2] == "claude_md" for r in rows if r["claude_md"]) else "fails",
    )  # type: ignore[index]
    print(
        "claim 3: in the end state, memory follows the retry context immediately "
        "when both are present ->",
        "holds"
        if all(
            r["after_retry"] == "memory"
            for r in rows
            if r["state"] == "end" and r["retry"] and r["memory"]
        )
        else "fails",
    )
    print(
        "claim 4: golden patterns sit between knowledge and feedforward ->",
        "holds"
        if all(
            r["order"].index("knowledge")
            < r["order"].index("golden")
            < r["order"].index("feedforward")  # type: ignore[attr-defined]
            for r in rows
            if r["state"] == "end" and r["knowledge"] and r["golden"] and r["feedforward"]
        )
        else "fails",
    )
    print(
        "claim 5: today, what follows the retry context is CLAUDE.md when the worktree has one, "
        "else the instructions ->",
        "holds"
        if all(
            r["after_retry"] == ("claude_md" if r["claude_md"] else "template")
            for r in rows
            if r["state"] == "today" and r["retry"]
        )
        else "fails",
    )
    print(
        "claim 6: the memory file is never the last part; CLAUDE.md or the "
        "instructions always follow it ->",
        "holds"
        if all(
            r["order"][-1] != "memory" and r["order"][-2] != "memory" or not r["claude_md"]  # type: ignore[index]
            for r in rows
            if r["memory"]
        )
        and all(
            r["order"].index("memory") < r["order"].index("claude_md")  # type: ignore[attr-defined]
            for r in rows
            if r["memory"] and r["claude_md"]
        )
        else "fails",
    )
    print()
    full = order("end", dict.fromkeys(PARTS, True))
    print("end state, everything present:", " -> ".join(NAMES[p] for p in full))
    today = order("today", dict.fromkeys(PARTS, True))
    print("today, everything present:    ", " -> ".join(NAMES[p] for p in today))


if __name__ == "__main__":
    main()
