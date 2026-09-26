"""End-to-end tests through ``ks ci poll`` (#553).

The real command, the real manifest loader on a real file, the real
``run_gh`` spawning a fake ``gh`` found on PATH, the real ledger in the
control directory. The fake answers with GitHub replies captured on
2026-09-26 (``tests/fixtures/ci/PROVENANCE.md``) or with a named edit
of one. No network: the fake is the only ``gh`` these tests can reach.
``isolate_kstrl_state`` (autouse) points ``XDG_STATE_HOME`` at a sibling
of ``tmp_path``.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl.ci_state import CI_QUERY, CiState, read_ci_ledger
from kstrl.cli import cli
from kstrl.manifest import Component, Manifest
from kstrl.statedir import CONTROL_CI_CHECKS, control_file
from tests.helpers.fakegh import put_gh_on_path

FIXTURES = Path(__file__).parent / "fixtures" / "ci"

SHA_PASSED = "b057de21b57bb8543db8bb0b2e1569e0a98e84e5"
SHA_FAILED = "0aa55d980241dd702f6f56d661ea0fc8bad04a8f"
SHA_RUNNING = "6cabef5547ce47529addcbde62e6c865e89d5aff"
SHA_OTHER = "1" * 40

#: Answers ``gh api graphql ... -f oid=<sha> ...`` with
#: ``$FAKE_GH_DIR/<sha>.json``, or fails like an unauthenticated gh when
#: ``$FAKE_GH_DIR/<sha>.fail`` exists. Appends every argv, one argument
#: per line and a blank line after, to ``$FAKE_GH_DIR/calls``.
FAKE_GH = """#!/bin/sh
sha=""
for arg in "$@"; do
  case "$arg" in oid=*) sha="${arg#oid=}" ;; esac
  printf '%s\\n' "$arg" >> "$FAKE_GH_DIR/calls"
done
printf '\\n' >> "$FAKE_GH_DIR/calls"
if [ -f "$FAKE_GH_DIR/$sha.fail" ]; then
  cat "$FAKE_GH_DIR/$sha.fail" >&2
  exit 4
fi
cat "$FAKE_GH_DIR/$sha.json"
"""

ISO_UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _fixture(name: str) -> dict[str, Any]:
    document = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


@pytest.fixture
def gh_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    put_gh_on_path(tmp_path, monkeypatch, FAKE_GH)
    replies = tmp_path / "gh-replies"
    replies.mkdir()
    monkeypatch.setenv("FAKE_GH_DIR", str(replies))
    return replies


@pytest.fixture
def local_clock_is_not_utc() -> Iterator[None]:
    """Run with the process's local time zone at UTC+05:30, so a reading
    stamped with local time instead of UTC is 5.5 hours off, not equal."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def _answer(gh_dir: Path, sha: str, reply: dict[str, Any] | str) -> None:
    text = reply if isinstance(reply, str) else json.dumps(reply)
    (gh_dir / f"{sha}.json").write_text(text, encoding="utf-8")


def _calls(gh_dir: Path) -> list[list[str]]:
    path = gh_dir / "calls"
    if not path.exists():
        return []
    blocks = path.read_text(encoding="utf-8").split("\n\n")
    return [block.split("\n") for block in blocks if block.strip()]


def _project(tmp_path: Path, merges: dict[str, str]) -> Path:
    """A project whose manifest records ``merges`` (component id -> sha;
    "" is a component with no recorded merge)."""
    root = tmp_path / "project"
    (root / "scripts" / "kstrl").mkdir(parents=True)
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="demo",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=component_id,
                title=component_id,
                description=component_id,
                dependencies=[],
                prd_path=f"scripts/kstrl/demo/{component_id}/prd.json",
                branch_name=f"kstrl/demo/{component_id}",
                status="completed",
                merge_sha=sha,
            )
            for component_id, sha in merges.items()
        ],
    )
    manifest.save(root / "scripts" / "kstrl" / "manifest.json")
    return root


def _poll(root: Path) -> Result:
    return CliRunner().invoke(
        cli, ["ci", "poll", "--root", str(root), "--ui", "plain"], catch_exceptions=True
    )


class TestEachMergeCommitIsRecorded:
    def test_passed_failed_and_running_are_recorded_with_sha_and_time(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(
            tmp_path,
            {"comp-a": SHA_PASSED, "comp-b": SHA_FAILED, "comp-c": SHA_RUNNING, "comp-d": ""},
        )
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))
        _answer(gh_dir, SHA_FAILED, _fixture("failed.json"))
        _answer(gh_dir, SHA_RUNNING, _fixture("running.json"))

        result = _poll(root)

        # A failed commit is a finding: exit 1 under the CLI exit contract.
        assert result.exit_code == 1, result.output
        ledger = read_ci_ledger(root)
        assert ledger.dropped == 0
        got = [(r.sha, r.state, r.checks) for r in ledger.readings]
        assert got == [
            (SHA_PASSED, CiState.PASSED, 7),
            (SHA_FAILED, CiState.FAILED, 5),
            (SHA_RUNNING, CiState.RUNNING, 4),
        ]
        assert all(ISO_UTC.fullmatch(r.observed_at) for r in ledger.readings)
        assert ledger.readings[1].reason == "failed: test"
        assert ledger.readings[2].reason == "running: test"
        # gh was asked once per recorded merge, never for comp-d, with the
        # query the fixtures were captured with.
        calls = _calls(gh_dir)
        assert [c[c.index("-f") + 1] for c in calls] == [
            f"oid={SHA_PASSED}",
            f"oid={SHA_FAILED}",
            f"oid={SHA_RUNNING}",
        ]
        assert all(c[:2] == ["api", "graphql"] and f"query={CI_QUERY}" in c for c in calls)
        for component_id, state in (("comp-a", "passed"), ("comp-b", "failed")):
            assert re.search(rf"{component_id}\s+\w{{12}}\s+{state}", result.output), result.output

    def test_every_commit_passed_exits_zero(self, tmp_path: Path, gh_dir: Path) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))

        result = _poll(root)

        assert result.exit_code == 0, result.output
        assert read_ci_ledger(root).readings[0].reason == "7 checks passed"

    def test_a_failing_status_beside_passing_check_runs_is_failed(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        reply = _fixture("passed.json")
        nodes = reply["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]
        nodes["nodes"].append(
            {"__typename": "StatusContext", "context": "ext/ci", "state": "ERROR"}
        )
        nodes["totalCount"] = 8
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _answer(gh_dir, SHA_PASSED, reply)

        result = _poll(root)

        assert result.exit_code == 1, result.output
        reading = read_ci_ledger(root).readings[0]
        assert (reading.state, reading.reason, reading.checks) == (
            CiState.FAILED,
            "failed: ext/ci",
            8,
        )

    def test_a_failure_beside_a_running_check_is_failed(self, tmp_path: Path, gh_dir: Path) -> None:
        reply = _fixture("failed.json")
        contexts = reply["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]
        contexts["nodes"].append(
            {"__typename": "CheckRun", "name": "late", "status": "IN_PROGRESS", "conclusion": None}
        )
        contexts["totalCount"] = 6
        root = _project(tmp_path, {"comp-a": SHA_FAILED})
        _answer(gh_dir, SHA_FAILED, reply)

        result = _poll(root)

        assert result.exit_code == 1, result.output
        reading = read_ci_ledger(root).readings[0]
        assert (reading.state, reading.reason) == (CiState.FAILED, "failed: test")

    def test_a_pending_status_is_running(self, tmp_path: Path, gh_dir: Path) -> None:
        reply = _fixture("passed.json")
        contexts = reply["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]
        contexts["nodes"].append(
            {"__typename": "StatusContext", "context": "ext/ci", "state": "PENDING"}
        )
        contexts["totalCount"] = 8
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _answer(gh_dir, SHA_PASSED, reply)

        result = _poll(root)

        assert result.exit_code == 0, result.output
        assert read_ci_ledger(root).readings[0].state is CiState.RUNNING

    def test_the_read_time_is_utc_now(
        self, tmp_path: Path, gh_dir: Path, local_clock_is_not_utc: None
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))
        before = datetime.now(UTC).replace(microsecond=0)

        assert _poll(root).exit_code == 0

        after = datetime.now(UTC)
        stamped = datetime.strptime(
            read_ci_ledger(root).readings[0].observed_at, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
        assert before <= stamped <= after + timedelta(seconds=1)

    def test_a_ledger_that_cannot_be_written_fails_the_poll(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))
        # A directory where the ledger file goes: every append raises.
        control_file(root, CONTROL_CI_CHECKS).mkdir(parents=True)

        result = _poll(root)

        # Not recorded must not look recorded: the OSError reaches the
        # caller instead of a clean exit 0 over a reading nobody kept.
        assert isinstance(result.exception, OSError), result.output
        assert result.exit_code != 0


def _with_node(node: Any) -> dict[str, Any]:
    """The captured passing reply plus one extra entry, counted."""
    reply = copy.deepcopy(_fixture("passed.json"))
    contexts = reply["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]
    contexts["nodes"].append(node)
    contexts["totalCount"] = len(contexts["nodes"])
    return reply


def _truncated() -> dict[str, Any]:
    reply = copy.deepcopy(_fixture("passed.json"))
    reply["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]["totalCount"] = 8
    return reply


def _no_entries() -> dict[str, Any]:
    reply = copy.deepcopy(_fixture("passed.json"))
    contexts = reply["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]
    contexts["nodes"] = []
    contexts["totalCount"] = 0
    return reply


#: (id, reply) where reply is a JSON document, raw text, or None for a
#: gh that fails. Every one must be recorded as unknown, never passed.
#: Seven of the nine "extra entry" cases sit beside seven PASSING
#: entries: the reading is unknown because the unread entry could be the
#: failing one.
UNREADABLE: list[tuple[str, dict[str, Any] | str | None]] = [
    ("gh-fails-unauthenticated", None),
    ("reply-is-not-json", "gh: this is not JSON"),
    ("reply-is-a-list", "[]"),
    ("graphql-errors", {"errors": [{"message": "Something went wrong"}], "data": None}),
    # GitHub can answer with partial data AND errors: the errors win even
    # when the data beside them reads as seven passing checks.
    (
        "graphql-errors-beside-passing-data",
        {**_fixture("passed.json"), "errors": [{"message": "Resource not accessible"}]},
    ),
    ("no-repository", {"data": {"repository": None}}),
    ("no-commit", _fixture("no-commit.json")),
    ("not-a-commit", _fixture("not-a-commit.json")),
    ("no-checks", _fixture("no-checks.json")),
    ("zero-entries", _no_entries()),
    ("read-fewer-than-total", _truncated()),
    ("entry-is-a-string", _with_node("CheckRun")),
    ("entry-of-unknown-type", _with_node({"__typename": "Deployment", "name": "x"})),
    (
        "unknown-conclusion",
        _with_node(
            {"__typename": "CheckRun", "name": "x", "status": "COMPLETED", "conclusion": "WEIRD"}
        ),
    ),
    (
        "completed-with-null-conclusion",
        _with_node(
            {"__typename": "CheckRun", "name": "x", "status": "COMPLETED", "conclusion": None}
        ),
    ),
    (
        "unknown-status",
        _with_node({"__typename": "CheckRun", "name": "x", "status": "PAUSED", "conclusion": None}),
    ),
    (
        "unhashable-status",
        _with_node({"__typename": "CheckRun", "name": "x", "status": {}, "conclusion": None}),
    ),
    (
        "unknown-status-context-state",
        _with_node({"__typename": "StatusContext", "context": "x", "state": "WEIRD"}),
    ),
    ("entry-missing-its-name", _with_node({"__typename": "CheckRun", "status": "COMPLETED"})),
]


class TestAStateKstrlCouldNotReadIsUnknown:
    @pytest.mark.parametrize(
        ("reply",), [(reply,) for _, reply in UNREADABLE], ids=[case for case, _ in UNREADABLE]
    )
    def test_recorded_as_unknown_never_passed(
        self, tmp_path: Path, gh_dir: Path, reply: dict[str, Any] | str | None
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        if reply is None:
            (gh_dir / f"{SHA_PASSED}.fail").write_text(
                "gh: To get started with GitHub CLI, please run:  gh auth login\n",
                encoding="utf-8",
            )
        else:
            _answer(gh_dir, SHA_PASSED, reply)

        result = _poll(root)

        # Unknown needs the operator: exit 1, and recorded, not skipped.
        assert result.exit_code == 1, result.output
        ledger = read_ci_ledger(root)
        assert [(r.sha, r.state, r.checks) for r in ledger.readings] == [
            (SHA_PASSED, CiState.UNKNOWN, 0)
        ]
        assert ledger.readings[0].reason
        assert ISO_UTC.fullmatch(ledger.readings[0].observed_at)
        assert "unknown" in result.output

    def test_a_merge_sha_that_is_not_a_full_sha_is_unknown_without_asking_gh(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": "not-a-sha"})

        result = _poll(root)

        assert result.exit_code == 1, result.output
        assert [r.state for r in read_ci_ledger(root).readings] == [CiState.UNKNOWN]
        assert _calls(gh_dir) == []


class TestAReaderSeesTheNewestReadingAndItsAge:
    def test_a_second_poll_supersedes_the_first_and_both_are_kept(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_RUNNING})
        _answer(gh_dir, SHA_RUNNING, _fixture("running.json"))
        assert _poll(root).exit_code == 0
        _answer(gh_dir, SHA_RUNNING, _fixture("passed.json"))
        assert _poll(root).exit_code == 0

        ledger = read_ci_ledger(root)

        assert [r.state for r in ledger.readings] == [CiState.RUNNING, CiState.PASSED]
        latest = ledger.latest(SHA_RUNNING)
        assert latest is not None and latest.state is CiState.PASSED
        assert latest.observed_at >= ledger.readings[0].observed_at
        assert ledger.latest(SHA_OTHER) is None

    def test_a_torn_or_unusable_line_is_counted_not_hidden(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))
        assert _poll(root).exit_code == 0
        path = control_file(root, CONTROL_CI_CHECKS)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"sha": "' + SHA_OTHER + '", "state": "green"}\n{"sha": "tor')

        ledger = read_ci_ledger(root)

        assert ledger.dropped == 2
        assert [r.state for r in ledger.readings] == [CiState.PASSED]

    def test_an_undecodable_ledger_is_refused_not_read_as_empty(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))
        assert _poll(root).exit_code == 0
        with control_file(root, CONTROL_CI_CHECKS).open("ab") as handle:
            handle.write(b"\xff\xfe not utf-8\n")

        # An empty answer would silently remove the record; a refusal
        # names the problem.
        with pytest.raises(UnicodeDecodeError):
            read_ci_ledger(root)

    def test_nothing_polled_reads_as_an_empty_ledger(self, tmp_path: Path) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})

        ledger = read_ci_ledger(root)

        assert ledger.readings == () and ledger.dropped == 0
        assert ledger.latest(SHA_PASSED) is None


class TestNothingToRead:
    def test_no_manifest_is_exit_2_and_gh_is_not_asked(self, tmp_path: Path, gh_dir: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()

        result = _poll(root)

        assert result.exit_code == 2, result.output
        assert "No manifest found" in result.output
        assert _calls(gh_dir) == []
        assert not control_file(root, CONTROL_CI_CHECKS).exists()

    def test_no_recorded_merge_is_exit_0_and_writes_nothing(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": ""})

        result = _poll(root)

        assert result.exit_code == 0, result.output
        assert "No merge commits recorded" in result.output
        assert _calls(gh_dir) == []
        assert not control_file(root, CONTROL_CI_CHECKS).exists()
