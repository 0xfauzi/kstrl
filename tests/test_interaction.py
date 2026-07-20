"""Stage 3 PR A (TUI rewrite): the interaction seam.

Covers the channel primitives (Ui + Queue), the E6 checkpoint context,
and the evolve-apply fix (the one prompt that previously crashed with
click.Abort on non-TTY EOF).
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

from kstrl.interaction import (
    CheckpointContext,
    PromptKind,
    PromptRequest,
    PromptResponse,
    QueueInteractionChannel,
    UiInteractionChannel,
)
from kstrl.ui.plain import PlainUI


def _req(default: int = 0) -> PromptRequest:
    return PromptRequest(
        kind=PromptKind.CONFIRM,
        header="Proceed?",
        options=("Yes", "No"),
        default=default,
    )


class TestUiInteractionChannel:
    def test_non_tty_returns_default_unanswered(self) -> None:
        channel = UiInteractionChannel(PlainUI(no_color=True, file=io.StringIO()))
        # pytest's stdin is not a tty -> can_prompt False.
        assert channel.can_prompt() is False
        response = channel.request(_req(default=1))
        assert response == PromptResponse(
            request_id=response.request_id, choice=1, answered=False,
        )

    def test_delegates_to_ui_choose(self) -> None:
        class FakeUI(PlainUI):
            def can_prompt(self) -> bool:
                return True

            def choose(self, header: str, options: list[str],
                       default: int = 0) -> int:
                return 1

        channel = UiInteractionChannel(FakeUI(no_color=True, file=io.StringIO()))
        response = channel.request(_req())
        assert response.answered is True
        assert response.choice == 1

    def test_invalid_ui_choice_degrades_to_default(self) -> None:
        class InvalidUI(PlainUI):
            def can_prompt(self) -> bool:
                return True

            def choose(self, header: str, options: list[str],
                       default: int = 0) -> int:
                return len(options)

        channel = UiInteractionChannel(
            InvalidUI(no_color=True, file=io.StringIO()),
        )
        response = channel.request(_req(default=1))
        assert response.answered is False
        assert response.choice == 1


class TestQueueInteractionChannel:
    def test_detached_degrades_to_default(self) -> None:
        channel = QueueInteractionChannel()
        assert channel.can_prompt() is False
        response = channel.request(_req(default=1))
        assert response.answered is False
        assert response.choice == 1

    def test_request_resolve_round_trip_across_threads(self) -> None:
        channel = QueueInteractionChannel()
        seen: list[PromptRequest] = []
        channel.attach(seen.append)
        results: list[PromptResponse] = []

        def requester() -> None:
            results.append(channel.request(_req()))

        thread = threading.Thread(target=requester)
        thread.start()
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.005)
        assert seen, "resolver never notified"
        assert channel.resolve(seen[0].request_id, 1) is True
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert results[0].answered is True
        assert results[0].choice == 1

    def test_double_resolve_rejected(self) -> None:
        channel = QueueInteractionChannel()
        seen: list[PromptRequest] = []
        channel.attach(seen.append)
        thread = threading.Thread(target=lambda: channel.request(_req()))
        thread.start()
        while not seen:
            time.sleep(0.005)
        assert channel.resolve(seen[0].request_id, 0) is True
        assert channel.resolve(seen[0].request_id, 1) is False
        thread.join(timeout=2)

    def test_unknown_request_id_rejected(self) -> None:
        channel = QueueInteractionChannel()
        assert channel.resolve("nope", 0) is False

    def test_out_of_range_choice_rejected_without_releasing_waiter(self) -> None:
        channel = QueueInteractionChannel()
        seen: list[PromptRequest] = []
        channel.attach(seen.append)
        results: list[PromptResponse] = []
        thread = threading.Thread(
            target=lambda: results.append(channel.request(_req())),
        )
        thread.start()
        while not seen:
            time.sleep(0.005)
        assert channel.resolve(seen[0].request_id, 2) is False
        assert thread.is_alive()
        assert channel.resolve(seen[0].request_id, 1) is True
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert results[0].choice == 1

    def test_cancel_all_releases_waiters_with_defaults(self) -> None:
        channel = QueueInteractionChannel()
        channel.attach(lambda req: None)  # resolver that never answers
        results: list[PromptResponse] = []
        thread = threading.Thread(
            target=lambda: results.append(channel.request(_req(default=1))),
        )
        thread.start()
        time.sleep(0.02)
        channel.cancel_all()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert results[0].answered is False
        assert results[0].choice == 1

    def test_detach_releases_and_degrades(self) -> None:
        channel = QueueInteractionChannel()
        channel.attach(lambda req: None)
        results: list[PromptResponse] = []
        thread = threading.Thread(
            target=lambda: results.append(channel.request(_req())),
        )
        thread.start()
        time.sleep(0.02)
        channel.detach()
        thread.join(timeout=2)
        assert results[0].answered is False
        # After detach, new requests degrade immediately (no hang).
        response = channel.request(_req(default=1))
        assert response.answered is False

    def test_dying_notifier_never_hangs(self) -> None:
        channel = QueueInteractionChannel()

        def boom(req: PromptRequest) -> None:
            raise RuntimeError("UI died")

        channel.attach(boom)
        response = channel.request(_req(default=1))
        assert response.answered is False
        assert response.choice == 1


class TestCheckpointContext:
    def test_pipeline_builds_full_context(self, tmp_path: Path) -> None:
        """The E6 request carries diff/findings/usage - not just the
        review summary string (verified through a recording channel)."""
        from unittest.mock import patch

        from kstrl.factory import ComponentResult, run_factory
        from tests.test_event_stream import (
            _component,
            _factory_config,
            _make_base_config,
            _make_manifest,
            _setup_project,
            _usage,
        )

        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(
            root, create_prs=True, pause_before_pr_merge=True,
        )
        result = ComponentResult(
            "comp-a", success=True, iterations=1, usage=_usage(1234),
        )

        requests: list[PromptRequest] = []

        class Recorder:
            def can_prompt(self) -> bool:
                return True

            def request(self, req: PromptRequest) -> PromptResponse:
                requests.append(req)
                return PromptResponse(
                    request_id=req.request_id, choice=0, answered=True,
                )

        with patch(
            "kstrl.factory._run_component", return_value=result,
        ), patch(
            "kstrl.git.get_diff_content", return_value="+real diff\n",
        ), patch("kstrl.pr.is_gh_available", return_value=False):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root,
                interaction=Recorder(),
            )

        assert len(requests) == 1
        req = requests[0]
        assert req.kind == PromptKind.CHECKPOINT
        assert req.component_id == "comp-a"
        ctx = req.checkpoint
        assert isinstance(ctx, CheckpointContext)
        assert "+real diff" in ctx.diff_excerpt
        assert ctx.usage is not None
        assert ctx.usage.total_tokens == 1234
        assert ctx.branch == "kstrl/factory/comp-a"


class TestEvolveApplyNonTty:
    def test_no_click_abort_on_non_tty(self, tmp_path: Path) -> None:
        """The old raw click.confirm crashed with click.Abort on EOF;
        the seam degrades to a clean "not applied" skip."""
        from click.testing import CliRunner

        from kstrl.cli import cli
        from kstrl.evolution import (
            EvolutionConfig,
            EvolutionJournal,
            FailurePattern,
        )

        (tmp_path / "CLAUDE.md").write_text(
            "# X\n\n## Agent Learnings\n\n### Conventions\n",
        )
        journal = EvolutionJournal(EvolutionConfig())
        proposals = journal.propose_improvements([FailurePattern(
            description="linter failure 'S608' in 2/4 components",
            frequency=2, total_components=4,
            affected_components=["a", "b"], check_name="linter",
            error_signature="S608", category="verification",
        )])
        journal.save_proposals(proposals, tmp_path / ".kstrl" / "proposals")

        # No input= at all: stdin is at EOF, which used to raise
        # click.Abort out of the raw click.confirm.
        result = CliRunner().invoke(cli, [
            "evolve", "--apply", "PROP-001", "--root", str(tmp_path),
            "--ui", "plain", "--no-color",
        ])
        assert result.exit_code == 0, result.output
        assert "not applied (declined)" in result.output
        assert "S608" not in (tmp_path / "CLAUDE.md").read_text()
