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
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kstrl.agents.base import UsageRecord, UsageTotals, collect_usage
from kstrl.agents.claude_code import ClaudeCodeAgent, _usage_from_result_event
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.config import KstrlConfig
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    _format_usage_rollup,
    run_factory,
)
from kstrl.loop import COMPLETION_MARKER, run_loop
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.observability import ProgressLog
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
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    return KstrlConfig(
        max_iterations=max_iterations,
        prompt_file=ralph_dir / "prompt.md",
        prd_file=ralph_dir / "prd.json",
        sleep_seconds=0,
        ralph_branch="",
        ralph_branch_explicit=True,
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
        prompt_file=root_dir / "scripts" / "ralph" / "prompt.md",
        prd_file=root_dir / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        ralph_branch="",
        ralph_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _setup_project(tmp_path: Path, component_ids: list[str]) -> Path:
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    # Knowledge distillation off by default in these tests: its agent
    # call would add nondeterministic usage rows.
    (tmp_path / "ralph.toml").write_text("[knowledge]\nenabled = false\n")
    for comp_id in component_ids:
        feature_dir = ralph_dir / "feature" / comp_id
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
        f"scripts/ralph/feature/{comp_id}/prd.json",
        f"ralph/factory/{comp_id}",
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


def _read_journal(tmp_path: Path) -> list[dict[str, Any]]:
    journal_path = tmp_path / ".ralph" / "evolution.jsonl"
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
        tsv = (root / ".ralph" / "experiments.tsv").read_text().splitlines()
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
        (root / "ralph.toml").write_text("[knowledge]\nenabled = true\n")
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
        tsv = (root / ".ralph" / "experiments.tsv").read_text().splitlines()
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
        tsv = (root / ".ralph" / "experiments.tsv").read_text().splitlines()
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
        monkeypatch.setenv("RALPH_FACTORY_MAX_TOTAL_TOKENS", "250000")
        assert FactoryConfig.from_env().max_total_tokens == 250000

    def test_load_toml_and_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "ralph.toml").write_text(
            "[factory]\nmax_total_tokens = 111\n"
        )
        assert FactoryConfig.load(tmp_path).max_total_tokens == 111
        monkeypatch.setenv("RALPH_FACTORY_MAX_TOTAL_TOKENS", "222")
        assert FactoryConfig.load(tmp_path).max_total_tokens == 222
