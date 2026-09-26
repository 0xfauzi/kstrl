"""The CI state of a merge commit without an operator command (#570).

End to end: the real ``ks serve`` as its own process, the real ``ks
status`` and ``ks ci poll`` through Click, the real ``run_gh`` spawning
the fake ``gh`` from ``tests/test_ci_state.py`` found on PATH, the real
ledger in the control directory, and real run streams under
``.kstrl/runs/`` written with the event serializer the factory uses. No
network: the fake is the only ``gh`` these tests can reach.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl import ci_state
from kstrl.ci_state import CiReading, CiState, read_ci_ledger
from kstrl.cli import cli
from kstrl.events import PrMerged
from kstrl.statedir import CONTROL_CI_CHECKS, control_file, ensure_control_state
from tests.helpers.fakegh import put_gh_on_path
from tests.helpers.procs import run_serve_subprocess
from tests.test_ci_state import (
    FAKE_GH,
    SHA_FAILED,
    SHA_PASSED,
    SHA_RUNNING,
    _answer,
    _calls,
    _fixture,
    _project,
)

#: A run id in the shape ``kstrl.runid`` writes, older than any real one.
EARLIER_RUN = "factory-20260901-000000.000000-aaaaaa"

#: Bound on one ``ks serve --once`` against an empty queue; a failure
#: path, not the expected runtime.
SERVE_FUSE = 60.0


@pytest.fixture
def gh_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    put_gh_on_path(tmp_path, monkeypatch, FAKE_GH)
    replies = tmp_path / "gh-replies"
    replies.mkdir()
    monkeypatch.setenv("FAKE_GH_DIR", str(replies))
    return replies


def _earlier_run_merged(root: Path, component_id: str, sha: str) -> None:
    """An earlier run's stream recording one merge, as the factory writes it."""
    events = root / ".kstrl" / "runs" / EARLIER_RUN / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    line = PrMerged(
        component=component_id,
        run_id=EARLIER_RUN,
        pr_number=3,
        pr_url="https://github.com/o/r/pull/3",
        merge_sha=sha,
    ).to_json_line()
    with events.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _serve_root(root: Path) -> None:
    # max_open_prs = 0: the open-PR bound would otherwise ask gh for PRs.
    (root / "kstrl.toml").write_text("[serve]\nmax_open_prs = 0\n", encoding="utf-8")


def _serve_once(root: Path) -> tuple[int, str]:
    done = run_serve_subprocess(root, "--once", fuse=SERVE_FUSE, merge_stderr=True)
    return done.returncode, done.stdout


def _record(root: Path, *readings: CiReading) -> None:
    ensure_control_state(root)
    with control_file(root, CONTROL_CI_CHECKS).open("a", encoding="utf-8") as handle:
        for reading in readings:
            handle.write(json.dumps(reading.to_dict()) + "\n")


def _status(root: Path) -> Result:
    return CliRunner().invoke(
        cli,
        ["status", "--root", str(root), "--ui", "plain", "--no-tui", "--no-color"],
        catch_exceptions=False,
    )


def _poll(root: Path) -> Result:
    return CliRunner().invoke(
        cli, ["ci", "poll", "--root", str(root), "--ui", "plain"], catch_exceptions=True
    )


class TestServeRefreshesWithoutACommand:
    def test_serve_once_records_the_state_of_a_merge_nobody_polled(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _serve_root(root)
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))

        code, out = _serve_once(root)

        assert code == 0, out
        latest = read_ci_ledger(root).latest(SHA_PASSED)
        assert latest is not None, out
        assert (latest.state, latest.checks) == (CiState.PASSED, 7)
        assert f"CI {SHA_PASSED[:12]} passed" in out

    def test_serve_reads_a_merge_only_an_earlier_run_recorded(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-new": ""})
        _earlier_run_merged(root, "comp-old", SHA_FAILED)
        _serve_root(root)
        _answer(gh_dir, SHA_FAILED, _fixture("failed.json"))

        code, out = _serve_once(root)

        assert code == 0, out
        latest = read_ci_ledger(root).latest(SHA_FAILED)
        assert latest is not None, out
        assert (latest.state, latest.reason) == (CiState.FAILED, "failed: test")

    def test_serve_does_not_ask_again_for_a_commit_that_passed(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _serve_root(root)
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))

        first_code, first_out = _serve_once(root)
        second_code, second_out = _serve_once(root)

        assert (first_code, second_code) == (0, 0), first_out + second_out
        graphql = [c for c in _calls(gh_dir) if c[:2] == ["api", "graphql"]]
        assert len(graphql) == 1, graphql
        assert [r.state for r in read_ci_ledger(root).readings] == [CiState.PASSED]

    def test_serve_reads_a_running_commit_again_and_stops_once_it_settles(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_RUNNING})
        _serve_root(root)
        _answer(gh_dir, SHA_RUNNING, _fixture("running.json"))

        outs = [_serve_once(root)]
        # CI finished between the two firings.
        _answer(gh_dir, SHA_RUNNING, _fixture("passed.json"))
        outs += [_serve_once(root), _serve_once(root)]

        assert [code for code, _ in outs] == [0, 0, 0], "".join(out for _, out in outs)
        graphql = [c for c in _calls(gh_dir) if c[:2] == ["api", "graphql"]]
        assert len(graphql) == 2, graphql
        assert [r.state for r in read_ci_ledger(root).readings] == [
            CiState.RUNNING,
            CiState.PASSED,
        ], "".join(out for _, out in outs)

    def test_a_refresh_that_cannot_record_is_reported_and_serve_carries_on(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _serve_root(root)
        _answer(gh_dir, SHA_PASSED, _fixture("passed.json"))
        # A directory where the ledger file goes: every append raises.
        control_file(root, CONTROL_CI_CHECKS).mkdir(parents=True)

        code, out = _serve_once(root)

        assert code == 0, out
        assert "CI state not refreshed: IsADirectoryError" in out, out


class TestStatusShowsTheRecordedState:
    def test_each_merge_commit_shows_its_state_reason_and_read_time(self, tmp_path: Path) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED, "comp-b": SHA_RUNNING})
        _earlier_run_merged(root, "comp-old", SHA_FAILED)
        _record(
            root,
            CiReading(SHA_PASSED, CiState.PASSED, "7 checks passed", "2026-09-26T10:00:00Z", 7),
            CiReading(SHA_FAILED, CiState.UNKNOWN, "gh: auth login", "2026-09-26T10:01:00Z", 0),
        )

        result = _status(root)

        assert result.exit_code == 0, result.output
        out = result.output
        assert re.search(
            rf"comp-a\S*\s+{SHA_PASSED[:12]}\s+passed: 7 checks passed "
            r"\(read 2026-09-26T10:00:00Z\)",
            out,
        ), out
        assert re.search(rf"comp-b\S*\s+{SHA_RUNNING[:12]}\s+unknown: never read", out), out
        assert re.search(
            rf"comp-old\S*\s+{SHA_FAILED[:12]}\s+unknown: gh: auth login "
            r"\(read 2026-09-26T10:01:00Z\)",
            out,
        ), out

    def test_an_unreadable_ledger_shows_unknown_with_the_reason(self, tmp_path: Path) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _record(
            root,
            CiReading(SHA_PASSED, CiState.PASSED, "7 checks passed", "2026-09-26T10:00:00Z", 7),
        )
        with control_file(root, CONTROL_CI_CHECKS).open("ab") as handle:
            handle.write(b"\xff\xfe not utf-8\n")

        result = _status(root)

        assert result.exit_code == 0, result.output
        assert "CI: unknown, the record could not be read: UnicodeDecodeError" in result.output
        assert "passed: 7 checks passed" not in result.output

    def test_a_torn_ledger_line_is_counted_not_hidden(self, tmp_path: Path) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _record(
            root,
            CiReading(SHA_PASSED, CiState.PASSED, "7 checks passed", "2026-09-26T10:00:00Z", 7),
        )
        with control_file(root, CONTROL_CI_CHECKS).open("a", encoding="utf-8") as handle:
            handle.write('{"sha": "tor')

        result = _status(root)

        assert result.exit_code == 0, result.output
        assert re.search(r"ledger\S*\s+1 line\(s\) could not be read", result.output), result.output

    def test_an_unreadable_run_stream_is_refused_not_read_as_no_merges(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        _earlier_run_merged(root, "comp-old", SHA_FAILED)
        events = root / ".kstrl" / "runs" / EARLIER_RUN / "events.jsonl"
        with events.open("ab") as handle:
            handle.write(b"\xff\xfe not utf-8\n")

        result = _status(root)

        assert result.exit_code == 0, result.output
        assert "CI: unknown, the record could not be read: UnicodeDecodeError" in result.output


class TestEveryPageIsRead:
    def test_a_commit_with_more_than_one_page_of_checks_is_read_completely(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        # Captured with page size 3: three pages, seven checks.
        (gh_dir / f"{SHA_PASSED}.json").write_text(
            (Path(__file__).parent / "fixtures" / "ci" / "paged.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        result = _poll(root)

        assert result.exit_code == 0, result.output
        reading = read_ci_ledger(root).readings[0]
        assert (reading.state, reading.reason, reading.checks) == (
            CiState.PASSED,
            "7 checks passed",
            7,
        )
        (call,) = _calls(gh_dir)
        assert "--paginate" in call and "--slurp" in call, call

    @pytest.mark.parametrize("kept", [1, 2])
    def test_a_reply_missing_a_page_is_unknown_never_passed(
        self, tmp_path: Path, gh_dir: Path, kept: int
    ) -> None:
        pages: list[Any] = json.loads(
            (Path(__file__).parent / "fixtures" / "ci" / "paged.json").read_text(encoding="utf-8")
        )
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        (gh_dir / f"{SHA_PASSED}.json").write_text(json.dumps(pages[:kept]), encoding="utf-8")

        result = _poll(root)

        assert result.exit_code == 1, result.output
        reading = read_ci_ledger(root).readings[0]
        assert (reading.state, reading.reason) == (
            CiState.UNKNOWN,
            f"read {3 * kept} of 7 checks",
        )

    def test_pages_that_disagree_on_the_check_count_are_unknown(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        pages: list[Any] = json.loads(
            (Path(__file__).parent / "fixtures" / "ci" / "paged.json").read_text(encoding="utf-8")
        )
        # A check was added between two page requests: page 2 counts 8.
        pages[1]["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]["totalCount"] = 8
        root = _project(tmp_path, {"comp-a": SHA_PASSED})
        (gh_dir / f"{SHA_PASSED}.json").write_text(json.dumps(pages), encoding="utf-8")

        result = _poll(root)

        assert result.exit_code == 1, result.output
        reading = read_ci_ledger(root).readings[0]
        assert (reading.state, reading.reason) == (
            CiState.UNKNOWN,
            "the pages disagree on the check count: [7, 8]",
        )

    def test_ci_poll_reads_a_merge_only_an_earlier_run_recorded(
        self, tmp_path: Path, gh_dir: Path
    ) -> None:
        root = _project(tmp_path, {"comp-new": ""})
        _earlier_run_merged(root, "comp-old", SHA_FAILED)
        _answer(gh_dir, SHA_FAILED, _fixture("failed.json"))

        result = _poll(root)

        assert result.exit_code == 1, result.output
        assert [(r.sha, r.state) for r in read_ci_ledger(root).readings] == [
            (SHA_FAILED, CiState.FAILED)
        ]
        assert re.search(rf"comp-old\s+{SHA_FAILED[:12]}\s+failed", result.output)


T0 = datetime(2026, 9, 26, 10, 0, 0, tzinfo=UTC)


def _at(state: CiState, moment: datetime) -> CiReading:
    return CiReading(SHA_PASSED, state, "r", moment.strftime("%Y-%m-%dT%H:%M:%SZ"), 0)


class TestWhenACommitIsReadAgain:
    @pytest.mark.parametrize(
        ("readings", "now", "due"),
        [
            ([], T0, True),
            ([_at(CiState.PASSED, T0)], T0 + timedelta(days=9), False),
            ([_at(CiState.RUNNING, T0), _at(CiState.FAILED, T0)], T0 + timedelta(days=9), False),
            ([_at(CiState.RUNNING, T0)], T0, True),
            ([_at(CiState.UNKNOWN, T0)], T0 + timedelta(seconds=1), True),
            (
                [_at(CiState.RUNNING, T0), _at(CiState.RUNNING, T0 + timedelta(seconds=60))],
                T0 + timedelta(seconds=119),
                False,
            ),
            (
                [_at(CiState.RUNNING, T0), _at(CiState.UNKNOWN, T0 + timedelta(seconds=60))],
                T0 + timedelta(seconds=120),
                True,
            ),
            (
                [
                    CiReading(SHA_PASSED, CiState.RUNNING, "r", "yesterday", 0),
                    _at(CiState.RUNNING, T0),
                ],
                T0,
                True,
            ),
        ],
        ids=[
            "never-read",
            "passed-is-final",
            "failed-is-final",
            "one-running-reading",
            "one-unknown-reading",
            "gap-not-yet-doubled",
            "gap-doubled",
            "unparseable-read-time",
        ],
    )
    def test_refresh_due(self, readings: list[CiReading], now: datetime, due: bool) -> None:
        # Through the module, so a tree without it fails here, not at import.
        assert ci_state.refresh_due(readings, now) is due
