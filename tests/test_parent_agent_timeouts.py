"""A parent-process agent call is bounded, and a killed call is never a verdict (#603).

The defect had two halves. The code reviewer, the integration reviewer and
the architect run in the factory's parent process and had no limit an
operator could set, so one silent hang held the whole run. The security
reviewer and the distiller had a limit, but when it fired the reply streamed
before the kill was parsed as if the call had finished: a hard-mode security
review killed after printing a clean reply passed, and a distiller killed
after printing a fact wrote the fact.

Every test drives the real CLI (or, for the integration review, the real
``run_factory`` over a merged feature) in a session of its own, bounded in
real time and killed by process group on expiry, so a missing bound fails
here instead of hanging the suite. Each stub agent writes its pid and starts
a grandchild, and both must be dead when the command ends. The stub prints
the literal timeout line after its reply: a reply that says it timed out is
still read, because only the adapter's own record says a call was killed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl.init_cmd import _detect_project_context, gitignore_block
from tests.helpers import gitrepo, procs
from tests.helpers.stack_confirmation import confirm_stack, write_stack
from tests.test_isolation_rung import needs_nono

REPO = Path(__file__).resolve().parent.parent
COMP = "comp-a"
BOUND_SECONDS = 90.0
#: The limit where a stub prints a reply before it hangs: long enough for
#: the reply to arrive first on a loaded machine.
LIMIT = 3
TIMED_OUT = f"timed out after {float(LIMIT)}s"
#: The limit where a stub hangs silently: it only has to write its pid.
SILENT_LIMIT = 1
TIMED_OUT_SILENT = f"timed out after {float(SILENT_LIMIT)}s"
#: The limits in the control test: set, so the test proves a reply inside a
#: limit is read, and long, so a loaded machine cannot fire them.
CONTROL_LIMIT = 60
AGENT_CLIS = ("claude", "codex")

#: The stub every role runs: ``stub.py <role> <hang|exit> <pid dir>``. The
#: engineer commits ``a.txt``, and on the distiller's prompt prints one
#: fact. A reviewer prints a passing reply whose diffstat it measures from
#: the worktree it runs in. After a reply, ``hang`` starts a grandchild and
#: waits on it; ``exit`` returns.
STUB = """\
import json, os, subprocess, sys
role, then, piddir = sys.argv[1:4]
prompt = sys.stdin.read()
if role == "engineer" and "knowledge-distillation agent" not in prompt:
    open("a.txt", "w").write("a\\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "a"], check=True)
    print("<promise>COMPLETE</promise>", flush=True)
    sys.exit(0)
if role == "engineer":
    reply = {"facts": [{"id": "fact-001", "scope": "invariant", "confidence": "asserted",
                        "evidence": ["a.txt:1"], "claim": "a.txt holds the letter a."}]}
else:
    rows = subprocess.run(["git", "diff", "--numstat", "main...HEAD"],
                          capture_output=True, text=True, check=True).stdout.splitlines()
    stat = {"files": len(rows),
            "insertions": sum(int(r.split("\\t")[0]) for r in rows),
            "deletions": sum(int(r.split("\\t")[1]) for r in rows)}
    reply = {"observedDiffstat": stat, "exhaustively_searched": True, "overallNotes": ""}
    if role == "review":
        criterion = {"criterion": "AC1", "verdict": "pass", "explanation": "a.txt:1 read",
                     "suggestion": ""}
        reply["stories"] = [{"storyId": "US-001", "storyTitle": "t", "criteria": [criterion]}]
        reply["concerns"] = []
    else:
        reply["findings"] = []
print(json.dumps(reply), flush=True)
print("ERROR: agent timed out after 99.0s", flush=True)
if then == "hang":
    open(os.path.join(piddir, role + ".pid"), "w").write(str(os.getpid()))
    child = subprocess.Popen(["sleep", "3600"])
    open(os.path.join(piddir, role + "-child.pid"), "w").write(str(child.pid))
    child.wait()
"""

#: A silent hang with a grandchild, for the architect and the integration
#: reviewer: ``hang.sh <pid dir>``.
HANG = (
    'echo $$ > "$1/hang.pid"; echo $$ >> "$1/reap.pids"\n'
    'sleep 3600 & echo $! > "$1/hang-child.pid"; echo $! >> "$1/reap.pids"; wait\n'
)

#: The architect that hangs on its first call only and then answers
#: something that does not parse, and counts its calls: ``once.sh <pid dir>``.
HANG_ONCE = (
    'echo call >> "$1/calls.log"\n'
    'if [ ! -e "$1/hung" ]; then\n'
    '  touch "$1/hung"; echo $$ > "$1/hang.pid"; echo $$ >> "$1/reap.pids"\n'
    '  sleep 3600 & echo $! > "$1/hang-child.pid"; echo $! >> "$1/reap.pids"; wait\n'
    "fi\n"
    "cat > /dev/null; echo not-json\n"
)


def _path_without_agent_clis() -> str:
    kept = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and not any(os.access(os.path.join(d, n), os.X_OK) for n in AGENT_CLIS)
    ]
    return os.pathsep.join(kept)


def _env(knowledge: bool = False) -> dict[str, str]:
    # #696: a confirmed [stack] means these runs now prove the isolation
    # rung, which shells out to nono. Resolved from the UNSTRIPPED PATH,
    # same lookup kstrl.isolation.py uses, because on this machine nono
    # and codex are symlinked from the same bin directory, so stripping
    # every directory that holds an agent CLI (below) would take nono
    # out too. Pinning it through KSTRL_NONO survives that strip.
    nono = os.environ.get("KSTRL_NONO", "").strip() or shutil.which("nono") or ""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL", "FACTORY_MAX_PARALLEL")
    }
    env["PATH"] = _path_without_agent_clis()
    env["KSTRL_AGENT_PROBE"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    env["KSTRL_KNOWLEDGE_ENABLED"] = "1" if knowledge else "0"
    if nono:
        env["KSTRL_NONO"] = nono
    env["PYTHONPATH"] = str(REPO)
    return env


def _stub(tmp_path: Path, role: str, then: str) -> str:
    script = tmp_path / "stub.py"
    script.write_text(STUB, encoding="utf-8")
    return f"{sys.executable} {script} {role} {then} {tmp_path}"


def _repo(tmp_path: Path, toml: str) -> Path:
    """One component whose engineer commits ``a.txt``; ``toml`` is kstrl.toml."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / ".gitignore").write_text(".kstrl/\n", encoding="utf-8")
    (root / "kstrl.toml").write_text("[factory]\nmax_retries = 0\n" + toml, encoding="utf-8")
    prd = root / "scripts" / "kstrl" / "feature" / COMP / "prd.json"
    prd.parent.mkdir(parents=True)
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    prd.write_text(
        json.dumps(
            {
                "branchName": f"kstrl/factory/{COMP}",
                "userStories": [story],
                "allowedPaths": ["a.txt"],
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "version": "1",
        "specFile": "",
        "projectName": "p",
        "baseBranch": "main",
        "singlePr": False,
        "components": [
            {
                "id": COMP,
                "title": COMP,
                "description": "",
                "dependencies": [],
                "prdPath": f"scripts/kstrl/feature/{COMP}/prd.json",
                "branchName": f"kstrl/factory/{COMP}",
            }
        ],
    }
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    gitrepo.git_in(root, "add", "-A")
    write_stack(root)
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    confirm_stack(root)
    return root


@pytest.fixture(autouse=True)
def _reap_stubs(tmp_path: Path) -> Iterator[None]:
    """After the test, and after its assertions on them, kill every pid a
    stub wrote under ``tmp_path``, so a failing run leaves nothing behind
    (#292). Only pids this test's own stubs wrote are touched. The architect
    runs its stub once per attempt, so ``reap.pids`` keeps every attempt's
    pids where ``hang.pid`` keeps only the last."""
    yield
    for pidfile in [*tmp_path.glob("*.pid"), *tmp_path.glob("*.pids")]:
        for text in pidfile.read_text(encoding="utf-8").split():
            # An empty or partly written line must not become os.kill(0, ...),
            # which signals this test's own process group.
            if not text.isdigit() or int(text) <= 1:
                continue
            try:
                os.kill(int(text), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run ``argv`` in its own session; kill its group at the bound and fail."""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=BOUND_SECONDS)
    except subprocess.TimeoutExpired:
        procs.kill_group(proc.pid)
        out, _ = proc.communicate()
        pytest.fail(f"did not finish within {BOUND_SECONDS}s: the call has no bound\n{out}")
    finally:
        procs.kill_group(proc.pid)
    return proc.returncode, out


def _stubs_died(pid_dir: Path, role: str) -> None:
    """The stub and the grandchild it started both ended with the call."""
    for name in (f"{role}.pid", f"{role}-child.pid"):
        pid = procs.read_pid(pid_dir / name)
        assert procs.wait_for_pid_to_die(pid, timeout=10), f"{name} {pid} outlived its call"


def _factory(
    root: Path, *flags: str, knowledge: bool = False, extra_env: dict[str, str] | None = None
) -> tuple[int, str]:
    argv = [
        sys.executable,
        "-m",
        "kstrl",
        "factory",
        "--manifest",
        str(root / "scripts" / "kstrl" / "manifest.json"),
        "--root",
        str(root),
        "--yes",
        "--ui",
        "plain",
        "--no-color",
        "--no-tui",
        "--no-prs",
        "--max-parallel",
        "1",
        "--contract-check",
        "skip",
        *flags,
    ]
    return _run(argv, root, {**_env(knowledge), **(extra_env or {})})


def _component(root: Path) -> dict[str, object]:
    manifest = json.loads(
        (root / "scripts" / "kstrl" / "manifest.json").read_text(encoding="utf-8")
    )
    (comp,) = manifest["components"]
    return dict(comp)


def _recorded(root: Path, phase: str) -> list[str]:
    """The explanation of every infrastructure_error finding ``phase``
    recorded, from the manifest and from the run's event stream, which
    must agree."""
    in_manifest = [
        f["explanation"]
        for f in _component(root)["findings"]  # type: ignore[attr-defined]
        if f["phase"] == phase and f["category"] == "infrastructure_error"
    ]
    in_events = [
        e["data"]["explanation"]
        for path in (root / ".kstrl" / "runs").glob("factory-*/events.jsonl")
        for e in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if e.get("event") == "finding_recorded"
        and e["data"]["phase"] == phase
        and e["data"]["category"] == "infrastructure_error"
    ]
    assert in_manifest == in_events
    return in_manifest


def _facts(root: Path) -> list[Path]:
    return sorted((root / ".kstrl" / "knowledge" / COMP).glob("*/*.md"))


@pytest.mark.parametrize(
    ("mode", "status", "setting"),
    [("hard", "failed", "toml"), ("advisory", "completed", "env")],
    ids=["hard-failed-toml", "advisory-completed-env"],
)
@needs_nono
def test_a_reviewer_killed_after_printing_a_passing_verdict_is_not_a_pass(
    tmp_path: Path, mode: str, status: str, setting: str
) -> None:
    """The limit comes from kstrl.toml in one case and from
    KSTRL_FACTORY_REVIEW_TIMEOUT_SECONDS in the other, so both readers are
    driven end to end."""
    in_toml = setting == "toml"
    root = _repo(tmp_path, f"review_timeout_seconds = {LIMIT}\n" if in_toml else "")

    _code, out = _factory(
        root,
        "--agent-cmd",
        _stub(tmp_path, "engineer", "exit"),
        "--review-mode",
        mode,
        "--review-agent-cmd",
        _stub(tmp_path, "review", "hang"),
        extra_env={} if in_toml else {"KSTRL_FACTORY_REVIEW_TIMEOUT_SECONDS": str(LIMIT)},
    )

    assert [TIMED_OUT in e for e in _recorded(root, "review")] == [True], out
    assert _component(root)["status"] == status, out
    _stubs_died(tmp_path, "review")


@needs_nono
def test_a_security_reviewer_killed_after_printing_a_clean_verdict_is_not_a_pass(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, f'[security]\nmode = "hard"\ntimeout_seconds = {LIMIT}\n')

    _code, out = _factory(
        root,
        "--agent-cmd",
        _stub(tmp_path, "engineer", "exit"),
        "--review-agent-cmd",
        _stub(tmp_path, "review", "exit"),
        "--security-agent-cmd",
        _stub(tmp_path, "security", "hang"),
    )

    assert _recorded(root, "review") == [], out
    assert [TIMED_OUT in e for e in _recorded(root, "security")] == [True], out
    assert _component(root)["status"] == "failed", out
    _stubs_died(tmp_path, "security")


@needs_nono
def test_a_distiller_killed_after_printing_a_fact_writes_no_fact(tmp_path: Path) -> None:
    root = _repo(tmp_path, f"[knowledge]\ndistill_timeout_seconds = {LIMIT}\n")

    _code, out = _factory(
        root,
        "--agent-cmd",
        _stub(tmp_path, "engineer", "hang"),
        "--review-agent-cmd",
        _stub(tmp_path, "review", "exit"),
        knowledge=True,
    )

    assert _component(root)["status"] == "completed", out
    assert _facts(root) == [], out
    assert f"Knowledge: the distiller agent failed: the agent {TIMED_OUT}" in out
    _stubs_died(tmp_path, "engineer")


@needs_nono
def test_the_same_replies_without_the_hang_pass_and_write_a_fact(tmp_path: Path) -> None:
    """The control for the three tests above: the same stubs, the same
    limits, no hang. Without it a reply that failed on its own (a diffstat
    that does not match the change, a fact that does not parse) would make
    those tests pass for the wrong reason."""
    root = _repo(
        tmp_path,
        f"review_timeout_seconds = {CONTROL_LIMIT}\n"
        f'[security]\nmode = "hard"\ntimeout_seconds = {CONTROL_LIMIT}\n'
        f"[knowledge]\ndistill_timeout_seconds = {CONTROL_LIMIT}\n",
    )

    code, out = _factory(
        root,
        "--agent-cmd",
        _stub(tmp_path, "engineer", "exit"),
        "--review-agent-cmd",
        _stub(tmp_path, "review", "exit"),
        "--security-agent-cmd",
        _stub(tmp_path, "security", "exit"),
        knowledge=True,
    )

    assert code == 0, out
    assert _component(root)["status"] == "completed", out
    assert _recorded(root, "review") == [] and _recorded(root, "security") == [], out
    assert [p.name for p in _facts(root)] == ["fact-001.md"], out


def _architect_repo(tmp_path: Path, script: str) -> Path:
    """A repository with ``spec.md`` whose ``[agent] command`` is ``script``
    and whose ``[factory] architect_timeout_seconds`` is ``SILENT_LIMIT``."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    stub = tmp_path / "architect.sh"
    stub.write_text(script, encoding="utf-8")
    (root / "spec.md").write_text("# Spec\n\nBuild a CLI.\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / ".gitignore").write_text(
        gitignore_block(_detect_project_context(root)["language"]), encoding="utf-8"
    )
    (root / "kstrl.toml").write_text(
        f'[agent]\ncommand = "bash {stub} {tmp_path}"\n'
        f"[factory]\narchitect_timeout_seconds = {SILENT_LIMIT}\n",
        encoding="utf-8",
    )
    gitrepo.git_in(root, "add", "-A")
    write_stack(root)
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    confirm_stack(root)
    return root


def _architect(tmp_path: Path, script: str, *command: str) -> tuple[int, str]:
    """Run ``ks <command> --spec`` over :func:`_architect_repo`."""
    root = _architect_repo(tmp_path, script)
    argv = [
        sys.executable,
        "-m",
        "kstrl",
        *command,
        "--spec",
        str(root / "spec.md"),
        "--project-name",
        "demo",
        "--root",
        str(root),
        "--no-tui",
        "--ui",
        "plain",
        "--no-color",
    ]
    return _run(argv, root, _env())


@pytest.mark.parametrize(
    "command",
    [
        ("decompose",),
        ("factory", "--yes", "--no-prs", "--contract-check", "skip"),
    ],
    ids=["ks-decompose", "ks-factory-spec"],
)
def test_a_hung_architect_ends_the_command_with_a_recorded_timeout(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    """``ks decompose`` and ``ks factory --spec`` both run the architect in
    the command's own process; each reads the limit and passes it on."""
    code, out = _architect(tmp_path, HANG, *command)

    assert code != 0, out
    assert f"Last error: the agent {TIMED_OUT_SILENT}" in out
    _stubs_died(tmp_path, "hang")


def test_an_architect_attempt_after_a_timed_out_one_is_read_normally(tmp_path: Path) -> None:
    """A timed-out attempt is one failed attempt: the retry count is
    unchanged, the next attempt is told why, and its own reply is judged on
    its own. A timeout check that looks at records from earlier attempts
    would call every later attempt a timeout too."""
    code, out = _architect(tmp_path, HANG_ONCE, "decompose")

    assert code != 0, out
    assert (tmp_path / "calls.log").read_text(encoding="utf-8").count("call") == 3, out
    (last_error,) = [line for line in out.splitlines() if "Last error:" in line]
    assert "timed out" not in last_error, out
    _stubs_died(tmp_path, "hang")


#: The home shell's decompose form launches through ``start_run_session``,
#: which runs the architect on a worker thread of the TUI's own process.
#: Driven in a subprocess, so a missing bound fails at the test's bound.
TUI_DRIVER = """\
import sys
from pathlib import Path
from kstrl.launch import DecomposeLaunch
from kstrl.tui.session import start_run_session
root = Path(sys.argv[1])
session = start_run_session(DecomposeLaunch(spec_path=root / "spec.md", project_name="demo"), root)
try:
    session.handle.join()
finally:
    session.close()
print((session.run_dir / "events.jsonl").read_text(encoding="utf-8"))
sys.exit(session.handle.exit_code)
"""


def test_a_hung_architect_launched_from_the_tui_ends_with_a_recorded_timeout(
    tmp_path: Path,
) -> None:
    root = _architect_repo(tmp_path, HANG)

    code, out = _run([sys.executable, "-c", TUI_DRIVER, str(root)], root, _env())

    assert code != 0, out
    assert f"Last error: the agent {TIMED_OUT_SILENT}" in out
    _stubs_died(tmp_path, "hang")


#: The integration review has no CLI of its own: it runs inside
#: ``ks factory`` once components have merged. This drives the real
#: ``run_factory`` over a merged feature the way tests/helpers/
#: integration_harness does, with a real hanging CustomAgent as the
#: reviewer, in a subprocess so a missing bound fails at the test's bound.
INTEGRATION_DRIVER = """\
import sys
from pathlib import Path
from kstrl.agents.custom import CustomAgent
from tests.helpers import integration_harness as h
root, command, limit = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
result, out = h.run_factory_over(root, CustomAgent(command), review_timeout_seconds=limit)
print(out)
sys.exit(result.exit_code)
"""


@needs_nono
def test_a_hung_integration_reviewer_is_recorded_as_a_timeout(tmp_path: Path) -> None:
    from tests.helpers import integration_harness as h

    root = tmp_path / "repo"
    h.merged_feature(root)
    hang = tmp_path / "hang.sh"
    hang.write_text(HANG, encoding="utf-8")
    argv = [
        sys.executable,
        "-c",
        INTEGRATION_DRIVER,
        str(root),
        f"bash {hang} {tmp_path}",
        str(SILENT_LIMIT),
    ]

    _code, out = _run(argv, REPO, _env())

    (evidence,) = h.evidence_files(root)
    review = json.loads(evidence.read_text(encoding="utf-8"))["review"]
    assert review["infrastructureError"] is True, out
    assert TIMED_OUT_SILENT in review["overallNotes"], out
    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["stops"][-1]["outcome"] == "red"
    _stubs_died(tmp_path, "hang")


@pytest.mark.parametrize(
    ("toml", "extra_env", "named"),
    [
        ("review_timeout_seconds = -1\n", {}, "[factory] review_timeout_seconds"),
        (
            "",
            {"KSTRL_FACTORY_ARCHITECT_TIMEOUT_SECONDS": "-1"},
            "(set by KSTRL_FACTORY_ARCHITECT_TIMEOUT_SECONDS)",
        ),
    ],
    ids=["review-toml", "architect-env"],
)
def test_a_negative_limit_is_refused_before_any_agent_call(
    tmp_path: Path, toml: str, extra_env: dict[str, str], named: str
) -> None:
    """Both new limits go through the same refusal as every other number."""
    root = _repo(tmp_path, toml)
    calls = tmp_path / "calls.log"

    code, out = _factory(root, "--agent-cmd", f"echo called >> {calls}", extra_env=extra_env)

    assert code == 2, out
    assert "must be >= 0, got -1.0" in out and named in out, out
    assert not calls.exists(), out
