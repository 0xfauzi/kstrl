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
from typing import Any
from unittest.mock import patch

from kstrl.contract import ContractConfig
from kstrl.feature_verify import resolve_feature_verify_config
from kstrl.init_cmd import DEFAULT_KSTRL_TOML, DEFAULT_PROMPT
from kstrl.loop import LoopResult
from kstrl.policy import ENFORCEMENT_MACHINERY_PATHS
from kstrl.verify import (
    DEFAULT_TEST_COMMAND,
    VERIFY_COMMANDS_PROMPT,
    VerifyConfig,
    resolve_verify_commands,
)
from tests.test_verify_command_contract import (
    _block_is_injected,
    _engineer_prompt,
    _feature_cli_args,
    _prompt_from_cli,
    _prompts_from_cli,
    _run_cli,
    _write_feature_prd,
)


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
        assert "Find the project's fastest typecheck and tests" not in _unwrapped(DEFAULT_PROMPT)
        assert "Do NOT derive your own" in _unwrapped(_step_nine(DEFAULT_PROMPT))

    def test_lint_is_named(self) -> None:
        """The omission that cost the iteration."""
        assert "lint" in _step_nine(DEFAULT_PROMPT).lower()

    def test_it_forbids_substituting_a_variant(self) -> None:
        step_nine = _unwrapped(_step_nine(DEFAULT_PROMPT))
        assert "substitute a narrower or broader variant" in step_nine
        assert "exactly as written" in step_nine

    def test_the_done_rule_is_stated_once(self) -> None:
        """Step 14 carried its own copy reading "only after
        tests/typecheck pass" - the two-of-three formulation whose lint
        omission is the premise of this change, sitting at the moment the
        agent flips ``passes`` to true. It now refers to step 9."""
        assert "tests/typecheck pass" not in DEFAULT_PROMPT
        unwrapped = _unwrapped(DEFAULT_PROMPT)
        assert "Do NOT mark the story as done until every command passes" in unwrapped
        assert "only after step 9 is green" in unwrapped

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
    """``verify_config=None`` means no mechanical gate runs (#261).

    Who reaches this branch decides how strong the instruction has to be,
    and #288 narrowed it. `ks feature`'s implement and repair loops used
    to land here - the one path that wrote production code with nothing
    checking it - and now name the checks that really run on their
    output. What is left is the two UNDERSTAND paths: `ks understand` on
    a project that was never init'd (`ks init` scaffolds a separate
    ``understand_prompt.md``, so an init'd one never falls back), and
    `ks feature`'s own understand loop when that file is missing. Neither
    writes production code.

    The floor stays a floor rather than a proportionality hint, because
    the other guards are vacuous over an empty command list and the
    v1.1.1 body carried an unconditional "do NOT mark the story as done
    unless typecheck AND tests pass". It stays scoped to marking a story
    done, so a map-only understand iteration owes nothing while any
    future caller that reaches this branch WITH stories to mark is still
    told to run the project's checks itself.
    """

    def test_the_fallback_states_a_floor_and_not_a_proportionality_hint(self) -> None:
        assert (
            "If that block is absent, nothing will check this work mechanically: "
            "run the project's own typecheck and tests yourself first"
            in _unwrapped(_step_nine(DEFAULT_PROMPT))
        )

    def test_the_floor_is_scoped_to_marking_a_story_done(self) -> None:
        """Not to every iteration. An `ks understand` fallback edits only
        the codebase map and marks no story, so it must not be told to
        run a suite; `ks feature` implement does mark stories, and is."""
        assert "Do NOT mark the story as done until every command passes." in _unwrapped(
            _step_nine(DEFAULT_PROMPT)
        )

    def test_it_does_not_name_the_project_context_as_a_command_source(self) -> None:
        """On the no-gate path ``build_project_context`` injects CLAUDE.md
        UNSCRUBBED - it only drops stale verification bullets when it has
        resolved commands to compare them against - so a pre-#261
        project's `## Verification Commands` section is still in the
        prompt, and the block's own "ignore any other verification
        command list" sentence is not. The fallback must not point the
        agent at that list."""
        assert "project context" not in _unwrapped(_step_nine(DEFAULT_PROMPT))

    def test_the_engineer_gets_the_floor_and_no_block(self, tmp_path: Path) -> None:
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


class TestStepNineNamesTheRightRepair:
    """#284 review: "configure the tooling" was the wrong default advice.

    With ``[verify]`` unset, ``resolve_verify_commands`` yields the Python
    defaults whatever the project is written in, so in a Node or Go repo
    the commands fail because they are the wrong commands, not because
    tooling is missing. Telling the agent to configure the tooling there
    means installing pytest and mypy into a repo that is not Python,
    while step 9 forbids the substitution that would be its only way out.

    The repair names `[verify]` but routes the agent to REPORT it rather
    than edit it, because `kstrl.toml` is in
    ``policy.ENFORCEMENT_MACHINERY_PATHS``.
    """

    def test_it_names_the_config_that_owns_the_command(self) -> None:
        step_nine = _unwrapped(_step_nine(DEFAULT_PROMPT))
        assert "wrong for this project's language" in step_nine
        assert "`[verify]` section of `kstrl.toml`" in step_nine

    def test_it_routes_the_agent_to_report_that_file_not_edit_it(self) -> None:
        """`kstrl.toml` is enforcement machinery: editing it is a
        non-overridable hard fail whenever the envelope is enabled,
        independent of paths_deny and of the autonomy level, precisely so
        an agent cannot widen its own permissions. An earlier draft of
        this clause told the agent to correct the file, which is an
        instruction to trip that halt."""
        assert "kstrl.toml" in ENFORCEMENT_MACHINERY_PATHS
        assert "in your progress entry rather than editing it" in _unwrapped(
            _step_nine(DEFAULT_PROMPT)
        )

    def test_it_no_longer_tells_the_agent_to_install_a_toolchain(self) -> None:
        assert "configure the tooling rather than swapping" not in _unwrapped(DEFAULT_PROMPT)


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
