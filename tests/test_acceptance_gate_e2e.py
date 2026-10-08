"""#700 slice 6: an operator-written acceptance plan gates the head.

A check that did not pass on the head no longer just goes on the record.
A visible one goes to the engineer's retry, with its criterion, its
command and what it printed, while a held-out one in the same retry is
named by its id alone (owner decision 3). A held-out check that fails
halts the component with no retry, on a halted_run item that names the
failing checks and the commit. A person may merge over exactly those
checks on exactly that commit by approving the item and running
``ks retry`` (decision 14): the commit is judged again with no engineer,
and the record, the terminal and the PR body name the approval, who gave
it and when. Since slice 10a the engineer can dispute a visible check in
its progress entry: a dispute kstrl can take halts the component as a
failed held-out check does, and any other is rejected and the check
result stands.

End to end: the real ``ks factory``, ``ks inbox approve`` and ``ks retry``
as subprocesses on a real git repository after the real ``ks init``, with
a confirmed ``[stack]``, a bare origin, a stub ``gh`` that keeps the PR
body and a stub engineer that counts its calls (the harnesses of
``tests/test_acceptance_e2e.py`` and ``tests/test_stack_e2e.py``).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import cast

from textual.widgets import DataTable, Static

from kstrl.acceptance import HEAD_RUNS
from kstrl.context import ACCEPTANCE_RETRY_PROMPT
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind
from kstrl.manifest import Manifest
from kstrl.tui.screens.inbox import InboxScreen
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in
from tests.helpers.rendered import flat
from tests.helpers.settle import mounted, settled
from tests.helpers.stack_confirmation import confirm_stack
from tests.helpers.tui_screens import home_app
from tests.test_acceptance_e2e import (
    COMP,
    CORRECT,
    FAKE_GH,
    SPECIAL_CASED,
    _accept,
    _check,
    _evidence_root,
    _greeting_repo,
    _plan,
    _recheck,
    _with_greet,
)
from tests.test_isolation_rung import runs_a_stack
from tests.test_stack_e2e import _factory, _repo, _spawn, _stack

BRANCH = f"kstrl/factory/{COMP}"

#: The greeting check, plus ``--marker``: the tree under test holds made.marker.
GREETS_OR_MARKER = (
    "#!/bin/sh\n"
    'if [ "$1" = "--marker" ]; then\n'
    '  [ -f "$KSTRL_TREE/made.marker" ] || { echo "no made.marker in the tree"; exit 1; }\n'
    "  exit 0\n"
    "fi\n"
    'out=$("$KSTRL_TREE/greet" "$1") || exit $?\n'
    '[ "$out" = "Hello, $1" ] || { echo "expected Hello, $1, got $out"; exit 1; }\n'
)


#: The progress log of the component: where its engineer writes a dispute.
PROGRESS = f"scripts/kstrl/feature/{COMP}/progress.txt"

#: The one form of a dispute line, as DEFAULT_PROMPT gives it to the engineer.
DISPUTE_FORM = "Dispute: <check-id>: <cause>"


def _progress(*lines: str) -> str:
    """An engineer step that appends ``lines`` to its progress log and
    commits it. No line may hold a quote or a percent sign."""
    text = "\\n".join(lines)
    return f"printf '{text}\\n' >> {PROGRESS} && git add -A && git commit -q -m log >/dev/null 2>&1"


def _counted(counter: Path, first: str, later: str) -> str:
    """An engineer that runs ``first`` on its first call and ``later`` after."""
    return (
        f'n=$(( $(cat "{counter}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{counter}"; '
        f'if [ "$n" -eq 1 ]; then {first}; else {later}; fi'
    )


def _halts(root: Path) -> list[InboxItem]:
    """Every halted_run item the inbox holds, open or decided."""
    box = Inbox(root, InboxConfig.load(root))
    return [item for item in box.scan().folded_items() if item.kind is ItemKind.HALTED_RUN]


def _status(root: Path) -> tuple[str, str]:
    comp = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").get_component(COMP)
    assert comp is not None
    return comp.status, comp.failed_phase


def _acceptance_events(root: Path) -> list[tuple[bool, bool]]:
    """(passed, advisory) of each acceptance verification_result the latest run emitted."""
    run_id = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").run_id
    path = root / ".kstrl" / "runs" / run_id / "events.jsonl"
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        data = record.get("data")
        if record.get("event") == "verification_result" and data.get("phase") == "acceptance":
            found.append((data["passed"], data["advisory"]))
    return found


def _tip(root: Path) -> str:
    """The component branch's commit."""
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", BRANCH],
        capture_output=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


@runs_a_stack
def test_a_visible_failure_retries_with_the_check_and_never_the_held_out_one(
    tmp_path: Path,
) -> None:
    """Attempt 1 deletes greet and writes no marker: the visible marker check
    fails, and the held-out greeting cannot run (exit 127), which is never a
    failure, so nothing halts. The engineer is retried and told the marker
    check's criterion, command and output, and the held-out check by its id
    alone. Attempt 2 writes a correct greet and the marker, and every check
    passes. Neither prompt holds the held-out name."""
    root = _greeting_repo(tmp_path)
    hidden = f"Grace{secrets.token_hex(4)}"
    plan = _plan(
        tmp_path,
        [
            _check("has-marker", ["/bin/sh", "check.sh", "--marker"]),
            _check("greets-hidden", ["/bin/sh", "check.sh", hidden], held_out=True),
        ],
        script=GREETS_OR_MARKER,
    )
    deleted = "git rm -q greet && git commit -q -m rm >/dev/null 2>&1"
    fixed = CORRECT.replace("git add -A", "touch made.marker && git add -A")
    engineer = _counted(tmp_path / "count", deleted, fixed)

    run = _accept(tmp_path, root, plan, engineer, "--max-retries", "1")

    assert run.code == 0, run.out
    assert run.calls == 2, run.out
    assert _status(root) == ("completed", ""), run.out
    assert f"- has-marker (visible): passed 0 of {HEAD_RUNS} runs -> fail" in run.out, run.out
    unrun = ", ".join(["127"] * HEAD_RUNS)
    assert f"- greets-hidden (held out): did not run ({unrun})" in run.prompts, run.prompts
    assert ACCEPTANCE_RETRY_PROMPT.split("\n")[0][:60] in run.prompts, run.prompts
    for told in (
        "criterion: has-marker holds",
        "check.sh --marker",
        "| no made.marker in the tree",
    ):
        assert told in run.prompts, run.prompts
    assert hidden not in run.prompts, run.prompts


@runs_a_stack
def test_a_check_that_passes_its_first_head_run_and_fails_a_later_one_halts(
    tmp_path: Path,
) -> None:
    """The held-out check exits 0 on its first head run and 1 on every later
    one, so it passed one of K runs and is not satisfactory: the gate halts
    the component with no retry. A check judged on its first run alone
    would have passed. The counter it keeps shows exactly K head runs."""
    shared = tmp_path / "shared"
    shared.mkdir()
    counter = shared / "count"
    late = (
        '#!/bin/sh\n[ -f "$KSTRL_TREE/made.marker" ] || exit 1\n'
        f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{counter}"\n'
        '[ "$n" -eq 1 ]\n'
    )
    root = _repo(tmp_path, _stack({"tests": "true"}, rung={"writable": [str(shared)]}))
    plan = _plan(tmp_path, [_check("late", ["/bin/sh", "check.sh"], held_out=True)], script=late)
    engineer = "touch made.marker && git add -A && git commit -q -m marker >/dev/null 2>&1"

    run = _accept(tmp_path, root, plan, engineer, "--max-retries", "1")

    assert run.code == 1, run.out
    assert run.calls == 1, run.out
    assert _status(root) == ("failed", "acceptance"), run.out
    assert counter.read_text(encoding="utf-8").strip() == str(HEAD_RUNS), run.out
    assert _acceptance_events(root) == [(False, False)], run.out
    (item,) = _halts(root)
    assert item.evidence["check"] == "late", item.evidence


@runs_a_stack
async def test_a_held_out_failure_halts_and_an_approval_merges_over_it(tmp_path: Path) -> None:
    """The engineer special-cases the visible name, so the held-out check
    fails and the component halts with no retry. The TUI's inbox says what
    approving that open halt does. Approving it, then ``ks retry``, keeps
    that commit, judges it again with no engineer and merges it: the PR
    body names the approval, who gave it and when."""
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
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GH_BODY": str(body),
        "GH_HEAD": str(tmp_path / "pr-head"),
    }
    hidden = f"Grace{secrets.token_hex(4)}"
    plan = _plan(
        tmp_path,
        [
            _check("greets-ada", ["/bin/sh", "check.sh", "Ada"]),
            _check("greets-hidden", ["/bin/sh", "check.sh", hidden], held_out=True),
        ],
    )
    calls = tmp_path / "engineer.calls"
    stub = write_executable(
        tmp_path / "engineer.sh",
        f"#!/bin/sh\necho call >> '{calls}'\ncat > /dev/null\n{SPECIAL_CASED}\n"
        "echo '<promise>COMPLETE</promise>'\n",
    )
    manifest = str(root / "scripts" / "kstrl" / "manifest.json")
    code, out = _spawn(
        [
            "factory",
            *("--manifest", manifest, "--root", str(root), "--agent-cmd", str(stub)),
            *("--acceptance", str(plan), "--no-tui", "--yes", "--ui", "plain", "--no-color"),
            *("--max-retries", "1", "--max-parallel", "1"),
            *("--review-mode", "skip", "--security-mode", "skip"),
            *("--contract-check", "skip"),
        ],
        root,
        env,
    )

    assert code == 1, out
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1, out
    assert _status(root) == ("failed", "acceptance"), out
    assert not body.exists(), out
    assert _acceptance_events(root) == [(False, False)], out
    (item,) = _halts(root)
    assert item.evidence["check"] == "greets-hidden", item.evidence
    assert item.evidence["head_sha"] == _tip(root), item.evidence
    assert hidden not in item.detail + item.title, item.detail
    app = home_app(root)
    async with app.run_test(size=(120, 36)) as pilot:
        await mounted(pilot, lambda: app.screen, "#home-runs")
        app.push_screen(InboxScreen())
        screen = app.screen
        assert isinstance(screen, InboxScreen)
        detail = cast(Static, await mounted(pilot, lambda: screen, "#inbox-detail"))
        await settled(pilot, lambda: bool(screen._items), what="the inbox screen to read the log")
        row = [i.id for i in screen._items].index(item.id)
        screen.query_one("#inbox-table", DataTable).move_cursor(row=row)
        await settled(
            pilot,
            lambda: f"id {item.id[:8]}" in flat(detail) and "what each choice does" in flat(detail),
            what="the halt's choices",
        )
        offered = " ".join(flat(detail).split())
    assert "merges over the failing acceptance checks greets-hidden" in offered, offered
    assert "no kstrl step reads" not in offered, offered

    code, said = _spawn(
        ["inbox", "approve", item.id, "--root", str(root), "--ui", "plain", "--no-color"],
        root,
        env,
    )
    assert code == 0, said
    assert "merges over the failing acceptance checks greets-hidden" in said, said
    (approved,) = _halts(root)
    code, out = _spawn(
        ["retry", COMP, "--yes", "--root", str(root), "--ui", "plain", "--no-color"], root, env
    )

    assert code == 0, out
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1, out
    assert _status(root)[0] == "completed", out
    kept = (
        f"ks retry kept commit {item.evidence['head_sha'][:12]}, which inbox approval {item.id[:8]}"
    )
    assert kept in out, out
    merged = (
        f"- merged over the failing checks greets-hidden by inbox approval {item.id[:8]} "
        f"({approved.decided_by} at {approved.decided_at})"
    )
    assert merged in out, out
    assert merged in body.read_text(encoding="utf-8").splitlines(), body.read_text("utf-8")


@runs_a_stack
def test_an_approval_covers_only_the_checks_and_the_commit_it_names(tmp_path: Path) -> None:
    """An approved halt is no blanket pass. Its commit judged again with a
    second check now failing halts again; and once the branch is gone, the
    regenerated commit fails the approved held-out check alone, and halts
    again on a new item, because the approval named the other commit."""
    shared = tmp_path / "shared"
    shared.mkdir()
    flag = shared / "fail-now"
    # Fails on a head (the engineer writes stamp) while the test has set the flag.
    script = GREETS_OR_MARKER.replace(
        "#!/bin/sh\n",
        '#!/bin/sh\nif [ "$1" = "--flagged" ]; then\n'
        f'  if [ -f "$KSTRL_TREE/stamp" ] && [ -f "{flag}" ]; then echo flagged; exit 1; fi\n'
        "  exit 0\nfi\n",
    )
    root = _repo(tmp_path, _stack({"tests": "true"}, rung={"writable": [str(shared)]}))
    _with_greet(root)
    hidden = f"Grace{secrets.token_hex(4)}"
    plan = _plan(
        tmp_path,
        [
            _check("flagged", ["/bin/sh", "check.sh", "--flagged"], on_base="passes"),
            _check("greets-hidden", ["/bin/sh", "check.sh", hidden], held_out=True),
        ],
        script=script,
    )
    # Each call stamps its own pid, so a regenerated commit is a new commit.
    engineer = f"echo $$ > stamp && {SPECIAL_CASED}"
    first = _accept(tmp_path, root, plan, engineer)
    assert first.code == 1, first.out
    (item,) = _halts(root)
    head = _tip(root)
    approve = ["inbox", "approve", item.id, "--root", str(root), "--ui", "plain"]
    code, said = _spawn(approve, root, None)
    assert code == 0, said
    flag.write_text("", encoding="utf-8")
    retry = ["retry", COMP, "--yes", "--root", str(root), "--ui", "plain", "--no-color"]

    code, out = _spawn(retry, root, None)

    assert code == 1, out
    assert f"Kept branch '{BRANCH}' at {head[:12]}" in out, out
    assert "merged over the failing checks" not in out, out
    assert _status(root) == ("failed", "acceptance"), out

    git_in(root, "branch", "-D", BRANCH)
    flag.unlink()
    code, out = _spawn(retry, root, None)

    assert code == 1, out
    assert "merged over the failing checks" not in out, out
    assert _status(root) == ("failed", "acceptance"), out
    regenerated = _tip(root)
    assert regenerated != head, out
    reopened = [
        i for i in _halts(root) if i.evidence.get("head_sha") == regenerated and i.id != item.id
    ]
    assert [(i.status.value, i.evidence["check"]) for i in reopened] == [
        ("open", "greets-hidden")
    ], reopened


@runs_a_stack
def test_an_approval_of_a_halt_on_two_checks_merges_over_both(tmp_path: Path) -> None:
    """A halt on a failed held-out check and a failed visible check on one
    head names both, and approving it covers both: ``ks retry`` keeps the
    commit, judges it with no engineer and merges it, and the terminal names
    both checks and the approval. The engineer also disputes the visible
    check (#700 slice 10a): the halt still names the failed held-out check,
    so a dispute never hides it from the person who approves, and the PR
    body shows the dispute, its argv and its output, from the renderer the
    terminal uses."""
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
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GH_BODY": str(body),
        "GH_HEAD": str(tmp_path / "pr-head"),
    }
    hidden = f"Grace{secrets.token_hex(4)}"
    plan = _plan(
        tmp_path,
        [
            _check("has-marker", ["/bin/sh", "check.sh", "--marker"]),
            _check("greets-hidden", ["/bin/sh", "check.sh", hidden], held_out=True),
        ],
        script=GREETS_OR_MARKER,
    )
    cause = f"the criterion asks for no marker file {secrets.token_hex(4)}"
    dispute = _progress("## [2026-10-08] - US-001", f"Dispute: has-marker: {cause}")
    # --create-prs after the harness's --no-prs: the retry opens the PR.
    first = _factory(
        tmp_path,
        root,
        *("--acceptance", str(plan), "--create-prs"),
        env=env,
        engineer=f"{SPECIAL_CASED} && {dispute}",
    )
    assert (first.code, first.calls) == (1, 1), first.out
    (item,) = _halts(root)
    assert item.evidence["check"] == "has-marker, greets-hidden", item.evidence
    for shown in (
        "the held-out acceptance checks greets-hidden failed",
        f"- the engineer disputes has-marker: {cause}",
        "argv: /bin/sh check.sh --marker",
    ):
        assert shown in item.detail, item.detail
    assert hidden not in item.detail, item.detail
    code, said = _spawn(
        ["inbox", "approve", item.id, "--root", str(root), "--ui", "plain"], root, env
    )
    assert code == 0, said
    (approved,) = _halts(root)

    code, out = _spawn(
        ["retry", COMP, "--yes", "--root", str(root), "--ui", "plain", "--no-color"], root, env
    )

    assert code == 0, out
    assert len((tmp_path / "engineer.calls").read_text(encoding="utf-8").splitlines()) == 1, out
    assert _status(root) == ("completed", ""), out
    merged = (
        f"- merged over the failing checks has-marker, greets-hidden by inbox approval "
        f"{item.id[:8]} ({approved.decided_by} at {approved.decided_at})"
    )
    assert merged in out, out
    said_in_body = body.read_text(encoding="utf-8").splitlines()
    for shown in (
        merged,
        f"- the engineer disputes has-marker: {cause}",
        "  argv: /bin/sh check.sh --marker",
    ):
        assert shown in said_in_body, said_in_body
    assert hidden not in "\n".join(said_in_body), said_in_body


@runs_a_stack
def test_a_dispute_of_a_visible_check_halts_and_an_approval_merges_over_it(
    tmp_path: Path,
) -> None:
    """#700 slice 10a. Attempt 1 greets every name and writes no marker, so
    the visible marker check fails and the retry shows its command and its
    output. Attempt 2 disputes that check in its progress entry and changes
    no code: the component halts with no further retry, although two were
    allowed, and the halt shows the check's argv, its output and the
    engineer's cause. The dispute passes nothing: an approval of that halt
    and ``ks retry`` merge over it through the decision 14 path, with no
    engineer."""
    root = _greeting_repo(tmp_path)
    plan = _plan(
        tmp_path,
        [_check("has-marker", ["/bin/sh", "check.sh", "--marker"])],
        script=GREETS_OR_MARKER,
    )
    cause = f"the criterion asks for no marker file {secrets.token_hex(4)}"
    dispute = _progress("## [2026-10-08] - US-001", f"Dispute: has-marker: {cause}")
    engineer = _counted(tmp_path / "count", CORRECT, dispute)

    run = _accept(tmp_path, root, plan, engineer, "--max-retries", "2")

    assert run.code == 1, run.out
    assert run.calls == 2, run.out
    assert _status(root) == ("failed", "acceptance"), run.out
    assert DISPUTE_FORM in run.prompts, run.prompts
    (item,) = _halts(root)
    assert item.evidence["check"] == "has-marker", item.evidence
    assert item.evidence["head_sha"] == _tip(root), item.evidence
    for shown in (
        "the engineer disputes the acceptance checks has-marker",
        "halted with no retry",
        "argv: /bin/sh check.sh --marker",
        "| no made.marker in the tree",
        cause,
    ):
        assert shown in item.detail, item.detail
    # Attempt 1 wrote no progress log: that is no dispute, so nothing is
    # rejected. An operator wrote this plan, so no line says a model did.
    assert "dispute rejected" not in run.out, run.out
    assert "can be incorrect" not in item.detail, item.detail
    code, said = _spawn(
        ["inbox", "approve", item.id, "--root", str(root), "--ui", "plain"], root, None
    )
    assert code == 0, said
    (approved,) = _halts(root)

    code, out = _spawn(
        ["retry", COMP, "--yes", "--root", str(root), "--ui", "plain", "--no-color"], root, None
    )

    assert code == 0, out
    assert len((tmp_path / "engineer.calls").read_text(encoding="utf-8").splitlines()) == 2, out
    assert _status(root) == ("completed", ""), out
    assert f"- the engineer disputes has-marker: {cause}" in out, out
    assert (
        f"- merged over the failing checks has-marker by inbox approval {item.id[:8]} "
        f"({approved.decided_by} at {approved.decided_at})"
    ) in out, out


@runs_a_stack
def test_a_dispute_kstrl_cannot_take_is_rejected_and_the_check_result_stands(
    tmp_path: Path,
) -> None:
    """#700 slice 10a. Attempt 1 greets every name and writes no marker.
    Each later attempt writes a progress log whose latest entry holds five
    disputes kstrl cannot take: two not in the form (one of them a list
    item in lower case, which is read and not passed over), one of a held-out
    check, which no engineer can see, one of a visible check that passed,
    and one of no check at all. An older entry holds a well-formed dispute,
    which is not this iteration's. None of them halts: the marker check's
    result stands, each retry is told why each line was rejected, and the
    run fails once the retries are spent."""
    root = _greeting_repo(tmp_path)
    hidden = f"Grace{secrets.token_hex(4)}"
    plan = _plan(
        tmp_path,
        [
            _check("has-marker", ["/bin/sh", "check.sh", "--marker"]),
            _check("greets-ada", ["/bin/sh", "check.sh", "Ada"]),
            _check("greets-hidden", ["/bin/sh", "check.sh", hidden], held_out=True),
        ],
        script=GREETS_OR_MARKER,
    )
    disputes = _progress(
        "## [2026-10-07] - US-001",
        "Dispute: has-marker: an older entry says so",
        "## [2026-10-08] - US-001",
        "Dispute has-marker as it is incorrect",
        "- dispute: has-marker: in lower case",
        "Dispute: greets-hidden: it cannot be met",
        "Dispute: greets-ada: it is too strict",
        "Dispute: no-such-check: it is not there",
    )
    engineer = _counted(tmp_path / "count", CORRECT, disputes)

    run = _accept(tmp_path, root, plan, engineer, "--max-retries", "2")

    assert run.code == 1, run.out
    assert run.calls == 3, run.out
    assert _status(root) == ("failed", "acceptance"), run.out
    for told in (
        f"'Dispute has-marker as it is incorrect' is not in the form {DISPUTE_FORM}",
        f"'- dispute: has-marker: in lower case' is not in the form {DISPUTE_FORM}",
        "greets-hidden is held out",
        "greets-ada passed on this head, so there is nothing to dispute",
        "no-such-check is not an acceptance check of this component",
    ):
        assert told in run.prompts, run.prompts
    assert hidden not in run.prompts, run.prompts
    assert "the engineer disputes" not in run.out, run.out
    (item,) = _halts(root)
    assert item.evidence["check"] == "has-marker", item.evidence
    assert "the engineer disputes" not in item.detail, item.detail


@runs_a_stack
def test_a_held_out_check_leaves_nothing_in_the_repository_and_recheck_finds_it(
    tmp_path: Path,
) -> None:
    """The engineer retries inside the repository, so a held-out check's
    argv must not be in any file there (#700 slice 6, owner instruction
    2026-10-06). After a gated run that halts on the held-out check, no
    file of the repository outside .git holds its random argv, and
    `ks recheck` of the record kept under the control directory agrees
    when given the absolute path the factory printed, and refuses (exit
    2), naming the path, one relative to that directory: a RECORD is
    read from the current directory only."""
    root = _greeting_repo(tmp_path)
    hidden = f"Grace{secrets.token_hex(4)}"
    plan = _plan(
        tmp_path,
        [
            _check("greets-ada", ["/bin/sh", "check.sh", "Ada"]),
            _check("greets-hidden", ["/bin/sh", "check.sh", hidden], held_out=True),
        ],
    )

    run = _accept(tmp_path, root, plan, SPECIAL_CASED)

    assert run.code == 1, run.out
    holders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if ".git" not in path.relative_to(root).parts
        and path.is_file()
        and hidden.encode() in path.read_bytes()
    ]
    assert holders == [], holders
    (record,) = sorted(_evidence_root(root).glob(f"*/acceptance/{COMP}/attempt-*/record.json"))
    assert hidden in (record.parent / "checks" / "plan.json").read_text(encoding="utf-8")
    (printed,) = [
        line.removeprefix("- record: ") for line in run.out.splitlines() if "- record: " in line
    ]
    assert Path(printed.strip()).resolve() == record.resolve(), run.out
    code, out = _recheck(root, Path(printed.strip()))
    assert code == 0, out
    assert "The recheck agrees with the record." in out, out
    relative = record.relative_to(_evidence_root(root))
    code, out = _recheck(root, relative)
    assert code == 2, out
    assert str(relative) in out, out
    # Read from the current directory, never from --root: run from the
    # record's own directory, the bare file name agrees.
    argv = ["recheck", record.name, "--root", str(root), "--ui", "plain", "--no-color"]
    code, out = _spawn(argv, record.parent, None)
    assert code == 0, out
    assert "The recheck agrees with the record." in out, out
