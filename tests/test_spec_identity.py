"""A plan names the exact spec text it was made from, and nothing runs on another (#639).

The defect, in three parts, each reproduced through the real CLI with a
stub architect. The manifest and the register recorded only the spec's
basename, so a spec edited after its decompose reached the L1 plan gate
with every preflight passed, and ``ks inbox approve`` ran it. An
escalation item was keyed on the project and that basename and resolved
on any later decompose that escalated nothing, so re-running the same
bytes closed the owner's question, and so did a decompose of a different
spec with the same file name.

The fix: decompose pins the sha256 of the text the architect read
(``load_spec_input``) and the spec's root-relative path on the manifest,
the register, the escalation item and the plan gate item. ``ks factory``
re-reads the spec before any spend and refuses a plan whose spec moved,
went missing or cannot be read (exit 2). ``ks inbox approve`` of a plan
park refuses the same way before it records anything. An escalation is
keyed on the spec's path and resolves only when the text changed.

End to end: ``python -m kstrl`` in a subprocess, real git with a bare
origin, the stub ``gh`` and logging engineer of
``tests/test_merge_gate_park.py``, a stub architect that prints a fixed
payload, and the inbox under the per-test ``XDG_STATE_HOME``.

Disclosed blind spot: computing the pin from a second read of the spec
file instead of the ``spec_content`` the architect was given survives
every test here, because nothing changes the file between the two reads.

Slice 4 (owner answers, decision 8a): an answer given with ``ks inbox
approve <id> --comment ANSWER`` on a spec's escalation reaches the next
decompose of that spec, appended after the spec under
``OWNER_ANSWER_PROMPT`` and after the pin, so the plan still runs on the
spec as written. Another project's or spec's answer is never read, and an
inbox that cannot be read refuses the decompose before the architect runs.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kstrl.decompose import load_spec_input
from kstrl.inbox import Inbox, InboxItem, ItemKind, ItemStatus
from kstrl.init_cmd import gitignore_block
from kstrl.manifest import Manifest
from kstrl.owner_answers import render_owner_answer
from tests.helpers import gitrepo
from tests.helpers.plan_approval import approve_plan
from tests.test_build_manifest_preflight import MANIFESTS
from tests.test_escalation_inbox import CLOSED, ESCALATED
from tests.test_l1_plan_gate import AUTONOMY, REVIEWER, _plan_asks, _plan_items, _runs
from tests.test_merge_gate_park import (
    FACTORY_FLAGS,
    _engineer_ran,
    _env,
    _ks,
    _manifest_path,
)
from tests.test_merge_gate_park import _repo as _handmade_repo

pytestmark = pytest.mark.usefixtures("no_open_prs")

SPEC = "# Spec\n\nUsers sign in with a password.\n"
EDITED = "# Spec\n\nUsers sign in with SSO only. Passwords are forbidden.\n"
#: The component the CLOSED payload plans; the engineer log records it.
LOGIN = "login"
#: The refusal headline ``ks factory`` prints for a spec that moved.
STALE = "Refusing to run: the plan does not match the spec it was made from"
#: The words the legacy (unpinned) warning is recognised by.
UNPINNED = "before kstrl pinned specs"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _project(tmp_path: Path, *, toml: str = "", files: dict[str, str] | None = None) -> Path:
    """A committed repository holding ``spec.md`` (and ``files``), with a bare origin."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    tree = {"pyproject.toml": MANIFESTS["pyproject.toml"], "spec.md": SPEC, **(files or {})}
    for rel, body in tree.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (root / ".gitignore").write_text(gitignore_block("Python"), encoding="utf-8")
    (root / "kstrl.toml").write_text("[inbox]\nenabled = true\n" + toml, encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    origin = tmp_path / "origin.git"
    gitrepo.git_in(tmp_path, "init", "-q", "--bare", str(origin))
    gitrepo.git_in(root, "remote", "add", "origin", str(origin))
    gitrepo.git_in(root, "push", "-q", "-u", "origin", "main")
    return root


def _decompose(
    root: Path,
    env: dict[str, str],
    payload: dict[str, Any],
    *,
    spec: str = "spec.md",
    project: str = "p",
    tee: Path | None = None,
    expect: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """``ks decompose`` with a stub architect that prints ``payload``.

    ``tee`` is where the stub writes the prompt it was sent; the file does
    not exist when the architect never ran.
    """
    architect = root.parent / "architect.json"
    architect.write_text(json.dumps(payload), encoding="utf-8")
    sink = "/dev/null" if tee is None else f"'{tee}'"
    proc = _ks(
        root,
        env,
        "decompose",
        "--spec",
        str(root / spec),
        "--project-name",
        project,
        "--agent-cmd",
        f"cat > {sink}; cat '{architect}'",
        "--ui",
        "plain",
        "--no-color",
    )
    expected = (2 if payload is ESCALATED else 0) if expect is None else expect
    assert proc.returncode == expected, proc.stdout + proc.stderr
    return proc


def _factory(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _ks(
        root, env, "factory", "--manifest", str(_manifest_path(root)), *FACTORY_FLAGS, *REVIEWER
    )


def _planned(tmp_path: Path, *, toml: str = AUTONOMY, **kw: Any) -> tuple[Path, dict[str, str]]:
    """A repository whose ``spec.md`` (or ``kw['spec']``) was decomposed into one component."""
    files = kw.pop("files", None)
    root = _project(tmp_path, toml=toml, files=files)
    env = _env(tmp_path)
    _decompose(root, env, CLOSED, **kw)
    return root, env


def _raw_manifest(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
    return data


def _register(root: Path) -> dict[str, Any]:
    path = root / "scripts" / "kstrl" / "decisions.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _escalations(root: Path) -> list[InboxItem]:
    return [i for i in Inbox(root).items() if i.kind is ItemKind.SPEC_ESCALATION]


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


# --- 1. the pin is written ----------------------------------------------------


class TestThePinIsWritten:
    def test_a_file_spec_is_pinned_by_the_sha256_of_its_bytes(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        env = _env(tmp_path)

        proc = _decompose(root, env, CLOSED)

        digest = hashlib.sha256((root / "spec.md").read_bytes()).hexdigest()
        manifest = _raw_manifest(root)
        assert (manifest["specPath"], manifest["specDigest"]) == ("spec.md", digest)
        assert _register(root)["specDigest"] == digest
        assert "Spec digest" in _out(proc) and digest[:12] in _out(proc), _out(proc)

    def test_a_speckit_directory_is_pinned_by_the_text_the_architect_read(
        self, tmp_path: Path
    ) -> None:
        files = {"specs/feat/spec.md": SPEC, "specs/feat/plan.md": "# Plan\n\nUse Flask.\n"}
        root, _env_ = _planned(tmp_path, toml="", files=files, spec="specs/feat")

        manifest = _raw_manifest(root)
        assert manifest["specPath"] == "specs/feat"
        assert manifest["specDigest"] == _sha(load_spec_input(root / "specs" / "feat"))


# --- 2 to 5. ks factory refuses a plan whose spec moved -----------------------


class TestAStaleSpecIsRefused:
    @pytest.mark.parametrize("edited", [True, False], ids=["edited", "unedited-control"])
    def test_an_edited_spec_is_refused_before_the_plan_gate(
        self, tmp_path: Path, edited: bool
    ) -> None:
        root, env = _planned(tmp_path)
        pinned = _raw_manifest(root)["specDigest"]
        if edited:
            (root / "spec.md").write_text(EDITED, encoding="utf-8")

        proc = _factory(root, env)
        out = _out(proc)

        assert _engineer_ran(tmp_path) == [], out
        if not edited:
            # The control: the same plan passes every preflight and parks at L1.
            assert proc.returncode == 1, out
            assert [str(i.status) for i in _plan_items(root)] == ["open"], out
            return
        assert proc.returncode == 2, out
        assert STALE in out, out
        assert pinned[:12] in out and _sha(EDITED)[:12] in out, out
        assert _plan_items(root) == [], out

    def test_restoring_the_spec_lets_the_plan_run(self, tmp_path: Path) -> None:
        root, env = _planned(tmp_path)
        (root / "spec.md").write_text(EDITED, encoding="utf-8")
        assert _factory(root, env).returncode == 2
        gitrepo.git_in(root, "checkout", "--", "spec.md")
        approve_plan(root, Manifest.load(_manifest_path(root)))

        proc = _factory(root, env)

        assert _engineer_ran(tmp_path) == [LOGIN], _out(proc)

    @pytest.mark.parametrize("edited", [True, False], ids=["edited", "unedited-control"])
    def test_an_edit_to_speckit_plan_md_alone_is_refused(
        self, tmp_path: Path, edited: bool
    ) -> None:
        files = {"specs/feat/spec.md": SPEC, "specs/feat/plan.md": "# Plan\n\nUse Flask.\n"}
        root, env = _planned(tmp_path, files=files, spec="specs/feat")
        if edited:
            (root / "specs" / "feat" / "plan.md").write_text(
                "# Plan\n\nUse Django.\n", encoding="utf-8"
            )

        proc = _factory(root, env)
        out = _out(proc)

        assert _engineer_ran(tmp_path) == [], out
        if not edited:
            # The control: an unedited SpecKit directory is re-read the way
            # decompose read it, so the plan passes every preflight and parks.
            assert proc.returncode == 1, out
            assert [str(i.status) for i in _plan_items(root)] == ["open"], out
            return
        assert proc.returncode == 2, out
        assert STALE in out, out

    def test_a_deleted_spec_is_refused_and_named(self, tmp_path: Path) -> None:
        root, env = _planned(tmp_path)
        (root / "spec.md").unlink()

        proc = _factory(root, env)
        out = _out(proc)

        assert proc.returncode == 2, out
        assert STALE in out, out
        assert "cannot be read: spec.md" in out, out
        assert _engineer_ran(tmp_path) == []

    @pytest.mark.parametrize("edited", [False, True], ids=["moved", "moved-and-edited"])
    def test_a_queue_spec_is_followed_to_the_state_its_item_moved_to(
        self, tmp_path: Path, edited: bool
    ) -> None:
        """``ks serve`` plans from ``.kstrl/queue/running/<id>/spec.md`` and then
        renames the item directory into another state (awaiting_approval when
        the plan parks), so the pinned path is gone while the same file is not."""
        running = ".kstrl/queue/running/q1/spec.md"
        root, env = _planned(tmp_path, files={running: SPEC}, spec=running)
        assert _raw_manifest(root)["specPath"] == running
        waiting = root / ".kstrl" / "queue" / "awaiting_approval" / "q1"
        waiting.parent.mkdir(parents=True)
        (root / running).parent.replace(waiting)
        if edited:
            (waiting / "spec.md").write_text(EDITED, encoding="utf-8")

        proc = _factory(root, env)
        out = _out(proc)

        assert _engineer_ran(tmp_path) == [], out
        if edited:
            assert proc.returncode == 2, out
            assert STALE in out and _sha(EDITED)[:12] in out, out
            return
        # Passes every preflight and parks at L1, as before the pin.
        assert proc.returncode == 1, out
        assert [str(i.status) for i in _plan_items(root)] == ["open"], out


# --- 6. a blanked or malformed pin is not a legacy manifest -------------------


@pytest.mark.parametrize(
    "value",
    ["abc", "", 5, "0" * 65, "A" * 64],
    ids=["short", "empty", "number", "too-long", "uppercase"],
)
def test_a_malformed_spec_digest_is_refused_by_the_schema(tmp_path: Path, value: object) -> None:
    root, env = _planned(tmp_path)
    data = _raw_manifest(root)
    data["specDigest"] = value
    _manifest_path(root).write_text(json.dumps(data), encoding="utf-8")

    proc = _factory(root, env)
    out = _out(proc)

    assert proc.returncode == 2, out
    assert "Failed to load manifest" in out and "specDigest" in out, out
    assert _engineer_ran(tmp_path) == []


# --- 7. the plan gate names the spec, and approve refuses a stale one ---------


def test_the_plan_park_names_its_spec_and_approve_refuses_a_stale_one(tmp_path: Path) -> None:
    root, env = _planned(tmp_path)
    pinned = _raw_manifest(root)["specDigest"]
    parked = _factory(root, env)
    assert parked.returncode == 1, _out(parked)
    (item,) = _plan_items(root)
    (asked,) = _plan_asks(_runs(root)[-1])
    assert f"made from spec.md ({pinned[:12]})" in asked, asked
    assert (item.evidence["spec_path"], item.evidence["spec_digest"]) == ("spec.md", pinned)

    (root / "spec.md").write_text(EDITED, encoding="utf-8")
    approved = _ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")

    assert approved.returncode == 2, _out(approved)
    assert "nothing was approved" in _out(approved), _out(approved)
    assert [str(i.status) for i in _plan_items(root)] == ["open"]
    assert _engineer_ran(tmp_path) == []

    rejected = _ks(root, env, "inbox", "reject", item.id, "--comment", "x", "--ui", "plain")

    assert [str(i.status) for i in _plan_items(root)] == ["rejected"], _out(rejected)
    assert _engineer_ran(tmp_path) == []


# --- 8. the register binds by digest ------------------------------------------


def test_a_register_for_other_spec_text_is_refused(tmp_path: Path) -> None:
    root, env = _planned(tmp_path)
    path = root / "scripts" / "kstrl" / "decisions.json"
    register = _register(root)
    register["specDigest"] = "0" * 64
    path.write_text(json.dumps(register), encoding="utf-8")

    proc = _factory(root, env)
    out = _out(proc)

    assert proc.returncode == 2, out
    assert "Refusing to run: the architect decision register cannot bind" in out, out
    assert _plan_items(root) == [], out
    assert _engineer_ran(tmp_path) == []


# --- 9 and 10. an escalation resolves only when its own spec changed ----------


class TestAnEscalationClosesOnlyOnNewText:
    def test_the_same_text_leaves_the_item_open_and_says_why(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        env = _env(tmp_path)
        _decompose(root, env, ESCALATED)

        proc = _decompose(root, env, CLOSED)

        (item,) = _escalations(root)
        assert item.status is ItemStatus.OPEN, item
        assert "has not changed since it was escalated" in _out(proc), _out(proc)
        assert f"ks inbox approve {item.id[:8]} --comment ANSWER" in _out(proc), _out(proc)

    def test_an_edited_spec_resolves_it_naming_both_digests(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        env = _env(tmp_path)
        _decompose(root, env, ESCALATED)
        (root / "spec.md").write_text(EDITED, encoding="utf-8")

        _decompose(root, env, CLOSED)

        (item,) = _escalations(root)
        assert item.status is ItemStatus.RESOLVED, item
        assert f"({_sha(SPEC)[:12]} -> {_sha(EDITED)[:12]})" in item.decision_comment, (
            item.decision_comment
        )

    def test_another_spec_with_the_same_name_leaves_it_open(self, tmp_path: Path) -> None:
        files = {"a/spec.md": "# A\n\nPayments.\n", "b/spec.md": "# B\n\nA report.\n"}
        root = _project(tmp_path, files=files)
        env = _env(tmp_path)
        _decompose(root, env, ESCALATED, spec="a/spec.md")

        _decompose(root, env, CLOSED, spec="b/spec.md")

        (item,) = _escalations(root)
        assert item.status is ItemStatus.OPEN, item
        assert item.evidence["spec_source"] == "a/spec.md", item.evidence

    def test_an_item_from_before_the_pin_is_never_closed_by_a_decompose(
        self, tmp_path: Path
    ) -> None:
        """An item opened before #639 records no digest, so kstrl cannot tell
        whether the spec changed since: it stays open for the owner, even when
        the spec's text did change."""
        root = _project(tmp_path)
        env = _env(tmp_path)
        _decompose(root, env, ESCALATED)
        (item,) = _escalations(root)
        # The evidence a pre-#639 escalation carried: a basename, no digest.
        Inbox(root).add(
            ItemKind.SPEC_ESCALATION,
            item.title,
            dedupe_key=item.dedupe_key,
            evidence={"project": "p", "spec_file": "spec.md", "questions": [], "register": ""},
        )
        (root / "spec.md").write_text(EDITED, encoding="utf-8")

        proc = _decompose(root, env, CLOSED)

        (item,) = _escalations(root)
        assert item.status is ItemStatus.OPEN, item
        assert "opened before kstrl recorded the spec's digest" in _out(proc), _out(proc)


# --- 11 and 12. controls: no spec, and a manifest from before the pin ---------


class TestManifestsWithoutAPin:
    def test_a_manifest_with_no_spec_is_not_checked(self, tmp_path: Path) -> None:
        root = _handmade_repo(tmp_path)
        data = _raw_manifest(root)
        data["specFile"] = ""
        _manifest_path(root).write_text(json.dumps(data), encoding="utf-8")

        proc = _factory(root, _env(tmp_path))
        out = _out(proc)

        assert _engineer_ran(tmp_path)[:1] == ["http"], out
        assert UNPINNED not in out and STALE not in out, out

    def test_a_manifest_from_before_the_pin_warns_and_runs(self, tmp_path: Path) -> None:
        root = _handmade_repo(tmp_path)
        assert "specDigest" not in _raw_manifest(root)

        proc = _factory(root, _env(tmp_path))
        out = _out(proc)

        assert _engineer_ran(tmp_path)[:1] == ["http"], out
        assert UNPINNED in out, out


# --- slice 4: the owner's inbox answer reaches the next decompose ------------

ANSWER = "Users sign in with passwords; SSO is out of scope for this release."


def _answer_in_inbox(root: Path, env: dict[str, str]) -> InboxItem:
    """``ks inbox approve <id> --comment ANSWER`` on the one open escalation."""
    (item,) = [i for i in _escalations(root) if i.status is ItemStatus.OPEN]
    proc = _ks(root, env, "inbox", "approve", item.id, "--comment", ANSWER, "--ui", "plain")
    assert proc.returncode == 0, _out(proc)
    (approved,) = [i for i in _escalations(root) if i.id == item.id]
    return approved


def test_an_inbox_answer_reaches_the_next_decompose_after_the_pin(tmp_path: Path) -> None:
    root = _project(tmp_path, toml=AUTONOMY)
    env = _env(tmp_path)
    _decompose(root, env, ESCALATED)
    (asked,) = _escalations(root)
    assert "ks inbox approve" in asked.detail and "--comment" in asked.detail, asked.detail
    item = _answer_in_inbox(root, env)
    tee = tmp_path / "prompt.txt"

    proc = _decompose(root, env, CLOSED, tee=tee)

    assert f"Owner answers: {item.id[:8]}" in _out(proc), _out(proc)
    prompt = tee.read_text(encoding="utf-8")
    block = render_owner_answer(item)
    assert block in prompt, prompt
    assert ANSWER in block and "auth-model" in block, block
    end = prompt.index(":END SPECIFICATION>>>")
    assert prompt.index(SPEC.strip()) < prompt.index(block) < end, prompt
    register = _register(root)
    assert register["answeredItems"] == [item.id], register
    assert register["answersDigest"] == _sha("\n\n" + block), register
    assert _raw_manifest(root)["specDigest"] == _sha(SPEC), _out(proc)
    parked = _factory(root, env)
    assert STALE not in _out(parked), _out(parked)
    assert len(_plan_items(root)) == 1, _out(parked)


def test_an_answer_for_another_project_or_spec_is_never_read(tmp_path: Path) -> None:
    files = {"a/spec.md": "# A\n\nPayments.\n", "b/spec.md": "# B\n\nA report.\n"}
    root = _project(tmp_path, files=files)
    env = _env(tmp_path)
    _decompose(root, env, ESCALATED, spec="a/spec.md")
    item = _answer_in_inbox(root, env)
    tee = tmp_path / "prompt.txt"

    for spec, project in (("b/spec.md", "p"), ("a/spec.md", "q")):
        _decompose(root, env, CLOSED, spec=spec, project=project, tee=tee)
        assert ANSWER not in tee.read_text(encoding="utf-8"), (spec, project)
        assert _register(root)["answeredItems"] == [], (spec, project)

    _decompose(root, env, CLOSED, spec="a/spec.md", tee=tee)
    assert ANSWER in tee.read_text(encoding="utf-8")
    assert _register(root)["answeredItems"] == [item.id]


def test_every_answer_to_a_spec_is_read_after_it_escalates_again(tmp_path: Path) -> None:
    root = _project(tmp_path)
    env = _env(tmp_path)
    _decompose(root, env, ESCALATED)
    first = _answer_in_inbox(root, env)

    _decompose(root, env, ESCALATED)

    assert _register(root)["answeredItems"] == [first.id], _register(root)
    second = _answer_in_inbox(root, env)
    assert second.id != first.id
    tee = tmp_path / "prompt.txt"
    _decompose(root, env, CLOSED, tee=tee)
    prompt = tee.read_text(encoding="utf-8")
    assert render_owner_answer(first) in prompt and render_owner_answer(second) in prompt
    assert _register(root)["answeredItems"] == [first.id, second.id], _register(root)


@pytest.mark.parametrize(
    "decision",
    [("reject", "--comment", ANSWER), ("approve",)],
    ids=["rejected-with-a-comment", "approved-with-no-comment"],
)
def test_a_rejection_or_a_bare_approval_is_not_an_answer(
    tmp_path: Path, decision: tuple[str, ...]
) -> None:
    root = _project(tmp_path)
    env = _env(tmp_path)
    _decompose(root, env, ESCALATED)
    (item,) = _escalations(root)
    decided = _ks(root, env, "inbox", decision[0], item.id, *decision[1:], "--ui", "plain")
    assert decided.returncode == 0, _out(decided)
    tee = tmp_path / "prompt.txt"

    proc = _decompose(root, env, CLOSED, tee=tee)

    prompt = tee.read_text(encoding="utf-8")
    assert item.id not in prompt and ANSWER not in prompt, prompt
    assert "Owner answers" not in _out(proc), _out(proc)
    register = _register(root)
    assert register["answeredItems"] == [] and register["answersDigest"] == "", register


@pytest.mark.parametrize(
    "line",
    [
        b"\xff\xfe not utf-8\n",
        b"{torn\n",
        json.dumps(
            {"id": "f" * 32, "kind": "spec_escalation", "status": "Approved", "evidence": {}}
        ).encode()
        + b"\n",
    ],
    ids=["not-utf-8", "not-json", "unreadable-escalation"],
)
def test_an_inbox_that_cannot_be_read_refuses_before_the_architect(
    tmp_path: Path, line: bytes
) -> None:
    root = _project(tmp_path)
    env = _env(tmp_path)
    inbox = Inbox(root).path
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_bytes(line)
    tee = tmp_path / "prompt.txt"

    proc = _decompose(root, env, CLOSED, tee=tee, expect=2)

    assert not tee.exists(), _out(proc)
    assert str(inbox) in _out(proc) and "Nothing was run" in _out(proc), _out(proc)
    assert not _manifest_path(root).exists(), _out(proc)


def test_ks_factory_spec_refuses_an_unreadable_inbox_before_the_architect(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    env = _env(tmp_path)
    inbox = Inbox(root).path
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_bytes(b"{torn\n")
    architect = root.parent / "architect.json"
    architect.write_text(json.dumps(CLOSED), encoding="utf-8")
    tee = tmp_path / "prompt.txt"
    agent = f"cat > '{tee}'; cat '{architect}'"

    proc = _ks(
        root,
        env,
        "factory",
        "--spec",
        str(root / "spec.md"),
        "--project-name",
        "p",
        "--agent-cmd",
        agent,
        *FACTORY_FLAGS,
    )

    assert proc.returncode == 2, _out(proc)
    assert not tee.exists(), _out(proc)
    assert str(inbox) in _out(proc) and "Nothing was run" in _out(proc), _out(proc)
    assert not _manifest_path(root).exists(), _out(proc)
