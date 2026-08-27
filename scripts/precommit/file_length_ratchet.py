#!/usr/bin/env python3
"""Stop a file GROWING TO more than 800 lines.

WHAT IT FAILS ON, AND WHY NOT MORE
----------------------------------
Two cases fail: a file this commit adds that is already over the limit, and a
file that was at or under the limit and is now over it. Those are the moments a
file becomes a problem.

A file that is ALREADY over the limit and grows is reported, not failed. That is
a deliberate difference from the complexity gates, and the reason is that length
is a property of the whole file while complexity is a property of one function.
You can leave a monstrous function alone and edit its neighbour, so freezing it
costs nothing. You cannot add a line to a 5994-line file without touching the
file, so failing on growth would block every edit to it until somebody does a
large refactor they did not come here to do. Measured on these repos: 135 Python
files are already over 800 lines, 77 of them source. Freezing all 135 is how a
hook gets --no-verify'd.

If you want the stricter rule, set FREEZE_OVERSIZE = True below. Nothing else
changes.

WHY 800 AND WHY IT COUNTS LINES
-------------------------------
800 is the number the owner asked for; it is not derived from measurement here.
Lines, not bytes: `check-added-large-files` already caps bytes, and it only looks
at files being ADDED, so a file that grows past a megabyte one commit at a time
is invisible to it. Neither hook covers what this one does.

Python only, via `types: [python]` in the hook config. A generic length rule
fires on lock files, JSON fixtures and generated output, where length is not a
defect. Widen it there if you want more.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MAX_LINES = 800
FREEZE_OVERSIZE = False


def emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")


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
        # No repo, or no commits yet: nothing to compare against, so nothing to
        # report. Passing is the honest answer.
        return None
    return Path(top)


def line_count_at(repo: Path, ref: str, rel: str) -> int | None:
    """Lines in `rel` at a git ref, or None when the file is not there."""
    spec = f"{ref}:{rel}" if ref else f":{rel}"
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", spec], capture_output=True, check=False
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.count(b"\n")


def classify(repo: Path, rel: str) -> tuple[str | None, str | None]:
    """Return (blocking failure, informational note) for one file."""
    now = line_count_at(repo, "", rel)
    if now is None or now <= MAX_LINES:
        return None, None
    was = line_count_at(repo, "HEAD", rel)
    if was is None:
        return f"  ADDED    {rel}  {now} lines  (limit {MAX_LINES})", None
    if was <= MAX_LINES:
        return f"  CROSSED  {rel}  {was} -> {now} lines  (limit {MAX_LINES})", None
    if now > was:
        line = f"  {rel}  {was} -> {now} lines, already over the limit"
        return (f"  GREW    {line}", None) if FREEZE_OVERSIZE else (None, line)
    return None, None


def survey(repo: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    """Split every path into blocking failures and informational notes."""
    failures: list[str] = []
    notes: list[str] = []
    for name in paths:
        try:
            rel = str(Path(name).resolve().relative_to(repo))
        except ValueError:
            # A path outside this repo. Nothing here to compare it against.
            continue
        failure, note = classify(repo, rel)
        if failure:
            failures.append(failure)
        if note:
            notes.append(note)
    return failures, notes


def main(argv: list[str]) -> int:
    paths = [p for p in argv if p.endswith(".py")]
    if not paths:
        return 0

    repo = find_repo()
    if repo is None:
        return 0

    failures, notes = survey(repo, paths)

    if not failures:
        for note in notes:
            emit(note)
        return 0

    emit(f"File length: this commit takes a file past {MAX_LINES} lines.")
    emit("")
    for failure in failures:
        emit(failure)
    emit("")
    emit("Split it. A file this long has more than one job in it, and the split")
    emit("is cheapest now, while you still remember what the new part does.")
    emit("")
    emit("Files already over the limit are grandfathered and are not failed for")
    emit("growing; set FREEZE_OVERSIZE = True in this script to change that.")
    for note in notes:
        emit(note)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
