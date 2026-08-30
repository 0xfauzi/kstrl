"""#276: what the engineer prompt says about the injected verify block.

``tests/test_verify_command_contract.py`` covers the harness side of
#261: one resolver, the gate shelling out to exactly what it returns,
and the resolved block reaching the engineer. This module covers the
other side of the same seam - what ``DEFAULT_PROMPT`` tells the engineer
to do with that block, and what it tells the engineer when no block was
injected at all.

Before #276 the answer was "derive your own": the harness handed the
agent an authoritative block naming the three commands the gate would
run, and step 9, one line below it, told the agent to work two of them
out for itself and never mentioned lint. `ruff check` blocks Phase 1, so
a lint error was found by the gate rather than by the agent. Measured
cost: one wasted engineer iteration.

Split from the contract module rather than appended: the two are
different jobs, and that file was at the 800-line ratchet.
"""

from __future__ import annotations

from pathlib import Path

from kstrl.contract import ContractConfig
from kstrl.init_cmd import DEFAULT_KSTRL_TOML, DEFAULT_PROMPT
from kstrl.verify import (
    DEFAULT_TEST_COMMAND,
    VERIFY_COMMANDS_PROMPT,
    VerifyConfig,
)
from tests.test_verify_command_contract import (
    _block_is_injected,
    _engineer_prompt,
    _prompt_from_cli,
)


def _step_nine(prompt_body: str) -> str:
    """The text of DEFAULT_PROMPT's step 9, up to step 10."""
    start = prompt_body.index("\n9. ")
    return prompt_body[start : prompt_body.index("\n10. ", start)]


class TestStepNineDefersToTheInjectedBlock:
    #: The literal DEFAULT_PROMPT points the engineer at.
    HEADING = "Verification Commands (resolved by kstrl)"

    def test_it_points_at_the_block(self) -> None:
        assert self.HEADING in _step_nine(DEFAULT_PROMPT)

    def test_the_heading_it_names_is_the_block_s_own(self) -> None:
        """The pointer is a literal, so a retitled block would leave the
        engineer looking for a section that does not exist. This
        assertion is the only thing holding the two strings together."""
        assert self.HEADING in VERIFY_COMMANDS_PROMPT

    def test_it_no_longer_tells_the_agent_to_derive_commands(self) -> None:
        assert "Find the project's fastest typecheck and tests" not in DEFAULT_PROMPT
        assert "derive your own" in _step_nine(DEFAULT_PROMPT)

    def test_lint_is_named(self) -> None:
        """The omission that cost the iteration."""
        assert "lint" in _step_nine(DEFAULT_PROMPT).lower()

    def test_it_forbids_substituting_a_variant(self) -> None:
        step_nine = _step_nine(DEFAULT_PROMPT)
        assert "narrower or broader" in step_nine
        assert "exactly as written" in step_nine

    def test_the_done_rule_is_stated_once(self) -> None:
        """Step 14 carried its own copy reading "only after
        tests/typecheck pass" - the two-of-three formulation whose lint
        omission is the premise of this change, sitting at the moment the
        agent flips ``passes`` to true. It now refers to step 9."""
        assert "tests/typecheck pass" not in DEFAULT_PROMPT
        assert "Do NOT mark the story as done until every command passes" in DEFAULT_PROMPT
        assert "only after step 9's commands pass" in DEFAULT_PROMPT

    def test_the_body_names_no_command_of_its_own(self) -> None:
        """It defers or it says nothing. DEFAULT_PROMPT is a static
        template scaffolded to disk, so any command literal in it is a
        second source of truth that cannot know what the gate resolved -
        which is the whole of #261 restated one layer up."""
        for tool in ("uv run", "pytest", "mypy", "ruff", "pyright"):
            assert tool not in DEFAULT_PROMPT

    def test_the_block_and_the_deferral_arrive_together(self, tmp_path: Path) -> None:
        """End to end: the fallback body and the injected block compose,
        so the pointer resolves in the prompt the agent is handed."""
        prompt = _engineer_prompt(tmp_path, VerifyConfig(), scaffold_prompt=False)
        assert _block_is_injected(prompt)
        assert self.HEADING in _step_nine(prompt)


class TestStepNineWhenNoBlockIsInjected:
    """``verify_config=None`` means no mechanical gate runs (#261), which
    is what `ks understand` and `ks feature` produce. Step 9 must not
    then send the agent after commands it was never given: an understand
    iteration whose allowed paths permit only the codebase map has
    nothing to gain from a suite run, and the old step 9 told it to go
    and find one anyway.
    """

    def test_the_fallback_clause_is_stated(self) -> None:
        step_nine = _step_nine(DEFAULT_PROMPT)
        assert "If that block is absent" in step_nine
        assert "no mechanical gate runs on this iteration" in step_nine

    def test_it_does_not_name_the_project_context_as_a_command_source(self) -> None:
        """On the no-gate path ``build_project_context`` injects CLAUDE.md
        UNSCRUBBED - it only drops stale verification bullets when it has
        resolved commands to compare them against - so a pre-#261
        project's `## Verification Commands` section is still in the
        prompt, and the block's own "ignore any other verification
        command list" sentence is not. The fallback must not point the
        agent at that list."""
        assert "project context" not in _step_nine(DEFAULT_PROMPT)

    def test_the_engineer_gets_the_fallback_and_no_block(self, tmp_path: Path) -> None:
        prompt = _engineer_prompt(tmp_path, scaffold_prompt=False)
        assert not _block_is_injected(prompt)
        assert "no mechanical gate runs on this iteration" in prompt

    def test_ks_understand_renders_this_body_and_gets_no_block(self, tmp_path: Path) -> None:
        """The path that actually reaches DEFAULT_PROMPT: no
        understand_prompt.md was scaffolded, so run_loop falls back to
        it. Asserted here rather than in the contract module because what
        is at stake is which step 9 that command ends up running."""
        prompt = _prompt_from_cli(["understand", "--root", str(tmp_path)])
        assert _step_nine(DEFAULT_PROMPT) in prompt
        assert not _block_is_injected(prompt)


class TestContractDefaultIsNotACopy:
    """#276 sweep: Phase 3 carried its own literal copy of the Phase 1
    default. Not a prompt edit, so no calibration policy attaches, but it
    is the duplicated-literal shape #261 removed from Phase 1.

    ``tests/test_contract.py::TestContractConfig`` covers the values.
    What it cannot see is whether they are one fact or two copies that
    happen to agree today, which is what these two pin.
    """

    def test_the_dataclass_default_is_the_phase_1_default(self) -> None:
        assert ContractConfig().test_command == DEFAULT_TEST_COMMAND

    def test_the_scaffolded_kstrl_toml_documents_that_same_default(self) -> None:
        """`ks init` writes the [contract] default into every user's
        kstrl.toml as a commented line. Nothing else pins it, so moving
        DEFAULT_TEST_COMMAND would leave every scaffolded file
        documenting the old value with a green suite."""
        assert f'# test_command = "{ContractConfig().test_command}"' in DEFAULT_KSTRL_TOML
