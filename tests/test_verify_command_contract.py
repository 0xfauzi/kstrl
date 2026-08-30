"""#261: one source of truth for the verification commands.

``ks init`` used to write three hardcoded verification commands into the
generated CLAUDE.md. ``loop.run_loop`` prepends CLAUDE.md into the
engineer prompt on every iteration and ``factory`` copies it into every
worktree, so those commands were instructions the agent followed. All
three disagreed with what the Phase 1 gate actually ran, from the moment
init finished and before the operator touched anything: the agent was
told to lint ``src/`` while the gate linted everything, so a lint error
introduced under ``tests/`` passed the agent's own check and failed the
gate.

The fix is structural rather than a corrected copy. The commands live in
``verify`` only; the gate and the engineer prompt both ask that module
what will run. These tests hold the two sides together.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import _run_component
from kstrl.init_cmd import _generate_claude_md
from kstrl.loop import COMPLETION_MARKER, LoopResult, build_project_context, run_loop
from kstrl.ui.plain import PlainUI
from kstrl.verify import (
    DEFAULT_LINT_COMMAND,
    DEFAULT_TEST_COMMAND,
    DEFAULT_TYPECHECK_COMMAND,
    SCOPED_TYPECHECK_COMMAND,
    ResolvedVerifyCommands,
    VerifyConfig,
    check_linter,
    check_test_suite,
    check_typecheck,
    resolve_verify_commands,
    scrub_stale_verify_commands,
)

# The chained command from the repo that found this bug: a Python
# backend plus a TypeScript frontend, which is the only way to gate a
# two-language repo and the case a single-language CLAUDE.md is silent
# about.
POLYGLOT_TEST = "uv run pytest -q && cd web && npm run test"
POLYGLOT_TYPECHECK = "uv run mypy && cd web && npm run check"

# KSTRL_VERIFY_* env overrides are cleared for every test by the
# autouse ``isolate_kstrl_state`` fixture in tests/conftest.py.


class _PromptCapturingAgent:
    """Records the prompt the engineer was actually handed."""

    name = "capture"
    final_message: str | None = None

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        self.prompts.append(prompt)
        yield COMPLETION_MARKER


def _project(root: Path) -> KstrlConfig:
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("STORY-PROMPT-BODY")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}')
    return KstrlConfig(
        max_iterations=1,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        kstrl_branch="",
        kstrl_branch_explicit=True,
    )


def _engineer_prompt(
    root: Path,
    verify_config: VerifyConfig | None = None,
    skip_verification: bool = False,
) -> str:
    config = _project(root)
    agent = _PromptCapturingAgent()
    result = run_loop(
        config,
        PlainUI(no_color=True),
        agent,  # type: ignore[arg-type]
        root,
        verify_config=verify_config,
        skip_verification=skip_verification,
    )
    assert result.completed is True
    assert agent.prompts
    return agent.prompts[0]


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class TestResolveVerifyCommands:
    def test_unset_config_reports_the_gate_defaults(self, tmp_path: Path) -> None:
        commands = resolve_verify_commands(VerifyConfig(), tmp_path)
        assert commands.test == DEFAULT_TEST_COMMAND
        assert commands.typecheck == DEFAULT_TYPECHECK_COMMAND
        assert commands.lint == DEFAULT_LINT_COMMAND

    def test_typecheck_default_defers_to_configured_mypy_scope(
        self,
        tmp_path: Path,
    ) -> None:
        """The Gap 2 rule, now reported to the agent as well as obeyed
        by the gate."""
        (tmp_path / "pyproject.toml").write_text('[tool.mypy]\nfiles = ["pkg"]\n')
        resolved = resolve_verify_commands(VerifyConfig(), tmp_path).typecheck
        assert resolved == SCOPED_TYPECHECK_COMMAND

    def test_configured_commands_win_verbatim(self, tmp_path: Path) -> None:
        commands = resolve_verify_commands(
            VerifyConfig(
                test_command="pytest -x",
                typecheck_command="pyright",
                lint_command="flake8 .",
            ),
            tmp_path,
        )
        assert (commands.test, commands.typecheck, commands.lint) == (
            "pytest -x",
            "pyright",
            "flake8 .",
        )

    def test_a_chained_polyglot_command_survives_unsplit(
        self,
        tmp_path: Path,
    ) -> None:
        """The resolver never parses or rewrites the operator's command,
        so a two-toolchain chain reaches the agent whole."""
        commands = resolve_verify_commands(
            VerifyConfig(
                test_command=POLYGLOT_TEST,
                typecheck_command=POLYGLOT_TYPECHECK,
            ),
            tmp_path,
        )
        assert commands.test == POLYGLOT_TEST
        assert commands.typecheck == POLYGLOT_TYPECHECK


class TestGateRunsWhatTheResolverReports:
    """The claim the whole fix rests on: the command the gate shells out
    to is character-for-character the one the resolver reports, so the
    prompt cannot name a different one."""

    def _executed(self, run: Any) -> str:
        assert run.call_count == 1
        return str(run.call_args.args[0])

    def test_test_gate(self, tmp_path: Path) -> None:
        expected = resolve_verify_commands(VerifyConfig(), tmp_path).test
        with patch("kstrl.verify.run_scrubbed") as run:
            run.return_value.returncode = 0
            check_test_suite(tmp_path)
        assert self._executed(run) == expected

    def test_typecheck_gate_with_configured_mypy_scope(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.mypy]\npackages = ["pkg"]\n')
        expected = resolve_verify_commands(VerifyConfig(), tmp_path).typecheck
        with patch("kstrl.verify.run_scrubbed") as run:
            run.return_value.returncode = 0
            check_typecheck(tmp_path)
        assert self._executed(run) == expected

    def test_lint_gate(self, tmp_path: Path) -> None:
        expected = resolve_verify_commands(VerifyConfig(), tmp_path).lint
        with patch("kstrl.verify.run_scrubbed") as run:
            run.return_value.returncode = 0
            check_linter(tmp_path)
        assert self._executed(run) == expected

    def test_polyglot_chain_reaches_the_gate_whole(self, tmp_path: Path) -> None:
        config = VerifyConfig(test_command=POLYGLOT_TEST)
        expected = resolve_verify_commands(config, tmp_path).test
        with patch("kstrl.verify.run_scrubbed") as run:
            run.return_value.returncode = 0
            check_test_suite(tmp_path, config.test_command)
        assert self._executed(run) == expected == POLYGLOT_TEST


class TestPromptSection:
    def test_it_names_all_three_commands(self) -> None:
        section = ResolvedVerifyCommands(
            test="t-cmd",
            typecheck="tc-cmd",
            lint="l-cmd",
        ).format_for_prompt()
        assert "`t-cmd`" in section
        assert "`tc-cmd`" in section
        assert "`l-cmd`" in section

    def test_it_declares_itself_authoritative(self) -> None:
        section = ResolvedVerifyCommands(test="a", typecheck="b", lint="c").format_for_prompt()
        assert "authoritative" in section.lower()


# ---------------------------------------------------------------------------
# Migration: a CLAUDE.md scaffolded before this fix
# ---------------------------------------------------------------------------

_LEGACY_CLAUDE_MD = """# CLAUDE.md - legacy

## Project Overview
- **Language**: Python

## Verification Commands
- **Test**: `uv run pytest tests/ -v --tb=short`
- **Typecheck**: `uv run mypy src/ --strict`
- **Lint**: `uv run ruff check src/`

Note on scope: this prose explains something a human wrote.

## Agent Learnings
- keep me
"""


class TestScrubStaleVerifyCommands:
    def _commands(self) -> ResolvedVerifyCommands:
        return ResolvedVerifyCommands(
            test=DEFAULT_TEST_COMMAND,
            typecheck=DEFAULT_TYPECHECK_COMMAND,
            lint=DEFAULT_LINT_COMMAND,
        )

    def test_every_diverging_bullet_is_dropped(self) -> None:
        scrubbed = scrub_stale_verify_commands(_LEGACY_CLAUDE_MD, self._commands())
        assert "uv run pytest tests/ -v --tb=short" not in scrubbed.text
        assert "uv run mypy src/ --strict" not in scrubbed.text
        assert "uv run ruff check src/" not in scrubbed.text
        assert len(scrubbed.divergences) == 3

    def test_the_warning_names_both_sides(self) -> None:
        scrubbed = scrub_stale_verify_commands(_LEGACY_CLAUDE_MD, self._commands())
        lint_warning = next(w for w in scrubbed.divergences if "lint" in w)
        assert "uv run ruff check src/" in lint_warning
        assert DEFAULT_LINT_COMMAND in lint_warning

    def test_surrounding_prose_and_headings_survive(self) -> None:
        scrubbed = scrub_stale_verify_commands(_LEGACY_CLAUDE_MD, self._commands())
        assert "## Verification Commands" in scrubbed.text
        assert "Note on scope: this prose explains something a human wrote." in scrubbed.text
        # proposals.py anchors on this heading and errors without it.
        assert "## Agent Learnings" in scrubbed.text
        assert "- keep me" in scrubbed.text

    def test_a_bullet_that_already_agrees_is_kept(self) -> None:
        text = f"## Verification Commands\n- **Lint**: `{DEFAULT_LINT_COMMAND}`\n"
        scrubbed = scrub_stale_verify_commands(text, self._commands())
        assert scrubbed.text == text
        assert scrubbed.divergences == []

    def test_a_file_with_no_command_bullets_is_returned_byte_identical(self) -> None:
        text = "# CLAUDE.md\n\nSome prose.\n- **Test**: not in backticks\n"
        scrubbed = scrub_stale_verify_commands(text, self._commands())
        assert scrubbed.text == text
        assert scrubbed.divergences == []

    def test_the_trailing_newline_is_preserved(self) -> None:
        assert scrub_stale_verify_commands(
            _LEGACY_CLAUDE_MD,
            self._commands(),
        ).text.endswith("\n")

    def test_a_star_bullet_marker_is_matched_too(self) -> None:
        scrubbed = scrub_stale_verify_commands(
            "* **Test**: `pytest tests/`\n",
            self._commands(),
        )
        assert scrubbed.divergences
        assert "pytest tests/" not in scrubbed.text

    def test_crlf_endings_round_trip(self) -> None:
        """The kept lines are re-joined with their own endings, so a
        Windows-authored CLAUDE.md is not silently rewritten to LF."""
        text = "# Title\r\n- **Test**: `pytest tests/`\r\n\r\nprose\r\n"
        scrubbed = scrub_stale_verify_commands(text, self._commands())
        assert scrubbed.divergences
        assert scrubbed.text == "# Title\r\n\r\nprose\r\n"

    def test_a_file_with_no_trailing_newline_gains_none(self) -> None:
        scrubbed = scrub_stale_verify_commands(
            "prose\n- **Lint**: `ruff check src/`",
            self._commands(),
        )
        assert scrubbed.text == "prose\n"


# ---------------------------------------------------------------------------
# ks init no longer writes a second copy
# ---------------------------------------------------------------------------


class TestGeneratedClaudeMd:
    def _generated(self) -> str:
        return _generate_claude_md(
            {"name": "demo", "language": "Python", "framework": "FastAPI"},
        )

    def test_it_states_no_verification_command(self) -> None:
        generated = self._generated()
        for label in ("**Test**", "**Typecheck**", "**Lint**"):
            assert label not in generated

    def test_none_of_the_three_wrong_literals_survive(self) -> None:
        generated = self._generated()
        for literal in (
            "pytest tests/ -v --tb=short",
            "mypy src/ --strict",
            "ruff check src/",
        ):
            assert literal not in generated

    def test_it_still_tells_a_human_where_verification_lives(self) -> None:
        generated = self._generated()
        assert "## Verification" in generated
        assert "[verify]" in generated
        assert "test_command" in generated

    def test_the_proposals_anchor_heading_is_untouched(self) -> None:
        """proposals.py:194 errors when this literal heading is absent."""
        assert "## Agent Learnings" in self._generated()

    def test_a_scrub_of_a_freshly_generated_file_finds_nothing_to_do(
        self,
        tmp_path: Path,
    ) -> None:
        """The end state: init's own output cannot diverge, because it
        states nothing that could."""
        scrubbed = scrub_stale_verify_commands(
            self._generated(),
            resolve_verify_commands(VerifyConfig(), tmp_path),
        )
        assert scrubbed.divergences == []


# ---------------------------------------------------------------------------
# What the engineer is actually handed
# ---------------------------------------------------------------------------


class TestEngineerPromptCarriesTheGateCommands:
    def test_the_block_is_injected_without_a_claude_md(self, tmp_path: Path) -> None:
        prompt = _engineer_prompt(tmp_path)
        assert DEFAULT_TEST_COMMAND in prompt
        assert DEFAULT_LINT_COMMAND in prompt
        assert "STORY-PROMPT-BODY" in prompt

    def test_configured_commands_reach_the_agent(self, tmp_path: Path) -> None:
        prompt = _engineer_prompt(
            tmp_path,
            VerifyConfig(test_command="pytest -q", lint_command="ruff check kstrl/"),
        )
        assert "pytest -q" in prompt
        assert "ruff check kstrl/" in prompt

    def test_the_polyglot_chain_reaches_the_agent_in_full(self, tmp_path: Path) -> None:
        """The bug was found on a repo whose frontend gates the agent
        was never told about. Both halves must arrive."""
        prompt = _engineer_prompt(
            tmp_path,
            VerifyConfig(
                test_command=POLYGLOT_TEST,
                typecheck_command=POLYGLOT_TYPECHECK,
            ),
        )
        assert POLYGLOT_TEST in prompt
        assert POLYGLOT_TYPECHECK in prompt
        assert "npm run test" in prompt
        assert "npm run check" in prompt

    def test_kstrl_toml_is_read_when_no_config_is_passed(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            f'[verify]\ntest_command = "{POLYGLOT_TEST}"\n',
        )
        assert POLYGLOT_TEST in _engineer_prompt(tmp_path)

    def test_a_legacy_claude_md_cannot_contradict_the_gate(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text(_LEGACY_CLAUDE_MD)
        prompt = _engineer_prompt(tmp_path)
        # The stale instructions are gone from what the agent reads...
        assert "ruff check src/" not in prompt
        assert "mypy src/ --strict" not in prompt
        # ...the rest of the project context is not,...
        assert "## Agent Learnings" in prompt
        assert "- keep me" in prompt
        # ...and the truth is there instead.
        assert DEFAULT_LINT_COMMAND in prompt

    def test_the_file_on_disk_is_never_rewritten(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(_LEGACY_CLAUDE_MD)
        _engineer_prompt(tmp_path)
        assert claude_md.read_text() == _LEGACY_CLAUDE_MD

    def test_each_divergence_is_reported_to_the_operator(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "CLAUDE.md").write_text(_LEGACY_CLAUDE_MD)
        _engineer_prompt(tmp_path)
        # PlainUI writes to stderr.
        warnings = capsys.readouterr().err
        assert "uv run ruff check src/" in warnings
        assert "kstrl.toml" in warnings

    def test_a_correct_claude_md_is_left_alone(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "CLAUDE.md").write_text(
            f"## Verification Commands\n- **Lint**: `{DEFAULT_LINT_COMMAND}`\n",
        )
        prompt = _engineer_prompt(tmp_path)
        assert f"- **Lint**: `{DEFAULT_LINT_COMMAND}`" in prompt
        assert "Dropping the stale line" not in capsys.readouterr().err


class TestBuildProjectContext:
    def test_it_reads_the_projects_own_config_when_given_none(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[verify]\nlint_command = "eslint ."\n')
        context = build_project_context(tmp_path, PlainUI(no_color=True))
        assert "eslint ." in context

    def test_an_explicit_config_beats_the_projects_file(self, tmp_path: Path) -> None:
        """A CLI --lint-command or an uncommitted edit lives only in the
        parent, so the parent's answer has to win."""
        (tmp_path / "kstrl.toml").write_text('[verify]\nlint_command = "eslint ."\n')
        context = build_project_context(
            tmp_path,
            PlainUI(no_color=True),
            VerifyConfig(lint_command="ruff check --preview ."),
        )
        assert "ruff check --preview ." in context
        assert "eslint ." not in context

    def test_skip_verification_states_no_commands_at_all(self, tmp_path: Path) -> None:
        """--no-verify means no gate runs, so claiming one would is the
        same species of untruth this issue is about."""
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md\n\nprose\n")
        context = build_project_context(
            tmp_path,
            PlainUI(no_color=True),
            VerifyConfig(),
            skip_verification=True,
        )
        assert "prose" in context
        assert DEFAULT_TEST_COMMAND not in context
        assert "Verification Commands (resolved by kstrl)" not in context

    def test_skip_verification_with_no_claude_md_yields_no_context(
        self,
        tmp_path: Path,
    ) -> None:
        assert (
            build_project_context(
                tmp_path,
                PlainUI(no_color=True),
                skip_verification=True,
            )
            == ""
        )

    def test_the_loop_omits_the_separator_when_there_is_no_context(
        self,
        tmp_path: Path,
    ) -> None:
        prompt = _engineer_prompt(tmp_path, skip_verification=True)
        assert prompt == "STORY-PROMPT-BODY"


class TestFactoryForwardsItsResolvedConfig:
    """The seam that makes the parent's answer reach the worker.

    Without it the worker falls back to the worktree's own kstrl.toml,
    which cannot see a CLI override, an uncommitted edit, or the
    parent's own ``or VerifyConfig()`` fallback - so the agent and the
    gate could disagree again.
    """

    def _forwarded(self, root: Path, **kwargs: Any) -> dict[str, Any]:
        _project(root)
        (root / "kstrl.toml").write_text("[knowledge]\nenabled = false\n")
        seen: list[dict[str, Any]] = []

        def fake_run_loop(*args: Any, **kw: Any) -> LoopResult:
            seen.append(kw)
            return LoopResult(completed=True, iterations=1, exit_code=0)

        with patch("kstrl.loop.run_loop", side_effect=fake_run_loop):
            _run_component(
                component_id="comp-a",
                prd_path_str="scripts/kstrl/prd.json",
                worktree_path_str=str(root),
                root_dir_str=str(root),
                prompt_file_str="scripts/kstrl/prompt.md",
                agent_cmd="echo test",
                model=None,
                reasoning=None,
                agent_type=None,
                sleep_seconds=0.0,
                redirect_output=False,
                **kwargs,
            )
        assert len(seen) == 1
        return seen[0]

    def test_the_config_reaches_the_loop(self, tmp_path: Path) -> None:
        passed = VerifyConfig(test_command=POLYGLOT_TEST)
        assert self._forwarded(tmp_path, verify_config=passed)["verify_config"] is passed

    def test_the_skip_flag_reaches_the_loop(self, tmp_path: Path) -> None:
        assert self._forwarded(tmp_path, skip_verification=True)["skip_verification"] is True

    def test_both_default_off(self, tmp_path: Path) -> None:
        forwarded = self._forwarded(tmp_path)
        assert forwarded["verify_config"] is None
        assert forwarded["skip_verification"] is False
