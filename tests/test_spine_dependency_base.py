"""A dependent starts from its dependencies' code and is judged on its own change (#543).

Real git, real ``bash -lc`` engineers, the real ``run_factory``; nothing is
mocked. Each engineer records whether the file its dependency wrote is in
its worktree, then commits a file named after itself.

Per-component PR mode gives a dependent its dependency's code by merging the
dependency into the base before the dependent is cut from it. Under
``create_prs=False``, or with no gh, nothing merges: a completed dependency's
code exists only on its own branch, so the dependent's branch must take it,
and the dependent must then be judged from the commit it started at rather
than from the base branch, or the dependency's files count as its own. In
single_pr mode every component shares one branch, so a later component starts
on its predecessors' commits and must be judged from the same point.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from kstrl.factory import FactoryResult, run_factory
from kstrl.manifest import Manifest
from kstrl.security import SecurityConfig
from kstrl.timeout import TimeoutConfig
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.spine_utils import (
    base_config,
    component,
    factory_config,
    git,
    init_kstrl_repo,
    make_manifest,
)

pytestmark = pytest.mark.spine

SHARED_BRANCH = "kstrl/factory/spine"

# Logs whether the dependency's file is present, runs the optional extra
# step, then commits <comp>.txt. The worktree basename is the component id.
_ENGINEER = textwrap.dedent("""\
    comp="$(basename "$PWD")"
    if [ -f a.txt ]; then echo "$comp sees a.txt" >> "$ENGINEER_LOG"
    else echo "$comp lacks a.txt" >> "$ENGINEER_LOG"; fi
    {extra}
    echo "$comp" > "$comp.txt"
    git add -A
    git commit -q -m "$comp" || true
    echo '<promise>COMPLETE</promise>'
""")

# What the paid run's engineer did to get its dependency's code (P-A,
# deviation 7). A no-op when the code is already there.
_FETCH_DEPENDENCY = '[ "$comp" = b ] && git merge -q --no-edit kstrl/factory/a'

# One file outside every component's scope, so Phase 1 names its base.
_STRAY = '[ "$comp" = b ] && echo stray > stray.txt'

# b's first attempt outlives the component wall clock, so the pipeline
# retries it on a branch cut again from the base (fresh_from_base). The
# marker file sits outside the worktree, so the retry does not sleep.
_FIRST_B_ATTEMPT_TIMES_OUT = (
    'if [ "$comp" = b ] && [ ! -f "$ENGINEER_LOG.b-slept" ]; then '
    'touch "$ENGINEER_LOG.b-slept"; sleep 30; fi'
)

# a1 and a2 write the same file differently: they cannot both be merged.
_CONFLICT = 'case "$comp" in a1|a2) echo "$comp" > shared.txt;; esac'

# gh stub that REALLY squash-merges the PR's head into the bare origin, so
# the per-component PR path runs end to end.
_MERGING_GH = textwrap.dedent("""\
    #!/bin/sh
    state="$GH_STATE"
    if [ "$1" = "auth" ]; then exit 0; fi
    if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
      n=$(( $(cat "$state/n" 2>/dev/null || echo 40) + 1 ))
      echo "$n" > "$state/n"
      for arg in "$@"; do
        case "$arg" in --head=*) echo "${arg#--head=}" > "$state/head.$n";; esac
      done
      echo "https://github.com/spine/repo/pull/$n"; exit 0
    fi
    if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
      n="$3"
      [ -f "$state/merged.$n" ] && exit 0
      # gh runs in the project root, whose config holds the identity
      # tests.helpers.gitrepo.set_identity wrote; the clone includes it.
      identity="include.path=$PWD/.git/config"
      head=$(cat "$state/head.$n"); clone="$state/clone.$n"
      git clone -q -b main "$GH_ORIGIN" "$clone" || exit 1
      cd "$clone" || exit 1
      git -c "$identity" merge -q --squash "origin/$head" || exit 1
      git -c "$identity" commit -q -m "squash $head" || exit 1
      git push -q origin HEAD:main || exit 1
      git rev-parse HEAD > "$state/merged.$n"; exit 0
    fi
    if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
      if [ -f "$state/merged.$3" ]; then
        printf '{"state": "MERGED", "mergeCommit": {"oid": "%s"}}\\n' "$(cat "$state/merged.$3")"
      else
        printf '{"state": "OPEN", "mergeCommit": null}\\n'
      fi
      exit 0
    fi
    exit 0
""")


def _project(
    tmp_path: Path,
    deps: dict[str, list[str]],
    *,
    single_pr: bool = False,
    with_origin: bool = False,
) -> tuple[Path, Manifest, Path | None]:
    """A repo whose components each own exactly ``<id>.txt``."""
    root = tmp_path / "repo"
    origin = init_kstrl_repo(root, tuple(deps), with_origin=with_origin)
    components = []
    for comp_id, comp_deps in deps.items():
        comp = component(comp_id, comp_deps)
        if single_pr:
            comp.branch_name = SHARED_BRANCH
        prd_path = root / comp.prd_path
        prd = json.loads(prd_path.read_text())
        prd["allowedPaths"] = [f"{comp_id}.txt"]
        prd["branchName"] = comp.branch_name
        prd_path.write_text(json.dumps(prd))
        components.append(comp)
    manifest = make_manifest(components)
    manifest.single_pr = single_pr
    return root, manifest, origin


def _run(
    tmp_path: Path,
    root: Path,
    manifest: Manifest,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra: str = "",
    **config: Any,
) -> FactoryResult:
    monkeypatch.setenv("ENGINEER_LOG", str(tmp_path / "engineer.log"))
    return run_factory(
        manifest,
        factory_config(
            single_pr=manifest.single_pr,
            progress_log_path=tmp_path / "progress.jsonl",
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=True,
                check_bad_patterns=False,
                subprocess_timeout=10.0,
            ),
            **config,
        ),
        base_config(root, _ENGINEER.format(extra=extra or ":")),
        PlainUI(no_color=True),
        root,
        manifest_path=tmp_path / "manifest.json",
    )


def _engineer_log(tmp_path: Path) -> list[str]:
    log = tmp_path / "engineer.log"
    return log.read_text().splitlines() if log.exists() else []


def _scope_failures(tmp_path: Path, comp_id: str) -> list[str]:
    """Every failure text of every failed verification of ``comp_id``."""
    events = [
        json.loads(line)
        for line in (tmp_path / "progress.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return [
        failure
        for e in events
        if e["event"] == "verification_result"
        and e.get("component") == comp_id
        and not e["data"]["passed"]
        for failure in e["data"]["failures"]
    ]


def _statuses(tmp_path: Path) -> dict[str, str]:
    final = Manifest.load(tmp_path / "manifest.json")
    return {c.id: c.status for c in final.components}


def _without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    kept = [p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "gh").exists()]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))


@pytest.mark.parametrize("create_prs", [False, True], ids=["no_prs", "no_gh"])
def test_dependent_worktree_holds_its_dependency_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_prs: bool,
) -> None:
    """Under --no-prs, and with PRs on but no gh, nothing merges a into the
    base, so b's branch must take a's branch itself."""
    if create_prs:
        _without_gh(monkeypatch)
    root, manifest, _ = _project(tmp_path, {"a": [], "b": ["a"]})

    _run(tmp_path, root, manifest, monkeypatch, create_prs=create_prs)

    assert _statuses(tmp_path) == {"a": "completed", "b": "completed"}
    b_lines = [line for line in _engineer_log(tmp_path) if line.startswith("b ")]
    assert b_lines and set(b_lines) == {"b sees a.txt"}, _engineer_log(tmp_path)
    assert "a.txt" in git("ls-tree", "--name-only", "kstrl/factory/b", cwd=root).split()


def test_dependent_is_judged_on_its_own_change_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paid run's shape: b's engineer merges a's branch itself. diff_scope
    used to count a.txt as b's and fail it; b owns only b.txt."""
    root, manifest, _ = _project(tmp_path, {"a": [], "b": ["a"]})

    _run(tmp_path, root, manifest, monkeypatch, extra=_FETCH_DEPENDENCY, create_prs=False)

    assert _scope_failures(tmp_path, "b") == []
    assert _statuses(tmp_path) == {"a": "completed", "b": "completed"}


@pytest.mark.parametrize("max_parallel", [1, 2], ids=["inline", "pool"])
def test_dependent_is_judged_from_the_commit_it_started_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_parallel: int,
) -> None:
    """A real violation still fails, names only b's own stray file, and names
    the commit b's branch was cut at: a's tip, which b fast-forwarded to.

    The message is written by the in-loop guard inside the worker, so both
    submit paths (in-process and process pool) are driven."""
    root, manifest, _ = _project(tmp_path, {"a": [], "b": ["a"]})

    _run(
        tmp_path,
        root,
        manifest,
        monkeypatch,
        extra=_STRAY,
        create_prs=False,
        max_parallel=max_parallel,
    )

    assert _statuses(tmp_path) == {"a": "completed", "b": "failed"}
    failures = _scope_failures(tmp_path, "b")
    assert failures, "b's stray.txt was not judged at all"
    a_tip = git("rev-parse", "kstrl/factory/a", cwd=root)
    for text in failures:
        assert "stray.txt" in text
        assert "a.txt" not in text
        assert f"Base branch: {a_tip} " in text


def test_retry_keeps_the_base_its_branch_was_cut_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 1 commits stray.txt; the retry keeps the branch and adds
    nothing. Judged from where the branch was CUT, stray.txt still counts;
    judged from where the retry started, the diff would be empty and b
    would complete with an out-of-scope file on its branch."""
    root, manifest, _ = _project(tmp_path, {"a": [], "b": ["a"]})

    _run(
        tmp_path,
        root,
        manifest,
        monkeypatch,
        extra=_STRAY,
        create_prs=False,
        max_retries=1,
    )

    assert _statuses(tmp_path) == {"a": "completed", "b": "failed"}
    failures = _scope_failures(tmp_path, "b")
    assert len(failures) == 2, failures
    assert all("stray.txt" in text for text in failures)


def test_single_pr_later_component_is_judged_on_its_own_change_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """single_pr: c is independent of a but starts on the shared branch after
    a's commit. diff_scope used to count a.txt as c's."""
    root, manifest, _ = _project(tmp_path, {"a": [], "c": []}, single_pr=True)

    _run(tmp_path, root, manifest, monkeypatch, create_prs=False)

    assert _scope_failures(tmp_path, "c") == []
    assert _statuses(tmp_path) == {"a": "completed", "c": "completed"}


def test_per_component_pr_mode_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mode that already worked: a's PR squash-merges into origin/main,
    b is cut from origin/main, sees a.txt, and is still judged against
    origin/main by name. Green before #543 and after."""
    root, manifest, origin = _project(tmp_path, {"a": [], "b": ["a"]}, with_origin=True)
    assert origin is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_MERGING_GH)
    gh.chmod(0o755)
    state = tmp_path / "gh-state"
    state.mkdir()
    monkeypatch.setenv("GH_STATE", str(state))
    monkeypatch.setenv("GH_ORIGIN", str(origin))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    _run(tmp_path, root, manifest, monkeypatch, extra=_STRAY, create_prs=True)

    assert _statuses(tmp_path) == {"a": "completed", "b": "failed"}
    assert set(line for line in _engineer_log(tmp_path) if line.startswith("b ")) == {
        "b sees a.txt"
    }
    failures = _scope_failures(tmp_path, "b")
    assert failures
    for text in failures:
        assert "Base branch: origin/main " in text
        assert "stray.txt" in text
        assert "a.txt" not in text


def test_conflicting_dependencies_fail_the_dependent_at_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a1 and a2 cannot both be merged. b fails before its engineer runs,
    named, and its worktree does not outlive the run."""
    root, manifest, _ = _project(tmp_path, {"a1": [], "a2": [], "b": ["a1", "a2"]})
    for comp_id in ("a1", "a2"):
        prd_path = root / "scripts" / "kstrl" / "feature" / comp_id / "prd.json"
        prd = json.loads(prd_path.read_text())
        prd["allowedPaths"] = [f"{comp_id}.txt", "shared.txt"]
        prd_path.write_text(json.dumps(prd))

    _run(tmp_path, root, manifest, monkeypatch, extra=_CONFLICT, create_prs=False)

    final = Manifest.load(tmp_path / "manifest.json")
    b = final.get_component("b")
    assert b is not None
    assert (b.status, b.failed_phase, b.failed_check) == (
        "failed",
        "provisioning",
        "worktree_setup",
    )
    assert "kstrl/factory/a2" in b.error
    assert not [line for line in _engineer_log(tmp_path) if line.startswith("b ")]
    listing = git("worktree", "list", "--porcelain", cwd=root)
    assert [line for line in listing.splitlines() if line.startswith("worktree ")] == [
        f"worktree {root}"
    ]


def test_a_fresh_base_retry_records_the_base_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """b depends on a1 and a2, so provisioning makes a merge commit. b's first
    attempt hits the component timeout; the retry deletes b's branch and cuts
    it again, which makes a NEW merge commit at least the timeout later.
    Judged from the old merge commit, whose merge bases with the new branch
    are a1's tip and a2's tip, one dependency's file counts as b's."""
    root, manifest, _ = _project(tmp_path, {"a1": [], "a2": [], "b": ["a1", "a2"]})

    _run(
        tmp_path,
        root,
        manifest,
        monkeypatch,
        extra=_FIRST_B_ATTEMPT_TIMES_OUT,
        create_prs=False,
        max_retries=1,
        timeout_config=TimeoutConfig(component_total=5.0),
    )

    assert (tmp_path / "engineer.log.b-slept").exists()
    assert _scope_failures(tmp_path, "b") == []
    assert _statuses(tmp_path) == {"a1": "completed", "a2": "completed", "b": "completed"}
    on_b = git("ls-tree", "--name-only", "kstrl/factory/b", cwd=root).split()
    assert {"a1.txt", "a2.txt", "b.txt"} <= set(on_b), on_b


def test_reviewers_are_told_the_commit_the_dependent_started_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code reviewer and the security reviewer each diff against the
    commit their prompt names. For b that must be the commit b started at,
    a's tip, or both reviewers read a's code as b's change.

    Both reviewers are a command that saves its prompt (stdin) and says
    nothing, which the advisory modes let through."""
    root, manifest, _ = _project(tmp_path, {"a": [], "b": ["a"]})
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    save_prompt = f'cat > "{prompts}/$(basename "$PWD").$$"'

    _run(
        tmp_path,
        root,
        manifest,
        monkeypatch,
        create_prs=False,
        review_mode="advisory",
        review_agent_cmd=save_prompt,
        security_config=SecurityConfig(mode="advisory", agent_cmd=save_prompt),
    )

    assert _statuses(tmp_path) == {"a": "completed", "b": "completed"}
    a_tip = git("rev-parse", "kstrl/factory/a", cwd=root)
    main_tip = git("rev-parse", "main", cwd=root)
    b_prompts = [p.read_text() for p in prompts.glob("b.*")]
    assert len(b_prompts) == 2, sorted(p.name for p in prompts.iterdir())
    for text in b_prompts:
        assert a_tip in text
        assert main_tip not in text
