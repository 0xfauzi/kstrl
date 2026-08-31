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

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, _run_component, _warn_claude_md_divergence
from kstrl.loop import COMPLETION_MARKER, LoopResult, build_project_context, run_loop
from kstrl.ui.plain import PlainUI
from kstrl.verify import (
    DEFAULT_LINT_COMMAND,
    DEFAULT_TEST_COMMAND,
    DEFAULT_TYPECHECK_COMMAND,
    SCOPED_TYPECHECK_COMMAND,
    VERIFY_COMMANDS_PROMPT,
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
    usage_records: list[Any] = []

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


def _project(root: Path, *, scaffold_prompt: bool = True) -> KstrlConfig:
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    if scaffold_prompt:
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


def _run_cli(args: list[str]) -> _PromptCapturingAgent:
    """Invoke a real CLI entry point with the agent replaced.

    Returns the agent rather than its prompts, because a caller that
    stubs ``run_loop`` is asserting on the call and gets no prompt at
    all.
    """
    agent = _PromptCapturingAgent()
    runner = CliRunner()
    with (
        patch("kstrl.cli.get_agent", return_value=agent),
        patch("kstrl.feature_cmd.get_agent", return_value=agent),
        patch("kstrl.cli._check_agent_preflight"),
    ):
        runner.invoke(cli, args, catch_exceptions=False)
    return agent


def _prompts_from_cli(args: list[str]) -> list[str]:
    """Every prompt a real CLI entry point hands an agent, in order.

    ``run_loop`` runs unpatched, because that is where the block is
    built. Only the agent is replaced, and it is handed the finished
    prompt, so what is asserted is what the command actually produces.

    A multi-phase command (`ks feature` runs understand, then implement,
    then repair) yields one entry per agent.run call, so a caller that
    cares about a specific phase has to pick it rather than assume the
    first.
    """
    prompts = _run_cli(args).prompts
    assert prompts, "no engineer prompt was built"
    return prompts


def _prompt_from_cli(args: list[str]) -> str:
    """The FIRST prompt a real CLI entry point hands an agent."""
    return _prompts_from_cli(args)[0]


def _feature_cli_args(root: Path, *, auto_run: bool = False) -> list[str]:
    """argv for `ks feature` against the tree ``_write_feature_prd`` built.

    ``auto_run`` adds ``--implementation-auto-run``, without which the
    command halts at the interactive review checkpoint after the
    understand phase and never builds an implement prompt.
    """
    args = [
        "feature",
        "--root",
        str(root),
        "--prd",
        "scripts/kstrl/feature/demo/prd.json",
        "--understand-iterations",
        "1",
        "--branch",
        "",
    ]
    if auto_run:
        args.append("--implementation-auto-run")
    return args


def _write_noop_verify_toml(root: Path) -> None:
    """Point [verify] at three commands that succeed without executing.

    Shares its shape with tests.test_feature_cmd._write_fast_verify_toml;
    both leave an existing kstrl.toml alone so a test can write its own.
    The commands run through verify.run_scrubbed, which hands a string to
    /bin/sh, so this costs one shell fork and no exec. POSIX-only, like
    the rest of this suite.
    """
    config = root / "kstrl.toml"
    if config.exists():
        return
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        '[verify]\ntest_command = "exit 0"\n'
        'typecheck_command = "exit 0"\nlint_command = "exit 0"\n',
        encoding="utf-8",
    )


def _write_feature_prd(root: Path) -> None:
    feature_dir = root / "scripts" / "kstrl" / "feature" / "demo"
    feature_dir.mkdir(parents=True, exist_ok=True)
    # #288 review: `ks feature` now RUNS the [verify] commands after each
    # engineer loop, so a project with no kstrl.toml resolves the
    # DEFAULTS and these tests really spawn `uv run pytest` /
    # `uv run mypy .` / `uv run ruff check .` from inside pytest, in a
    # temp dir with no pyproject for uv to resolve against. Measured:
    # 0.32s for a CLI feature test against 0.01s for its siblings, with
    # "collected 0 items" in the captured output. On a cold runner each
    # command can block up to [verify] subprocess_timeout (300s).
    _write_noop_verify_toml(root)
    # ks feature refuses to start without one.
    (root / "scripts" / "kstrl" / "codebase_map.md").write_text("# map\n")
    (feature_dir / "prd.json").write_text(
        json.dumps(
            {
                "branchName": "feat/demo",
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Demo",
                        "acceptanceCriteria": ["AC"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            }
        )
    )


def _engineer_prompt(
    root: Path,
    verify_config: VerifyConfig | None = None,
    *,
    scaffold_prompt: bool = True,
) -> str:
    """The prompt the engineer was handed.

    ``verify_config=None`` is run_loop's own default and means "no gate
    runs", so it is what the no-verification entry points produce.

    ``scaffold_prompt=False`` leaves no prompt.md, so run_loop falls back
    to the harness DEFAULT_PROMPT. The stub body is right for the
    assembly tests here; the fallback is what an un-customised project
    actually runs, and is what tests/test_engineer_verify_instructions.py
    asserts against.
    """
    config = _project(root, scaffold_prompt=scaffold_prompt)
    agent = _PromptCapturingAgent()
    result = run_loop(
        config,
        PlainUI(no_color=True),
        agent,  # type: ignore[arg-type]
        root,
        verify_config=verify_config,
    )
    assert result.completed is True
    assert agent.prompts
    return agent.prompts[0]


def _block_is_injected(prompt: str) -> bool:
    """Whether the resolved verification block itself reached the agent.

    Searching for the bare heading text no longer answers that: since
    #276 DEFAULT_PROMPT names the same string to point the engineer at
    the block. As a markdown heading - line-initial, with its ``# `` -
    it is only ever the block.
    """
    return f"\n{VERIFY_COMMANDS_PROMPT.splitlines()[0]}\n" in f"\n{prompt}"


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
# ---------------------------------------------------------------------------
# What the engineer is actually handed
# ---------------------------------------------------------------------------


class TestEngineerPromptCarriesTheGateCommands:
    def test_the_block_is_injected_without_a_claude_md(self, tmp_path: Path) -> None:
        prompt = _engineer_prompt(tmp_path, VerifyConfig())
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

    def test_a_legacy_claude_md_cannot_contradict_the_gate(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text(_LEGACY_CLAUDE_MD)
        prompt = _engineer_prompt(tmp_path, VerifyConfig())
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
        _engineer_prompt(tmp_path, VerifyConfig())
        assert claude_md.read_text() == _LEGACY_CLAUDE_MD

    def test_each_divergence_is_reported_to_the_operator(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "CLAUDE.md").write_text(_LEGACY_CLAUDE_MD)
        _engineer_prompt(tmp_path, VerifyConfig())
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
        prompt = _engineer_prompt(tmp_path, VerifyConfig())
        assert f"- **Lint**: `{DEFAULT_LINT_COMMAND}`" in prompt
        assert "Dropping the stale line" not in capsys.readouterr().err


class TestBuildProjectContext:
    def test_the_caller_config_is_the_only_config_consulted(
        self,
        tmp_path: Path,
    ) -> None:
        """It never re-reads kstrl.toml. A CLI --lint-command or an
        uncommitted edit lives only in the parent, and the parent's
        fallback is VerifyConfig() rather than a reload, so re-reading
        here would state a command the gate will not run."""
        (tmp_path / "kstrl.toml").write_text('[verify]\nlint_command = "eslint ."\n')
        context = build_project_context(
            tmp_path,
            PlainUI(no_color=True),
            VerifyConfig(lint_command="ruff check --preview ."),
        )
        assert "ruff check --preview ." in context
        assert "eslint ." not in context

    def test_no_gate_means_no_commands_are_stated(self, tmp_path: Path) -> None:
        """None is the default and means no mechanical gate runs, so
        claiming one would is the same species of untruth this issue is
        about. The CLAUDE.md context still goes through."""
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md\n\nprose\n")
        context = build_project_context(tmp_path, PlainUI(no_color=True))
        assert "prose" in context
        assert DEFAULT_TEST_COMMAND not in context
        assert "Verification Commands (resolved by kstrl)" not in context

    def test_no_gate_and_no_claude_md_yields_no_context(self, tmp_path: Path) -> None:
        assert build_project_context(tmp_path, PlainUI(no_color=True)) == ""

    def test_the_loop_omits_the_separator_when_there_is_no_context(
        self,
        tmp_path: Path,
    ) -> None:
        assert _engineer_prompt(tmp_path) == "STORY-PROMPT-BODY"

    def test_a_stale_claude_md_is_not_scrubbed_when_no_gate_runs(
        self,
        tmp_path: Path,
    ) -> None:
        """Nothing to reconcile against: with no gate there is no
        resolved command that the file could contradict."""
        (tmp_path / "CLAUDE.md").write_text(_LEGACY_CLAUDE_MD)
        context = build_project_context(tmp_path, PlainUI(no_color=True))
        assert "uv run ruff check src/" in context


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

    def test_it_defaults_to_no_gate(self, tmp_path: Path) -> None:
        """A caller that names no gate gets the fail-safe: silence, not
        a claim about commands nothing will run."""
        assert self._forwarded(tmp_path)["verify_config"] is None


class TestEngineerVerifyConfigHelper:
    """One resolver for "what does Phase 1 run with", used by the gate
    (``pipeline._phase_verify``) and by the engineer submit, so the two
    cannot answer it differently (#261).

    Methods on FactoryConfig rather than free functions taking the two
    fields: the coupling is the point, and unpacking them at each call
    site made ``(cfg.verify_config, False)`` a legal miscall."""

    def test_none_resolves_to_bare_defaults_not_a_disk_reload(self) -> None:
        """``verify_config=None`` has always meant "use the defaults"
        on FactoryConfig. Re-reading kstrl.toml here instead would make
        the prompt state a command the gate will not run."""
        assert FactoryConfig().resolved_verify_config() == VerifyConfig()

    def test_an_explicit_config_passes_through_untouched(self) -> None:
        config = VerifyConfig(test_command=POLYGLOT_TEST)
        assert FactoryConfig(verify_config=config).resolved_verify_config() is config

    def test_the_engineer_is_told_nothing_when_phase_1_is_off(self) -> None:
        config = FactoryConfig(verify_config=VerifyConfig(), skip_verification=True)
        assert config.engineer_verify_config() is None

    def test_otherwise_the_engineer_gets_what_the_gate_gets(self) -> None:
        config = VerifyConfig(lint_command="ruff check kstrl/")
        assert FactoryConfig(verify_config=config).engineer_verify_config() is config

    def test_the_pipeline_and_the_engineer_agree_on_the_default(self) -> None:
        """The exact divergence this pairing exists to prevent."""
        config = FactoryConfig()
        assert config.engineer_verify_config() == config.resolved_verify_config()


class TestNoVerificationEntryPoints:
    """`ks understand` and `ks feature` run no mechanical verification at
    all, so the engineer must not be told a gate will check its work.

    Before the fail-safe default, an `ks understand` iteration whose
    allowed paths permit only the codebase map was instructed to run the
    whole test suite plus mypy plus ruff on every pass.
    """

    def test_run_loops_default_states_no_commands(self, tmp_path: Path) -> None:
        """Every call site that does not name a gate inherits this."""
        prompt = _engineer_prompt(tmp_path)
        assert DEFAULT_TEST_COMMAND not in prompt
        assert DEFAULT_LINT_COMMAND not in prompt

    def test_ks_understand_states_no_commands(self, tmp_path: Path) -> None:
        """Driven through the real CLI. Its allowed paths permit only
        the codebase map, so instructing it to run the suite is minutes
        and tokens spent on a claim that is false for that command."""
        prompt = _prompt_from_cli(["understand", "--root", str(tmp_path)])
        assert DEFAULT_TEST_COMMAND not in prompt
        assert DEFAULT_LINT_COMMAND not in prompt
        assert not _block_is_injected(prompt)

    def test_ks_feature_states_no_commands(self, tmp_path: Path) -> None:
        _write_feature_prd(tmp_path)
        prompt = _prompt_from_cli(_feature_cli_args(tmp_path))
        assert DEFAULT_TEST_COMMAND not in prompt
        assert not _block_is_injected(prompt)


class TestParentReportsDivergence:
    """The migration warning has to reach the terminal the operator is
    watching. The worker's copy goes to that component's engineer.jsonl,
    and in pool mode nothing mirrors it to the parent.
    """

    def _warn(self, root: Path, config: FactoryConfig | None = None) -> str:
        out = io.StringIO()
        _warn_claude_md_divergence(
            root,
            config or FactoryConfig(),
            PlainUI(no_color=True, file=out),
        )
        return out.getvalue()

    def test_it_names_both_sides_before_any_spend(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text(_LEGACY_CLAUDE_MD)
        out = self._warn(tmp_path)
        assert "uv run ruff check src/" in out
        assert DEFAULT_LINT_COMMAND in out

    def test_it_says_nothing_when_claude_md_agrees(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text(
            f"- **Lint**: `{DEFAULT_LINT_COMMAND}`\n",
        )
        assert self._warn(tmp_path) == ""

    def test_it_says_nothing_without_a_claude_md(self, tmp_path: Path) -> None:
        assert self._warn(tmp_path) == ""

    def test_it_says_nothing_when_phase_1_is_off(self, tmp_path: Path) -> None:
        """No gate, so there is no divergence to report."""
        (tmp_path / "CLAUDE.md").write_text(_LEGACY_CLAUDE_MD)
        config = FactoryConfig(verify_config=VerifyConfig(), skip_verification=True)
        assert self._warn(tmp_path, config) == ""
