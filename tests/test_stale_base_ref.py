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


def test_the_baseline_agrees_with_the_merge_base(fx: StaleBase) -> None:
    """The baseline is the MERGE BASE of the resolved ref and HEAD, not
    that ref's current tip. In this fixture the worktree is cut right
    after the fetch and carries no commits of its own yet, so the merge
    base and the tip happen to be the identical commit - which is why a
    test cutting the worktree BEFORE a sibling's merge is needed too
    (below): it is the only shape where the two anchors can disagree,
    and asserting equality with the tip here would still pass after a
    regression that anchored on the tip again."""
    baseline = git.capture_workspace_baseline(fx.worktree, base_ref="main")
    base_label = git.resolve_base_ref("main", fx.worktree)
    expected = subprocess.run(
        ["git", "merge-base", base_label, "HEAD"],
        cwd=fx.worktree,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    ).stdout.strip()
    assert baseline.head == expected


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
    """``capture_workspace_baseline`` wraps neither ``resolve_base_ref``
    nor ``merge_base_ref`` in a try/except of its own: both of those
    refuse by RETURNING a fallback value on an ordinary git failure, so
    a genuine programming error - the wrong arity here - is not a
    candidate for the HEAD fallback and must propagate instead."""

    def boom(*_args: object, **_kwargs: object) -> str:
        raise TypeError("wrong arity")

    monkeypatch.setattr(git, "merge_base_ref", boom)
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


def test_the_no_tests_sentence_survives_the_2000_character_store(tmp_path: Path) -> None:
    """All three contract callers store output[:2000], so the sentence
    goes first or a long install log pushes it out of the record
    entirely."""
    passed, output = contract._run_tests(
        tmp_path, 'python3 -c "print(chr(120)*2500)"; exit 5', 60.0
    )
    assert passed is False
    assert len(output) > 2000
    assert "no tests were collected" in output[:2000]


def test_a_real_failure_is_not_called_an_empty_collection(tmp_path: Path) -> None:
    """Exit 5 is the only code that means nothing ran. A condition
    widened to `!= 0` staples pytest's no-collection sentence onto
    every genuine failure and pushes 200 characters of the real
    evidence past the 2000-character store. 5 is the only code that
    means nothing ran, so the condition is wrong widened in either
    direction: too narrow misses real no-collection exits, too wide
    (`>= 5`) staples the no-collection sentence onto an unrelated
    failure such as exit 7."""
    passed, output = contract._run_tests(tmp_path, "exit 1", 30.0)
    assert passed is False
    assert "no tests were collected" not in output

    passed_high, output_high = contract._run_tests(tmp_path, "exit 7", 30.0)
    assert passed_high is False
    assert "no tests were collected" not in output_high


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


def _build_sibling_merges_mid_run(root: Path) -> StaleBase:
    """Case B (#435 review, finding A0): a SIBLING component's PR
    squash-merges on the remote AFTER this worktree is cut, not before.

    Every fixture above cuts the worktree AFTER the merge that advances
    ``origin/main`` (:func:`_build_stale_base`, lines above), so the base
    branch's current TIP and its MERGE BASE with this worktree's HEAD are
    the same commit there - nothing in this file so far can tell a
    tip-anchored guard from a merge-base-anchored one apart. This
    fixture cuts the worktree FIRST and merges the sibling second, which
    is the shape a factory run at ``max_parallel > 1`` produces routinely:
    ``refs/remotes/origin/*`` is fetched into every worktree that shares
    the repository, so the tip moves under a component that is still
    mid-loop.
    """
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

    # This engineer's worktree is cut FIRST, before any sibling merges.
    wt = root / "wt"
    git_in(repo, "worktree", "add", str(wt), "-b", "comp/storage", "origin/main")

    # The sibling tier merges on the remote WHILE this worktree is
    # running - a squash merge directly on the bare remote, exactly as a
    # GitHub squash merge does.
    sibling = root / "sibling-clone"
    git_in(root, "clone", str(remote), str(sibling))
    set_identity(sibling)
    (sibling / "src").mkdir(parents=True)
    (sibling / "src" / "rules.py").write_text("RULES = []\n", encoding="utf-8")
    git_in(sibling, "add", "-A")
    git_in(sibling, "commit", "-q", "-m", "squash: link-rules")
    git_in(sibling, "push", "origin", "main")

    # What the factory does after every sibling's PR merge: update the
    # remote-tracking ref, touch nothing else. `wt` is a linked worktree
    # of `repo` and shares `refs/remotes/*`, so its own view of
    # `origin/main` has now moved without `wt` doing anything.
    error = git.fetch_base_branch("main", repo)
    assert error is None, error

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


def test_the_in_loop_guard_blames_no_file_from_a_sibling_that_merges_mid_run(
    tmp_path: Path,
) -> None:
    """The direction #435's own fixture (above) cannot cover: the base
    branch's tip moving AHEAD of this worktree's own fork point, not
    behind it. A tip-anchored baseline sees the sibling's ``src/rules.py``
    as something this worktree removed (it is present at the new tip and
    absent in this worktree's history) and blames it on this engineer
    alongside the real change. Only the merge base - the commit this
    worktree actually forked from - excludes it."""
    fx = _build_sibling_merges_mid_run(tmp_path)
    baseline = git.capture_workspace_baseline(fx.worktree, base_ref="main")

    (fx.worktree / "src").mkdir(parents=True, exist_ok=True)
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
