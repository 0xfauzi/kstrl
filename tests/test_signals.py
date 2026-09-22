"""Unit tests over the pure and near-pure parts of kstrl/signals.py (#155).

R8.8 slice 1: observe and record, never enqueue. These tests call
``kstrl.signals`` functions directly, with no CLI and no real network -
``TestFetchRefuses`` and ``TestTheTokenIsNeverInAMessage`` monkeypatch
``urllib.request.urlopen`` itself.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kstrl.jsonread import read_json
from kstrl.signals import (
    Disposition,
    Signal,
    SignalKind,
    SignalsConfig,
    SignalsError,
    classify,
    fetch_bugsink,
    normalise_bugsink,
    read_ledger,
)

FIXTURE = Path(__file__).parent / "fixtures" / "signals" / "bugsink-issues-canonical.json"


def _rows() -> list[dict[str, Any]]:
    document = read_json(FIXTURE.read_bytes().decode("utf-8"))
    assert isinstance(document, dict)
    results = document["results"]
    assert isinstance(results, list)
    return results


def _config(**overrides: Any) -> SignalsConfig:
    return SignalsConfig(
        enabled=True,
        product="demo-product",
        base_url="http://127.0.0.1:9999",
        project_id="1",
        **overrides,
    )


class TestNormaliseBugsink:
    def test_the_captured_page_normalises_field_for_field(self) -> None:
        row = _rows()[0]

        signal = normalise_bugsink(row, product="demo-product")

        assert signal is not None
        assert signal.issue_id == "542994d2-e596-4ed6-b6a8-4c293dd67602"
        assert signal.friendly_id == "DEMO-PRODUCT-1"
        assert signal.event_count == 8
        assert signal.resolved is False
        assert signal.error_type == "ValueError"
        assert signal.error_value == "cart total went negative: 3"
        assert signal.last_seen == "2026-09-21T23:31:01.034359Z"
        assert signal.release is None

    def test_a_row_with_no_id_is_dropped_not_defaulted(self) -> None:
        row = {k: v for k, v in _rows()[0].items() if k != "id"}

        assert normalise_bugsink(row, product="p") is None

    def test_an_absent_count_is_a_refusal_not_a_zero(self) -> None:
        row = {k: v for k, v in _rows()[0].items() if k != "digested_event_count"}

        assert normalise_bugsink(row, product="p") is None


class TestFetchRefuses:
    def test_a_non_list_results_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_SIGNALS_TOKEN", "tok-" + "0" * 36)
        body = json.dumps({"results": {"a": 1}}).encode("utf-8")

        class _FakeResponse:
            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def read(self) -> bytes:
                return body

        def _fake_urlopen(*args: object, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        import kstrl.signals as signals_mod

        monkeypatch.setattr(signals_mod.urllib.request, "urlopen", _fake_urlopen)

        with pytest.raises(SignalsError):
            fetch_bugsink(_config())

    def test_a_transport_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_SIGNALS_TOKEN", "tok-" + "0" * 36)

        def _raise_urlopen(*args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("boom")

        import kstrl.signals as signals_mod

        monkeypatch.setattr(signals_mod.urllib.request, "urlopen", _raise_urlopen)

        with pytest.raises(SignalsError, match="boom"):
            fetch_bugsink(_config())

    def test_a_read_timeout_is_a_signals_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_SIGNALS_TOKEN", "tok-" + "0" * 36)

        def _raise_timeout(*args: object, **kwargs: object) -> None:
            raise TimeoutError("timed out")

        import kstrl.signals as signals_mod

        monkeypatch.setattr(signals_mod.urllib.request, "urlopen", _raise_timeout)

        with pytest.raises(SignalsError, match="timed out"):
            fetch_bugsink(_config())


class TestTheTokenIsNeverInAMessage:
    def test_a_401_names_the_status_and_not_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "tok-" + "0" * 36
        monkeypatch.setenv("KSTRL_SIGNALS_TOKEN", token)

        def _raise_401(*args: object, **kwargs: object) -> None:
            raise urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

        import kstrl.signals as signals_mod

        monkeypatch.setattr(signals_mod.urllib.request, "urlopen", _raise_401)

        with pytest.raises(SignalsError) as excinfo:
            fetch_bugsink(_config())

        assert "401" in str(excinfo.value)
        assert token not in str(excinfo.value)

    def test_an_unset_token_names_the_variable(self) -> None:
        with pytest.raises(SignalsError, match="KSTRL_SIGNALS_TOKEN"):
            fetch_bugsink(_config())


class TestTheLedgerRefuses:
    def test_an_unreadable_ledger_raises_rather_than_reading_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "signals.jsonl"
        path.write_bytes(b"\xff\xfe not utf-8")

        with pytest.raises(ValueError):
            read_ledger(path)

    def test_a_missing_ledger_reads_empty(self, tmp_path: Path) -> None:
        assert read_ledger(tmp_path / "signals.jsonl") == []

    def test_a_torn_tail_line_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "signals.jsonl"
        signal = normalise_bugsink(_rows()[0], product="p")
        assert signal is not None
        good_line = json.dumps(signal.to_dict())
        torn = '{"schema_version": 1, "issue_id"'  # truncated: valid utf-8, invalid JSON
        path.write_text(good_line + "\n" + good_line + "\n" + torn, encoding="utf-8")

        records = read_ledger(path)

        assert len(records) == 2


class TestTheStormCountIsEvents:
    def test_one_issue_with_five_events_and_five_issues_with_one_are_told_apart(
        self,
    ) -> None:
        import kstrl.signals as signals_mod

        rows = _rows()
        by_friendly = {r["friendly_id"]: r for r in rows}
        page_a = [by_friendly["DEMO-PRODUCT-3"]]
        page_b = [by_friendly[f"DEMO-PRODUCT-{n}"] for n in range(4, 9)]

        classified_a, dropped_a = signals_mod._classify_rows(page_a, {}, _config())
        classified_b, dropped_b = signals_mod._classify_rows(page_b, {}, _config())

        assert dropped_a == 0
        assert dropped_b == 0
        new_a, max_a = signals_mod._storm_figures(classified_a)
        new_b, max_b = signals_mod._storm_figures(classified_b)

        assert (new_a, max_a) == (1, 5)
        assert (new_b, max_b) == (5, 1)


class TestClassify:
    @pytest.mark.parametrize(
        (
            "previous",
            "event_count",
            "prev_event_count",
            "resolved_prev",
            "last_seen_moved",
            "expected",
        ),
        [
            (False, 5, 0, False, False, (SignalKind.NEW_ISSUE, Disposition.WOULD_ENQUEUE)),
            (False, 1, 0, False, False, (SignalKind.NEW_ISSUE, Disposition.WOULD_WATCH)),
            (True, 5, 5, True, True, (SignalKind.RECURRENCE, Disposition.WOULD_ENQUEUE)),
            (True, 20, 1, False, False, (SignalKind.REPEAT, Disposition.WOULD_NOTIFY)),
            (False, 3, 0, False, False, (SignalKind.NEW_ISSUE, Disposition.WOULD_ENQUEUE)),
            (True, 11, 1, False, False, (SignalKind.REPEAT, Disposition.WOULD_NOTIFY)),
        ],
        ids=[
            "new-enqueue",
            "new-watch",
            "recurrence",
            "repeat-notify",
            "new-at-the-threshold",
            "repeat-growth-at-the-threshold",
        ],
    )
    def test_each_ladder_branch(
        self,
        previous: bool,
        event_count: int,
        prev_event_count: int,
        resolved_prev: bool,
        last_seen_moved: bool,
        expected: tuple[SignalKind, Disposition],
    ) -> None:
        base = normalise_bugsink(_rows()[0], product="p")
        assert base is not None
        signal = _with(base, event_count=event_count, last_seen="2026-01-02T00:00:00Z")
        prev = (
            _with(
                base,
                event_count=prev_event_count,
                resolved=resolved_prev,
                last_seen=("2026-01-01T00:00:00Z" if last_seen_moved else "2026-01-02T00:00:00Z"),
            )
            if previous
            else None
        )

        result = classify(signal, prev, new_issue_events=3, repeat_growth_events=10)

        assert result == expected


def _with(signal: Signal, **overrides: Any) -> Signal:
    from dataclasses import replace

    return replace(signal, **overrides)
