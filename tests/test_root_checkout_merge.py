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

#569 is the same census with ``ks init`` output left uncommitted. kstrl
used to copy prompt.md, CLAUDE.md and AGENTS.md into the worktree, the
branch committed them, and the merge refused on the root checkout's
untracked copies. kstrl now reads them from the root checkout and copies
only the PRD seed.

#585 is the codebase map. The engineer prompt told the engineer to append
its facts to the map at its path in the worktree; with ``ks init`` output
uncommitted the base branch does not track it there, so the branch created
it and the merge refused. The engineer now reads the root checkout's map
and writes its facts to its own progress log, and the stub here follows
the prompt it receives rather than a path this file names.
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

#: What the engineer that follows the prompt records as a durable fact.
FACT = "FACT-585"

#: Appended to the root checkout's codebase map after ``ks init`` and never
#: committed, so only a read of the root checkout's copy can see it.
MAP_MARKER = "MAP-MARKER-585"

#: The engineer's side of the prompt, run by the stub with the prompt on
#: stdin. It reads the file step 4 names into ``argv[1]`` (or writes
#: ``ABSENT``) and appends the fact to the file step 10 names. Each is the
#: first backquoted absolute path in that step's text, so the stub goes
#: wherever the prompt sends it and this file names neither path.
FOLLOW_PROMPT = """\
import re
import sys
from pathlib import Path

prompt = sys.stdin.read()
task = prompt[prompt.index("## Your Task (one iteration)"):]


def step(n):
    start = task.index(f"\\n{n}. ")
    return task[start : task.index(f"\\n{n + 1}. ", start)]


def named_path(text):
    return Path(next(p for p in re.findall(r"`([^`]+)`", text) if p.startswith("/")))


seen = named_path(step(4))
Path(sys.argv[1]).write_text(
    seen.read_text(encoding="utf-8") if seen.is_file() else "ABSENT\\n", encoding="utf-8"
)
with named_path(step(10)).open("a", encoding="utf-8") as f:
    f.write(sys.argv[2] + "\\n")
"""

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


def _stub_agent(
    tmp_path: Path,
    *,
    rewrite_criteria: bool = False,
    prompt_dump: Path | None = None,
    map_seen: Path | None = None,
) -> Path:
    """The architect outside a worktree; inside one, an engineer that marks
    its story done, writes code and its progress log, and commits
    everything with ``git add -A`` the way a real engineer does. With
    ``rewrite_criteria`` it also replaces its story's acceptance criteria,
    which no engineer may do. With ``prompt_dump`` the engineer writes the
    prompt it received there. With ``map_seen`` it also follows steps 4
    and 10 of that prompt (``FOLLOW_PROMPT``): what it read at step 4 goes
    to ``map_seen``, and ``FACT`` goes where step 10 says."""
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
    follow = bin_dir / "follow_prompt.py"
    follow.write_text(FOLLOW_PROMPT, encoding="utf-8")
    follow_steps = (
        f"printf '%s\\n' \"$prompt\" | '{sys.executable}' '{follow}' '{map_seen}' {FACT}"
        if map_seen is not None
        else ""
    )
    agent = bin_dir / "agent"
    agent.write_text(
        textwrap.dedent(f"""\
            #!/bin/bash
            prompt=$(cat)
            case "$(pwd)" in
              */.kstrl/worktrees/*) ;;
              *) echo '{ARCHITECT_REPLY}'; exit 0 ;;
            esac
            set -e
            printf '%s\\n' "$prompt" > '{prompt_dump or os.devnull}'
            feature=scripts/kstrl/feature/{COMP}
            '{sys.executable}' '{mark_done}' "$feature/prd.json" {int(rewrite_criteria)}
            mkdir -p src
            echo 'print("hello")' > src/greeter.py
            printf '## Self-Critique\\nnone\\n' >> scripts/kstrl/feature/{COMP}/progress.txt
            {follow_steps}
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


@pytest.mark.parametrize("commit_init", [True, False], ids=["init-committed", "init-uncommitted"])
def test_a_component_branch_merges_into_the_checkout_kstrl_ran_from(
    tmp_path: Path, commit_init: bool
) -> None:
    """With ``commit_init`` false, what ``ks init`` wrote is untracked in the
    root checkout, and a file kstrl copied into the worktree would be
    committed by the branch and refuse the merge (#569)."""
    root = _initialised_project(tmp_path, commit_init=commit_init)

    run = _factory(root, _stub_agent(tmp_path))
    assert run.returncode == 0, run.stdout

    untracked, committed = _untracked_and_committed(root)
    # Both sides are non-empty, so the empty intersection below is a
    # comparison and not two empty sets agreeing.
    assert "scripts/kstrl/manifest.json" in untracked, untracked
    if not commit_init:
        # The arm is about these files, so prove they are untracked here.
        assert {"AGENTS.md", "CLAUDE.md", "scripts/kstrl/prompt.md"} <= untracked, untracked
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


def test_uncommitted_ks_init_output_does_not_fail_phase_1(tmp_path: Path) -> None:
    """With ``ks init`` output uncommitted, a file kstrl put in the worktree
    is committed by the branch and sits outside the component's
    ``allowedPaths``, so Phase 1's diff_scope failed a component whose
    engineer did nothing wrong (#569). Every other check passes here."""
    root = _initialised_project(tmp_path, commit_init=False)

    run = _factory(
        root,
        _stub_agent(tmp_path),
        "--test-command",
        "true",
        "--typecheck-command",
        "true",
        "--lint-command",
        "true",
    )

    (comp,) = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").components
    assert (comp.status, comp.failed_check) == ("completed", ""), run.stdout
    assert run.returncode == 0, run.stdout


def test_the_engineer_reads_prompt_and_claude_md_from_the_root_checkout(tmp_path: Path) -> None:
    """kstrl no longer copies prompt.md and CLAUDE.md into the worktree,
    so the engineer's prompt must still carry both, read from the root
    checkout where they are uncommitted. The markers are appended after
    ``ks init``, so the harness DEFAULT_PROMPT fallback cannot supply them.
    Phase 1 is on so the #261 scrub runs: the stale ``Test`` bullet
    disagrees with the gate's ``true`` and must be dropped from the
    prompt, which it is only when the scrub reads the same CLAUDE.md the
    prompt carries. No exit-code assertion: on a tree that still copies
    the files, Phase 1 fails the run on diff_scope after the prompt was
    written, and this test is about the prompt."""
    root = _initialised_project(tmp_path, commit_init=False)
    with (root / "scripts" / "kstrl" / "prompt.md").open("a", encoding="utf-8") as f:
        f.write("\nPROMPT-MARKER-569\n")
    with (root / "CLAUDE.md").open("a", encoding="utf-8") as f:
        f.write("\nCLAUDE-MARKER-569\n- **Test**: `pytest STALE-569`\n")
    dump = tmp_path / "engineer-prompt.txt"

    run = _factory(
        root,
        _stub_agent(tmp_path, prompt_dump=dump),
        "--test-command",
        "true",
        "--typecheck-command",
        "true",
        "--lint-command",
        "true",
    )

    assert dump.is_file(), run.stdout
    prompt = dump.read_text(encoding="utf-8")
    assert "PROMPT-MARKER-569" in prompt, prompt[:2000]
    assert "# Project Context (from CLAUDE.md)" in prompt, prompt[:2000]
    assert "CLAUDE-MARKER-569" in prompt, prompt[:2000]
    assert "STALE-569" not in prompt, prompt[:2000]


@pytest.mark.parametrize("commit_init", [True, False], ids=["init-committed", "init-uncommitted"])
def test_the_fact_an_engineer_records_merges_with_its_branch(
    tmp_path: Path, commit_init: bool
) -> None:
    """The census over an engineer that follows the prompt's step 10. With
    ``ks init`` output uncommitted, step 10 used to send the fact to the
    codebase map in the worktree, which the base branch does not track, so
    the branch created the map and ``git merge`` refused on the root
    checkout's untracked copy (#585). The fact now lands in the component's
    own progress log: in the branch diff, which is what the knowledge
    distiller reads, and on the base branch once the branch merges."""
    root = _initialised_project(tmp_path, commit_init=commit_init)

    run = _factory(root, _stub_agent(tmp_path, map_seen=tmp_path / "map-seen.txt"))
    assert run.returncode == 0, run.stdout

    untracked, committed = _untracked_and_committed(root)
    # Both sides are non-empty, so the empty intersection is a comparison.
    assert "scripts/kstrl/manifest.json" in untracked, untracked
    assert f"scripts/kstrl/feature/{COMP}/progress.txt" in committed, committed
    assert untracked & committed == set(), sorted(untracked & committed)
    assert FACT in _git(root, "diff", f"main...{BRANCH}").stdout

    merge = _git(root, "merge", "--no-edit", BRANCH)
    assert merge.returncode == 0, merge.stdout + merge.stderr
    holders = _git(root, "grep", "-l", FACT, "HEAD").stdout.splitlines()
    assert holders == [f"HEAD:scripts/kstrl/feature/{COMP}/progress.txt"], holders


@pytest.mark.parametrize("commit_init", [True, False], ids=["init-committed", "init-uncommitted"])
def test_the_engineer_reads_the_codebase_map_from_the_root_checkout(
    tmp_path: Path, commit_init: bool
) -> None:
    """Step 4's map is the root checkout's, the file ``ks understand``
    writes and the architect reads. In the worktree it is missing while
    ``ks init`` output is uncommitted, and it lacks an edit the operator
    has not committed. With the root map edited and uncommitted, the
    branch must also still merge: a branch that changed the map would be
    refused on the operator's local change."""
    root = _initialised_project(tmp_path, commit_init=commit_init)
    with (root / "scripts" / "kstrl" / "codebase_map.md").open("a", encoding="utf-8") as f:
        f.write(f"\n{MAP_MARKER}\n")
    map_seen = tmp_path / "map-seen.txt"

    run = _factory(root, _stub_agent(tmp_path, map_seen=map_seen))
    assert run.returncode == 0, run.stdout

    assert MAP_MARKER in map_seen.read_text(encoding="utf-8")
    merge = _git(root, "merge", "--no-edit", BRANCH)
    assert merge.returncode == 0, merge.stdout + merge.stderr


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


def _stub_agent_in_root(tmp_path: Path, *, rewrite_criteria: bool) -> Path:
    """The stub for ``--no-worktrees``, where the engineer runs in the root
    checkout too: it is the architect until the run has seeded the
    engineer's PRD at the feature path, and the engineer after. It reuses
    ``_stub_agent``'s ``mark_done.py`` and commits only its own files."""
    agent = _stub_agent(tmp_path, rewrite_criteria=rewrite_criteria)
    mark_done = agent.parent / "mark_done.py"
    agent.write_text(
        textwrap.dedent(f"""\
            #!/bin/bash
            cat > /dev/null
            feature=scripts/kstrl/feature/{COMP}
            if [ ! -f "$feature/prd.json" ]; then echo '{ARCHITECT_REPLY}'; exit 0; fi
            set -e
            '{sys.executable}' '{mark_done}' "$feature/prd.json" {int(rewrite_criteria)}
            mkdir -p src
            echo 'print("hello")' > src/greeter.py
            printf '## Self-Critique\\nnone\\n' >> "$feature/progress.txt"
            git add src "$feature"
            git commit -q -m 'feat: US-001 hello'
            echo '<promise>COMPLETE</promise>'
        """),
        encoding="utf-8",
    )
    return agent


@pytest.mark.parametrize("rewrite", [False, True])
def test_without_worktrees_phase_1_still_compares_with_the_planned_copy(
    tmp_path: Path, rewrite: bool
) -> None:
    """Under ``--no-worktrees`` the run seeds the engineer's PRD at
    ``prdPath`` in the root checkout itself, so a root copy exists there.
    ``pre_run_prd_path`` must still prefer the planned copy: preferring the
    root copy would compare the engineer's PRD with itself, and a
    rewritten criterion would pass Phase 1."""
    root = _initialised_project(tmp_path)

    run = _factory(
        root,
        _stub_agent_in_root(tmp_path, rewrite_criteria=rewrite),
        "--test-command",
        "true",
        "--typecheck-command",
        "true",
        "--lint-command",
        "true",
        "--no-worktrees",
    )

    (comp,) = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").components
    assert comp.status == ("failed" if rewrite else "completed"), run.stdout
    assert comp.failed_check == ("prd_stories" if rewrite else ""), run.stdout
