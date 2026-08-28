"""Widget 7 of docs/lessons/pr-221.html: which ks command can measure a tree.

The widget's data is the command list ``uv run ks --help`` prints at merge
commit 72404e9 (fifteen commands; the design doc says fourteen), plus a flag
per command saying whether that command reaches the mechanical sensors, and
whether it does so without a factory invocation. A second state adds the
``sense`` command PR #237 (R10.1, open at the time of writing) introduces.

When run from inside the repository this script re-derives the standalone
flag by grepping kstrl/ for callers of ``run_mechanical_verification``;
otherwise it prints the recorded data. ``--json`` emits both states.
"""

from __future__ import annotations

import json
import re
import sys
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
AFTER_237: list[tuple[str, bool, bool]] = COMMANDS + [("sense", True, True)]


def classify(reaches: bool, standalone: bool) -> str:
    if standalone:
        return "direct: runs the sensors against a tree, no factory run"
    if reaches:
        return "through a factory run: needs a PRD, a branch, a worktree and agent spend"
    return "does not reach the sensors"


def sweep() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for state, table in (("main", COMMANDS), ("after_237", AFTER_237)):
        for cmd, reaches, standalone in table:
            rows.append(
                {
                    "state": state,
                    "command": cmd,
                    "reaches": reaches,
                    "standalone": standalone,
                    "verdict": classify(reaches, standalone),
                }
            )
    return rows


def live_callers(repo: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted((repo / "kstrl").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "run_mechanical_verification(" in line and not re.match(r"\s*def ", line):
                hits.append(f"{path.relative_to(repo)}:{i}")
    return hits


def main() -> None:
    rows = sweep()
    if "--json" in sys.argv:
        json.dump(rows, sys.stdout)
        return
    for state, table in (("main", COMMANDS), ("after_237", AFTER_237)):
        reach = [c for c, r, _ in table if r]
        direct = [c for c, _, s in table if s]
        print(
            f"{state}: {len(table)} commands; {len(reach)} reach the sensors through a "
            f"factory run {reach}; {len(direct)} run them directly {direct}"
        )
    repo = Path(__file__).resolve().parents[4]
    if (repo / "kstrl").is_dir():
        hits = live_callers(repo)
        print(f"live grep, call sites of run_mechanical_verification( under kstrl/: {hits}")
        in_cli = [h for h in hits if h.startswith("kstrl/cli.py")]
        print(
            "claim: on this tree no command calls the sensor directly ->",
            "holds" if not in_cli else f"fails ({in_cli})",
        )
    print(
        "claim: the design doc's count (fourteen) matches the help output ->",
        "holds" if len(COMMANDS) == 14 else f"fails (measured {len(COMMANDS)})",
    )
    print(
        "claim: zero commands run a sensor standalone on main; one after PR #237 ->",
        "holds"
        if sum(1 for _, _, s in COMMANDS if s) == 0 and sum(1 for _, _, s in AFTER_237 if s) == 1
        else "fails",
    )


if __name__ == "__main__":
    main()
