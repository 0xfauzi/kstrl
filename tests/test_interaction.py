"""Stage 3 PR A (TUI rewrite): the interaction seam, through run_factory.

The E6 checkpoint request the factory hands its interaction channel
carries the diff excerpt, the usage and the branch, not just the review
summary string. A recording channel stands in for the UI so the request
the factory built is asserted as a whole.
"""

from __future__ import annotations

import io
from pathlib import Path

from kstrl.interaction import (
    CheckpointContext,
    PromptKind,
    PromptRequest,
    PromptResponse,
)
from kstrl.ui.plain import PlainUI


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
            root,
            create_prs=True,
            pause_before_pr_merge=True,
        )
        result = ComponentResult(
            "comp-a",
            success=True,
            iterations=1,
            usage=_usage(1234),
        )

        requests: list[PromptRequest] = []

        class Recorder:
            def can_prompt(self) -> bool:
                return True

            def request(self, req: PromptRequest) -> PromptResponse:
                requests.append(req)
                return PromptResponse(
                    request_id=req.request_id,
                    choice=0,
                    answered=True,
                )

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=result,
            ),
            patch(
                "kstrl.git.get_diff_content",
                return_value="+real diff\n",
            ),
            patch("kstrl.pr.is_gh_available", return_value=False),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()),
                root,
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
