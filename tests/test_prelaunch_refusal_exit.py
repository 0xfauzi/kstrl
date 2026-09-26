"""A refusal before any work starts exits 2, wherever kstrl decides it (#531).

The #452 contract is 0 ok, 1 a finding, 2 could not do what was asked.
These refusals are decided outside ``kstrl/cli.py`` (in ``kstrl/factory.py``
and ``kstrl/loop.py``), so each is driven through the real ``ks`` entry
point in a child process, run by ``ks serve``'s own supervised runner, and
judged by the exit code and output that child produced. The static census
in ``tests/test_cli_conventions.py`` holds the same contract for the next
refusal nobody has written a process test for.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.manifest import Component, Manifest
from kstrl.serve import RunOutcome, Verdict, classify_run, run_supervised
from tests.helpers.gitrepo import git_in, set_identity
from tests.test_cli_conventions import _child_env

MANIFEST = "scripts/kstrl/manifest.json"

#: (component id, dependencies) per component, and the line the refusal prints.
INVALID_GRAPHS: dict[str, tuple[list[tuple[str, list[str]]], str]] = {
    "cycle": ([("a", ["b"]), ("b", ["a"])], "Dependency cycle detected in component graph"),
    "unknown-dependency": (
        [("a", ["ghost"])],
        "Component 'a' depends on unknown component 'ghost'",
    ),
    "duplicate-id": ([("a", []), ("a", [])], "Duplicate component ID: a"),
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on `main` with one commit, and nothing kstrl wrote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    set_identity(repo)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    git_in(repo, "add", "f.txt")
    git_in(repo, "commit", "-q", "-m", "init")
    return repo


def _ks(repo: Path, *argv: str) -> RunOutcome:
    """Run `ks` the way `ks serve` runs a factory: own process group, bounded."""
    return run_supervised(
        [sys.executable, "-m", "kstrl", *argv],
        cwd=repo,
        env=_child_env(),
        timeout_seconds=120,
    )


def _write_graph(repo: Path, shape: str) -> None:
    components = [
        Component(cid, cid.upper(), "", deps, f"{cid}.json", f"b/{cid}")
        for cid, deps in INVALID_GRAPHS[shape][0]
    ]
    Manifest(
        version="1",
        spec_file="spec.md",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=components,
    ).save(repo / MANIFEST)


def _factory(repo: Path) -> RunOutcome:
    return _ks(
        repo,
        "factory",
        "--manifest",
        MANIFEST,
        "--agent-cmd",
        "true",
        "--yes",
        "--ui",
        "plain",
        "--no-prs",
    )


@pytest.mark.parametrize("shape", sorted(INVALID_GRAPHS))
def test_ks_factory_refuses_an_invalid_dependency_graph_with_exit_2(repo: Path, shape: str) -> None:
    _write_graph(repo, shape)

    run = _factory(repo)

    assert not run.timed_out
    assert run.returncode == 2, run.output_tail
    assert INVALID_GRAPHS[shape][1] in run.output_tail
    assert "Traceback" not in run.output_tail


def test_ks_serve_files_the_graph_refusal_as_a_refusal_not_an_interrupted_run(repo: Path) -> None:
    _write_graph(repo, "cycle")

    outcome = classify_run(repo, run=_factory(repo), manifest_path=repo / MANIFEST)

    assert outcome.verdict == Verdict.UNCLASSIFIABLE
    assert "refusing to guess which refusal it was" in outcome.reason
    assert "interrupted run" not in outcome.reason


def test_ks_understand_refuses_a_branch_it_cannot_check_out_with_exit_2(repo: Path) -> None:
    run = _ks(
        repo, "understand", "--branch", "bad..name", "--agent-cmd", "false", "--ui", "plain", "1"
    )

    assert not run.timed_out
    assert run.returncode == 2, run.output_tail
    assert "Failed to checkout branch: bad..name" in run.output_tail


def test_ks_understand_refuses_when_the_guard_baseline_cannot_be_taken_with_exit_2(
    repo: Path,
) -> None:
    """A staged path that is not utf-8 is a diff kstrl cannot read (#416),
    so the scope guard has no before-picture and the loop must not start.
    Staged through the index alone: the path never touches the filesystem."""
    blob = subprocess.run(
        ["git", "hash-object", "-w", "f.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    git_in(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},bad\udcff.txt")

    run = _ks(repo, "understand", "--branch", "", "--agent-cmd", "false", "--ui", "plain", "1")

    assert not run.timed_out
    assert run.returncode == 2, run.output_tail
    assert "Guard baseline could not be taken" in run.output_tail


def test_ks_retry_refuses_a_graph_with_an_unknown_dependency_with_exit_2(repo: Path) -> None:
    """Only a hand-edited manifest reaches this: the factory refuses the
    graph before any component can fail."""
    failed = Component("a", "A", "", ["ghost"], "a.json", "b/a")
    failed.status = "failed"
    Manifest(
        version="1",
        spec_file="spec.md",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=[failed],
    ).save(repo / MANIFEST)

    run = _ks(repo, "retry", "a", "--yes", "--ui", "plain")

    assert not run.timed_out
    assert run.returncode == 2, run.output_tail
    assert "Component 'a' depends on unknown component 'ghost'" in run.output_tail
    assert "Traceback" not in run.output_tail
