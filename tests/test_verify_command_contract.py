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

The fix is structural rather than a corrected copy, and since the #696
flag day the one source is the confirmed ``[stack]``: the gate runs its
checks and the engineer prompt states them (``stack.STACK_PROMPT``). What
remains here reads the answer where it lands: the prompt ``run_loop``,
``ks understand`` and ``ks feature`` actually hand a capturing agent (the
stack block is injected after the project's CLAUDE.md, and a no-gate entry
point states no checks).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.config import KstrlConfig
from kstrl.iteration_prompt import build_project_context
from kstrl.loop import COMPLETION_MARKER, run_loop
from kstrl.stack import STACK_PROMPT
from kstrl.ui.plain import PlainUI
from kstrl.verify_model import VerifyConfig
from tests.helpers.stack_confirmation import in_process_stack, write_stack
from tests.test_feature_cmd import _write_fast_verify_toml

# The chained command from the repo that found this bug: a Python
# backend plus a TypeScript frontend, which is the only way to gate a
# two-language repo and the case a single-language CLAUDE.md is silent
# about.
POLYGLOT_TEST = "uv run pytest -q && cd web && npm run test"
POLYGLOT_TYPECHECK = "uv run mypy && cd web && npm run check"

#: A check command no harness default could produce, so finding it in a
#: prompt proves the stack put it there.
MARKED_TEST = "make test-marked-261"


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


def _write_feature_prd(root: Path) -> None:
    feature_dir = root / "scripts" / "kstrl" / "feature" / "demo"
    feature_dir.mkdir(parents=True, exist_ok=True)
    # #288 review: `ks feature` RUNS the checks after each engineer loop,
    # and since #696 it refuses with no confirmed [stack]: this writes a
    # confirmed one of no-op checks.
    _write_fast_verify_toml(root)
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
    drives through `ks feature` and `ks understand`.
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
    """Whether the ``[stack]`` block itself reached the agent: its heading,
    line-initial with its ``# ``, which DEFAULT_PROMPT never writes."""
    return f"\n{STACK_PROMPT.splitlines()[0]}\n" in f"\n{prompt}"


_LEGACY_CLAUDE_MD = """# CLAUDE.md - legacy

## Project Overview
- **Language**: Python

## Verification Commands
- **Test**: `uv run pytest tests/ -v --tb=short`
- **Lint**: `uv run ruff check src/`

## Agent Learnings
- keep me
"""


class TestEngineerPromptCarriesTheGateCommands:
    def test_configured_commands_reach_the_agent(self, tmp_path: Path) -> None:
        prompt = _engineer_prompt(
            tmp_path,
            VerifyConfig(
                project_stack=in_process_stack({"tests": MARKED_TEST, "lint": "ruff check kstrl/"})
            ),
        )
        assert _block_is_injected(prompt)
        assert MARKED_TEST in prompt
        assert "ruff check kstrl/" in prompt
        assert "STORY-PROMPT-BODY" in prompt

    def test_the_polyglot_chain_reaches_the_agent_in_full(self, tmp_path: Path) -> None:
        """The bug was found on a repo whose frontend gates the agent
        was never told about. Both halves must arrive."""
        prompt = _engineer_prompt(
            tmp_path,
            VerifyConfig(
                project_stack=in_process_stack(
                    {"tests": POLYGLOT_TEST, "typecheck": POLYGLOT_TYPECHECK}
                )
            ),
        )
        assert POLYGLOT_TEST in prompt
        assert POLYGLOT_TYPECHECK in prompt

    def test_the_stack_block_follows_the_project_claude_md_unchanged(self, tmp_path: Path) -> None:
        """CLAUDE.md is the operator's and reaches the agent as written; the
        stack block comes after it and says it binds over any list above."""
        (tmp_path / "CLAUDE.md").write_text(_LEGACY_CLAUDE_MD)
        prompt = _engineer_prompt(
            tmp_path, VerifyConfig(project_stack=in_process_stack({"tests": MARKED_TEST}))
        )
        assert _LEGACY_CLAUDE_MD in prompt
        assert prompt.index(_LEGACY_CLAUDE_MD) < prompt.index(STACK_PROMPT.splitlines()[0])
        assert (tmp_path / "CLAUDE.md").read_text() == _LEGACY_CLAUDE_MD


class TestBuildProjectContext:
    def test_the_caller_config_is_the_only_config_consulted(
        self,
        tmp_path: Path,
    ) -> None:
        """It never re-reads kstrl.toml: the stack the caller's gate runs
        is the one stated, whatever the file now holds."""
        write_stack(tmp_path, {"lint": "eslint ."})
        context = build_project_context(
            tmp_path,
            PlainUI(no_color=True),
            VerifyConfig(project_stack=in_process_stack({"lint": "ruff check --preview ."})),
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
        assert not _block_is_injected(context)

    def test_no_gate_and_no_claude_md_yields_no_context(self, tmp_path: Path) -> None:
        assert build_project_context(tmp_path, PlainUI(no_color=True)) == ""

    def test_the_loop_omits_the_separator_when_there_is_no_context(
        self,
        tmp_path: Path,
    ) -> None:
        assert _engineer_prompt(tmp_path) == "STORY-PROMPT-BODY"


class TestNoVerificationEntryPoints:
    """`ks understand` and `ks feature` run no mechanical verification at
    all, so the engineer must not be told a gate will check its work.

    Before the fail-safe default, an `ks understand` iteration whose
    allowed paths permit only the codebase map was instructed to run the
    whole test suite plus mypy plus ruff on every pass.
    """

    def test_run_loops_default_states_no_commands(self, tmp_path: Path) -> None:
        """Every call site that does not name a gate inherits this."""
        assert not _block_is_injected(_engineer_prompt(tmp_path))

    def test_ks_understand_states_no_commands(self, tmp_path: Path) -> None:
        """Driven through the real CLI. Its allowed paths permit only
        the codebase map, so instructing it to run the suite is minutes
        and tokens spent on a claim that is false for that command."""
        prompt = _prompt_from_cli(["understand", "--root", str(tmp_path)])
        assert not _block_is_injected(prompt)

    def test_ks_feature_states_no_commands(self, tmp_path: Path) -> None:
        _write_feature_prd(tmp_path)
        prompt = _prompt_from_cli(_feature_cli_args(tmp_path))
        assert not _block_is_injected(prompt)
