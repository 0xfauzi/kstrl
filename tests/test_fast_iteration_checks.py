"""#233 Part A: checks between engineer iterations.

Every test drives the real ``run_loop`` with a real ``VerifyConfig`` loaded
from a real kstrl.toml, and the configured gate is a real shell command the
real ``check_linter`` runs. Only the agent is scripted. What is asserted is
what the loop produced: the prompts the agent was handed, the file the gate
command appends to every time it runs, and the ``iteration_completed``
events as written to and read back from a JSONL file.
"""

from __future__ import annotations

import textwrap
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kstrl.agents.proc import TIMEOUT_MESSAGE_PREFIX
from kstrl.breaker import BreakerConfig
from kstrl.cli import cli
from kstrl.config import KstrlConfig
from kstrl.event_catalog import IterationCompleted
from kstrl.events import EventBus, JsonlSink, read_events
from kstrl.factory import _run_component, _setup_worktree
from kstrl.loop import COMPLETION_MARKER, run_loop
from kstrl.stack import stack_toml
from kstrl.timeout import TimeoutConfig
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers.stack_confirmation import confirm_stack, in_process_stack, write_stack
from tests.spine_utils import init_kstrl_repo

#: Appends one line to lint_runs.txt every time the gate runs, then
#: passes only once the agent has written fixed.txt.
_LINT = "echo ran >> lint_runs.txt; test -f fixed.txt"

_HEADER = "=== LAST ITERATION MEASUREMENT ==="
_FOOTER = "=== END LAST ITERATION MEASUREMENT ==="
#: #696: the row is "stack:<name>", and the message is the exit status
#: only (decision 6) - no more per-check-type text ("Linter failed").
_LINT_FAILURE = f"- stack:linter: FAIL - `{_LINT}` exited 1"

_ENV = (
    "KSTRL_VERIFY_FAST_ITERATION_CHECKS",
    "KSTRL_VERIFY_LINT_CMD",
    "KSTRL_VERIFY_TYPECHECK_CMD",
    "KSTRL_VERIFY_TEST_CMD",
    "KSTRL_TIMEOUT_VERIFY",
)


@pytest.fixture(autouse=True)
def _no_verify_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader overlays env on toml; a developer's shell must not."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


class _ScriptedAgent:
    """Never completes unless told to. On an iteration named in ``fix_on``
    it writes fixed.txt, which is what makes the lint gate pass."""

    name = "scripted"
    final_message: str | None = None
    usage_records: list[Any] = []

    def __init__(
        self,
        *,
        fix_on: frozenset[int] = frozenset(),
        complete_on: frozenset[int] = frozenset(),
        time_out_on: frozenset[int] = frozenset(),
    ) -> None:
        self.prompts: list[str] = []
        self._fix_on = fix_on
        self._complete_on = complete_on
        self._time_out_on = time_out_on

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        self.prompts.append(prompt)
        iteration = len(self.prompts)
        assert cwd is not None
        if iteration in self._fix_on:
            (cwd / "fixed.txt").write_text("fixed\n", encoding="utf-8")
        if iteration in self._time_out_on:
            yield f"{TIMEOUT_MESSAGE_PREFIX} simulated"
        if iteration in self._complete_on:
            yield COMPLETION_MARKER
        yield "working"


def _project(
    root: Path,
    verify_toml: str,
    *,
    checks: Mapping[str, str] | None = None,
    iterations: int,
) -> tuple[KstrlConfig, VerifyConfig]:
    """``checks`` is written as the (confirmed) ``[stack]``, never a
    retired ``[verify]`` command key (#696); ``verify_toml`` is the rest
    of ``[verify]`` (``fast_iteration_checks``, ``subprocess_timeout``)."""
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("STORY-PROMPT-BODY", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}', encoding="utf-8")
    if checks is not None:
        write_stack(root, checks)
        confirm_stack(root)
    toml_path = root / "kstrl.toml"
    existing = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    toml_path.write_text(f"{existing}\n[verify]\n{verify_toml}", encoding="utf-8")
    config = KstrlConfig(
        max_iterations=iterations,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        kstrl_branch="",
        kstrl_branch_explicit=True,
    )
    return config, VerifyConfig.load(root)


def _run(
    root: Path,
    config: KstrlConfig,
    verify: VerifyConfig,
    agent: _ScriptedAgent,
    *,
    context_prefix: str | None = None,
) -> list[list[str] | None]:
    """Run the loop; return each iteration_completed event's
    ``fast_checks_failed`` as written to disk (None when absent)."""
    events_path = root / "engineer.jsonl"
    sink = JsonlSink(events_path)
    try:
        run_loop(
            config,
            PlainUI(no_color=True),
            agent,  # type: ignore[arg-type]
            root,
            context_prefix=context_prefix,
            timeouts=TimeoutConfig(),
            breaker_config=BreakerConfig(no_progress_iterations=0),
            bus=EventBus(sink),
            verify_config=verify,
        )
    finally:
        sink.close()
    return [
        event.to_dict()["data"].get("fast_checks_failed")
        for event in read_events(events_path)
        if isinstance(event, IterationCompleted)
    ]


def _lint_runs(root: Path) -> int:
    path = root / "lint_runs.txt"
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


#: #696: the check moves to [stack]; [verify] now carries only the gate list.
_LINTER = {"linter": _LINT}
_ON = 'fast_iteration_checks = ["linter"]\n'


def test_the_next_prompt_carries_the_failing_gate_and_is_replaced_not_accumulated(
    tmp_path: Path,
) -> None:
    config, verify = _project(tmp_path, _ON, checks=_LINTER, iterations=3)
    agent = _ScriptedAgent(fix_on=frozenset({2}))

    readings = _run(tmp_path, config, verify, agent)

    first, second, third = agent.prompts
    block = f"{_HEADER}\n{_LINT_FAILURE}"
    assert second.startswith(block), second[:200]
    assert second.endswith(f"{_FOOTER}\n\n{first}")
    assert _HEADER not in first
    # Iteration 2 fixed the gate, so iteration 3's prompt drops the block
    # entirely: replaced, never accumulated.
    assert third == first
    assert _lint_runs(tmp_path) == 3
    assert readings == [["stack:linter"], [], []]


def test_the_block_sits_between_the_retry_context_and_the_body(tmp_path: Path) -> None:
    config, verify = _project(tmp_path, _ON, checks=_LINTER, iterations=2)
    agent = _ScriptedAgent()

    _run(tmp_path, config, verify, agent, context_prefix="RETRY-CONTEXT")

    first, second = agent.prompts
    assert first.startswith("RETRY-CONTEXT\n\n")
    body = first[len("RETRY-CONTEXT\n\n") :]
    assert second == f"RETRY-CONTEXT\n\n{_HEADER}\n{_LINT_FAILURE}\n{_FOOTER}\n\n{body}"


def test_off_by_default_runs_nothing_and_changes_no_prompt(tmp_path: Path) -> None:
    config, verify = _project(tmp_path, "", checks=_LINTER, iterations=3)
    agent = _ScriptedAgent()

    readings = _run(tmp_path, config, verify, agent)

    assert verify.fast_iteration_checks == []
    assert agent.prompts[0] == agent.prompts[1] == agent.prompts[2]
    assert _lint_runs(tmp_path) == 0
    assert readings == [[], [], []]


def test_a_completed_iteration_is_not_measured(tmp_path: Path) -> None:
    config, verify = _project(tmp_path, _ON, checks=_LINTER, iterations=3)
    agent = _ScriptedAgent(complete_on=frozenset({1}))

    readings = _run(tmp_path, config, verify, agent)

    assert len(agent.prompts) == 1
    assert _lint_runs(tmp_path) == 0
    assert readings == [[]]


def test_a_killed_iteration_is_not_measured(tmp_path: Path) -> None:
    config, verify = _project(tmp_path, _ON, checks=_LINTER, iterations=2)
    agent = _ScriptedAgent(time_out_on=frozenset({1}))

    readings = _run(tmp_path, config, verify, agent)

    first, second = agent.prompts
    assert second == first
    assert _lint_runs(tmp_path) == 1
    assert readings == [[], ["stack:linter"]]


def test_a_gate_that_overruns_the_verify_limit_is_cut_off_and_reported(
    tmp_path: Path,
) -> None:
    """The gates run under ``[verify] subprocess_timeout``, the limit Phase 1
    uses. A gate that would run for 30 s is killed at 1 s, the loop goes
    on, and the next prompt says the gate timed out. Passing no limit to
    the gate function makes this take 30 s per iteration and fail."""
    config, verify = _project(
        tmp_path,
        'subprocess_timeout = 1.0\nfast_iteration_checks = ["linter"]\n',
        checks={"linter": "sleep 30"},
        iterations=2,
    )
    agent = _ScriptedAgent()

    started = time.monotonic()
    readings = _run(tmp_path, config, verify, agent)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"the loop took {elapsed:.1f}s; the 1 s gate limit was not applied"
    assert (
        f"{_HEADER}\n- stack:linter: FAIL - `sleep 30` timed out after 1.0s\n{_FOOTER}"
        in agent.prompts[1]
    )
    assert readings == [["stack:linter"], ["stack:linter"]]


# #696 flag day: test_a_python_default_between_iterations_on_a_tree_with_no_
# python_is_not_run is deleted. Its subject was the Python-default command
# resolution ("Not run: `uv run ruff check .`" when no lint_command was set
# and no pyproject.toml existed) - every default command and the toolchain
# detection that chose one are retired (kstrl/toolchains.py deviation 1); a
# check now runs only the exact command an operator's [stack] names, never
# a command kstrl invented, so there is no default left to not-run.


def test_the_factory_worker_hands_the_reading_to_the_next_iteration(tmp_path: Path) -> None:
    """End to end through ``factory._run_component``, the function the
    factory scheduler submits for every component, with a real worktree
    and a fake agent BINARY run as a subprocess. The factory passes its
    ``VerifyConfig`` to the loop; this proves the reading reaches the
    prompt the agent process actually read on its stdin."""
    root = tmp_path / "repo"
    init_kstrl_repo(root, ("comp-a",))
    worktree = _setup_worktree(
        "comp-a", "kstrl/factory/comp-a", "main", root, "run-233", ui=PlainUI(no_color=True)
    )
    capture = tmp_path / "capture"
    capture.mkdir()
    agent_bin = tmp_path / "bin" / "fake-agent"
    agent_bin.parent.mkdir()
    # Saves each prompt as prompt-<n>.txt and never completes.
    agent_bin.write_text(
        textwrap.dedent(f"""\
            #!/bin/bash
            n=$(ls '{capture}' | wc -l | tr -d ' ')
            cat > "{capture}/prompt-$((n + 1)).txt"
            echo working
        """),
        encoding="utf-8",
    )
    agent_bin.chmod(0o755)

    result = _run_component(
        "comp-a",
        "scripts/kstrl/feature/comp-a/prd.json",
        str(worktree),
        str(root),
        "scripts/kstrl/prompt.md",
        str(agent_bin),
        None,  # model
        None,  # reasoning
        None,  # agent_type
        0.0,  # sleep_seconds
        max_iterations=2,
        verify_config=VerifyConfig(
            project_stack=in_process_stack({"linter": "test -f fixed.txt"}),
            fast_iteration_checks=["linter"],
        ),
        run_id="test-run",
    )

    assert result.success is False
    first = (capture / "prompt-1.txt").read_text(encoding="utf-8")
    second = (capture / "prompt-2.txt").read_text(encoding="utf-8")
    # This component's check command differs from the module-level _LINT, so
    # its own failure line, not the shared _LINT_FAILURE constant.
    block = f"{_HEADER}\n- stack:linter: FAIL - `test -f fixed.txt` exited 1\n{_FOOTER}\n\n"
    assert _HEADER not in first
    assert block in second
    assert second.replace(block, "", 1) == first


# ---------------------------------------------------------------------------
# The config key, through the real loader on a real kstrl.toml.
# ---------------------------------------------------------------------------


def _load(root: Path, verify_toml: str, *, checks: Mapping[str, str] | None = None) -> VerifyConfig:
    """``checks`` is an (unconfirmed) ``[stack]``: presence is enough here,
    since ``fast_iteration_checks`` validates against its check NAMES
    (#696), never against whether anyone confirmed it."""
    stack_text = stack_toml(in_process_stack(checks)) if checks is not None else ""
    (root / "kstrl.toml").write_text(f"{stack_text}\n[verify]\n{verify_toml}", encoding="utf-8")
    return VerifyConfig.load(root)


_BOTH = {"typecheck": "true", "linter": "true"}


def test_the_toml_list_is_loaded_in_order(tmp_path: Path) -> None:
    loaded = _load(tmp_path, 'fast_iteration_checks = ["typecheck", "linter"]\n', checks=_BOTH)
    assert loaded.fast_iteration_checks == ["typecheck", "linter"]


def test_the_default_is_an_empty_list(tmp_path: Path) -> None:
    assert VerifyConfig().fast_iteration_checks == []
    assert _load(tmp_path, "").fast_iteration_checks == []


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ('["mypy"]', "mypy"),
        ('"linter"', "list"),
        ("[1]", "list"),
    ],
)
def test_a_bad_toml_value_is_refused(tmp_path: Path, value: str, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        _load(tmp_path, f"fast_iteration_checks = {value}\n")


def test_env_overrides_toml_and_empty_env_turns_it_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KSTRL_VERIFY_FAST_ITERATION_CHECKS", "typecheck, linter")
    assert _load(
        tmp_path, 'fast_iteration_checks = ["linter"]\n', checks=_BOTH
    ).fast_iteration_checks == [
        "typecheck",
        "linter",
    ]
    monkeypatch.setenv("KSTRL_VERIFY_FAST_ITERATION_CHECKS", "")
    # The toml value is still validated on its own names even though the
    # env overrides it to [] (kstrl/verify.py::VerifyConfig.load, unchanged
    # by #696), so the [stack] naming "linter" is still needed here.
    assert (
        _load(tmp_path, 'fast_iteration_checks = ["linter"]\n', checks=_BOTH).fast_iteration_checks
        == []
    )


def test_an_unknown_env_gate_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSTRL_VERIFY_FAST_ITERATION_CHECKS", "linter,ruff")
    with pytest.raises(ValueError, match="ruff"):
        _load(tmp_path, "")


def test_ks_check_refuses_a_bad_value_before_measuring(tmp_path: Path) -> None:
    """The command-entry preflight reads [verify] through the same loader,
    so a typo in the new key stops the command with exit 2 and names it."""
    write_stack(tmp_path, {"tests": "true", "typecheck": "true", "linter": "true"})
    confirm_stack(tmp_path)
    toml_path = tmp_path / "kstrl.toml"
    toml_path.write_text(
        toml_path.read_text(encoding="utf-8") + '\n[verify]\nfast_iteration_checks = ["mypy"]\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["check", "--root", str(tmp_path), "--json"])
    assert result.exit_code == 2, result.output
    assert "fast_iteration_checks" in result.output
    # A valid environment value does not hide the bad kstrl.toml one.
    overridden = CliRunner().invoke(
        cli,
        ["check", "--root", str(tmp_path), "--json"],
        env={"KSTRL_VERIFY_FAST_ITERATION_CHECKS": "linter"},
    )
    assert overridden.exit_code == 2, overridden.output
    assert "mypy" in overridden.output, overridden.output
