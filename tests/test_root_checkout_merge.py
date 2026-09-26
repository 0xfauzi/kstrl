"""A finished component branch merges into the checkout kstrl ran from (#545).

Paid run P-A (#217) could not merge a component branch into the root
checkout after ``ks factory --no-prs``: git refused with "untracked working
tree files would be overwritten by merge:
scripts/kstrl/feature/<comp>/prd.json". kstrl had written that file
untracked in the root checkout, and the component branch commits the same
path. The operator had to move ``scripts/kstrl/feature`` aside first.

This drives the real ``ks init`` and the real ``ks factory --spec`` as
subprocesses, with one stub agent binary answering as the architect and as
the engineer, and then merges with real git. The census assertion compares
two sets the run produces, the untracked files in the root checkout and the
files the branch changes, so a file kstrl starts writing on either side
later is caught without being named here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kstrl.manifest import Manifest
from tests.helpers.gitrepo import git_in, set_identity
from tests.helpers.procs import kill_group

COMP = "greeter"
BRANCH = f"kstrl/factory/{COMP}"

#: What a repository whose ``ks init`` output was never committed still
#: collides on after #545, a disclosed residual: ``_run_component`` copies
#: these into the worktree when the base lacks them, and the engineer's
#: ``git add -A`` commits them. Not copying CLAUDE.md reaches ``loop.py``,
#: which reads it from the worktree.
UNCOMMITTED_INIT_RESIDUAL = frozenset({"AGENTS.md", "CLAUDE.md", "scripts/kstrl/prompt.md"})

#: What the stub architect returns: one component whose engineer writes
#: product code and its own feature subtree.
ARCHITECT_REPLY = (
    '{"components": [{"id": "greeter", "title": "Greeter", '
    '"description": "Say hello", "dependencies": [], '
    '"allowedPaths": ["src/", "scripts/kstrl/feature/greeter/"], '
    '"userStories": [{"id": "US-001", "title": "Hello", '
    '"acceptanceCriteria": ["prints hello"], "priority": 1, '
    '"passes": false, "notes": ""}]}], "spec_issues": [], "decisions": []}'
)


def _run(root: Path, *args: str, timeout: float = 240.0) -> subprocess.CompletedProcess[str]:
    """``python -m kstrl <args>`` in its own process group, stdout and
    stderr merged. The group is killed on timeout, so an agent the run
    spawned cannot outlive the bound."""
    env = {**os.environ, "KSTRL_NO_TUI": "1", "KSTRL_KNOWLEDGE_ENABLED": "0"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc.pid)
        out, _ = proc.communicate()
        raise AssertionError(f"ks {' '.join(args)} did not finish in {timeout}s:\n{out}") from None
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, "")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False
    )


def _stub_agent(tmp_path: Path, *, rewrite_criteria: bool = False) -> Path:
    """The architect outside a worktree; inside one, an engineer that marks
    its story done, writes code and its progress log, and commits
    everything with ``git add -A`` the way a real engineer does. With
    ``rewrite_criteria`` it also replaces its story's acceptance criteria,
    which no engineer may do."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    mark_done = bin_dir / "mark_done.py"
    mark_done.write_text(
        textwrap.dedent("""\
            import json
            import sys

            path, rewrite = sys.argv[1], sys.argv[2] == "1"
            with open(path, encoding="utf-8") as f:
                prd = json.load(f)
            for story in prd["userStories"]:
                story["passes"] = True
                if rewrite:
                    story["acceptanceCriteria"] = ["anything"]
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(prd, indent=2))
        """),
        encoding="utf-8",
    )
    agent = bin_dir / "agent"
    agent.write_text(
        textwrap.dedent(f"""\
            #!/bin/bash
            cat > /dev/null
            case "$(pwd)" in
              */.kstrl/worktrees/*) ;;
              *) echo '{ARCHITECT_REPLY}'; exit 0 ;;
            esac
            set -e
            feature=scripts/kstrl/feature/{COMP}
            '{sys.executable}' '{mark_done}' "$feature/prd.json" {int(rewrite_criteria)}
            mkdir -p src
            echo 'print("hello")' > src/greeter.py
            printf '## Self-Critique\\nnone\\n' >> scripts/kstrl/feature/{COMP}/progress.txt
            git add -A
            git commit -q -m 'feat: US-001 hello'
            echo '<promise>COMPLETE</promise>'
        """),
        encoding="utf-8",
    )
    agent.chmod(0o755)
    return agent


def _initialised_project(tmp_path: Path, *, commit_init: bool = True) -> Path:
    """A repository after ``ks init`` and a commit of what it scaffolded,
    which is the state P-A ran from. With ``commit_init`` false the commit
    holds only the spec, and what ``ks init`` wrote stays untracked."""
    root = tmp_path / "proj"
    root.mkdir()
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "seed")
    init = _run(root, "init", "--ui", "plain", "--no-color", str(root))
    assert init.returncode == 0, init.stdout
    (root / "spec.md").write_text("# Spec\nBuild a greeter.\n", encoding="utf-8")
    git_in(root, "add", *(["-A"] if commit_init else ["spec.md"]))
    git_in(root, "commit", "-q", "-m", "ks init")
    return root


def _factory(root: Path, agent: Path, *verify: str) -> subprocess.CompletedProcess[str]:
    """``ks factory --spec --no-prs``. ``verify`` replaces ``--no-verify``."""
    return _run(
        root,
        "factory",
        "--root",
        str(root),
        "--spec",
        str(root / "spec.md"),
        "--project-name",
        "demo",
        "--no-prs",
        "--agent-cmd",
        str(agent),
        "--review-mode",
        "skip",
        *(verify or ("--no-verify",)),
        "--contract-check",
        "skip",
        "--max-parallel",
        "1",
        "--max-retries",
        "0",
        "--ui",
        "plain",
        "--no-tui",
        "-y",
    )


def _untracked_and_committed(root: Path) -> tuple[set[str], set[str]]:
    """The untracked files in the root checkout, and the files the
    component branch changes."""
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    committed = _git(root, "diff", "--name-only", f"main...{BRANCH}").stdout.splitlines()
    return set(untracked), set(committed)


def test_a_component_branch_merges_into_the_checkout_kstrl_ran_from(tmp_path: Path) -> None:
    root = _initialised_project(tmp_path)

    run = _factory(root, _stub_agent(tmp_path))
    assert run.returncode == 0, run.stdout

    untracked, committed = _untracked_and_committed(root)
    # Both sides are non-empty, so the empty intersection below is a
    # comparison and not two empty sets agreeing.
    assert "scripts/kstrl/manifest.json" in untracked, untracked
    assert f"scripts/kstrl/feature/{COMP}/prd.json" in committed, committed
    assert f"scripts/kstrl/feature/{COMP}/progress.txt" in committed, committed
    assert untracked & committed == set(), (
        f"kstrl left these untracked in the root checkout and the component "
        f"branch commits them, so `git merge {BRANCH}` refuses: "
        f"{sorted(untracked & committed)}"
    )

    merge = _git(root, "merge", "--no-edit", BRANCH)
    assert merge.returncode == 0, merge.stdout + merge.stderr
    assert _git(root, "status", "--porcelain", "--untracked-files=no").stdout == ""


def test_uncommitted_ks_init_output_collides_only_on_the_disclosed_residual(
    tmp_path: Path,
) -> None:
    """The same census over a repository whose ``ks init`` output was never
    committed. Pinned exactly rather than marked xfail: an xfail would also
    absorb a NEW colliding file, such as the PRD #545 moved, and still read
    as expected. The day the residual is fixed this fails, and the set is
    emptied here."""
    root = _initialised_project(tmp_path, commit_init=False)

    run = _factory(root, _stub_agent(tmp_path))
    assert run.returncode == 0, run.stdout

    untracked, committed = _untracked_and_committed(root)
    assert f"scripts/kstrl/feature/{COMP}/prd.json" in committed, committed
    assert untracked & committed == UNCOMMITTED_INIT_RESIDUAL, sorted(untracked & committed)


@pytest.mark.parametrize("rewrite", [False, True])
def test_phase_1_compares_the_engineers_prd_with_the_planned_copy(
    tmp_path: Path, rewrite: bool
) -> None:
    """The planned copy is still the one Phase 1 judges the engineer's PRD
    against (#269). Read from a path that holds no copy, it would not load,
    the comparison would be empty, and a rewritten criterion would pass.
    Every other check passes here, so ``prd_stories`` failing is the
    comparison and nothing else."""
    root = _initialised_project(tmp_path)

    run = _factory(
        root,
        _stub_agent(tmp_path, rewrite_criteria=rewrite),
        "--test-command",
        "true",
        "--typecheck-command",
        "true",
        "--lint-command",
        "true",
    )

    (comp,) = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").components
    assert comp.status == ("failed" if rewrite else "completed"), run.stdout
    assert comp.failed_check == ("prd_stories" if rewrite else ""), run.stdout
    assert (run.returncode == 0) is not rewrite, run.stdout
