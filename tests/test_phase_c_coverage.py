"""Phase C: integration tests filling untested code paths.

C1 - parallel ProcessPoolExecutor execution (spine: real git worktrees,
     two workers proven concurrent via a filesystem barrier)
C2 - Phase 2 review retry path
C3 - Phase 2.5 security review retry path
C4 - Phase 3 contract testing tier-breaker
C5 - single_pr mode integration
C6 - concurrent factory invocation against the SAME root_dir (spine:
     second invocation refused by the run-level flock, wave-3 R0.5
     semantics; fails if the flock is deleted)
C8 - pickling round-trip for every config dataclass
C9 - agent factory returns the right type for each agent_type

C7 (crash recovery) is already covered by test_factory.py::
TestRunFactoryExecution::test_crash_recovery_resets_running and
test_crash_recovery_resets_verifying. Not re-tested here.

C10 - Windows skip markers applied to flock / POSIX-only tests below.
"""

from __future__ import annotations

import json
import pickle  # Safe: we pickle and immediately unpickle objects we

# constructed in this same test, never untrusted data.
# This guards the ProcessPoolExecutor compat surface.
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ralph_py.config import RalphConfig
from ralph_py.contract import ContractConfig, ContractMode
from ralph_py.evolution import EvolutionConfig
from ralph_py.factory import (
    ComponentResult,
    FactoryConfig,
    FactoryResult,
    run_factory,
)
from ralph_py.feedforward import FeedforwardConfig
from ralph_py.knowledge import KnowledgeConfig
from ralph_py.manifest import Component, Manifest
from ralph_py.review import ReviewResult
from ralph_py.security import SecurityConfig, SecurityMode
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import VerifyConfig
from tests import spine_utils

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=components,
    )


def _component(comp_id: str, deps: list[str] | None = None) -> Component:
    return Component(
        id=comp_id,
        title=f"Component {comp_id}",
        description=f"desc {comp_id}",
        dependencies=deps or [],
        prd_path=f"scripts/ralph/feature/{comp_id}/prd.json",
        branch_name=f"ralph/{comp_id}",
    )


def _setup_project(tmp_path: Path, component_ids: list[str]) -> Path:
    """Lay down minimal scaffold + per-component PRDs."""
    (tmp_path / "scripts" / "ralph").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "ralph" / "prompt.md").write_text("test prompt")
    (tmp_path / "scripts" / "ralph" / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    for comp_id in component_ids:
        feature_dir = tmp_path / "scripts" / "ralph" / "feature" / comp_id
        feature_dir.mkdir(parents=True, exist_ok=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": f"US-{comp_id}", "title": "ok",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
    return tmp_path


def _base_config(root: Path) -> RalphConfig:
    return RalphConfig(
        prompt_file=root / "scripts/ralph/prompt.md",
        prd_file=root / "scripts/ralph/prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        ralph_branch="",
        ralph_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _verify_passing() -> VerifyConfig:
    return VerifyConfig(
        test_command="true", typecheck_command="true",
        lint_command="true", check_diff_scope=False,
        check_bad_patterns=False, subprocess_timeout=5.0,
    )


# ---------------------------------------------------------------------------
# C1 - parallel ProcessPoolExecutor
# ---------------------------------------------------------------------------


@pytest.mark.spine
@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell fake agents")
class TestC1ParallelExecution:
    """R4.3: true two-worker execution through the ProcessPoolExecutor
    path - real git repo, real worktrees, real fake-agent subprocesses,
    no mocks.

    Concurrency proof: the two engineers rendezvous at a filesystem
    barrier. Each records its start marker, then waits (bounded) until
    BOTH markers exist before emitting the completion promise. If the
    factory ran them sequentially, the first engineer would exhaust the
    barrier wait and leave a ``.barrier-timeout`` marker, failing the
    test - so this cannot pass without two engineers alive at once."""

    def test_two_components_run_concurrently_in_worktrees(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        spine_utils.init_ralph_repo(root, ("comp-a", "comp-b"))
        sync = tmp_path / "sync"
        sync.mkdir()

        # The engineer's cwd is its worktree, whose basename is the
        # component id. 300 * 0.05s = 15s barrier bound, far above the
        # sub-second rendezvous when both workers are genuinely live.
        barrier_agent = (
            f'comp=$(basename "$PWD"); '
            f'touch "{sync}/$comp.started"; n=0; '
            f'while [ "$(ls "{sync}" | grep -c ".started$")" -lt 2 ]; do '
            f'n=$((n+1)); '
            f'if [ "$n" -gt 300 ]; then '
            f'touch "{sync}/$comp.barrier-timeout"; break; fi; '
            f"sleep 0.05; done; {spine_utils.COMPLETE_LINE}"
        )

        manifest = spine_utils.make_manifest([
            spine_utils.component("comp-a"), spine_utils.component("comp-b"),
        ])
        config = spine_utils.factory_config(max_parallel=2)
        result = run_factory(
            manifest, config,
            spine_utils.base_config(root, agent_cmd=barrier_agent),
            PlainUI(no_color=True), root,
        )

        started = sorted(p.name for p in sync.glob("*.started"))
        assert started == ["comp-a.started", "comp-b.started"]
        barrier_timeouts = sorted(p.name for p in sync.glob("*.barrier-timeout"))
        assert barrier_timeouts == [], (
            "an engineer exhausted the rendezvous barrier: the factory "
            "did not run the two components concurrently"
        )
        assert sorted(result.completed) == ["comp-a", "comp-b"]
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# C2 - Phase 2 review retry
# ---------------------------------------------------------------------------


class TestC2ReviewRetry:
    """Reviewer fails first iteration, passes the second. Component
    should retry and ultimately succeed."""

    def test_phase_2_failure_triggers_retry(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            max_retries=2, retry_delay=0, review_mode="hard",
            verify_config=_verify_passing(),
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        review_calls = {"count": 0}

        def fake_run_review(*args, **kwargs):
            review_calls["count"] += 1
            if review_calls["count"] == 1:
                return ReviewResult(passed=False, mode="hard")
            return ReviewResult(passed=True, mode="hard")

        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ), patch(
            "ralph_py.factory.run_review", side_effect=fake_run_review,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert review_calls["count"] >= 2  # retried
        assert "comp-a" in result.completed


# ---------------------------------------------------------------------------
# C3 - Phase 2.5 security retry
# ---------------------------------------------------------------------------


class TestC3SecurityRetry:
    def test_security_failure_triggers_retry(self, tmp_path: Path) -> None:
        from ralph_py.security import SecurityResult

        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            max_retries=2, retry_delay=0, review_mode="skip",
            security_config=SecurityConfig(
                mode=SecurityMode.HARD.value, fail_threshold="high",
            ),
            verify_config=_verify_passing(),
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        sec_calls = {"count": 0}

        def fake_run_security(*args, **kwargs):
            sec_calls["count"] += 1
            if sec_calls["count"] == 1:
                return SecurityResult(passed=False, mode="hard")
            return SecurityResult(passed=True, mode="hard")

        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ), patch(
            "ralph_py.factory.run_security_review", side_effect=fake_run_security,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert sec_calls["count"] >= 2
        assert "comp-a" in result.completed


# ---------------------------------------------------------------------------
# C4 - Phase 3 contract testing tier-breaker
# ---------------------------------------------------------------------------


class TestC4ContractBreaker:
    def test_contract_failure_sends_breaker_for_retry(
        self, tmp_path: Path,
    ) -> None:
        from ralph_py.contract import ContractResult

        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        comp_a = _component("comp-a")
        comp_b = _component("comp-b", deps=["comp-a"])
        manifest = _make_manifest([comp_a, comp_b])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            max_retries=1, retry_delay=0, review_mode="skip",
            contract_config=ContractConfig(mode=ContractMode.TIER.value),
            verify_config=_verify_passing(),
        )
        success_a = ComponentResult("comp-a", success=True, iterations=1)
        success_b = ComponentResult("comp-b", success=True, iterations=1)

        # First contract call fails with breaker=comp-a. R0.3: the
        # breaker reset must actually re-enter scheduling (a third
        # _run_component call) and the second, passing contract call
        # completes the run cleanly.
        contract_calls = {"count": 0}

        def fake_contract(manifest, root, cfg, ui, components_merged=False):
            contract_calls["count"] += 1
            if contract_calls["count"] == 1:
                return [ContractResult(
                    passed=False, tier=0,
                    components_tested=["comp-a"], breaker="comp-a",
                    test_output="planted failure",
                )]
            return [ContractResult(
                passed=True, tier=0, components_tested=["comp-a"],
            )]

        with patch(
            "ralph_py.factory._run_component",
            side_effect=[success_a, success_b, success_a],
        ) as mock_run, patch(
            "ralph_py.factory.run_contract_testing", side_effect=fake_contract,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert contract_calls["count"] == 2
        assert mock_run.call_count == 3
        assert "comp-a" in result.completed
        assert result.contract_failures == []
        assert result.exit_code == 0
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.retries == 1


# ---------------------------------------------------------------------------
# C5 - single_pr mode
# ---------------------------------------------------------------------------


class TestC5SinglePrMode:
    def test_single_pr_skips_knowledge_distill_and_completes(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        manifest.single_pr = True
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="skip", verify_config=_verify_passing(),
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ), patch(
            "ralph_py.factory.distill_facts", return_value=(0, "skipped"),
        ) as mock_distill, patch(
            "ralph_py.git.get_diff_content", return_value="",
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.completed
        # A2 invariant: distill is skipped in single_pr mode.
        mock_distill.assert_not_called()


# ---------------------------------------------------------------------------
# C6 - concurrent factory invocations
# ---------------------------------------------------------------------------


@pytest.mark.spine
@pytest.mark.skipif(sys.platform == "win32",
                    reason="flock is POSIX-only; concurrent factory test")
class TestC6ConcurrentFactory:
    """R4.3: two real run_factory invocations against the SAME root_dir,
    no mocks. The first invocation is held mid-run by a gated engineer;
    while it is live, a second invocation on the same root must be
    refused with exit code 2 (wave-3 R0.5 semantics: the run-level
    ``.ralph/factory.lock`` flock refuses, it does not queue). Deleting
    the flock from _acquire_run_lock makes the second invocation proceed
    instead, so this test fails without it.

    flock exclusion applies between two separate open()s of the lock
    file even within one process (verified: second LOCK_EX|LOCK_NB is
    denied with EWOULDBLOCK), so the first invocation can run on a
    thread while the contender runs on the test thread."""

    def test_second_invocation_on_same_root_is_refused_while_first_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        spine_utils.init_ralph_repo(root, ("comp-a",))
        started = tmp_path / "engineer.started"
        release = tmp_path / "engineer.release"

        # Gated engineer: proves the first invocation is mid-run, then
        # holds until released. 600 * 0.05s = 30s bound.
        gated_agent = (
            f'touch "{started}"; n=0; '
            f'while [ ! -e "{release}" ]; do n=$((n+1)); '
            f'if [ "$n" -gt 600 ]; then exit 1; fi; sleep 0.05; done; '
            f"{spine_utils.COMPLETE_LINE}"
        )

        first_results: list[FactoryResult] = []

        def first_invocation() -> None:
            first_results.append(run_factory(
                spine_utils.make_manifest([spine_utils.component("comp-a")]),
                spine_utils.factory_config(),
                spine_utils.base_config(root, agent_cmd=gated_agent),
                PlainUI(no_color=True), root,
            ))

        holder = threading.Thread(target=first_invocation)
        holder.start()
        try:
            deadline = time.monotonic() + 30
            while not started.exists():
                assert time.monotonic() < deadline, (
                    "first invocation never reached its engineer"
                )
                assert holder.is_alive(), (
                    "first invocation died before its engineer started"
                )
                time.sleep(0.01)

            # First invocation is now mid-engineer and holds the run
            # lock. A contender on the SAME root must be refused without
            # scheduling anything.
            contender_manifest = spine_utils.make_manifest(
                [spine_utils.component("comp-a")],
            )
            refused = run_factory(
                contender_manifest, spine_utils.factory_config(),
                spine_utils.base_config(root), PlainUI(no_color=True), root,
            )
            assert refused.exit_code == 2
            assert refused.completed == []
            assert contender_manifest.components[0].status == "pending"
        finally:
            release.write_text("go")
            holder.join(timeout=60)

        assert not holder.is_alive(), "first invocation did not finish"
        assert first_results and first_results[0].exit_code == 0
        assert first_results[0].completed == ["comp-a"]


# ---------------------------------------------------------------------------
# C8 - pickling regression for every config dataclass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory_fn", [
    lambda: RalphConfig(),
    lambda: FactoryConfig(),
    lambda: VerifyConfig(),
    lambda: ContractConfig(),
    lambda: FeedforwardConfig(),
    lambda: EvolutionConfig(),
    lambda: KnowledgeConfig(),
    lambda: SecurityConfig(),
])
def test_c8_config_pickle_roundtrip(factory_fn) -> None:
    """Configs flow through ProcessPoolExecutor's pickling. If a config
    gains a non-picklable field, workers break silently. This catches
    it."""
    obj = factory_fn()
    restored = pickle.loads(pickle.dumps(obj))
    assert type(restored) is type(obj)


# ---------------------------------------------------------------------------
# C9 - agent factory matrix
# ---------------------------------------------------------------------------


class TestC9AgentFactoryMatrix:
    def test_get_agent_returns_custom_when_cmd_set(self) -> None:
        from ralph_py.agents import CustomAgent, get_agent
        agent = get_agent(agent_cmd="my-cli --opt")
        assert isinstance(agent, CustomAgent)

    def test_get_agent_returns_codex_when_typed(self) -> None:
        from ralph_py.agents import CodexAgent, get_agent
        agent = get_agent(agent_type="codex")
        assert isinstance(agent, CodexAgent)

    def test_get_agent_returns_claude_when_typed(self) -> None:
        import shutil

        from ralph_py.agents import ClaudeCodeAgent, get_agent
        if shutil.which("claude") is None:
            pytest.skip("claude CLI not present; auto-detect would skip claude")
        agent = get_agent(agent_type="claude-code")
        assert isinstance(agent, ClaudeCodeAgent)

    def test_get_agent_auto_prefers_claude_when_available(self) -> None:
        import shutil

        from ralph_py.agents import ClaudeCodeAgent, CodexAgent, get_agent
        agent = get_agent(agent_type="auto")
        if shutil.which("claude") is not None:
            assert isinstance(agent, ClaudeCodeAgent)
        else:
            assert isinstance(agent, CodexAgent)
