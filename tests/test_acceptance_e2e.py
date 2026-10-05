"""#700 slice 4: acceptance checks from an operator-written plan, record-only.

``ks factory --acceptance <dir>`` takes a plan directory from outside the
repository, checks it entry by entry and copies it under the control
directory by its digest. Once a person approved the plan, every check
runs once on the base inside the proven rung, and a check the plan says
fails on the base must fail there; a check that cannot run there refuses,
except in a component the plan marks as creating the app (decision 11).
On each head the checks run ``HEAD_RUNS`` times, pass only when every run
exits 0, and are recorded, printed and put in the PR body from one
renderer. Nothing about the verdict routes the component.

End to end: the real ``ks factory`` as a subprocess on a real git
repository after the real ``ks init``, with a confirmed ``[stack]`` and a
stub engineer that records each call and its prompt (the harness of
``tests/test_stack_e2e.py``). The checks run inside the rung, so every
test that reaches a check needs nono 0.79 or later on macOS; on a
platform with no prover they run on the host under the fallback label
(#700, owner decision 2026-10-05), so Linux CI runs them (``runs_a_stack``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kstrl.acceptance import BASE_NOT_RUNNABLE, HEAD_RUNS
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in
from tests.helpers.stack_confirmation import confirm_stack
from tests.test_isolation_rung import runs_a_stack
from tests.test_isolation_stack import FALLBACK_LABEL, NO_PROVER
from tests.test_stack_e2e import Run, _factory, _repo, _spawn, _stack

COMP = "greeter"

#: The check every greeting test runs: the application under test prints
#: the greeting for its argument. It reaches the app through KSTRL_TREE,
#: and exits 127 when there is no app to run.
GREETS = (
    '#!/bin/sh\nout=$("$KSTRL_TREE/greet" "$1") || exit $?\n'
    '[ "$out" = "Hello, $1" ] || { echo "expected Hello, $1, got $out"; exit 1; }\n'
)

#: The base's own greet, wrong for every name: each greeting check fails there.
WRONG_GREET = '#!/bin/sh\necho "Hi $1"\n'

#: An engineer that special-cases the one name it was shown.
SPECIAL_CASED = (
    "printf '#!/bin/sh\\necho \"Hello, Ada\"\\n' > greet && chmod +x greet && "
    "git add -A && git commit -q -m greet >/dev/null 2>&1"
)

#: An engineer that writes a greet that greets every name.
CORRECT = (
    "printf '#!/bin/sh\\necho \"Hello, $1\"\\n' > greet && chmod +x greet && "
    "git add -A && git commit -q -m greet >/dev/null 2>&1"
)

REFUSED_BASE = "Refusing to run: the acceptance checks do not hold on the base"
REFUSED_PLAN = "Refusing to run: the acceptance plan cannot be used"


def _check(
    check_id: str, argv: list[str], *, on_base: str = "fails", held_out: bool = False
) -> dict[str, Any]:
    return {
        "id": check_id,
        "criterion": f"{check_id} holds",
        "argv": argv,
        "onBase": on_base,
        "heldOut": held_out,
    }


def _plan(
    tmp_path: Path, checks: list[dict[str, Any]], *, creates_app: bool = False, script: str = GREETS
) -> Path:
    """A plan directory outside the repository: plan.json and check.sh."""
    plan = tmp_path / "plan"
    plan.mkdir()
    write_executable(plan / "check.sh", script)
    entry = {"createsApp": creates_app, "checks": checks}
    (plan / "plan.json").write_text(json.dumps({"components": {COMP: entry}}), encoding="utf-8")
    return plan


def _with_greet(root: Path, text: str = WRONG_GREET) -> None:
    """Commit an executable greet to the base branch."""
    write_executable(root / "greet", text)
    git_in(root, "add", "greet")
    git_in(root, "commit", "-q", "-m", "greet")


def _greeting_repo(tmp_path: Path, stack: str | None = None) -> Path:
    root = _repo(tmp_path, stack or _stack({"tests": "true"}))
    _with_greet(root)
    return root


def _accept(tmp_path: Path, root: Path, plan: Path, engineer: str = "", *extra: str) -> Run:
    return _factory(tmp_path, root, "--acceptance", str(plan), *extra, engineer=engineer)


def _head_record(root: Path) -> dict[str, Any]:
    (path,) = sorted((root / ".kstrl" / "runs").glob(f"*/acceptance/{COMP}/attempt-*/record.json"))
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return record


def _row(record: dict[str, Any], check_id: str) -> dict[str, Any]:
    (row,) = [row for row in record["checks"] if row["id"] == check_id]
    return row


def _manifest(root: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (root / "scripts" / "kstrl" / "manifest.json").read_text(encoding="utf-8")
    )
    return document


@runs_a_stack
def test_a_check_that_passes_on_the_base_it_should_fail_refuses_before_the_engineer(
    tmp_path: Path,
) -> None:
    """A check that cannot fail tells the change from nothing: ``true`` with
    ``onBase: fails`` passes on the base, so the run exits 2 before any
    engineer call and pins no plan on the manifest."""
    root = _greeting_repo(tmp_path)
    plan = _plan(tmp_path, [_check("vacuous", ["true"])])

    run = _accept(tmp_path, root, plan)

    assert run.code == 2, run.out
    assert REFUSED_BASE in run.out, run.out
    assert f"{COMP}: the check vacuous passes on the base" in run.out, run.out
    assert run.calls == 0, run.out
    assert "acceptanceDigest" not in _manifest(root)


@runs_a_stack
def test_a_check_that_fails_on_the_base_it_should_pass_refuses_before_the_engineer(
    tmp_path: Path,
) -> None:
    """The other contradiction: a check the plan says passes on the base (a
    behaviour the change must keep) fails there, so the plan is wrong about
    the base and the run exits 2 before any engineer call, pinning nothing."""
    root = _greeting_repo(tmp_path)
    plan = _plan(tmp_path, [_check("keeps-ada", ["/bin/sh", "check.sh", "Ada"], on_base="passes")])

    run = _accept(tmp_path, root, plan)

    assert run.code == 2, run.out
    assert REFUSED_BASE in run.out, run.out
    expected = f"{COMP}: the check keeps-ada fails on the base (exit 1); onBase: passes"
    assert expected in run.out, run.out
    assert run.calls == 0, run.out
    assert "acceptanceDigest" not in _manifest(root)


@runs_a_stack
def test_a_check_that_cannot_run_on_the_base_is_refused_as_not_runnable(tmp_path: Path) -> None:
    """A check whose command is not found exits 127 on the base: it measured
    nothing, which is never a failure, and the component does not create the
    app, so the run exits 2 before the engineer."""
    root = _greeting_repo(tmp_path)
    plan = _plan(tmp_path, [_check("missing", ["kstrl-no-such-check-7c1d"])])

    run = _accept(tmp_path, root, plan)

    assert run.code == 2, run.out
    assert REFUSED_BASE in run.out, run.out
    assert f"{COMP}: the check missing could not run on the base (exit 127)" in run.out, run.out
    assert run.calls == 0, run.out


@runs_a_stack
def test_a_component_that_creates_the_app_runs_on_a_base_that_cannot_run(tmp_path: Path) -> None:
    """Decision 11: the base has no greet, so the check exits 127 there. The
    plan marks the component as creating the app, so the base is recorded as
    not runnable and the run goes on; the head's greet passes the check."""
    root = _repo(tmp_path, _stack({"tests": "true"}))
    plan = _plan(tmp_path, [_check("greets-ada", ["/bin/sh", "check.sh", "Ada"])], creates_app=True)

    run = _accept(tmp_path, root, plan, CORRECT)

    assert run.code == 0, run.out
    assert run.calls == 1, run.out
    (base_path,) = sorted((root / ".kstrl" / "runs").glob("*/acceptance/base.json"))
    base = json.loads(base_path.read_text(encoding="utf-8"))
    assert base["components"][COMP]["base"] == BASE_NOT_RUNNABLE, base
    assert base["components"][COMP]["checks"][0]["exit"] == 127, base
    record = _head_record(root)
    assert record["base"] == BASE_NOT_RUNNABLE, record
    assert _row(record, "greets-ada")["verdict"] == "pass", record
    assert f"passed {HEAD_RUNS} of {HEAD_RUNS} runs; base exit 127" in run.out, run.out


def test_with_no_prover_the_checks_run_on_the_host_and_every_record_says_so(
    tmp_path: Path,
) -> None:
    """On a platform with no prover (#700, owner decision 2026-10-05) the
    checks run on the host, and the base reading, the head record and the
    line the terminal prints each carry the one fallback label."""
    root = _greeting_repo(tmp_path)
    plan = _plan(tmp_path, [_check("greets-ada", ["/bin/sh", "check.sh", "Ada"])])

    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    env = {**NO_PROVER, "TMPDIR": str(tmpdir)}

    run = _factory(tmp_path, root, "--acceptance", str(plan), engineer=CORRECT, env=env)

    assert run.code == 0, run.out
    # The fallback has no zone, so each run's copy of the plan is removed after it.
    assert sorted(path.name for path in tmpdir.glob("greets-ada-*")) == [], run.out
    (base_path,) = sorted((root / ".kstrl" / "runs").glob("*/acceptance/base.json"))
    base = json.loads(base_path.read_text(encoding="utf-8"))
    labels = {"setup": FALLBACK_LABEL, "test": FALLBACK_LABEL}
    assert base["isolation"] == labels, base
    record = _head_record(root)
    assert record["isolation"] == labels, record
    assert _row(record, "greets-ada")["verdict"] == "pass", record
    assert f"head {record['headSha'][:12]}; {FALLBACK_LABEL}" in run.out, run.out


@runs_a_stack
def test_a_special_cased_head_fails_the_held_out_check_that_no_engineer_saw(
    tmp_path: Path,
) -> None:
    """The engineer greets only the name it could have been shown. The
    visible check passes and the held-out one fails, which the record and
    the terminal show while the run itself completes: record-only. The
    held-out name is nowhere in the branch, the worktrees or the prompt."""
    root = _greeting_repo(tmp_path)
    hidden = f"Grace{secrets.token_hex(4)}"
    plan = _plan(
        tmp_path,
        [
            _check("greets-ada", ["/bin/sh", "check.sh", "Ada"]),
            _check("greets-hidden", ["/bin/sh", "check.sh", hidden], held_out=True),
        ],
    )

    seen = tmp_path / "seen-by-engineer"
    look = f"grep -r -l '{hidden}' '{root}' > '{seen}' 2>/dev/null; "
    run = _accept(tmp_path, root, plan, look + SPECIAL_CASED)

    assert run.code == 0, run.out
    assert run.calls == 1, run.out
    # What the engineer could find in the repository while it ran: nothing.
    found_by_engineer = seen.read_text(encoding="utf-8")
    assert found_by_engineer == "", found_by_engineer
    record = _head_record(root)
    assert _row(record, "greets-ada")["verdict"] == "pass", record
    held = _row(record, "greets-hidden")
    assert (held["verdict"], held["heldOut"], held["kind"]) == ("fail", True, "held out"), held
    assert f"- greets-hidden (held out): passed 0 of {HEAD_RUNS} runs -> fail" in run.out
    branch = subprocess.run(
        ["git", "-C", str(root), "grep", "-l", hidden, f"kstrl/factory/{COMP}"],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert branch.returncode == 1, branch.stdout + branch.stderr
    worktrees = root / ".kstrl" / "worktrees"
    if worktrees.exists():
        found = subprocess.run(
            ["grep", "-r", "-l", hidden, str(worktrees)],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        assert found.returncode == 1, found.stdout
    assert hidden not in run.prompts
    assert run.prompts, "the stub engineer recorded no prompt"


@runs_a_stack
def test_a_check_that_fails_one_of_its_head_runs_fails(tmp_path: Path) -> None:
    """The check fails its first head run and passes every later one, so it
    passed K-1 of K runs, which fails: nothing passes on a retry. The counter
    it keeps shows exactly K head runs."""
    shared = tmp_path / "shared"
    shared.mkdir()
    counter = shared / "count"
    flaky = (
        '#!/bin/sh\n[ -f "$KSTRL_TREE/made.marker" ] || exit 1\n'
        f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{counter}"\n'
        '[ "$n" -ne 1 ]\n'
    )
    root = _repo(tmp_path, _stack({"tests": "true"}, rung={"writable": [str(shared)]}))
    plan = _plan(tmp_path, [_check("flaky", ["/bin/sh", "check.sh"])], script=flaky)
    engineer = "touch made.marker && git add -A && git commit -q -m marker >/dev/null 2>&1"

    run = _accept(tmp_path, root, plan, engineer)

    assert run.code == 0, run.out
    assert f"- flaky (visible): passed {HEAD_RUNS - 1} of {HEAD_RUNS} runs -> fail" in run.out
    row = _row(_head_record(root), "flaky")
    assert row["verdict"] == "fail", row
    assert len(row["headExits"]) == HEAD_RUNS and row["headExits"][0] == 1, row
    assert counter.read_text(encoding="utf-8").strip() == str(HEAD_RUNS)


#: Records the PR body in $GH_BODY; `pr merge` moves origin's main to the PR
#: head, and `pr view` reports it merged (the fake of test_inbox_waivers.py).
FAKE_GH = """#!/bin/sh
if [ "$1" = "auth" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  prev=""
  for a in "$@"; do
    case "$a" in --head=*) printf '%s' "${a#--head=}" > "$GH_HEAD";; esac
    if [ "$prev" = "--body" ]; then printf '%s' "$a" > "$GH_BODY"; fi
    prev="$a"
  done
  echo "https://github.com/o/r/pull/41"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  git push -q origin "refs/heads/$(cat "$GH_HEAD"):refs/heads/main" || exit 1
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  printf '{"state": "MERGED", "mergeCommit": null}\\n'
  exit 0
fi
echo "[]"
exit 0
"""


@runs_a_stack
def test_the_terminal_and_the_pr_body_show_the_same_lines(tmp_path: Path) -> None:
    """The run prints the head record's lines and opens the component's PR;
    the PR body's ## Acceptance section is exactly those lines, in order."""
    # The origin first: it names the control directory the confirmation goes in.
    root = _repo(tmp_path, _stack({"tests": "true"}), confirm=False)
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    git_in(root, "remote", "add", "origin", str(origin))
    confirm_stack(root)
    _with_greet(root)
    git_in(root, "push", "-q", "-u", "origin", "main")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_executable(bindir / "gh", FAKE_GH)
    body = tmp_path / "pr-body.md"
    plan = _plan(
        tmp_path,
        [
            _check("greets-ada", ["/bin/sh", "check.sh", "Ada"]),
            _check("greets-bob", ["/bin/sh", "check.sh", "Bob"], held_out=True),
        ],
    )
    stub = write_executable(
        tmp_path / "engineer.sh",
        f"#!/bin/sh\ncat > /dev/null\n{CORRECT}\necho '<promise>COMPLETE</promise>'\n",
    )

    code, out = _spawn(
        [
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--root", str(root), "--agent-cmd", str(stub), "--acceptance", str(plan)),
            *("--no-tui", "--yes", "--ui", "plain", "--no-color"),
            *("--max-retries", "0", "--max-parallel", "1"),
            *("--review-mode", "skip", "--contract-check", "skip"),
        ],
        root,
        {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "GH_BODY": str(body),
            "GH_HEAD": str(tmp_path / "pr-head"),
        },
    )

    assert code == 0, out
    lines = body.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Acceptance") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].startswith(("## ", "---")))
    section = [line for line in lines[start:end] if line]
    assert len(section) == 3, section
    printed = out.splitlines()
    first = printed.index(section[0])
    assert printed[first : first + len(section)] == section, out


@pytest.mark.parametrize("case", ["unreadable-directory", "inside-the-repository"])
def test_a_plan_that_cannot_be_taken_whole_is_a_refusal_never_an_empty_plan(
    tmp_path: Path, case: str
) -> None:
    """A plan whose checks sit in a directory kstrl cannot list is refused,
    never read as a plan with fewer checks; so is a plan inside the
    repository, where an engineer could read it. Both exit 2 before any
    rung is proven or engineer called."""
    root = _greeting_repo(tmp_path)
    if case == "inside-the-repository":
        plan = root / "plan"
        plan.mkdir()
        (plan / "plan.json").write_text("{}", encoding="utf-8")
        expected = "is inside the repository"
    else:
        plan = _plan(tmp_path, [_check("hidden", ["/bin/sh", "checks/hidden.sh"])])
        (plan / "checks").mkdir()
        write_executable(plan / "checks" / "hidden.sh", "#!/bin/sh\nexit 1\n")
        (plan / "checks").chmod(0)
        expected = "cannot be read"
    try:
        run = _accept(tmp_path, root, plan)
    finally:
        if case != "inside-the-repository":
            (plan / "checks").chmod(0o755)

    assert run.code == 2, run.out
    assert REFUSED_PLAN in run.out, run.out
    assert expected in run.out, run.out
    assert run.calls == 0, run.out


@runs_a_stack
def test_an_edited_plan_is_refused_naming_both_digests(tmp_path: Path) -> None:
    """The first run's base accepts the plan, which the manifest then pins.
    The operator edits check.sh, a file the check runs; the next run of the
    same plan exits 2 before any engineer call, naming the pinned digest and
    the new one. A run that names no plan at all is refused as well."""
    root = _greeting_repo(tmp_path)
    plan = _plan(tmp_path, [_check("greets-ada", ["/bin/sh", "check.sh", "Ada"])])
    first = _accept(tmp_path, root, plan, CORRECT)
    pinned = _manifest(root).get("acceptanceDigest", "")
    # The edit is to a file the check runs, not to plan.json: the pin covers
    # every file of the plan directory.
    with (plan / "check.sh").open("a", encoding="utf-8") as fh:
        fh.write("# edited\n")

    second = _accept(tmp_path, root, plan, CORRECT)

    assert first.code == 0, first.out
    assert re.fullmatch(r"[0-9a-f]{64}", pinned), _manifest(root)
    assert second.code == 2, second.out
    assert REFUSED_PLAN in second.out, second.out
    found = re.search(
        r"pinned the acceptance plan ([0-9a-f]{12}), and .* now reads ([0-9a-f]{12})", second.out
    )
    assert found is not None, second.out
    assert found.group(1) == pinned[:12] and found.group(2) != pinned[:12], second.out
    assert second.calls == first.calls == 1, second.out
    # A run that names no plan on a manifest that pinned one is refused too:
    # dropping --acceptance must not drop the checks.
    dropped = _factory(tmp_path, root, engineer=CORRECT)
    assert dropped.code == 2, dropped.out
    assert REFUSED_PLAN in dropped.out, dropped.out
    named = f"this plan pinned the acceptance plan {pinned[:12]}, and this run names none"
    assert named in dropped.out, dropped.out
    assert dropped.calls == 1, dropped.out


@runs_a_stack
def test_the_plan_a_person_approves_includes_the_acceptance_checks(tmp_path: Path) -> None:
    """At L1 the run parks the plan for a person, bound to its digest. The
    same manifest parked with and without --acceptance is two different
    plans, so an approval of one never runs the other."""
    root = _greeting_repo(tmp_path)
    plan = _plan(tmp_path, [_check("greets-ada", ["/bin/sh", "check.sh", "Ada"])])
    ladder = {"KSTRL_AUTONOMY_ENABLED": "1"}

    bare = _factory(tmp_path, root, env=ladder)
    parked_bare = str(_manifest(root).get("planAwaitingApproval", ""))
    with_plan = _factory(tmp_path, root, "--acceptance", str(plan), env=ladder)
    parked_with = str(_manifest(root).get("planAwaitingApproval", ""))

    assert (bare.code, with_plan.code) == (1, 1), bare.out + with_plan.out
    assert with_plan.calls == 0, with_plan.out
    assert len(parked_bare) == len(parked_with) == 64, _manifest(root)
    assert parked_bare != parked_with, with_plan.out
