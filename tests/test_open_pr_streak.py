"""R10.7: the open-PR count streak, and the alarm it arms (#228 round 2).

`ks serve` ships in two launchd shapes and only one of them is a
long-lived process. `--plist-mode keepalive` runs one process that polls;
`--plist-mode interval` runs `ks serve --once` on a
`StartCalendarInterval`, one process per firing. The alarm that files an
inbox item after three consecutive unusable counts lived in the loop's
frame, so in interval mode `consecutive` never exceeded 1 and the
threshold was unreachable. The mode with no alarm is the one most
exposed to the failure the alarm names: a `gh` missing from launchd's
PATH.

Everything here is about the two boundaries a process makes: what
survives one, and what a damaged or unwritable record does at one.
`tests/test_flow_control.py` is what the daemon does about the count
inside a single process.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.serve import (
    STREAK_SCHEMA_VERSION,
    CycleResult,
    OpenPrCountStreak,
    RunOutcome,
    ServeConfig,
    _record_count_failure,
    serve,
)
from kstrl.statedir import CONTROL_PR_COUNT_STREAK, control_file
from tests.helpers.fakegh import install_marker_gh as _install_marker_gh
from tests.test_serve import _add, _queue, _stub_runner

_REASON = "cannot count open kstrl PRs: gh pr failed (99): "


def _streak_path(root: Path) -> Path:
    return control_file(root, CONTROL_PR_COUNT_STREAK)


def _open_items(root: Path) -> list[Any]:
    return Inbox(root, InboxConfig.load(root)).open_items()


def _serve_once(root: Path) -> CycleResult:
    return serve(
        root,
        once=True,
        config=ServeConfig(max_open_prs=1),
        runner=_stub_runner(RunOutcome(0)),
    )[0]


def _serve_loop(root: Path, cycles: int) -> list[CycleResult]:
    return serve(
        root,
        config=ServeConfig(max_open_prs=1),
        runner=_stub_runner(RunOutcome(0)),
        max_cycles=cycles,
        sleeper=lambda _: None,
    )


def _failing_daemon(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_marker_gh(root, monkeypatch)
    _add(_queue(root))


class TestTheStreakSurvivesAProcessBoundary:
    """The measurement SHOULD-FIX 1 was raised on, as a test.

    Five `--once` firings and five polls of one keepalive loop are the
    same five polls, so they must reach the same answer. Before this,
    the first filed nothing at all.
    """

    def test_five_once_firings_file_exactly_one_item(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _failing_daemon(tmp_path, monkeypatch)

        results = [_serve_once(tmp_path) for _ in range(5)]

        assert all("cannot count" in r.skipped for r in results)
        assert [r.needs_human for r in results] == [False, False, True, False, False]
        items = _open_items(tmp_path)
        assert len(items) == 1, "one item per streak, not one per firing"
        assert items[0].kind is ItemKind.HALTED_RUN
        assert items[0].occurrences == 1

    def test_five_keepalive_polls_reach_the_identical_answer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The positive control for the row above: same five polls, one
        process. Without it, "one item" could pass because nothing files
        in either mode."""
        _failing_daemon(tmp_path, monkeypatch)

        results = _serve_loop(tmp_path, 5)

        assert [r.needs_human for r in results] == [False, False, True, False, False]
        assert len(_open_items(tmp_path)) == 1

    def test_the_file_carries_the_count_and_its_schema(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _failing_daemon(tmp_path, monkeypatch)

        _serve_once(tmp_path)
        after_one = json.loads(_streak_path(tmp_path).read_text(encoding="utf-8"))
        _serve_once(tmp_path)
        after_two = json.loads(_streak_path(tmp_path).read_text(encoding="utf-8"))

        assert after_one == {
            "schema_version": STREAK_SCHEMA_VERSION,
            "consecutive": 1,
            "filed": False,
        }
        assert after_two["consecutive"] == 2

    def test_a_good_count_clears_the_persisted_streak(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two failing firings, then a working one, then two more: nothing
        files. The reset has to survive the process boundary as well, or
        the alarm fires on failures spread over an afternoon."""
        from tests.helpers.fakegh import FAKE_GH_THIRD_CALL_WORKS, put_gh_on_path

        counter_file = tmp_path / "gh_calls"
        monkeypatch.setenv("FAKE_GH_COUNT", str(counter_file))
        payload = tmp_path / "ok.json"
        payload.write_text("[]", encoding="utf-8")
        monkeypatch.setenv("FAKE_GH_JSON", str(payload))
        put_gh_on_path(tmp_path, monkeypatch, FAKE_GH_THIRD_CALL_WORKS)
        _add(_queue(tmp_path))

        results = [_serve_once(tmp_path) for _ in range(5)]

        assert counter_file.read_text(encoding="utf-8").strip() == "5"
        assert [("cannot count" in r.skipped) for r in results] == [
            True,
            True,
            False,
            True,
            True,
        ]
        assert _open_items(tmp_path) == [], "a good count did not clear the file"


class TestTwoInstancesFileOneItem:
    """The dedupe key, which had no test: mutation T5 was STILL GREEN.

    `filed` guarantees one write per streak, so inside one process the
    key never gets a second chance to matter. Its actual job is ACROSS
    streaks - a restart, or a second checkout sharing the control
    directory - and a key that varied per poll would leave two rows here.
    """

    def test_two_streaks_filing_on_the_same_failure_make_one_item(
        self,
        tmp_path: Path,
    ) -> None:
        for consecutive in (3, 4):
            streak = OpenPrCountStreak(consecutive=consecutive - 1)
            streak.record_inconclusive(_REASON)
            result = CycleResult()
            _record_count_failure(tmp_path, streak, result)
            assert result.inbox_items != (), "each instance must have filed"
            assert streak.filed is True

        items = _open_items(tmp_path)
        assert len(items) == 1, "a per-poll dedupe key would leave two rows"
        assert items[0].occurrences == 2


class TestAFailedWriteDoesNotDisarmTheAlarm:
    """SHOULD-FIX 2. `_file_inbox_item` swallows every failure and returns
    "", and it returns "" without trying at all when the inbox is
    disabled. Marking the streak filed BEFORE the write threw the alarm
    away at the moment it was needed, and put an id that names nothing
    into `CycleResult.inbox_items`."""

    def test_a_failed_crossing_poll_files_on_the_next_one(
        self,
        tmp_path: Path,
    ) -> None:
        config = tmp_path / "kstrl.toml"
        config.write_text("[inbox]\nenabled = false\n", encoding="utf-8")
        streak = OpenPrCountStreak(consecutive=2)
        streak.record_inconclusive(_REASON)

        crossed = CycleResult()
        _record_count_failure(tmp_path, streak, crossed)

        assert crossed.needs_human is True, "a human is needed either way"
        assert crossed.inbox_items == (), "an empty id is not an item id"
        assert streak.filed is False, "the alarm must stay armed"

        config.write_text("[inbox]\nenabled = true\n", encoding="utf-8")
        streak.record_inconclusive(_REASON)
        recovered = CycleResult()
        _record_count_failure(tmp_path, streak, recovered)

        assert len(recovered.inbox_items) == 1
        assert recovered.inbox_items[0] != ""
        assert streak.filed is True
        items = _open_items(tmp_path)
        assert len(items) == 1, "exactly one item across both polls"
        assert items[0].occurrences == 1


class TestADamagedFileFailsTowardTheAlarm:
    """A record of a silent failure that cannot be read must not restart
    the count from zero and re-earn three polls of silence. It comes back
    AT the threshold, so the next unusable count files at once, and the
    bytes are left exactly as they were: overwriting them destroys the
    only thing an operator could inspect, and the next load would then
    find a clean file and report nothing."""

    @pytest.mark.parametrize(
        ("content", "cause"),
        [
            ("not json at all", "malformed JSON"),
            ("[1, 2, 3]", "top-level value is not an object"),
            ('{"consecutive": 1, "filed": false}', "schema_version"),
            ('{"schema_version": 99, "consecutive": 1, "filed": false}', "schema_version"),
            ('{"schema_version": 1, "consecutive": "two", "filed": false}', "consecutive"),
            ('{"schema_version": 1, "consecutive": true, "filed": false}', "consecutive"),
            ('{"schema_version": 1, "consecutive": -1, "filed": false}', "consecutive"),
            ('{"schema_version": 1, "consecutive": 1, "filed": "yes"}', "filed"),
        ],
        ids=[
            "not json",
            "not an object",
            "no schema version",
            "wrong schema version",
            "consecutive is a string",
            "consecutive is a bool",
            "consecutive is negative",
            "filed is a string",
        ],
    )
    def test_every_unreadable_shape_arms_the_alarm(
        self,
        tmp_path: Path,
        content: str,
        cause: str,
    ) -> None:
        path = _streak_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        with pytest.warns(RuntimeWarning, match="rejected the open-PR count streak"):
            streak = OpenPrCountStreak.load(tmp_path)

        assert streak.consecutive == OpenPrCountStreak().threshold
        assert streak.filed is False
        assert streak.damaged is not None
        assert cause in streak.damaged
        assert str(path) in streak.damaged

    def test_bytes_that_are_not_utf8_arm_the_alarm(self, tmp_path: Path) -> None:
        """`UnicodeDecodeError` is a `ValueError`, so it escapes a handler
        written as `except OSError` alone (#291)."""
        path = _streak_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"schema_version": 1, "consecutive": \xff\xfe}')

        with pytest.warns(RuntimeWarning, match="unreadable"):
            streak = OpenPrCountStreak.load(tmp_path)

        assert streak.consecutive == OpenPrCountStreak().threshold
        assert streak.damaged is not None

    def test_a_damaged_file_files_one_item_and_is_left_byte_identical(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _failing_daemon(tmp_path, monkeypatch)
        path = _streak_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        damaged = b'{"schema_version": 1, "consecutive": "two"}\n'
        path.write_bytes(damaged)

        with pytest.warns(RuntimeWarning, match="rejected the open-PR count streak"):
            result = _serve_once(tmp_path)

        assert result.needs_human is True
        assert len(_open_items(tmp_path)) == 1, "the first failure after damage files"
        assert path.read_bytes() == damaged, "the damaged record was overwritten"

    def test_a_missing_file_is_first_run_and_not_an_alarm(self, tmp_path: Path) -> None:
        """The one case that must NOT arm it, or every fresh install files
        an item on its first failing poll."""
        assert not _streak_path(tmp_path).exists()

        streak = OpenPrCountStreak.load(tmp_path)

        assert streak.consecutive == 0
        assert streak.filed is False
        assert streak.damaged is None

    def test_a_damaged_load_does_not_file_on_an_unrelated_gate(
        self,
        tmp_path: Path,
    ) -> None:
        """Restored at the threshold with no count of its own recorded.

        Every wait gate's refusal reaches `_record_count_failure`, so
        without the reason check a damaged file would file a
        "cannot count open pull requests" item the first time the inbox
        cap or the factory lock refused.
        """
        path = _streak_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        with pytest.warns(RuntimeWarning):
            streak = OpenPrCountStreak.load(tmp_path)

        result = CycleResult()
        _record_count_failure(tmp_path, streak, result)

        assert result.inbox_items == ()
        assert result.needs_human is False
        assert _open_items(tmp_path) == []


class TestAFailedSaveIsReportedNotRaised:
    """The daemon has no per-cycle handler, so dying over a bookkeeping
    file would be worse than the degraded alarm it causes. The failure is
    still named, with the consequence spelled out."""

    def test_an_unwritable_control_directory_returns_the_reason(
        self,
        tmp_path: Path,
    ) -> None:
        streak = OpenPrCountStreak(consecutive=1)
        path = _streak_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.parent.stat().st_mode)
        os.chmod(path.parent, 0o500)
        try:
            refusal = streak.save(tmp_path)
        finally:
            os.chmod(path.parent, mode)

        assert refusal is not None
        assert str(path) in refusal
        assert "interval" in refusal

    def test_a_clean_save_returns_none(self, tmp_path: Path) -> None:
        """The control for the row above: without it, "returns a string"
        would pass with the save switched off entirely."""
        streak = OpenPrCountStreak(consecutive=2, filed=True)

        assert streak.save(tmp_path) is None
        assert json.loads(_streak_path(tmp_path).read_text(encoding="utf-8")) == {
            "schema_version": STREAK_SCHEMA_VERSION,
            "consecutive": 2,
            "filed": True,
        }

    def test_a_damaged_streak_refuses_to_save(self, tmp_path: Path) -> None:
        path = _streak_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        with pytest.warns(RuntimeWarning):
            streak = OpenPrCountStreak.load(tmp_path)

        refusal = streak.save(tmp_path)

        assert refusal == streak.damaged
        assert path.read_text(encoding="utf-8") == "{"
