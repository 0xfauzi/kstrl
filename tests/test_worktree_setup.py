"""#624: every worktree kstrl creates gets its own dependencies before a gate reads it.

kstrl nests its worktrees under the project root, and Node resolves a module
by walking up the directory tree, so a worktree with no ``node_modules`` of its
own silently used the ROOT checkout's. Measured on the unfixed tree with the
fixture below: a branch that adds a dependency failed Phase 1 with ``Cannot find
module 'greet'``, and a branch whose code only works on the root's older copy
passed Phase 1 and Phase 3.

The fixture is a Node project with no registry: the branch vendors its
dependency under ``deps/`` and the setup command installs it into the
worktree's own ``node_modules``, wiping what was there first, the way
``npm ci`` does. The gates run ``node check.js``, which prints the version it
resolved. Each test drives the real factory with a shell-command engineer.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.config import KstrlConfig
from kstrl.contract import ContractConfig, ContractMode
from kstrl.factory import FactoryConfig, FactoryResult, run_factory
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.timeout import TimeoutConfig
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers import gitrepo
from tests.helpers.procs import wait_for_pid_to_die
from tests.helpers.stack_confirmation import confirm_stack, in_process_stack, write_stack

CHECK = "node check.js"
#: What ``npm ci`` does, without a registry: wipe node_modules, install the lock.
INSTALL = (
    "rm -rf node_modules && mkdir node_modules && "
    "if [ -d deps ]; then cp -R deps/. node_modules/; fi"
)
PRD = "scripts/kstrl/feature/a/prd.json"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=30)


def _repo(root: Path, root_greet: str | None = None) -> Path:
    """A repo on main with the kstrl scaffolding committed. ``root_greet``
    installs ``greet`` at that version in the ROOT checkout only, untracked,
    the way an operator's own ``npm install`` leaves it."""
    assert _node_is_installed(), "these tests need node on PATH"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (root / "scripts" / "kstrl").mkdir(parents=True)
    (root / "scripts" / "kstrl" / "prompt.md").write_text("test prompt\n", encoding="utf-8")
    (root / PRD).parent.mkdir(parents=True)
    story = {
        "id": "US-001",
        "title": "T",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    (root / PRD).write_text(
        json.dumps({"branchName": "kstrl/factory/a", "userStories": [story]}), encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    if root_greet is not None:
        installed = root / "node_modules" / "greet"
        installed.mkdir(parents=True)
        (installed / "index.js").write_text(f'module.exports = "{root_greet}";\n', encoding="utf-8")
    return root


def _node_is_installed() -> bool:
    return subprocess.run(["node", "--version"], capture_output=True, timeout=30).returncode == 0


def _engineer(branch_greet: str, expect: str) -> str:
    """A shell engineer: the branch pins ``greet`` at ``branch_greet`` and
    adds ``check.js``, which exits 1 unless it resolved ``expect``."""
    return (
        "mkdir -p deps/greet && "
        f"printf 'module.exports = \"{branch_greet}\";\\n' > deps/greet/index.js && "
        'printf \'const v = require("greet"); console.log("greet " + v); '
        f'if (v !== "{expect}") process.exit(1);\\n\' > check.js && '
        "git add deps check.js && git commit -qm dep && "
        "echo '<promise>COMPLETE</promise>'"
    )


def _skip_on_base_gates(command: str) -> str:
    """``command``, except trivially true on the one throwaway worktree
    Phase 1's base-gates preflight runs it on (``.kstrl/contract/base-
    gates-*``, #654+#696): that worktree is cut from the base commit, so
    it never has the branch's own commits a check or setup written for
    the component worktree expects, and would otherwise refuse the whole
    run before any engineer call. Same pattern as
    tests/test_spine_crash_recovery.py's worktree-path case."""
    if not command:
        return command
    return f'case "$(pwd)" in */.kstrl/contract/base-gates-*) true;; *) {command};; esac'


def _manifest(scaffold: str = "") -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id="a",
                title="A",
                description="",
                dependencies=[],
                prd_path=PRD,
                branch_name="kstrl/factory/a",
                status=ComponentStatus.PENDING.value,
                scaffold=scaffold,
            )
        ],
    )


def _run(
    root: Path,
    agent_cmd: str,
    gate: str,
    setup: str,
    monkeypatch: pytest.MonkeyPatch,
    typecheck_and_lint: str = "true",
    scaffold: str = "",
    contract_gate: str = "",
    max_retries: int = 0,
    manifest: Manifest | None = None,
) -> tuple[FactoryResult, Component]:
    """One factory run: ``gate`` is the test command of Phase 1, and of
    Phase 3 unless ``contract_gate`` names another."""
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    manifest = manifest or _manifest(scaffold)
    # Phase 1's base-gates preflight (#654) now runs under the confirmed
    # [stack] (#696), on the base commit, before any engineer call: these
    # fixtures' check and setup commands are written for the component
    # worktree, so the base-gates worktree needs its own pass-through.
    base_gate = _skip_on_base_gates(gate)
    base_tc = _skip_on_base_gates(typecheck_and_lint)
    base_setup = _skip_on_base_gates(setup)
    factory_config = FactoryConfig(
        use_worktrees=True,
        create_prs=False,
        max_parallel=1,
        max_retries=max_retries,
        retry_delay=0,
        review_mode="skip",
        integration_review=False,
        progress_log_path=root / ".kstrl" / "progress.jsonl",
        project_stack=in_process_stack(
            {"tests": base_gate, "typecheck": base_tc, "lint": base_tc},
            setup=base_setup,
        ),
        verify_config=VerifyConfig(
            project_stack=in_process_stack(
                {"tests": base_gate, "typecheck": base_tc, "lint": base_tc},
                setup=base_setup,
            ),
            check_diff_scope=False,
            check_bad_patterns=False,
        ),
        contract_config=ContractConfig(
            mode=ContractMode.TIER.value,
            project_stack=in_process_stack({"tests": contract_gate or gate}, setup=setup),
            timeout=60,
        ),
        timeout_config=TimeoutConfig(agent_iteration=60, component_total=120),
    )
    base = KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        agent_cmd=agent_cmd,
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    result = run_factory(manifest, factory_config, base, PlainUI(no_color=True), root)
    comp = manifest.get_component("a")
    assert comp is not None
    return result, comp


def _events(root: Path, name: str) -> list[dict[str, object]]:
    rows = (root / ".kstrl" / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    return [e["data"] for e in map(json.loads, rows) if e["event"] == name]


def test_a_branch_dependency_is_installed_in_the_component_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")  # nothing installed in the root checkout

    result, comp = _run(root, _engineer("2.0.0", "2.0.0"), CHECK, INSTALL, monkeypatch)

    assert result.exit_code == 0, (comp.failed_phase, comp.failed_check, comp.error)
    assert result.completed == ["a"]
    assert [e["passed"] for e in _events(root, "verification_result")] == [True]
    # Phase 3 merged the branch into a fresh contract worktree, which has no
    # node_modules until its own setup runs.
    assert [e["passed"] for e in _events(root, "contract_result")] == [True]
    assert result.contract_failures == []


def test_a_failed_worktree_setup_stops_the_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    gate_ran = tmp_path / "gate-ran"

    result, comp = _run(
        root,
        _engineer("2.0.0", "2.0.0"),
        f"touch {gate_ran}",
        "echo setup-broke >&2; exit 7",
        monkeypatch,
        typecheck_and_lint=f"touch {gate_ran}",
    )

    assert result.exit_code == 1
    assert result.failed == ["a"]
    assert (comp.failed_phase, comp.failed_check) == ("provisioning", "worktree_setup")
    infra = [f for f in comp.findings if f.is_infrastructure_error]
    assert len(infra) == 1
    assert "exited 7" in infra[0].explanation
    assert "setup-broke" in infra[0].explanation
    assert not gate_ran.exists(), "a gate ran on a worktree whose setup failed"
    assert _events(root, "verification_result") == []
    assert _events(root, "contract_result") == []
    # The run before the engineer is a warning to the operator, not a stop:
    # the engineer still ran and committed.
    log = next((root / ".kstrl" / "runs").glob("*/components/a/engineer.jsonl"))
    assert "Worktree setup failed for a: " in log.read_text(encoding="utf-8")


def test_the_retry_engineer_is_told_why_the_worktree_setup_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed setup before Phase 1 is the engineer's to repair (a lockfile
    # its attempt broke), so the retry's prompt carries the setup's output.
    root = _repo(tmp_path / "repo")
    prompts = tmp_path / "prompts"

    result, _comp = _run(
        root,
        f"cat >> {prompts}; echo ===END-OF-PROMPT=== >> {prompts}; " + _engineer("2.0.0", "2.0.0"),
        CHECK,
        "echo lockfile-out-of-sync >&2; exit 9",
        monkeypatch,
        max_retries=1,
    )

    # The retry's engineer commits nothing new, so the no-progress
    # breaker ends the run after it; what matters is what it was told.
    assert result.failed == ["a"]
    attempts = prompts.read_text(encoding="utf-8").split("===END-OF-PROMPT===")[:-1]
    assert len(attempts) >= 2, attempts
    assert "lockfile-out-of-sync" not in attempts[0]
    assert any("lockfile-out-of-sync" in a for a in attempts[1:]), attempts[1][-3000:]


def test_the_bisection_worktree_is_set_up_before_it_names_a_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two components in one tier. a adds the dependency and check.js; b adds
    # a file the contract gate refuses. The merged tier fails, so Phase 3
    # bisects: it merges a, tests, merges b, tests. A bisection worktree
    # with no setup of its own cannot resolve greet after merging a, and
    # blames a; with its setup it passes a and names b.
    root = _repo(tmp_path / "repo")
    prd_b = PRD.replace("/a/", "/b/")
    (root / prd_b).parent.mkdir(parents=True)
    (root / prd_b).write_text(
        (root / PRD).read_text(encoding="utf-8").replace("kstrl/factory/a", "kstrl/factory/b"),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "prd b")
    manifest = _manifest()
    manifest.components.append(
        Component(
            id="b",
            title="B",
            description="",
            dependencies=[],
            prd_path=prd_b,
            branch_name="kstrl/factory/b",
            status=ComponentStatus.PENDING.value,
        )
    )
    engineer = (
        'if [ "$(basename "$PWD")" = a ]; then '
        + _engineer("2.0.0", "2.0.0")
        + "; else touch broken && git add broken && git commit -qm b && "
        "echo '<promise>COMPLETE</promise>'; fi"
    )

    result, _comp = _run(
        root,
        engineer,
        "true",
        INSTALL,
        monkeypatch,
        contract_gate=f"{CHECK} && test ! -f broken",
        manifest=manifest,
    )

    assert [e["passed"] for e in _events(root, "verification_result")] == [True, True]
    assert [e["passed"] for e in _events(root, "contract_result")] == [False]
    assert result.failed == ["b"], result.contract_failures
    assert result.completed == ["a"]


def test_the_gate_does_not_use_the_root_checkouts_node_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The branch pins greet 2.0.0 while its code only works on 1.0.0, the
    # version the operator has installed in the root checkout. A gate that
    # resolved the root's copy passes; one that measures the branch fails.
    root = _repo(tmp_path / "repo", root_greet="1.0.0")

    result, comp = _run(root, _engineer("2.0.0", "1.0.0"), CHECK, INSTALL, monkeypatch)

    assert result.failed == ["a"]
    assert (comp.failed_phase, comp.failed_check) == ("verify", "stack:tests")
    log = next((root / ".kstrl" / "debug").glob("*/a/attempt-1/stack:tests.log"))
    assert "greet 2.0.0" in log.read_text(encoding="utf-8")


def test_a_failed_contract_worktree_setup_runs_no_contract_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The component's scaffold replaces the setup in its own worktree, so
    # Phase 1 passes. The contract worktree gets [factory]
    # worktree_setup_command, which fails: the tier fails with the setup's
    # output and its test never runs.
    root = _repo(tmp_path / "repo")
    contract_ran = tmp_path / "contract-ran"

    result, _comp = _run(
        root,
        _engineer("2.0.0", "2.0.0"),
        CHECK,
        "echo contract-setup-broke >&2; exit 5",
        monkeypatch,
        scaffold=INSTALL,
        contract_gate=f"touch {contract_ran}",
    )

    assert [e["passed"] for e in _events(root, "verification_result")] == [True]
    assert [e["passed"] for e in _events(root, "contract_result")] == [False]
    assert not contract_ran.exists(), "a contract test ran on a worktree whose setup failed"
    assert result.exit_code == 1
    assert len(result.contract_failures) == 1
    assert "contract-setup-broke" in result.contract_failures[0]


def test_a_hung_setup_is_killed_on_time(tmp_path: Path) -> None:
    """The real ``ks factory`` in a subprocess of its own, bounded in real
    time: a setup that backgrounds a child and waits on it is killed with
    its whole process group when ``worktree_setup_timeout`` expires."""
    root = _repo(tmp_path / "repo")
    manifest_path = tmp_path / "manifest.json"
    _manifest().save(manifest_path)
    pids = tmp_path / "setup-children"
    gate_ran = tmp_path / "gate-ran"
    (root / "kstrl.toml").write_text(
        "[factory]\n"
        "worktree_setup_timeout = 2\n"
        "integration_review = false\n"
        "[verify]\n"
        "check_diff_scope = false\n"
        "check_bad_patterns = false\n",
        encoding="utf-8",
    )
    write_stack(
        root,
        {"tests": f"touch {gate_ran}"},
        setup=f"sleep 300 >/dev/null 2>&1 & echo $! >> {pids}; wait",
        # #700: the setup runs confined to the rung; it writes the pid
        # file under tmp_path, outside the worktree, so that needs to be
        # named writable (rule 12).
        writable=(str(tmp_path),),
    )
    confirm_stack(root)
    env = {
        **os.environ,
        "AGENT_CMD": "echo '<promise>COMPLETE</promise>'",
        "KSTRL_BRANCH": "",
        "KSTRL_KNOWLEDGE_ENABLED": "0",
    }
    argv = [
        *("factory", "--manifest", str(manifest_path), "--root", str(root), "--yes"),
        *("--no-prs", "--max-parallel", "1", "--max-retries", "0"),
        *("--review-mode", "skip", "--contract-check", "skip"),
        *("--ui", "plain", "--no-color"),
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "kstrl", *argv],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            pytest.fail("ks factory did not finish within 180s of real time")
        children = [int(line) for line in pids.read_text(encoding="utf-8").split()]
        # Once, on the base before any engineer (#654): under a [stack] a
        # failed setup refuses the base (#696), so nothing runs after it.
        assert len(children) == 1, out
        for pid in children:
            assert wait_for_pid_to_die(pid, timeout=10.0), f"setup child {pid} outlived its timeout"
        assert proc.returncode == 2, out
        assert "did not finish within 2.0s; its process group was killed" in out
        assert not gate_ran.exists()
    finally:
        for line in pids.read_text(encoding="utf-8").split() if pids.exists() else []:
            try:
                os.kill(int(line), signal.SIGKILL)
            except ProcessLookupError:
                pass
