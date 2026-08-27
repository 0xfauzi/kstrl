#!/usr/bin/env python3
"""Ratchet cyclomatic complexity: fail only on what this commit makes worse.

WHY THIS EXISTS AND WHY IT IS NOT JUST A RUFF SETTING
-----------------------------------------------------
Ruff already computes cyclomatic complexity (rule C901). What it cannot do is
compare a function against its own previous value, and a flat C901 threshold at
the level where the metric carries information (10) reports 264 existing
functions across these repos. A gate that fails 264 times on day one gets
switched off. The alternative, baselining each one with `# noqa: C901`,
suppresses that function forever: it stays quiet when the same function later
goes from 20 to 40, which is the case most worth catching.

So this does for cyclomatic complexity what `complexipy --staged` does for
cognitive complexity. It reads each function's C901 value in the git INDEX and
in HEAD, matches them by name, and fails only when this commit adds a function
above the gate or pushes an existing one past it. Existing debt is grandfathered
by construction: no baseline file, no suppression comments.

WHY NOT diff-quality
--------------------
`diff-quality --violations=ruff.check` is the obvious off-the-shelf answer and
it is wrong for this rule. It reports violations whose LINE falls inside the
diff, and a C901 violation is anchored to the `def` line. Measured on a probe
repo: a function taken from cyclomatic 9 to 15 by adding branches to its body,
never touching the `def` line, was reported by diff-quality as
"Violations: 0 lines, % Quality: 100%". It silently passes function-level
metrics. This script catches that case.

WHY 10 AND NOT 15
-----------------
Cyclomatic and cognitive complexity mostly agree, so a high cyclomatic gate is
redundant with the cognitive one already running alongside it. Measured over
1814 paired deckgen functions, against a cognitive gate of 15: a cyclomatic gate
at 20 catches ZERO functions the cognitive gate misses, at 15 it catches 4, at
10 it catches 64. Those 64 are the shape cognitive complexity deliberately
forgives, many branches with little nesting, e.g. cyclomatic 20 / cognitive 13.
That is a long if/elif chain or a flat dispatch table. It reads easily and still
needs 20 tests for branch coverage, which is what cyclomatic complexity is
actually good for. 10 is also McCabe's own 1976 recommendation.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Pinned so the number this gate produces is reproducible. Ruff's mccabe is not
# interchangeable with other tools': measured on this tree, ruff and lizard agree
# on only 47.9% of the same functions, because lizard counts `and`/`or` as
# decision points and ruff does not. A cyclomatic threshold means nothing without
# naming the tool that produced it.
RUFF = ["uvx", "ruff@0.16.4"]

# Sentinel for "the file as it sits on disk", as opposed to a git ref or the index.
# The commit gate compares the INDEX against HEAD, because the index is what the
# commit will contain. The edit-time advisory compares the WORKING TREE against
# HEAD, because an agent's edit is not staged yet and would otherwise read as no
# change at all.
WORKTREE = "\x00worktree"
MAX_COMPLEXITY = 10

# max-complexity = 0 makes ruff report EVERY function with its value, which turns
# a pass/fail rule into the full census this needs.
CENSUS = [
    "--isolated",
    "--select",
    "C901",
    "--config",
    "lint.mccabe.max-complexity = 0",
    "--output-format",
    "concise",
    "--no-cache",
]

REPORT = re.compile(
    r"^(?P<path>.*?):\d+:\d+: C901 `(?P<name>.+?)` "
    r"is too complex \((?P<value>\d+) > 0\)$"
)


def emit(text: str = "") -> None:
    """Write one line to stdout.

    Not `print`. Ruff's T201 (flake8-print) is selected in several of the repos
    this is dropped into, and deckgen alone carries about 40 per-file-ignore
    entries to work around it. Going through sys.stdout means this file lints
    clean anywhere without a config entry having to travel with it.
    """
    sys.stdout.write(text + "\n")


def census(root: Path) -> dict[tuple[str, str], int]:
    """Map (relative path, function name) -> cyclomatic complexity for a tree."""
    proc = subprocess.run(
        [*RUFF, "check", ".", *CENSUS],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    found: dict[tuple[str, str], list[int]] = {}
    for raw in proc.stdout.splitlines():
        match = REPORT.match(raw.strip())
        if match is None:
            continue
        key = (match.group("path").lstrip("./"), match.group("name"))
        found.setdefault(key, []).append(int(match.group("value")))
    # One file can define two functions with the same name: a method on two
    # classes, or a redefinition under `if TYPE_CHECKING`. Taking the worst value
    # per name is the conservative read - it never lets a regression hide behind
    # a namesake.
    return {key: max(values) for key, values in found.items()}


def materialise(paths: list[str], repo: Path, ref: str, dest: Path) -> None:
    """Write each path's content at `ref` into dest, preserving layout.

    `ref` of "" means the git index; WORKTREE means the file as it sits on disk.
    A path absent at `ref` is one this commit adds; it is simply not written, so
    every function in it reads as new, which is correct.
    """
    for rel in paths:
        out = dest / rel
        if ref == WORKTREE:
            source = repo / rel
            if not source.is_file():
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(source.read_bytes())
            continue
        spec = f"{ref}:{rel}" if ref else f":{rel}"
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", spec],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(proc.stdout)


def find_repo() -> Path | None:
    """The repo whose index we compare against, or None if there is nothing to do."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", top, "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        # No repo, or no commits yet. There is nothing to compare against, so
        # there is no regression to report. Passing is the honest answer.
        return None
    return Path(top)


def classify(
    before: dict[tuple[str, str], int],
    after: dict[tuple[str, str], int],
) -> tuple[list[str], list[str]]:
    """Split each changed function into blocking failures and informational notes."""
    failures: list[str] = []
    notes: list[str] = []
    limit = MAX_COMPLEXITY
    for (path, name), now in sorted(after.items()):
        was = before.get((path, name))
        where = f"{path}::{name}"
        if was is None:
            if now > limit:
                failures.append(
                    f"  NEW        {where}  cyclomatic {now}  (limit {limit})"
                )
        elif now > was:
            if now > limit:
                failures.append(
                    f"  REGRESSED  {where}  cyclomatic {was} -> {now}  (limit {limit})"
                )
            else:
                notes.append(
                    f"  rose       {where}  cyclomatic {was} -> {now} (under limit)"
                )
        elif now < was:
            notes.append(f"  IMPROVED   {where}  cyclomatic {was} -> {now}")
    return failures, notes


def main(argv: list[str]) -> int:
    compare_from = WORKTREE if "--worktree" in argv else ""
    paths = [p for p in argv if p.endswith(".py")]
    if not paths:
        return 0

    repo = find_repo()
    if repo is None:
        return 0

    try:
        rel = [str(Path(p).resolve().relative_to(repo)) for p in paths]
    except ValueError:
        # A path outside this repo. Nothing to compare it against here.
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        head = Path(tmp) / "head"
        index = Path(tmp) / "index"
        head.mkdir()
        index.mkdir()
        materialise(rel, repo, "HEAD", head)
        materialise(rel, repo, compare_from, index)
        failures, notes = classify(census(head), census(index))

    if not failures:
        for note in notes:
            emit(note)
        return 0

    emit("Cyclomatic complexity ratchet: this commit adds or worsens branching")
    emit("past the limit.")
    emit()
    for failure in failures:
        emit(failure)
    emit()
    emit("Cyclomatic complexity counts independent paths through a function, so")
    emit("the number above is roughly how many tests it needs for branch")
    emit(f"coverage. Functions already over {MAX_COMPLEXITY} are grandfathered; this")
    emit("only refuses to let the number grow.")
    emit()
    emit("Split the function, or replace a long if/elif chain with a lookup")
    emit("table. If it is genuinely irreducible, `# noqa: C901` on the `def` line")
    emit("is the escape hatch, and it shows up in review as the deliberate")
    emit("choice it is.")
    for note in notes:
        emit(note)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
