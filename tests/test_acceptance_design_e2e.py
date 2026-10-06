"""#700 slice 7: the verification designer writes the acceptance checks.

``ks factory --design-acceptance`` asks a model, once per component and in
a checkout of the base, for the checks ``--acceptance`` takes from an
operator. Its reply is checked against the plan vocabulary and asked once
more when it fails, each ask charged to the adversarial call budget. The
plan is written outside the repository, keyed by the plan it was designed
for, so a later run of the same plan asks nothing again. The checks run on
the base and on each head as an operator's do, and stay record only
(owner decision 10): a failing one is recorded, printed and emitted as
advisory, and the component goes on.

End to end: the real ``ks factory`` as a subprocess on a real git
repository after the real ``ks init``, with a confirmed ``[stack]`` and one
stub agent that answers as the designer when its prompt says so and as
the engineer otherwise (the harnesses of ``tests/test_acceptance_e2e.py``
and ``tests/test_stack_e2e.py``).
"""

from __future__ import annotations

import json
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.acceptance import DESIGNER_FILE, HEAD_RUNS
from kstrl.acceptance_design import ACCEPTANCE_PROMPT_VERSION, NO_DESIGNED_CHECK
from kstrl.decompose import spec_digest
from kstrl.statedir import control_dir
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in
from tests.test_acceptance_e2e import (
    COMP,
    SPECIAL_CASED,
    _evidence_root,
    _greeting_repo,
    _head_record,
    _manifest,
    _recheck,
    _row,
    _with_greet,
)
from tests.test_isolation_rung import runs_a_stack
from tests.test_stack_e2e import INSTRUCTIONS, _repo, _spawn, _stack

#: The first line of ACCEPTANCE_PROMPT: how the stub tells a designer call
#: from an engineer call.
DESIGNER_MARK = "You are the verification designer"

REFUSED_DESIGN = "Refusing to run: the verification designer wrote no usable plan"
REFUSED_BASE = "Refusing to run: the acceptance checks do not hold on the base"

#: What the run prints for a designed check the base removed (owner
#: decision of 2026-10-06 on #700).
REMOVED = (
    f"{COMP}: the designed check vacuous passes on the base (exit 0), so it cannot tell "
    "the change from no change: it is removed"
)


@dataclass(frozen=True)
class Designed:
    code: int
    out: str
    #: One line per designer call: the directory it ran in and the commit
    #: checked out there.
    designer: list[str]
    engineer_calls: int
    designer_prompts: str


def _greets(name: str) -> list[str]:
    """A check that the app under test greets ``name``."""
    script = f'out=$("$KSTRL_TREE/greet" {name}) && [ "$out" = "Hello, {name}" ]'
    return ["/bin/sh", "-c", script]


def _entry(checks: list[dict[str, Any]], creates_app: bool = False) -> str:
    return json.dumps({"createsApp": creates_app, "checks": checks})


def _check(check_id: str, argv: list[str], *, held_out: bool = False) -> dict[str, Any]:
    return {
        "id": check_id,
        "criterion": f"{check_id} holds",
        "argv": argv,
        "onBase": "fails",
        "heldOut": held_out,
    }


def _design(
    tmp_path: Path,
    root: Path,
    replies: list[str],
    *extra: str,
    env: dict[str, str] | None = None,
    pause: int = 0,
) -> Designed:
    """The real `ks factory --design-acceptance` with one stub agent. A call
    whose prompt carries DESIGNER_MARK is the designer: it logs where it ran
    and prints the next of ``replies`` (the last one again once they run
    out), after ``pause`` seconds. Any other call is the engineer, which
    special-cases the one name it could have been shown."""
    replies_dir = tmp_path / "replies"
    replies_dir.mkdir(exist_ok=True)
    for number, reply in enumerate(replies, start=1):
        (replies_dir / f"{number}.json").write_text(reply + "\n", encoding="utf-8")
    designer_log = tmp_path / "designer.calls"
    designer_prompts = tmp_path / "designer.prompts"
    engineer_log = tmp_path / "engineer.calls"
    prompt = tmp_path / "prompt.txt"
    stub = write_executable(
        tmp_path / "agent.sh",
        f"""#!/bin/sh
cat > '{prompt}'
if grep -q '{DESIGNER_MARK}' '{prompt}'; then
  echo "$(pwd -P) $(git rev-parse HEAD)" >> '{designer_log}'
  cat '{prompt}' >> '{designer_prompts}'
  sleep {pause}
  n=$(wc -l < '{designer_log}' | tr -d ' ')
  [ -f '{replies_dir}'/"$n".json ] || n={len(replies)}
  cat '{replies_dir}'/"$n".json
  exit 0
fi
echo call >> '{engineer_log}'
{SPECIAL_CASED}
echo '<promise>COMPLETE</promise>'
""",
    )
    code, out = _spawn(
        [
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--root", str(root), "--agent-cmd", str(stub)),
            *("--no-tui", "--yes", "--ui", "plain", "--no-color", "--no-prs"),
            *("--max-retries", "0", "--max-parallel", "1"),
            *("--review-mode", "skip", "--security-mode", "skip", "--contract-check", "skip"),
            "--design-acceptance",
            *extra,
        ],
        root,
        env,
    )

    def lines(path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    said = designer_prompts.read_text(encoding="utf-8") if designer_prompts.exists() else ""
    return Designed(code, out, lines(designer_log), len(lines(engineer_log)), said)


def _pin_spec(root: Path, text: str) -> None:
    """Commit ``text`` as spec.md and pin it on the manifest as the architect
    does (specFile, specPath, specDigest), so the preflight spec check passes
    and the designer is given the spec."""
    (root / "spec.md").write_text(text, encoding="utf-8")
    manifest = _manifest(root)
    manifest["specFile"] = manifest["specPath"] = "spec.md"
    manifest["specDigest"] = spec_digest(text)
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "spec")


def _designed_plans(root: Path) -> list[Path]:
    return sorted((control_dir(root) / "acceptance" / "designed").glob("*"))


def _base_record(root: Path) -> dict[str, Any]:
    (path,) = sorted(_evidence_root(root).glob("*/acceptance/base.json"))
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _head_records(root: Path) -> list[Path]:
    return sorted(_evidence_root(root).glob(f"*/acceptance/{COMP}/attempt-*/record.json"))


@runs_a_stack
def test_the_designer_writes_checks_that_run_record_only_on_the_head(tmp_path: Path) -> None:
    """The designer is asked once, in a checkout of the base, with the
    criteria and the stack. Its held-out check catches the engineer that
    special-cased the one name it could see, and the record, the terminal
    and the event say so; the checks are record only, so the component
    completes with no retry. The plan is kept outside the repository with
    who wrote it."""
    root = _greeting_repo(tmp_path)
    spec_line = f"Greet anyone by name ({secrets.token_hex(4)})."
    _pin_spec(root, f"# Greeter\n\n{spec_line}\n")
    base = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    hidden = f"Grace{secrets.token_hex(4)}"
    reply = _entry(
        [
            _check("greets-ada", _greets("Ada")),
            _check("greets-hidden", _greets(hidden), held_out=True),
        ]
    )

    run = _design(tmp_path, root, [reply])

    assert run.code == 0, run.out
    assert len(run.designer) == 1, run.out
    where, commit = run.designer[0].split()
    assert commit == base, run.designer
    # A throwaway checkout of the base, removed once the designer answered.
    assert Path(where) != root.resolve() and not Path(where).exists(), run.designer
    assert "prints hello" in run.designer_prompts
    assert spec_line in run.designer_prompts
    assert INSTRUCTIONS in run.designer_prompts
    assert run.engineer_calls == 1, run.out
    record = _head_record(root)
    assert record["writtenBy"] == "designer", record
    assert _row(record, "greets-ada")["verdict"] == "pass", record
    assert _row(record, "greets-hidden")["verdict"] == "fail", record
    assert f"- greets-hidden (held out): passed 0 of {HEAD_RUNS} runs -> fail" in run.out
    assert "the verification designer wrote these checks: record only" in run.out, run.out
    (events_path,) = sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    verdicts = [
        (event["data"]["passed"], event["data"]["advisory"])
        for event in map(json.loads, events_path.read_text(encoding="utf-8").splitlines())
        if event["event"] == "verification_result" and event["data"]["phase"] == "acceptance"
    ]
    assert verdicts == [(False, True)], verdicts
    (designed,) = _designed_plans(root)
    designer = json.loads((designed / DESIGNER_FILE).read_text(encoding="utf-8"))
    assert designer["promptVersion"] == ACCEPTANCE_PROMPT_VERSION, designer
    assert designer["asks"] == {COMP: 1}, designer
    assert _manifest(root)["acceptanceDigest"], _manifest(root)


@runs_a_stack
def test_a_reply_that_is_not_a_plan_is_asked_once_more_and_then_refused(tmp_path: Path) -> None:
    """Two replies the plan vocabulary refuses: the designer is asked twice
    and no more, the run exits 2 before any engineer, naming the indexed
    field, and nothing is pinned."""
    root = _greeting_repo(tmp_path)
    bad = _entry([{**_check("greets-ada", _greets("Ada")), "onBase": "maybe"}])

    run = _design(tmp_path, root, [bad, bad])

    assert run.code == 2, run.out
    assert REFUSED_DESIGN in run.out, run.out
    assert "ask 2: components.greeter.checks[0].onBase must be one of fails, passes" in run.out
    assert len(run.designer) == 2, run.out
    assert run.engineer_calls == 0, run.out
    assert _designed_plans(root) == []
    assert "acceptanceDigest" not in _manifest(root)


@runs_a_stack
def test_a_second_ask_is_one_more_adversarial_call(tmp_path: Path) -> None:
    """With one adversarial call allowed, the first ask spends it, so the
    second is not made and the run is refused before any engineer."""
    root = _greeting_repo(tmp_path)

    run = _design(tmp_path, root, ["no plan here"], "--max-adversarial-calls", "1")

    assert run.code == 2, run.out
    assert "ask 2 was not made: the adversarial call budget (1) is exhausted" in run.out
    assert len(run.designer) == 1, run.out
    assert run.engineer_calls == 0, run.out


@runs_a_stack
def test_each_component_gets_its_own_designer_in_its_own_checkout(tmp_path: Path) -> None:
    """Owner decision 1(c): one designer per component, each in a fresh
    context. Two components: two asks, each in its own throwaway checkout
    of the base, each prompt naming its own component and not the other.
    Both plans name a check that cannot run on the base, so the base refuses
    the run before any engineer."""
    root = _repo(tmp_path, _stack({"tests": "true"}), comps=("greeter", "farewell"))
    _with_greet(root)
    base = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    reply = _entry([_check("missing", ["kstrl-no-such-check-7c1d"])])

    run = _design(tmp_path, root, [reply])

    assert run.code == 2, run.out
    assert REFUSED_BASE in run.out, run.out
    assert run.engineer_calls == 0, run.out
    assert len(run.designer) == 2, run.out
    wheres = [line.split()[0] for line in run.designer]
    assert len(set(wheres)) == 2, run.designer
    assert all(line.split()[1] == base for line in run.designer), run.designer
    assert all(not Path(where).exists() for where in wheres), run.designer
    first, second = run.designer_prompts.split(DESIGNER_MARK)[1:]
    assert "Component: greeter" in first and "Component: farewell" not in first
    assert "Component: farewell" in second and "Component: greeter" not in second
    (designed,) = _designed_plans(root)
    designer = json.loads((designed / DESIGNER_FILE).read_text(encoding="utf-8"))
    assert designer["asks"] == {"greeter": 1, "farewell": 1}, designer


@runs_a_stack
def test_a_designed_check_that_cannot_run_on_the_base_refuses_and_the_next_run_asks_nothing_again(
    tmp_path: Path,
) -> None:
    """A designed check that cannot run on the base, or that fails on the
    base it says passes on, refuses the run before the engineer, as an
    operator's does, naming the designed plan to delete to ask again. A
    second run of the same plan uses the same plan, so the designer is not
    asked again and the run is refused the same way."""
    root = _greeting_repo(tmp_path)
    keeps = {**_check("keeps-ada", _greets("Ada")), "onBase": "passes"}
    reply = _entry([keeps, _check("missing", ["kstrl-no-such-check-7c1d"])])

    first = _design(tmp_path, root, [reply])
    second = _design(tmp_path, root, [reply])

    (designed,) = _designed_plans(root)
    for run in (first, second):
        assert run.code == 2, run.out
        assert REFUSED_BASE in run.out, run.out
        assert f"{COMP}: the check missing could not run on the base (exit 127)" in run.out
        assert f"{COMP}: the check keeps-ada fails on the base (exit 1); onBase: passes" in run.out
        assert f"Delete {designed} and run again" in run.out, run.out
        assert run.engineer_calls == 0, run.out
    assert len(second.designer) == 1, second.out


@runs_a_stack
def test_a_designed_check_that_passes_on_the_base_is_removed_and_the_others_run(
    tmp_path: Path,
) -> None:
    """Owner decision of 2026-10-06 on #700: a designed check that passes on
    the base it says fails on is removed, not a refusal of the plan. The
    run output and the base record name it with its base exit, the other
    checks run on the head, and the head record lists the removed check,
    which `ks recheck` accepts without running it. A check that passes on
    the base it says passes on is kept."""
    root = _greeting_repo(tmp_path)
    steady = {**_check("base-ok", ["true"]), "onBase": "passes"}
    reply = _entry([_check("vacuous", ["true"]), _check("greets-ada", _greets("Ada")), steady])

    run = _design(tmp_path, root, [reply])

    assert run.code == 0, run.out
    assert REFUSED_BASE not in run.out, run.out
    assert REMOVED in run.out, run.out
    assert NO_DESIGNED_CHECK not in run.out, run.out
    assert run.engineer_calls == 1, run.out
    base = _base_record(root)
    entry = base["components"][COMP]
    assert entry["base"] == "", base
    rows = [(row["id"], row["exit"], row["removed"]) for row in entry["checks"]]
    assert rows == [("vacuous", 0, True), ("greets-ada", 1, False), ("base-ok", 0, False)], base
    assert base["refused"] == [], base
    (path,) = _head_records(root)
    record = _head_record(root)
    assert [row["id"] for row in record["checks"]] == ["greets-ada", "base-ok"], record
    assert _row(record, "greets-ada")["verdict"] == "pass", record
    assert record["removed"] == ["vacuous"], record
    assert sorted(log.name for log in (path.parent / "logs").iterdir()) == sorted(
        f"{check}-{run}.log"
        for check in ("base-ok", "greets-ada")
        for run in range(1, HEAD_RUNS + 1)
    ), record
    code, out = _recheck(root, path)
    assert code == 0, out
    assert "The recheck agrees with the record." in out, out
    assert "vacuous" not in out, out


@runs_a_stack
def test_a_designed_plan_whose_only_check_passes_on_the_base_has_no_designed_check(
    tmp_path: Path,
) -> None:
    """A component whose every designed check passes on the base keeps no
    check: the base record and the run output say it has no designed
    acceptance check, no check runs on its head, and the run completes."""
    root = _greeting_repo(tmp_path)
    reply = _entry([_check("vacuous", ["true"])])

    run = _design(tmp_path, root, [reply])

    assert run.code == 0, run.out
    assert REMOVED in run.out, run.out
    assert f"{COMP}: {NO_DESIGNED_CHECK}" in run.out, run.out
    assert run.engineer_calls == 1, run.out
    base = _base_record(root)
    entry = base["components"][COMP]
    assert entry["base"] == NO_DESIGNED_CHECK, base
    assert [(row["id"], row["removed"]) for row in entry["checks"]] == [("vacuous", True)], base
    assert base["refused"] == [], base
    assert _head_records(root) == [], run.out
    assert f"Acceptance for {COMP}" not in run.out, run.out


@runs_a_stack
def test_a_designer_past_the_review_timeout_is_stopped_and_asked_once_more(
    tmp_path: Path,
) -> None:
    """The designer is bound by [factory] review_timeout_seconds, the [review]
    selection's own limit (decision 12). A designer that would answer a
    valid plan only after that limit is stopped, asked once more, stopped
    again, and the run is refused before any engineer, naming the limit.
    Bound by any other clock, or by none, it answers and the run goes on."""
    root = _greeting_repo(tmp_path)
    reply = _entry([_check("greets-hidden", _greets("Grace"), held_out=True)])

    run = _design(
        tmp_path,
        root,
        [reply],
        env={"KSTRL_FACTORY_REVIEW_TIMEOUT_SECONDS": "2"},
        pause=20,
    )

    assert run.code == 2, run.out
    assert REFUSED_DESIGN in run.out, run.out
    assert f"{COMP}: ask 2: the agent timed out after 2.0s" in run.out, run.out
    assert len(run.designer) == 2, run.out
    assert run.engineer_calls == 0, run.out
    assert _designed_plans(root) == []


def test_an_operator_plan_and_a_designed_one_are_refused_together(tmp_path: Path) -> None:
    """--acceptance and --design-acceptance name two plans: the run exits 2
    before anything is asked."""
    root = _greeting_repo(tmp_path)

    run = _design(tmp_path, root, ["{}"], "--acceptance", str(tmp_path / "plan"))

    assert run.code == 2, run.out
    assert "pass one of them" in run.out, run.out
    assert run.designer == [] and run.engineer_calls == 0, run.out
