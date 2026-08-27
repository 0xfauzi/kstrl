#!/usr/bin/env python3
"""Fail when AGENTS.md and its CLAUDE.md sibling have drifted structurally.

Two files describing the same project to two different agents will diverge, and
the divergence is invisible: both files still read fine on their own. What goes
wrong is that one agent is told about a section the other has never heard of.
Comparing HEADING STRUCTURE catches that while ignoring the wording differences
that are deliberate.

Adapted from the version in smartdecks. Two things were changed to make it
shareable: the repo root is the working directory (pre-commit's contract) rather
than the script's parent, and the alias table is loaded from the consuming repo
instead of being hardcoded.

Aliases: put a JSON object in .agent-docs-aliases.json mapping the AGENTS.md
spelling to the CLAUDE.md spelling, for sections deliberately renamed:

    {"Project - AGENTS Runbook": "Project - Claude Code Runbook"}

No AGENTS.md anywhere means nothing to check, and the hook passes silently. That
is what makes it safe to put in the shared set for every repo.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ALIAS_FILE = ".agent-docs-aliases.json"
HEADING = re.compile(r"^(#{1,6})\s+(.*)")


def emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def load_aliases(root: Path) -> dict[str, str]:
    path = root / ALIAS_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        emit(f"{ALIAS_FILE} is not readable JSON; ignoring it.")
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def headings(path: Path, aliases: dict[str, str]) -> list[str]:
    out: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        m = HEADING.match(line)
        if m is None:
            continue
        title = m.group(2).strip()
        for agents_form, claude_form in aliases.items():
            title = title.replace(agents_form, claude_form)
        out.append(f"{'#' * len(m.group(1))} {title}")
    return out


IMPORT_LINE = re.compile(r"^@(\S+)\s*$")


def imports_the_other(claude: Path, agents: Path) -> bool:
    """True when CLAUDE.md is just an import of AGENTS.md.

    Claude Code treats a line of `@path` in CLAUDE.md as an import: the target's
    text is pulled in wholesale. A CLAUDE.md whose only content is `@AGENTS.md`
    therefore has ONE source of truth and cannot drift, which is the best possible
    answer to the problem this hook exists for. Comparing headings would report
    every section as missing, so the pointer is recognised instead of punished.
    """
    lines = [ln.strip() for ln in claude.read_text(errors="replace").splitlines()]
    body = [ln for ln in lines if ln and not ln.startswith("<!--")]
    if not body:
        return False
    targets = []
    for ln in body:
        m = IMPORT_LINE.match(ln)
        if m is None:
            return False
        targets.append(m.group(1))
    return any((claude.parent / t).resolve() == agents.resolve() for t in targets)


def check_pair(
    agents: Path, claude: Path, root: Path, aliases: dict[str, str]
) -> list[str]:
    where = agents.parent.relative_to(root)
    if not claude.exists():
        return [f"{where}: CLAUDE.md is missing, but AGENTS.md is there"]
    if imports_the_other(claude, agents):
        return []
    if claude.is_symlink():
        return [
            (
                f"{where}: CLAUDE.md is a symlink. The two files are meant to say"
                f" different things to different agents, so make it a real adapted"
                f" copy."
            )
        ]
    a = headings(agents, aliases)
    c = headings(claude, aliases)
    if a == c:
        return []
    only_a = set(a) - set(c)
    only_c = set(c) - set(a)
    if not only_a and not only_c:
        return [f"{where}: same sections, different order"]
    return [
        f"{where}: section {h!r} is in AGENTS.md but not CLAUDE.md"
        for h in sorted(only_a)
    ] + [
        f"{where}: section {h!r} is in CLAUDE.md but not AGENTS.md"
        for h in sorted(only_c)
    ]


def tracked_agents_files(root: Path) -> list[Path]:
    """Every AGENTS.md this repo actually owns.

    `rglob` is the obvious way and it is wrong: it descends into .venv and into
    scratch checkouts, and reports drift in a vendored package's own AGENTS.md.
    Measured on two repos here, it produced findings under
    .venv/.../litellm/proxy/_experimental/mcp_server and inside a benchmark
    scratch checkout, neither of which the repo can act on.

    git ls-files is the exact answer to "files this repo owns", and it already
    honours .gitignore. No git, no pairs: this hook must never invent work.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", "*AGENTS.md", "AGENTS.md"],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout.decode()
    except (subprocess.CalledProcessError, FileNotFoundError, UnicodeDecodeError):
        return []
    return sorted({root / name for name in out.split("\0") if name})


def main() -> int:
    root = Path.cwd()
    pairs = [(p, p.parent / "CLAUDE.md") for p in tracked_agents_files(root)]
    if not pairs:
        return 0
    aliases = load_aliases(root)
    problems: list[str] = []
    for agents, claude in pairs:
        problems.extend(check_pair(agents, claude, root, aliases))
    if not problems:
        return 0
    emit("AGENTS.md and CLAUDE.md have drifted apart:")
    for p in problems:
        emit(f"  {p}")
    emit("")
    emit(f"If a section was renamed on purpose, record it in {ALIAS_FILE}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
