"""#276: the engineer prompt the CLI hands out defers to the injected verify block.

``tests/test_verify_command_contract.py`` covers the harness side of
#261: one resolver, the gate shelling out to exactly what it returns,
and the resolved block reaching the engineer. This module covers what
the engineer is handed on each path: the block plus a step 9 that points
at it (`ks feature` implement), or no block and a step 9 that states the
floor (`ks understand`, `ks feature` understand).

Before #276 the answer was "derive your own": the harness handed the
agent an authoritative block naming the three commands the gate would
run, and step 9, one line below it, told the agent to work two of them
out for itself and never mentioned lint. `ruff check` blocks Phase 1, so
a lint error was found by the gate rather than by the agent. Measured
cost: one wasted engineer iteration.

The wording of step 9 itself (lint named, no variant substitution, the
`[verify]` repair routed to a report rather than an edit) is not pinned
here as substrings: any edit to ``DEFAULT_PROMPT`` fails the H3 hash
snapshot in ``tests/test_prompt_versions.py``, which is the one place a
prompt change is reviewed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl.feature_verify import resolve_feature_verify_config
from kstrl.init_cmd import DEFAULT_PROMPT
from kstrl.loop import LoopResult
from kstrl.verify import VerifyConfig, resolve_verify_commands
from tests.test_verify_command_contract import (
    _block_is_injected,
    _engineer_prompt,
    _feature_cli_args,
    _prompt_from_cli,
    _prompts_from_cli,
    _run_cli,
    _write_feature_prd,
)

#: The literal DEFAULT_PROMPT points the engineer at.
HEADING = "Verification Commands (resolved by kstrl)"


def _step_nine(prompt_body: str) -> str:
    """The text of DEFAULT_PROMPT's step 9, up to step 10."""
    start = prompt_body.index("\n9. ")
    return prompt_body[start : prompt_body.index("\n10. ", start)]


def _unwrapped(text: str) -> str:
    """``text`` with every run of whitespace collapsed to one space.

    The prompt body is hard-wrapped, so a sentence the agent reads as one
    phrase is not a contiguous substring of the source. Asserting against
    the wrapped form would make these tests fail on a reflow that changes
    nothing the agent sees.
    """
    return " ".join(text.split())


class TestTheEngineerPromptOnEachPath:
    def test_the_block_and_the_deferral_arrive_together(self, tmp_path: Path) -> None:
        """End to end: the fallback body and the injected block compose,
        so the pointer resolves in the prompt the agent is handed."""
        prompt = _engineer_prompt(tmp_path, VerifyConfig(), scaffold_prompt=False)
        assert _block_is_injected(prompt)
        assert HEADING in _step_nine(prompt)

    def test_the_engineer_gets_the_floor_and_no_block(self, tmp_path: Path) -> None:
        """``verify_config=None`` means no mechanical gate runs (#261), and
        the prompt says so rather than pointing at a block that is absent."""
        prompt = _engineer_prompt(tmp_path, scaffold_prompt=False)
        assert not _block_is_injected(prompt)
        assert "nothing will check this work mechanically" in _unwrapped(prompt)

    def test_ks_feature_names_the_gate_only_for_the_loops_it_gates(
        self,
        tmp_path: Path,
    ) -> None:
        """#288 at the seam rather than through the prompt text.

        The understand loop keeps ``None``, which still means "no
        mechanical gate runs" and is still TRUE there: no gate runs on an
        understand file. The implement loop gets the config the report
        will run with, which is what makes the injected block's claim
        true on that path. Asserted per phase rather than in aggregate,
        because "some phase names a gate" would pass on the wiring this
        replaced and on its exact inverse.
        """
        _write_feature_prd(tmp_path)
        seen: list[Any] = []

        def fake_run_loop(*args: Any, **kwargs: Any) -> LoopResult:
            seen.append(kwargs.get("verify_config"))
            return LoopResult(completed=True, iterations=1, exit_code=0)

        # feature_cmd binds the name at import (from kstrl.loop import
        # run_loop), so patching kstrl.loop.run_loop would miss it.
        with patch("kstrl.feature_cmd.run_loop", side_effect=fake_run_loop):
            _run_cli(_feature_cli_args(tmp_path, auto_run=True))
        assert len(seen) == 2, f"expected understand + implement, got {len(seen)}"
        assert seen[0] is None
        assert seen[1] is not None

    def test_ks_feature_splits_the_two_prompts_end_to_end(
        self,
        tmp_path: Path,
    ) -> None:
        """End to end through the real CLI, on BOTH prompts.

        The previous version took ``[0]`` only and pointed at a sibling
        for the implement half. That sibling calls ``_engineer_prompt``
        directly and never goes through `ks feature`, so nothing asserted
        that the prompt the implement loop actually receives carries the
        block: a wiring regression would have been caught only by an
        ``is not None`` on a kwarg (#288 review round 2 finding 10). That
        wiring is the half of #288 which is not the report.

        ``--implementation-auto-run`` is load-bearing: without it the
        command halts at the interactive review checkpoint and only the
        understand prompt is ever built.
        """
        _write_feature_prd(tmp_path)
        prompts = _prompts_from_cli(_feature_cli_args(tmp_path, auto_run=True))
        assert len(prompts) == 2, len(prompts)
        understand, implement = prompts

        # The understand loop gates nothing, so it states nothing and
        # gets the floor instead (#261).
        assert _step_nine(DEFAULT_PROMPT) in understand
        assert not _block_is_injected(understand)
        assert "run the project's own typecheck and tests yourself" in _unwrapped(understand)

        # The implement loop gets the block, carrying the RESOLVED
        # commands the report will then run: the same three strings, from
        # the same pinned config, so the prompt cannot name one command
        # while another produces the verdict.
        assert _block_is_injected(implement)
        commands = resolve_verify_commands(resolve_feature_verify_config(tmp_path), tmp_path)
        for command in (commands.test, commands.typecheck, commands.lint):
            assert command in implement, command

    def test_ks_understand_renders_this_body_and_gets_no_block(self, tmp_path: Path) -> None:
        """The other, narrower way in: no understand_prompt.md was
        scaffolded, so run_loop falls back to this body."""
        prompt = _prompt_from_cli(["understand", "--root", str(tmp_path)])
        assert _step_nine(DEFAULT_PROMPT) in prompt
        assert not _block_is_injected(prompt)
