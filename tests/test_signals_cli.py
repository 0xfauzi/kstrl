"""End-to-end tests through ``ks signals poll`` (#155, R8.8 slice 1).

Driven through the real entry point against a real captured payload:
``--from-file`` means no socket and no monkeypatch of the network
anywhere in this file. ``isolate_kstrl_state`` (autouse,
``tests/conftest.py:97-133``) already chdirs to ``tmp_path``, clears
every ``KSTRL_*`` variable, and points ``XDG_STATE_HOME`` at a sibling of
``tmp_path`` - no test here sets a state-directory variable itself.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.jsonread import read_json
from kstrl.signals import poll as signals_poll
from kstrl.signals import read_ledger
from kstrl.statedir import CONTROL_SIGNALS, control_file

FIXTURE = Path(__file__).parent / "fixtures" / "signals" / "bugsink-issues-canonical.json"


def _write_toml(root: Path) -> None:
    (root / "kstrl.toml").write_text(
        '[signals]\nenabled = true\nproduct = "demo-product"\n', encoding="utf-8"
    )


def _fixture_document() -> dict[str, Any]:
    document = read_json(FIXTURE.read_bytes().decode("utf-8"))
    assert isinstance(document, dict)
    return document


def _fixture_issue_ids() -> set[str]:
    return {row["id"] for row in _fixture_document()["results"]}


def _page_with(mutations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A deep-ish copy of the captured page with named rows mutated."""
    document = _fixture_document()
    document = json.loads(json.dumps(document))  # deep copy
    for row in document["results"]:
        if row["friendly_id"] in mutations:
            row.update(mutations[row["friendly_id"]])
    return document


def _write_page(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _invoke_poll(root: Path, from_file: Path, *, capture: Path | None = None) -> Any:
    args = ["signals", "poll", "--root", str(root), "--from-file", str(from_file)]
    if capture is not None:
        args += ["--capture", str(capture)]
    return CliRunner().invoke(cli, args, catch_exceptions=True)


def _ledger_records(root: Path) -> list[Any]:
    return list(read_ledger(control_file(root, CONTROL_SIGNALS)).signals)


class TestPollEndToEnd:
    def test_a_captured_page_becomes_ledger_rows_and_a_printed_tally(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)

        result = _invoke_poll(root, FIXTURE)

        assert result.exit_code == 0, result.output
        records = _ledger_records(root)
        assert len(records) == 8
        assert {r.issue_id for r in records} == _fixture_issue_ids()
        assert all(r.schema_version == 1 for r in records)
        assert "8 signals" in result.output
        assert "would_enqueue" in result.output
        # The committed fixture has 8 issues, all new on the first poll,
        # and its largest digested_event_count is 8: both storm figures
        # equal 8 on every row, and the CLI prints both key names.
        assert all(r.poll_new_issues == 8 for r in records)
        assert all(r.poll_max_events_on_a_new_issue == 8 for r in records)
        assert "poll_new_issues" in result.output
        assert "poll_max_events_on_a_new_issue" in result.output

    def test_a_second_poll_of_the_same_page_records_repeats_not_new_issues(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)

        first = _invoke_poll(root, FIXTURE)
        second = _invoke_poll(root, FIXTURE)

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        records = _ledger_records(root)
        assert len(records) == 16
        first_batch, second_batch = records[:8], records[8:]
        assert all(str(r.kind) == "new_issue" for r in first_batch)
        assert all(str(r.kind) == "repeat" for r in second_batch)
        assert not any(str(r.kind) == "new_issue" for r in second_batch)

    def test_a_resolved_key_whose_last_seen_moved_is_a_recurrence(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)

        page_a = _page_with({"DEMO-PRODUCT-1": {"is_resolved": True}})
        page_a_path = tmp_path / "page_a.json"
        _write_page(page_a_path, page_a)

        moved_last_seen = (
            (
                datetime.fromisoformat(page_a["results"][0]["last_seen"].replace("Z", "+00:00"))
                + timedelta(hours=1)
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        page_c = _page_with(
            {"DEMO-PRODUCT-1": {"is_resolved": False, "last_seen": moved_last_seen}}
        )
        page_c_path = tmp_path / "page_c.json"
        _write_page(page_c_path, page_c)

        result_a = _invoke_poll(root, page_a_path)
        result_b = _invoke_poll(root, page_a_path)  # byte-for-byte the same page
        result_c = _invoke_poll(root, page_c_path)
        # A fourth poll, byte-for-byte the same as C, is what makes "latest
        # wins" observable: A and B are identical, so a lookup of the
        # ledger's FIRST row for this key and its LAST row agree by C's
        # time (both point at A-or-B's identical resolved/last_seen) and
        # this test would pass under either rule without it. By D's time
        # the ledger holds two distinct shapes (A/B, then C), so a
        # first-wins lookup would hand classify() the STALE resolved=true
        # row and misclassify an ordinary repeat as a second recurrence.
        result_d = _invoke_poll(root, page_c_path)

        assert result_a.exit_code == 0, result_a.output
        assert result_b.exit_code == 0, result_b.output
        assert result_c.exit_code == 0, result_c.output
        assert result_d.exit_code == 0, result_d.output

        records = _ledger_records(root)
        by_batch = [records[0:8], records[8:16], records[16:24], records[24:32]]

        def _row_for(batch: list[Any]) -> Any:
            return next(r for r in batch if r.friendly_id == "DEMO-PRODUCT-1")

        assert str(_row_for(by_batch[1]).kind) == "repeat"
        row_c = _row_for(by_batch[2])
        assert str(row_c.kind) == "recurrence"
        assert str(row_c.disposition) == "would_enqueue"
        assert str(_row_for(by_batch[3]).kind) == "repeat"

        recurrences = [r for batch in by_batch for r in batch if str(r.kind) == "recurrence"]
        assert len(recurrences) == 1
        assert recurrences[0].friendly_id == "DEMO-PRODUCT-1"

    def test_the_recurrence_rule_reads_the_payload_clock_not_the_wall_clock(
        self, tmp_path: Path
    ) -> None:
        """Calls ``poll()`` directly (still the CLI's own function, still
        driven off a real captured page) so the wall clock can be forced
        a day ahead - a knob the CLI itself does not expose."""
        from kstrl.signals import SignalsConfig

        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)
        config = SignalsConfig.load(root)

        page_a = _page_with({"DEMO-PRODUCT-1": {"is_resolved": True}})
        page_a_path = tmp_path / "page_a.json"
        _write_page(page_a_path, page_a)
        page_b = _page_with({"DEMO-PRODUCT-1": {"is_resolved": False}})
        page_b_path = tmp_path / "page_b.json"
        _write_page(page_b_path, page_b)

        signals_poll(root, config, from_file=page_a_path)
        report = signals_poll(
            root,
            config,
            from_file=page_b_path,
            now=lambda: datetime.now(UTC) + timedelta(days=1),
        )

        row = next(s for s in report.signals if s.friendly_id == "DEMO-PRODUCT-1")
        assert str(row.kind) == "repeat"

    def test_a_capture_flag_writes_the_raw_body(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)
        out = tmp_path / "captured.json"

        result = _invoke_poll(root, FIXTURE, capture=out)

        assert result.exit_code == 0, result.output
        assert read_json(out.read_text(encoding="utf-8")) == _fixture_document()

    def test_the_ledger_is_pure_ascii_for_a_non_ascii_error_value(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)
        curly_quote = "cannot’t process order"
        page = _page_with({"DEMO-PRODUCT-1": {"calculated_value": curly_quote}})
        page_path = tmp_path / "page.json"
        _write_page(page_path, page)

        result = _invoke_poll(root, page_path)

        assert result.exit_code == 0, result.output
        ledger_path = control_file(root, CONTROL_SIGNALS)
        ledger_path.read_bytes().decode("ascii")  # raises if not pure ASCII
        records = _ledger_records(root)
        row = next(r for r in records if r.friendly_id == "DEMO-PRODUCT-1")
        assert row.error_value == curly_quote


def _dead_port() -> int:
    """A TCP port nothing is listening on: bind, read the assigned port,
    close. A connection attempt to it fails immediately with a real
    ``ConnectionRefusedError`` rather than a network timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestPollRefusesLoudly:
    def test_disabled_refuses_before_opening_a_socket(self, tmp_path: Path) -> None:
        """#155 fix round A1: `[signals] enabled` was documented as the off
        switch and gated nothing - with enabled=false, poll still opened
        the socket (a dead port proves the attempt: Connection refused)
        and wrote ledger rows. The refusal must land before either."""
        root = tmp_path / "project"
        root.mkdir()
        (root / "kstrl.toml").write_text(
            '[signals]\nenabled = false\nproduct = "demo-product"\n'
            f'base_url = "http://127.0.0.1:{_dead_port()}"\n',
            encoding="utf-8",
        )

        result = CliRunner().invoke(
            cli, ["signals", "poll", "--root", str(root)], catch_exceptions=True
        )

        assert result.exit_code == 1, result.output
        assert "enabled" in result.output
        assert not control_file(root, CONTROL_SIGNALS).exists()

    def test_disabled_refuses_a_from_file_replay_too(self, tmp_path: Path) -> None:
        """The bug measured: `--from-file` wrote 8 ledger rows and exited 0
        with enabled=false, because the replay path never checked the
        key either."""
        root = tmp_path / "project"
        root.mkdir()
        (root / "kstrl.toml").write_text(
            '[signals]\nenabled = false\nproduct = "demo-product"\n', encoding="utf-8"
        )

        result = _invoke_poll(root, FIXTURE)

        assert result.exit_code == 1, result.output
        assert "enabled" in result.output
        assert not control_file(root, CONTROL_SIGNALS).exists()

    def test_an_unparseable_kstrl_toml_is_exit_1_not_a_traceback(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "kstrl.toml").write_bytes(b"[signals\n")

        result = CliRunner().invoke(
            cli, ["signals", "poll", "--root", str(root)], catch_exceptions=True
        )

        assert result.exit_code == 1, result.output
        assert "error:" in result.output
        assert "kstrl.toml" in result.output
        assert not isinstance(result.exception, ValueError)


class TestAnIncompleteLedgerLineIsARefusalWithACountNeverATraceback:
    """#155 fix round A2c, driven through the real CLI. The old
    ``Signal.from_dict`` subscripted every field, so a valid-JSON-but-
    incomplete line raised ``KeyError`` and the ledger is append-only -
    the bad line bricked both ``poll`` and ``ls`` permanently."""

    def _seed_incomplete_ledger_line(self, root: Path) -> None:
        from kstrl.statedir import ensure_control_state

        ensure_control_state(root)
        path = control_file(root, CONTROL_SIGNALS)
        path.write_text(
            json.dumps({"schema_version": 1, "issue_id": "old"}) + "\n", encoding="utf-8"
        )

    def test_poll_exits_0_and_reports_the_drop(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)
        self._seed_incomplete_ledger_line(root)

        result = _invoke_poll(root, FIXTURE)

        assert result.exit_code == 0, result.output
        assert "dropped 1" in result.output
        # The incomplete line is dropped, not stored: 8 new rows only.
        records = _ledger_records(root)
        assert len(records) == 8
        assert all(str(r.kind) == "new_issue" for r in records)

    def test_ls_exits_0_and_reports_the_drop(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)
        self._seed_incomplete_ledger_line(root)

        result = CliRunner().invoke(
            cli, ["signals", "ls", "--root", str(root)], catch_exceptions=True
        )

        assert result.exit_code == 0, result.output
        assert "1 ledger row(s) could not be read" in result.output
        assert "No signals recorded yet." in result.output

    def test_ls_reports_the_drop_alongside_the_readable_rows(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        _write_toml(root)
        poll_result = _invoke_poll(root, FIXTURE)
        assert poll_result.exit_code == 0, poll_result.output
        path = control_file(root, CONTROL_SIGNALS)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema_version": 1, "issue_id": "old"}) + "\n")

        result = CliRunner().invoke(
            cli, ["signals", "ls", "--root", str(root)], catch_exceptions=True
        )

        assert result.exit_code == 0, result.output
        assert "dropped" in result.output
        assert "1" in result.output
