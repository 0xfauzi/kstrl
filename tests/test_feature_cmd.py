"""TUI surface C2: run_feature extraction - direct flow tests."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl.config import KstrlConfig
from kstrl.feature_cmd import FeatureParams, run_feature
from kstrl.interaction import (
    PromptRequest,
    PromptResponse,
)
from kstrl.loop import LoopResult
from kstrl.prd import PRD, UserStory
from kstrl.sandbox import SandboxConfig
from kstrl.ui.plain import PlainUI


class StubAgent:
    name = "stub"
    final_message: str | None = None
    usage_records: list[Any] = []

    def run(self, prompt: str, cwd: Path | None = None,
            timeout: float | None = None) -> Any:
        yield "line"


class ScriptedChannel:
    """InteractionChannel answering every request with a fixed choice."""

    def __init__(self, choice: int, *, promptable: bool = True) -> None:
        self.choice = choice
        self.promptable = promptable
        self.requests: list[PromptRequest] = []

    def can_prompt(self) -> bool:
        return self.promptable

    def request(self, req: PromptRequest) -> PromptResponse:
        self.requests.append(req)
        return PromptResponse(
            request_id=req.request_id, choice=self.choice, answered=True,
        )


def _params(tmp_path: Path, *, stories: int = 1,
            repair_max_runs: int = 0) -> FeatureParams:
    feature_dir = tmp_path / "scripts" / "kstrl" / "feature" / "demo"
    feature_dir.mkdir(parents=True, exist_ok=True)
    understand = feature_dir / "understand.md"
    understand.write_text("# understanding\n")
    prd_path = feature_dir / "prd.json"
    prd_path.write_text("{}")
    prd_doc = PRD(
        branch_name="kstrl/demo",
        user_stories=[
            UserStory(
                id=f"US-{n}", title=f"story {n}",
                acceptance_criteria=["tests pass"], priority=1,
                passes=False, notes="",
            )
            for n in range(stories)
        ],
    )
    return FeatureParams(
        prd_path=prd_path,
        prd_doc=prd_doc,
        feature_name="demo",
        feature_dir=feature_dir,
        feature_understand=understand,
        log_dir=tmp_path / ".kstrl" / "logs" / "feature_demo",
        understand_iterations=2,
        understand_prompt_file=None,
        implementation_auto_run=False,
        repair_max_runs=repair_max_runs,
        repair_iterations=2,
        repair_agent_cmd=None,
        branch_override=None,
        allowed_paths_override=None,
        sandbox=SandboxConfig(),
    )


def _loop_results(*codes: int) -> Any:
    """run_loop stub returning the given exit codes in order."""
    remaining = list(codes)

    def fake(config: Any, ui: Any, agent: Any, *args: Any,
             **kwargs: Any) -> LoopResult:
        code = remaining.pop(0)
        return LoopResult(completed=code == 0, iterations=1, exit_code=code)

    return fake


def _ui() -> tuple[PlainUI, io.StringIO]:
    stream = io.StringIO()
    return PlainUI(no_color=True, file=stream), stream


class TestReviewGate:
    def test_quit_to_amend_exits_zero(self, tmp_path: Path) -> None:
        ui, stream = _ui()
        channel = ScriptedChannel(choice=1)
        with patch("kstrl.feature_cmd.run_loop", _loop_results(0)):
            code = run_feature(
                _params(tmp_path), KstrlConfig(), StubAgent(), ui, tmp_path,
                interaction=channel,
            )
        assert code == 0
        assert "Amend the understand file" in stream.getvalue()
        assert len(channel.requests) == 1
        assert channel.requests[0].options == (
            "Start implementation", "Quit to amend",
        )

    def test_non_promptable_channel_refuses_with_2(
        self, tmp_path: Path,
    ) -> None:
        ui, stream = _ui()
        with patch("kstrl.feature_cmd.run_loop", _loop_results(0)):
            code = run_feature(
                _params(tmp_path), KstrlConfig(), StubAgent(), ui, tmp_path,
                interaction=ScriptedChannel(0, promptable=False),
            )
        assert code == 2
        assert "Interactive review required" in stream.getvalue()

    def test_auto_run_skips_the_gate(self, tmp_path: Path) -> None:
        ui, stream = _ui()
        params = _params(tmp_path)
        params.implementation_auto_run = True
        channel = ScriptedChannel(0)
        with patch("kstrl.feature_cmd.run_loop", _loop_results(0, 0)):
            code = run_feature(
                params, KstrlConfig(), StubAgent(), ui, tmp_path,
                interaction=channel,
            )
        assert code == 0
        assert channel.requests == []
        assert "skipping review gate" in stream.getvalue()


class TestExitCodes:
    def test_failed_understand_short_circuits(self, tmp_path: Path) -> None:
        ui, _ = _ui()
        with patch("kstrl.feature_cmd.run_loop", _loop_results(3)):
            code = run_feature(
                _params(tmp_path), KstrlConfig(), StubAgent(), ui, tmp_path,
                interaction=ScriptedChannel(0),
            )
        assert code == 3

    def test_empty_prd_skips_implementation(self, tmp_path: Path) -> None:
        ui, stream = _ui()
        with patch("kstrl.feature_cmd.run_loop", _loop_results(0)):
            code = run_feature(
                _params(tmp_path, stories=0), KstrlConfig(), StubAgent(),
                ui, tmp_path, interaction=ScriptedChannel(0),
            )
        assert code == 0
        assert "PRD has no user stories" in stream.getvalue()

    def test_repair_loop_recovers(self, tmp_path: Path) -> None:
        """understand ok, implement fails, first repair succeeds -> 0,
        and the repair PRD lands on disk."""
        ui, _ = _ui()
        params = _params(tmp_path, repair_max_runs=2)
        with (
            patch("kstrl.feature_cmd.run_loop", _loop_results(0, 1, 0)),
            patch("kstrl.feature_cmd.get_agent", return_value=StubAgent()),
        ):
            code = run_feature(
                params, KstrlConfig(), StubAgent(), ui, tmp_path,
                interaction=ScriptedChannel(0),
            )
        assert code == 0
        repairs = list((params.feature_dir / "repairs").glob("repair_*.json"))
        assert len(repairs) == 1
        assert (params.feature_dir / "repairs" / "latest.json").exists()

    def test_repairs_exhausted_returns_last_code(self, tmp_path: Path) -> None:
        ui, _ = _ui()
        params = _params(tmp_path, repair_max_runs=2)
        with (
            patch("kstrl.feature_cmd.run_loop", _loop_results(0, 1, 1, 4)),
            patch("kstrl.feature_cmd.get_agent", return_value=StubAgent()),
        ):
            code = run_feature(
                params, KstrlConfig(), StubAgent(), ui, tmp_path,
                interaction=ScriptedChannel(0),
            )
        assert code == 4
