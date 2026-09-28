"""#622: output no registered parser reads reaches the engineer as its failure lines.

Before #622 a gate whose output matched none of the six registered parsers
showed the engineer the last 5 lines (test gate) or the last 3 (typecheck and
lint). For ``cargo test`` those lines are cargo's own chatter, and the panic
location and the ``left:`` / ``right:`` values sit earlier and were dropped.

Each test builds a real git repository whose ``[verify]`` command replays a
capture of a real tool run from ``tests/tool_output`` and exits with that
tool's status, then drives ``ks check --json`` through ``CliRunner`` and reads
the failing row's ``details``: the retry detail the engineer is shown. No test
needs cargo, go or node installed.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers import gitrepo
from tests.helpers.tool_output import TOOL_OUTPUT_DIR
from tests.spine_utils import git

_OK = f"{sys.executable} -c 'print(1)'"


def _replay(capture: str, exit_code: int) -> str:
    """A shell command that prints a captured tool run and exits as the tool did."""
    return f"cat {shlex.quote(str(TOOL_OUTPUT_DIR / capture))}; exit {exit_code}"


def _repo(tmp_path: Path, *, test_command: str = _OK, lint_command: str = _OK) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    (root / "kstrl.toml").write_text(
        "[verify]\n"
        f"test_command = {json.dumps(test_command)}\n"
        f"typecheck_command = {json.dumps(_OK)}\n"
        f"lint_command = {json.dumps(lint_command)}\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root


def _failing_details(root: Path, gate: str) -> list[str]:
    """The ``details`` of the one failing ``gate`` row ``ks check --json`` reports."""
    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--json"])
    assert result.exit_code == 1, result.output
    rows = [row for row in json.loads(result.stdout)["checks"] if row["name"] == gate]
    assert len(rows) == 1, rows
    assert rows[0]["passed"] is False
    details: list[str] = rows[0]["details"]
    return details


def test_an_unparsed_cargo_failure_shows_the_panic_location_and_values(tmp_path: Path) -> None:
    root = _repo(tmp_path, test_command=_replay("cargo-1.94.0-test-fail.txt", 101))

    shown = "\n".join(_failing_details(root, "test_suite"))

    assert "panicked at src/pricing.rs:18:9:" in shown
    assert "assertion `left == right` failed" in shown
    assert "left: 11" in shown
    assert "right: 10" in shown


def test_an_unparsed_go_failure_shows_the_file_and_line(tmp_path: Path) -> None:
    # Four packages, the failing one second: go prints the passing
    # packages after it, which is what pushed the failure out of the tail.
    root = _repo(tmp_path, test_command=_replay("go-1.21.6-test-fail.txt", 1))

    shown = "\n".join(_failing_details(root, "test_suite"))

    assert "--- FAIL: TestBulkPercent" in shown
    assert "pricing_test.go:7: BulkPercent(20) = 11, want 10" in shown


def test_an_unparsed_jest_failure_shows_the_assertion_and_location(tmp_path: Path) -> None:
    # jest prints the test name, the values and the code frame ABOVE its
    # stack line, so a window after the location alone would miss them.
    root = _repo(tmp_path, test_command=_replay("jest-29.7.0-fail.txt", 1))

    shown = "\n".join(_failing_details(root, "test_suite"))

    assert "● twenty items is ten percent" in shown
    assert "Expected: 10" in shown
    assert "Received: 11" in shown
    assert "at Object.toBe (src/bulk.test.js:4:27)" in shown


def test_an_unparsed_lint_failure_shows_every_location(tmp_path: Path) -> None:
    # The lint gate's tail was 3 lines: clippy's last three are a
    # suggestion frame and `could not compile`.
    root = _repo(tmp_path, lint_command=_replay("clippy-0.1.94-fail.txt", 101))

    shown = "\n".join(_failing_details(root, "linter"))

    assert "error: unneeded `return` statement" in shown
    assert "--> src/pricing.rs:9:5" in shown
    assert "--> src/pricing.rs:15:5" in shown


def test_paths_outside_the_worktree_are_not_shown(tmp_path: Path) -> None:
    # RUST_BACKTRACE=1: the frames inside the project are `./src/...`, the
    # rest are the toolchain's own sources under /rustc and ~/.rustup.
    root = _repo(tmp_path, test_command=_replay("cargo-1.94.0-test-fail-backtrace.txt", 101))

    shown = "\n".join(_failing_details(root, "test_suite"))

    assert "at ./src/pricing.rs:18:9" in shown
    assert "left: 11" in shown
    assert "/rustc/" not in shown
    assert "/home/dev/" not in shown


def test_an_outside_path_the_location_pattern_cannot_read_is_not_shown(tmp_path: Path) -> None:
    # Frames next to a failure inside the worktree whose paths are outside it
    # and written in shapes `<path>:<line>` does not cover: `@` (Homebrew's
    # node@20), a `file:///` URL (node's ESM frames) and a space in a home
    # directory. Each must be dropped, not shown in the failure's window.
    frames = tmp_path / "frames.py"
    frames.write_text(
        "import sys\n"
        "print('src/pricing.rs:3: failure here')\n"
        "print('    at run (/opt/homebrew/Cellar/node@20/20.1.0/lib/jest/x.js:10:5)')\n"
        "print('    at file:///opt/tools/runner.mjs:11:5')\n"
        "print('    at run (/Users/Jane Doe/.nvm/versions/node/v20/lib/y.js:11:5)')\n"
        # A directory whose name starts with the worktree's name is not inside it.
        f"print('    at run ({tmp_path / 'proj'}-sibling/src/other.rs:12:5)')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    root = _repo(tmp_path, test_command=f"{sys.executable} {frames}")

    shown = "\n".join(_failing_details(root, "test_suite"))

    assert "src/pricing.rs:3: failure here" in shown
    assert "node@20" not in shown
    assert "runner.mjs" not in shown
    assert "Jane Doe" not in shown
    assert "proj-sibling" not in shown


@pytest.mark.parametrize("width", [40, 400], ids=["short-lines", "long-lines"])
def test_the_extraction_is_bounded(tmp_path: Path, width: int) -> None:
    # 10,000 lines, every one naming a location inside the worktree. The
    # short lines reach the line cap first, the long ones the character cap.
    flood = tmp_path / "flood.py"
    flood.write_text(
        "import sys\n"
        "for i in range(1, 10_001):\n"
        f"    print(f'src/pricing.rs:{{i}}: failure {{i}} ' + 'x' * {width})\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    root = _repo(tmp_path, test_command=f"{sys.executable} {flood}")

    shown = "\n".join(_failing_details(root, "test_suite"))

    assert "src/pricing.rs:1: failure 1 " in shown
    # The cap is 80 lines and 8,000 characters; the rest of the margin is
    # the command label and the line saying the excerpt was cut.
    assert len(shown.splitlines()) <= 85
    assert len(shown) <= 8_500
    assert "excerpt cut at" in shown


@pytest.mark.parametrize(
    ("capture", "summary", "failure"),
    [
        (
            "pytest-9.1.1-two-failures.txt",
            "[pytest] 2 failed, 2 passed in 0.01s",
            "  test_drafts.py:13 [test_counts_the_words_in_a_draft_title] "
            "AssertionError: assert 3 == 4",
        ),
        (
            "vitest-2.1.9-two-failures.txt",
            "[vitest] Test Files  2 failed (2)\nTests  2 failed | 3 passed (5)",
            "  tests/failing.test.ts:5 [word counter > counts the words in a draft title] "
            "AssertionError: expected 3 to be 4 // Object.is equality",
        ),
    ],
    ids=["pytest", "vitest"],
)
def test_pytest_and_vitest_details_are_unchanged(
    tmp_path: Path, capture: str, summary: str, failure: str
) -> None:
    # The control: a registered parser that read the output wins, and the
    # excerpt never replaces the summary it found.
    root = _repo(tmp_path, test_command=_replay(capture, 1))

    details = _failing_details(root, "test_suite")

    assert details[0] == summary
    assert failure in details
