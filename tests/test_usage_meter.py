"""R3.1 cost meter tests.

Covers the four required behaviors:
1. A fake agent emitting realistic usage events produces correct rollup
   math (adapter extraction, loop aggregation, factory attribution).
2. Missing usage degrades to call counts + wall time (CustomAgent /
   codex-without-trailer fallback).
3. The max_total_tokens budget halt fires LOUDLY and is recorded
   (synthetic finding, progress-log event, FAILED component).
4. Malformed usage never raises - the meter must not gate correctness.

The "realistic" fixtures are verbatim from the R3.1 measurement probes:
claude CLI 2.1.214 stream-json result event, codex CLI 0.134.0 plain
"tokens used" trailer.
"""

from __future__ import annotations

import io
import json
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from kstrl.agents.base import UsageRecord, UsageTotals, collect_usage
from kstrl.agents.claude_code import ClaudeCodeAgent, _usage_from_result_event
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.config import KstrlConfig
from kstrl.events import RunPaths
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    _clear_partial_usage,
    _format_usage_rollup,
    _read_partial_usage,
    _run_component,
    _salvage_aborted_usage,
    _write_partial_usage,
    run_factory,
)
from kstrl.inbox import Inbox, InboxConfig
from kstrl.loop import COMPLETION_MARKER, LoopBudget, run_loop
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.observability import ProgressLog
from kstrl.shutdown import StopController
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

# Verbatim (trimmed to relevant fields) from the measurement probe:
# `claude --print --output-format stream-json --verbose` on CLI 2.1.214.
CLAUDE_RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1778,
    "duration_api_ms": 1550,
    "num_turns": 1,
    "result": "hello",
    "total_cost_usd": 0.0227028,
    "usage": {
        "input_tokens": 9,
        "cache_creation_input_tokens": 10371,
        "cache_read_input_tokens": 17418,
        "output_tokens": 42,
        "service_tier": "standard",
    },
}


def _claude_record() -> UsageRecord:
    return _usage_from_result_event(json.dumps(CLAUDE_RESULT_EVENT), 99.0)


# ---------------------------------------------------------------------------
# UsageTotals rollup math
# ---------------------------------------------------------------------------


class TestUsageTotalsMath:
    def test_claude_style_record_rollup(self) -> None:
        totals = UsageTotals()
        totals.add_record(_claude_record())
        totals.add_record(_claude_record())
        assert totals.calls == 2
        assert totals.known_calls == 2
        assert totals.unreported_calls == 0
        assert totals.input_tokens == 18
        assert totals.output_tokens == 84
        assert totals.cache_read_tokens == 2 * 17418
        assert totals.cache_creation_tokens == 2 * 10371
        assert totals.total_tokens == 2 * (9 + 42 + 17418 + 10371)
        assert totals.cost_usd == pytest.approx(2 * 0.0227028)

    def test_codex_style_total_only_record(self) -> None:
        totals = UsageTotals()
        totals.add_record(UsageRecord(
            total_tokens=14511, duration_seconds=3.0, source="codex-text",
        ))
        assert totals.calls == 1
        assert totals.known_calls == 1
        assert totals.total_tokens == 14511
        assert totals.input_tokens == 0  # not reported, stays zero
        assert totals.cost_usd == 0.0

    def test_unavailable_record_is_calls_plus_wall_time(self) -> None:
        totals = UsageTotals()
        totals.add_record(UsageRecord(duration_seconds=2.5))
        assert totals.calls == 1
        assert totals.known_calls == 0
        assert totals.unreported_calls == 1
        assert totals.total_tokens == 0
        assert totals.duration_seconds == pytest.approx(2.5)

    def test_partial_record_derives_total_from_parts(self) -> None:
        totals = UsageTotals()
        totals.add_record(UsageRecord(input_tokens=100, output_tokens=50))
        assert totals.total_tokens == 150

    def test_merge(self) -> None:
        a = UsageTotals()
        a.add_record(_claude_record())
        b = UsageTotals()
        b.add_record(UsageRecord(total_tokens=1000, duration_seconds=1.0))
        a.merge(b)
        assert a.calls == 2
        assert a.total_tokens == (9 + 42 + 17418 + 10371) + 1000
        assert a.known_calls == 2
        assert a.token_calls == 2

    def test_cost_only_record_is_known_but_tokenless(self) -> None:
        """Review regression (P1-b): ``known_calls`` is not token evidence.

        ``add_record`` sets ``known`` for a COST figure alone, and the
        claude adapter reports ``total_cost_usd`` even when the ``usage``
        dict is absent or drifted. Two such records therefore look like
        perfect coverage (calls == known_calls) while ``total_tokens``
        stays 0 forever - so anything gating on a TOKEN ceiling has to
        read ``token_calls``, which counts only calls that reported an
        actual token figure.

        Built with the real ``add_record``, not a hand-assembled totals
        object: the defect lives in that method's ``known`` flag.
        """
        totals = UsageTotals()
        totals.add_record(UsageRecord(
            cost_usd=0.0227028, duration_seconds=1.8,
            source="claude-stream-json",
        ))
        totals.add_record(UsageRecord(
            cost_usd=0.0104, duration_seconds=2.1,
            source="claude-stream-json",
        ))
        assert totals.calls == 2
        assert totals.known_calls == 2      # unchanged semantics
        assert totals.unreported_calls == 0  # unchanged semantics
        assert totals.token_calls == 0       # the honest token coverage
        assert totals.tokenless_calls == 2
        assert totals.total_tokens == 0
        assert totals.cost_usd == pytest.approx(0.0331028)

    def test_token_calls_counts_only_token_bearing_records(self) -> None:
        totals = UsageTotals()
        totals.add_record(_claude_record())                        # parts
        totals.add_record(UsageRecord(total_tokens=10))            # total
        totals.add_record(UsageRecord(cost_usd=0.5))               # cost
        totals.add_record(UsageRecord(duration_seconds=1.0))       # silence
        assert totals.calls == 4
        assert totals.known_calls == 3
        assert totals.token_calls == 2
        assert totals.tokenless_calls == 2
        assert totals.unreported_calls == 1

    def test_token_calls_round_trips_through_to_dict(self) -> None:
        """Serialized so the audit trail keeps the distinction; readers
        of older payloads see the key missing and decode it as 0."""
        totals = UsageTotals()
        totals.add_record(_claude_record())
        totals.add_record(UsageRecord(cost_usd=0.5))
        assert totals.to_dict()["token_calls"] == 1
        assert totals.to_dict()["known_calls"] == 2

    def test_cost_calls_counts_only_cost_bearing_records(self) -> None:
        """The mirror of ``token_calls``, and independent of it: a cost
        ceiling has to know whether a COST was reported, which
        ``known_calls`` cannot answer either."""
        totals = UsageTotals()
        totals.add_record(_claude_record())                        # both
        totals.add_record(UsageRecord(total_tokens=10))            # tokens
        totals.add_record(UsageRecord(cost_usd=0.5))               # cost
        totals.add_record(UsageRecord(duration_seconds=1.0))       # silence
        assert totals.calls == 4
        assert totals.known_calls == 3
        assert totals.token_calls == 2
        assert totals.cost_calls == 2
        assert totals.costless_calls == 2
        assert totals.tokenless_calls == 2

    def test_token_only_record_is_known_but_costless(self) -> None:
        """The converse of the P1-b case, and just as real: codex
        reports a token total and never a cost, so ``known_calls`` says
        perfect coverage for a COST ceiling that can never advance."""
        totals = UsageTotals()
        totals.add_record(UsageRecord(total_tokens=14511, source="codex-text"))
        totals.add_record(UsageRecord(total_tokens=9000, source="codex-text"))
        assert totals.known_calls == 2       # the misleading signal
        assert totals.cost_calls == 0        # the honest one
        assert totals.costless_calls == 2
        assert totals.cost_usd == 0.0

    def test_a_reported_cost_of_zero_still_counts_as_coverage(self) -> None:
        """0.0 is a REPORT, not silence: the adapter told us this call
        cost nothing. Treating it as no-coverage would condemn a working
        adapter."""
        totals = UsageTotals()
        totals.add_record(UsageRecord(cost_usd=0.0, source="claude-stream-json"))
        assert totals.cost_calls == 1
        assert totals.costless_calls == 0

    def test_cost_calls_round_trips_through_to_dict_and_merge(self) -> None:
        a = UsageTotals()
        a.add_record(_claude_record())
        b = UsageTotals()
        b.add_record(UsageRecord(total_tokens=1000))
        a.merge(b)
        assert a.cost_calls == 1
        assert a.to_dict()["cost_calls"] == 1

    def test_malformed_records_never_raise(self) -> None:
        totals = UsageTotals()
        for garbage in (None, "junk", 42, object(), {"input_tokens": 5}):
            totals.add_record(garbage)
        # Every entry still counted as a call; nothing reported.
        assert totals.calls == 5
        assert totals.known_calls == 0
        assert totals.total_tokens == 0

    def test_bool_and_negative_values_rejected(self) -> None:
        totals = UsageTotals()
        totals.add_record(UsageRecord(
            input_tokens=True,  # type: ignore[arg-type]
            output_tokens=-5,
            cost_usd=-1.0,
        ))
        assert totals.known_calls == 0
        assert totals.input_tokens == 0
        assert totals.output_tokens == 0
        assert totals.cost_usd == 0.0

    def test_collect_usage_without_attribute(self) -> None:
        totals = collect_usage(object())
        assert totals.calls == 0

    def test_collect_usage_with_non_iterable_attribute(self) -> None:
        class Broken:
            usage_records = 42

        totals = collect_usage(Broken())
        assert totals.calls == 0


# ---------------------------------------------------------------------------
# Claude adapter extraction (measured stream-json result event)
# ---------------------------------------------------------------------------


class TestClaudeUsageExtraction:
    def test_measured_result_event_parses(self) -> None:
        record = _claude_record()
        assert record.source == "claude-stream-json"
        assert record.input_tokens == 9
        assert record.output_tokens == 42
        assert record.cache_read_tokens == 17418
        assert record.cache_creation_tokens == 10371
        assert record.total_tokens == 9 + 42 + 17418 + 10371
        assert record.cost_usd == pytest.approx(0.0227028)
        assert record.duration_seconds == pytest.approx(1.778)

    def test_missing_event_records_unavailable(self) -> None:
        record = _usage_from_result_event(None, 5.0)
        assert record.source == "unavailable"
        assert record.total_tokens is None
        assert record.duration_seconds == pytest.approx(5.0)

    def test_malformed_json_records_parse_error_and_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, "kstrl.agents.claude_code"):
            record = _usage_from_result_event("{not json", 5.0)
        assert record.source == "parse-error"
        assert record.total_tokens is None
        assert any("usage" in r.message for r in caplog.records)

    def test_event_without_usage_dict_warns_not_raises(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, "kstrl.agents.claude_code"):
            record = _usage_from_result_event(
                json.dumps({"type": "result", "result": "hi"}), 5.0,
            )
        assert record.source == "parse-error"
        assert caplog.records

    def test_drifted_usage_types_record_unknown_fields(self) -> None:
        evt = {
            "type": "result",
            "total_cost_usd": 0.5,
            "usage": {"input_tokens": "many", "output_tokens": 10},
        }
        record = _usage_from_result_event(json.dumps(evt), 1.0)
        assert record.input_tokens is None
        assert record.output_tokens == 10
        assert record.total_tokens == 10
        assert record.cost_usd == pytest.approx(0.5)

    def test_agent_run_appends_record_from_stream(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "working"},
            ]}}) + "\n",
            json.dumps(CLAUDE_RESULT_EVENT) + "\n",
        ])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            agent = ClaudeCodeAgent()
            list(agent.run("prompt", cwd=tmp_path))

        assert len(agent.usage_records) == 1
        assert agent.usage_records[0].source == "claude-stream-json"
        assert agent.usage_records[0].total_tokens == 9 + 42 + 17418 + 10371

    def test_agent_run_without_result_event_records_unavailable(
        self, tmp_path: Path,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter(["hello\n", "world\n"])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            agent = ClaudeCodeAgent()
            list(agent.run("prompt", cwd=tmp_path))

        assert len(agent.usage_records) == 1
        assert agent.usage_records[0].source == "unavailable"

    def test_records_accumulate_across_runs(self, tmp_path: Path) -> None:
        agent = ClaudeCodeAgent()
        for _ in range(2):
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = iter([json.dumps(CLAUDE_RESULT_EVENT) + "\n"])
            mock_proc.wait.return_value = 0
            with patch("subprocess.Popen", return_value=mock_proc):
                list(agent.run("prompt", cwd=tmp_path))
        assert len(agent.usage_records) == 2


# ---------------------------------------------------------------------------
# Codex adapter extraction (measured plain-text trailer)
# ---------------------------------------------------------------------------


def _run_codex_with_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: list[str],
) -> CodexAgent:
    monkeypatch.setattr(CodexAgent, "_supports_output_last_message", False)
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter(lines)
    mock_proc.wait.return_value = 0
    with patch("subprocess.Popen", return_value=mock_proc):
        agent = CodexAgent()
        list(agent.run("prompt", cwd=tmp_path))
    return agent


class TestCodexUsageExtraction:
    def test_measured_two_line_trailer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # Verbatim tail of the codex 0.134.0 probe output.
        agent = _run_codex_with_stdout(monkeypatch, tmp_path, [
            "codex\n", "hello\n", "tokens used\n", "14,511\n", "hello\n",
        ])
        assert len(agent.usage_records) == 1
        record = agent.usage_records[0]
        assert record.source == "codex-text"
        assert record.total_tokens == 14511
        assert record.input_tokens is None  # codex reports only a total

    def test_single_line_trailer_variant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        agent = _run_codex_with_stdout(monkeypatch, tmp_path, [
            "hello\n", "tokens used: 1,234\n",
        ])
        assert agent.usage_records[0].total_tokens == 1234

    def test_no_trailer_falls_back_to_calls_plus_wall_time(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        agent = _run_codex_with_stdout(monkeypatch, tmp_path, ["hello\n"])
        assert len(agent.usage_records) == 1
        record = agent.usage_records[0]
        assert record.source == "unavailable"
        assert record.total_tokens is None
        assert record.duration_seconds >= 0.0

    def test_non_numeric_after_tokens_used_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        agent = _run_codex_with_stdout(monkeypatch, tmp_path, [
            "tokens used\n", "not a number\n",
        ])
        assert agent.usage_records[0].total_tokens is None

    def test_last_trailer_wins_over_echoed_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        agent = _run_codex_with_stdout(monkeypatch, tmp_path, [
            "tokens used\n", "111\n", "more output\n",
            "tokens used\n", "222\n",
        ])
        assert agent.usage_records[0].total_tokens == 222


# ---------------------------------------------------------------------------
# CustomAgent fallback
# ---------------------------------------------------------------------------


class TestCustomAgentFallback:
    def test_records_calls_and_wall_time_only(self, tmp_path: Path) -> None:
        agent = CustomAgent("echo hi")
        list(agent.run("prompt", cwd=tmp_path))
        list(agent.run("prompt", cwd=tmp_path))
        assert len(agent.usage_records) == 2
        assert all(r.source == "unavailable" for r in agent.usage_records)
        totals = collect_usage(agent)
        assert totals.calls == 2
        assert totals.known_calls == 0
        assert totals.total_tokens == 0


# ---------------------------------------------------------------------------
# Engineer-loop aggregation (run_loop -> LoopResult.usage)
# ---------------------------------------------------------------------------


class FakeUsageAgent:
    """Protocol-satisfying fake that emits one usage record per run."""

    def __init__(
        self,
        outputs: list[list[str]],
        record: UsageRecord | None = None,
        records: Any = None,
    ) -> None:
        self._outputs = outputs
        self._record = record
        self._runs = 0
        self._final_message: str | None = None
        # `records` overrides accumulation for malformed-usage tests.
        self._forced_records = records
        self._usage_records: list[UsageRecord] = []

    @property
    def name(self) -> str:
        return "fake-usage"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        output = self._outputs[min(self._runs, len(self._outputs) - 1)]
        self._runs += 1
        if self._record is not None:
            self._usage_records.append(self._record)
        yield from output
        self._final_message = output[-1] if output else None

    @property
    def final_message(self) -> str | None:
        return self._final_message

    @property
    def usage_records(self) -> Any:
        if self._forced_records is not None:
            return self._forced_records
        return list(self._usage_records)


def _loop_config(tmp_path: Path, max_iterations: int) -> KstrlConfig:
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    return KstrlConfig(
        max_iterations=max_iterations,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        kstrl_branch="",
        kstrl_branch_explicit=True,
    )


class TestLoopUsageAggregation:
    def test_two_iterations_sum_correctly(self, tmp_path: Path) -> None:
        record = UsageRecord(
            input_tokens=100, output_tokens=200, total_tokens=300,
            cost_usd=0.01, duration_seconds=1.0, source="claude-stream-json",
        )
        agent = FakeUsageAgent(
            outputs=[["working..."], [COMPLETION_MARKER]], record=record,
        )
        result = run_loop(
            _loop_config(tmp_path, 5), PlainUI(no_color=True), agent, tmp_path,
        )
        assert result.completed is True
        assert result.iterations == 2
        assert result.usage.calls == 2
        assert result.usage.input_tokens == 200
        assert result.usage.output_tokens == 400
        assert result.usage.total_tokens == 600
        assert result.usage.cost_usd == pytest.approx(0.02)

    def test_usage_present_on_max_iterations_failure(
        self, tmp_path: Path,
    ) -> None:
        record = UsageRecord(total_tokens=50, source="codex-text")
        agent = FakeUsageAgent(outputs=[["no marker"]], record=record)
        result = run_loop(
            _loop_config(tmp_path, 3), PlainUI(no_color=True), agent, tmp_path,
        )
        assert result.completed is False
        assert result.usage.calls == 3
        assert result.usage.total_tokens == 150

    def test_agent_without_usage_records_yields_empty_totals(
        self, tmp_path: Path,
    ) -> None:
        class BareAgent:
            name = "bare"
            final_message = None

            def run(
                self, prompt: str, cwd: Path | None = None,
                timeout: float | None = None,
            ) -> Iterator[str]:
                yield COMPLETION_MARKER

        result = run_loop(
            _loop_config(tmp_path, 1), PlainUI(no_color=True),
            BareAgent(), tmp_path,
        )
        assert result.completed is True
        assert result.usage.calls == 0

    def test_malformed_usage_records_never_crash_the_loop(
        self, tmp_path: Path,
    ) -> None:
        agent = FakeUsageAgent(
            outputs=[[COMPLETION_MARKER]],
            records=[None, "garbage", 42],
        )
        result = run_loop(
            _loop_config(tmp_path, 1), PlainUI(no_color=True), agent, tmp_path,
        )
        assert result.completed is True
        assert result.usage.calls == 3
        assert result.usage.known_calls == 0


# ---------------------------------------------------------------------------
# Factory aggregation + journal + experiments.tsv + rollup rendering
# ---------------------------------------------------------------------------


def _make_manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=components,
    )


def _make_base_config(root_dir: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root_dir / "scripts" / "kstrl" / "prompt.md",
        prd_file=root_dir / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _setup_project(tmp_path: Path, component_ids: list[str]) -> Path:
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    # Knowledge distillation off by default in these tests: its agent
    # call would add nondeterministic usage rows.
    (tmp_path / "kstrl.toml").write_text("[knowledge]\nenabled = false\n")
    for comp_id in component_ids:
        feature_dir = kstrl_dir / "feature" / comp_id
        feature_dir.mkdir(parents=True, exist_ok=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
    return tmp_path


def _component(comp_id: str, deps: list[str] | None = None) -> Component:
    return Component(
        comp_id, comp_id.title(), "Desc", deps or [],
        f"scripts/kstrl/feature/{comp_id}/prd.json",
        f"kstrl/factory/{comp_id}",
    )


def _factory_config(tmp_path: Path, **overrides: Any) -> FactoryConfig:
    defaults: dict[str, Any] = dict(
        use_worktrees=False, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=5.0,
        ),
        progress_log_path=tmp_path / "progress.jsonl",
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


def _engineer_usage(total: int, cost: float = 0.0) -> UsageTotals:
    totals = UsageTotals()
    totals.add_record(UsageRecord(
        input_tokens=total // 3,
        output_tokens=total - total // 3,
        total_tokens=total,
        cost_usd=cost or None,
        duration_seconds=1.0,
        source="claude-stream-json",
    ))
    return totals


def _engineer_usage_events(root: Path) -> list[int]:
    """Every engineer-phase usage total the run recorded, in order.

    A list (not a sum) on purpose: a double count and a correct total
    are the same number of tokens but a different number of events.
    """
    events = ProgressLog(root / "progress.jsonl").read_events()
    return [
        int(e["data"]["total_tokens"]) for e in events
        if e["event"] == "component_usage" and e["data"]["phase"] == "engineer"
    ]


def _read_journal(tmp_path: Path) -> list[dict[str, Any]]:
    journal_path = tmp_path / ".kstrl" / "evolution.jsonl"
    entries = []
    for line in journal_path.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


class TestFactoryUsageAggregation:
    def test_engineer_usage_lands_in_journal_tsv_and_log(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root)
        success = ComponentResult(
            "comp-a", success=True, iterations=2,
            usage=_engineer_usage(1200, cost=0.05),
        )
        ui_buffer = io.StringIO()

        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True, file=ui_buffer), root,
            )

        assert "comp-a" in result.completed

        # Journal entry carries the per-phase usage dict.
        entries = _read_journal(root)
        comp_entry = next(e for e in entries if e["component_id"] == "comp-a")
        assert comp_entry["usage"]["engineer"]["total_tokens"] == 1200
        assert comp_entry["usage"]["engineer"]["calls"] == 1
        assert comp_entry["usage"]["engineer"]["cost_usd"] == pytest.approx(0.05)

        # experiments.tsv gains the totals columns.
        tsv = (root / ".kstrl" / "experiments.tsv").read_text().splitlines()
        header = tsv[0].split("\t")
        row = dict(zip(header, tsv[1].split("\t"), strict=True))
        assert row["total_tokens"] == "1200"
        assert float(row["total_cost_usd"]) == pytest.approx(0.05)
        assert row["unreported_calls"] == "0"

        # Progress log records the per-phase usage event.
        events = ProgressLog(root / "progress.jsonl").read_events()
        usage_events = [e for e in events if e["event"] == "component_usage"]
        assert len(usage_events) == 1
        assert usage_events[0]["component"] == "comp-a"
        assert usage_events[0]["data"]["phase"] == "engineer"
        assert usage_events[0]["data"]["total_tokens"] == 1200

        # The run summary prints the rollup table.
        out = ui_buffer.getvalue()
        assert "Usage rollup" in out
        assert "comp-a" in out
        assert "1,200" in out

    def test_review_phase_usage_attributed(self, tmp_path: Path) -> None:
        from kstrl.review import ReviewResult

        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, review_mode="advisory")
        success = ComponentResult(
            "comp-a", success=True, iterations=1,
            usage=_engineer_usage(1000),
        )
        review_agent = FakeUsageAgent(outputs=[["ok"]])
        review_agent._usage_records.append(UsageRecord(
            input_tokens=10, output_tokens=20, total_tokens=30,
            duration_seconds=0.5, source="claude-stream-json",
        ))

        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.git.get_diff_content", return_value="",
        ), patch(
            "kstrl.agents.get_agent", return_value=review_agent,
        ), patch(
            "kstrl.factory.run_review",
            return_value=ReviewResult(passed=True, mode="advisory"),
        ):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert "comp-a" in result.completed
        entries = _read_journal(root)
        comp_entry = next(e for e in entries if e["component_id"] == "comp-a")
        assert comp_entry["usage"]["engineer"]["total_tokens"] == 1000
        assert comp_entry["usage"]["review"]["total_tokens"] == 30

    def test_all_four_phases_attributed(self, tmp_path: Path) -> None:
        """Engineer, review, security, and distill spend each land under
        their own phase key with the correct totals."""
        from kstrl.review import ReviewResult
        from kstrl.security import SecurityConfig, SecurityResult

        root = _setup_project(tmp_path, ["comp-a"])
        # Re-enable knowledge: distillation is one of the four phases.
        (root / "kstrl.toml").write_text("[knowledge]\nenabled = true\n")
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(
            root,
            review_mode="advisory",
            security_config=SecurityConfig(mode="advisory"),
        )
        success = ComponentResult(
            "comp-a", success=True, iterations=1,
            usage=_engineer_usage(1000),
        )

        # Each phase's get_agent call yields a fresh fake preloaded with
        # a distinct spend (review 30, security 40, distill 50).
        phase_tokens = iter((30, 40, 50))

        def make_agent(*args: Any, **kwargs: Any) -> FakeUsageAgent:
            agent = FakeUsageAgent(outputs=[["ok"]])
            agent._usage_records.append(UsageRecord(
                total_tokens=next(phase_tokens),
                duration_seconds=0.1, source="claude-stream-json",
            ))
            return agent

        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.git.get_diff_content", return_value="",
        ), patch(
            "kstrl.agents.get_agent", side_effect=make_agent,
        ), patch(
            "kstrl.factory.run_review",
            return_value=ReviewResult(passed=True, mode="advisory"),
        ), patch(
            "kstrl.factory.run_security_review",
            return_value=SecurityResult(passed=True, mode="advisory"),
        ), patch(
            "kstrl.factory.distill_facts", return_value=(1, "ok"),
        ):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert "comp-a" in result.completed
        entries = _read_journal(root)
        comp_entry = next(e for e in entries if e["component_id"] == "comp-a")
        usage = comp_entry["usage"]
        assert usage["engineer"]["total_tokens"] == 1000
        assert usage["review"]["total_tokens"] == 30
        assert usage["security"]["total_tokens"] == 40
        assert usage["distill"]["total_tokens"] == 50

    def test_missing_usage_still_completes_and_records_nothing(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root)
        success = ComponentResult("comp-a", success=True, iterations=1)

        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert "comp-a" in result.completed
        entries = _read_journal(root)
        comp_entry = next(e for e in entries if e["component_id"] == "comp-a")
        assert comp_entry["usage"] == {}
        tsv = (root / ".kstrl" / "experiments.tsv").read_text().splitlines()
        header = tsv[0].split("\t")
        row = dict(zip(header, tsv[1].split("\t"), strict=True))
        assert row["total_tokens"] == "0"

    def test_unreported_calls_marked_as_lower_bound(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root)
        fallback = UsageTotals()
        fallback.add_record(UsageRecord(duration_seconds=4.0))
        success = ComponentResult(
            "comp-a", success=True, iterations=1, usage=fallback,
        )
        ui_buffer = io.StringIO()

        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True, file=ui_buffer), root,
            )

        out = ui_buffer.getvalue()
        assert "lower bounds" in out
        tsv = (root / ".kstrl" / "experiments.tsv").read_text().splitlines()
        header = tsv[0].split("\t")
        row = dict(zip(header, tsv[1].split("\t"), strict=True))
        assert row["unreported_calls"] == "1"


# ---------------------------------------------------------------------------
# max_total_tokens budget halt
# ---------------------------------------------------------------------------


class TestTokenBudgetHalt:
    def test_halt_fires_and_is_recorded(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest([_component("comp-a"), _component("comp-b")])
        config = _factory_config(root, max_total_tokens=500)

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            comp_id = args[0]
            return ComponentResult(
                comp_id, success=True, iterations=1,
                usage=_engineer_usage(600),
            )

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        # comp-a's engineer spend (600 >= 500) trips the cap at the
        # phase boundary: comp-a fails with a synthetic budget finding.
        assert "comp-a" in result.failed
        assert result.exit_code == 1
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.status == ComponentStatus.FAILED.value
        assert "max_total_tokens" in (comp_a.error or "")
        budget_findings = [
            f for f in comp_a.findings
            if f.is_infrastructure_error and "token budget" in f.explanation
        ]
        assert len(budget_findings) == 1
        assert budget_findings[0].phase == "engineer"

        # comp-b never launches: the scheduling gate fails it loudly too.
        assert "comp-b" in result.failed
        comp_b = manifest.get_component("comp-b")
        assert comp_b is not None
        assert any(
            f.is_infrastructure_error and f.phase == "scheduling"
            for f in comp_b.findings
        )

        # Progress log carries the budget_exceeded events.
        events = ProgressLog(root / "progress.jsonl").read_events()
        breaches = [e for e in events if e["event"] == "budget_exceeded"]
        assert len(breaches) == 2
        assert breaches[0]["data"]["max_total_tokens"] == 500
        assert breaches[0]["data"]["total_tokens"] >= 500

        # The journal still recorded the spend that tripped the cap.
        entries = _read_journal(root)
        comp_entry = next(e for e in entries if e["component_id"] == "comp-a")
        assert comp_entry["usage"]["engineer"]["total_tokens"] == 600

    def test_unbounded_by_default(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root)  # max_total_tokens defaults to 0
        success = ComponentResult(
            "comp-a", success=True, iterations=1,
            usage=_engineer_usage(10_000_000),
        )

        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert "comp-a" in result.completed
        assert result.exit_code == 0

    def test_unknown_usage_cannot_trip_the_cap(self, tmp_path: Path) -> None:
        """Fallback-only usage (no tokens reported) must not halt: the
        cap compares tokens, and unknown spend contributes none."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, max_total_tokens=100)
        fallback = UsageTotals()
        fallback.add_record(UsageRecord(duration_seconds=60.0))
        success = ComponentResult(
            "comp-a", success=True, iterations=1, usage=fallback,
        )

        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert "comp-a" in result.completed


# ---------------------------------------------------------------------------
# R8: max_total_tokens enforced DURING the engineer loop
#
# The R3.1 checks above are post-hoc detectors at parent-process phase
# boundaries: they stop the NEXT phase, never the iteration already
# running. These cover the in-loop half - the loop refusing to start
# another iteration - and the fact that both routes land in the same
# audit state.
# ---------------------------------------------------------------------------


class SequenceUsageAgent:
    """Fake agent appending one caller-supplied record per run.

    Unlike FakeUsageAgent's single fixed record, the sequence lets a
    test mix reporting and non-reporting iterations (the claude adapter
    records source="timeout"/"parse-error" with no token counts).
    """

    def __init__(
        self, records: list[UsageRecord], outputs: list[str] | None = None,
    ) -> None:
        self._records = records
        self._outputs = outputs or ["working..."]
        self._usage_records: list[UsageRecord] = []

    @property
    def name(self) -> str:
        return "sequence-usage"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        index = min(len(self._usage_records), len(self._records) - 1)
        self._usage_records.append(self._records[index])
        yield from self._outputs

    @property
    def final_message(self) -> str | None:
        return None

    @property
    def usage_records(self) -> list[UsageRecord]:
        return list(self._usage_records)


_REPORTED = UsageRecord(total_tokens=300, source="codex-text")
_UNREPORTED = UsageRecord(duration_seconds=5.0, source="unavailable")
# Reports a cost but no tokens: "known" to the meter, invisible to a
# token ceiling (review finding P1-b).
_COST_ONLY = UsageRecord(
    cost_usd=0.0227028, duration_seconds=1.8, source="claude-stream-json",
)
# Both axes, as the claude adapter reports on a healthy call.
_BOTH = UsageRecord(
    total_tokens=300, cost_usd=0.03, duration_seconds=1.8,
    source="claude-stream-json",
)


def _budget_for(
    run_total: UsageTotals, cap: int, cost_cap: float = 0.0,
) -> LoopBudget:
    """Exactly the LoopBudget ``_submit_args`` hands a worker.

    Threading the run totals through this helper is what makes the
    run-wide half of the unenforceable rule testable: the priors are the
    only channel by which one loop's non-reporting calls reach the next.

    ``cost_cap`` was added with the cost ceiling; the cost priors are
    threaded from the same totals, exactly as ``_submit_args`` does.
    """
    return LoopBudget(
        max_total_tokens=cap,
        prior_total_tokens=run_total.total_tokens,
        prior_known_calls=run_total.known_calls,
        prior_calls=run_total.calls,
        prior_token_calls=run_total.token_calls,
        max_cost_usd=cost_cap,
        prior_cost_usd=run_total.cost_usd,
        prior_cost_calls=run_total.cost_calls,
    )


class TestInLoopTokenBudget:
    def test_halt_happens_between_iterations_not_at_max_iterations(
        self, tmp_path: Path,
    ) -> None:
        """The loop must stop ITSELF: 300 tokens/iteration against a 500
        cap means iteration 3 never starts, even though max_iterations
        is 10 and no phase boundary has been reached."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.completed is False
        assert result.iterations == 2
        assert len(agent.usage_records) == 2  # the agent really stopped
        assert "token budget exceeded" in result.budget_halt_reason
        assert "600" in result.budget_halt_reason
        assert result.usage.total_tokens == 600

    def test_spend_from_earlier_components_counts(self, tmp_path: Path) -> None:
        """The cap is run-level: a worker launched with 450 tokens
        already on the run's meter has 50 left, not 500."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(
                max_total_tokens=500, prior_total_tokens=450,
                prior_known_calls=1, prior_calls=1, prior_token_calls=1,
            ),
        )
        assert result.iterations == 1
        assert len(agent.usage_records) == 1
        assert "750" in result.budget_halt_reason

    def test_zero_cap_is_unbounded(self, tmp_path: Path) -> None:
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 3), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=0),
        )
        assert result.iterations == 3
        assert result.budget_halt_reason == ""

    def test_no_budget_argument_keeps_pre_r8_behaviour(
        self, tmp_path: Path,
    ) -> None:
        """`ks run` / `ks feature` pass no budget; nothing changes."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 3), PlainUI(no_color=True), agent, tmp_path,
        )
        assert result.iterations == 3
        assert result.budget_halt_reason == ""

    def test_completion_in_the_breaching_iteration_still_completes(
        self, tmp_path: Path,
    ) -> None:
        """Ordering: an iteration that finished the work is a success.
        The overrun is then caught by the phase-boundary check, which is
        exactly the pre-R8 path - the loop does not retro-fail work that
        is already done."""
        agent = FakeUsageAgent(
            outputs=[[COMPLETION_MARKER]], record=_REPORTED,
        )
        result = run_loop(
            _loop_config(tmp_path, 5), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=100),
        )
        assert result.completed is True
        assert result.budget_halt_reason == ""

    def test_wholly_unreported_usage_halts_as_unenforceable(
        self, tmp_path: Path,
    ) -> None:
        """Unknown usage is NOT treated as zero when nothing at all has
        reported: a cap that provably cannot trip is the defect being
        fixed, so the loop halts loudly instead of running to
        max_iterations under a dead ceiling."""
        agent = SequenceUsageAgent([_UNREPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.completed is False
        assert result.iterations == 2  # _UNENFORCEABLE_CALLS
        assert "unenforceable" in result.budget_halt_reason
        assert result.usage.total_tokens == 0
        assert result.usage.unreported_calls == 2

    def test_one_silent_call_is_an_incident_not_a_dead_cap(
        self, tmp_path: Path,
    ) -> None:
        """A single non-reporting call (a timed-out or unparseable
        iteration on an adapter that normally reports) must not kill the
        run: iteration 2 reports, the cap is demonstrably alive, and the
        loop continues until the arithmetic trips it."""
        agent = SequenceUsageAgent([_UNREPORTED, _REPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.iterations == 3  # 0 + 300 + 300 >= 500
        assert "token budget exceeded" in result.budget_halt_reason
        assert result.usage.unreported_calls == 1

    def test_unreported_usage_is_free_when_the_cap_is_off(
        self, tmp_path: Path,
    ) -> None:
        """The unenforceable halt is a property of an ENABLED cap. With
        no cap there is nothing to enforce and a non-reporting adapter
        (CustomAgent) runs exactly as before."""
        agent = SequenceUsageAgent([_UNREPORTED])
        result = run_loop(
            _loop_config(tmp_path, 3), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=0),
        )
        assert result.iterations == 3
        assert result.budget_halt_reason == ""

    def test_mixed_reporting_counts_unknown_as_zero_and_keeps_going(
        self, tmp_path: Path,
    ) -> None:
        """One unreported iteration among reporting ones does not halt:
        the total stays a documented lower bound that still grows toward
        the cap. Here 300 (reported) + unknown + 300 trips at iteration
        3, one iteration later than the true spend would have."""
        agent = SequenceUsageAgent([_REPORTED, _UNREPORTED, _REPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.iterations == 3
        assert "token budget exceeded" in result.budget_halt_reason
        assert result.usage.calls == 3
        assert result.usage.unreported_calls == 1
        assert result.usage.total_tokens == 600

    def test_prior_reported_calls_do_not_disarm_the_unenforceable_halt(
        self, tmp_path: Path,
    ) -> None:
        """A silent ENGINEER is a dead cap even when earlier phases
        reported.

        Review regression: the halt used to be judged on the whole run
        (`prior_known_calls + loop known_calls`), so a reporting
        architect suppressed it and a silent engineer then spent
        unbounded under a nominal cap. `prior_total_tokens` is frozen at
        launch, so once this loop reports nothing the total can never
        grow toward the ceiling - the cap is exactly as dead as in the
        all-silent case. That configuration is easy to reach: `[agent]
        command` sets a custom engineer command while the adversarial
        roles keep a reporting adapter.

        Still true after the run-wide threshold landed (P1-a): the
        TOKEN-EVIDENCE half of the rule is still judged on this loop
        alone, so the reporting prior below (one call, one token call)
        cannot suppress anything - it only keeps the run's tokenless
        count at 0, which is why the halt lands on this loop's OWN
        second tokenless call.
        """
        agent = SequenceUsageAgent([_UNREPORTED])
        result = run_loop(
            _loop_config(tmp_path, 5), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(
                max_total_tokens=500, prior_total_tokens=100,
                prior_known_calls=1, prior_calls=1, prior_token_calls=1,
            ),
        )
        assert result.iterations == 2      # halts at _UNENFORCEABLE_CALLS
        assert "unenforceable" in result.budget_halt_reason
        assert not result.completed

    def test_one_silent_call_alongside_prior_reporting_keeps_going(
        self, tmp_path: Path,
    ) -> None:
        """A single unparseable result is an incident, not a dead cap.

        The threshold is what separates "this adapter never reports"
        from "one call came back unreadable"; a lone silent iteration
        must not kill a capped run.

        This is the case the run-wide threshold (P1-a) had to preserve:
        the prior call reported tokens, so the run's tokenless count is
        1 after this loop's silent first iteration - below the
        threshold - and the loop keeps going. Counting ALL prior calls
        instead of only the tokenless ones would have halted here, which
        is why the counter is tokenless-scoped.
        """
        agent = SequenceUsageAgent([_UNREPORTED, _REPORTED, _REPORTED])
        result = run_loop(
            _loop_config(tmp_path, 3), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(
                max_total_tokens=500_000, prior_total_tokens=100,
                prior_known_calls=1, prior_calls=1, prior_token_calls=1,
            ),
        )
        assert result.budget_halt_reason == ""
        assert result.iterations == 3

    def test_cost_only_reporting_is_not_token_evidence(
        self, tmp_path: Path,
    ) -> None:
        """Review regression (P1-b): an adapter that reports cost but no
        tokens moved ``known_calls`` and so looked fully instrumented,
        while ``total_tokens`` stayed 0 and the cap could never trip.
        The loop ran to max_iterations under a dead ceiling. It now
        halts at the threshold like any other tokenless adapter.
        """
        agent = SequenceUsageAgent([_COST_ONLY])
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.iterations == 2      # not the configured 10
        assert "unenforceable" in result.budget_halt_reason
        assert result.usage.known_calls == 2   # the misleading signal
        assert result.usage.token_calls == 0   # the honest one
        assert result.usage.total_tokens == 0

    def test_unenforceable_threshold_does_not_reset_per_loop(
        self, tmp_path: Path,
    ) -> None:
        """Review regression (P1-a): the threshold is run-wide.

        Reproduced by the reviewer: with ``max_iterations = 1`` (or a
        retry that dies after one call) no single loop ever reached
        ``_UNENFORCEABLE_CALLS``, so three sequential one-iteration
        loops reached calls=3 / token_calls=0 / total_tokens=0 with
        every ``budget_halt_reason`` empty. The cap was decorative
        whenever work was split into short loops.

        The priors are threaded exactly as ``_submit_args`` threads
        them, so this pins the whole channel, not just the arithmetic.
        """
        run_total = UsageTotals()
        reasons: list[str] = []
        for _ in range(3):
            agent = SequenceUsageAgent([_UNREPORTED])
            result = run_loop(
                _loop_config(tmp_path, 1), PlainUI(no_color=True), agent,
                tmp_path, budget=_budget_for(run_total, 500),
            )
            run_total.merge(result.usage)
            reasons.append(result.budget_halt_reason)

        assert reasons[0] == ""                    # one is an incident
        assert "unenforceable" in reasons[1]       # two is the adapter
        assert "unenforceable" in reasons[2]
        assert run_total.calls == 3
        assert run_total.token_calls == 0

    def test_a_high_prior_call_count_cannot_halt_a_loop_that_has_not_run(
        self,
    ) -> None:
        """The run-wide threshold must not pre-empt the engineer.

        A worker launched into a run that already has tokenless calls on
        its meter has made no calls of its own yet, so there is no
        token evidence either way and nothing to conclude. Halting here
        would fail a component that never got to run.
        """
        budget = LoopBudget(
            max_total_tokens=500, prior_calls=20, prior_token_calls=0,
        )
        assert budget.halt_reason(UsageTotals()) is None

    def test_a_tokenless_call_after_an_earlier_one_halts_immediately(
        self, tmp_path: Path,
    ) -> None:
        """The deliberate cost of counting the threshold across attempts.

        Once the ENGINEER has accumulated one tokenless call earlier in
        the run - a previous component's last iteration, or a retry that
        died after one call - its next tokenless iteration reaches the
        threshold and halts, where an isolated loop would have given it a
        second chance. Accepted on purpose: two tokenless engineer calls
        in one run is adapter behavior rather than an incident, and the
        failure is loud, recorded and recoverable (raise or clear
        max_total_tokens), whereas the alternative is a per-loop counter
        that never fires at all (P1-a).

        Scoped to engineer calls, so another role's timeout cannot get
        here: `prior_calls`/`prior_token_calls` come from
        `pipeline.engineer_usage_totals()`, not run-wide totals. See
        TestUnenforceableHaltIsEngineerScoped.
        """
        agent = SequenceUsageAgent([_UNREPORTED, _REPORTED])
        result = run_loop(
            _loop_config(tmp_path, 5), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(
                max_total_tokens=500_000, prior_total_tokens=900,
                prior_known_calls=2, prior_calls=3, prior_token_calls=2,
            ),
        )
        assert result.iterations == 1
        assert "unenforceable" in result.budget_halt_reason
        assert "2 tokenless call(s) this run" in result.budget_halt_reason

    def test_iteration_usage_callback_sees_cumulative_totals(
        self, tmp_path: Path,
    ) -> None:
        seen: list[int] = []
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        run_loop(
            _loop_config(tmp_path, 3), PlainUI(no_color=True), agent, tmp_path,
            on_iteration_usage=lambda totals: seen.append(totals.total_tokens),
        )
        assert seen == [300, 600, 900]

    def test_iteration_usage_callback_failure_never_breaks_the_loop(
        self, tmp_path: Path,
    ) -> None:
        def explode(totals: UsageTotals) -> None:
            raise OSError("disk full")

        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 2), PlainUI(no_color=True), agent, tmp_path,
            on_iteration_usage=explode,
        )
        assert result.iterations == 2


class TestWorkerPropagatesInLoopHalt:
    def _worker_args(self, root: Path, **overrides: Any) -> dict[str, Any]:
        args: dict[str, Any] = dict(
            component_id="comp-a",
            prd_path_str="scripts/kstrl/feature/comp-a/prd.json",
            worktree_path_str=str(root),
            root_dir_str=str(root),
            prompt_file_str="scripts/kstrl/prompt.md",
            agent_cmd="echo hi",
            model=None, reasoning=None, agent_type=None,
            sleep_seconds=0.0,
            max_iterations=10,
            events_dir_str=None,
            run_id="run-b",
            redirect_output=False,  # NEVER dup2 inside the test process
        )
        args.update(overrides)
        return args

    def test_worker_reports_budget_exceeded_and_stops_early(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)

        with patch("kstrl.agents.get_agent", return_value=agent):
            result = _run_component(**self._worker_args(
                root, token_budget=LoopBudget(max_total_tokens=500),
            ))

        assert result.success is False
        assert result.budget_exceeded is True
        assert result.iterations == 2  # not the configured 10
        assert "token budget exceeded" in (result.error or "")
        assert result.usage is not None
        assert result.usage.total_tokens == 600

    def test_worker_without_a_budget_runs_to_max_iterations(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)

        with patch("kstrl.agents.get_agent", return_value=agent):
            result = _run_component(**self._worker_args(
                root, max_iterations=3,
            ))

        assert result.budget_exceeded is False
        assert result.iterations == 3

    def test_worker_persists_usage_at_every_iteration_boundary(
        self, tmp_path: Path,
    ) -> None:
        """The durable snapshot the abort path reads back.

        Keyed off ``usage_dir_str``, not ``events_dir_str``: review
        finding P2-d showed the snapshot dying with the observability
        opt-out, so the accounting channel is now its own argument.
        """
        root = _setup_project(tmp_path, ["comp-a"])
        usage_dir = root / ".kstrl" / "runs" / "run-b"
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)

        with patch("kstrl.agents.get_agent", return_value=agent):
            _run_component(**self._worker_args(
                root, max_iterations=2,
                events_dir_str=str(usage_dir), usage_dir_str=str(usage_dir),
            ))

        snapshot = _read_partial_usage(
            RunPaths(root=usage_dir).engineer_usage("comp-a")
        )
        assert snapshot is not None
        assert snapshot.total_tokens == 600
        assert snapshot.calls == 2

    def test_worker_persists_usage_without_an_events_dir(
        self, tmp_path: Path,
    ) -> None:
        """Review regression (P2-d), worker half.

        ``progress_log_enabled = false`` makes the parent pass
        ``events_dir_str=None``. That used to skip installing
        ``on_iteration_usage`` entirely, so a killed worker recorded
        nothing in a fully supported configuration: an observability
        opt-out silently disabled the meter. The accounting channel is
        now independent of the event channel.
        """
        root = _setup_project(tmp_path, ["comp-a"])
        usage_dir = root / ".kstrl" / "accounting-only"
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)

        with patch("kstrl.agents.get_agent", return_value=agent):
            _run_component(**self._worker_args(
                root, max_iterations=2,
                events_dir_str=None, usage_dir_str=str(usage_dir),
            ))

        snapshot = _read_partial_usage(
            RunPaths(root=usage_dir).engineer_usage("comp-a")
        )
        assert snapshot is not None
        assert snapshot.total_tokens == 600
        # No events were written: the opt-out is still honored.
        assert not (usage_dir / "events.jsonl").exists()


class TestSchedulerHandsDownTheBudget:
    def test_worker_is_told_the_cap_and_what_the_run_already_spent(
        self, tmp_path: Path,
    ) -> None:
        """The cap is run-level, so the second worker must launch with
        the first component's spend already on its meter - otherwise the
        in-loop check degrades to a per-component budget. Also pins the
        positional contract between _submit_args and _run_component: the
        budget is the last element of the submit tuple, which is how the
        process-pool path passes it."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest([
            _component("comp-a"), _component("comp-b", deps=["comp-a"]),
        ])
        config = _factory_config(root, max_total_tokens=100_000)
        budgets: list[Any] = []

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            budgets.append(args[-1])
            return ComponentResult(
                str(args[0]), success=True, iterations=1,
                usage=_engineer_usage(700),
            )

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert len(budgets) == 2
        assert all(isinstance(b, LoopBudget) for b in budgets)
        assert budgets[0] == LoopBudget(
            max_total_tokens=100_000, prior_total_tokens=0,
            prior_known_calls=0, prior_calls=0, prior_token_calls=0,
        )
        # prior_calls / prior_token_calls are the channel that stops the
        # unenforceable threshold resetting per component (P1-a).
        assert budgets[1] == LoopBudget(
            max_total_tokens=100_000, prior_total_tokens=700,
            prior_known_calls=1, prior_calls=1, prior_token_calls=1,
        )

    def test_tokenless_prior_calls_reach_the_next_worker(
        self, tmp_path: Path,
    ) -> None:
        """Review regression (P1-a), scheduler half: a component whose
        engineer reported nothing must leave that fact on the NEXT
        worker's budget, or the run-wide threshold cannot exist."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest([
            _component("comp-a"), _component("comp-b", deps=["comp-a"]),
        ])
        config = _factory_config(root, max_total_tokens=100_000)
        budgets: list[Any] = []
        silent = UsageTotals()
        silent.add_record(_UNREPORTED)

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            budgets.append(args[-1])
            return ComponentResult(
                str(args[0]), success=True, iterations=1, usage=silent,
            )

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert budgets[1].prior_calls == 1
        assert budgets[1].prior_token_calls == 0

    def test_no_cap_still_hands_down_a_disabled_budget(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        budgets: list[Any] = []

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            budgets.append(args[-1])
            return ComponentResult(str(args[0]), success=True, iterations=1)

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, _factory_config(root), _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert [b.enabled for b in budgets] == [False]


class TestInLoopHaltAuditState:
    def test_same_audit_state_as_a_phase_boundary_breach(
        self, tmp_path: Path,
    ) -> None:
        """An in-loop halt must be indistinguishable, downstream, from
        the R3.1 phase-boundary halt: BudgetExceeded event, one typed
        infrastructure_error finding, exactly one budget_overrun inbox
        item, and no duplicate halted_run.

        The cap here is huge and the reported spend tiny, so ONLY the
        typed flag can route this - the parent's own totals show no
        breach (the unreportable-usage case).
        """
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, max_total_tokens=1_000_000)
        reason = (
            "token budget unenforceable: none of this loop's 1 agent "
            "call(s) reported any token usage, so max_total_tokens "
            "(1000000) can never trip on this adapter; halting rather "
            "than spending under a cap that cannot fire (R8)"
        )
        halted = ComponentResult(
            "comp-a", success=False, iterations=1, error=reason,
            usage=_engineer_usage(10), budget_exceeded=True,
        )

        with patch(
            "kstrl.factory._run_component", return_value=halted,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert "comp-a" in result.failed
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.status == ComponentStatus.FAILED.value
        assert comp_a.failed_phase == "engineer"
        assert comp_a.failed_check == "token_budget"
        # The recorded error is the loop's reason, not a derived
        # "10 >= 1000000" sentence that would be false.
        assert comp_a.error == reason
        budget_findings = [
            f for f in comp_a.findings
            if f.is_infrastructure_error and "token budget" in f.explanation
        ]
        assert len(budget_findings) == 1
        assert budget_findings[0].phase == "engineer"

        events = ProgressLog(root / "progress.jsonl").read_events()
        breaches = [e for e in events if e["event"] == "budget_exceeded"]
        assert len(breaches) == 1
        assert breaches[0]["data"]["max_total_tokens"] == 1_000_000

        kinds = [str(i.kind) for i in Inbox(root, InboxConfig()).open_items()]
        assert kinds == ["budget_overrun"]

        # The spend that got us here is still on the meter.
        entries = _read_journal(root)
        comp_entry = next(e for e in entries if e["component_id"] == "comp-a")
        assert comp_entry["usage"]["engineer"]["total_tokens"] == 10

    def test_no_retry_is_burned_on_an_in_loop_halt(
        self, tmp_path: Path,
    ) -> None:
        """Retrying cannot un-spend tokens; the component fails outright
        even with retries available."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, max_total_tokens=1_000_000, max_retries=3)
        calls: list[str] = []

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            calls.append(str(args[0]))
            return ComponentResult(
                "comp-a", success=False, iterations=1,
                error="token budget unenforceable (R8)",
                usage=_engineer_usage(10), budget_exceeded=True,
            )

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert calls == ["comp-a"]
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.retries == 0


def _pending_executor(
    sync_calls: int, on_pending: Callable[[tuple[Any, ...]], None],
) -> type:
    """Executor stand-in whose LATER submissions never resolve.

    Why this exists (review finding P2-e): ``_factory_config`` sets
    ``use_worktrees=False``, which forces ``max_parallel=1``, which
    selects ``_InlineExecutor`` - and that executor runs the worker
    synchronously inside ``submit()``. By the time a stop is observed
    the future is therefore already done, and ``_salvage_aborted_usage``
    takes the already-delivered route. Every test of the DISK route has
    to inject a genuinely pending future at the executor seam, or it
    passes with the snapshot write/read entirely broken.

    ``on_pending`` receives the submit arguments, which is how a test
    learns the run-scoped usage directory the worker would have been
    told to write to (the run id is minted inside ``run_factory``).
    """
    state = {"n": 0}

    class _PendingExecutor:
        def submit(
            self, fn: Callable[..., ComponentResult], /, *args: Any,
        ) -> Future[ComponentResult]:
            state["n"] += 1
            future: Future[ComponentResult] = Future()
            # Inline mode binds the args into a functools.partial; pool
            # mode passes them through. Accept both.
            submit_args = args or tuple(getattr(fn, "args", ()))
            if state["n"] > sync_calls:
                on_pending(submit_args)
                return future  # never resolves: the worker was killed
            try:
                future.set_result(fn(*args))
            except Exception as exc:  # noqa: BLE001 - mirrors the real one
                future.set_exception(exc)
            return future

        def shutdown(
            self, wait: bool = True, cancel_futures: bool = False,
        ) -> None:
            """Nothing to shut down."""

    return _PendingExecutor


def _usage_dir_from_args(args: tuple[Any, ...]) -> Path:
    """The accounting directory ``_submit_args`` hands the worker.

    Pins the positional contract from the other end: the submit tuple
    ends with (events_dir, usage_dir, run_id, token_budget).
    """
    assert isinstance(args[-1], LoopBudget)
    return Path(str(args[-3]))


class TestAbortedWorkerUsage:
    def test_snapshot_roundtrips(self, tmp_path: Path) -> None:
        totals = _engineer_usage(1234, cost=0.5)
        path = tmp_path / "engineer_usage.json"
        _write_partial_usage(path, totals)
        back = _read_partial_usage(path)
        assert back is not None
        assert back.to_dict() == totals.to_dict()

    def test_unusable_snapshots_read_as_none(self, tmp_path: Path) -> None:
        assert _read_partial_usage(tmp_path / "missing.json") is None
        torn = tmp_path / "torn.json"
        torn.write_text('{"calls": 2, "total_tok')
        assert _read_partial_usage(torn) is None
        listy = tmp_path / "list.json"
        listy.write_text("[1, 2]")
        assert _read_partial_usage(listy) is None
        empty = tmp_path / "empty.json"
        empty.write_text('{"calls": 0, "total_tokens": 0}')
        assert _read_partial_usage(empty) is None  # nothing ran

    def test_unwritable_snapshot_path_is_swallowed(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        _write_partial_usage(blocker / "sub" / "usage.json", _engineer_usage(1))

    def test_clearing_a_snapshot_is_idempotent(self, tmp_path: Path) -> None:
        """The scheduler clears before every submission, including the
        first, when there is nothing to clear."""
        path = tmp_path / "engineer_usage.json"
        _clear_partial_usage(path)              # absent: no error
        _write_partial_usage(path, _engineer_usage(700))
        _clear_partial_usage(path)
        assert _read_partial_usage(path) is None
        _clear_partial_usage(tmp_path)          # a directory: swallowed

    def test_killed_worker_spend_recovered_from_the_snapshot(
        self, tmp_path: Path,
    ) -> None:
        usage_paths = RunPaths(root=tmp_path)
        _write_partial_usage(
            usage_paths.engineer_usage("comp-a"), _engineer_usage(700),
        )
        pipeline = MagicMock()
        pipeline.usage_paths = usage_paths
        future: Future[ComponentResult] = Future()  # never completes

        _salvage_aborted_usage(future, "comp-a", pipeline)

        pipeline.record_engineer_usage.assert_called_once()
        comp_id, totals = pipeline.record_engineer_usage.call_args[0]
        assert comp_id == "comp-a"
        assert totals.total_tokens == 700

    def test_a_delivered_result_wins_over_the_snapshot(
        self, tmp_path: Path,
    ) -> None:
        """No double counting: a future that DID deliver is
        authoritative and the (staler) snapshot is ignored."""
        usage_paths = RunPaths(root=tmp_path)
        _write_partial_usage(
            usage_paths.engineer_usage("comp-a"), _engineer_usage(700),
        )
        pipeline = MagicMock()
        pipeline.usage_paths = usage_paths
        future: Future[ComponentResult] = Future()
        future.set_result(ComponentResult(
            "comp-a", success=False, usage=_engineer_usage(900),
        ))

        _salvage_aborted_usage(future, "comp-a", pipeline)

        pipeline.record_engineer_usage.assert_called_once()
        assert pipeline.record_engineer_usage.call_args[0][1].total_tokens == 900

    def test_crashed_worker_records_nothing(self, tmp_path: Path) -> None:
        pipeline = MagicMock()
        pipeline.usage_paths = RunPaths(root=tmp_path)
        future: Future[ComponentResult] = Future()
        future.set_exception(RuntimeError("worker died"))

        _salvage_aborted_usage(future, "comp-a", pipeline)

        pipeline.record_engineer_usage.assert_not_called()

    def test_stop_mid_run_keeps_the_aborted_component_spend(
        self, tmp_path: Path,
    ) -> None:
        """End to end: the shutdown path used to drop the worker's
        usage on the floor. Now it lands on the meter.

        SCOPE (review finding P2-e): this covers the ALREADY-DELIVERED
        route only. ``_factory_config`` sets use_worktrees=False, which
        forces max_parallel=1 and the synchronous ``_InlineExecutor``,
        so the future is done before the stop is observed and the disk
        snapshot is never read. It passed with snapshot write/read
        entirely broken. The disk route is covered by the pending-future
        tests below; both routes need their own test because they are
        mutually exclusive branches of ``_salvage_aborted_usage``.
        """
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        stop = StopController()

        def slow_component(comp_id: str, *a: Any, **k: Any) -> ComponentResult:
            stop.request("mid-run test stop")
            time.sleep(0.2)
            return ComponentResult(
                comp_id, success=True, iterations=1,
                usage=_engineer_usage(4242),
            )

        with patch(
            "kstrl.factory._run_component", side_effect=slow_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, _factory_config(root), _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root, stop=stop,
            )

        assert result.exit_code == 130
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.failed_phase == "aborted"
        assert _engineer_usage_events(root) == [4242]

    def test_pending_worker_spend_is_salvaged_from_disk_end_to_end(
        self, tmp_path: Path,
    ) -> None:
        """Review regression (P2-e): the disk route, exercised for real.

        The future is still PENDING when the stop lands - the worker was
        killed mid-loop - so the only surviving record of its spend is
        the snapshot it published at its last iteration boundary. Break
        ``_write_partial_usage`` or ``_read_partial_usage`` and this
        test fails; the pre-existing abort test does not.
        """
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        stop = StopController()

        def worker_published_then_died(args: tuple[Any, ...]) -> None:
            _write_partial_usage(
                RunPaths(root=_usage_dir_from_args(args))
                .engineer_usage("comp-a"),
                _engineer_usage(700),
            )
            stop.request("mid-run test stop")

        with patch(
            "kstrl.factory._InlineExecutor",
            _pending_executor(0, worker_published_then_died),
        ), patch(
            "kstrl.factory._run_component",
            side_effect=AssertionError("the worker never returned"),
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, _factory_config(root), _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root, stop=stop,
            )

        assert result.exit_code == 130
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.failed_phase == "aborted"
        assert _engineer_usage_events(root) == [700]

    def test_a_stale_snapshot_from_a_finished_attempt_is_not_recounted(
        self, tmp_path: Path,
    ) -> None:
        """Review regression (P2-c): the snapshot is attempt-scoped.

        The file is keyed by run + component only, and a normal result
        leaves it on disk. Attempt 1 here completes with 700 tokens
        (recorded by ``process_result``) and leaves its snapshot behind;
        attempt 2 is cancelled before its own first iteration boundary,
        so it has no spend of its own. Before the fix, salvage read the
        stale file and the run reported 1400 for 700 tokens of work.

        The scheduler now clears the snapshot immediately before every
        submission - in the PARENT, so the window where the worker never
        starts is covered too.
        """
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, max_retries=1)
        stop = StopController()

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            # Attempt 1: a real worker publishing its boundary snapshot,
            # then failing organically with the same spend on the result.
            _write_partial_usage(
                RunPaths(root=_usage_dir_from_args(args))
                .engineer_usage("comp-a"),
                _engineer_usage(700),
            )
            return ComponentResult(
                "comp-a", success=False, iterations=1, error="tests failed",
                usage=_engineer_usage(700),
            )

        with patch(
            "kstrl.factory._InlineExecutor",
            # Attempt 1 runs; attempt 2's future hangs and is aborted.
            _pending_executor(1, lambda args: stop.request("mid-run stop")),
        ), patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root, stop=stop,
            )

        assert _engineer_usage_events(root) == [700]

    def test_pending_worker_spend_survives_progress_log_disabled(
        self, tmp_path: Path,
    ) -> None:
        """Review regression (P2-d): accounting is not observability.

        With ``progress_log_enabled = false`` the parent had no
        ``run_paths``, so it passed the worker no place to publish
        usage and then had no place to read one back: a killed worker
        recorded nothing at all, in a supported configuration. The
        accounting directory is now allocated for every run.

        Asserted through the end-of-run usage rollup because the opt-out
        means there is no progress log to read - and the rollup is what
        a human actually sees.
        """
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, progress_log_enabled=False)
        stop = StopController()
        out = io.StringIO()

        def worker_published_then_died(args: tuple[Any, ...]) -> None:
            usage_dir = _usage_dir_from_args(args)
            _write_partial_usage(
                RunPaths(root=usage_dir).engineer_usage("comp-a"),
                _engineer_usage(700),
            )
            stop.request("mid-run test stop")

        with patch(
            "kstrl.factory._InlineExecutor",
            _pending_executor(0, worker_published_then_died),
        ), patch(
            "kstrl.factory._run_component",
            side_effect=AssertionError("the worker never returned"),
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True, file=out), root, stop=stop,
            )

        rollup = [
            line for line in out.getvalue().splitlines()
            if "comp-a" in line and "engineer" in line
        ]
        assert rollup, "the aborted worker's spend never reached the meter"
        assert "700" in rollup[0]
        # The opt-out is still honored: no progress log, no event stream.
        assert not (root / "progress.jsonl").exists()
        assert not list((root / ".kstrl" / "runs").glob("*/events.jsonl"))


# ---------------------------------------------------------------------------
# Rollup rendering
# ---------------------------------------------------------------------------


class TestRollupRendering:
    def test_rows_ordered_and_totalled(self) -> None:
        engineer = UsageTotals()
        engineer.add_record(UsageRecord(
            input_tokens=100, output_tokens=200, total_tokens=300,
            cost_usd=0.5, duration_seconds=10.0, source="claude-stream-json",
        ))
        review = UsageTotals()
        review.add_record(UsageRecord(total_tokens=50, source="codex-text"))
        run_usage = UsageTotals()
        run_usage.merge(engineer)
        run_usage.merge(review)

        lines = _format_usage_rollup(
            {"comp-a": {"review": review, "engineer": engineer}}, run_usage,
        )
        # Header, engineer row before review row (fixed phase order), TOTAL.
        assert len(lines) == 4
        assert "tokens_total" in lines[0]
        assert "engineer" in lines[1]
        assert "review" in lines[2]
        assert lines[3].startswith("TOTAL")
        assert "350" in lines[3]

    def test_unknown_usage_rendered_as_dash_with_note(self) -> None:
        unknown = UsageTotals()
        unknown.add_record(UsageRecord(duration_seconds=3.0))
        lines = _format_usage_rollup({"comp-a": {"engineer": unknown}}, unknown)
        assert "-" in lines[1]
        assert any("lower bounds" in line for line in lines)


# ---------------------------------------------------------------------------
# Control plane: toml / env for max_total_tokens (flag surface is covered
# in test_config_control_plane.py alongside its R2.2 siblings)
# ---------------------------------------------------------------------------


class TestMaxTotalTokensConfig:
    def test_default_unbounded(self) -> None:
        assert FactoryConfig().max_total_tokens == 0

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_MAX_TOTAL_TOKENS", "250000")
        assert FactoryConfig.from_env().max_total_tokens == 250000

    def test_load_toml_and_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\nmax_total_tokens = 111\n"
        )
        assert FactoryConfig.load(tmp_path).max_total_tokens == 111
        monkeypatch.setenv("KSTRL_FACTORY_MAX_TOTAL_TOKENS", "222")
        assert FactoryConfig.load(tmp_path).max_total_tokens == 222


class TestMaxCostUsdConfig:
    def test_default_unbounded(self) -> None:
        """0.0 = unbounded, deliberately the same convention as
        max_total_tokens rather than a sentinel like -1."""
        assert FactoryConfig().max_cost_usd == 0.0

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_MAX_COST_USD", "12.50")
        assert FactoryConfig.from_env().max_cost_usd == 12.50

    def test_load_toml_and_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\nmax_cost_usd = 1.5\n"
        )
        assert FactoryConfig.load(tmp_path).max_cost_usd == 1.5
        monkeypatch.setenv("KSTRL_FACTORY_MAX_COST_USD", "2.5")
        assert FactoryConfig.load(tmp_path).max_cost_usd == 2.5

    def test_both_ceilings_coexist(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\nmax_total_tokens = 500000\nmax_cost_usd = 5.0\n"
        )
        config = FactoryConfig.load(tmp_path)
        assert config.max_total_tokens == 500_000
        assert config.max_cost_usd == 5.0


class TestUnenforceableHaltIsEngineerScoped:
    """The tokenless threshold asks whether the ENGINEER's adapter
    reports tokens, so only engineer calls may answer it.

    Review regression: the counter was first sourced from
    `pipeline.run_usage`, which aggregates every role. Two unrelated
    timeouts (say an architect call plus the engineer's first iteration)
    then halted a run whose engineer adapter had been reporting fine -
    and the message asserted the cap could "never trip on this adapter"
    while the run sat at half the ceiling with four reporting calls
    behind it.
    """

    def test_other_roles_timeouts_do_not_condemn_a_healthy_engineer(
        self,
    ) -> None:
        # 4 engineer calls, all reported; the run also had an architect
        # timeout, which is NOT engineer evidence and must not count.
        budget = LoopBudget(
            max_total_tokens=500_000, prior_total_tokens=250_000,
            prior_known_calls=5, prior_calls=4, prior_token_calls=4,
        )
        assert budget.halt_reason(
            UsageTotals(calls=1, known_calls=0, token_calls=0, total_tokens=0),
        ) is None

    def test_silent_engineer_across_attempts_still_halts(self) -> None:
        # P1-a must stay fixed: one tokenless engineer call already
        # recorded, this loop's first is the second.
        budget = LoopBudget(
            max_total_tokens=500_000, prior_total_tokens=0,
            prior_known_calls=0, prior_calls=1, prior_token_calls=0,
        )
        reason = budget.halt_reason(
            UsageTotals(calls=1, known_calls=0, token_calls=0, total_tokens=0),
        )
        assert reason is not None
        assert "unenforceable" in reason

    def test_halt_message_does_not_claim_the_adapter_never_reports(
        self,
    ) -> None:
        # The message must not assert something false about an adapter
        # that demonstrably reported; it states what is actually true -
        # prior spend is frozen, so the cap cannot advance from here.
        budget = LoopBudget(
            max_total_tokens=500_000, prior_total_tokens=250_000,
            prior_known_calls=4, prior_calls=2, prior_token_calls=0,
        )
        reason = budget.halt_reason(
            UsageTotals(calls=1, known_calls=0, token_calls=0, total_tokens=0),
        )
        assert reason is not None
        assert "never trip on this adapter" not in reason
        assert "cannot advance from this loop" in reason
        assert "frozen at 250000" in reason

    def test_pipeline_engineer_totals_exclude_other_phases(self) -> None:
        """The source of prior_calls: engineer phase only.

        Exercises the real ``engineer_usage_totals`` fold over the
        usage_meter shape ``{comp_id: {phase: UsageTotals}}`` without
        standing up a whole pipeline.
        """
        from kstrl.pipeline import ComponentPipeline

        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))
        pipeline.usage_meter = {
            "comp-a": {
                "review": UsageTotals(
                    calls=3, known_calls=3, token_calls=3, total_tokens=900,
                ),
                "engineer": UsageTotals(
                    calls=2, known_calls=0, token_calls=0, total_tokens=0,
                ),
            },
            "comp-b": {
                "engineer": UsageTotals(
                    calls=1, known_calls=1, token_calls=1, total_tokens=50,
                ),
            },
        }
        engineer = ComponentPipeline.engineer_usage_totals(pipeline)
        assert engineer.calls == 3          # 2 + 1, review excluded
        assert engineer.token_calls == 1
        assert engineer.total_tokens == 50


class TestCompletionBoundaryBypass:
    """A loop that emits COMPLETE returns before its own budget check.

    Review regression on 22e99b4: that is the ORDINARY success path for a
    custom `agent_cmd` - each component finishes on its first tokenless
    call, the in-loop halt is never reached, and the engineer's tokenless
    count climbs across components while the cap never fires. The
    docstring's old claim that "the halt lands on the next loop" was
    false when the next loop also completes.

    The parent-side gate is what closes it: the run stops handing out NEW
    work once the cap provably cannot advance.
    """

    @staticmethod
    def _pipeline_with_engineer_usage(
        tmp_path: Path, calls: int, token_calls: int, cap: int,
    ) -> Any:
        from kstrl.pipeline import ComponentPipeline

        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))
        pipeline.usage_meter = {
            "comp-a": {
                "engineer": UsageTotals(
                    calls=calls, known_calls=0, token_calls=token_calls,
                    total_tokens=0,
                ),
            },
        }
        pipeline.factory_config = _factory_config(
            tmp_path, max_total_tokens=cap,
        )
        return pipeline

    def test_completed_tokenless_components_trip_the_parent_gate(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline_with_engineer_usage(
            tmp_path, calls=2, token_calls=0, cap=500_000,
        )
        reason = ComponentPipeline.token_budget_unenforceable(pipeline)
        assert reason is not None
        assert "cannot advance" in reason
        assert "refusing to schedule further components" in reason

    def test_one_completed_tokenless_component_is_not_enough(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline_with_engineer_usage(
            tmp_path, calls=1, token_calls=0, cap=500_000,
        )
        assert ComponentPipeline.token_budget_unenforceable(pipeline) is None

    def test_a_reporting_engineer_never_trips_the_gate(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline_with_engineer_usage(
            tmp_path, calls=9, token_calls=9, cap=500_000,
        )
        assert ComponentPipeline.token_budget_unenforceable(pipeline) is None

    def test_gate_is_inert_without_a_cap(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline_with_engineer_usage(
            tmp_path, calls=5, token_calls=0, cap=0,
        )
        assert ComponentPipeline.token_budget_unenforceable(pipeline) is None


# ---------------------------------------------------------------------------
# R8: [factory] max_cost_usd, the cost-denominated run ceiling
#
# WHY, measured rather than assumed. A real factory run halted on
# max_total_tokens = 500000 at 1,864,081 total tokens. Its own journal:
#
#     input_tokens           52
#     output_tokens          20,855
#     cache_read_tokens      1,781,669   <- 95.6%
#     cache_creation_tokens  61,505
#     total_tokens           1,864,081
#     cost_usd               1.216512
#
# UsageTotals.total_tokens counts cache reads at par with input tokens
# and cache reads cost roughly an order of magnitude less, so the
# operator who set a 500k "budget" expecting a spend ceiling was halted
# at $1.22. The token cap measures something real but nearly
# uncorrelated with money; these tests cover the ceiling that is not.
#
# The cost ceiling is NOT a hard cap and these tests do not pretend it
# is: the same run overshot its entire cap by 3.7x inside ONE engineer
# call of 376s, and nothing here would have interrupted it.
# ---------------------------------------------------------------------------


class TestInLoopCostBudget:
    def test_cost_overrun_halts_between_iterations(
        self, tmp_path: Path,
    ) -> None:
        """$0.03/iteration against a $0.05 ceiling means iteration 3
        never starts, even though max_iterations is 10."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_cost_usd=0.05),
        )
        assert result.completed is False
        assert result.iterations == 2
        assert len(agent.usage_records) == 2  # the agent really stopped
        assert "cost budget exceeded" in result.budget_halt_reason
        assert "max_cost_usd" in result.budget_halt_reason
        assert result.usage.cost_usd == pytest.approx(0.06)

    def test_prior_cost_from_earlier_components_counts(
        self, tmp_path: Path,
    ) -> None:
        """The ceiling is run-level: a worker launched with $0.045
        already on the run's meter has half a cent left, not $0.05."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(
                max_cost_usd=0.05, prior_cost_usd=0.045,
                prior_known_calls=1, prior_calls=1, prior_cost_calls=1,
            ),
        )
        assert result.iterations == 1
        assert "cost budget exceeded" in result.budget_halt_reason

    def test_zero_cost_cap_is_inert(self, tmp_path: Path) -> None:
        """0.0 = unbounded, matching the max_total_tokens convention."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 3), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_cost_usd=0.0),
        )
        assert result.iterations == 3
        assert result.budget_halt_reason == ""

    def test_token_ceiling_wins_when_it_is_the_tighter_one(
        self, tmp_path: Path,
    ) -> None:
        """Both set: whichever is reached first halts, and the message
        names THAT one. 300 tok + $0.03 per iteration against a 500-token
        / $100 pair trips the token ceiling at iteration 2."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=500, max_cost_usd=100.0),
        )
        assert result.iterations == 2
        assert "token budget exceeded" in result.budget_halt_reason
        assert "cost budget exceeded" not in result.budget_halt_reason

    def test_cost_ceiling_wins_when_it_is_the_tighter_one(
        self, tmp_path: Path,
    ) -> None:
        """The mirror, and the case the measured run is about: a token
        ceiling set high enough to be useless while the money runs out."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=1_000_000, max_cost_usd=0.05),
        )
        assert result.iterations == 2
        assert "cost budget exceeded" in result.budget_halt_reason
        assert "token budget exceeded" not in result.budget_halt_reason

    def test_cost_only_adapter_enforces_cost_while_the_token_cap_is_dead(
        self, tmp_path: Path,
    ) -> None:
        """The inconsistency this change resolves.

        An adapter reporting ``total_cost_usd`` with no ``usage`` dict
        (real: review finding P1-b on PR #176 established that
        ``known_calls`` increments on cost alone) makes the TOKEN ceiling
        provably dead - it can never advance. The old rule halted on
        that. But the COST ceiling is perfectly enforceable on the same
        adapter, so halting throws away a working ceiling.

        Here the loop keeps going past the point where the token cap is
        dead (iteration 2) and halts on the ceiling that actually works.
        """
        agent = SequenceUsageAgent([_COST_ONLY])  # $0.0227028 per call
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=500, max_cost_usd=0.05),
        )
        assert result.iterations == 3           # 3 x 0.0227028 >= 0.05
        assert "cost budget exceeded" in result.budget_halt_reason
        assert "unenforceable" not in result.budget_halt_reason
        assert result.usage.token_calls == 0    # token cap was dead
        assert result.usage.cost_calls == 3     # cost cap was not

    def test_token_only_adapter_enforces_tokens_while_the_cost_cap_is_dead(
        self, tmp_path: Path,
    ) -> None:
        """The converse, and just as real: codex reports a token total
        and no cost at all."""
        agent = SequenceUsageAgent([_REPORTED])  # 300 tokens, no cost
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=1000, max_cost_usd=100.0),
        )
        assert result.iterations == 4           # 4 x 300 >= 1000
        assert "token budget exceeded" in result.budget_halt_reason
        assert "unenforceable" not in result.budget_halt_reason
        assert result.usage.cost_calls == 0     # cost cap was dead throughout

    def test_unenforceable_only_when_every_configured_ceiling_is_dead(
        self, tmp_path: Path,
    ) -> None:
        """A wholly silent adapter kills both ceilings, and only then
        does the loop halt as unenforceable - naming both."""
        agent = SequenceUsageAgent([_UNREPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_total_tokens=500, max_cost_usd=0.05),
        )
        assert result.iterations == 2           # UNENFORCEABLE_CALLS
        assert "token budget unenforceable" in result.budget_halt_reason
        assert "cost budget unenforceable" in result.budget_halt_reason
        assert "max_total_tokens (500)" in result.budget_halt_reason
        assert "max_cost_usd ($0.05)" in result.budget_halt_reason

    def test_a_cost_ceiling_alone_is_dead_on_a_token_only_adapter(
        self, tmp_path: Path,
    ) -> None:
        """With ONLY max_cost_usd configured, a codex-style adapter that
        never reports a cost leaves it unable to fire. Before the cost
        ceiling existed this configuration had no protection at all."""
        agent = SequenceUsageAgent([_REPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_cost_usd=100.0),
        )
        assert result.iterations == 2
        assert "cost budget unenforceable" in result.budget_halt_reason
        assert "token budget" not in result.budget_halt_reason
        assert "2 costless call(s) this run" in result.budget_halt_reason

    def test_one_costless_call_is_an_incident_not_a_dead_ceiling(
        self, tmp_path: Path,
    ) -> None:
        """Symmetric with the token rule: a lone unparseable result must
        not kill a capped run."""
        agent = SequenceUsageAgent([_UNREPORTED, _BOTH, _BOTH])
        result = run_loop(
            _loop_config(tmp_path, 3), PlainUI(no_color=True), agent, tmp_path,
            budget=LoopBudget(max_cost_usd=100.0),
        )
        assert result.budget_halt_reason == ""
        assert result.iterations == 3

    def test_costless_threshold_does_not_reset_per_loop(
        self, tmp_path: Path,
    ) -> None:
        """The cost mirror of P1-a: the threshold is run-wide, threaded
        through the same priors ``_submit_args`` threads."""
        run_total = UsageTotals()
        reasons: list[str] = []
        for _ in range(3):
            agent = SequenceUsageAgent([_REPORTED])  # tokens, never a cost
            result = run_loop(
                _loop_config(tmp_path, 1), PlainUI(no_color=True), agent,
                tmp_path, budget=_budget_for(run_total, 0, cost_cap=100.0),
            )
            run_total.merge(result.usage)
            reasons.append(result.budget_halt_reason)

        assert reasons[0] == ""                      # one is an incident
        assert "cost budget unenforceable" in reasons[1]
        assert "cost budget unenforceable" in reasons[2]
        assert run_total.calls == 3
        assert run_total.cost_calls == 0

    def test_a_loop_that_has_not_run_cannot_halt_on_cost(self) -> None:
        """No calls means no evidence either way; a worker launched into
        a run with costless priors must still get to run its engineer."""
        budget = LoopBudget(
            max_cost_usd=100.0, prior_calls=20, prior_cost_calls=0,
        )
        assert budget.halt_reason(UsageTotals()) is None

    def test_no_ceiling_configured_is_always_none(self) -> None:
        budget = LoopBudget(prior_calls=20, prior_cost_calls=0)
        assert budget.halt_reason(
            UsageTotals(calls=5, known_calls=0),
        ) is None


class TestCostCeilingParentGates:
    """The parent-side twins: the scheduling gate and the phase gates.

    Built on the real ``ComponentPipeline`` methods over a hand-built
    usage_meter, the same shape ``TestCompletionBoundaryBypass`` uses -
    no whole pipeline needed to pin the arithmetic and the naming.
    """

    @staticmethod
    def _pipeline(
        tmp_path: Path,
        *,
        calls: int = 0,
        token_calls: int = 0,
        cost_calls: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
        max_total_tokens: int = 0,
        max_cost_usd: float = 0.0,
    ) -> Any:
        from kstrl.pipeline import ComponentPipeline

        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))
        engineer = UsageTotals(
            calls=calls, known_calls=max(token_calls, cost_calls),
            token_calls=token_calls, cost_calls=cost_calls,
            total_tokens=total_tokens, cost_usd=cost_usd,
        )
        pipeline.usage_meter = {"comp-a": {"engineer": engineer}}
        pipeline.run_usage = UsageTotals()
        pipeline.run_usage.merge(engineer)
        pipeline.factory_config = _factory_config(
            tmp_path, max_total_tokens=max_total_tokens,
            max_cost_usd=max_cost_usd,
        )
        return pipeline

    def test_cost_overrun_is_detected_and_named(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path, calls=1, cost_calls=1, cost_usd=0.25, max_cost_usd=0.10,
        )
        assert ComponentPipeline.cost_budget_exceeded(p) is True
        assert ComponentPipeline.token_budget_exceeded(p) is False
        assert ComponentPipeline.breached_ceiling(p) == "max_cost_usd"
        assert ComponentPipeline.budget_exceeded(p) is True

    def test_token_overrun_still_named_max_total_tokens(
        self, tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path, calls=1, token_calls=1, total_tokens=900,
            max_total_tokens=500,
        )
        assert ComponentPipeline.breached_ceiling(p) == "max_total_tokens"

    def test_zero_cost_ceiling_never_trips(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path, calls=1, cost_calls=1, cost_usd=99.0, max_cost_usd=0.0,
        )
        assert ComponentPipeline.cost_budget_exceeded(p) is False
        assert ComponentPipeline.breached_ceiling(p) is None

    def test_a_live_cost_ceiling_keeps_a_dead_token_one_from_halting(
        self, tmp_path: Path,
    ) -> None:
        """The scheduling gate must not stop a run whose cost ceiling can
        still fire, even though its token ceiling provably cannot."""
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path, calls=2, token_calls=0, cost_calls=2, cost_usd=0.01,
            max_total_tokens=500_000, max_cost_usd=100.0,
        )
        assert ComponentPipeline.token_budget_unenforceable(p) is not None
        assert ComponentPipeline.cost_budget_unenforceable(p) is None
        assert ComponentPipeline.budget_unenforceable(p) is None

    def test_a_live_token_ceiling_keeps_a_dead_cost_one_from_halting(
        self, tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path, calls=2, token_calls=2, cost_calls=0, total_tokens=100,
            max_total_tokens=500_000, max_cost_usd=100.0,
        )
        assert ComponentPipeline.cost_budget_unenforceable(p) is not None
        assert ComponentPipeline.token_budget_unenforceable(p) is None
        assert ComponentPipeline.budget_unenforceable(p) is None

    def test_both_dead_halts_and_names_both(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path, calls=2, token_calls=0, cost_calls=0,
            max_total_tokens=500_000, max_cost_usd=100.0,
        )
        reason = ComponentPipeline.budget_unenforceable(p)
        assert reason is not None
        assert "max_total_tokens" in reason
        assert "max_cost_usd" in reason
        assert "refusing to schedule further components" in reason

    def test_a_lone_cost_ceiling_can_be_the_only_dead_one(
        self, tmp_path: Path,
    ) -> None:
        """Only max_cost_usd configured: it is the only ceiling that has
        to be alive, so its death halts the gate on its own."""
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path, calls=2, token_calls=2, cost_calls=0, total_tokens=100,
            max_cost_usd=100.0,
        )
        reason = ComponentPipeline.budget_unenforceable(p)
        assert reason is not None
        assert "cost budget unenforceable" in reason

    def test_gate_is_inert_with_no_ceiling_configured(
        self, tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(tmp_path, calls=9, token_calls=0, cost_calls=0)
        assert ComponentPipeline.budget_unenforceable(p) is None


class TestCostCeilingEndToEnd:
    def test_cost_halt_names_the_ceiling_everywhere_it_is_recorded(
        self, tmp_path: Path,
    ) -> None:
        """One run, every audit surface: the component error, the typed
        finding, and the budget_exceeded event all name max_cost_usd.
        The token ceiling is off, so nothing may claim it tripped."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest([_component("comp-a"), _component("comp-b")])
        config = _factory_config(root, max_cost_usd=0.10)

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            return ComponentResult(
                str(args[0]), success=True, iterations=1,
                usage=_engineer_usage(600, cost=0.25),
            )

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert "comp-a" in result.failed
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.status == ComponentStatus.FAILED.value
        assert "max_cost_usd" in (comp_a.error or "")
        assert "max_total_tokens" not in (comp_a.error or "")
        budget_findings = [
            f for f in comp_a.findings
            if f.is_infrastructure_error and "cost budget" in f.explanation
        ]
        assert len(budget_findings) == 1
        assert budget_findings[0].phase == "engineer"

        # comp-b never launches: the scheduling gate fails it loudly too.
        assert "comp-b" in result.failed

        events = ProgressLog(root / "progress.jsonl").read_events()
        breaches = [e for e in events if e["event"] == "budget_exceeded"]
        assert len(breaches) == 2
        assert breaches[0]["data"]["ceiling"] == "max_cost_usd"
        assert breaches[0]["data"]["max_cost_usd"] == 0.10
        assert breaches[0]["data"]["cost_usd"] >= 0.10

    def test_token_halt_audit_trail_is_unchanged(self, tmp_path: Path) -> None:
        """The token ceiling keeps working exactly as before, and its
        event now carries ceiling="max_total_tokens" alongside the
        pre-existing fields older readers look up."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, max_total_tokens=500)
        success = ComponentResult(
            "comp-a", success=True, iterations=1, usage=_engineer_usage(600),
        )

        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        events = ProgressLog(root / "progress.jsonl").read_events()
        breach = next(e for e in events if e["event"] == "budget_exceeded")
        assert breach["data"]["ceiling"] == "max_total_tokens"
        assert breach["data"]["max_total_tokens"] == 500
        assert breach["data"]["total_tokens"] >= 500

    def test_scheduler_hands_the_cost_ceiling_and_priors_down(
        self, tmp_path: Path,
    ) -> None:
        """``_submit_args`` must snapshot the cost priors per launch, or
        the in-loop cost check degrades to a per-component budget."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest([
            _component("comp-a"), _component("comp-b", deps=["comp-a"]),
        ])
        config = _factory_config(root, max_cost_usd=100.0)
        budgets: list[Any] = []

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            budgets.append(args[-1])
            return ComponentResult(
                str(args[0]), success=True, iterations=1,
                usage=_engineer_usage(700, cost=0.5),
            )

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True), root,
            )

        assert len(budgets) == 2
        assert budgets[0].max_cost_usd == 100.0
        assert budgets[0].prior_cost_usd == 0.0
        assert budgets[0].prior_cost_calls == 0
        assert budgets[1].max_cost_usd == 100.0
        assert budgets[1].prior_cost_usd == pytest.approx(0.5)
        assert budgets[1].prior_cost_calls == 1


class TestFailedSnapshotDeletionInvalidates:
    """Deletion IS the attempt-scoping invariant.

    Review regression on 22e99b4: `_clear_partial_usage` swallowed every
    OSError, so a snapshot that could not be deleted stayed addressable
    as the current attempt and was salvaged again on top of the totals
    `process_result` had already recorded.
    """

    def test_missing_file_is_the_clean_case(self, tmp_path: Path) -> None:
        assert _clear_partial_usage(tmp_path / "nope.json") is True

    def test_successful_delete_reports_clean(self, tmp_path: Path) -> None:
        target = tmp_path / "usage.json"
        target.write_text("{}")
        assert _clear_partial_usage(target) is True
        assert not target.exists()

    def test_failed_delete_reports_unsafe(self, tmp_path: Path) -> None:
        target = tmp_path / "usage.json"
        target.write_text("{}")
        with patch.object(
            Path, "unlink", side_effect=PermissionError("read-only"),
        ):
            assert _clear_partial_usage(target) is False
        assert target.exists()      # still there, hence unsafe

    def test_unsafe_attempt_refuses_disk_salvage(self, tmp_path: Path) -> None:
        """The point of the flag: a stale 700 must not be recounted."""
        from concurrent.futures import Future

        from kstrl.pipeline import ComponentPipeline

        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))
        pipeline.usage_meter = {}
        pipeline.run_usage = UsageTotals()
        pipeline._usage_salvage_unsafe = set()
        pipeline.usage_paths = RunPaths.for_run(tmp_path, "run-1")
        recorded: list[UsageTotals] = []
        pipeline.record_engineer_usage = lambda _c, totals: recorded.append(totals)

        snapshot = pipeline.usage_paths.engineer_usage("comp-a")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        _write_partial_usage(
            snapshot,
            UsageTotals(calls=1, known_calls=1, token_calls=1, total_tokens=700),
        )

        pending: Future[Any] = Future()      # never completes
        ComponentPipeline.mark_usage_salvage_unsafe(pipeline, "comp-a")
        _salvage_aborted_usage(pending, "comp-a", pipeline)
        assert recorded == [], "a stale snapshot must not be salvaged"

        # And the safe path still salvages, so the guard is not a blanket off.
        ComponentPipeline.mark_usage_salvage_safe(pipeline, "comp-a")
        _salvage_aborted_usage(pending, "comp-a", pipeline)
        assert [t.total_tokens for t in recorded] == [700]
