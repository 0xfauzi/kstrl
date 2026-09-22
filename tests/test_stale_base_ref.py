"""After a squash merge lands on GitHub, ``origin/<base>`` advances and
local ``<base>`` does not, because kstrl never moves the operator's
branches. Every consumer that measures against the bare name then sees
the previous tier's merged work as this engineer's foreign files. This
file drives the real functions against a real temporary repository with a
real bare remote (#435).
"""

from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl import contract, factory, git, guards, verify
from kstrl.config import KstrlConfig
from kstrl.contract import ContractConfig, ContractMode
from kstrl.loop import LoopResult
from kstrl.manifest import Manifest
from kstrl.scope import ComponentScope
from kstrl.ui.plain import PlainUI
from tests.helpers.component_prd import write_component_prd
from tests.helpers.gitrepo import git_in, set_identity


def _sha(repo: Path, rev: str) -> str:
    done = subprocess.run(
        ["git", "rev-parse", rev],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return done.stdout.strip()


@dataclass(frozen=True)
class StaleBase:
    root: Path  # holds remote.git, repo, wt
    repo: Path  # the project checkout: local main behind origin/main
    worktree: Path  # a tier-2 component worktree cut from origin/main
    local_main: str  # sha
    origin_main: str  # sha


def _build_stale_base(root: Path) -> StaleBase:
    remote = root / "remote.git"
    repo = root / "repo"
    git_in(root, "init", "--bare", "-b", "main", str(remote))

    git_in(root, "init", "-b", "main", str(repo))
    set_identity(repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "init")
    git_in(repo, "remote", "add", "origin", str(remote))
    git_in(repo, "push", "-u", "origin", "main")

    # Tier 1: a component worktree cut from origin/main.
    wt1 = root / "wt1"
    git_in(repo, "worktree", "add", str(wt1), "-b", "comp/link-rules", "origin/main")
    (wt1 / "src").mkdir(parents=True)
    (wt1 / "tests").mkdir(parents=True)
    (wt1 / "src" / "rules.py").write_text("RULES = []\n", encoding="utf-8")
    (wt1 / "tests" / "test_rules.py").write_text("def test_rules():\n    pass\n", encoding="utf-8")
    git_in(wt1, "add", "-A")
    git_in(wt1, "commit", "-q", "-m", "add rules")
    git_in(wt1, "push", "-u", "origin", "comp/link-rules")

    # The remote merge: squash tier 1 into main directly on the bare
    # remote, exactly as a GitHub squash merge does - a NEW commit main
    # never carried locally.
    merger = root / "merger"
    git_in(root, "clone", str(remote), str(merger))
    set_identity(merger)
    git_in(merger, "merge", "--squash", "origin/comp/link-rules")
    git_in(merger, "commit", "-q", "-m", "squash: link-rules")
    git_in(merger, "push", "origin", "main")

    # What the factory does after every PR merge: update the
    # remote-tracking ref, touch nothing else.
    error = git.fetch_base_branch("main", repo)
    assert error is None, error

    # Tier 2: a component worktree cut from the now-advanced origin/main.
    wt = root / "wt"
    git_in(repo, "worktree", "add", str(wt), "-b", "comp/storage", "origin/main")

    local_main = _sha(repo, "main")
    origin_main = _sha(repo, "origin/main")
    assert local_main != origin_main, (
        "fixture stopped reproducing the situation: local main and "
        "origin/main must differ, or every test below is vacuous"
    )
    return StaleBase(
        root=root,
        repo=repo,
        worktree=wt,
        local_main=local_main,
        origin_main=origin_main,
    )


@pytest.fixture
def fx(tmp_path: Path) -> StaleBase:
    return _build_stale_base(tmp_path)


def test_the_guard_baseline_is_the_advanced_remote(fx: StaleBase) -> None:
    baseline = git.capture_workspace_baseline(fx.worktree, base_ref="main")
    assert baseline.head == fx.origin_main


def test_the_baseline_agrees_with_the_one_precedence_rule(fx: StaleBase) -> None:
    baseline = git.capture_workspace_baseline(fx.worktree, base_ref="main")
    assert baseline.head == git.resolve_base_sha("main", fx.worktree)


def test_there_is_one_resolver() -> None:
    import kstrl.git as git_mod

    assert not hasattr(git_mod, "resolve_ref")
    assert git_mod.base_ref_candidates("main") == ("origin/main", "main")


def test_the_in_loop_guard_blames_no_file_from_the_previous_tier(fx: StaleBase) -> None:
    baseline = git.capture_workspace_baseline(fx.worktree, base_ref="main")
    (fx.worktree / "src" / "storage.py").write_text("STORAGE = {}\n", encoding="utf-8")
    git_in(fx.worktree, "add", "-A")
    git_in(fx.worktree, "commit", "-q", "-m", "add storage")

    config = KstrlConfig(
        max_iterations=1,
        prompt_file=fx.worktree / "scripts" / "kstrl" / "prompt.md",
        prd_file=fx.worktree / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        interactive=False,
        kstrl_branch="",
        kstrl_branch_explicit=True,
        allowed_paths=["src/storage.py"],
    )
    ok, violations = guards.enforce_allowed_paths(
        config, PlainUI(no_color=True, file=io.StringIO()), fx.worktree, baseline=baseline
    )
    assert violations == []
    assert ok is True


def test_a_programming_error_is_not_a_silent_fallback(
    fx: StaleBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> str:
        raise TypeError("wrong arity")

    monkeypatch.setattr(git, "resolve_base_sha", boom)
    with pytest.raises(TypeError):
        git.capture_workspace_baseline(fx.worktree, base_ref="main")


def test_the_contract_checkout_carries_the_merged_tier(fx: StaleBase) -> None:
    manifest = Manifest(
        version="1",
        spec_file="",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=[],
    )
    config = ContractConfig(
        mode=ContractMode.TIER.value,
        test_command="test -f src/rules.py",
        timeout=120.0,
    )
    result = contract.run_integrated_base_check(
        manifest, ["link-rules"], fx.repo, config, PlainUI(no_color=True, file=io.StringIO())
    )
    assert result.passed is True


def test_a_contract_run_that_collected_nothing_says_so(fx: StaleBase) -> None:
    manifest = Manifest(
        version="1",
        spec_file="",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=[],
    )
    config = ContractConfig(
        mode=ContractMode.TIER.value,
        test_command="exit 5",
        timeout=120.0,
    )
    result = contract.run_integrated_base_check(
        manifest, ["link-rules"], fx.repo, config, PlainUI(no_color=True, file=io.StringIO())
    )
    assert result.passed is False
    assert "exited 5" in result.test_output
    assert "no tests were collected" in result.test_output


def test_the_no_tests_sentence_lives_in_the_shared_helper(fx: StaleBase) -> None:
    passed, output = contract._run_tests(fx.worktree, "exit 5", 30.0)
    assert passed is False
    assert "exited 5" in output
    assert "no tests were collected" in output


def test_the_in_loop_scope_message_names_the_ref_it_judged(fx: StaleBase) -> None:
    rel = "components/storage/prd.json"
    (fx.worktree / "scripts" / "kstrl").mkdir(parents=True)
    (fx.worktree / "scripts" / "kstrl" / "prompt.md").write_text("test prompt", encoding="utf-8")
    (fx.worktree / "scripts" / "kstrl" / "prd.json").write_text(
        '{"branchName": "t", "userStories": []}', encoding="utf-8"
    )
    (fx.worktree / "kstrl.toml").write_text("[knowledge]\nenabled = false\n", encoding="utf-8")
    write_component_prd(fx.worktree, rel, allowed_paths=["src/"])

    seen: dict[str, object] = {}

    def fake_run_loop(*_args: object, **kwargs: object) -> LoopResult:
        seen.update(kwargs)
        return LoopResult(
            completed=False,
            iterations=1,
            exit_code=1,
            duration_seconds=0.0,
            guard_violations=("src/rules.py",),
        )

    with patch("kstrl.loop.run_loop", side_effect=fake_run_loop):
        result = factory._run_component(
            component_id="storage",
            prd_path_str=rel,
            worktree_path_str=str(fx.worktree),
            root_dir_str=str(fx.worktree),
            prompt_file_str="scripts/kstrl/prompt.md",
            agent_cmd="echo test",
            model=None,
            reasoning=None,
            agent_type=None,
            sleep_seconds=0.0,
            scope=ComponentScope(["src/"], [rel], "storage", rel),
            base_branch="main",
            redirect_output=False,
        )

    assert seen["guard_base_ref"] == "origin/main"
    assert "Base branch: origin/main" in (result.error or "")
    assert "git diff origin/main...HEAD" in (result.error or "")


def test_phase_1_scope_message_names_the_ref_it_judged(fx: StaleBase) -> None:
    (fx.worktree / "evil.py").write_text("x = 1\n", encoding="utf-8")
    git_in(fx.worktree, "add", "-A")
    git_in(fx.worktree, "commit", "-q", "-m", "add evil")

    result = verify.check_diff_scope(fx.worktree, "main", allowed_paths=["src/"])
    assert result.passed is False
    assert "origin/main" in result.message
    assert "Base branch: origin/main" in "\n".join(result.details)


def test_a_local_only_repository_still_names_the_bare_branch(tmp_path: Path) -> None:
    repo = tmp_path / "solo"
    git_in(tmp_path, "init", "-b", "main", str(repo))
    set_identity(repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "init")
    # main stays behind; the diff runs from a branch cut off it, the same
    # shape fx.worktree has against origin/main, so there is something for
    # check_diff_scope to see. Diffing a branch against its own tip is
    # empty and would make this test vacuous either way.
    git_in(repo, "checkout", "-b", "work")
    (repo / "evil.py").write_text("x = 1\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "add evil")

    result = verify.check_diff_scope(repo, "main", allowed_paths=["src/"])
    assert result.passed is False
    details = "\n".join(result.details)
    assert "Base branch: main" in details
    assert "origin/" not in details
