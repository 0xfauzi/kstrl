"""Widget 5 of docs/lessons/pr-221.html: the open-PR bound.

The rule is ``check_open_pr_bound`` as specified in issue #228 (R10.7),
section 5.6 of docs/control-loop-design.md. It is the last admission gate in
``serve_cycle`` and applies only to the daemon; ``ks factory`` and ``ks run``
never call it.

Sweeps invocation x bound x open count x create_prs x counter failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str
    wait: bool = False


def check_open_pr_bound(
    bound: int, open_count: int | None, create_prs: bool,
) -> Admission:
    """open_count None means the gh call failed."""
    if bound == 0:
        return Admission(True, "open-PR bound disabled")
    if not create_prs:
        return Admission(True, "open-PR bound not applicable (create_prs = false)")
    if open_count is None:
        return Admission(False, "cannot count open kstrl PRs: gh failed", wait=True)
    if open_count < bound:
        return Admission(True, f"{open_count} of {bound} kstrl PRs open")
    return Admission(False, f"{open_count} kstrl PR(s) open (bound {bound}); waiting for review", wait=True)


def admit(invocation: str, bound: int, open_count: int | None, create_prs: bool) -> Admission:
    if invocation != "ks serve":
        return Admission(True, f"{invocation} is manual: the human typing it is the authorisation")
    return check_open_pr_bound(bound, open_count, create_prs)


def main() -> None:
    print("invocation  bound  open   create_prs | allowed | reason")
    refusals = 0
    rows = 0
    for inv, bound, open_count, create in product(
        ("ks serve", "ks factory"), range(0, 4), [0, 1, 2, 3, None], (True, False),
    ):
        a = admit(inv, bound, open_count, create)
        rows += 1
        refusals += 0 if a.allowed else 1
        shown = "gh-fail" if open_count is None else str(open_count)
        print(f"{inv:10s}  {bound:5d}  {shown:6s} {str(create):10s} | {str(a.allowed):7s} | {a.reason}")
    print(f"\nrows swept: {rows}; refusals: {refusals}")
    print("claim: a manual invocation is never refused ->",
          "holds" if all(admit('ks factory', b, o, c).allowed
                         for b in range(4) for o in [0, 1, 2, 3, None] for c in (True, False)) else "fails")
    print("claim: the default bound (1) refuses as soon as one kstrl PR is open ->",
          "holds" if not admit('ks serve', 1, 1, True).allowed and admit('ks serve', 1, 0, True).allowed else "fails")
    print("claim: a failed count refuses rather than reading as zero ->",
          "holds" if not admit('ks serve', 1, None, True).allowed else "fails")
    print("claim: every refusal is a wait, never a pause ->",
          "holds" if all(a.wait for a in (admit('ks serve', b, o, True) for b in range(1, 4) for o in [0, 1, 2, 3, None]) if not a.allowed) else "fails")
    print("claim: bound 0 or create_prs=false never calls gh ->",
          "holds" if admit('ks serve', 0, None, True).allowed and admit('ks serve', 1, None, False).allowed else "fails")


if __name__ == "__main__":
    main()
