"""#508 (slice 4 of #217): the fact-necessary learning fixture and its scorer.

Every scorer test drives the real entry point, ``python -m
kstrl.learning_fixture``, in a subprocess, against a worktree built from the
fixture's own seed repository, and asserts on the exit code and the JSON
report. Nothing here calls a model or runs a factory: the paid run that uses
the fixture (P-A in #217) is documented in the fixture's README and is not
run by this suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "learning_fixtures" / "fact_necessary"
SEED = FIXTURE / "repo"
HIDDEN_CHECK = FIXTURE / "hidden_check.py"
MARKER = "LL-417"
TIMEOUT = 120

#: A slice-2 module that follows the convention slice 1's spec states.
FOLLOWS = """
SUFFIX = " [LL-417]"


def parse_amount(text: str) -> int:
    if not text:
        raise ValueError(f"amount is empty{SUFFIX}")
    whole, dot, cents = text.partition(".")
    if not whole.isdigit() or (dot and not cents.isdigit()):
        raise ValueError(f"amount {text!r} has a character other than digits and one '.'{SUFFIX}")
    if len(cents) > 2:
        raise ValueError(f"amount {text!r} has more than two digits after the '.'{SUFFIX}")
    return int(whole) * 100 + int(cents.ljust(2, "0"))
"""

#: The same module with the convention's suffix left off every message.
BREAKS = FOLLOWS.replace('SUFFIX = " [LL-417]"', 'SUFFIX = ""')

#: A delivered prompt whose knowledge prefix carries the fact in the sibling
#: tier, in the exact line shape ``knowledge._format_section`` renders.
PROMPT_WITH_FACT = """## Component Knowledge
Durable facts captured from prior successful iterations.

### Other components (summary)
- **accounts**[module] {high}: Every error message ends with [LL-417]. (evidence: a.py:1)

# kstrl Agent Instructions
Build the component.
"""

#: The marker is in the prompt, but only in codebase context, never in a
#: knowledge claim. The scorer must not read this as the fact being delivered.
PROMPT_MARKER_OUTSIDE_A_CLAIM = """=== CODEBASE CONTEXT (auto-generated) ===
src/ledgerlite/accounts.py: SUFFIX = " [LL-417]"
=== END CODEBASE CONTEXT ===

## Component Knowledge
Durable facts captured from prior successful iterations.

### Other components (summary)
- **accounts**[module] {high}: Balances are integer cents. (evidence: a.py:1)
"""


def _worktree(tmp_path: Path, amounts_source: str | None) -> Path:
    """A copy of the seed repository, with slice 2's module written in."""
    wt = tmp_path / "wt"
    shutil.copytree(SEED, wt)
    if amounts_source is not None:
        (wt / "src" / "ledgerlite" / "amounts.py").write_text(amounts_source, encoding="utf-8")
    return wt


def _score(*args: str | Path, check: Path = HIDDEN_CHECK) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "-m",
        "kstrl.learning_fixture",
        "--check",
        str(check),
        "--marker",
        MARKER,
        *(str(a) for a in args),
    ]
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=TIMEOUT, cwd=str(REPO_ROOT), check=False
    )


def _report(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    report = json.loads(proc.stdout)
    assert isinstance(report, dict)
    return report


def test_scorer_passes_a_worktree_that_follows_the_convention(tmp_path: Path) -> None:
    proc = _score("--worktree", _worktree(tmp_path, FOLLOWS))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _report(proc)["hidden_check"]["passed"] is True


def test_scorer_fails_a_worktree_that_breaks_the_convention(tmp_path: Path) -> None:
    proc = _score("--worktree", _worktree(tmp_path, BREAKS))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    check = _report(proc)["hidden_check"]
    assert check["passed"] is False
    assert "does not end with" in check["reason"]


def test_scorer_fails_a_worktree_without_the_slice_2_module(tmp_path: Path) -> None:
    proc = _score("--worktree", _worktree(tmp_path, None))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    check = _report(proc)["hidden_check"]
    assert check["passed"] is False
    assert "cannot import" in check["reason"]


def test_relative_paths_are_resolved_against_the_callers_directory(tmp_path: Path) -> None:
    """The check runs with the worktree as its working directory, so a
    relative ``--check`` must be resolved before it is handed over."""
    relative_check = HIDDEN_CHECK.relative_to(REPO_ROOT)

    proc = _score("--worktree", _worktree(tmp_path, FOLLOWS), check=relative_check)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _report(proc)["hidden_check"]["passed"] is True


def test_fact_in_prompt_is_read_from_the_delivered_knowledge_claims(tmp_path: Path) -> None:
    wt = _worktree(tmp_path, FOLLOWS)
    with_fact = tmp_path / "with_fact.txt"
    with_fact.write_text(PROMPT_WITH_FACT, encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text(PROMPT_MARKER_OUTSIDE_A_CLAIM, encoding="utf-8")

    delivered = _report(_score("--worktree", wt, "--prompt", with_fact))
    not_delivered = _report(_score("--worktree", wt, "--prompt", outside))

    assert delivered["fact_in_prompt"] is True
    assert [c["tier"] for c in delivered["claims_with_marker"]] == ["sibling"]
    assert not_delivered["fact_in_prompt"] is False
    assert not_delivered["claims_with_marker"] == []


def test_absent_prompt_and_journal_are_unmeasured_not_false(tmp_path: Path) -> None:
    report = _report(_score("--worktree", _worktree(tmp_path, FOLLOWS)))

    assert report["fact_in_prompt"] is None
    assert report["claims_with_marker"] is None
    assert report["utilization"] is None
    assert sorted(report["not_measured"]) == ["fact_in_prompt", "utilization"]


def test_utilization_is_read_from_the_journal_rows(tmp_path: Path) -> None:
    wt = _worktree(tmp_path, FOLLOWS)
    counts = {"measured": True, "injected": 3, "referenced": 0, "reason": ""}
    journal = tmp_path / "evolution.jsonl"
    rows = [
        {"run_id": "run-2", "component_id": "amounts", "knowledge_utilization": counts},
        {"run_id": "run-2", "component_id": "amounts", "event_type": "role_usage"},
    ]
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    report = _report(_score("--worktree", wt, "--journal", journal))

    assert report["utilization"] == {"run-2": {"amounts": counts}}
    assert "utilization" not in report["not_measured"]


def test_a_hidden_check_that_gives_no_verdict_is_refused(tmp_path: Path) -> None:
    silent = tmp_path / "silent_check.py"
    silent.write_text("print('nothing to say')\n", encoding="utf-8")

    proc = _score("--worktree", _worktree(tmp_path, FOLLOWS), check=silent)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "no verdict" in proc.stderr


def test_a_verdict_that_disagrees_with_the_exit_code_is_refused(tmp_path: Path) -> None:
    """A check that printed pass and then exited 1 did not finish cleanly;
    its pass line is not a measurement."""
    torn = tmp_path / "torn_check.py"
    torn.write_text(
        "import sys\nprint('HIDDEN-CHECK-VERDICT: pass printed early')\nsys.exit(1)\n",
        encoding="utf-8",
    )

    proc = _score("--worktree", _worktree(tmp_path, FOLLOWS), check=torn)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "disagrees" in proc.stderr


def test_a_named_journal_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    proc = _score(
        "--worktree", _worktree(tmp_path, FOLLOWS), "--journal", tmp_path / "missing.jsonl"
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "missing.jsonl" in proc.stderr


def test_a_journal_line_that_does_not_parse_is_refused(tmp_path: Path) -> None:
    """A skipped row would read as a component that recorded no utilization,
    so a journal line the scorer cannot parse is refused, not skipped."""
    counts = {"measured": True, "injected": 3, "referenced": 0, "reason": ""}
    journal = tmp_path / "evolution.jsonl"
    journal.write_text(
        json.dumps({"run_id": "run-2", "component_id": "amounts", "knowledge_utilization": counts})
        + '\n{"run_id": "run-2", "component_id": "acc\n',
        encoding="utf-8",
    )

    proc = _score("--worktree", _worktree(tmp_path, FOLLOWS), "--journal", journal)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "line 2" in proc.stderr
    assert proc.stdout == ""


def test_a_named_prompt_that_cannot_be_read_is_refused(tmp_path: Path) -> None:
    proc = _score("--worktree", _worktree(tmp_path, FOLLOWS), "--prompt", tmp_path / "missing.txt")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "missing.txt" in proc.stderr


def test_only_slice_1_states_the_convention() -> None:
    """The fixture's premise: the fact is stated once, in slice 1's spec."""
    assert MARKER in (FIXTURE / "slices" / "slice-1.md").read_text(encoding="utf-8")
    assert MARKER not in (FIXTURE / "slices" / "slice-2.md").read_text(encoding="utf-8")
    # Tool caches (a local `.ruff_cache/`, `__pycache__/`) are ignored by git
    # and are not part of the seed.
    seed_files = [
        p
        for p in SEED.rglob("*")
        if p.is_file()
        and not any(
            part.startswith(".") or part == "__pycache__" for part in p.relative_to(SEED).parts
        )
    ]
    assert seed_files
    assert [p for p in seed_files if MARKER in p.read_text(encoding="utf-8")] == []
    assert MARKER in HIDDEN_CHECK.read_text(encoding="utf-8")
    assert MARKER in (FIXTURE / "README.md").read_text(encoding="utf-8")


def test_seed_repository_tests_pass(tmp_path: Path) -> None:
    """P-A starts from a green base: the seed's own suite passes as shipped."""
    wt = _worktree(tmp_path, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        cwd=str(wt),
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_seed_repository_is_not_collected_by_our_own_suite() -> None:
    """Without the fixture's conftest the seed's tests are imported by this
    suite and collection dies on ModuleNotFoundError. 5 is pytest's
    EXIT_NOTESTSCOLLECTED; a collection error exits 2."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/learning_fixtures",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert proc.returncode == 5, proc.stdout + proc.stderr


#: A slice-2 module that validates nothing. No call raises, so no message can
#: carry the suffix, and the check must not read that as following the convention.
VALIDATES_NOTHING = """
def parse_amount(text: str) -> int:
    return 0
"""


def test_scorer_fails_a_worktree_whose_parse_amount_raises_nothing(tmp_path: Path) -> None:
    proc = _score("--worktree", _worktree(tmp_path, VALIDATES_NOTHING))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    check = _report(proc)["hidden_check"]
    assert check["passed"] is False
    assert "raised nothing" in check["reason"]
