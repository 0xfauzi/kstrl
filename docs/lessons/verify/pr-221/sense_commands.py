"""Widget 4 of docs/lessons/pr-221.html: which ks command can measure a tree.

The widget's data is the command list ``uv run ks --help`` printed at merge
commit 72404e9, plus a flag per command saying whether that command reaches the
mechanical sensors, and whether it does so without a factory invocation.

When run from inside the repository this script re-derives the second flag by
grepping kstrl/ for callers of ``run_mechanical_verification``; otherwise it
prints the recorded data. The design doc says fourteen commands; the help
output lists fifteen. The claim that matters is the zero.
"""

from __future__ import annotations

import re
from pathlib import Path

# (command, reaches the mechanical sensors at all, standalone against a tree)
COMMANDS: list[tuple[str, bool, bool]] = [
    ("autonomy", False, False),
    ("config", False, False),
    ("dash", False, False),
    ("decompose", False, False),
    ("evolve", False, False),
    ("factory", True, False),
    ("feature", True, False),
    ("inbox", False, False),
    ("init", False, False),
    ("queue", False, False),
    ("retry", True, False),
    ("run", True, False),
    ("serve", True, False),
    ("status", False, False),
    ("understand", False, False),
]


def live_callers(repo: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted((repo / "kstrl").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "run_mechanical_verification(" in line and not re.match(r"\s*def ", line):
                hits.append(f"{path.relative_to(repo)}:{i}")
    return hits


def main() -> None:
    total = len(COMMANDS)
    reach = [c for c, r, _ in COMMANDS if r]
    standalone = [c for c, _, s in COMMANDS if s]
    print(f"commands listed by ks --help at 72404e9: {total}")
    print(f"commands that reach the mechanical sensors through a factory invocation: {len(reach)} {reach}")
    print(f"commands that run the sensors standalone against a tree: {len(standalone)} {standalone}")
    repo = Path(__file__).resolve().parents[4]
    if (repo / "kstrl").is_dir():
        hits = live_callers(repo)
        print(f"live grep, call sites of run_mechanical_verification( under kstrl/: {hits}")
        in_cli = [h for h in hits if h.startswith("kstrl/cli.py")]
        print(f"of those, inside kstrl/cli.py: {len(in_cli)}")
        print("claim: no command calls the sensor directly ->", "holds" if not in_cli else "fails")
    print("claim: the design doc's count (fourteen) matches the help output ->",
          "holds" if total == 14 else f"fails (measured {total})")
    print("claim: zero commands run a sensor without a factory run ->",
          "holds" if not standalone else "fails")


if __name__ == "__main__":
    main()
