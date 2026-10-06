"""#696 slice 7: the architect proposes a ``[stack]``, and a person confirms it.

Before this slice ``ks decompose`` and ``ks factory --spec`` refused a
repository with no root build manifest from a table of manifest names
(#434), and no component could be scoped to one. That table was the last
language-specific refusal in front of the architect. Now the architect
says what the project is built with (``DECOMPOSE_PROMPT`` 4.0.0, owner
decision 3(a)): its ``stack`` is checked by ``stack.stack_errors`` inside
the decompose retry loop, and kstrl files it as a proposed
``stack_confirmation`` inbox item, which nothing approves but a person. A
component may be scoped to a build file; never to kstrl.toml, which holds
the confirmed stack. ``ks factory --spec`` measures the base before the
architect is paid, and the run reuses that reading.

End to end: ``python -m kstrl`` as a subprocess on a temp git repository,
with an agent command that answers each architect attempt from a file and
keeps the prompt it was given. Other test modules import ``MANIFESTS``,
``greenfield`` and ``run_ks`` from here.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind, ItemStatus
from kstrl.init_cmd import gitignore_block
from kstrl.manifest import Manifest
from kstrl.stack import load_stack
from kstrl.statedir import plan_prd_path
from tests.helpers.gitrepo import git_in, set_identity
from tests.helpers.stack_confirmation import TEST_INSTRUCTIONS, confirm_stack, write_stack
from tests.test_isolation_rung import runs_a_stack
from tests.test_prompt_record import _architect_then_engineer, _spec_project
from tests.test_stack_e2e import _spawn, _stack

#: One manifest per ecosystem the #434 issue named. Other modules seed a
#: repository with one of these; kstrl reads none of them.
MANIFESTS: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n',
    "package.json": '{"name": "demo"}\n',
    "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
    "go.mod": "module example.com/demo\n\ngo 1.22\n",
}

#: The stack the stub architect proposes: a toolchain kstrl names nowhere.
#: ``up`` is optional; it is here so the table the proposal carries is
#: shown to keep every key.
STACK: dict[str, Any] = {
    "instructions": "A Zig library built with zig build.",
    "setup": "zig build --fetch",
    "env": ["ZIG_GLOBAL_CACHE_DIR"],
    "checks": {"tests": "zig build test", "fmt": "zig fmt --check ."},
    "up": "zig build serve",
}


def greenfield(tmp_path: Path, *, extra: dict[str, str] | None = None) -> Path:
    """A repository with one commit holding a spec, a stray source file and
    kstrl's ignores, and no kstrl.toml. ``extra`` adds files at the root."""
    root = tmp_path / "greenfield"
    root.mkdir()
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    (root / "spec.md").write_text("# Spec\n\nBuild a Python CLI.\n", encoding="utf-8")
    (root / "legacy").mkdir()
    (root / "legacy" / "old_notes.py").write_text("x = 1\n", encoding="utf-8")
    for name, body in (extra or {}).items():
        (root / name).write_text(body, encoding="utf-8")
    (root / ".gitignore").write_text(gitignore_block(), encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "initial")
    return root


def run_ks(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """`python -m kstrl <args>` from ``root.parent``, stdout and stderr merged,
    with ``env`` on top of this process's environment.

    A `gh` that exits 1 goes first on PATH, so no run reaches the network.
    """
    bin_dir = root.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    child_env = {
        **os.environ,
        "KSTRL_NO_TUI": "1",
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        **(env or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root.parent,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
    )


def _component(allowed: list[str]) -> dict[str, Any]:
    return {
        "id": "comp-a",
        "title": "Library",
        "description": "The library",
        "dependencies": [],
        "allowedPaths": allowed,
        "userStories": [
            {
                "id": "US-001",
                "title": "Add two numbers",
                "acceptanceCriteria": ["WHEN given 2 and 3 THE SYSTEM SHALL return 5"],
                "priority": 1,
                "passes": False,
                "notes": "",
            }
        ],
    }


def _output(stack: object, allowed: list[str] | None = None) -> dict[str, Any]:
    return {
        "stack": stack,
        "spec_issues": [],
        "decisions": [],
        "components": [_component(allowed or ["src/", "scripts/kstrl/feature/comp-a/"])],
    }


def _architect(tmp_path: Path, *answers: dict[str, Any]) -> tuple[str, Path]:
    """An agent command whose call N saves its prompt to ``<prompts>/N.txt``
    and answers ``answers[N-1]``; a call past the end answers the last one."""
    home = tmp_path / "architect"
    prompts = home / "prompts"
    prompts.mkdir(parents=True)
    for number, answer in enumerate(answers, start=1):
        (home / f"{number}.json").write_text(json.dumps(answer), encoding="utf-8")
    (home / "last.json").write_text(json.dumps(answers[-1]), encoding="utf-8")
    script = home / "architect.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(( $(ls '{prompts}' | wc -l) + 1 ))\n"
        f"cat > '{prompts}/'\"$n\".txt\n"
        f"if [ -f '{home}/'\"$n\".json ]; then cat '{home}/'\"$n\".json; "
        f"else cat '{home}/last.json'; fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return f"sh {shlex.quote(str(script))}", prompts


def _decompose(root: Path, agent: str) -> subprocess.CompletedProcess[str]:
    return run_ks(
        root,
        "decompose",
        *("--spec", str(root / "spec.md"), "--project-name", "demo", "--root", str(root)),
        *("--agent-cmd", agent, "--ui", "plain", "--no-color", "--no-tui"),
    )


def _stack_items(root: Path) -> list[InboxItem]:
    box = Inbox(root, InboxConfig.load(root))
    return [i for i in box.items() if i.kind is ItemKind.STACK_CONFIRMATION]


def test_a_repository_with_no_build_manifest_gets_the_architects_stack_as_a_proposal(
    tmp_path: Path,
) -> None:
    """No manifest and no kstrl.toml: the architect runs, and its stack is
    filed for a person, unconfirmed. The table the item carries, written into
    kstrl.toml, is the stack the architect proposed, digest for digest."""
    root = greenfield(tmp_path)
    agent, prompts = _architect(tmp_path, _output(STACK))

    proc = _decompose(root, agent)

    assert proc.returncode == 0, proc.stdout
    assert len(list(prompts.iterdir())) == 1, proc.stdout
    (item,) = _stack_items(root)
    assert item.status is ItemStatus.OPEN, item
    assert item.title.startswith("Proposed [stack]"), item.title
    assert "the architect" in item.title, item.title
    assert f"inbox item {item.id[:8]}" in proc.stdout, proc.stdout
    manifest = Manifest.load(root / "scripts" / "kstrl" / "manifest.json")
    assert manifest.stack_digest == "", manifest.stack_digest
    (root / "kstrl.toml").write_text(str(item.evidence["toml"]), encoding="utf-8")
    adopted = load_stack(root)
    assert adopted is not None
    assert adopted.digest == item.evidence["stack_digest"]
    assert adopted.checks == (("tests", "zig build test"), ("fmt", "zig fmt --check ."))
    assert adopted.env == ("ZIG_GLOBAL_CACHE_DIR",)


def test_a_proposal_that_cannot_be_filed_is_printed_and_the_plan_stands(
    tmp_path: Path,
) -> None:
    """With the inbox off there is nowhere to file the proposal: kstrl says
    so and prints the table to put in kstrl.toml, and the plan is written."""
    root = greenfield(tmp_path)
    agent, _prompts = _architect(tmp_path, _output(STACK))

    proc = run_ks(
        root,
        "decompose",
        *("--spec", str(root / "spec.md"), "--project-name", "demo", "--root", str(root)),
        *("--agent-cmd", agent, "--ui", "plain", "--no-color", "--no-tui"),
        env={"KSTRL_INBOX_ENABLED": "0"},
    )

    assert proc.returncode == 0, proc.stdout
    assert "no item was filed: [inbox] is disabled" in proc.stdout, proc.stdout
    assert '"tests" = "zig build test"' in proc.stdout, proc.stdout
    assert (root / "scripts" / "kstrl" / "manifest.json").exists()


def test_the_stack_kstrl_toml_holds_is_not_proposed_again_and_a_different_one_is(
    tmp_path: Path,
) -> None:
    """kstrl.toml holds a [stack]: the architect answering that same table
    files nothing, and one answering a different table files it as a
    proposal beside the stack that stays in force."""
    root = greenfield(tmp_path)
    write_stack(root, STACK["checks"])
    same = {
        "instructions": TEST_INSTRUCTIONS,
        "setup": "",
        "env": [],
        "checks": STACK["checks"],
    }
    agent, _prompts = _architect(tmp_path, _output(same))

    kept = _decompose(root, agent)

    assert kept.returncode == 0, kept.stdout
    assert _stack_items(root) == [], kept.stdout
    other, _ = _architect(tmp_path / "other", _output(STACK))
    proposed = _decompose(root, other)
    assert proposed.returncode == 0, proposed.stdout
    (item,) = _stack_items(root)
    assert item.evidence["stack"]["instructions"] == STACK["instructions"]


def test_the_architects_stack_is_checked_inside_the_retry_loop(tmp_path: Path) -> None:
    """A null stack where kstrl.toml has none, then a stack with no checks
    and a secret-shaped variable: each is a retry naming the fault, and the
    third, valid answer is the one filed. A repository with a manifest, so
    the architect runs whatever kstrl thinks of manifests."""
    root = greenfield(tmp_path, extra={"Cargo.toml": MANIFESTS["Cargo.toml"]})
    broken = {**STACK, "checks": {}, "env": ["GITHUB_TOKEN"]}
    agent, prompts = _architect(tmp_path, _output(None), _output(broken), _output(STACK))

    proc = _decompose(root, agent)

    assert proc.returncode == 0, proc.stdout
    assert sorted(p.name for p in prompts.iterdir()) == ["1.txt", "2.txt", "3.txt"], proc.stdout
    second = (prompts / "2.txt").read_text(encoding="utf-8")
    third = (prompts / "3.txt").read_text(encoding="utf-8")
    assert "kstrl.toml has no [stack], so 'stack' must propose one" in second
    assert "stack: checks is empty" in third, third[-600:]
    assert "stack: env[0] GITHUB_TOKEN looks like a secret" in third, third[-600:]
    (item,) = _stack_items(root)
    assert dict(item.evidence["stack"]["checks"]) == STACK["checks"]


def test_a_component_may_create_a_build_manifest_but_never_kstrl_toml(tmp_path: Path) -> None:
    """kstrl.toml holds the confirmed [stack], so no component may be scoped
    to it; a build manifest is the project's own file, and a component may
    create one."""
    root = greenfield(tmp_path)
    first = _output(STACK, ["kstrl.toml", "Cargo.toml", "src/", "scripts/kstrl/feature/comp-a/"])
    second = _output(STACK, ["Cargo.toml", "src/", "scripts/kstrl/feature/comp-a/"])
    agent, prompts = _architect(tmp_path, first, second)

    proc = _decompose(root, agent)

    assert proc.returncode == 0, proc.stdout
    retry = (prompts / "2.txt").read_text(encoding="utf-8")
    assert "entry 'kstrl.toml' is on the DECOMPOSE_PROMPT EXCLUDE list" in retry
    assert "entry 'Cargo.toml'" not in retry
    manifest = Manifest.load(root / "scripts" / "kstrl" / "manifest.json")
    (component,) = manifest.components
    prd = json.loads(
        plan_prd_path(root, "comp-a", plan_id=component.plan_id).read_text(encoding="utf-8")
    )
    assert "Cargo.toml" in prd["allowedPaths"], prd["allowedPaths"]


#: An architect that proposes STACK and escalates adopting it to the owner
#: (stack rule S3), with no components: the decompose halts, exit 2.
HALTED: dict[str, Any] = {
    "stack": STACK,
    "spec_issues": [
        {
            "id": "stack-change",
            "severity": "blocker",
            "kind": "ambiguity",
            "summary": "The spec needs a Zig toolchain, which is the owner's choice",
            "location": "spec.md:1",
            "suggestion": "Adopt the proposed [stack]",
        }
    ],
    "decisions": [
        {
            "issue": "stack-change",
            "question": "change the project's stack to Zig",
            "disposition": "escalated",
            "resolution": "changing a project's stack is the owner's decision",
        }
    ],
    "components": [],
}


def test_an_architect_that_halts_still_files_its_proposal(tmp_path: Path) -> None:
    """kstrl.toml holds a [stack] the spec does not fit: the architect
    proposes another and escalates the change to the owner (stack rule S3).
    The decompose halts, and the proposal is filed beside the escalation,
    unconfirmed, so the owner sees both."""
    root = greenfield(tmp_path)
    write_stack(root)
    agent, _prompts = _architect(tmp_path, HALTED)

    proc = _decompose(root, agent)

    assert proc.returncode == 2, proc.stdout
    (item,) = _stack_items(root)
    assert item.status is ItemStatus.OPEN, item
    assert item.evidence["stack"]["instructions"] == STACK["instructions"], item.evidence
    box = Inbox(root, InboxConfig.load(root))
    assert [i.kind for i in box.items() if i.kind is ItemKind.SPEC_ESCALATION] == [
        ItemKind.SPEC_ESCALATION
    ], proc.stdout
    assert not (root / "scripts" / "kstrl" / "manifest.json").exists(), proc.stdout


def test_ks_factory_spec_no_verify_pays_the_architect_without_measuring_the_base(
    tmp_path: Path,
) -> None:
    """``--no-verify`` turns off Phase 1 and the base check with it, so on a
    repository with no [stack] the base is not measured before the architect
    either: the architect runs (here it halts, so no engineer follows)."""
    root = greenfield(tmp_path)
    agent, prompts = _architect(tmp_path, HALTED)

    proc = run_ks(
        root,
        "factory",
        *("--spec", str(root / "spec.md"), "--project-name", "demo", "--root", str(root)),
        *("--agent-cmd", agent, "--yes", "--no-tui", "--ui", "plain", "--no-color"),
        *("--no-prs", "--no-verify"),
    )

    assert len(list(prompts.iterdir())) == 1, proc.stdout
    assert "Measuring the gates on the base branch" not in proc.stdout, proc.stdout
    assert list(root.glob(".kstrl/runs/*/base-gates.json")) == [], proc.stdout
    (item,) = _stack_items(root)
    assert item.evidence["stack"]["instructions"] == STACK["instructions"], item.evidence


#: What the stub architect does to the repository while it runs, before it
#: answers: nothing, an empty commit on the base branch, or a [verify]
#: subprocess_timeout (the line ``ks init`` writes commented out, set and left
#: uncommitted) that changes how the base is measured (``BaseGates.digest``)
#: without changing the confirmed [stack].
_MOVES: dict[str, str] = {
    "same-base": "",
    "base-moved": "git commit -q --allow-empty -m moved; ",
    "config-moved": (
        "sed 's/^# subprocess_timeout = 0.0 /subprocess_timeout = 299.0 /' kstrl.toml > k.tmp; "
        "mv k.tmp kstrl.toml; "
    ),
}


@runs_a_stack
@pytest.mark.parametrize("move", sorted(_MOVES))
def test_ks_factory_spec_measures_the_base_before_the_architect(tmp_path: Path, move: str) -> None:
    """Under a confirmed [stack] the base is measured before the architect is
    paid, and the factory run reuses that reading while the base names the
    same commit and is measured the same way: one base measurement, logged
    before the architect's line. When the base moves while the architect
    runs (the stub commits to main), or the timeout it is measured under
    changes, the run measures it again."""
    moved = move != "same-base"
    root = _spec_project(tmp_path, initialised=True)
    log = tmp_path / "order"
    log.mkdir()
    with (root / "kstrl.toml").open("a", encoding="utf-8") as fh:
        fh.write("\n" + _stack({"tests": f"pwd >> '{log}/lines'"}, rung={"writable": [str(log)]}))
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "stack")
    confirm_stack(root)
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "stack": None,
                "spec_issues": [],
                "decisions": [],
                "components": [_component(["work.txt", "scripts/kstrl/feature/comp-a/"])],
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "architect-ran"
    agent = (
        f"if [ ! -f {shlex.quote(str(marker))} ]; then echo architect >> '{log}/lines'; "
        f"{_MOVES[move]}fi; " + _architect_then_engineer(tmp_path / "calls", payload, marker)
    )

    _code, out = _spawn(
        [
            "factory",
            *("--spec", str(root / "spec.md"), "--project-name", "demo", "--root", str(root)),
            *("--agent-cmd", agent, "--yes", "--no-tui", "--ui", "plain", "--no-color"),
            *("--no-prs", "--max-retries", "0", "--review-mode", "skip"),
            *("--contract-check", "skip"),
        ],
        root,
        None,
    )

    lines = (log / "lines").read_text(encoding="utf-8").splitlines()
    base = [i for i, line in enumerate(lines) if "base-gates" in line]
    assert len(base) == (2 if moved else 1), (lines, out)
    assert base[0] < lines.index("architect"), (lines, out)
    reused = "was measured before the architect ran; that reading holds" in out
    assert reused is not moved, out
