"""Score a worktree against a learning fixture's hidden check (#508, slice 4 of #217).

``python -m kstrl.learning_fixture --check CHECK --marker TEXT --worktree DIR
[--prompt FILE] [--journal FILE]`` prints one JSON report with three readings:

- ``hidden_check``: the fixture's hidden check, run against ``--worktree`` in a
  subprocess. The check prints ``HIDDEN-CHECK-VERDICT: pass|fail <reason>`` and
  exits 0 or 1; the last verdict line is read, and a run with no verdict, or
  whose verdict and exit code disagree, is refused rather than scored.
- ``fact_in_prompt``: whether a knowledge claim in ``--prompt`` (the text the
  engineer was given) contains ``--marker``. Only claim lines count, parsed by
  the same function the utilization metric uses, so the marker appearing in
  codebase context or in a spec does not read as the fact being delivered.
- ``utilization``: the ``knowledge_utilization`` each journal row in
  ``--journal`` recorded, by run id and component id, exactly as recorded.

A reading whose input was not given is ``null`` and is named in
``not_measured``; it is never reported as false or zero. Exit codes: 0 when
the hidden check passed, 1 when it failed, 2 when a named input could not be
read or the check gave no usable verdict.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.jsonread import read_json
from kstrl.knowledge import _extract_prefix_claims
from kstrl.verify import ChildOutputDecodeError, run_scrubbed

VERDICT_PREFIX = "HIDDEN-CHECK-VERDICT:"
CHECK_TIMEOUT_SECONDS = 120.0
_EXIT_FOR_VERDICT = {"pass": 0, "fail": 1}
EXIT_UNMEASURED = 2


class ScoreError(Exception):
    """A named input could not be read, or the hidden check gave no usable verdict."""


@dataclass(frozen=True)
class CheckVerdict:
    passed: bool
    reason: str


def run_hidden_check(
    check: Path, worktree: Path, *, timeout: float = CHECK_TIMEOUT_SECONDS
) -> CheckVerdict:
    """Run ``check`` against ``worktree`` and return its verdict.

    Both paths are resolved against the caller's directory first, because the
    check runs with the worktree as its working directory.
    """
    check, worktree = check.resolve(), worktree.resolve()
    if not worktree.is_dir():
        raise ScoreError(f"worktree {worktree} is not a directory")
    try:
        proc = run_scrubbed(
            [sys.executable, "-B", str(check), str(worktree)], cwd=worktree, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise ScoreError(f"hidden check {check} timed out after {timeout}s") from exc
    except (OSError, ChildOutputDecodeError) as exc:
        raise ScoreError(f"hidden check {check} could not run: {exc}") from exc
    verdicts = [line for line in proc.stdout.splitlines() if line.startswith(VERDICT_PREFIX)]
    if not verdicts:
        raise ScoreError(
            f"hidden check {check} printed no verdict line (exit {proc.returncode}); "
            f"stderr: {proc.stderr[-2000:]}"
        )
    word, _, reason = verdicts[-1][len(VERDICT_PREFIX) :].strip().partition(" ")
    if _EXIT_FOR_VERDICT.get(word) != proc.returncode:
        raise ScoreError(
            f"hidden check {check} verdict {verdicts[-1]!r} disagrees with its exit code "
            f"{proc.returncode}"
        )
    return CheckVerdict(passed=word == "pass", reason=reason)


def claims_with_marker(prompt_text: str, marker: str) -> list[dict[str, str]]:
    """The knowledge claims in ``prompt_text`` that contain ``marker``, with their tier."""
    return [
        {"tier": tier, "claim": claim}
        for claim, tier in _extract_prefix_claims(prompt_text)
        if marker in claim
    ]


def utilization_by_run(journal: Path) -> dict[str, dict[str, Any]]:
    """``{run_id: {component_id: knowledge_utilization}}`` for every row that recorded one.

    A line that does not parse is refused, not skipped: a skipped row would
    read as a component that recorded no utilization.
    """
    by_run: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(_read_input("journal", journal).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = read_json(line)
        except ValueError as exc:
            raise ScoreError(f"journal {journal} line {number} is not JSON: {exc}") from exc
        if not isinstance(row, dict) or "knowledge_utilization" not in row:
            continue
        run = by_run.setdefault(str(row.get("run_id", "")), {})
        run[str(row.get("component_id", ""))] = row["knowledge_utilization"]
    return by_run


def _read_input(what: str, path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise ScoreError(f"{what} {path} could not be read: {exc}") from exc


def score(
    check: Path, worktree: Path, marker: str, prompt: Path | None, journal: Path | None
) -> dict[str, Any]:
    """Every reading, as one report. Raises :class:`ScoreError` on an unreadable input."""
    report: dict[str, Any] = {
        "fact_in_prompt": None,
        "claims_with_marker": None,
        "utilization": None,
        "not_measured": [],
    }
    if prompt is None:
        report["not_measured"].append("fact_in_prompt")
    else:
        claims = claims_with_marker(_read_input("prompt", prompt), marker)
        report["claims_with_marker"] = claims
        report["fact_in_prompt"] = bool(claims)
    if journal is None:
        report["not_measured"].append("utilization")
    else:
        report["utilization"] = utilization_by_run(journal)
    verdict = run_hidden_check(check, worktree)
    report["hidden_check"] = {"passed": verdict.passed, "reason": verdict.reason}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kstrl.learning_fixture")
    parser.add_argument("--check", type=Path, required=True, help="the fixture's hidden check")
    parser.add_argument("--worktree", type=Path, required=True, help="tree holding the code")
    parser.add_argument("--marker", required=True, help="text that identifies the fact")
    parser.add_argument("--prompt", type=Path, help="the prompt text the engineer was given")
    parser.add_argument("--journal", type=Path, help="the project's evolution journal")
    args = parser.parse_args(argv)
    try:
        report = score(args.check, args.worktree, args.marker, args.prompt, args.journal)
    except ScoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNMEASURED
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["hidden_check"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
