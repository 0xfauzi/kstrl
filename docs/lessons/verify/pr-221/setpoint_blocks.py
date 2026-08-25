"""Widget 3 of docs/lessons/pr-221.html: advisory first, and who may tighten.

The rule is issue #224 section 4, which mirrors the existing
``layer0_blocks`` at kstrl/adequacy.py:721-737: a gate blocks when its config
says ``block`` OR the resolved autonomy level is 1 or higher. Autonomy may
tighten a gate and never loosen one. Level 0 means the ladder is off.

Sweeps mode x level and prints what happens to the PRD flag in each case.
"""

from __future__ import annotations


def blocks(mode: str, autonomy_level: int) -> bool:
    if mode == "block":
        return True
    return autonomy_level >= 1


def effect(mode: str, level: int) -> str:
    if blocks(mode, level):
        return "BLOCKS: passes reverted to false, notes line appended, component retries"
    return "records only: finding on the PR body and in the journal, component proceeds"


def main() -> None:
    print("mode      level | blocks | what happens to a disagreeing story")
    for mode in ("advisory", "block"):
        for level in range(0, 5):
            print(f"{mode:9s} L{level}    | {str(blocks(mode, level)):5s}  | {effect(mode, level)}")
    print()
    expected = {("advisory", 0): False, ("block", 0): True, ("advisory", 1): True, ("advisory", 2): True}
    ok = all(blocks(m, lv) == want for (m, lv), want in expected.items())
    print("issue #224 test 10 truth table ->", "holds" if ok else "fails")
    print("claim: with the default config and the ladder off, nothing blocks ->",
          "holds" if not blocks("advisory", 0) else "fails")
    print("claim: no level ever loosens a gate the config set to block ->",
          "holds" if all(blocks("block", lv) for lv in range(5)) else "fails")
    print("claim: the verdict moves exactly once across the level range in advisory mode ->",
          "holds" if [blocks("advisory", lv) for lv in range(5)] == [False, True, True, True, True] else "fails")


if __name__ == "__main__":
    main()
