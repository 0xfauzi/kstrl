"""The scheduler's positional tuple, bound name by name (#229 BLOCKER 2).

``factory._submit_args`` builds a 35-element POSITIONAL tuple and the
pool calls ``_run_component`` with it, 1800 lines away. Most of the slots
are strings. Rotating three adjacent ``str`` slots is type-compatible, so
mypy is silent, and review round 1 measured what the suite said about it:
a plant that handed the golden-patterns path to ``progress_file_str``,
the progress path to ``codebase_map_file_str`` and the codebase-map path
to ``golden_patterns_file_str`` gave **5741 passed, 37 skipped, 38
xfailed**. In production that writes the engineer's progress log to the
golden-patterns path and reads the golden block out of
``codebase_map.md``, with every gate green.

Existing coverage was per-slot and therefore could not see it:
``test_usage_meter`` pins the LAST element, ``test_decisions`` binds the
tuple and then reads ONE argument out of it. This binds the whole thing.
It is closed over the tuple in both directions: a slot inserted in the
wrong place fails on the values, and a slot added at the end fails on the
key set, because the expectation is compared for equality and not for
containment.

It is also closed over BOTH submit branches (review round 2, nit 7).
``max_parallel == 1`` selects ``_InlineExecutor`` and passes five
keywords; anything higher selects the process pool, which passes two and
lets the other three take ``_run_component``'s defaults. The positional
tuple is shared, so the rotation above is caught either way, but "closed
over the signature" was true of one call site until the pool branch was
driven here too.
"""

from __future__ import annotations

import inspect
import json
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl import factory as factory_mod
from kstrl.config import KstrlConfig
from kstrl.events import RunPaths
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

COMP = "comp-a"
PRD_REL = f"scripts/kstrl/feature/{COMP}/prd.json"

#: The five parameters the scheduler passes BY NAME rather than through
#: the tuple. Named here so a sixth is a decision somebody writes down.
BY_KEYWORD = {"base_branch", "live_line", "redirect_output", "stop_check", "verify_config"}

#: What the POOL branch passes by name, which is two of the five. A pool
#: worker has no parent terminal to mirror transcript lines to and no
#: in-process stop event to share, so those three take
#: ``_run_component``'s own defaults there. Review round 2, nit 7: this
#: file exercised the inline branch only, so "closed over the SIGNATURE"
#: held for one of two call sites.
POOL_KEYWORDS = {"base_branch", "verify_config"}


class _CapturingPool:
    """Stands in for ProcessPoolExecutor so the POOL branch can be read.

    A real pool pickles what it is given, and a patched ``_run_component``
    is not picklable, which is why the pool branch had no coverage. This
    records the ``functools.partial`` the branch builds and resolves the
    future itself, so the scheduling loop runs unchanged.
    """

    seen: dict[str, Any] = {}

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers

    def submit(self, fn: Any, /, *args: Any) -> Future[ComponentResult]:
        _CapturingPool.seen = {"args": fn.args, "kwargs": fn.keywords}
        future: Future[ComponentResult] = Future()
        future.set_result(ComponentResult(COMP, success=True, iterations=1))
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """Nothing to shut down: every submit already resolved."""


def _project(tmp_path: Path) -> Path:
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    (kstrl_dir / "feature" / COMP).mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("test prompt", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}', encoding="utf-8"
    )
    (tmp_path / PRD_REL).write_text(
        json.dumps(
            {
                "branchName": "test",
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Test",
                        "acceptanceCriteria": ["AC1"],
                        "priority": 1,
                        "passes": True,
                        "notes": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _manifest() -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="proj",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=COMP,
                title="Component comp-a",
                description="d",
                dependencies=[],
                prd_path=PRD_REL,
                branch_name="kstrl/comp-a",
            )
        ],
    )


def _submitted(
    root: Path,
    max_parallel: int = 1,
    progress_log_enabled: bool = True,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """The exact ``(args, kwargs)`` one real ``run_factory`` submits.

    ``max_parallel`` selects the branch: 1 is ``_InlineExecutor``, 2 is
    the pool branch through :class:`_CapturingPool`.
    """
    # Worktrees ON for the pool branch and nothing else: `run_factory`
    # forces max_parallel=1 when they are off, so the pool branch is
    # unreachable without them. `_setup_worktree` is patched below to
    # hand back the root, which keeps this a unit test with no git in it.
    factory_config = FactoryConfig(
        use_worktrees=max_parallel > 1,
        create_prs=False,
        progress_log_enabled=progress_log_enabled,
        max_parallel=max_parallel,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=root / "scripts/kstrl/prompt.md",
        prd_file=root / "scripts/kstrl/prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> ComponentResult:
        seen["args"], seen["kwargs"] = args, kwargs
        return ComponentResult(COMP, success=True, iterations=1)

    _CapturingPool.seen = {}
    with (
        patch("kstrl.factory._run_component", side_effect=capture),
        patch("kstrl.factory.ProcessPoolExecutor", _CapturingPool),
        patch("kstrl.factory._setup_worktree", return_value=root),
        patch("kstrl.git.get_diff_content", return_value=""),
    ):
        run_factory(_manifest(), factory_config, base, PlainUI(no_color=True), root)
    submitted = seen or _CapturingPool.seen
    assert "args" in submitted, "the scheduler never submitted a component"
    return submitted["args"], submitted["kwargs"]


def _positional(
    root: Path,
    max_parallel: int = 1,
    progress_log_enabled: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(name -> value)`` for the tuple, plus the keyword arguments.

    ``bind_partial`` over the POSITIONALS ONLY: binding args and kwargs
    together would merge the two and hide which half a name came from,
    and it is the positional half that has no names in it to get wrong.
    """
    args, kwargs = _submitted(root, max_parallel, progress_log_enabled)
    signature = inspect.signature(factory_mod._run_component)
    return dict(signature.bind_partial(*args).arguments), kwargs


class TestTheWholeSubmitTupleIsBound:
    @pytest.mark.parametrize("max_parallel", [1, 2], ids=["inline", "pool"])
    def test_every_slot_lands_on_the_parameter_it_is_meant_for(
        self,
        tmp_path: Path,
        max_parallel: int,
    ) -> None:
        root = _project(tmp_path)

        bound, _kwargs = _positional(root, max_parallel)

        # The four run-scoped values are compared by the property that a
        # rotation would break rather than by a literal nobody can
        # predict: each is checked below to be in its own slot.
        expected: dict[str, Any] = {
            "component_id": COMP,
            "prd_path_str": PRD_REL,
            "worktree_path_str": str(root),
            "root_dir_str": str(root),
            "prompt_file_str": "scripts/kstrl/prompt.md",
            "agent_cmd": "echo test",
            "model": None,
            "reasoning": None,
            "agent_type": None,
            "sleep_seconds": 0,
            "previous_context_json": None,
            "feedforward_config_dict": None,
            "scaffold_cmd": None,
            "component_deps": None,
            "knowledge_prefix": "",
            "decisions_prefix": "",
            "progress_file_str": "scripts/kstrl/feature/comp-a/progress.txt",
            "codebase_map_file_str": "scripts/kstrl/codebase_map.md",
            "golden_patterns_file_str": "scripts/kstrl/golden-patterns.md",
            "memory_file_str": "scripts/kstrl/memory.md",
            "agent_iteration_timeout": 1800.0,
            "component_timeout": 7200.0,
            "max_iterations": 10,
            "interactive": False,
            "scope": bound["scope"],
            "breaker_iterations": 3,
            "breaker_test_command": "true",
            "breaker_test_timeout": 300.0,
            "sandbox_enabled": False,
            "sandbox_allow_network": False,
            "agent_budget_usd": None,
            "events_dir_str": bound["events_dir_str"],
            "usage_dir_str": bound["usage_dir_str"],
            "run_id": bound["run_id"],
            "token_budget": bound["token_budget"],
        }
        assert bound == expected

        assert bound["scope"].__class__.__name__ == "ComponentScope"
        assert bound["token_budget"].__class__.__name__ == "LoopBudget"
        assert bound["run_id"].startswith("factory-")
        for name in ("events_dir_str", "usage_dir_str"):
            assert bound[name].startswith(str(root)), name
            assert bound[name].endswith(bound["run_id"]), name

    def test_the_four_string_path_slots_are_distinguishable(self, tmp_path: Path) -> None:
        """The rotation review round 1 planted is a permutation of these,
        so the test above is worth nothing unless their values differ
        from each other. Said out loud rather than left to luck.

        Four since R10.9, not three: ``memory_file_str`` sits next to
        ``golden_patterns_file_str`` and is the same type, so it widens
        the set of type-compatible rotations mypy stays silent about.
        """
        root = _project(tmp_path)

        bound, _kwargs = _positional(root)
        quartet = [
            str(bound["progress_file_str"]),
            str(bound["codebase_map_file_str"]),
            str(bound["golden_patterns_file_str"]),
            str(bound["memory_file_str"]),
        ]

        assert len(set(quartet)) == 4, quartet

    @pytest.mark.parametrize(
        "max_parallel, expected",
        [(1, BY_KEYWORD), (2, POOL_KEYWORDS)],
        ids=["inline", "pool"],
    )
    def test_the_tuple_plus_the_keywords_covers_the_whole_signature(
        self,
        tmp_path: Path,
        max_parallel: int,
        expected: set[str],
    ) -> None:
        """Closed over the SIGNATURE, not over the tuple, on BOTH branches.

        A parameter the scheduler starts or stops passing moves one of
        these sets, and neither is a subset check. The two branches share
        the positional tuple and differ on the keyword half, so the three
        keywords the pool branch does not pass have to have defaults, or
        a pool run would raise where an inline run does not.
        """
        root = _project(tmp_path)

        bound, kwargs = _positional(root, max_parallel)
        signature = inspect.signature(factory_mod._run_component)

        assert set(kwargs) == expected
        assert set(bound) | BY_KEYWORD == set(signature.parameters)
        assert set(bound) & BY_KEYWORD == set()
        defaulted = {
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is not inspect.Parameter.empty
        }
        assert BY_KEYWORD - expected <= defaulted

    def test_the_two_run_scoped_directory_slots_are_distinguishable(
        self,
        tmp_path: Path,
    ) -> None:
        """Nit 8 of review round 2, measured: with the event log ON the
        two slots hold the SAME string, so swapping them in
        ``_submit_args`` left this file green (4 passed) while the full
        suite caught it. Progress logging off is the state that separates
        them, and it is the reason they are two arguments at all: the
        event channel goes None and the accounting channel does not."""
        root = _project(tmp_path)

        on, _ = _positional(root)
        off, _ = _positional(root, progress_log_enabled=False)

        run_dir = str(RunPaths.for_run(root, on["run_id"]).root)
        assert on["events_dir_str"] == run_dir
        assert on["usage_dir_str"] == run_dir
        assert off["events_dir_str"] is None
        assert off["usage_dir_str"] == str(RunPaths.for_run(root, off["run_id"]).root)

    def test_the_keyword_half_carries_the_values_it_is_meant_to(
        self,
        tmp_path: Path,
    ) -> None:
        root = _project(tmp_path)

        _bound, kwargs = _positional(root)

        assert kwargs["base_branch"] == "main"
        assert kwargs["redirect_output"] is False
        assert kwargs["stop_check"] is None
        assert kwargs["verify_config"].test_command == "true"
        assert callable(kwargs["live_line"])
