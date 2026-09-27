"""The signals ledger read over real files (#155, R8.8 slice 1).

``read_ledger`` over ``signals.jsonl`` on disk: a ledger that is not
utf-8 raises rather than reading empty; a missing ledger reads empty; a
torn tail line, a valid-JSON line missing a required field (#155 fix
round A2c, the ``KeyError`` that used to brick the append-only ledger
permanently), a line missing ``issue_id`` and a line with an invalid
``kind`` are each dropped and counted, never raised. The good lines are
built from the captured Bugsink page in ``tests/fixtures/signals``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kstrl.jsonread import read_json
from kstrl.signals import normalise_bugsink, read_ledger

FIXTURE = Path(__file__).parent / "fixtures" / "signals" / "bugsink-issues-canonical.json"


def _rows() -> list[dict[str, Any]]:
    document = read_json(FIXTURE.read_bytes().decode("utf-8"))
    assert isinstance(document, dict)
    results = document["results"]
    assert isinstance(results, list)
    return results


class TestTheLedgerRefuses:
    def test_an_unreadable_ledger_raises_rather_than_reading_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "signals.jsonl"
        path.write_bytes(b"\xff\xfe not utf-8")

        with pytest.raises(ValueError):
            read_ledger(path)

    def test_a_missing_ledger_reads_empty(self, tmp_path: Path) -> None:
        ledger = read_ledger(tmp_path / "signals.jsonl")
        assert ledger.signals == ()
        assert ledger.dropped == 0

    def test_a_torn_tail_line_is_skipped_and_counted(self, tmp_path: Path) -> None:
        path = tmp_path / "signals.jsonl"
        signal = normalise_bugsink(_rows()[0], product="p")
        assert signal is not None
        good_line = json.dumps(signal.to_dict())
        torn = '{"schema_version": 1, "issue_id"'  # truncated: valid utf-8, invalid JSON
        path.write_text(good_line + "\n" + good_line + "\n" + torn, encoding="utf-8")

        ledger = read_ledger(path)

        assert len(ledger.signals) == 2
        assert ledger.dropped == 1

    def test_a_valid_json_line_missing_a_required_field_is_dropped_and_counted(
        self, tmp_path: Path
    ) -> None:
        """#155 fix round A2c: the regression this guards against.
        ``Signal.from_dict`` used to subscript every field, so a
        valid-JSON-but-incomplete line raised ``KeyError`` here and the
        ledger is append-only - the bad line bricks every later poll and
        ls permanently. Now it is dropped and counted, like a torn line."""
        path = tmp_path / "signals.jsonl"
        signal = normalise_bugsink(_rows()[0], product="p")
        assert signal is not None
        good_line = json.dumps(signal.to_dict())
        incomplete = json.dumps({"schema_version": 1, "issue_id": "some-id"})
        path.write_text(good_line + "\n" + incomplete + "\n" + good_line + "\n", encoding="utf-8")

        ledger = read_ledger(path)

        assert len(ledger.signals) == 2
        assert ledger.dropped == 1

    def test_a_line_missing_issue_id_is_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "signals.jsonl"
        signal = normalise_bugsink(_rows()[0], product="p")
        assert signal is not None
        data = signal.to_dict()
        del data["issue_id"]
        path.write_text(json.dumps(data) + "\n", encoding="utf-8")

        ledger = read_ledger(path)

        assert ledger.signals == ()
        assert ledger.dropped == 1

    def test_a_line_with_an_invalid_kind_is_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "signals.jsonl"
        signal = normalise_bugsink(_rows()[0], product="p")
        assert signal is not None
        data = signal.to_dict()
        data["kind"] = "not_a_real_kind"
        path.write_text(json.dumps(data) + "\n", encoding="utf-8")

        ledger = read_ledger(path)

        assert ledger.signals == ()
        assert ledger.dropped == 1
