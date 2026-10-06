"""#696 slice 4, the flag day: no confirmed ``[stack]`` is a refusal everywhere.

Before slice 4 a project with no ``[stack]`` ran kstrl's Python defaults
(``uv run pytest``, ``uv run mypy``, ``uv run ruff check .``) or, outside a
Python project, ran nothing and called it unmeasured. Now every paid command
refuses before it spends, exit 2, naming ``[stack]``; ``ks serve`` claims
nothing; ``--no-verify`` is the one way to run with no checks. The legacy
``[verify]`` command keys are retired: ``ks doctor`` files them as a PROPOSED
stack, which only a person approves.

End to end: the real CLI as a subprocess on a temp git repository after the
real ``ks init`` (the harness of ``tests/test_stack_e2e.py``), with a stub
engineer that counts its calls; ``ks serve --once`` in process with a fake
``claude`` architect (the harness of ``tests/test_stack_confirmation_e2e.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind, ItemStatus
from kstrl.workqueue import ItemState, Queue, QueueConfig
from tests.helpers.gitrepo import git_in
from tests.test_isolation_rung import needs_nono
from tests.test_prompt_record import _spec_project
from tests.test_queue_awaiting_answer import _scripted_claude
from tests.test_serve_architect_spend import BLOCKER
from tests.test_stack_e2e import PRD_PATH, _commit, _factory, _repo, _spawn, _stack

#: What every refusal names: the table the operator has to write.
NO_STACK = "kstrl.toml has no [stack]"

#: The legacy [verify] keys an operator upgrading from before the flag day
#: still has, with commands no kstrl default ever spelled, and that pass on
#: the base branch so the approved proposal can run.
LEGACY_VERIFY = (
    'test_command = "test -d ."\n'
    'typecheck_command = "echo typecheck-clean"\n'
    'lint_command = "echo lint-clean"\n'
)


def _engineer(tmp_path: Path) -> tuple[Path, Path]:
    """A stub engineer that appends one line per call and says COMPLETE."""
    calls = tmp_path / "engineer.calls"
    stub = tmp_path / "engineer.sh"
    stub.write_text(
        f"#!/bin/sh\necho call >> '{calls}'\ncat > /dev/null\necho '<promise>COMPLETE</promise>'\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, calls


def _args(command: str, root: Path, stub: Path) -> list[str]:
    """The arguments each paid command takes on the ``_repo`` project."""
    prd = PRD_PATH.format(comp="greeter")
    return {
        "factory": [
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--agent-cmd", str(stub), "--no-tui", "--yes", "--no-prs"),
            *("--review-mode", "skip", "--contract-check", "skip"),
        ],
        "run": ["run", "1", "--prd", prd, "--agent-cmd", str(stub), "--sleep", "0", "--branch", ""],
        "feature": [
            "feature",
            *("--prd", prd, "--agent-cmd", str(stub)),
            *("--no-tui", "--implementation-auto-run", "--understand-iterations", "1"),
        ],
        "check": ["check"],
        "spec": [
            "factory",
            *("--spec", str(root / "spec.md"), "--project-name", "demo"),
            *("--agent-cmd", str(stub), "--no-tui", "--yes", "--no-prs"),
        ],
    }[command] + ["--root", str(root), "--ui", "plain"]


def _with_verify(root: Path, keys: str) -> None:
    """Put ``keys`` under the [verify] header ``ks init`` wrote, committed."""
    text = (root / "kstrl.toml").read_text(encoding="utf-8")
    _commit(root, "kstrl.toml", text.replace("\n[verify]\n", "\n[verify]\n" + keys, 1))


def _stack_items(root: Path) -> list[InboxItem]:
    box = Inbox(root, InboxConfig.load(root))
    return [i for i in box.items() if i.kind is ItemKind.STACK_CONFIRMATION]


PAID = ["factory", "run", "feature", "check"]

#: ``ks factory`` and ``ks run`` prove the #700 isolation rung under a
#: [stack] before they reach the engineer, so getting past the refusal needs
#: macOS and nono; elsewhere they refuse for the rung (Linux CI skips them).
PROCEED = [
    pytest.param("factory", marks=needs_nono),
    pytest.param("run", marks=needs_nono),
    "feature",
    "check",
]


@pytest.mark.parametrize("command", [*PAID, "spec"])
def test_no_stack_refuses_every_paid_command_before_it_spends(tmp_path: Path, command: str) -> None:
    """Exit 2, the refusal names [stack], and the engineer is never called."""
    root = _repo(tmp_path, "", confirm=False)
    _commit(root, "spec.md", "# Spec\n\nBuild a thing.\n")
    stub, calls = _engineer(tmp_path)

    code, out = _spawn(_args(command, root, stub), root, None)

    assert code == 2, out
    assert NO_STACK in out, out
    assert "uv run pytest" not in out, out
    assert not calls.exists(), out


@pytest.mark.parametrize("command", PROCEED)
def test_a_confirmed_stack_lets_every_paid_command_proceed(tmp_path: Path, command: str) -> None:
    """The control: the same commands on the same project, with one
    confirmed check, get past the refusal and reach the engineer (or, for
    ``ks check``, measure the check)."""
    root = _repo(tmp_path, _stack({"tests": "true"}))
    stub, calls = _engineer(tmp_path)

    code, out = _spawn(_args(command, root, stub), root, None)

    assert NO_STACK not in out, out
    if command == "check":
        assert code == 0, out
        assert "stack:tests" in out, out
    else:
        assert calls.exists(), out


def test_ks_retry_refuses_once_the_stack_is_gone(tmp_path: Path) -> None:
    """A component that failed under a confirmed stack, retried after the
    [stack] was removed: refused, exit 2, with no engineer call."""
    root = _repo(tmp_path, _stack({"tests": "true"}))
    first = _factory(tmp_path, root)
    text = (root / "kstrl.toml").read_text(encoding="utf-8")
    _commit(root, "kstrl.toml", text[: text.index("\n[stack]\n")] + "\n")
    manifest_path = root / "scripts" / "kstrl" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["components"][0]["status"] = "failed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "fail it")
    before = first.calls

    code, out = _spawn(
        ["retry", "greeter", "--root", str(root), "--yes", "--ui", "plain"], root, None
    )
    calls = tmp_path / "engineer.calls"
    after = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0

    assert code == 2, out
    assert NO_STACK in out, out
    assert after == before, out


@pytest.mark.usefixtures("no_open_prs")
def test_ks_serve_claims_nothing_with_no_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ks serve --once` with no [stack] says why it waits, charges no
    attempt and calls no architect."""
    root = _spec_project(tmp_path)
    calls = _scripted_claude(tmp_path, monkeypatch, [BLOCKER])
    queue = Queue(root, QueueConfig.load(root))
    queued = queue.add("# Spec\n\nBuild a thing.\n", title="waits", project_name="demo")

    result = CliRunner().invoke(cli, ["serve", "--once", "--root", str(root), "--ui", "plain"])
    item = queue.get(queued.item_id)

    assert "waiting, nothing claimed" in result.output, result.output
    assert NO_STACK in result.output, result.output
    assert item is not None
    assert (item.state, item.attempts) == (ItemState.QUEUED, 0)
    assert list(calls.iterdir()) == []


def test_doctor_files_the_legacy_verify_commands_as_a_proposal_it_never_approves(
    tmp_path: Path,
) -> None:
    """The upgrade path. `ks doctor` reads the retired [verify] command keys,
    files ONE proposed [stack] carrying them, and leaves it OPEN however
    often it runs. The paid commands still refuse, now naming the retired
    key."""
    root = _repo(tmp_path, "", confirm=False)
    _with_verify(root, LEGACY_VERIFY)
    stub, calls = _engineer(tmp_path)

    first_code, first = _spawn(["doctor", "--root", str(root)], root, None)
    _spawn(["doctor", "--root", str(root)], root, None)
    (item,) = _stack_items(root)
    refused_code, refused = _spawn(_args("factory", root, stub), root, None)

    assert first_code != 0, first
    assert item.id[:8] in first, first
    assert item.status is ItemStatus.OPEN
    assert item.occurrences == 2
    proposed = str(item.evidence["toml"])
    for line in (
        '"tests" = "test -d ."',
        '"typecheck" = "echo typecheck-clean"',
        '"lint" = "echo lint-clean"',
    ):
        assert line in proposed.splitlines(), proposed
    assert refused_code == 2, refused
    assert "test_command" in refused and "[stack]" in refused, refused
    assert not calls.exists(), refused


@needs_nono
def test_the_approved_proposal_is_what_lets_ks_factory_run(tmp_path: Path) -> None:
    """Pasting the proposed table in place of the retired keys and approving
    the item with `ks inbox approve` is what lets `ks factory` reach the
    engineer. Needs the #700 rung, so macOS and nono."""
    root = _repo(tmp_path, "", confirm=False)
    _with_verify(root, LEGACY_VERIFY)
    _spawn(["doctor", "--root", str(root)], root, None)
    (item,) = _stack_items(root)
    proposed = str(item.evidence["toml"])

    text = (root / "kstrl.toml").read_text(encoding="utf-8").replace(LEGACY_VERIFY, "", 1)
    _commit(root, "kstrl.toml", text + "\n" + proposed)
    approve_code, approved = _spawn(
        ["inbox", "approve", item.id[:8], "--root", str(root)], root, None
    )
    run = _factory(tmp_path, root)

    assert approve_code == 0, approved
    assert NO_STACK not in run.out, run.out
    assert run.calls >= 1, run.out


def test_a_legacy_key_beside_a_confirmed_stack_is_refused(tmp_path: Path) -> None:
    """A retired key is never silently accepted, even with a confirmed
    stack that would otherwise run: exit 2, naming the key and [stack]."""
    root = _repo(tmp_path, _stack({"tests": "true"}))
    _with_verify(root, 'test_command = "make test-all"\n')

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert "test_command" in run.out and "[stack]" in run.out, run.out
    assert run.calls == 0, run.out


def test_no_verify_is_the_one_way_to_run_with_no_stack(tmp_path: Path) -> None:
    """`--no-verify` runs with no [stack] and no check, and says nothing
    about a stack it was told not to need."""
    root = _repo(tmp_path, "", confirm=False)

    run = _factory(tmp_path, root, "--no-verify")

    assert run.code == 0, run.out
    assert run.calls >= 1, run.out
    assert NO_STACK not in run.out, run.out


def test_doctor_measure_reads_no_stack_as_a_failed_base(tmp_path: Path) -> None:
    """`ks doctor --measure` with no [stack] reaches Phase 1's own no-stack
    row (``verify.NO_STACK_CHECK``) on the base branch: it is a failed
    reading that names [stack], never a pass and never a command kstrl chose."""
    root = _repo(tmp_path, "", confirm=False)

    code, out = _spawn(["doctor", "--measure", "--json", "--root", str(root)], root, None)
    document = json.loads(out[out.index("{") :])
    (row,) = [c for c in document["checks"] if c["name"] == "base_gates"]

    assert code != 0, out
    assert document["verdict"] == "not-ready", out
    assert row["status"] == "fail", row
    assert NO_STACK in row["detail"], row
    assert "uv run pytest" not in out, out
