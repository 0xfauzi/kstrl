"""Widget 4 of docs/lessons/pr-221.html: why the daemon can refuse to start work.

The rule is ``serve_cycle`` (kstrl/serve.py:1866 onward) as it reads on the
merge commit, plus the open-PR bound issue #228 (R10.7) inserts. The order
the code evaluates, each with what a refusal means:

    unreadable spend ledger ............ halt, needs a human (before anything)
    pause marker active ................ skip this cycle
    poison breaker ..................... pause the queue
    cost coverage ...................... pause the queue
    daily budget ....................... pause the queue, resume at midnight
    open-PR bound (after R10.7) ........ wait; re-check next cycle
    inbox open-item cap ................ wait
    factory lock held .................. wait
    claim .............................. one item, or "nothing ready"

Issue #228 places the open-PR bound "as the last gate, after check_budget"
in the ``gates`` tuple at serve.py:1974-1978. That tuple is evaluated before
the inbox cap and the factory lock, so the bound sits before those two, not
after them. This script follows the issue's concrete instruction.

Defaults from the code: max_consecutive_poison = 3, daily_budget_usd = 0
(off), allow_uncovered_cost = False, inbox open_item_cap = 50, and R10.7's
max_open_prs = 1. ``ks factory`` and ``ks run`` never enter serve_cycle.

Sweeps every combination of the widget's controls. ``--json`` emits it.
"""

from __future__ import annotations

import json
import sys
from itertools import product

MAX_CONSECUTIVE_POISON = 3


def admit(
    after_r10_7: bool,
    ledger_readable: bool,
    paused: bool,
    poison_streak: int,
    budget_on: bool,
    budget_reached: bool,
    coverage_seen: bool,
    allow_uncovered: bool,
    max_open_prs: int,
    open_prs: int,
    create_prs: bool,
    gh_ok: bool,
    inbox_at_cap: bool,
    lock_held: bool,
    ready_item: bool,
) -> dict[str, str]:
    """(gate, kind, reason). kind is halt | skip | pause | wait | claim."""
    if not ledger_readable:
        return {
            "gate": "spend ledger",
            "kind": "halt",
            "reason": "the daemon's own state is unreadable; nothing can be evaluated",
        }
    if paused:
        return {"gate": "pause marker", "kind": "skip", "reason": "the queue is paused"}
    if poison_streak >= MAX_CONSECUTIVE_POISON:
        return {
            "gate": "poison breaker",
            "kind": "pause",
            "reason": f"{poison_streak} items poisoned in a row; something systemic",
        }
    if budget_on and not allow_uncovered and not coverage_seen:
        return {
            "gate": "cost coverage",
            "kind": "pause",
            "reason": "a budget is set but no call ever reported a cost; the cap can never fire",
        }
    if budget_on and budget_reached:
        return {
            "gate": "daily budget",
            "kind": "pause",
            "reason": "daily budget reached; resumes at local midnight",
        }
    if after_r10_7 and max_open_prs > 0 and create_prs:
        if not gh_ok:
            return {
                "gate": "open-PR bound",
                "kind": "wait",
                "reason": "cannot count open kstrl PRs: gh failed; an unknown count is not zero",
            }
        if open_prs >= max_open_prs:
            return {
                "gate": "open-PR bound",
                "kind": "wait",
                "reason": f"{open_prs} kstrl PR(s) open (bound {max_open_prs}); waiting for review",
            }
    if inbox_at_cap:
        return {
            "gate": "inbox cap",
            "kind": "wait",
            "reason": "the inbox is at its open-item cap; triage before queueing more",
        }
    if lock_held:
        return {
            "gate": "factory lock",
            "kind": "wait",
            "reason": "a factory run already holds this root",
        }
    if not ready_item:
        return {"gate": "claim", "kind": "skip", "reason": "nothing ready"}
    return {"gate": "claim", "kind": "claim", "reason": "one item leased and run"}


GRID = {
    "after_r10_7": (False, True),
    "ledger_readable": (True, False),
    "paused": (False, True),
    "poison_streak": (0, 2, 3),
    "budget_on": (False, True),
    "budget_reached": (False, True),
    "coverage_seen": (True, False),
    "allow_uncovered": (False, True),
    "max_open_prs": (0, 1, 2),
    "open_prs": (0, 1, 2),
    "create_prs": (True, False),
    "gh_ok": (True, False),
    "inbox_at_cap": (False, True),
    "lock_held": (False, True),
    "ready_item": (True, False),
}
ORDER = [
    "spend ledger",
    "pause marker",
    "poison breaker",
    "cost coverage",
    "daily budget",
    "open-PR bound",
    "inbox cap",
    "factory lock",
    "claim",
]


def sweep() -> list[dict[str, object]]:
    keys = list(GRID)
    rows: list[dict[str, object]] = []
    for values in product(*(GRID[k] for k in keys)):
        args = dict(zip(keys, values, strict=True))
        rows.append({**args, **admit(**args)})  # type: ignore[arg-type]
    return rows


def main() -> None:
    rows = sweep()
    if "--json" in sys.argv:
        json.dump(rows, sys.stdout)
        return
    print(f"rows swept: {len(rows)}")
    by_gate = {g: sum(1 for r in rows if r["gate"] == g) for g in ORDER}
    for g in ORDER:
        kinds = sorted({str(r["kind"]) for r in rows if r["gate"] == g})
        print(f"  {g:15s} decides {by_gate[g]:6d} rows, as {kinds}")
    print()
    pauses = {str(r["gate"]) for r in rows if r["kind"] == "pause"}
    print(
        "claim 1: only the three ledger gates ever pause the queue ->",
        "holds" if pauses == {"poison breaker", "cost coverage", "daily budget"} else "fails",
    )
    print(
        "claim 2: the open-PR bound only ever waits, never pauses ->",
        "holds"
        if all(r["kind"] == "wait" for r in rows if r["gate"] == "open-PR bound")
        else "fails",
    )
    print(
        "claim 3: before R10.7 the open-PR inputs never decide anything ->",
        "holds"
        if not any(r["gate"] == "open-PR bound" for r in rows if not r["after_r10_7"])
        else "fails",
    )
    print(
        "claim 4: a failed gh count refuses; it is never read as zero ->",
        "holds"
        if all(
            r["gate"] == "open-PR bound"
            for r in rows
            if r["after_r10_7"]
            and not r["gh_ok"]
            and r["max_open_prs"] > 0
            and r["create_prs"]
            and not r["paused"]
            and r["ledger_readable"]
            and r["poison_streak"] < 3
            and not (r["budget_on"] and not r["allow_uncovered"] and not r["coverage_seen"])
            and not (r["budget_on"] and r["budget_reached"])
        )
        else "fails",
    )
    print(
        "claim 5: bound 0 or create_prs=false never consults gh ->",
        "holds"
        if not any(
            r["gate"] == "open-PR bound"
            for r in rows
            if r["max_open_prs"] == 0 or not r["create_prs"]
        )
        else "fails",
    )
    print(
        "claim 6: the default bound (1) refuses as soon as one kstrl PR is open ->",
        "holds"
        if admit(
            True, True, False, 0, False, False, True, False, 1, 1, True, True, False, False, True
        )["gate"]
        == "open-PR bound"
        and admit(
            True, True, False, 0, False, False, True, False, 1, 0, True, True, False, False, True
        )["kind"]
        == "claim"
        else "fails",
    )
    print(
        "claim 7: an unreadable ledger halts before any other gate is read ->",
        "holds" if all(r["kind"] == "halt" for r in rows if not r["ledger_readable"]) else "fails",
    )
    print(
        "claim 8: with everything nominal and an item ready, the cycle claims ->",
        "holds"
        if admit(
            True, True, False, 0, False, False, True, False, 1, 0, True, True, False, False, True
        )["kind"]
        == "claim"
        else "fails",
    )
    print(
        "claim 9: the budget gates are inert while daily_budget_usd is 0 ->",
        "holds"
        if not any(
            r["gate"] in ("cost coverage", "daily budget") for r in rows if not r["budget_on"]
        )
        else "fails",
    )
    print()
    print(
        "observation: the open-PR bound is evaluated before the inbox cap and the "
        "factory lock, because issue #228 puts it in the gates tuple; the issue's "
        "sentence 'a GitHub call happens only when everything else admits' is true "
        "of the three ledger gates and not of those two."
    )


if __name__ == "__main__":
    main()
