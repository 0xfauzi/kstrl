"""#427: a JSON document a parser refuses is a verdict, not a traceback.

Two shapes, neither of them a syntax error, that ``json.loads`` raises
for and that most call sites in ``kstrl/`` never named a handler for:

- ``DEEPLY_NESTED``: a ``RecursionError``, a ``RuntimeError`` and not a
  ``ValueError``, from the scanner's own recursive descent.
- ``OVERLONG_INTEGER``: a plain ``ValueError`` from CPython's 4300-digit
  integer-string conversion limit, which is not a ``json.JSONDecodeError``
  and so escapes a handler that only names that type.

One behaviour test per consumer FAMILY, through its real entry point,
rather than additions to each family's own test file, so this file's diff
does not enter any other lane's way.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.agents.claude_code import _parse_stream_event
from kstrl.decompose import _extract_json
from kstrl.events import parse_event_line
from kstrl.inbox import Inbox, InboxConfig
from kstrl.intake_github import parse_issue_list
from kstrl.manifest import Manifest
from kstrl.tui.tail import JsonlTailer
from tests.helpers.astwalk import REPO_ROOT

#: Deeper than any recursion limit a caller could have left. json's C
#: scanner gives up at roughly 1000 levels; 100000 removes the caller's
#: own stack depth from the answer.
DEEPLY_NESTED = "[" * 100000 + "]" * 100000

#: The other escape, and not a RecursionError. CPython refuses to build an
#: int from more than sys.get_int_max_str_digits digits (4300), and raises
#: a plain ValueError out of json.loads, which walks past every
#: `except json.JSONDecodeError`.
OVERLONG_INTEGER = "1" * 5000


def test_the_daemon_inbox_skips_a_deeply_nested_line(tmp_path: Path) -> None:
    box = Inbox(tmp_path, InboxConfig())
    box.path.parent.mkdir(parents=True, exist_ok=True)
    box.path.write_text(DEEPLY_NESTED + "\n", encoding="utf-8")

    scan = box.scan()

    assert scan.records == ()
    assert scan.skipped_lines == 1
    assert scan.unreadable is False


def test_the_daemon_inbox_skips_an_overlong_integer_line(tmp_path: Path) -> None:
    box = Inbox(tmp_path, InboxConfig())
    box.path.parent.mkdir(parents=True, exist_ok=True)
    box.path.write_text(OVERLONG_INTEGER + "\n", encoding="utf-8")

    scan = box.scan()

    assert scan.records == ()
    assert scan.skipped_lines == 1
    assert scan.unreadable is False


def test_a_deeply_nested_stream_line_reaches_the_ui_as_text() -> None:
    assert list(_parse_stream_event(DEEPLY_NESTED)) == [DEEPLY_NESTED]


def test_a_deeply_nested_agent_payload_is_no_valid_json_found() -> None:
    with pytest.raises(ValueError, match="No valid JSON found in output"):
        _extract_json(DEEPLY_NESTED)


def test_a_deeply_nested_gh_payload_is_an_error_string_not_a_crash() -> None:
    issues, error = parse_issue_list(DEEPLY_NESTED)

    assert issues == []
    assert "could not parse `gh issue list` output" in error
    assert "RecursionError" in error


def test_a_deeply_nested_event_line_is_skipped_and_the_tail_continues(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(DEEPLY_NESTED + "\n", encoding="utf-8")
    tailer = JsonlTailer(path)

    first = tailer.poll()

    assert first.events == []
    assert first.truncated is False

    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"event": "run_started", "ts": "2026-01-01T00:00:00Z"}\n')

    second = tailer.poll()

    assert len(second.events) == 1


def test_the_event_line_parser_returns_none_for_a_deeply_nested_line() -> None:
    assert parse_event_line(DEEPLY_NESTED) is None


def test_calibration_compare_refuses_an_unreadable_baseline_with_exit_2(tmp_path: Path) -> None:
    old = tmp_path / "old.json"
    old.write_text(DEEPLY_NESTED, encoding="utf-8")
    new = tmp_path / "new.json"
    new.write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "kstrl.calibration", "compare", str(old), str(new)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        start_new_session=True,
    )

    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert result.stderr.strip().count("\n") == 0
    assert result.stderr.startswith("error: cannot read baseline ")
    assert "RecursionError" in result.stderr


def test_a_no_try_site_raises_a_json_decode_error_rather_than_a_recursion_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(DEEPLY_NESTED, encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        Manifest.load(path)
