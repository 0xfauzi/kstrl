"""#629: a test gate that declares ``test_tool = "go-test-json"`` is read from
the report its command writes to ``$KSTRL_REPORT``.

Each test builds a real git repository holding the toy go module the captures
in ``tests/tool_output`` were made from, whose ``[verify] test_command`` copies
a capture of a real ``go test -json`` run (go 1.21.6) to ``$KSTRL_REPORT`` and
exits as go did. It then drives ``ks check --json`` or ``--write-baseline``
through ``CliRunner`` and reads what an operator and the engineer see: the
failing row's ``details``, ``not_measured`` and the baseline document. No test
needs go installed.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from tests.helpers import gitrepo
from tests.helpers.tool_output import TOOL_OUTPUT_DIR
from tests.spine_utils import git

GO_FORMAT = "go-test-json"
FAIL = "go-1.21.6-test-json-fail.jsonl"
PYTEST_CAPTURE = "pytest-9.1.1-two-failures.txt"

#: The toy module the go captures were made from, so a failure's location
#: resolves through go.mod and its source line can be shown.
GO_MOD = "module example.com/tracer\n\ngo 1.21\n"
PRICING_TEST_GO = """package pricing

import "testing"

func TestBulkPercent(t *testing.T) {
\tif got := BulkPercent(20); got != 10 {
\t\tt.Errorf("BulkPercent(20) = %d, want 10", got)
\t}
}

func TestZero(t *testing.T) {
\tif BulkPercent(1) != 0 {
\t\tt.Fatal("nonzero")
\t}
}

func TestSkipped(t *testing.T) { t.Skip("not today") }
"""

#: The failure line the engineer is shown for the fail capture.
LOCATED_FAILURE = "  pricing/pricing_test.go:7 [TestBulkPercent] BulkPercent(20) = 11, want 10"


def _capture(name: str) -> str:
    return shlex.quote(str(TOOL_OUTPUT_DIR / name))


def _writes(capture: str, exit_code: int = 1) -> str:
    """A test command that writes a captured report to $KSTRL_REPORT and exits."""
    return f'cp {_capture(capture)} "$KSTRL_REPORT"; exit {exit_code}'


def _repo(
    tmp_path: Path,
    test_command: str,
    *,
    test_tool: str | None = GO_FORMAT,
    go_mod: str | None = GO_MOD,
    files: dict[str, str] | None = None,
) -> Path:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    tool_line = f"test_tool = {json.dumps(test_tool)}\n" if test_tool else ""
    (root / "kstrl.toml").write_text(
        "[verify]\n"
        f"test_command = {json.dumps(test_command)}\n"
        'typecheck_command = "true"\n'
        'lint_command = "true"\n' + tool_line,
        encoding="utf-8",
    )
    tree = {"pricing/pricing_test.go": PRICING_TEST_GO, **(files or {})}
    if go_mod is not None:
        tree["go.mod"] = go_mod
    for relative, text in tree.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root


def _invoke(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["check", "--root", str(root), *args])


def _check(root: Path, *args: str) -> dict[str, Any]:
    """``ks check --json`` on a tree whose test gate fails."""
    result = _invoke(root, "--json", *args)
    assert result.exit_code == 1, result.output
    document: dict[str, Any] = json.loads(result.stdout)
    return document


def _test_row(document: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in document["checks"] if row["name"] == "test_suite"]
    assert len(rows) == 1, rows
    row: dict[str, Any] = rows[0]
    assert row["passed"] is False
    return row


def _test_gaps(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [gap for gap in document["not_measured"] if gap["check"] == "test_suite"]


def _baseline(root: Path, tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "baseline.json"
    result = _invoke(root, "--write-baseline", str(path))
    assert path.is_file(), result.output
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def test_a_go_test_json_failure_reaches_details(tmp_path: Path) -> None:
    root = _repo(tmp_path, _writes(FAIL))

    document = _check(root)
    row = _test_row(document)

    assert row["message"] == "Tests failed (exit code 1)"
    assert LOCATED_FAILURE in row["details"]
    assert '    | >    7 | \t\tt.Errorf("BulkPercent(20) = %d, want 10", got)' in row["details"]
    assert _test_gaps(document) == []


@pytest.mark.parametrize("diff", ["no-test-file-changed", "a-test-file-changed"])
def test_a_missing_report_is_not_measured(tmp_path: Path, diff: str) -> None:
    # The command prints pytest's text for two failures, so a text parser run
    # over declared-format output would show them as located failures.
    root = _repo(tmp_path, f"cat {_capture(PYTEST_CAPTURE)}; exit 1")
    if diff == "a-test-file-changed":
        # A changed test file sends the gate through the #620 branch, which
        # runs the suite with a report directory of its own.
        git("checkout", "-q", "-b", "work", cwd=root)
        (root / "test_prices.py").write_text("def test_prices():\n    pass\n", encoding="utf-8")
        git("add", "-A", cwd=root)
        git("commit", "-q", "-m", "a test file", cwd=root)

    document = _check(root, "--base", "main")
    row = _test_row(document)

    assert [gap["reason"] for gap in _test_gaps(document)] == ["tool_missing"]
    # The command label itself holds no $KSTRL_REPORT here, so this is the
    # refusal line and nothing else.
    assert "wrote no report to $KSTRL_REPORT" in row["details"][0].splitlines()[0]
    # No text parser read the output: a parsed pytest failure would be its own
    # details line, "  test_drafts.py:13 [test_counts_the_words_in_a_draft_title] ...".
    assert not any("[test_counts_the_words_in_a_draft_title]" in line for line in row["details"])
    baseline = _baseline(root, tmp_path)
    assert "test_suite" in baseline["unmeasured_checks"]
    assert "test_suite" not in baseline["measured_checks"]
    assert baseline["signatures"] == {}


def test_a_stale_report_from_an_earlier_run_is_not_read(tmp_path: Path) -> None:
    marker = tmp_path / "write-the-report"
    marker.touch()
    seen = tmp_path / "report-paths"
    root = _repo(
        tmp_path,
        f'echo "$KSTRL_REPORT" >> {shlex.quote(str(seen))}; '
        f"[ -f {shlex.quote(str(marker))} ] && {_writes(FAIL)}; exit 1",
    )
    assert LOCATED_FAILURE in _test_row(_check(root))["details"]

    marker.unlink()
    document = _check(root)

    assert [gap["reason"] for gap in _test_gaps(document)] == ["tool_missing"]
    assert not any("TestBulkPercent" in line for line in _test_row(document)["details"])
    # Each run got its own path, outside the worktree, and nothing is left at
    # either once the gate is done.
    paths = [Path(line) for line in seen.read_text(encoding="utf-8").splitlines()]
    assert len(paths) == 2 and paths[0] != paths[1], paths
    for path in paths:
        assert root.resolve() not in path.resolve().parents, path
        assert not path.parent.exists(), path


def _with_a_line_that_has_no_action(tmp_path: Path) -> str:
    lines = (TOOL_OUTPUT_DIR / FAIL).read_text(encoding="utf-8").splitlines(keepends=True)
    bad = tmp_path / "no-action.jsonl"
    bad.write_text(
        lines[0]
        + '{"Package":"example.com/tracer/pricing","Test":"TestBulkPercent"}\n'
        + "".join(lines[1:]),
        encoding="utf-8",
    )
    return f'cp {shlex.quote(str(bad))} "$KSTRL_REPORT"; exit 1'


@pytest.mark.parametrize(
    ("shape", "parser_said"),
    [
        ("truncated", "line 1: JSONDecodeError: "),
        ("no-action", 'line 2: not a go test event (no string "Action")'),
        # go 1.21 prints a build failure on stdout as text, not as an event.
        ("build-error", "line 1: JSONDecodeError: Expecting value"),
    ],
)
def test_a_malformed_report_fails_closed(tmp_path: Path, shape: str, parser_said: str) -> None:
    command = {
        "truncated": f'head -c 90 {_capture(FAIL)} > "$KSTRL_REPORT"; exit 1',
        "no-action": _with_a_line_that_has_no_action(tmp_path),
        "build-error": _writes("go-1.21.6-test-json-build-error.jsonl"),
    }[shape]
    root = _repo(tmp_path, command)

    document = _check(root)
    row = _test_row(document)

    gaps = _test_gaps(document)
    assert [gap["reason"] for gap in gaps] == ["command_failed"]
    assert parser_said in gaps[0]["detail"]
    assert parser_said in row["details"][0]
    assert LOCATED_FAILURE not in row["details"]


@pytest.mark.parametrize(
    "capture",
    [
        "go-1.21.6-test-json-pass.jsonl",
        "go-1.21.6-test-json-skip.jsonl",
        "go-1.21.6-test-json-empty.jsonl",
    ],
)
def test_a_report_naming_no_failure_on_a_failed_exit_is_not_measured(
    tmp_path: Path, capture: str
) -> None:
    root = _repo(tmp_path, _writes(capture))

    document = _check(root)

    gaps = _test_gaps(document)
    assert [gap["reason"] for gap in gaps] == ["command_failed"]
    assert "names no failed test" in gaps[0]["detail"]
    assert "names no failed test" in _test_row(document)["details"][0]


def test_a_location_outside_the_worktree_is_dropped(tmp_path: Path) -> None:
    # A -fullpath capture: go printed absolute paths, which the capture holds
    # under /repo, outside any test worktree.
    outside = _repo(tmp_path / "outside", _writes("go-1.21.6-test-json-fail-fullpath.jsonl"))
    # The control: the same capture pointed at this worktree keeps its path,
    # so the drop above is the inside check and not a reader that never locates.
    inside = _repo(
        tmp_path / "inside",
        f'sed "s#/repo/#$PWD/#" {_capture("go-1.21.6-test-json-fail-fullpath.jsonl")}'
        ' > "$KSTRL_REPORT"; exit 1',
    )

    outside_details = _test_row(_check(outside))["details"]
    inside_details = _test_row(_check(inside))["details"]

    assert "   [TestBulkPercent] BulkPercent(20) = 11, want 10" in outside_details
    assert not any("/repo/" in line for line in outside_details)
    assert LOCATED_FAILURE in inside_details


def test_a_location_in_an_unknown_package_stays_in_the_message(tmp_path: Path) -> None:
    # No go.mod at the root (a nested module, say): go printed the path
    # relative to a package directory kstrl cannot name, so the location stays
    # in the message and no file is claimed.
    root = _repo(tmp_path, _writes(FAIL), go_mod=None)

    details = _test_row(_check(root))["details"]

    assert "   [TestBulkPercent] pricing_test.go:7: BulkPercent(20) = 11, want 10" in details
    assert not any(line.startswith("  pricing_test.go:7") for line in details)


@pytest.mark.parametrize(
    "command",
    ["exit 0", _writes(FAIL, exit_code=0)],
    ids=["no-report", "a-report-naming-a-failure"],
)
def test_a_passing_exit_reads_no_report(tmp_path: Path, command: str) -> None:
    # The exit code decides pass or fail, and slice 1 reads nothing on exit 0
    # (owner decision 4 held at its conservative setting).
    root = _repo(tmp_path, command)

    result = _invoke(root, "--json")
    document = json.loads(result.stdout)
    rows = [row for row in document["checks"] if row["name"] == "test_suite"]

    assert result.exit_code == 0, result.output
    assert [row["passed"] for row in rows] == [True]
    assert rows[0]["message"] == "Tests passed"
    assert _test_gaps(document) == []


def test_an_unknown_format_is_refused_before_anything_runs(tmp_path: Path) -> None:
    sentinel = tmp_path / "the-command-ran"
    root = _repo(tmp_path, f"touch {shlex.quote(str(sentinel))}; exit 1", test_tool="junit")

    result = _invoke(root, "--json")

    assert result.exit_code == 2, result.output
    assert "expected one of: pytest, vitest, go-test-json" in result.output
    assert not sentinel.exists()


def test_a_declared_format_refuses_an_older_baseline(tmp_path: Path) -> None:
    root = _repo(tmp_path, "exit 1", test_tool=None)
    path = tmp_path / "baseline.json"
    assert _invoke(root, "--write-baseline", str(path)).exit_code != 2
    # The control: the configuration the baseline was written under compares.
    assert _invoke(root, "--compare-baseline", str(path)).exit_code != 2

    toml = root / "kstrl.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + f'test_tool = "{GO_FORMAT}"\n', encoding="utf-8"
    )
    result = _invoke(root, "--compare-baseline", str(path))

    assert result.exit_code == 2, result.output
    assert "measured with a different verify configuration" in result.output


def test_go_rows_stay_unmeasured_until_decided(tmp_path: Path) -> None:
    root = _repo(tmp_path, _writes(FAIL))

    baseline = _baseline(root, tmp_path)

    assert "test_suite" in baseline["unmeasured_checks"]
    assert baseline["signatures"] == {}


#: ``ks check`` on the pytest capture with test_tool unset, recorded at
#: f16ce0ca, the commit before #629. The command is location independent so
#: the digest is the same on every machine, and it exits 3 if kstrl ever
#: exports $KSTRL_REPORT to a gate that declared no format.
PYTEST_COMMAND = 'cat pytest-out.txt; test -z "${KSTRL_REPORT+x}" || exit 3; exit 1'
PYTEST_DETAILS_AT_F16CE0CA = [
    "[pytest] 2 failed, 2 passed in 0.01s",
    "  test_drafts.py:13 [test_counts_the_words_in_a_draft_title] AssertionError: assert 3 == 4",
    "    hint: Assertion failed - check the expected vs actual values.",
    "  test_drafts.py:21 [test_renders_the_title_and_the_count] AssertionError: "
    "assert {'title': 'a ...', 'words': 2} == {'title': 'a ...', 'words': 3}",
    "    hint: Assertion failed - check the expected vs actual values.",
]
PYTEST_SIGNATURES_AT_F16CE0CA = {"test_suite:assertion-error": 2}
PYTEST_DIGEST_AT_F16CE0CA = "d2605a3f328abb7c"


def test_python_signatures_are_unchanged_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A value in the operator's shell must not reach the gate: only kstrl sets it.
    monkeypatch.setenv("KSTRL_REPORT", str(tmp_path / "from-the-shell"))
    capture = (TOOL_OUTPUT_DIR / PYTEST_CAPTURE).read_text(encoding="utf-8")
    root = _repo(tmp_path, PYTEST_COMMAND, test_tool=None, files={"pytest-out.txt": capture})

    row = _test_row(_check(root))
    baseline = _baseline(root, tmp_path)

    assert row["message"] == "Tests failed (exit code 1)"
    assert row["details"] == PYTEST_DETAILS_AT_F16CE0CA
    assert baseline["signatures"] == PYTEST_SIGNATURES_AT_F16CE0CA
    assert baseline["verify_digest"] == PYTEST_DIGEST_AT_F16CE0CA


def test_a_refused_report_still_shows_what_the_command_printed(tmp_path: Path) -> None:
    # go's own error names no location, so the #622 excerpt is empty and the
    # output's tail is what the engineer is shown, under the refusal line.
    root = _repo(tmp_path, "printf 'go: no main %s\\n' module; exit 1")

    document = _check(root)
    details = _test_row(document)["details"]

    assert [gap["reason"] for gap in _test_gaps(document)] == ["tool_missing"]
    assert "wrote no report to $KSTRL_REPORT" in details[0].splitlines()[0]
    assert "go: no main module" in details[0]


def test_an_unlocated_failure_shows_what_the_test_printed(tmp_path: Path) -> None:
    # A panic prints no "name.go:N: " line, so the failure is unlocated and its
    # message is the first line that is not go's own framing (=== RUN, --- FAIL).
    original = (TOOL_OUTPUT_DIR / FAIL).read_text(encoding="utf-8")
    panicked = original.replace(
        '"Output":"    pricing_test.go:7: BulkPercent(20) = 11, want 10\\n"',
        '"Output":"panic: assignment to entry in nil map [recovered]\\n"',
    )
    assert panicked != original
    report = tmp_path / "panic.jsonl"
    report.write_text(panicked, encoding="utf-8")
    root = _repo(tmp_path, f'cp {shlex.quote(str(report))} "$KSTRL_REPORT"; exit 1')

    details = _test_row(_check(root))["details"]

    assert "   [TestBulkPercent] panic: assignment to entry in nil map [recovered]" in details
