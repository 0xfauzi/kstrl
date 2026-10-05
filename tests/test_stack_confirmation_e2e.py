"""#696 slice 3: a ``[stack]`` runs nothing until a person confirms its exact text.

Before slice 3 a ``[stack]`` was used as loaded: `ks doctor --measure` ran its
checks and `ks factory` paid the engineer with nobody asked. The confirmation
is an APPROVED ``stack_confirmation`` inbox item bound to the stack's digest
(owner decision 1(b)); only the most recent approval counts, so a changed
table, and a revert to an older confirmed one, refuse until confirmed again.
With ``[inbox] enabled = false`` an answer at the prompt holds for that run
only (decision 1(i)). A hand-written stack needs confirming like any other
(decision 2): every stack here is hand-written.

End to end: the real CLI as a subprocess on a temp git repository after the
real ``ks init`` (the harness of ``tests/test_stack_e2e.py``), with a stub
engineer that counts its calls; `ks decompose` and `ks serve --once` in
process through ``CliRunner`` with a fake ``claude`` architect (the harness
of ``tests/test_queue_awaiting_answer.py``), because serve's child inherits
this process's PATH.
"""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind, ItemStatus
from kstrl.manifest import Manifest
from kstrl.stack import load_stack
from kstrl.workqueue import ItemState, Queue, QueueConfig
from tests.helpers import astwalk
from tests.helpers.plan_approval import approve_plan
from tests.helpers.stack_confirmation import confirm_stack
from tests.test_isolation_rung import needs_nono
from tests.test_prompt_record import ONE_COMPONENT, _spec_project
from tests.test_queue_awaiting_answer import _scripted_claude
from tests.test_serve_architect_spend import BLOCKER
from tests.test_stack_e2e import (
    FUSE_SECONDS,
    PY,
    Run,
    _child_env,
    _commit,
    _factory,
    _repo,
    _spawn,
    _stack,
)

GREEN = _stack({"tests": "true"})

#: The headline `ks factory` and the run's pre-spend check print for an
#: unconfirmed stack (``cli._stack_checkpoint``, ``factory._preflight_stack``).
NOT_CONFIRMED = "Refusing to run: the [stack] in kstrl.toml is not confirmed"


def _digest(root: Path) -> str:
    stack = load_stack(root)
    assert stack is not None
    return stack.digest


def _stack_items(root: Path) -> list[InboxItem]:
    box = Inbox(root, InboxConfig.load(root))
    return [i for i in box.items() if i.kind is ItemKind.STACK_CONFIRMATION]


def _restack(root: Path, table: str) -> None:
    """Replace the ``[stack]`` at the end of kstrl.toml with ``table``, committed."""
    text = (root / "kstrl.toml").read_text(encoding="utf-8")
    _commit(root, "kstrl.toml", text[: text.index("\n[stack]\n")] + "\n" + table)


def _events(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((root / ".kstrl" / "runs").glob("*/events.jsonl")):
        text = path.read_text(encoding="utf-8")
        rows += [json.loads(line) for line in text.splitlines() if line]
    return rows


def _stack_resolutions(root: Path) -> list[str]:
    """``decided_by`` of every ``checkpoint_resolved`` the runs under ``root``
    recorded for their stack."""
    return [
        r["data"]["decided_by"]
        for r in _events(root)
        if r["event"] == "checkpoint_resolved" and r["data"]["kind"] == "stack"
    ]


def _ks(root: Path, *args: str) -> tuple[int, str]:
    """The real CLI in a subprocess: its exit code and its output."""
    return _spawn([*args, "--root", str(root)], root, None)


def _approve(root: Path, item: InboxItem) -> tuple[int, str]:
    return _spawn(["inbox", "approve", item.id[:8], "--root", str(root)], root, None)


def _factory_on_a_terminal(tmp_path: Path, root: Path, answer: str, env: dict[str, str]) -> Run:
    """`ks factory --manifest` with a pseudo-terminal on stdin, typing ``answer``.

    The stub engineer and its call log are ``_factory``'s, so calls add up
    across both. Bounded by the same fuse, and its process group is killed
    if it outlives it.
    """
    calls = tmp_path / "engineer.calls"
    stub = tmp_path / "engineer.sh"
    stub.write_text(
        f"#!/bin/sh\necho call >> '{calls}'\ncat > /dev/null\necho '<promise>COMPLETE</promise>'\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [
            PY,
            "-m",
            "kstrl",
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--root", str(root), "--agent-cmd", str(stub)),
            *("--no-tui", "--yes", "--ui", "plain", "--no-color", "--no-prs"),
            *("--max-retries", "0", "--max-parallel", "1"),
            *("--review-mode", "skip", "--contract-check", "skip"),
        ],
        cwd=root,
        env=_child_env(env),
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    os.close(slave)
    chunks: list[bytes] = []
    try:
        os.write(master, answer.encode("utf-8"))
        deadline = time.monotonic() + FUSE_SECONDS
        assert child.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"ks factory outlived its {FUSE_SECONDS}s fuse (hung)"
            ready, _, _ = select.select([child.stdout, master], [], [], remaining)
            if master in ready:
                try:
                    os.read(master, 4096)  # the terminal's echo; drained, not read
                except OSError:
                    pass
            if child.stdout in ready:
                data = os.read(child.stdout.fileno(), 4096)
                if not data:
                    break
                chunks.append(data)
        code = child.wait(timeout=FUSE_SECONDS)
    finally:
        os.close(master)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    count = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
    return Run(code, b"".join(chunks).decode("utf-8", "replace"), count, "")


# --- 1 and 2: refused before the architect, then approved -------------------


def test_an_unconfirmed_stack_refuses_before_the_architect_and_files_one_item(
    tmp_path: Path,
) -> None:
    """1. `ks factory --spec` exits 2 before the architect runs and files the
    stack_confirmation item. A second command on the same stack bumps that
    item; it never files a second one."""
    root = _repo(tmp_path, GREEN, confirm=False)
    (root / "spec.md").write_text("# Spec\n\nBuild a thing.\n", encoding="utf-8")
    architect_calls = tmp_path / "architect.calls"
    architect = tmp_path / "architect.sh"
    architect.write_text(f"#!/bin/sh\necho call >> '{architect_calls}'\nexit 1\n", encoding="utf-8")
    architect.chmod(0o755)

    code, out = _ks(
        root,
        "factory",
        *("--spec", str(root / "spec.md"), "--project-name", "demo"),
        *("--agent-cmd", str(architect), "--no-tui", "--yes", "--ui", "plain", "--no-color"),
    )
    again = _factory(tmp_path, root)

    assert code == 2, out
    assert NOT_CONFIRMED in out, out
    assert not architect_calls.exists(), out
    (item,) = _stack_items(root)
    assert item.status is ItemStatus.OPEN
    assert item.evidence["stack_digest"] == _digest(root)
    assert f"ks inbox approve {item.id[:8]}" in out, out
    assert (again.code, again.calls) == (2, 0), again.out
    (bumped,) = _stack_items(root)
    assert (bumped.id, bumped.occurrences) == (item.id, 2)


@needs_nono
def test_ks_inbox_approve_confirms_the_stack_and_the_run_proceeds(tmp_path: Path) -> None:
    """2. Approving the filed item is the confirmation: the next run pays the
    engineer and records in events.jsonl that the inbox confirmed its stack."""
    root = _repo(tmp_path, GREEN, confirm=False)
    refused = _factory(tmp_path, root)
    (item,) = _stack_items(root)

    code, out = _approve(root, item)
    run = _factory(tmp_path, root)

    assert (refused.code, refused.calls) == (2, 0), refused.out
    assert code == 0, out
    assert run.calls == 1, run.out
    assert NOT_CONFIRMED not in run.out
    assert _stack_resolutions(root) == ["inbox"]


@needs_nono
def test_approving_an_item_for_a_stack_kstrl_toml_no_longer_holds_is_refused(
    tmp_path: Path,
) -> None:
    """`ks inbox approve` dispatches on the ``stack:`` key: an item filed for
    text kstrl.toml has since replaced is refused with nothing approved,
    because its approval would become the newest one and un-confirm the
    stack that is there. Once kstrl.toml holds that text again, approving
    the same item confirms it: the newest APPROVAL counts, not the item
    filed last (the item for the other text was filed after it)."""
    root = _repo(tmp_path, GREEN, confirm=False)
    _factory(tmp_path, root)
    (item,) = _stack_items(root)
    _restack(root, _stack({"tests": "true || true"}))

    code, out = _approve(root, item)
    statuses = [i.status for i in _stack_items(root)]
    _factory(tmp_path, root)
    (other,) = [i for i in _stack_items(root) if i.id != item.id]
    other_code, other_out = _approve(root, other)
    _restack(root, GREEN)
    again_code, again_out = _approve(root, item)
    run = _factory(tmp_path, root)

    assert code == 2, out
    assert "nothing was approved" in out, out
    assert statuses == [ItemStatus.OPEN]
    assert (other_code, again_code) == (0, 0), other_out + again_out
    assert NOT_CONFIRMED not in run.out, run.out
    assert (run.code, run.calls) == (0, 1), run.out


# --- 3 and 4: only the most recent approval of this exact text --------------


def test_appending_or_true_refuses_naming_both_digests(tmp_path: Path) -> None:
    """3. The `|| true` a merged PR could add makes a new digest; the old
    approval does not carry over, and the refusal names both."""
    root = _repo(tmp_path, GREEN)
    confirmed = _digest(root)
    _restack(root, _stack({"tests": "true || true"}))

    run = _factory(tmp_path, root)

    assert (run.code, run.calls) == (2, 0), run.out
    assert f"has changed since {confirmed[:12]}" in run.out, run.out
    assert f"it now reads {_digest(root)[:12]}" in run.out, run.out


def test_reverting_to_an_older_confirmed_stack_refuses(tmp_path: Path) -> None:
    """4. Only the most recent approval counts: A then B were confirmed, so
    going back to A needs a new confirmation."""
    root = _repo(tmp_path, GREEN)
    first = _digest(root)
    _restack(root, _stack({"tests": "true", "lint": "true"}))
    second = confirm_stack(root)
    _restack(root, GREEN)

    run = _factory(tmp_path, root)

    assert _digest(root) == first
    assert (run.code, run.calls) == (2, 0), run.out
    assert f"has changed since {second[:12]}" in run.out, run.out
    assert f"it now reads {first[:12]}" in run.out, run.out


@pytest.mark.parametrize(
    "withdraw", [("reject", "--comment", "wrong"), ("snooze",)], ids=["reject", "snooze"]
)
def test_withdrawing_the_newest_confirmation_brings_back_no_older_one(
    tmp_path: Path, withdraw: tuple[str, ...]
) -> None:
    """Only the most recent approval counts: A then B were confirmed, and B's
    approval is then withdrawn in the inbox. B no longer runs, and neither
    does A once kstrl.toml goes back to it: A's approval is older than B's,
    so A needs a new confirmation."""
    root = _repo(tmp_path, GREEN)
    first = _digest(root)
    _restack(root, _stack({"tests": "true", "lint": "true"}))
    second = confirm_stack(root)
    (newest,) = [i for i in _stack_items(root) if i.evidence["stack_digest"] == second]
    code, out = _ks(root, "inbox", withdraw[0], newest.id[:8], *withdraw[1:])
    under_b = _factory(tmp_path, root)
    _restack(root, GREEN)

    run = _factory(tmp_path, root)

    assert code == 0, out
    assert (under_b.code, under_b.calls) == (2, 0), under_b.out
    assert _digest(root) == first
    assert (run.code, run.calls) == (2, 0), run.out
    assert f"the newest confirmation, of {second[:12]}, was" in run.out, run.out


# --- 5: the plan carries the stack it was made under ------------------------


def test_a_plan_made_under_another_stack_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5. `ks decompose` pins the stack's digest in the manifest; once a
    different stack is confirmed, `ks factory` refuses that plan before the
    engineer, naming both."""
    root = _repo(tmp_path, GREEN)
    planned = _digest(root)
    _commit(root, "spec.md", "# Spec\n\nBuild a thing.\n")
    _scripted_claude(tmp_path, monkeypatch, [ONE_COMPONENT])
    spec = str(root / "spec.md")
    decomposed = CliRunner().invoke(
        cli, ["decompose", "--spec", spec, "--project-name", "demo", "--root", str(root)]
    )
    manifest = Manifest.load(root / "scripts" / "kstrl" / "manifest.json")
    _restack(root, _stack({"tests": "true", "lint": "true"}))
    now = confirm_stack(root)

    run = _factory(tmp_path, root)

    assert decomposed.exit_code == 0, decomposed.output
    assert manifest.stack_digest == planned
    assert (run.code, run.calls) == (2, 0), run.out
    assert f"made under the [stack] {planned[:12]}" in run.out, run.out
    assert f"it now reads {now[:12]}" in run.out, run.out


@needs_nono
def test_an_approved_plan_does_not_carry_over_to_another_stack(tmp_path: Path) -> None:
    """The plan digest includes the stack digest: an L1 plan approved under
    one stack is not approved under another, even when the manifest's pin
    and the confirmed stack agree again."""
    root = _repo(tmp_path, GREEN)
    path = root / "scripts" / "kstrl" / "manifest.json"
    manifest = Manifest.load(path)
    manifest.stack_digest = _digest(root)
    manifest.save(path)
    approve_plan(root, manifest)
    _restack(root, _stack({"tests": "true", "lint": "true"}))
    manifest.stack_digest = confirm_stack(root)
    manifest.save(path)

    run = _factory(
        tmp_path, root, "--review-agent-cmd", "true", env={"KSTRL_AUTONOMY_ENABLED": "1"}
    )

    assert run.calls == 0, run.out
    assert run.code == 1, run.out
    assert "PLAN AWAITING APPROVAL" in run.out, run.out


# --- 6: ks serve waits before it claims ---------------------------------------


@pytest.mark.usefixtures("no_open_prs")
def test_ks_serve_claims_nothing_until_the_stack_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6. While the stack is unconfirmed `ks serve --once` claims nothing,
    charges no attempt and poisons nothing, and a second cycle bumps the one
    item rather than filing another. After `ks inbox approve` it claims."""
    root = _spec_project(tmp_path)
    (root / "kstrl.toml").write_text(GREEN, encoding="utf-8")
    _commit(root, "kstrl.toml", GREEN)
    calls = _scripted_claude(tmp_path, monkeypatch, [BLOCKER])
    queue = Queue(root, QueueConfig.load(root))
    queued = queue.add("# Spec\n\nBuild a thing.\n", title="waits", project_name="demo")

    def serve_once() -> Result:
        return CliRunner().invoke(cli, ["serve", "--once", "--root", str(root), "--ui", "plain"])

    first, second = serve_once(), serve_once()
    waiting = queue.get(queued.item_id)
    (item,) = _stack_items(root)
    approved = CliRunner().invoke(cli, ["inbox", "approve", item.id[:8], "--root", str(root)])
    third = serve_once()
    claimed = queue.get(queued.item_id)

    for cycle in (first, second):
        assert "waiting, nothing claimed" in cycle.output, cycle.output
        assert "is not confirmed" in cycle.output, cycle.output
    assert waiting is not None
    assert (waiting.state, waiting.attempts) == (ItemState.QUEUED, 0)
    assert item.occurrences == 2
    assert approved.exit_code == 0, approved.output
    assert claimed is not None
    assert claimed.attempts == 1, third.output
    assert claimed.state is not ItemState.QUEUED, third.output
    assert len(list(calls.iterdir())) == 1, third.output


# --- 7: an inbox kstrl cannot read is a refusal ---------------------------------


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        (lambda path: path.write_bytes(b"\xff\xfe not utf-8\n"), "cannot be read"),
        (lambda path: path.write_text(path.read_text() + "{torn\n"), "are not JSON objects"),
        (
            lambda path: path.write_text(
                path.read_text()
                + json.dumps(
                    {
                        "id": "f" * 32,
                        "kind": "stack_confirmation",
                        "title": "t",
                        "created_at": "2026-10-04T00:00:00+00:00",
                        "status": "approved",
                        "dedupe_key": "stack:nope",
                        "evidence": {"stack_digest": "nope"},
                    }
                )
                + "\n"
            ),
            "is a stack decision kstrl cannot read",
        ),
    ],
    ids=["undecodable", "torn-line", "malformed-stack-record"],
)
def test_an_unreadable_inbox_refuses_the_stack(tmp_path: Path, damage: Any, said: str) -> None:
    """7. A confirmed stack whose inbox kstrl can no longer read in full is
    refused, never read as the parts it could parse."""
    root = _repo(tmp_path, GREEN)
    damage(Inbox(root, InboxConfig.load(root)).path)

    run = _factory(tmp_path, root)

    assert (run.code, run.calls) == (2, 0), run.out
    assert "the inbox is unreadable" in run.out, run.out
    assert said in run.out, run.out


# --- 8: with the inbox disabled, a confirmation holds for that run only -----


@needs_nono
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pseudo-terminal")
def test_with_the_inbox_disabled_a_confirmation_at_the_prompt_holds_for_that_run_only(
    tmp_path: Path,
) -> None:
    """8. Decision 1(i). Nobody to ask refuses; Enter at the prompt decides
    later and refuses; Confirm runs this run, recorded in events.jsonl as the
    operator's; the next run is asked again; `ks serve` waits and says why."""
    root = _repo(tmp_path, GREEN, confirm=False)
    off = {"KSTRL_INBOX_ENABLED": "0"}

    unasked = _factory(tmp_path, root, env=off)
    later = _factory_on_a_terminal(tmp_path, root, "\n", off)
    confirmed = _factory_on_a_terminal(tmp_path, root, "1\n", off)
    next_run = _factory(tmp_path, root, env=off)
    serve_code, served = _spawn(["serve", "--once", "--root", str(root)], root, off)

    assert (unasked.code, unasked.calls) == (2, 0), unasked.out
    assert "[inbox] is disabled" in unasked.out, unasked.out
    assert later.code == 2, later.out
    assert confirmed.calls == 1, confirmed.out
    assert _stack_resolutions(root) == ["operator"]
    assert next_run.code == 2, next_run.out
    assert next_run.calls == 1, "the confirmation held for its own run only"
    assert serve_code == 0, served
    assert "waiting, nothing claimed" in served, served
    assert "[inbox] is disabled" in served, served
    assert _stack_items(root) == []


@needs_nono
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pseudo-terminal")
def test_at_the_prompt_reject_is_recorded_and_confirm_holds_in_the_inbox(
    tmp_path: Path,
) -> None:
    """Decision 1(b) at the prompt, with the inbox on: Reject refuses and
    records a REJECTED item, so the next run is refused too; Confirm runs
    and records an APPROVED item, so the next run needs no answer."""
    root = _repo(tmp_path, GREEN, confirm=False)

    rejected = _factory_on_a_terminal(tmp_path, root, "2\n", {})
    after_reject = _factory(tmp_path, root)
    statuses = sorted(str(i.status) for i in _stack_items(root))
    confirmed = _factory_on_a_terminal(tmp_path, root, "1\n", {})
    next_run = _factory(tmp_path, root)

    assert (rejected.code, rejected.calls) == (2, 0), rejected.out
    assert (after_reject.code, after_reject.calls) == (2, 0), after_reject.out
    assert statuses == ["open", "rejected"]
    assert (confirmed.code, confirmed.calls) == (0, 1), confirmed.out
    assert next_run.code == 0, next_run.out
    assert NOT_CONFIRMED not in next_run.out, next_run.out
    assert _stack_resolutions(root) == ["inbox", "inbox"]


# --- the consumers: no entry runs an unconfirmed stack's commands -------------


@pytest.mark.parametrize(
    "command",
    [
        ("factory",),
        ("run",),
        ("check",),
        ("doctor", "--measure"),
        ("feature",),
    ],
    ids=["factory", "run", "check", "doctor-measure", "feature"],
)
def test_no_entry_runs_a_command_of_an_unconfirmed_stack(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    """Every command that runs a stack's commands refuses an unconfirmed
    one, and neither its check nor its setup runs. The runners refuse an
    unconfirmed stack themselves, so a path that reached one without
    asking ``confirmed_stack`` still runs nothing."""
    check_ran = tmp_path / "check.ran"
    setup_ran = tmp_path / "setup.ran"
    table = _stack({"tests": f"touch '{check_ran}'"}, setup=f"touch '{setup_ran}'")
    root = _repo(tmp_path, table, confirm=False)
    stub = tmp_path / "engineer.sh"
    calls = tmp_path / "engineer.calls"
    stub.write_text(f"#!/bin/sh\necho call >> '{calls}'\ncat > /dev/null\n", encoding="utf-8")
    stub.chmod(0o755)
    args: dict[str, list[str]] = {
        "factory": [
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--agent-cmd", str(stub), "--no-tui", "--yes", "--no-prs"),
        ],
        "run": [
            "1",
            *("--prd", "scripts/kstrl/feature/greeter/prd.json", "--agent-cmd", str(stub)),
            *("--sleep", "0", "--branch", ""),
        ],
        "check": [],
        "doctor": [],
        "feature": [
            *("--prd", "scripts/kstrl/feature/greeter/prd.json", "--agent-cmd", str(stub)),
            *("--no-tui", "--implementation-auto-run", "--understand-iterations", "1"),
        ],
    }

    code, out = _spawn([*command, *args[command[0]], "--root", str(root)], root, None)

    assert code != 0, out
    assert "is not confirmed" in out, out
    assert not check_ran.exists(), out
    assert not setup_ran.exists(), out
    assert not calls.exists(), out


# --- the static half: one place makes a stack usable --------------------------


#: Every module that writes ``unconfirmed``, the field that says why a stack
#: may not run, and how many times. ``stack.py`` is the only module that may
#: CLEAR it (``confirmed_stack``); the others only read it to refuse. A new
#: row is a new place that touches it: read it before re-pinning.
EXPECTED_UNCONFIRMED_SITES: dict[str, int] = {
    "cli.py": 1,
    "factory.py": 6,
    "stack.py": 5,
    "verify.py": 2,
}


def test_only_confirmed_stack_makes_a_stack_usable() -> None:
    astwalk.assert_census(
        sources=astwalk.package_sources(),
        sees=astwalk.spells("unconfirmed"),
        expected=EXPECTED_UNCONFIRMED_SITES,
        control=['replace(stack, unconfirmed="")', 'replace(stack, **{"unconfirmed": ""})'],
        message="a module names Stack.unconfirmed somewhere new, or a count moved.",
    )
