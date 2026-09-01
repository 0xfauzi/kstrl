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
import stat
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from kstrl.agents.base import (
    ARCHITECT_COMPONENT,
    ARCHITECT_ROLE,
    UsageRecord,
    UsageTotals,
    collect_usage,
    format_usage_rollup,
    usage_cursor,
)
from kstrl.agents.claude_code import ClaudeCodeAgent, _usage_from_result_event
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.config import KstrlConfig
from kstrl.events import RunPaths
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    FactoryLockHeldError,
    FactoryResult,
    _clear_partial_usage,
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
        totals.add_record(
            UsageRecord(
                total_tokens=14511,
                duration_seconds=3.0,
                source="codex-text",
            )
        )
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
        totals.add_record(
            UsageRecord(
                cost_usd=0.0227028,
                duration_seconds=1.8,
                source="claude-stream-json",
            )
        )
        totals.add_record(
            UsageRecord(
                cost_usd=0.0104,
                duration_seconds=2.1,
                source="claude-stream-json",
            )
        )
        assert totals.calls == 2
        assert totals.known_calls == 2  # unchanged semantics
        assert totals.unreported_calls == 0  # unchanged semantics
        assert totals.token_calls == 0  # the honest token coverage
        assert totals.tokenless_calls == 2
        assert totals.total_tokens == 0
        assert totals.cost_usd == pytest.approx(0.0331028)

    def test_token_calls_counts_only_token_bearing_records(self) -> None:
        totals = UsageTotals()
        totals.add_record(_claude_record())  # parts
        totals.add_record(UsageRecord(total_tokens=10))  # total
        totals.add_record(UsageRecord(cost_usd=0.5))  # cost
        totals.add_record(UsageRecord(duration_seconds=1.0))  # silence
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
        totals.add_record(_claude_record())  # both
        totals.add_record(UsageRecord(total_tokens=10))  # tokens
        totals.add_record(UsageRecord(cost_usd=0.5))  # cost
        totals.add_record(UsageRecord(duration_seconds=1.0))  # silence
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
        assert totals.known_calls == 2  # the misleading signal
        assert totals.cost_calls == 0  # the honest one
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
        totals.add_record(
            UsageRecord(
                input_tokens=True,  # type: ignore[arg-type]
                output_tokens=-5,
                cost_usd=-1.0,
            )
        )
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
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, "kstrl.agents.claude_code"):
            record = _usage_from_result_event("{not json", 5.0)
        assert record.source == "parse-error"
        assert record.total_tokens is None
        assert any("usage" in r.message for r in caplog.records)

    def test_event_without_usage_dict_warns_not_raises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, "kstrl.agents.claude_code"):
            record = _usage_from_result_event(
                json.dumps({"type": "result", "result": "hi"}),
                5.0,
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
        mock_proc.stdout = iter(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "working"},
                            ]
                        },
                    }
                )
                + "\n",
                json.dumps(CLAUDE_RESULT_EVENT) + "\n",
            ]
        )
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            agent = ClaudeCodeAgent()
            list(agent.run("prompt", cwd=tmp_path))

        assert len(agent.usage_records) == 1
        assert agent.usage_records[0].source == "claude-stream-json"
        assert agent.usage_records[0].total_tokens == 9 + 42 + 17418 + 10371

    def test_agent_run_without_result_event_records_unavailable(
        self,
        tmp_path: Path,
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lines: list[str],
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
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Verbatim tail of the codex 0.134.0 probe output.
        agent = _run_codex_with_stdout(
            monkeypatch,
            tmp_path,
            [
                "codex\n",
                "hello\n",
                "tokens used\n",
                "14,511\n",
                "hello\n",
            ],
        )
        assert len(agent.usage_records) == 1
        record = agent.usage_records[0]
        assert record.source == "codex-text"
        assert record.total_tokens == 14511
        assert record.input_tokens is None  # codex reports only a total

    def test_single_line_trailer_variant(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        agent = _run_codex_with_stdout(
            monkeypatch,
            tmp_path,
            [
                "hello\n",
                "tokens used: 1,234\n",
            ],
        )
        assert agent.usage_records[0].total_tokens == 1234

    def test_no_trailer_falls_back_to_calls_plus_wall_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        agent = _run_codex_with_stdout(monkeypatch, tmp_path, ["hello\n"])
        assert len(agent.usage_records) == 1
        record = agent.usage_records[0]
        assert record.source == "unavailable"
        assert record.total_tokens is None
        assert record.duration_seconds >= 0.0

    def test_non_numeric_after_tokens_used_never_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        agent = _run_codex_with_stdout(
            monkeypatch,
            tmp_path,
            [
                "tokens used\n",
                "not a number\n",
            ],
        )
        assert agent.usage_records[0].total_tokens is None

    def test_last_trailer_wins_over_echoed_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        agent = _run_codex_with_stdout(
            monkeypatch,
            tmp_path,
            [
                "tokens used\n",
                "111\n",
                "more output\n",
                "tokens used\n",
                "222\n",
            ],
        )
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
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
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
    (kstrl_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')
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
            input_tokens=100,
            output_tokens=200,
            total_tokens=300,
            cost_usd=0.01,
            duration_seconds=1.0,
            source="claude-stream-json",
        )
        agent = FakeUsageAgent(
            outputs=[["working..."], [COMPLETION_MARKER]],
            record=record,
        )
        result = run_loop(
            _loop_config(tmp_path, 5),
            PlainUI(no_color=True),
            agent,
            tmp_path,
        )
        assert result.completed is True
        assert result.iterations == 2
        assert result.usage.calls == 2
        assert result.usage.input_tokens == 200
        assert result.usage.output_tokens == 400
        assert result.usage.total_tokens == 600
        assert result.usage.cost_usd == pytest.approx(0.02)

    def test_usage_present_on_max_iterations_failure(
        self,
        tmp_path: Path,
    ) -> None:
        record = UsageRecord(total_tokens=50, source="codex-text")
        agent = FakeUsageAgent(outputs=[["no marker"]], record=record)
        result = run_loop(
            _loop_config(tmp_path, 3),
            PlainUI(no_color=True),
            agent,
            tmp_path,
        )
        assert result.completed is False
        assert result.usage.calls == 3
        assert result.usage.total_tokens == 150

    def test_agent_without_usage_records_yields_empty_totals(
        self,
        tmp_path: Path,
    ) -> None:
        class BareAgent:
            name = "bare"
            final_message = None

            def run(
                self,
                prompt: str,
                cwd: Path | None = None,
                timeout: float | None = None,
            ) -> Iterator[str]:
                yield COMPLETION_MARKER

        result = run_loop(
            _loop_config(tmp_path, 1),
            PlainUI(no_color=True),
            BareAgent(),
            tmp_path,
        )
        assert result.completed is True
        assert result.usage.calls == 0

    def test_malformed_usage_records_never_crash_the_loop(
        self,
        tmp_path: Path,
    ) -> None:
        agent = FakeUsageAgent(
            outputs=[[COMPLETION_MARKER]],
            records=[None, "garbage", 42],
        )
        result = run_loop(
            _loop_config(tmp_path, 1),
            PlainUI(no_color=True),
            agent,
            tmp_path,
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
    (kstrl_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')
    # Knowledge distillation off by default in these tests: its agent
    # call would add nondeterministic usage rows.
    (tmp_path / "kstrl.toml").write_text("[knowledge]\nenabled = false\n")
    for comp_id in component_ids:
        feature_dir = kstrl_dir / "feature" / comp_id
        feature_dir.mkdir(parents=True, exist_ok=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
    return tmp_path


def _component(comp_id: str, deps: list[str] | None = None) -> Component:
    return Component(
        comp_id,
        comp_id.title(),
        "Desc",
        deps or [],
        f"scripts/kstrl/feature/{comp_id}/prd.json",
        f"kstrl/factory/{comp_id}",
    )


def _factory_config(tmp_path: Path, **overrides: Any) -> FactoryConfig:
    defaults: dict[str, Any] = dict(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
        progress_log_path=tmp_path / "progress.jsonl",
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


def _engineer_usage(total: int, cost: float = 0.0) -> UsageTotals:
    totals = UsageTotals()
    totals.add_record(
        UsageRecord(
            input_tokens=total // 3,
            output_tokens=total - total // 3,
            total_tokens=total,
            cost_usd=cost or None,
            duration_seconds=1.0,
            source="claude-stream-json",
        )
    )
    return totals


def _architect_usage(cost: float | None, tokens: int = 1000) -> UsageTotals:
    """What `ks factory`'s decompose leaves on its agent (#257).

    The architect's record has the same shape as any other role's, so
    this is ``_engineer_usage`` under the name that makes the call sites
    readable. ``cost=None`` is the codex shape - a token count and no
    price - which is what the coverage accounting exists to describe.
    """
    return _engineer_usage(tokens, cost=cost or 0.0)


def _usage_events(root: Path, phase: str) -> list[dict[str, Any]]:
    """Every usage event one phase recorded, in order."""
    return [
        e
        for e in ProgressLog(root / "progress.jsonl").read_events()
        if e["event"] == "component_usage" and e["data"]["phase"] == phase
    ]


def _engineer_usage_events(root: Path) -> list[int]:
    """Every engineer-phase usage total the run recorded, in order.

    A list (not a sum) on purpose: a double count and a correct total
    are the same number of tokens but a different number of events.
    """
    return [int(e["data"]["total_tokens"]) for e in _usage_events(root, "engineer")]


def _assert_journal_rows_sum_to_tsv(run: _SeededRun, *, expected: float) -> None:
    """Every journal usage row adds up to the TSV's run total.

    The #257 review's property: ``record_run`` builds rows by walking the
    manifest, so a usage key belonging to no component was dropped while
    ``run_usage`` - which includes it - fed ``total_cost_usd``. Shared
    because #281 re-asserts it on a run whose component is NAMED for the
    role: splitting a key is exactly the kind of change that drops a row
    instead of moving it, and this arithmetic is what notices.
    """
    rows = sum(
        phase["cost_usd"]
        for e in _read_journal(run.root)
        if e.get("event_type") in ("component_result", "role_usage")
        for phase in e.get("usage", {}).values()
    )
    tsv = (run.root / ".kstrl" / "experiments.tsv").read_text().splitlines()
    # Keyed by header, not by index: a column appended to the TSV
    # must not silently move what this reads.
    totals = dict(zip(tsv[0].split("\t"), tsv[-1].split("\t"), strict=True))
    assert rows == pytest.approx(expected)
    assert rows == pytest.approx(float(totals["total_cost_usd"]))


def _read_journal(tmp_path: Path) -> list[dict[str, Any]]:
    journal_path = tmp_path / ".kstrl" / "evolution.jsonl"
    entries = []
    for line in journal_path.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


class TestFactoryUsageAggregation:
    def test_engineer_usage_lands_in_journal_tsv_and_log(
        self,
        tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root)
        success = ComponentResult(
            "comp-a",
            success=True,
            iterations=2,
            usage=_engineer_usage(1200, cost=0.05),
        )
        ui_buffer = io.StringIO()

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True, file=ui_buffer),
                root,
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
            "comp-a",
            success=True,
            iterations=1,
            usage=_engineer_usage(1000),
        )
        review_agent = FakeUsageAgent(outputs=[["ok"]])
        review_agent._usage_records.append(
            UsageRecord(
                input_tokens=10,
                output_tokens=20,
                total_tokens=30,
                duration_seconds=0.5,
                source="claude-stream-json",
            )
        )

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.git.get_diff_content",
                return_value="",
            ),
            patch(
                "kstrl.agents.get_agent",
                return_value=review_agent,
            ),
            patch(
                "kstrl.factory.run_review",
                return_value=ReviewResult(passed=True, mode="advisory"),
            ),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
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
            "comp-a",
            success=True,
            iterations=1,
            usage=_engineer_usage(1000),
        )

        # Each phase's get_agent call yields a fresh fake preloaded with
        # a distinct spend (review 30, security 40, distill 50).
        phase_tokens = iter((30, 40, 50))

        def make_agent(*args: Any, **kwargs: Any) -> FakeUsageAgent:
            agent = FakeUsageAgent(outputs=[["ok"]])
            agent._usage_records.append(
                UsageRecord(
                    total_tokens=next(phase_tokens),
                    duration_seconds=0.1,
                    source="claude-stream-json",
                )
            )
            return agent

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.git.get_diff_content",
                return_value="",
            ),
            patch(
                "kstrl.agents.get_agent",
                side_effect=make_agent,
            ),
            patch(
                "kstrl.factory.run_review",
                return_value=ReviewResult(passed=True, mode="advisory"),
            ),
            patch(
                "kstrl.factory.run_security_review",
                return_value=SecurityResult(passed=True, mode="advisory"),
            ),
            patch(
                "kstrl.factory.distill_facts",
                return_value=(1, "ok"),
            ),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
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
        self,
        tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root)
        success = ComponentResult("comp-a", success=True, iterations=1)

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
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
        self,
        tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root)
        fallback = UsageTotals()
        fallback.add_record(UsageRecord(duration_seconds=4.0))
        success = ComponentResult(
            "comp-a",
            success=True,
            iterations=1,
            usage=fallback,
        )
        ui_buffer = io.StringIO()

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True, file=ui_buffer),
                root,
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
                comp_id,
                success=True,
                iterations=1,
                usage=_engineer_usage(600),
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
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
            f
            for f in comp_a.findings
            if f.is_infrastructure_error and "token budget" in f.explanation
        ]
        assert len(budget_findings) == 1
        assert budget_findings[0].phase == "engineer"

        # comp-b never launches: the scheduling gate fails it loudly too.
        assert "comp-b" in result.failed
        comp_b = manifest.get_component("comp-b")
        assert comp_b is not None
        assert any(f.is_infrastructure_error and f.phase == "scheduling" for f in comp_b.findings)

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
            "comp-a",
            success=True,
            iterations=1,
            usage=_engineer_usage(10_000_000),
        )

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
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
            "comp-a",
            success=True,
            iterations=1,
            usage=fallback,
        )

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
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
        self,
        records: list[UsageRecord],
        outputs: list[str] | None = None,
    ) -> None:
        self._records = records
        self._outputs = outputs or ["working..."]
        self._usage_records: list[UsageRecord] = []

    @property
    def name(self) -> str:
        return "sequence-usage"

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
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
    cost_usd=0.0227028,
    duration_seconds=1.8,
    source="claude-stream-json",
)
# Both axes, as the claude adapter reports on a healthy call.
_BOTH = UsageRecord(
    total_tokens=300,
    cost_usd=0.03,
    duration_seconds=1.8,
    source="claude-stream-json",
)


def _budget_for(
    run_total: UsageTotals,
    cap: int,
    cost_cap: float = 0.0,
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
        self,
        tmp_path: Path,
    ) -> None:
        """The loop must stop ITSELF: 300 tokens/iteration against a 500
        cap means iteration 3 never starts, even though max_iterations
        is 10 and no phase boundary has been reached."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
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
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(
                max_total_tokens=500,
                prior_total_tokens=450,
                prior_known_calls=1,
                prior_calls=1,
                prior_token_calls=1,
            ),
        )
        assert result.iterations == 1
        assert len(agent.usage_records) == 1
        assert "750" in result.budget_halt_reason

    def test_zero_cap_is_unbounded(self, tmp_path: Path) -> None:
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 3),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=0),
        )
        assert result.iterations == 3
        assert result.budget_halt_reason == ""

    def test_no_budget_argument_keeps_pre_r8_behaviour(
        self,
        tmp_path: Path,
    ) -> None:
        """`ks run` / `ks feature` pass no budget; nothing changes."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 3),
            PlainUI(no_color=True),
            agent,
            tmp_path,
        )
        assert result.iterations == 3
        assert result.budget_halt_reason == ""

    def test_completion_in_the_breaching_iteration_still_completes(
        self,
        tmp_path: Path,
    ) -> None:
        """Ordering: an iteration that finished the work is a success.
        The overrun is then caught by the phase-boundary check, which is
        exactly the pre-R8 path - the loop does not retro-fail work that
        is already done."""
        agent = FakeUsageAgent(
            outputs=[[COMPLETION_MARKER]],
            record=_REPORTED,
        )
        result = run_loop(
            _loop_config(tmp_path, 5),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=100),
        )
        assert result.completed is True
        assert result.budget_halt_reason == ""

    def test_wholly_unreported_usage_halts_as_unenforceable(
        self,
        tmp_path: Path,
    ) -> None:
        """Unknown usage is NOT treated as zero when nothing at all has
        reported: a cap that provably cannot trip is the defect being
        fixed, so the loop halts loudly instead of running to
        max_iterations under a dead ceiling."""
        agent = SequenceUsageAgent([_UNREPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.completed is False
        assert result.iterations == 2  # _UNENFORCEABLE_CALLS
        assert "unenforceable" in result.budget_halt_reason
        assert result.usage.total_tokens == 0
        assert result.usage.unreported_calls == 2

    def test_one_silent_call_is_an_incident_not_a_dead_cap(
        self,
        tmp_path: Path,
    ) -> None:
        """A single non-reporting call (a timed-out or unparseable
        iteration on an adapter that normally reports) must not kill the
        run: iteration 2 reports, the cap is demonstrably alive, and the
        loop continues until the arithmetic trips it."""
        agent = SequenceUsageAgent([_UNREPORTED, _REPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.iterations == 3  # 0 + 300 + 300 >= 500
        assert "token budget exceeded" in result.budget_halt_reason
        assert result.usage.unreported_calls == 1

    def test_unreported_usage_is_free_when_the_cap_is_off(
        self,
        tmp_path: Path,
    ) -> None:
        """The unenforceable halt is a property of an ENABLED cap. With
        no cap there is nothing to enforce and a non-reporting adapter
        (CustomAgent) runs exactly as before."""
        agent = SequenceUsageAgent([_UNREPORTED])
        result = run_loop(
            _loop_config(tmp_path, 3),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=0),
        )
        assert result.iterations == 3
        assert result.budget_halt_reason == ""

    def test_mixed_reporting_counts_unknown_as_zero_and_keeps_going(
        self,
        tmp_path: Path,
    ) -> None:
        """One unreported iteration among reporting ones does not halt:
        the total stays a documented lower bound that still grows toward
        the cap. Here 300 (reported) + unknown + 300 trips at iteration
        3, one iteration later than the true spend would have."""
        agent = SequenceUsageAgent([_REPORTED, _UNREPORTED, _REPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.iterations == 3
        assert "token budget exceeded" in result.budget_halt_reason
        assert result.usage.calls == 3
        assert result.usage.unreported_calls == 1
        assert result.usage.total_tokens == 600

    def test_prior_reported_calls_do_not_disarm_the_unenforceable_halt(
        self,
        tmp_path: Path,
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
            _loop_config(tmp_path, 5),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(
                max_total_tokens=500,
                prior_total_tokens=100,
                prior_known_calls=1,
                prior_calls=1,
                prior_token_calls=1,
            ),
        )
        assert result.iterations == 2  # halts at _UNENFORCEABLE_CALLS
        assert "unenforceable" in result.budget_halt_reason
        assert not result.completed

    def test_one_silent_call_alongside_prior_reporting_keeps_going(
        self,
        tmp_path: Path,
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
            _loop_config(tmp_path, 3),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(
                max_total_tokens=500_000,
                prior_total_tokens=100,
                prior_known_calls=1,
                prior_calls=1,
                prior_token_calls=1,
            ),
        )
        assert result.budget_halt_reason == ""
        assert result.iterations == 3

    def test_cost_only_reporting_is_not_token_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        """Review regression (P1-b): an adapter that reports cost but no
        tokens moved ``known_calls`` and so looked fully instrumented,
        while ``total_tokens`` stayed 0 and the cap could never trip.
        The loop ran to max_iterations under a dead ceiling. It now
        halts at the threshold like any other tokenless adapter.
        """
        agent = SequenceUsageAgent([_COST_ONLY])
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=500),
        )
        assert result.iterations == 2  # not the configured 10
        assert "unenforceable" in result.budget_halt_reason
        assert result.usage.known_calls == 2  # the misleading signal
        assert result.usage.token_calls == 0  # the honest one
        assert result.usage.total_tokens == 0

    def test_unenforceable_threshold_does_not_reset_per_loop(
        self,
        tmp_path: Path,
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
                _loop_config(tmp_path, 1),
                PlainUI(no_color=True),
                agent,
                tmp_path,
                budget=_budget_for(run_total, 500),
            )
            run_total.merge(result.usage)
            reasons.append(result.budget_halt_reason)

        assert reasons[0] == ""  # one is an incident
        assert "unenforceable" in reasons[1]  # two is the adapter
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
            max_total_tokens=500,
            prior_calls=20,
            prior_token_calls=0,
        )
        assert budget.halt_reason(UsageTotals()) is None

    def test_a_tokenless_call_after_an_earlier_one_halts_immediately(
        self,
        tmp_path: Path,
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
            _loop_config(tmp_path, 5),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(
                max_total_tokens=500_000,
                prior_total_tokens=900,
                prior_known_calls=2,
                prior_calls=3,
                prior_token_calls=2,
            ),
        )
        assert result.iterations == 1
        assert "unenforceable" in result.budget_halt_reason
        assert "2 tokenless call(s) this run" in result.budget_halt_reason

    def test_iteration_usage_callback_sees_cumulative_totals(
        self,
        tmp_path: Path,
    ) -> None:
        seen: list[int] = []
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        run_loop(
            _loop_config(tmp_path, 3),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            on_iteration_usage=lambda totals: seen.append(totals.total_tokens),
        )
        assert seen == [300, 600, 900]

    def test_iteration_usage_callback_failure_never_breaks_the_loop(
        self,
        tmp_path: Path,
    ) -> None:
        def explode(totals: UsageTotals) -> None:
            raise OSError("disk full")

        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)
        result = run_loop(
            _loop_config(tmp_path, 2),
            PlainUI(no_color=True),
            agent,
            tmp_path,
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
            model=None,
            reasoning=None,
            agent_type=None,
            sleep_seconds=0.0,
            max_iterations=10,
            events_dir_str=None,
            run_id="run-b",
            redirect_output=False,  # NEVER dup2 inside the test process
        )
        args.update(overrides)
        return args

    def test_worker_reports_budget_exceeded_and_stops_early(
        self,
        tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)

        with patch("kstrl.agents.get_agent", return_value=agent):
            result = _run_component(
                **self._worker_args(
                    root,
                    token_budget=LoopBudget(max_total_tokens=500),
                )
            )

        assert result.success is False
        assert result.budget_exceeded is True
        assert result.iterations == 2  # not the configured 10
        assert "token budget exceeded" in (result.error or "")
        assert result.usage is not None
        assert result.usage.total_tokens == 600

    def test_worker_without_a_budget_runs_to_max_iterations(
        self,
        tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        agent = FakeUsageAgent(outputs=[["working..."]], record=_REPORTED)

        with patch("kstrl.agents.get_agent", return_value=agent):
            result = _run_component(
                **self._worker_args(
                    root,
                    max_iterations=3,
                )
            )

        assert result.budget_exceeded is False
        assert result.iterations == 3

    def test_worker_persists_usage_at_every_iteration_boundary(
        self,
        tmp_path: Path,
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
            _run_component(
                **self._worker_args(
                    root,
                    max_iterations=2,
                    events_dir_str=str(usage_dir),
                    usage_dir_str=str(usage_dir),
                )
            )

        snapshot = _read_partial_usage(RunPaths(root=usage_dir).engineer_usage("comp-a"))
        assert snapshot is not None
        assert snapshot.total_tokens == 600
        assert snapshot.calls == 2

    def test_worker_persists_usage_without_an_events_dir(
        self,
        tmp_path: Path,
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
            _run_component(
                **self._worker_args(
                    root,
                    max_iterations=2,
                    events_dir_str=None,
                    usage_dir_str=str(usage_dir),
                )
            )

        snapshot = _read_partial_usage(RunPaths(root=usage_dir).engineer_usage("comp-a"))
        assert snapshot is not None
        assert snapshot.total_tokens == 600
        # No events were written: the opt-out is still honored.
        assert not (usage_dir / "events.jsonl").exists()


class TestSchedulerHandsDownTheBudget:
    def test_worker_is_told_the_cap_and_what_the_run_already_spent(
        self,
        tmp_path: Path,
    ) -> None:
        """The cap is run-level, so the second worker must launch with
        the first component's spend already on its meter - otherwise the
        in-loop check degrades to a per-component budget. Also pins the
        positional contract between _submit_args and _run_component: the
        budget is the last element of the submit tuple, which is how the
        process-pool path passes it."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest(
            [
                _component("comp-a"),
                _component("comp-b", deps=["comp-a"]),
            ]
        )
        config = _factory_config(root, max_total_tokens=100_000)
        budgets: list[Any] = []

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            budgets.append(args[-1])
            return ComponentResult(
                str(args[0]),
                success=True,
                iterations=1,
                usage=_engineer_usage(700),
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )

        assert len(budgets) == 2
        assert all(isinstance(b, LoopBudget) for b in budgets)
        assert budgets[0] == LoopBudget(
            max_total_tokens=100_000,
            prior_total_tokens=0,
            prior_known_calls=0,
            prior_calls=0,
            prior_token_calls=0,
        )
        # prior_calls / prior_token_calls are the channel that stops the
        # unenforceable threshold resetting per component (P1-a).
        assert budgets[1] == LoopBudget(
            max_total_tokens=100_000,
            prior_total_tokens=700,
            prior_known_calls=1,
            prior_calls=1,
            prior_token_calls=1,
        )

    def test_tokenless_prior_calls_reach_the_next_worker(
        self,
        tmp_path: Path,
    ) -> None:
        """Review regression (P1-a), scheduler half: a component whose
        engineer reported nothing must leave that fact on the NEXT
        worker's budget, or the run-wide threshold cannot exist."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest(
            [
                _component("comp-a"),
                _component("comp-b", deps=["comp-a"]),
            ]
        )
        config = _factory_config(root, max_total_tokens=100_000)
        budgets: list[Any] = []
        silent = UsageTotals()
        silent.add_record(_UNREPORTED)

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            budgets.append(args[-1])
            return ComponentResult(
                str(args[0]),
                success=True,
                iterations=1,
                usage=silent,
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )

        assert budgets[1].prior_calls == 1
        assert budgets[1].prior_token_calls == 0

    def test_no_cap_still_hands_down_a_disabled_budget(
        self,
        tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        budgets: list[Any] = []

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            budgets.append(args[-1])
            return ComponentResult(str(args[0]), success=True, iterations=1)

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                _factory_config(root),
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )

        assert [b.enabled for b in budgets] == [False]


class TestInLoopHaltAuditState:
    def test_same_audit_state_as_a_phase_boundary_breach(
        self,
        tmp_path: Path,
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
            "comp-a",
            success=False,
            iterations=1,
            error=reason,
            usage=_engineer_usage(10),
            budget_exceeded=True,
        )

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=halted,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
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
            f
            for f in comp_a.findings
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
        self,
        tmp_path: Path,
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
                "comp-a",
                success=False,
                iterations=1,
                error="token budget unenforceable (R8)",
                usage=_engineer_usage(10),
                budget_exceeded=True,
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )

        assert calls == ["comp-a"]
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.retries == 0


def _pending_executor(
    sync_calls: int,
    on_pending: Callable[[tuple[Any, ...]], None],
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
            self,
            fn: Callable[..., ComponentResult],
            /,
            *args: Any,
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
            self,
            wait: bool = True,
            cancel_futures: bool = False,
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
        _clear_partial_usage(path)  # absent: no error
        _write_partial_usage(path, _engineer_usage(700))
        _clear_partial_usage(path)
        assert _read_partial_usage(path) is None
        _clear_partial_usage(tmp_path)  # a directory: swallowed

    def test_killed_worker_spend_recovered_from_the_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        usage_paths = RunPaths(root=tmp_path)
        _write_partial_usage(
            usage_paths.engineer_usage("comp-a"),
            _engineer_usage(700),
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
        self,
        tmp_path: Path,
    ) -> None:
        """No double counting: a future that DID deliver is
        authoritative and the (staler) snapshot is ignored."""
        usage_paths = RunPaths(root=tmp_path)
        _write_partial_usage(
            usage_paths.engineer_usage("comp-a"),
            _engineer_usage(700),
        )
        pipeline = MagicMock()
        pipeline.usage_paths = usage_paths
        future: Future[ComponentResult] = Future()
        future.set_result(
            ComponentResult(
                "comp-a",
                success=False,
                usage=_engineer_usage(900),
            )
        )

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
        self,
        tmp_path: Path,
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
                comp_id,
                success=True,
                iterations=1,
                usage=_engineer_usage(4242),
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=slow_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                _factory_config(root),
                _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()),
                root,
                stop=stop,
            )

        assert result.exit_code == 130
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.failed_phase == "aborted"
        assert _engineer_usage_events(root) == [4242]

    def test_pending_worker_spend_is_salvaged_from_disk_end_to_end(
        self,
        tmp_path: Path,
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
                RunPaths(root=_usage_dir_from_args(args)).engineer_usage("comp-a"),
                _engineer_usage(700),
            )
            stop.request("mid-run test stop")

        with (
            patch(
                "kstrl.factory._InlineExecutor",
                _pending_executor(0, worker_published_then_died),
            ),
            patch(
                "kstrl.factory._run_component",
                side_effect=AssertionError("the worker never returned"),
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                _factory_config(root),
                _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()),
                root,
                stop=stop,
            )

        assert result.exit_code == 130
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.failed_phase == "aborted"
        assert _engineer_usage_events(root) == [700]

    def test_a_stale_snapshot_from_a_finished_attempt_is_not_recounted(
        self,
        tmp_path: Path,
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
                RunPaths(root=_usage_dir_from_args(args)).engineer_usage("comp-a"),
                _engineer_usage(700),
            )
            return ComponentResult(
                "comp-a",
                success=False,
                iterations=1,
                error="tests failed",
                usage=_engineer_usage(700),
            )

        with (
            patch(
                "kstrl.factory._InlineExecutor",
                # Attempt 1 runs; attempt 2's future hangs and is aborted.
                _pending_executor(1, lambda args: stop.request("mid-run stop")),
            ),
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()),
                root,
                stop=stop,
            )

        assert _engineer_usage_events(root) == [700]

    def test_pending_worker_spend_survives_progress_log_disabled(
        self,
        tmp_path: Path,
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

        with (
            patch(
                "kstrl.factory._InlineExecutor",
                _pending_executor(0, worker_published_then_died),
            ),
            patch(
                "kstrl.factory._run_component",
                side_effect=AssertionError("the worker never returned"),
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True, file=out),
                root,
                stop=stop,
            )

        rollup = [
            line for line in out.getvalue().splitlines() if "comp-a" in line and "engineer" in line
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
        engineer.add_record(
            UsageRecord(
                input_tokens=100,
                output_tokens=200,
                total_tokens=300,
                cost_usd=0.5,
                duration_seconds=10.0,
                source="claude-stream-json",
            )
        )
        review = UsageTotals()
        review.add_record(UsageRecord(total_tokens=50, source="codex-text"))
        run_usage = UsageTotals()
        run_usage.merge(engineer)
        run_usage.merge(review)

        lines = format_usage_rollup(
            {"comp-a": {"review": review, "engineer": engineer}},
            run_usage,
        )
        # Header, engineer row before review row (fixed phase order),
        # TOTAL. This fixture is the measured mixed shape - a claude
        # engineer reporting cost and a codex reviewer reporting tokens
        # only - so R8 adds one cost-coverage note after the TOTAL row.
        rows = [line for line in lines if not line.startswith("note:")]
        assert len(rows) == 4
        assert "tokens_total" in rows[0]
        assert "engineer" in rows[1]
        assert "review" in rows[2]
        assert rows[3].startswith("TOTAL")
        assert "350" in rows[3]
        assert any("cost coverage is PARTIAL" in line for line in lines)

    def test_unknown_usage_rendered_as_dash_with_note(self) -> None:
        unknown = UsageTotals()
        unknown.add_record(UsageRecord(duration_seconds=3.0))
        lines = format_usage_rollup({"comp-a": {"engineer": unknown}}, unknown)
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_total_tokens = 111\n")
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_cost_usd = 1.5\n")
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
            max_total_tokens=500_000,
            prior_total_tokens=250_000,
            prior_known_calls=5,
            prior_calls=4,
            prior_token_calls=4,
        )
        assert (
            budget.halt_reason(
                UsageTotals(calls=1, known_calls=0, token_calls=0, total_tokens=0),
            )
            is None
        )

    def test_silent_engineer_across_attempts_still_halts(self) -> None:
        # P1-a must stay fixed: one tokenless engineer call already
        # recorded, this loop's first is the second.
        budget = LoopBudget(
            max_total_tokens=500_000,
            prior_total_tokens=0,
            prior_known_calls=0,
            prior_calls=1,
            prior_token_calls=0,
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
            max_total_tokens=500_000,
            prior_total_tokens=250_000,
            prior_known_calls=4,
            prior_calls=2,
            prior_token_calls=0,
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
                    calls=3,
                    known_calls=3,
                    token_calls=3,
                    total_tokens=900,
                ),
                "engineer": UsageTotals(
                    calls=2,
                    known_calls=0,
                    token_calls=0,
                    total_tokens=0,
                ),
            },
            "comp-b": {
                "engineer": UsageTotals(
                    calls=1,
                    known_calls=1,
                    token_calls=1,
                    total_tokens=50,
                ),
            },
        }
        engineer = ComponentPipeline.engineer_usage_totals(pipeline)
        assert engineer.calls == 3  # 2 + 1, review excluded
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
        tmp_path: Path,
        calls: int,
        token_calls: int,
        cap: int,
    ) -> Any:
        from kstrl.pipeline import ComponentPipeline

        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))
        pipeline.usage_meter = {
            "comp-a": {
                "engineer": UsageTotals(
                    calls=calls,
                    known_calls=0,
                    token_calls=token_calls,
                    total_tokens=0,
                ),
            },
        }
        pipeline.factory_config = _factory_config(
            tmp_path,
            max_total_tokens=cap,
        )
        return pipeline

    def test_completed_tokenless_components_trip_the_parent_gate(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline_with_engineer_usage(
            tmp_path,
            calls=2,
            token_calls=0,
            cap=500_000,
        )
        reason = ComponentPipeline.token_budget_unenforceable(pipeline)
        assert reason is not None
        assert "cannot advance" in reason
        assert "refusing to schedule further components" in reason

    def test_one_completed_tokenless_component_is_not_enough(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline_with_engineer_usage(
            tmp_path,
            calls=1,
            token_calls=0,
            cap=500_000,
        )
        assert ComponentPipeline.token_budget_unenforceable(pipeline) is None

    def test_a_reporting_engineer_never_trips_the_gate(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline_with_engineer_usage(
            tmp_path,
            calls=9,
            token_calls=9,
            cap=500_000,
        )
        assert ComponentPipeline.token_budget_unenforceable(pipeline) is None

    def test_gate_is_inert_without_a_cap(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline_with_engineer_usage(
            tmp_path,
            calls=5,
            token_calls=0,
            cap=0,
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
        self,
        tmp_path: Path,
    ) -> None:
        """$0.03/iteration against a $0.05 ceiling means iteration 3
        never starts, even though max_iterations is 10."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_cost_usd=0.05),
        )
        assert result.completed is False
        assert result.iterations == 2
        assert len(agent.usage_records) == 2  # the agent really stopped
        assert "cost budget exceeded" in result.budget_halt_reason
        assert "max_cost_usd" in result.budget_halt_reason
        assert result.usage.cost_usd == pytest.approx(0.06)

    def test_prior_cost_from_earlier_components_counts(
        self,
        tmp_path: Path,
    ) -> None:
        """The ceiling is run-level: a worker launched with $0.045
        already on the run's meter has half a cent left, not $0.05."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(
                max_cost_usd=0.05,
                prior_cost_usd=0.045,
                prior_known_calls=1,
                prior_calls=1,
                prior_cost_calls=1,
            ),
        )
        assert result.iterations == 1
        assert "cost budget exceeded" in result.budget_halt_reason

    def test_zero_cost_cap_is_inert(self, tmp_path: Path) -> None:
        """0.0 = unbounded, matching the max_total_tokens convention."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 3),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_cost_usd=0.0),
        )
        assert result.iterations == 3
        assert result.budget_halt_reason == ""

    def test_token_ceiling_wins_when_it_is_the_tighter_one(
        self,
        tmp_path: Path,
    ) -> None:
        """Both set: whichever is reached first halts, and the message
        names THAT one. 300 tok + $0.03 per iteration against a 500-token
        / $100 pair trips the token ceiling at iteration 2."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=500, max_cost_usd=100.0),
        )
        assert result.iterations == 2
        assert "token budget exceeded" in result.budget_halt_reason
        assert "cost budget exceeded" not in result.budget_halt_reason

    def test_cost_ceiling_wins_when_it_is_the_tighter_one(
        self,
        tmp_path: Path,
    ) -> None:
        """The mirror, and the case the measured run is about: a token
        ceiling set high enough to be useless while the money runs out."""
        agent = FakeUsageAgent(outputs=[["working..."]], record=_BOTH)
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=1_000_000, max_cost_usd=0.05),
        )
        assert result.iterations == 2
        assert "cost budget exceeded" in result.budget_halt_reason
        assert "token budget exceeded" not in result.budget_halt_reason

    def test_cost_only_adapter_enforces_cost_while_the_token_cap_is_dead(
        self,
        tmp_path: Path,
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
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=500, max_cost_usd=0.05),
        )
        assert result.iterations == 3  # 3 x 0.0227028 >= 0.05
        assert "cost budget exceeded" in result.budget_halt_reason
        assert "unenforceable" not in result.budget_halt_reason
        assert result.usage.token_calls == 0  # token cap was dead
        assert result.usage.cost_calls == 3  # cost cap was not

    def test_token_only_adapter_enforces_tokens_while_the_cost_cap_is_dead(
        self,
        tmp_path: Path,
    ) -> None:
        """The converse, and just as real: codex reports a token total
        and no cost at all."""
        agent = SequenceUsageAgent([_REPORTED])  # 300 tokens, no cost
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=1000, max_cost_usd=100.0),
        )
        assert result.iterations == 4  # 4 x 300 >= 1000
        assert "token budget exceeded" in result.budget_halt_reason
        assert "unenforceable" not in result.budget_halt_reason
        assert result.usage.cost_calls == 0  # cost cap was dead throughout

    def test_unenforceable_only_when_every_configured_ceiling_is_dead(
        self,
        tmp_path: Path,
    ) -> None:
        """A wholly silent adapter kills both ceilings, and only then
        does the loop halt as unenforceable - naming both."""
        agent = SequenceUsageAgent([_UNREPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_total_tokens=500, max_cost_usd=0.05),
        )
        assert result.iterations == 2  # UNENFORCEABLE_CALLS
        assert "token budget unenforceable" in result.budget_halt_reason
        assert "cost budget unenforceable" in result.budget_halt_reason
        assert "max_total_tokens (500)" in result.budget_halt_reason
        assert "max_cost_usd ($0.05)" in result.budget_halt_reason

    def test_a_cost_ceiling_alone_is_dead_on_a_token_only_adapter(
        self,
        tmp_path: Path,
    ) -> None:
        """With ONLY max_cost_usd configured, a codex-style adapter that
        never reports a cost leaves it unable to fire. Before the cost
        ceiling existed this configuration had no protection at all."""
        agent = SequenceUsageAgent([_REPORTED])
        result = run_loop(
            _loop_config(tmp_path, 10),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_cost_usd=100.0),
        )
        assert result.iterations == 2
        assert "cost budget unenforceable" in result.budget_halt_reason
        assert "token budget" not in result.budget_halt_reason
        assert "2 costless call(s) this run" in result.budget_halt_reason

    def test_one_costless_call_is_an_incident_not_a_dead_ceiling(
        self,
        tmp_path: Path,
    ) -> None:
        """Symmetric with the token rule: a lone unparseable result must
        not kill a capped run."""
        agent = SequenceUsageAgent([_UNREPORTED, _BOTH, _BOTH])
        result = run_loop(
            _loop_config(tmp_path, 3),
            PlainUI(no_color=True),
            agent,
            tmp_path,
            budget=LoopBudget(max_cost_usd=100.0),
        )
        assert result.budget_halt_reason == ""
        assert result.iterations == 3

    def test_costless_threshold_does_not_reset_per_loop(
        self,
        tmp_path: Path,
    ) -> None:
        """The cost mirror of P1-a: the threshold is run-wide, threaded
        through the same priors ``_submit_args`` threads."""
        run_total = UsageTotals()
        reasons: list[str] = []
        for _ in range(3):
            agent = SequenceUsageAgent([_REPORTED])  # tokens, never a cost
            result = run_loop(
                _loop_config(tmp_path, 1),
                PlainUI(no_color=True),
                agent,
                tmp_path,
                budget=_budget_for(run_total, 0, cost_cap=100.0),
            )
            run_total.merge(result.usage)
            reasons.append(result.budget_halt_reason)

        assert reasons[0] == ""  # one is an incident
        assert "cost budget unenforceable" in reasons[1]
        assert "cost budget unenforceable" in reasons[2]
        assert run_total.calls == 3
        assert run_total.cost_calls == 0

    def test_a_loop_that_has_not_run_cannot_halt_on_cost(self) -> None:
        """No calls means no evidence either way; a worker launched into
        a run with costless priors must still get to run its engineer."""
        budget = LoopBudget(
            max_cost_usd=100.0,
            prior_calls=20,
            prior_cost_calls=0,
        )
        assert budget.halt_reason(UsageTotals()) is None

    def test_no_ceiling_configured_is_always_none(self) -> None:
        budget = LoopBudget(prior_calls=20, prior_cost_calls=0)
        assert (
            budget.halt_reason(
                UsageTotals(calls=5, known_calls=0),
            )
            is None
        )


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
            calls=calls,
            known_calls=max(token_calls, cost_calls),
            token_calls=token_calls,
            cost_calls=cost_calls,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
        )
        pipeline.usage_meter = {"comp-a": {"engineer": engineer}}
        pipeline.run_usage = UsageTotals()
        pipeline.run_usage.merge(engineer)
        pipeline.factory_config = _factory_config(
            tmp_path,
            max_total_tokens=max_total_tokens,
            max_cost_usd=max_cost_usd,
        )
        return pipeline

    def test_cost_overrun_is_detected_and_named(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path,
            calls=1,
            cost_calls=1,
            cost_usd=0.25,
            max_cost_usd=0.10,
        )
        assert ComponentPipeline.cost_budget_exceeded(p) is True
        assert ComponentPipeline.token_budget_exceeded(p) is False
        assert ComponentPipeline.breached_ceiling(p) == "max_cost_usd"
        assert ComponentPipeline.budget_exceeded(p) is True

    def test_token_overrun_still_named_max_total_tokens(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path,
            calls=1,
            token_calls=1,
            total_tokens=900,
            max_total_tokens=500,
        )
        assert ComponentPipeline.breached_ceiling(p) == "max_total_tokens"

    def test_zero_cost_ceiling_never_trips(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path,
            calls=1,
            cost_calls=1,
            cost_usd=99.0,
            max_cost_usd=0.0,
        )
        assert ComponentPipeline.cost_budget_exceeded(p) is False
        assert ComponentPipeline.breached_ceiling(p) is None

    def test_a_live_cost_ceiling_keeps_a_dead_token_one_from_halting(
        self,
        tmp_path: Path,
    ) -> None:
        """The scheduling gate must not stop a run whose cost ceiling can
        still fire, even though its token ceiling provably cannot."""
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path,
            calls=2,
            token_calls=0,
            cost_calls=2,
            cost_usd=0.01,
            max_total_tokens=500_000,
            max_cost_usd=100.0,
        )
        assert ComponentPipeline.token_budget_unenforceable(p) is not None
        assert ComponentPipeline.cost_budget_unenforceable(p) is None
        assert ComponentPipeline.budget_unenforceable(p) is None

    def test_a_live_token_ceiling_keeps_a_dead_cost_one_from_halting(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path,
            calls=2,
            token_calls=2,
            cost_calls=0,
            total_tokens=100,
            max_total_tokens=500_000,
            max_cost_usd=100.0,
        )
        assert ComponentPipeline.cost_budget_unenforceable(p) is not None
        assert ComponentPipeline.token_budget_unenforceable(p) is None
        assert ComponentPipeline.budget_unenforceable(p) is None

    def test_both_dead_halts_and_names_both(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path,
            calls=2,
            token_calls=0,
            cost_calls=0,
            max_total_tokens=500_000,
            max_cost_usd=100.0,
        )
        reason = ComponentPipeline.budget_unenforceable(p)
        assert reason is not None
        assert "max_total_tokens" in reason
        assert "max_cost_usd" in reason
        assert "refusing to schedule further components" in reason

    def test_a_lone_cost_ceiling_can_be_the_only_dead_one(
        self,
        tmp_path: Path,
    ) -> None:
        """Only max_cost_usd configured: it is the only ceiling that has
        to be alive, so its death halts the gate on its own."""
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(
            tmp_path,
            calls=2,
            token_calls=2,
            cost_calls=0,
            total_tokens=100,
            max_cost_usd=100.0,
        )
        reason = ComponentPipeline.budget_unenforceable(p)
        assert reason is not None
        assert "cost budget unenforceable" in reason

    def test_gate_is_inert_with_no_ceiling_configured(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        p = self._pipeline(tmp_path, calls=9, token_calls=0, cost_calls=0)
        assert ComponentPipeline.budget_unenforceable(p) is None


class TestCostCeilingEndToEnd:
    def test_cost_halt_names_the_ceiling_everywhere_it_is_recorded(
        self,
        tmp_path: Path,
    ) -> None:
        """One run, every audit surface: the component error, the typed
        finding, and the budget_exceeded event all name max_cost_usd.
        The token ceiling is off, so nothing may claim it tripped."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest([_component("comp-a"), _component("comp-b")])
        config = _factory_config(root, max_cost_usd=0.10)

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            return ComponentResult(
                str(args[0]),
                success=True,
                iterations=1,
                usage=_engineer_usage(600, cost=0.25),
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )

        assert "comp-a" in result.failed
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.status == ComponentStatus.FAILED.value
        assert "max_cost_usd" in (comp_a.error or "")
        assert "max_total_tokens" not in (comp_a.error or "")
        budget_findings = [
            f
            for f in comp_a.findings
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
            "comp-a",
            success=True,
            iterations=1,
            usage=_engineer_usage(600),
        )

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )

        events = ProgressLog(root / "progress.jsonl").read_events()
        breach = next(e for e in events if e["event"] == "budget_exceeded")
        assert breach["data"]["ceiling"] == "max_total_tokens"
        assert breach["data"]["max_total_tokens"] == 500
        assert breach["data"]["total_tokens"] >= 500

    def test_scheduler_hands_the_cost_ceiling_and_priors_down(
        self,
        tmp_path: Path,
    ) -> None:
        """``_submit_args`` must snapshot the cost priors per launch, or
        the in-loop cost check degrades to a per-component budget."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest(
            [
                _component("comp-a"),
                _component("comp-b", deps=["comp-a"]),
            ]
        )
        config = _factory_config(root, max_cost_usd=100.0)
        budgets: list[Any] = []

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            budgets.append(args[-1])
            return ComponentResult(
                str(args[0]),
                success=True,
                iterations=1,
                usage=_engineer_usage(700, cost=0.5),
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
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
            Path,
            "unlink",
            side_effect=PermissionError("read-only"),
        ):
            assert _clear_partial_usage(target) is False
        assert target.exists()  # still there, hence unsafe

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

        pending: Future[Any] = Future()  # never completes
        ComponentPipeline.mark_usage_salvage_unsafe(pipeline, "comp-a")
        _salvage_aborted_usage(pending, "comp-a", pipeline)
        assert recorded == [], "a stale snapshot must not be salvaged"

        # And the safe path still salvages, so the guard is not a blanket off.
        ComponentPipeline.mark_usage_salvage_safe(pipeline, "comp-a")
        _salvage_aborted_usage(pending, "comp-a", pipeline)
        assert [t.total_tokens for t in recorded] == [700]


class TestCostCeilingConfigValidation:
    """A ceiling that cannot bound anything must be refused, not stored.

    Review regression on #180: every input path used a bare `float()`, so
    `nan`, `inf` and negatives were accepted. Each then failed in a
    DIFFERENT silent direction - `nan > 0` is False so the ceiling
    disabled itself while reading as configured; a negative did the same;
    `inf` produced a ceiling that is enabled and unreachable. All three
    are indistinguishable from "off" at the moment they matter, which is
    the one property a safety limit must not have.
    """

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-3.0"])
    def test_toml_rejects_unbounding_values(
        self,
        tmp_path: Path,
        bad: str,
    ) -> None:
        from kstrl.factory import BudgetConfigError, FactoryConfig

        (tmp_path / "kstrl.toml").write_text(f"[factory]\nmax_cost_usd = {bad}\n")
        with pytest.raises(BudgetConfigError):
            FactoryConfig.load(tmp_path)

    @pytest.mark.parametrize("bad", ["nan", "inf", "-3.0"])
    def test_env_rejects_unbounding_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bad: str,
    ) -> None:
        from kstrl.factory import BudgetConfigError, FactoryConfig

        monkeypatch.setenv("KSTRL_FACTORY_MAX_COST_USD", bad)
        with pytest.raises(BudgetConfigError):
            FactoryConfig.from_env()

    def test_zero_is_unbounded_not_invalid(self, tmp_path: Path) -> None:
        from kstrl.factory import FactoryConfig

        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_cost_usd = 0\n")
        assert FactoryConfig.load(tmp_path).max_cost_usd == 0.0

    def test_ordinary_value_survives(self, tmp_path: Path) -> None:
        from kstrl.factory import FactoryConfig

        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_cost_usd = 5.0\n")
        assert FactoryConfig.load(tmp_path).max_cost_usd == 5.0

    def test_run_factory_rechecks_a_programmatic_config(
        self,
        tmp_path: Path,
    ) -> None:
        """A limit that only holds via the front door is not a limit.

        FactoryConfig can be built directly (tests, embedders, the SDK
        path), bypassing every config-path validator.
        """
        from kstrl.config import KstrlConfig
        from kstrl.factory import BudgetConfigError, FactoryConfig, run_factory
        from kstrl.manifest import Component, Manifest
        from kstrl.ui.plain import PlainUI

        config = FactoryConfig(max_cost_usd=float("nan"))
        manifest = Manifest(
            version="1",
            spec_file="s",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    "comp-a",
                    "A",
                    "D",
                    [],
                    "prd.json",
                    "kstrl/comp-a",
                )
            ],
        )
        base = KstrlConfig(ui_mode="plain", no_color=True)
        with pytest.raises(BudgetConfigError):
            run_factory(manifest, config, base, PlainUI(no_color=True), tmp_path)


class TestUnenforceableHaltNamesItsCeiling:
    """An unenforceable halt crosses no threshold, but the ceiling that
    FAILED still has a name.

    Review regression on #180: `breached_ceiling()` correctly returned
    None for these halts, so `ceiling=""` flowed downstream and the audit
    surfaces rendered it as the token ceiling - reported as
    "run token budget exceeded (200/0)" for a cost-only run whose token
    ceiling was not even configured.
    """

    @staticmethod
    def _pipeline(max_tokens: int, max_cost: float, usage: UsageTotals) -> Any:
        from kstrl.pipeline import ComponentPipeline

        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))

        class _FC:
            max_total_tokens = max_tokens
            max_cost_usd = max_cost

        pipeline.factory_config = _FC()
        pipeline.usage_meter = {"comp-a": {"engineer": usage}}
        pipeline.run_usage = usage
        return pipeline

    def test_cost_only_run_names_the_cost_ceiling(self) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            0,
            5.0,
            UsageTotals(
                calls=2, known_calls=2, token_calls=2, cost_calls=0, total_tokens=200, cost_usd=0.0
            ),
        )
        assert ComponentPipeline.breached_ceiling(pipeline) is None
        assert ComponentPipeline.unenforceable_ceilings(pipeline) == [
            "max_cost_usd",
        ]

    def test_token_only_run_names_the_token_ceiling(self) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            1000,
            0.0,
            UsageTotals(
                calls=2, known_calls=2, token_calls=0, cost_calls=2, total_tokens=0, cost_usd=0.5
            ),
        )
        assert ComponentPipeline.unenforceable_ceilings(pipeline) == [
            "max_total_tokens",
        ]

    def test_both_dead_names_both(self) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            1000,
            5.0,
            UsageTotals(calls=2, known_calls=0, token_calls=0, cost_calls=0),
        )
        assert ComponentPipeline.unenforceable_ceilings(pipeline) == [
            "max_total_tokens",
            "max_cost_usd",
        ]

    def test_a_live_ceiling_is_never_named(self) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            1000,
            5.0,
            UsageTotals(
                calls=2, known_calls=2, token_calls=2, cost_calls=2, total_tokens=10, cost_usd=0.1
            ),
        )
        assert ComponentPipeline.unenforceable_ceilings(pipeline) == []

    def test_an_unconfigured_ceiling_is_never_named(self) -> None:
        """The exact defect: a disabled token ceiling must not be blamed."""
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            0,
            5.0,
            UsageTotals(calls=2, known_calls=0, token_calls=0, cost_calls=0),
        )
        named = ComponentPipeline.unenforceable_ceilings(pipeline)
        assert "max_total_tokens" not in named
        assert named == ["max_cost_usd"]


class TestBudgetHaltIdentityPrecedence:
    """A numeric breach outranks a dead ceiling.

    Review follow-up on #180: the two facts can coexist - a run whose
    token cap trips while its cost cap never received a figure - and the
    call sites each joined the dead list FIRST. The halt was then
    attributed to `max_cost_usd` and rendered as
    `cost budget exceeded: $0 >= $100`: a sentence that is false, and
    arithmetically impossible, for a run that blew its token cap at
    600/500.
    """

    @staticmethod
    def _pipeline(max_tokens: int, max_cost: float, usage: UsageTotals) -> Any:
        from kstrl.pipeline import ComponentPipeline

        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))

        class _FC:
            max_total_tokens = max_tokens
            max_cost_usd = max_cost

        pipeline.factory_config = _FC()
        pipeline.usage_meter = {"comp-a": {"engineer": usage}}
        pipeline.run_usage = usage
        return pipeline

    def test_breach_wins_over_a_coexisting_dead_ceiling(self) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            500,
            100.0,
            UsageTotals(
                calls=2, known_calls=2, token_calls=2, cost_calls=0, total_tokens=600, cost_usd=0.0
            ),
        )
        assert ComponentPipeline.breached_ceiling(pipeline) == "max_total_tokens"
        assert ComponentPipeline.unenforceable_ceilings(pipeline) == [
            "max_cost_usd",
        ]
        assert ComponentPipeline.budget_halt_identity(pipeline) == (
            "breached",
            ("max_total_tokens",),
        )

    def test_dead_ceilings_only_when_nothing_breached(self) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            500,
            100.0,
            UsageTotals(calls=2, known_calls=0, token_calls=0, cost_calls=0),
        )
        assert ComponentPipeline.budget_halt_identity(pipeline) == (
            "unenforceable",
            ("max_total_tokens", "max_cost_usd"),
        )

    def test_no_halt_yields_no_identity(self) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            1000,
            5.0,
            UsageTotals(
                calls=2, known_calls=2, token_calls=2, cost_calls=2, total_tokens=10, cost_usd=0.1
            ),
        )
        assert ComponentPipeline.budget_halt_identity(pipeline) == ("", ())


class TestLoopHaltCarriesItsOwnIdentity:
    """The loop halts against priors the parent cannot see, so it reports
    WHICH ceiling structurally instead of leaving the parent to guess."""

    def test_token_overrun(self) -> None:
        from kstrl.loop import LoopBudget

        verdict = LoopBudget(
            max_total_tokens=500,
            max_cost_usd=100.0,
        ).halt_verdict(
            UsageTotals(
                calls=2,
                known_calls=2,
                token_calls=2,
                total_tokens=600,
            )
        )
        assert verdict is not None
        assert verdict.condition == "breached"
        assert verdict.ceilings == ("max_total_tokens",)

    def test_cost_overrun(self) -> None:
        from kstrl.loop import LoopBudget

        verdict = LoopBudget(max_cost_usd=1.0).halt_verdict(
            UsageTotals(
                calls=2,
                known_calls=2,
                cost_calls=2,
                cost_usd=2.0,
            )
        )
        assert verdict is not None
        assert verdict.condition == "breached"
        assert verdict.ceilings == ("max_cost_usd",)

    def test_unenforceable_names_every_dead_ceiling(self) -> None:
        from kstrl.loop import LoopBudget

        verdict = LoopBudget(
            max_total_tokens=500,
            max_cost_usd=100.0,
            prior_calls=2,
        ).halt_verdict(UsageTotals(calls=2, known_calls=0))
        assert verdict is not None
        assert verdict.condition == "unenforceable"
        assert verdict.ceilings == ("max_total_tokens", "max_cost_usd")

    def test_halt_reason_still_returns_the_prose(self) -> None:
        """Existing callers read the sentence; it must not change shape."""
        from kstrl.loop import LoopBudget

        budget = LoopBudget(max_total_tokens=500)
        usage = UsageTotals(
            calls=2,
            known_calls=2,
            token_calls=2,
            total_tokens=600,
        )
        reason = budget.halt_reason(usage)
        verdict = budget.halt_verdict(usage)
        assert verdict is not None
        assert reason == verdict.reason
        assert reason is not None and reason.startswith("token budget exceeded")

    def test_no_halt_returns_none_from_both(self) -> None:
        from kstrl.loop import LoopBudget

        budget = LoopBudget(max_total_tokens=500)
        usage = UsageTotals(
            calls=1,
            known_calls=1,
            token_calls=1,
            total_tokens=10,
        )
        assert budget.halt_verdict(usage) is None
        assert budget.halt_reason(usage) is None


class TestBudgetHaltRendersHonestly:
    """Every surface reads one payload the same way, and an unenforceable
    halt never claims a threshold was crossed."""

    @staticmethod
    def _render(event: Any) -> tuple[str, str]:
        from kstrl.linear import LinearSink
        from kstrl.reducer import ComponentState, RunState, apply

        state = RunState(run_id="r")
        state.components["comp-a"] = ComponentState(component_id="comp-a")
        apply(state, event)
        sink = cast(Any, LinearSink.__new__(LinearSink))
        sink._run_id = "r"
        body = LinearSink._comment_body(
            sink,
            "budget_exceeded",
            event.to_dict()["data"],
        )
        return state.components["comp-a"].error, str(body)

    def test_breach_states_the_true_comparison(self) -> None:
        import kstrl.events as ev

        reducer, linear = self._render(
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=600,
                max_total_tokens=500,
                cost_usd=0.0,
                max_cost_usd=100.0,
                ceiling="max_total_tokens",
                condition="breached",
                ceilings=("max_total_tokens",),
            )
        )
        assert reducer == "token budget exceeded: 600 >= 500"
        assert "600/500" in linear
        assert "cost" not in reducer

    def test_unenforceable_claims_no_threshold(self) -> None:
        import kstrl.events as ev

        reducer, linear = self._render(
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=0,
                max_total_tokens=500,
                cost_usd=0.0,
                max_cost_usd=100.0,
                ceiling="max_total_tokens, max_cost_usd",
                condition="unenforceable",
                ceilings=("max_total_tokens", "max_cost_usd"),
            )
        )
        for surface in (reducer, linear):
            # The defect: both rendered "token budget exceeded: 0 >= 500"
            # for a halt where no total ever moved.
            assert "exceeded" not in surface
            assert "0 >= 500" not in surface
            assert "unenforceable" in surface
            # A multi-ceiling halt names BOTH; no single-value field
            # could express this, which is why it silently collapsed.
            assert "max_total_tokens" in surface
            assert "max_cost_usd" in surface

    def test_a_disabled_ceiling_is_never_blamed(self) -> None:
        import kstrl.events as ev

        reducer, linear = self._render(
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=200,
                max_total_tokens=0,
                cost_usd=0.0,
                max_cost_usd=5.0,
                ceiling="max_cost_usd",
                condition="unenforceable",
                ceilings=("max_cost_usd",),
            )
        )
        for surface in (reducer, linear):
            assert "max_total_tokens" not in surface
            assert "max_cost_usd" in surface

    def test_legacy_payloads_still_decode(self) -> None:
        """events.jsonl is append-only: payloads written before this
        change carry only `ceiling` and must keep their old reading."""
        import kstrl.events as ev

        reducer, linear = self._render(
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=5,
                max_total_tokens=10,
                cost_usd=9.0,
                max_cost_usd=8.0,
                ceiling="max_cost_usd",
            )
        )
        assert reducer == "cost budget exceeded: $9.000000 >= $8.0"
        assert "cost budget exceeded" in linear

    def test_the_structured_fields_survive_a_json_round_trip(self) -> None:
        import kstrl.events as ev

        event = ev.BudgetExceeded(
            component="comp-a",
            condition="unenforceable",
            ceilings=("max_total_tokens", "max_cost_usd"),
        )
        back = ev.event_from_dict(event.to_dict())
        assert isinstance(back, ev.BudgetExceeded)
        assert back.ceilings == ("max_total_tokens", "max_cost_usd")
        assert back.condition == "unenforceable"

    @pytest.mark.parametrize(
        ("condition", "ceilings", "legacy", "expected"),
        [
            ("breached", ("max_total_tokens",), "", "token"),
            ("breached", ("max_cost_usd",), "", "cost"),
            ("unenforceable", ("max_cost_usd",), "", "unenforceable"),
            ("unenforceable", ("max_total_tokens", "max_cost_usd"), "", "unenforceable"),
            ("", (), "max_cost_usd", "cost"),
            ("", (), "", "token"),
        ],
    )
    def test_one_classifier_for_every_surface(
        self,
        condition: str,
        ceilings: tuple[str, ...],
        legacy: str,
        expected: str,
    ) -> None:
        from kstrl.events import budget_halt_kind

        assert budget_halt_kind(condition, ceilings, legacy) == expected


class TestBudgetConfigErrorReachesTheOperator:
    """A rejected ceiling is reported, never raised.

    Review follow-up on #180: validation landed at the library boundary,
    but `ks factory` exited 1 with empty output and an uncaught
    BudgetConfigError - a traceback where a config error belongs.
    """

    @staticmethod
    def _manifest() -> dict[str, Any]:
        return {
            "version": "1",
            "specFile": "s.md",
            "projectName": "p",
            "baseBranch": "main",
            "singlePr": False,
            "components": [
                {
                    "id": "comp-a",
                    "title": "A",
                    "description": "D",
                    "dependencies": [],
                    "prdPath": "prd.json",
                    "branchName": "kstrl/comp-a",
                }
            ],
        }

    @pytest.mark.parametrize(
        ("command", "toml_value"),
        [
            # --agent-cmd keeps this independent of what is on PATH.
            # Without it the run aborts on agent detection BEFORE the
            # ceiling is read, so the test passed on a developer machine
            # with `claude` installed and failed in CI without it -
            # measuring the environment, not the fix.
            (["factory", "--manifest", "m.json", "--agent-cmd", "true"], "nan"),
            (["factory", "--manifest", "m.json", "--agent-cmd", "true"], "-3.0"),
            (["config", "show"], "nan"),
        ],
    )
    def test_no_entry_point_leaks_a_traceback(
        self,
        command: list[str],
        toml_value: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Proven against the harsher of the two environments: no agent
        # discoverable at all.
        monkeypatch.setenv("PATH", "")
        from click.testing import CliRunner

        from kstrl.cli import cli
        from kstrl.factory import BudgetConfigError

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            root = Path(fs)
            (root / "kstrl.toml").write_text(f"[factory]\nmax_cost_usd = {toml_value}\n")
            (root / "m.json").write_text(json.dumps(self._manifest()))
            (root / "s.md").write_text("# spec\n")
            result = runner.invoke(cli, command, catch_exceptions=True)

        assert not isinstance(result.exception, BudgetConfigError)
        assert result.exit_code == 1
        assert "error:" in _strip_ansi(result.output)
        assert "max_cost_usd" in result.output

    def test_the_flag_override_is_checked_before_any_work_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--max-cost-usd` bypasses every config-path validator, so the
        check has to hold at the run boundary too - and has to stop the
        run before it launches anything."""
        monkeypatch.setenv("PATH", "")
        from click.testing import CliRunner

        from kstrl.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            root = Path(fs)
            (root / "kstrl.toml").write_text("[factory]\nmax_cost_usd = 0\n")
            (root / "m.json").write_text(json.dumps(self._manifest()))
            result = runner.invoke(
                cli,
                ["factory", "--manifest", "m.json", "--max-cost-usd", "inf", "--agent-cmd", "true"],
                catch_exceptions=True,
            )

        output = _strip_ansi(result.output)
        assert result.exit_code == 1
        # The preflight names the FLAG, not the generic knob: the
        # operator has to know which of the three sources to fix.
        assert "error: --max-cost-usd must be a finite number" in output
        assert "Starting:" not in output


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestCeilingsAreCheckedBeforeAnythingSpends:
    """An invalid ceiling must cost nothing to discover.

    Review follow-up on #180: `ks factory --spec` built its config only
    AFTER decomposition, so a rejected ceiling surfaced once the
    architect had already spent a call - the operator paid for a call
    under the very ceiling that was supposed to bound it. Measured, not
    assumed: the fake agent below records its own invocation, and these
    tests fail if it ever runs.
    """

    @staticmethod
    def _fake_agent(root: Path) -> tuple[Path, Path]:
        """An agent that records being called. Its output is irrelevant;
        the marker file is the whole assertion."""
        marker = root / "AGENT_WAS_INVOKED"
        script = root / "fake-agent.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\necho '{{}}'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script, marker

    @pytest.mark.parametrize(
        ("toml_value", "extra_args", "expected"),
        [
            ("nan", [], "[factory] max_cost_usd must be a finite number"),
            ("-3.0", [], "[factory] max_cost_usd must be >= 0"),
            ("0", ["--max-cost-usd", "inf"], "--max-cost-usd must be a finite number"),
            ("0", ["--max-total-tokens", "-5"], "--max-total-tokens must be >= 0"),
        ],
    )
    def test_the_spec_path_rejects_before_decomposition(
        self,
        toml_value: str,
        extra_args: list[str],
        expected: str,
    ) -> None:
        from click.testing import CliRunner

        from kstrl.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            root = Path(fs)
            script, marker = self._fake_agent(root)
            (root / "kstrl.toml").write_text(f"[factory]\nmax_cost_usd = {toml_value}\n")
            (root / "s.md").write_text("# spec\nbuild a thing\n")
            result = runner.invoke(
                cli,
                ["factory", "--spec", "s.md", "--project-name", "p", "--agent-cmd", str(script)]
                + extra_args,
                catch_exceptions=True,
            )
            spent = marker.exists()

        assert not spent, "an agent call happened before the ceiling was checked"
        assert result.exit_code == 1
        assert expected in _strip_ansi(result.output)

    def test_a_valid_ceiling_does_not_block_the_spec_path(self) -> None:
        """The preflight must reject bad values without rejecting good
        ones - otherwise it would read as 'fixed' while breaking every
        ordinary run."""
        from click.testing import CliRunner

        from kstrl.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            root = Path(fs)
            script, marker = self._fake_agent(root)
            (root / "kstrl.toml").write_text(
                "[factory]\nmax_cost_usd = 5.0\nmax_total_tokens = 1000\n"
            )
            (root / "s.md").write_text("# spec\nbuild a thing\n")
            result = runner.invoke(
                cli,
                ["factory", "--spec", "s.md", "--project-name", "p", "--agent-cmd", str(script)],
                catch_exceptions=True,
            )
            reached_agent = marker.exists()
            output = _strip_ansi(result.output)

        # The run proceeds to the architect (and then fails on the fake
        # agent's empty output, which is fine - what matters is that the
        # ceiling did not stop it).
        assert reached_agent
        assert "must be a finite number" not in output
        assert "must be >= 0" not in output


class TestTokenCeilingRejectsUnboundingValues:
    """The sibling knob had the identical defect.

    `max_total_tokens = -5` made `max_total_tokens > 0` false, so the
    ceiling disabled itself while still reading as configured - the
    exact failure mode flagged for max_cost_usd, in the knob that
    predates it.
    """

    def test_negative_toml_value_is_rejected(self, tmp_path: Path) -> None:
        from kstrl.factory import BudgetConfigError, FactoryConfig

        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_total_tokens = -5\n")
        with pytest.raises(BudgetConfigError):
            FactoryConfig.load(tmp_path)

    def test_negative_env_value_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl.factory import BudgetConfigError, FactoryConfig

        monkeypatch.setenv("KSTRL_FACTORY_MAX_TOTAL_TOKENS", "-5")
        with pytest.raises(BudgetConfigError):
            FactoryConfig.from_env()

    def test_zero_is_unbounded_not_invalid(self, tmp_path: Path) -> None:
        from kstrl.factory import FactoryConfig

        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_total_tokens = 0\n")
        assert FactoryConfig.load(tmp_path).max_total_tokens == 0

    def test_ordinary_value_survives(self, tmp_path: Path) -> None:
        from kstrl.factory import FactoryConfig

        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_total_tokens = 500\n")
        assert FactoryConfig.load(tmp_path).max_total_tokens == 500

    def test_run_factory_rechecks_a_programmatic_config(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.config import KstrlConfig
        from kstrl.factory import BudgetConfigError, FactoryConfig, run_factory
        from kstrl.manifest import Component, Manifest
        from kstrl.ui.plain import PlainUI

        config = FactoryConfig(max_total_tokens=-5)
        manifest = Manifest(
            version="1",
            spec_file="s",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    "comp-a",
                    "A",
                    "D",
                    [],
                    "prd.json",
                    "kstrl/comp-a",
                )
            ],
        )
        base = KstrlConfig(ui_mode="plain", no_color=True)
        with pytest.raises(BudgetConfigError):
            run_factory(manifest, config, base, PlainUI(no_color=True), tmp_path)


# ---------------------------------------------------------------------------
# R8: cost-ceiling COVERAGE.
#
# WHY, measured rather than assumed. A paid factory run set
# `--max-cost-usd 25.0` and got a ceiling that bounded ONE role. Its own
# per-phase component_usage events:
#
#     engineer   calls=5  tokens= 8,036,800  cost=$9.9929  cost_calls=5
#     review     calls=2  tokens=    78,157  cost=$0.0000  cost_calls=0
#     engineer   calls=1  tokens= 7,939,537  cost=$7.3448  cost_calls=1
#     review     calls=3  tokens=   115,476  cost=$0.0000  cost_calls=0
#     engineer   calls=1  tokens= 7,404,185  cost=$7.4238  cost_calls=1
#     engineer   calls=1  tokens= 2,947,879  cost=$3.9930  cost_calls=1
#
# The run's TOTAL cost equalled the engineer total exactly: 193,633
# reviewer tokens contributed $0. TOKENS are counted across roles
# (26,522,034 run vs 26,328,401 engineer) - only the dollar figure
# under-counts, because the cross-family reviewer (codex) reports a
# token total and no cost. An adapter capability gap, not a mis-wired
# meter.
#
# The gap was invisible on every surface: nothing was breached, no
# ceiling was unenforceable (the engineer reports cost perfectly well),
# and the rollup's lower-bound footer reads `unreported_calls`, which was
# 0 because every call reported SOMETHING.
# ---------------------------------------------------------------------------


# (phase, calls, total_tokens, cost_usd, cost_calls, token_calls) verbatim.
_MEASURED_RUN = [
    ("engineer", 5, 8_036_800, 9.9929, 5, 5),
    ("review", 2, 78_157, 0.0, 0, 2),
    ("engineer", 1, 7_939_537, 7.3448, 1, 1),
    ("review", 3, 115_476, 0.0, 0, 3),
    ("engineer", 1, 7_404_185, 7.4238, 1, 1),
    ("engineer", 1, 2_947_879, 3.9930, 1, 1),
]


def _measured_meter() -> tuple[dict[str, dict[str, UsageTotals]], UsageTotals]:
    """The production run's meter and run total, rebuilt from its events."""
    meter: dict[str, dict[str, UsageTotals]] = {}
    run_usage = UsageTotals()
    for index, row in enumerate(_MEASURED_RUN):
        phase, calls, tokens, cost, cost_calls, token_calls = row
        totals = UsageTotals(
            calls=calls,
            known_calls=calls,
            token_calls=token_calls,
            cost_calls=cost_calls,
            total_tokens=tokens,
            cost_usd=cost,
        )
        comp_id = f"comp-{index // 2}"
        meter.setdefault(comp_id, {}).setdefault(
            phase,
            UsageTotals(),
        ).merge(totals)
        run_usage.merge(totals)
    return meter, run_usage


class TestMeasuredCoverageGap:
    """The measurement itself, pinned so the premise cannot rot."""

    def test_cost_undercounts_while_tokens_do_not(self) -> None:
        meter, run_usage = _measured_meter()
        engineer_cost = sum(
            phases["engineer"].cost_usd for phases in meter.values() if "engineer" in phases
        )
        engineer_tokens = sum(
            phases["engineer"].total_tokens for phases in meter.values() if "engineer" in phases
        )
        # The run's dollar total IS the engineer's, to the cent.
        assert run_usage.cost_usd == pytest.approx(engineer_cost)
        # Tokens are counted across roles; only money under-counts.
        assert run_usage.total_tokens == 26_522_034
        assert engineer_tokens == 26_328_401

    def test_no_existing_signal_could_see_it(self, tmp_path: Path) -> None:
        """Every pre-existing surface reported this run as healthy."""
        from kstrl.pipeline import ComponentPipeline

        meter, run_usage = _measured_meter()
        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))
        pipeline.usage_meter = meter
        pipeline.run_usage = run_usage
        pipeline.factory_config = _factory_config(tmp_path, max_cost_usd=25.0)

        # Not breached at the moment the reviews were logged...
        assert run_usage.costless_calls == 5
        # ...not unenforceable either: the engineer reports cost.
        assert ComponentPipeline.cost_budget_unenforceable(pipeline) is None
        assert ComponentPipeline.unenforceable_ceilings(pipeline) == []
        # ...and the rollup's lower-bound footer keys off a counter that
        # this run left at zero.
        assert run_usage.unreported_calls == 0


class TestUsageCoverage:
    """The coverage fold: the middle term between enforceable and dead."""

    def test_the_measured_run_is_partial_and_names_the_role(self) -> None:
        from kstrl.agents.base import usage_coverage

        meter, _ = _measured_meter()
        coverage = usage_coverage(meter, axis="cost", ceiling="max_cost_usd")
        assert coverage.calls == 13
        assert coverage.covered_calls == 8
        assert coverage.uncovered_calls == 5
        assert coverage.partial is True
        assert coverage.complete is False
        assert coverage.empty is False
        assert coverage.uncovered_roles == ("review",)
        assert coverage.uncovered_tokens == 193_633

    def test_the_token_axis_of_the_same_run_is_complete(self) -> None:
        """Both axes are folded by the same code and must not be
        conflated: this run's TOKEN coverage was perfect."""
        from kstrl.agents.base import usage_coverage

        meter, _ = _measured_meter()
        coverage = usage_coverage(meter, axis="token")
        assert coverage.complete is True
        assert coverage.note() == ""

    def test_the_note_states_the_gap_without_inventing_a_price(self) -> None:
        """The uncovered magnitude stays in TOKENS. Converting it to
        dollars would need a price table this repo does not have; a
        fabricated cost in an audit trail is worse than a missing one."""
        from kstrl.agents.base import usage_coverage

        meter, _ = _measured_meter()
        note = usage_coverage(
            meter,
            axis="cost",
            ceiling="max_cost_usd",
        ).note()
        assert "8 of 13 metered call(s) reported a cost" in note
        assert "max_cost_usd bounds only those" in note
        assert "review" in note
        assert "193,633 token(s) unpriced" in note
        assert "lower bound" in note
        # No estimated dollar figure anywhere in the sentence.
        assert "$" not in note

    def test_a_partially_covered_role_does_not_attribute_its_tokens(
        self,
    ) -> None:
        """A role where SOME calls reported a cost cannot say which of
        its tokens were unpriced - the aggregate does not carry that -
        so no token figure is claimed for it."""
        from kstrl.agents.base import usage_coverage

        meter = {
            "comp-a": {
                "review": UsageTotals(
                    calls=4,
                    known_calls=4,
                    token_calls=4,
                    cost_calls=2,
                    total_tokens=1_000,
                    cost_usd=0.5,
                )
            }
        }
        coverage = usage_coverage(meter, axis="cost", ceiling="max_cost_usd")
        assert coverage.uncovered_tokens == 0  # not 1000, and not a guess
        assert "review (2 of 4 call(s))" in coverage.note()
        assert "1,000" not in coverage.note()

    def test_full_coverage_says_nothing(self) -> None:
        from kstrl.agents.base import usage_coverage

        meter = {
            "comp-a": {
                "engineer": UsageTotals(
                    calls=3,
                    known_calls=3,
                    token_calls=3,
                    cost_calls=3,
                    total_tokens=10,
                    cost_usd=1.0,
                )
            }
        }
        coverage = usage_coverage(meter, axis="cost", ceiling="max_cost_usd")
        assert coverage.complete is True
        assert coverage.note() == ""

    def test_an_empty_axis_is_labelled_empty_not_partial(self) -> None:
        from kstrl.agents.base import usage_coverage

        meter = {
            "comp-a": {
                "engineer": UsageTotals(
                    calls=2,
                    known_calls=2,
                    token_calls=2,
                    cost_calls=0,
                    total_tokens=500,
                )
            }
        }
        coverage = usage_coverage(meter, axis="cost", ceiling="max_cost_usd")
        assert coverage.empty is True
        assert coverage.partial is False
        assert "EMPTY" in coverage.note()

    def test_an_empty_meter_reports_nothing(self) -> None:
        from kstrl.agents.base import usage_coverage

        coverage = usage_coverage({}, axis="cost", ceiling="max_cost_usd")
        assert coverage.calls == 0
        assert coverage.complete is False  # no calls is not "covered"
        assert coverage.note() == ""

    def test_an_unknown_axis_degrades_instead_of_raising(self) -> None:
        """Accounting must never gate a run (R3.1 requirement 4)."""
        from kstrl.agents.base import usage_coverage

        meter, _ = _measured_meter()
        coverage = usage_coverage(meter, axis="bananas")
        assert coverage.covered_calls == 0
        assert coverage.calls == 13

    def test_roles_fold_across_components(self) -> None:
        from kstrl.agents.base import usage_coverage

        meter, _ = _measured_meter()
        coverage = usage_coverage(meter, axis="cost", ceiling="max_cost_usd")
        roles = {role.role: role for role in coverage.roles}
        assert set(roles) == {"engineer", "review"}
        assert roles["engineer"].calls == 8  # 5 + 1 + 1 + 1
        assert roles["review"].calls == 5  # 2 + 3
        assert roles["review"].silent is True
        assert roles["engineer"].covered is True


class TestPipelineCeilingCoverage:
    """The pipeline accessor: configured ceilings only."""

    @staticmethod
    def _pipeline(tmp_path: Path, **config: Any) -> Any:
        from kstrl.pipeline import ComponentPipeline

        meter, run_usage = _measured_meter()
        pipeline = cast(Any, ComponentPipeline.__new__(ComponentPipeline))
        pipeline.usage_meter = meter
        pipeline.run_usage = run_usage
        pipeline.factory_config = _factory_config(tmp_path, **config)
        return pipeline

    def test_a_configured_cost_ceiling_reports_its_coverage(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(tmp_path, max_cost_usd=25.0)
        coverage = ComponentPipeline.ceiling_coverage(pipeline, "max_cost_usd")
        assert coverage is not None
        assert coverage.ceiling == "max_cost_usd"
        assert coverage.uncovered_roles == ("review",)

    def test_an_unconfigured_ceiling_has_nothing_to_qualify(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(tmp_path)
        assert (
            ComponentPipeline.ceiling_coverage(
                pipeline,
                "max_cost_usd",
            )
            is None
        )
        assert (
            ComponentPipeline.ceiling_coverage(
                pipeline,
                "max_total_tokens",
            )
            is None
        )

    def test_an_unknown_key_is_not_a_ceiling(self, tmp_path: Path) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(tmp_path, max_cost_usd=25.0)
        assert (
            ComponentPipeline.ceiling_coverage(
                pipeline,
                "max_adversarial_calls",
            )
            is None
        )

    def test_notes_are_emitted_only_for_ceilings_that_fall_short(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.pipeline import ComponentPipeline

        pipeline = self._pipeline(
            tmp_path,
            max_cost_usd=25.0,
            max_total_tokens=30_000_000,
        )
        notes = ComponentPipeline.coverage_notes(
            pipeline,
            ("max_total_tokens", "max_cost_usd"),
        )
        # The token ceiling covered every call on this run; only the
        # cost one has anything to disclose.
        assert len(notes) == 1
        assert "max_cost_usd bounds only those" in notes[0]


class TestRollupReportsPerAxisCoverage:
    """The rollup footer is where the operator reads the totals."""

    def test_the_measured_run_gets_a_cost_coverage_note(self) -> None:
        meter, run_usage = _measured_meter()
        lines = format_usage_rollup(meter, run_usage)
        notes = [line for line in lines if line.startswith("note:")]
        assert len(notes) == 1
        assert "cost coverage is PARTIAL" in notes[0]
        assert "8 of 13 metered call(s) reported a cost" in notes[0]
        assert "review" in notes[0]
        # The pre-existing footer could not fire: nothing was unreported.
        assert run_usage.unreported_calls == 0

    def test_an_uncosted_row_renders_a_dash_not_a_zero(self) -> None:
        """`-` means "no call here reported a cost", never "free"."""
        meter, run_usage = _measured_meter()
        lines = format_usage_rollup(meter, run_usage)
        review_rows = [line for line in lines if " review " in line]
        assert review_rows
        for row in review_rows:
            assert "0.0000" not in row

    def test_a_reported_zero_cost_is_not_rendered_as_silence(self) -> None:
        totals = UsageTotals(
            calls=1,
            known_calls=1,
            token_calls=1,
            cost_calls=1,
            total_tokens=10,
            cost_usd=0.0,
        )
        lines = format_usage_rollup({"comp-a": {"engineer": totals}}, totals)
        assert "0.0000" in lines[1]

    def test_a_cost_only_row_renders_dashes_not_zero_tokens(self) -> None:
        """R8 review finding 2: the token cells were gated on
        ``known_calls``, so a cost-only invocation printed ``0 0 0``
        tokens while the footer said token coverage was EMPTY and the
        total was a lower bound - the row and the footer contradicted
        each other. The reviewer's repro is one cost-only record.
        """
        totals = UsageTotals()
        totals.add_record(
            UsageRecord(
                cost_usd=1.0,
                source="claude-stream-json",
            )
        )
        assert (totals.known_calls, totals.token_calls) == (1, 0)
        lines = format_usage_rollup({"comp-a": {"engineer": totals}}, totals)
        rows = [line for line in lines if not line.startswith("note:")]
        # Both the component row and the TOTAL row showed numeric zeros.
        assert len(rows) == 3
        for row in rows[1:]:
            assert row.split()[-4:-2] == ["-", "-"], row
        assert any("token coverage is EMPTY" in line for line in lines)

    def test_a_reported_zero_token_count_is_not_rendered_as_silence(
        self,
    ) -> None:
        """The counterpart of the reported-zero-cost case: a call that
        reported zero tokens is a measurement, not silence."""
        totals = UsageTotals(
            calls=1,
            known_calls=1,
            token_calls=1,
            cost_calls=1,
            total_tokens=0,
            cost_usd=1.0,
        )
        lines = format_usage_rollup({"comp-a": {"engineer": totals}}, totals)
        assert lines[1].split()[-4:-2] == ["0", "0"]

    def test_a_fully_covered_run_gets_no_coverage_note(self) -> None:
        totals = UsageTotals(
            calls=2,
            known_calls=2,
            token_calls=2,
            cost_calls=2,
            total_tokens=10,
            cost_usd=1.0,
        )
        lines = format_usage_rollup({"comp-a": {"engineer": totals}}, totals)
        assert not [line for line in lines if line.startswith("note:")]

    def test_a_silent_run_keeps_exactly_one_note(self) -> None:
        """When nothing was reported at all the pre-existing footer says
        so precisely; three lines for one fact would read as noise."""
        unknown = UsageTotals()
        unknown.add_record(UsageRecord(duration_seconds=3.0))
        lines = format_usage_rollup({"comp-a": {"engineer": unknown}}, unknown)
        notes = [line for line in lines if line.startswith("note:")]
        assert len(notes) == 1
        assert "lower bounds" in notes[0]


class TestUsageCursorIsRecordsNotCalls:
    """#257 review: ``since`` is an index into ``usage_records``, so the
    cursor must count RECORDS. ``collect_usage(...).calls`` happens to
    equal that only because ``add_record`` increments unconditionally."""

    class _Hostile:
        """A record whose field access blows up mid-walk.

        Not contrived: ``collect_usage`` wraps its whole loop in a
        try/except precisely because a foreign or malformed record can do
        this, and that guard is what makes ``calls`` disagree with
        ``len(records)``.
        """

        @property
        def input_tokens(self) -> int:
            raise RuntimeError("malformed record")

    @staticmethod
    def _agent() -> SimpleNamespace:
        """Four records; the second raises on field access."""
        good = UsageRecord(total_tokens=5, source="claude-stream-json")
        return SimpleNamespace(
            usage_records=[good, TestUsageCursorIsRecordsNotCalls._Hostile(), good, good]
        )

    def test_a_walk_that_dies_partway_leaves_calls_short(self) -> None:
        agent = self._agent()
        # `calls` increments BEFORE the fields are read, so the record
        # that raised is counted and the two after it are not.
        assert collect_usage(agent).calls == 2
        assert usage_cursor(agent) == 4

    def test_the_short_offset_misattributes_the_unread_tail(self) -> None:
        """Why the distinction is worth its own helper: seeding `since`
        from `calls` hands the FIRST unit of work's unread records to the
        SECOND one."""
        agent = self._agent()
        short = collect_usage(agent).calls
        assert collect_usage(agent, since=short).calls == 2, "tail re-folded"
        assert collect_usage(agent, since=usage_cursor(agent)).calls == 0

    def test_a_cursor_over_a_normal_agent_is_the_record_count(self) -> None:
        agent = SimpleNamespace(usage_records=[UsageRecord(), UsageRecord()])
        assert usage_cursor(agent) == 2

    def test_an_agent_without_records_has_a_zero_cursor(self) -> None:
        """Folds everything, which is the pre-``since`` behavior."""
        assert usage_cursor(object()) == 0

    def test_a_hostile_records_attribute_degrades_to_zero(self) -> None:
        class Boom:
            @property
            def usage_records(self) -> list[UsageRecord]:
                raise RuntimeError("no records for you")

        assert usage_cursor(Boom()) == 0


class TestRollupRowsFollowTheOrderTheRolesRun:
    """#257: the architect was missing from ``_USAGE_PHASE_ORDER``, and
    unlisted phases sort last, so the role that runs FIRST printed after
    distill."""

    @staticmethod
    def _phase_column(lines: list[str]) -> list[str]:
        """Phase cells of the component rows, top to bottom."""
        return [line.split()[1] for line in lines if line.startswith("comp-a ")]

    @staticmethod
    def _meter(*phases: str) -> tuple[dict[str, dict[str, UsageTotals]], UsageTotals]:
        rows = {
            phase: UsageTotals(
                calls=1,
                known_calls=1,
                token_calls=1,
                cost_calls=1,
                total_tokens=1,
            )
            for phase in phases
        }
        run_usage = UsageTotals()
        for totals in rows.values():
            run_usage.merge(totals)
        return {"comp-a": rows}, run_usage

    def test_the_architect_prints_first(self) -> None:
        meter, run_usage = self._meter(
            "distill",
            "security",
            "review",
            "engineer",
            "architect",
        )
        lines = format_usage_rollup(meter, run_usage)
        assert self._phase_column(lines) == [
            "architect",
            "engineer",
            "review",
            "security",
            "distill",
        ]

    def test_an_unlisted_phase_still_sorts_last(self) -> None:
        """The listed order is a whitelist, not a total order: a phase
        added later must not silently displace a known one."""
        meter, run_usage = self._meter("zeta", "architect", "engineer")
        lines = format_usage_rollup(meter, run_usage)
        assert self._phase_column(lines) == ["architect", "engineer", "zeta"]


class TestCoverageReachesTheAuditTrail:
    """A mixed run, end to end: engineer reports cost, reviewer does not.

    The exact production shape, at test scale. Asserts the durable
    events.jsonl and the legacy progress.jsonl carry the same coverage
    facts - the divergence #181 fixed must not come back through a new
    field.
    """

    @staticmethod
    def _mixed_run(
        tmp_path: Path,
        *,
        max_cost_usd: float,
        engineer_costs: dict[str, float],
    ) -> tuple[Path, str]:
        """comp-a then comp-b, engineer cost per component.

        Two components on purpose: the budget gate fires the moment the
        engineer's usage lands, so a run that breaches on its FIRST
        component never reaches a review and would have perfect
        coverage. The measured run breached later, after several
        reviewer calls had gone unpriced.
        """
        from kstrl.review import ReviewResult

        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest([_component("comp-a"), _component("comp-b")])
        config = _factory_config(
            root,
            max_cost_usd=max_cost_usd,
            review_mode="advisory",
        )

        def fresh_review_agent(*args: Any, **kwargs: Any) -> FakeUsageAgent:
            # A NEW agent per phase: usage_records accumulate on the
            # instance, and a shared one would re-count the first
            # component's reviewer call against the second.
            agent = FakeUsageAgent(outputs=[["ok"]])
            # The cross-family reviewer: a token total and no cost,
            # which is verbatim what the codex adapter produces.
            agent._usage_records.append(
                UsageRecord(
                    total_tokens=40_000,
                    duration_seconds=0.5,
                    source="codex-text",
                )
            )
            return agent

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            comp_id = str(args[0])
            return ComponentResult(
                comp_id,
                success=True,
                iterations=1,
                usage=_engineer_usage(
                    1_000,
                    cost=engineer_costs.get(comp_id, 0.0),
                ),
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch(
                "kstrl.git.get_diff_content",
                return_value="",
            ),
            patch(
                "kstrl.agents.get_agent",
                side_effect=fresh_review_agent,
            ),
            patch(
                "kstrl.factory.run_review",
                return_value=ReviewResult(passed=True, mode="advisory"),
            ),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )
        run_dirs = sorted((root / ".kstrl" / "runs").iterdir())
        return root, str(run_dirs[-1])

    @staticmethod
    def _events(path: Path, name: str) -> list[dict[str, Any]]:
        rows = []
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("event") == name:
                    rows.append(row)
        return rows

    def test_the_gap_is_announced_before_the_money_is_spent(
        self,
        tmp_path: Path,
    ) -> None:
        """A `budget_coverage` record lands at the first phase that
        exposes the gap - not only in the halt the operator reads
        afterwards. Cost stays well under the ceiling here, so nothing
        breached and nothing is unenforceable: this signal is the ONLY
        one that fires."""
        root, run_dir = self._mixed_run(
            tmp_path,
            max_cost_usd=100.0,
            engineer_costs={"comp-a": 0.01, "comp-b": 0.01},
        )
        durable = self._events(
            Path(run_dir) / "events.jsonl",
            "budget_coverage",
        )
        assert len(durable) == 1, "one warning per ceiling per run"
        data = durable[0]["data"]
        assert data["ceiling"] == "max_cost_usd"
        assert data["axis"] == "cost"
        assert data["uncovered_roles"] == ["review"]
        assert data["uncovered_tokens"] == 40_000
        assert data["covered_calls"] == 1
        assert "PARTIAL" in data["detail"]
        # Nothing halted; the run completed under its ceiling.
        assert not self._events(
            Path(run_dir) / "events.jsonl",
            "budget_exceeded",
        )

    def test_both_sinks_carry_the_same_coverage_record(
        self,
        tmp_path: Path,
    ) -> None:
        root, run_dir = self._mixed_run(
            tmp_path,
            max_cost_usd=100.0,
            engineer_costs={"comp-a": 0.01, "comp-b": 0.01},
        )
        durable = self._events(
            Path(run_dir) / "events.jsonl",
            "budget_coverage",
        )
        legacy = self._events(root / "progress.jsonl", "budget_coverage")
        assert len(durable) == len(legacy) == 1
        assert durable[0]["data"] == legacy[0]["data"]

    def test_the_halt_record_says_what_the_ceiling_counted(
        self,
        tmp_path: Path,
    ) -> None:
        """The measured defect: a breach message that states a total
        without stating what the total covers."""
        # comp-a stays under the ceiling so its review runs and its
        # 40,000 unpriced reviewer tokens are on the meter; comp-b's
        # engineer then breaches. The measured shape.
        root, run_dir = self._mixed_run(
            tmp_path,
            max_cost_usd=0.10,
            engineer_costs={"comp-a": 0.05, "comp-b": 0.25},
        )
        durable = self._events(
            Path(run_dir) / "events.jsonl",
            "budget_exceeded",
        )
        legacy = self._events(root / "progress.jsonl", "budget_exceeded")
        assert durable and len(durable) == len(legacy)
        for a, b in zip(durable, legacy, strict=True):
            assert a["data"] == b["data"]
        coverage = durable[-1]["data"]["coverage"]
        assert coverage, "the halt must record what its ceiling covered"
        assert coverage[0]["ceiling"] == "max_cost_usd"
        assert coverage[0]["uncovered_roles"] == ["review"]

    def test_the_component_error_states_the_coverage(
        self,
        tmp_path: Path,
    ) -> None:
        # comp-a stays under the ceiling so its review runs and its
        # 40,000 unpriced reviewer tokens are on the meter; comp-b's
        # engineer then breaches. The measured shape.
        root, run_dir = self._mixed_run(
            tmp_path,
            max_cost_usd=0.10,
            engineer_costs={"comp-a": 0.05, "comp-b": 0.25},
        )
        failures = self._events(
            Path(run_dir) / "events.jsonl",
            "component_failed",
        )
        errors = [row["data"]["error"] for row in failures]
        budget_errors = [e for e in errors if "cost budget" in e]
        assert budget_errors
        assert any("cost coverage is PARTIAL" in e for e in budget_errors)
        assert any("review" in e for e in budget_errors)

    def test_a_fully_covered_halt_message_is_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        """No coverage clause when the ceiling counted every call - the
        disclosure must not become boilerplate on runs it does not
        apply to."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, max_cost_usd=0.10)
        success = ComponentResult(
            "comp-a",
            success=True,
            iterations=1,
            usage=_engineer_usage(600, cost=0.25),
        )
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert "cost budget exceeded" in (comp_a.error or "")
        assert "coverage" not in (comp_a.error or "")

        # The EVENT still records the coverage, positively. An absent
        # field would be ambiguous between "covered everything" and
        # "written before this landed"; a present entry with no
        # uncovered calls is a verified fact.
        run_dir = sorted((root / ".kstrl" / "runs").iterdir())[-1]
        halts = self._events(run_dir / "events.jsonl", "budget_exceeded")
        assert halts
        coverage = halts[-1]["data"]["coverage"]
        assert len(coverage) == 1
        assert coverage[0]["ceiling"] == "max_cost_usd"
        assert coverage[0]["uncovered_calls"] == 0
        assert coverage[0]["uncovered_roles"] == []


class TestCeilingScopeIsStatedUpFront:
    """At the plan stage an operator can still act; after the spend they
    cannot. What CANNOT be stated up front is which roles will report -
    no call has been made, so that would be a prediction."""

    @staticmethod
    def _run(tmp_path: Path, **config: Any) -> str:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        buffer = io.StringIO()
        success = ComponentResult(
            "comp-a",
            success=True,
            iterations=1,
            usage=_engineer_usage(10),
        )
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                _factory_config(root, **config),
                _make_base_config(root),
                PlainUI(no_color=True, file=buffer),
                root,
            )
        return buffer.getvalue()

    def test_the_cost_ceiling_states_what_it_counts(
        self,
        tmp_path: Path,
    ) -> None:
        out = self._run(tmp_path, max_cost_usd=25.0)
        assert "Cost ceiling" in out
        assert "$25.0" in out
        assert "counts only calls whose agent reports a cost" in out

    def test_the_token_ceiling_states_what_it_counts(
        self,
        tmp_path: Path,
    ) -> None:
        out = self._run(tmp_path, max_total_tokens=500_000)
        assert "Token ceiling" in out
        assert "counts only calls whose agent reports a token count" in out

    def test_an_unconfigured_ceiling_is_not_advertised(
        self,
        tmp_path: Path,
    ) -> None:
        out = self._run(tmp_path)
        assert "Cost ceiling" not in out
        assert "Token ceiling" not in out


class TestEveryConfiguredCeilingRecordsItsCoverage:
    """R8 review finding 3: coverage was built by iterating ``ceilings``.

    ``ceilings`` is the CAUSAL halt identity - with both caps enabled a
    token breach makes ``budget_halt_identity()`` return
    ``("max_total_tokens",)`` alone, so a simultaneously partial
    ``max_cost_usd`` never reached the halt event or the inbox evidence.
    That contradicts the docstring's "EVERY configured named ceiling is
    recorded", and makes a missing per-ceiling entry ambiguous between
    "not configured" and "not the cause".
    """

    @staticmethod
    def _dual_cap_run(tmp_path: Path) -> tuple[Path, Path]:
        """One component whose engineer breaches the TOKEN cap, after a
        review call that reported tokens and no cost.

        The reviewer's shape: two metered calls, both caps configured,
        the token axis fully covered and the cost axis PARTIAL.
        """
        from kstrl.review import ReviewResult

        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest([_component("comp-a"), _component("comp-b")])
        config = _factory_config(
            root,
            max_total_tokens=5_000,
            max_cost_usd=100.0,
            review_mode="advisory",
        )

        def fresh_review_agent(*args: Any, **kwargs: Any) -> FakeUsageAgent:
            agent = FakeUsageAgent(outputs=[["ok"]])
            # The cross-family reviewer: tokens, no cost (codex).
            agent._usage_records.append(
                UsageRecord(
                    total_tokens=1_000,
                    duration_seconds=0.5,
                    source="codex-text",
                )
            )
            return agent

        costs = {"comp-a": 0.01, "comp-b": 0.02}
        tokens = {"comp-a": 1_000, "comp-b": 9_000}

        def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
            comp_id = str(args[0])
            return ComponentResult(
                comp_id,
                success=True,
                iterations=1,
                usage=_engineer_usage(tokens[comp_id], cost=costs[comp_id]),
            )

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch(
                "kstrl.git.get_diff_content",
                return_value="",
            ),
            patch(
                "kstrl.agents.get_agent",
                side_effect=fresh_review_agent,
            ),
            patch(
                "kstrl.factory.run_review",
                return_value=ReviewResult(passed=True, mode="advisory"),
            ),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )
        run_dir = sorted((root / ".kstrl" / "runs").iterdir())[-1]
        return root, run_dir

    @staticmethod
    def _events(path: Path, name: str) -> list[dict[str, Any]]:
        rows = []
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("event") == name:
                    rows.append(row)
        return rows

    def test_a_token_breach_still_records_the_cost_ceilings_coverage(
        self,
        tmp_path: Path,
    ) -> None:
        root, run_dir = self._dual_cap_run(tmp_path)
        halts = self._events(run_dir / "events.jsonl", "budget_exceeded")
        assert halts, "the token cap must have halted the run"
        data = halts[-1]["data"]
        # The causal identity is unchanged: the TOKEN cap is what fired.
        assert data["ceilings"] == ["max_total_tokens"]
        assert data["condition"] == "breached"
        by_ceiling = {entry["ceiling"]: entry for entry in data["coverage"]}
        assert set(by_ceiling) == {"max_total_tokens", "max_cost_usd"}
        # Positively recorded, both ways round: the token ceiling counted
        # everything, the cost ceiling did not.
        assert by_ceiling["max_total_tokens"]["uncovered_calls"] == 0
        assert by_ceiling["max_cost_usd"]["uncovered_roles"] == ["review"]

    def test_the_inbox_evidence_carries_the_same_entries(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.inbox import Inbox, InboxConfig

        root, _ = self._dual_cap_run(tmp_path)
        items = [
            item
            for item in Inbox(root, InboxConfig()).items()
            if str(item.kind) == "budget_overrun"
        ]
        assert items
        coverage = items[-1].evidence["coverage"]
        assert {entry["ceiling"] for entry in coverage} == {
            "max_total_tokens",
            "max_cost_usd",
        }

    def test_both_sinks_still_agree(self, tmp_path: Path) -> None:
        root, run_dir = self._dual_cap_run(tmp_path)
        durable = self._events(run_dir / "events.jsonl", "budget_exceeded")
        legacy = self._events(root / "progress.jsonl", "budget_exceeded")
        assert durable and len(durable) == len(legacy)
        for a, b in zip(durable, legacy, strict=True):
            assert a["data"] == b["data"]

    def test_a_disabled_ceiling_is_still_absent(self, tmp_path: Path) -> None:
        """``ceiling_coverage()`` filters unconfigured ceilings, so an
        absent entry keeps ONE meaning: that cap was not enabled."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, max_total_tokens=500)
        success = ComponentResult(
            "comp-a",
            success=True,
            iterations=1,
            usage=_engineer_usage(600),
        )
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True),
                root,
            )
        run_dir = sorted((root / ".kstrl" / "runs").iterdir())[-1]
        halts = self._events(run_dir / "events.jsonl", "budget_exceeded")
        assert halts
        coverage = halts[-1]["data"]["coverage"]
        assert [entry["ceiling"] for entry in coverage] == ["max_total_tokens"]


@dataclass(frozen=True)
class _SeededRun:
    """One `ks factory` run and everything a #257 assertion reads off it."""

    root: Path
    manifest: Manifest
    launched: list[str]
    console: io.StringIO
    result: FactoryResult

    @property
    def printed(self) -> str:
        return self.console.getvalue()

    def usage_events(self, phase: str) -> list[dict[str, Any]]:
        return _usage_events(self.root, phase)


def _run_with_architect_spend(
    tmp_path: Path,
    architect_usage: UsageTotals | None,
    *,
    component_id: str = "comp-a",
    **config: Any,
) -> _SeededRun:
    """A one-component run whose engineer is cheap and whose architect
    already spent whatever the caller says `ks factory` paid it.

    The scaffolding lives here rather than at each call site because only
    the architect's spend and the ceiling ever vary between them.

    ``component_id`` exists for one caller: #281 needs a run whose single
    component is genuinely NAMED `architect`, which is a legal id an
    architect asked for a spec about design tooling may well emit.
    """
    root = _setup_project(tmp_path, [component_id])
    manifest = _make_manifest([_component(component_id)])
    launched: list[str] = []
    console = io.StringIO()

    def fake_run_component(*args: Any, **kwargs: Any) -> ComponentResult:
        launched.append(str(args[0]))
        return ComponentResult(
            str(args[0]),
            success=True,
            iterations=1,
            usage=_engineer_usage(100, cost=0.01),
        )

    with (
        patch("kstrl.factory._run_component", side_effect=fake_run_component),
        patch("kstrl.git.get_diff_content", return_value=""),
    ):
        result = run_factory(
            manifest,
            _factory_config(root, **config),
            _make_base_config(root),
            PlainUI(no_color=True, file=console),
            root,
            architect_usage=architect_usage,
        )
    return _SeededRun(root, manifest, launched, console, result)


class TestArchitectSpendCountsAgainstTheCeiling:
    """#257 piece B: ``max_cost_usd`` is enforced against
    ``pipeline.run_usage``, which only ``_record_usage`` feeds, and the
    architect never called it. An operator who set a ceiling was bounding
    engineer + review + security + distill, and the role that runs FIRST
    ran outside the bound entirely.
    """

    def test_the_ceiling_trips_before_any_engineer_launches(
        self,
        tmp_path: Path,
    ) -> None:
        """The property the issue is about: an operator who set $5 and
        whose architect spent $6 must not get an engineer at all.

        Before this the architect's $6 was not in ``run_usage``, the
        scheduling gate read $0.00, and the run went on to spend the
        whole ceiling again on top of it.
        """
        run = _run_with_architect_spend(
            tmp_path,
            _architect_usage(6.0),
            max_cost_usd=5.0,
        )

        assert run.launched == [], "the engineer must never start"
        assert "comp-a" in run.result.failed
        comp = run.manifest.get_component("comp-a")
        assert comp is not None
        assert comp.status == ComponentStatus.FAILED.value
        assert "max_cost_usd" in (comp.error or "")

    def test_the_ceiling_does_not_trip_on_spend_below_it(
        self,
        tmp_path: Path,
    ) -> None:
        """The other direction, so the gate above cannot pass by halting
        everything: an architect comfortably under the ceiling changes
        nothing about the run."""
        run = _run_with_architect_spend(
            tmp_path,
            _architect_usage(1.0),
            max_cost_usd=5.0,
        )

        assert run.launched == ["comp-a"]
        assert run.result.failed == []
        # It is in the total the ceiling reads all the same.
        assert run.usage_events(ARCHITECT_ROLE)

    def test_an_unbounded_run_is_not_given_a_bound(
        self,
        tmp_path: Path,
    ) -> None:
        """``max_cost_usd = 0`` means unbounded. Seeding real spend into
        an unbounded run must not invent a ceiling for it."""
        run = _run_with_architect_spend(tmp_path, _architect_usage(999.0))

        assert run.launched == ["comp-a"]
        assert run.result.failed == []

    def test_the_architect_is_a_row_in_the_meter_and_the_stream(
        self,
        tmp_path: Path,
    ) -> None:
        """One ``component_usage`` event, under the role name, on the
        pseudo-component `ks decompose` already reports under - so both
        commands write the architect to the same key.

        #281: that key is namespaced, and the assertion spells it from
        the constant rather than restating it. A literal here would keep
        passing against a writer that had moved on, which is exactly how
        the role row and the component keyspace drifted together in the
        first place.
        """
        run = _run_with_architect_spend(
            tmp_path,
            _architect_usage(1.5),
            max_cost_usd=50.0,
        )

        events = run.usage_events(ARCHITECT_ROLE)
        assert len(events) == 1
        assert events[0]["component"] == ARCHITECT_COMPONENT
        assert events[0]["data"]["cost_usd"] == pytest.approx(1.5)

    def test_a_run_with_no_architect_records_no_phantom_row(
        self,
        tmp_path: Path,
    ) -> None:
        """`ks factory --manifest` resumes a run that never decomposed,
        and every other ``run_factory`` caller passes nothing. An empty
        architect row would claim the role ran and cost nothing."""
        run = _run_with_architect_spend(tmp_path, None)

        assert run.launched == ["comp-a"]
        assert run.usage_events(ARCHITECT_ROLE) == []

    def test_an_architect_that_reported_nothing_records_no_row(
        self,
        tmp_path: Path,
    ) -> None:
        """A ``CustomAgent``, or any adapter predating R3.1, reports no
        usage records at all. ``collect_usage`` then yields zero calls,
        and zero calls must stay silent rather than become a $0.00 row
        that reads as "the architect was free"."""
        run = _run_with_architect_spend(tmp_path, UsageTotals())

        assert run.usage_events(ARCHITECT_ROLE) == []


class TestCoverageCountsTheArchitect:
    """#257: the architect was not an UNCOVERED metered call, it was not
    a metered call at all - so the "N of M metered call(s)" line the
    factory prints was short by a whole role, silently."""

    def test_the_uncovered_line_names_the_architect(
        self,
        tmp_path: Path,
    ) -> None:
        """A codex-shaped architect under a cost ceiling: tokens, no
        price. The warning has to name it, because a ceiling that cannot
        see the run's first call is a ceiling the operator misreads."""
        run = _run_with_architect_spend(
            tmp_path,
            _architect_usage(None),
            max_cost_usd=50.0,
        )

        assert "cost coverage is" in run.printed
        assert "architect (1 of 1 call(s), 1,000 token(s) unpriced)" in run.printed
        assert "lower bound" in run.printed

    def test_the_metered_call_count_includes_the_architect(
        self,
        tmp_path: Path,
    ) -> None:
        """The issue's line verbatim: "2 of 4 metered call(s)" would have
        been 2 of 5 with the architect enrolled. Asserted on the
        DENOMINATOR, which is the whole claim - one engineer call plus
        one architect call is two metered calls, not one."""
        run = _run_with_architect_spend(
            tmp_path,
            _architect_usage(None),
            max_cost_usd=50.0,
        )

        assert "1 of 2 metered call(s) reported a cost" in run.printed

    def test_the_rollup_prints_the_architect_in_the_run_total(
        self,
        tmp_path: Path,
    ) -> None:
        """The end-of-run table, where the operator reads what the run
        cost. The architect is a row, and the TOTAL covers it."""
        run = _run_with_architect_spend(
            tmp_path,
            _architect_usage(1.5),
            max_cost_usd=50.0,
        )

        assert "Usage rollup" in run.printed
        rows = [line for line in run.printed.splitlines() if " architect " in line]
        assert len(rows) == 1, run.printed
        assert "1.5000" in rows[0]
        total = next(line for line in run.printed.splitlines() if "TOTAL" in line)
        assert "1.5100" in total, total


def _invoke_factory_cli(
    args: list[str],
    *,
    setup: Callable[[Path], None] | None = None,
    catch_exceptions: bool = False,
) -> Any:
    """`ks factory` in an isolated filesystem holding a spec and a manifest.

    ``setup`` seeds anything else the case needs (a kstrl.toml, say).
    ``catch_exceptions`` is for the tests that assert an exception did
    NOT escape - with the default, an escaping one fails the test as a
    traceback rather than as the assertion that explains it.
    """
    from click.testing import CliRunner

    from kstrl.cli import cli

    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        (root / "s.md").write_text("# spec\n")
        _make_manifest([_component("comp-a")]).save(root / "m.json")
        if setup is not None:
            setup(root)
        return runner.invoke(cli, args, catch_exceptions=catch_exceptions)


class TestFactoryHandsTheArchitectSpendToTheRun:
    """The wiring, at the one command that has both an architect and a
    run. Without it every property above is true of a seat that nothing
    ever sits in.
    """

    @staticmethod
    def _install(
        monkeypatch: pytest.MonkeyPatch,
        *,
        halt: bool = False,
    ) -> dict[str, Any]:
        """Stand in for the two halves `ks factory` joins.

        The fake decompose bills its agent where a real adapter does -
        appending to ``usage_records`` when the call ends - so the test
        exercises the capture rather than a value handed straight
        through.
        """
        from kstrl import cli as cli_mod
        from kstrl.decisions import SpecDecision
        from kstrl.decompose import SpecBlockerError

        captured: dict[str, Any] = {}
        agent = SimpleNamespace(name="fake", usage_records=[])

        def fake_get_agent(*args: Any, **kwargs: Any) -> Any:
            return agent

        def fake_decompose(**kwargs: Any) -> Manifest:
            agent.usage_records.append(
                UsageRecord(
                    input_tokens=500,
                    output_tokens=500,
                    total_tokens=1000,
                    cost_usd=2.25,
                    duration_seconds=2.0,
                    source="claude-stream-json",
                )
            )
            if halt:
                raise SpecBlockerError(
                    [
                        SpecDecision(
                            issue="unstated",
                            question="the spec does not say",
                            disposition="escalated",
                            resolution="the owner must say",
                        )
                    ]
                )
            return _make_manifest([_component("comp-a")])

        def fake_run_factory(*args: Any, **kwargs: Any) -> Any:
            captured["architect_usage"] = kwargs.get("architect_usage")
            captured["called"] = True
            return FactoryResult()

        monkeypatch.setattr(cli_mod, "get_agent", fake_get_agent)
        monkeypatch.setattr(cli_mod, "decompose_spec", fake_decompose)
        monkeypatch.setattr(cli_mod, "run_factory", fake_run_factory)
        return captured

    def test_the_spec_path_passes_what_the_decompose_agent_spent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = self._install(monkeypatch)

        result = _invoke_factory_cli(
            [
                "factory",
                "--spec",
                "s.md",
                "--project-name",
                "p",
                "--agent-cmd",
                "true",
                "--yes",
            ]
        )

        assert result.exit_code == 0, result.output
        spent = captured["architect_usage"]
        assert spent is not None
        assert spent.calls == 1
        assert spent.cost_usd == pytest.approx(2.25)
        assert spent.total_tokens == 1000

    def test_the_manifest_path_passes_no_architect_spend(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A resume ran no architect, so there is nothing to hand over -
        and the seat must not be filled from an agent that never ran."""
        captured = self._install(monkeypatch)

        result = _invoke_factory_cli(
            [
                "factory",
                "--manifest",
                "m.json",
                "--agent-cmd",
                "true",
                "--yes",
            ]
        )

        assert result.exit_code == 0, result.output
        assert captured["architect_usage"].calls == 0

    def test_the_blocker_halt_reaches_no_run_at_all(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The accepted residue of #257 piece B, pinned so it cannot
        change silently. ``serve.RunSpend.unmetered_phases`` states why
        the halt leaves the spend nowhere on disk.
        """
        captured = self._install(monkeypatch, halt=True)

        result = _invoke_factory_cli(
            [
                "factory",
                "--spec",
                "s.md",
                "--project-name",
                "p",
                "--agent-cmd",
                "true",
                "--yes",
            ]
        )

        assert result.exit_code == 2
        assert "called" not in captured, "no run may start after a blocker halt"


class TestNothingBetweenTheRunDirAndTheMeterCanLoseTheSpend:
    """#257 review: the architect record claimed to precede every early
    exit, and two reachable ones preceded IT."""

    @staticmethod
    def _cyclic_manifest() -> Manifest:
        """What an LLM architect can hand back and `ks factory` accepts
        (`decompose.py` warns on DAG errors and returns anyway)."""
        return _make_manifest(
            [
                _component("comp-a", deps=["comp-b"]),
                _component("comp-b", deps=["comp-a"]),
            ]
        )

    @staticmethod
    def _run(root: Path, manifest: Manifest, manifest_path: Path | None = None) -> Any:
        with patch("kstrl.git.get_diff_content", return_value=""):
            return run_factory(
                manifest,
                _factory_config(root),
                _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()),
                root,
                manifest_path,
                architect_usage=_architect_usage(4.0),
            )

    def test_a_cyclic_manifest_still_records_the_architect(
        self,
        tmp_path: Path,
    ) -> None:
        """The run directory exists, so the spend has somewhere to go.

        The reachability argument lives at the record site in
        `factory._run_factory_locked`; this pins the outcome.
        """
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        result = self._run(root, self._cyclic_manifest())

        assert result.exit_code == 1, "the cyclic DAG must still fail the run"
        events = _usage_events(root, ARCHITECT_ROLE)
        assert len(events) == 1, "the spend must reach the run directory"
        assert events[0]["data"]["cost_usd"] == pytest.approx(4.0)

    def test_the_dag_check_still_rejects_before_the_manifest_is_stamped(
        self,
        tmp_path: Path,
    ) -> None:
        """Moving the meter up must not drag manifest mutation with it.

        A rejected DAG leaves the manifest unstamped and unsaved, so it
        cannot read afterwards as a run that is still in flight.
        """
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        manifest = self._cyclic_manifest()
        manifest_path = root / "scripts" / "kstrl" / "manifest.json"

        self._run(root, manifest, manifest_path)

        assert manifest.run_id == ""
        assert not manifest_path.exists()

    def test_a_refused_lock_records_nothing_because_no_run_exists(
        self,
        tmp_path: Path,
    ) -> None:
        """The other early exit, and accepted residue rather than a fix:
        the lock is refused before a run id is minted, so there is
        nowhere on disk to write. `serve.RunSpend.unmetered_phases` says
        how the daemon accounts for that."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])

        with patch(
            "kstrl.factory._acquire_run_lock",
            side_effect=FactoryLockHeldError("held by another run"),
        ):
            result = self._run(root, manifest)

        assert result.exit_code == 2
        assert not (root / ".kstrl" / "runs").exists()


class TestTheJournalKeepsTheArchitectRow:
    """#257 review: ``record_run`` iterates the MANIFEST, so an architect
    key in ``usage_by_component`` was silently dropped while
    ``run_usage`` - which includes it - fed the TSV's total_cost_usd. The
    journal's rows stopped summing to its own total."""

    def test_the_architect_reaches_the_journal(self, tmp_path: Path) -> None:
        run = _run_with_architect_spend(tmp_path, _architect_usage(1.5))

        roles = [e for e in _read_journal(run.root) if e.get("event_type") == "role_usage"]
        assert len(roles) == 1
        assert roles[0]["component_id"] == ARCHITECT_COMPONENT
        assert roles[0]["usage"][ARCHITECT_ROLE]["cost_usd"] == pytest.approx(1.5)

    def test_the_journal_rows_sum_to_the_run_total(self, tmp_path: Path) -> None:
        """The property that was broken, asserted as arithmetic rather
        than as the presence of a key."""
        run = _run_with_architect_spend(tmp_path, _architect_usage(1.5))

        _assert_journal_rows_sum_to_tsv(run, expected=1.51)

    def test_a_run_with_no_extra_role_adds_no_row(self, tmp_path: Path) -> None:
        """A resume has no architect, and every other usage key IS a
        manifest component. The new branch must add nothing."""
        run = _run_with_architect_spend(tmp_path, None)

        assert [e for e in _read_journal(run.root) if e.get("event_type") == "role_usage"] == []

    def test_the_role_row_is_not_counted_as_a_component_outcome(
        self,
        tmp_path: Path,
    ) -> None:
        """Three readers in evolution.py aggregate over component_result.
        The architect has no status, no retries and no findings, so it
        must not arrive wearing that type and skew them."""
        run = _run_with_architect_spend(tmp_path, _architect_usage(1.5))

        results = [e for e in _read_journal(run.root) if e.get("event_type") == "component_result"]
        assert [e["component_id"] for e in results] == ["comp-a"]


class TestAComponentNamedArchitectDoesNotMergeWithTheRoleRow:
    """#281: `architect` is a legal component id.

    ``validate_component_id`` enforces a charset and a length and has no
    reserved-name list, and the id is chosen by the architect LLM - so a
    spec about design tooling, which is an ordinary thing to point kstrl
    at, can produce a component genuinely called `architect`. While the
    role's own row was keyed by the bare word, the two merged in every
    surface that keys by component id.

    Every test below builds the SAME run shape - a real component named
    `architect`, plus a metered architect role - and reads a different
    surface off it. That is the run that used to be wrong. The run is
    rebuilt per test rather than shared: `_run_component` and the diff
    read are patched, so it costs ~20 ms, which measured at 0.19 s for
    the class against the file's 2.8 s.
    """

    @staticmethod
    def _collided_run(tmp_path: Path) -> _SeededRun:
        return _run_with_architect_spend(
            tmp_path,
            _architect_usage(1.5),
            component_id=ARCHITECT_ROLE,
        )

    def test_the_two_rows_are_separate_in_the_meter(self, tmp_path: Path) -> None:
        """The property the issue names first. The component's engineer
        spend and the architect's spend are two rows, not one.

        Read off the event stream rather than off the in-memory meter
        because the stream is what every later surface is rebuilt from -
        the reducer, the dashboard, ``serve``'s ledger. A meter that
        separated them and a stream that did not would be the same bug
        one layer down.
        """
        run = self._collided_run(tmp_path)

        architect = run.usage_events(ARCHITECT_ROLE)
        engineer = run.usage_events("engineer")
        assert len(architect) == len(engineer) == 1

        # Stated as a DIFFERENCE, not as equality against
        # ARCHITECT_COMPONENT. Measured: an earlier draft of this test
        # asserted each key equals its constant and passed with the
        # namespace collapsed to "" - because both constants collapse to
        # the same word alongside a component that carries it. An
        # assertion that survives the bug it names is not evidence.
        assert architect[0]["component"] != engineer[0]["component"], (
            "the role row and a component named for the role must not share a key"
        )
        assert engineer[0]["component"] == ARCHITECT_ROLE, "the component keeps its own name"
        assert architect[0]["data"]["cost_usd"] == pytest.approx(1.5)
        assert engineer[0]["data"]["cost_usd"] == pytest.approx(0.01)

    def test_the_journal_still_writes_the_role_row(self, tmp_path: Path) -> None:
        """``_role_usage_entries`` splits usage keys from manifest ids by
        SET DIFFERENCE. A component named `architect` used to empty that
        difference: no ``role_usage`` row was written at all, and the
        architect's money was reattributed to the component's own
        ``usage`` field, where three readers that aggregate over
        ``component_result`` would then count it.

        The component's own row is asserted too, and that is the
        compatibility half: #281 lists rejecting the name at validation
        as option 2, and this says option 1 was taken instead. The
        operator and the LLM keep the keyspace they already had.
        """
        run = self._collided_run(tmp_path)
        journal = _read_journal(run.root)

        roles = [e for e in journal if e.get("event_type") == "role_usage"]
        assert [e["component_id"] for e in roles] == [ARCHITECT_COMPONENT]
        assert roles[0]["usage"][ARCHITECT_ROLE]["cost_usd"] == pytest.approx(1.5)

        results = [e for e in journal if e.get("event_type") == "component_result"]
        assert [e["component_id"] for e in results] == [ARCHITECT_ROLE]
        assert set(results[0]["usage"]) == {"engineer"}, "the role's money is not the component's"

    def test_the_journal_rows_still_sum_to_the_run_total(self, tmp_path: Path) -> None:
        """The arithmetic the #257 review pinned, re-asserted on the
        colliding run."""
        _assert_journal_rows_sum_to_tsv(self._collided_run(tmp_path), expected=1.51)

    def test_the_rollup_prints_the_two_rows_under_different_labels(
        self,
        tmp_path: Path,
    ) -> None:
        """What the operator actually reads.

        Counted as DISTINCT labels rather than matched against the
        constants, for the reason given above: with the namespace
        collapsed both constants are the same word, and an assertion
        that each is present would be satisfied by the single merged row
        that is the bug.
        """
        run = self._collided_run(tmp_path)

        # The rollup's component column is fixed-width and left-aligned,
        # so the first token of a row is its key.
        labels = {
            tokens[0]
            for line in run.printed.splitlines()
            if (tokens := line.split()) and ARCHITECT_ROLE in tokens[0]
        }
        assert len(labels) == 2, f"expected a role row and a component row, got {labels}"


class TestNoConfigLoadStandsBetweenTheMoneyAndTheRun:
    """#257 review: the toml-notes sweep loaded the evolution config
    unguarded, between `decompose_spec` being paid for and `run_factory`
    being entered, where the spend exists only in a local variable.

    ``cli._collect_evolution_notes`` carries the reasoning, including
    what this does NOT fix.
    """

    @staticmethod
    def _captured_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Stop at the factory boundary and record whether we got there."""
        from kstrl import cli as cli_mod

        captured: dict[str, Any] = {}

        def fake_run_factory(*args: Any, **kwargs: Any) -> Any:
            captured["called"] = True
            return FactoryResult()

        monkeypatch.setattr(cli_mod, "run_factory", fake_run_factory)
        return captured

    ARGS = ["factory", "--manifest", "m.json", "--agent-cmd", "true", "--yes"]

    def test_a_bad_evolution_knob_does_not_leak_a_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reproduced against the real CLI before the fix: exit 1 with a
        raw ValueError traceback and no error line."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "many")
        captured = self._captured_run(monkeypatch)

        result = _invoke_factory_cli(self.ARGS, catch_exceptions=True)

        assert not isinstance(result.exception, ValueError), result.exception
        assert result.exit_code == 0, result.output
        assert "Evolution config unreadable" in result.output
        # The run still happens: an unreadable audit knob costs the NOTE
        # line, never the work the operator asked for.
        assert captured.get("called") is True

    def test_a_readable_config_still_produces_its_notes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guard must not silence the sweep it wraps, or a toml
        section that takes effect would stop announcing itself (R2.1)."""
        monkeypatch.delenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", raising=False)
        self._captured_run(monkeypatch)

        result = _invoke_factory_cli(
            self.ARGS,
            setup=lambda root: (root / "kstrl.toml").write_text("[evolution]\nlookback_runs = 7\n"),
        )

        assert result.exit_code == 0, result.output
        assert "lookback_runs" in result.output
        assert "Evolution config unreadable" not in result.output
