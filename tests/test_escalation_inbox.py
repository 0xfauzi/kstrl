"""An architect escalation that halts decompose opens an inbox item (#449).

Before #449 the halt path in ``kstrl/decompose.py`` never referenced the
inbox, so the one halt that is by definition waiting on the owner was the
one that did not reach the surface the owner reads. These tests drive
the real CLI as subprocesses (``python -m kstrl``) with a stub architect
that prints a fixed payload, and assert on what ``ks inbox`` shows.

The census at the bottom counts every place ``kstrl/`` constructs a
``SpecBlockerError`` and fails one that does not open an item first.
What it does NOT see, stated rather than left implicit: a halt that
stops decompose with some other exception type. The census is over the
exception the CLI maps to exit code 2, not over "halts" in general.
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path
from typing import Any

import pytest

from kstrl.decompose import SpecBlockerError, decompose_spec
from kstrl.inbox import Inbox, InboxItem, ItemKind, ItemStatus
from kstrl.manifest import Manifest
from kstrl.statedir import ControlStateError
from kstrl.ui.plain import PlainUI
from tests.helpers import astwalk
from tests.helpers.prompt_calls import architect_call
from tests.test_build_manifest_preflight import MANIFESTS, greenfield, run_ks
from tests.test_decompose import MockDecomposeAgent

QUESTION = "should users sign in with SSO or passwords"

ESCALATED: dict[str, Any] = {
    "spec_issues": [
        {
            "id": "auth-model",
            "severity": "blocker",
            "kind": "ambiguity",
            "summary": "Auth model unspecified",
            "location": "spec.md:1",
            "suggestion": "Pick one",
        }
    ],
    "decisions": [
        {
            "issue": "auth-model",
            "question": QUESTION,
            "disposition": "escalated",
            "resolution": "the owner must choose the auth model",
        }
    ],
    "components": [],
}

CLOSED: dict[str, Any] = {
    "components": [
        {
            "id": "login",
            "title": "Login page",
            "description": "Password login",
            "dependencies": [],
            "allowedPaths": ["src/", "tests/", "scripts/kstrl/feature/login/"],
            "userStories": [
                {
                    "id": "US-001",
                    "title": "Sign in",
                    "acceptanceCriteria": ["User can sign in", "Tests pass"],
                    "priority": 1,
                    "passes": False,
                    "notes": "",
                }
            ],
        }
    ],
    "spec_issues": [
        {
            "id": "auth-model",
            "severity": "major",
            "kind": "ambiguity",
            "summary": "Auth model unspecified",
            "location": "spec.md:1",
            "suggestion": "Passwords",
        }
    ],
    "decisions": [
        {
            "issue": "auth-model",
            "question": QUESTION,
            "disposition": "decided",
            "resolution": "passwords, as the spec now says",
            "reason": "the owner answered in the spec",
            "alternative": "SSO",
        }
    ],
}


def _project(tmp_path: Path) -> Path:
    """A committed repository with a build manifest and two specs."""
    return greenfield(
        tmp_path,
        extra={
            "pyproject.toml": MANIFESTS["pyproject.toml"],
            "other.md": "# Other spec\n\nBuild a report.\n",
        },
    )


def _decompose(root: Path, payload: dict[str, Any], *, spec: str = "spec.md") -> str:
    """Run ``ks decompose`` with a stub architect; return the run id it made.

    The run id is the one new directory under ``.kstrl/runs``: the CLI
    mints it and prints it nowhere, so the directory is the observable.
    Returns the combined output instead when no run directory appeared,
    so a failing assertion shows why.
    """
    runs = root / ".kstrl" / "runs"
    before = {p.name for p in runs.glob("*")} if runs.exists() else set()
    architect = root.parent / "architect.json"
    architect.write_text(json.dumps(payload), encoding="utf-8")
    proc = run_ks(
        root,
        "decompose",
        "--spec",
        str(root / spec),
        "--project-name",
        "demo",
        "--root",
        str(root),
        "--agent-cmd",
        f"cat > /dev/null; cat '{architect}'",
        "--ui",
        "plain",
        "--no-color",
    )
    expected = 2 if payload is ESCALATED else 0
    assert proc.returncode == expected, proc.stdout
    made = {p.name for p in runs.glob("*")} - before
    assert len(made) == 1, (made, proc.stdout)
    return made.pop()


def _inbox_ls(root: Path, *extra: str) -> str:
    proc = run_ks(root, "inbox", "ls", "--root", str(root), "--ui", "plain", "--no-color", *extra)
    assert proc.returncode == 0, proc.stdout
    return proc.stdout


def _escalations(root: Path) -> list[InboxItem]:
    return [i for i in Inbox(root).items() if i.kind is ItemKind.SPEC_ESCALATION]


class TestAnEscalationReachesTheInbox:
    def test_a_halt_lists_one_item_naming_the_question(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        run_id = _decompose(root, ESCALATED)

        listed = _inbox_ls(root)
        rows = [line for line in listed.splitlines() if "Architect escalated" in line]
        assert len(rows) == 1, listed
        assert "spec_escalation" in rows[0], listed
        assert "auth-model" in rows[0], listed
        assert "spec.md" in rows[0], listed

        (item,) = _escalations(root)
        shown = run_ks(root, "inbox", "show", item.id, "--root", str(root), "--ui", "plain")
        assert shown.returncode == 0, shown.stdout
        assert QUESTION in shown.stdout
        assert "scripts/kstrl/decisions.json" in shown.stdout
        assert item.run_id == run_id
        assert item.evidence["questions"] == ["auth-model"]

    def test_a_repeat_halt_bumps_the_same_item(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _decompose(root, ESCALATED)
        _decompose(root, ESCALATED)

        items = _escalations(root)
        assert len(items) == 1, items
        assert items[0].occurrences == 2
        assert "x2" in _inbox_ls(root)

    def test_a_disabled_inbox_records_nothing_and_still_halts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path)
        monkeypatch.setenv("KSTRL_INBOX_ENABLED", "0")
        _decompose(root, ESCALATED)
        assert _escalations(root) == []


class TestALaterDecomposeClosesIt:
    def test_the_item_is_resolved_with_the_closing_run_id(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _decompose(root, ESCALATED)
        closing_run = _decompose(root, CLOSED)

        assert "Inbox clear" in _inbox_ls(root)
        (item,) = _escalations(root)
        assert item.status is ItemStatus.RESOLVED
        assert closing_run in item.decision_comment, item.decision_comment
        assert "spec_escalation" in _inbox_ls(root, "--all")

    def test_a_decompose_of_another_spec_leaves_it_open(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _decompose(root, ESCALATED)
        _decompose(root, CLOSED, spec="other.md")

        (item,) = _escalations(root)
        assert item.status is ItemStatus.OPEN, item
        assert "spec_escalation" in _inbox_ls(root)

    def test_an_operator_decision_is_kept(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _decompose(root, ESCALATED)
        (item,) = _escalations(root)
        rejected = run_ks(
            root,
            "inbox",
            "reject",
            item.id,
            "--comment",
            "the spec is being rewritten",
            "--root",
            str(root),
            "--ui",
            "plain",
        )
        assert rejected.returncode == 0, rejected.stdout

        _decompose(root, CLOSED)

        (after,) = _escalations(root)
        assert after.status is ItemStatus.REJECTED, after
        assert after.decision_comment == "the spec is being rewritten"

    def test_a_snoozed_item_is_resolved_too(self, tmp_path: Path) -> None:
        """Snoozed is undecided. ``open_items()`` hides an item inside its
        snooze TTL, so a resolver that selected through it would leave this
        item to come back open for a question the spec already answers."""
        root = _project(tmp_path)
        _decompose(root, ESCALATED)
        (item,) = _escalations(root)
        snoozed = run_ks(
            root, "inbox", "snooze", item.id, "--hours", "24", "--root", str(root), "--ui", "plain"
        )
        assert snoozed.returncode == 0, snoozed.stdout

        closing_run = _decompose(root, CLOSED)

        (after,) = _escalations(root)
        assert after.status is ItemStatus.RESOLVED, after
        assert closing_run in after.decision_comment, after.decision_comment

    def test_a_halt_after_the_close_opens_a_fresh_item(self, tmp_path: Path) -> None:
        """The spec escalates again after it was closed: that is a new
        question for the owner, not a repeat of the closed one."""
        root = _project(tmp_path)
        _decompose(root, ESCALATED)
        _decompose(root, CLOSED)
        _decompose(root, ESCALATED)

        items = _escalations(root)
        assert sorted(str(i.status) for i in items) == sorted(
            [str(ItemStatus.OPEN), str(ItemStatus.RESOLVED)]
        ), items
        assert "Architect escalated" in _inbox_ls(root)

    def test_a_snoozed_item_and_a_repeat_halt_are_both_resolved(self, tmp_path: Path) -> None:
        """The operator snoozes the item, the spec halts again (``Inbox.add``
        opens a fresh OPEN item because the old one is SNOOZED), and a
        closing decompose must resolve both, not just the newest."""
        root = _project(tmp_path)
        _decompose(root, ESCALATED)
        (item,) = _escalations(root)
        snoozed = run_ks(
            root, "inbox", "snooze", item.id, "--hours", "24", "--root", str(root), "--ui", "plain"
        )
        assert snoozed.returncode == 0, snoozed.stdout
        _decompose(root, ESCALATED)
        assert len(_escalations(root)) == 2, _escalations(root)
        closing_run = _decompose(root, CLOSED)
        after = _escalations(root)
        assert [str(i.status) for i in after] == [str(ItemStatus.RESOLVED)] * 2, after
        assert all(closing_run in i.decision_comment for i in after), after


def _decompose_in_process(root: Path, payload: dict[str, Any], out: io.StringIO) -> None:
    """The real ``decompose_spec`` with a stub agent and no run bus."""
    spec = root / "spec.md"
    if not spec.exists():
        spec.write_text("# Spec\n", encoding="utf-8")
    decompose_spec(
        spec_path=spec,
        project_name="demo",
        base_branch="main",
        single_pr=False,
        agent=MockDecomposeAgent(json.dumps(payload)),  # type: ignore[arg-type]
        ui=PlainUI(no_color=True, file=out),
        root_dir=root,
        prompt_call=architect_call(root),
    )


#: The inbox's documented lock failure, and a type nobody would list in an
#: enumerated ``except`` tuple. Both must warn: the helpers catch ``Exception``.
INBOX_ERRORS = pytest.mark.parametrize(
    "error", [ControlStateError, RuntimeError], ids=["control-lock", "unforeseen"]
)


class TestInProcessFailures:
    """Through the real ``decompose_spec``, in process, because the failure
    is injected into a method a subprocess cannot reach."""

    @INBOX_ERRORS
    def test_a_failed_add_warns_and_the_halt_still_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
    ) -> None:
        def refuse(self: Inbox, *args: object, **kwargs: object) -> InboxItem:
            raise error("control lock held elsewhere")

        monkeypatch.setattr(Inbox, "add", refuse)
        out = io.StringIO()
        with pytest.raises(SpecBlockerError):
            _decompose_in_process(tmp_path, ESCALATED, out)
        assert (
            "WARN: Inbox write for the escalation on spec.md failed (non-fatal): "
            "control lock held elsewhere"
        ) in out.getvalue(), out.getvalue()

    @INBOX_ERRORS
    def test_a_failed_resolve_warns_and_the_decompose_still_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
    ) -> None:
        with pytest.raises(SpecBlockerError):
            _decompose_in_process(tmp_path, ESCALATED, io.StringIO())

        def refuse(self: Inbox, *args: object, **kwargs: object) -> InboxItem:
            raise error("control lock held elsewhere")

        monkeypatch.setattr(Inbox, "resolve", refuse)
        out = io.StringIO()
        _decompose_in_process(tmp_path, CLOSED, out)

        assert (
            "WARN: Inbox resolve for the escalation on spec.md failed (items stay open): "
            "control lock held elsewhere"
        ) in out.getvalue(), out.getvalue()
        (item,) = _escalations(tmp_path)
        assert item.status is ItemStatus.OPEN, item

    def test_a_decompose_whose_manifest_did_not_save_resolves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(SpecBlockerError):
            _decompose_in_process(tmp_path, ESCALATED, io.StringIO())

        def full_disk(self: Manifest, path: Path) -> None:
            raise OSError("No space left on device")

        monkeypatch.setattr(Manifest, "save", full_disk)
        with pytest.raises(OSError, match="No space left"):
            _decompose_in_process(tmp_path, CLOSED, io.StringIO())

        (item,) = _escalations(tmp_path)
        assert item.status is ItemStatus.OPEN, item


# --- the census: every halt opens an item -----------------------------------

HALT = "kstrl.decompose.SpecBlockerError"
EMITTER = "kstrl.decisions.open_escalation_item"

#: Every scope in ``kstrl/`` that constructs a ``SpecBlockerError``, and
#: how many times. A new row, or a count that moved, is a new decompose
#: halt path: call ``decisions.open_escalation_item`` before it, then
#: add the row here.
EXPECTED_HALTS = {"decompose.py::_decompose_spec_impl": 1}

#: One control per disjunct of the net, and the two coverage outcomes.
CONTROL_LOCAL = """
class SpecBlockerError(Exception):
    pass

def halt(escalated):
    raise SpecBlockerError(escalated)
"""

CONTROL_ALIAS = """
from kstrl.decompose import SpecBlockerError as Halt

def halt(escalated):
    raise Halt(escalated)
"""

CONTROL_WITH = """
from kstrl.decisions import open_escalation_item
from kstrl.decompose import SpecBlockerError

def halt(escalated):
    open_escalation_item(escalated)
    raise SpecBlockerError(escalated)
"""

CONTROL_AFTER = """
from kstrl.decisions import open_escalation_item
from kstrl.decompose import SpecBlockerError

def halt(escalated):
    error = SpecBlockerError(escalated)
    open_escalation_item(escalated)
    raise error
"""

CONTROL_NESTED = """
from kstrl.decisions import open_escalation_item
from kstrl.decompose import SpecBlockerError

def halt(escalated):
    def later():
        open_escalation_item(escalated)
    raise SpecBlockerError(escalated)
"""


def _is_halt(node: ast.AST, table: astwalk.Bindings) -> bool:
    """A call that constructs ``SpecBlockerError``.

    FLAGGING, so it may over-match. Two disjuncts: the bare leaf name,
    which is how ``decompose.py`` spells its own class (a local ``class``
    is deliberately not a binding in ``astwalk``, so it never resolves),
    and the resolved origin, which is how an aliased import spells it.
    """
    if not isinstance(node, ast.Call):
        return False
    return astwalk.leaf_name(node.func) == "SpecBlockerError" or table.resolve(node.func) == HALT


def halt_sites(tree: ast.Module, module: str = "") -> list[tuple[str, ast.Call, bool]]:
    """Every halt in one module: its scope, the call, and whether it is covered.

    Covered means the SAME scope calls the emitter, resolved by origin,
    on an EARLIER line. CLEARING, so narrow: a call in a nested function,
    a call on a later line, or a call the resolver cannot place does not
    clear.
    """
    table = astwalk.bindings(tree, module=module)
    found: list[tuple[str, ast.Call, bool]] = []
    for scope, qualified in astwalk.scopes(tree):
        own = astwalk.own_nodes(scope)
        emits = [
            node.lineno
            for node in own
            if isinstance(node, ast.Call) and table.resolve(node.func) == EMITTER
        ]
        for node in own:
            if _is_halt(node, table):
                assert isinstance(node, ast.Call)
                found.append((qualified, node, any(line < node.lineno for line in emits)))
    return found


def _package_halts() -> list[tuple[str, ast.Call, bool]]:
    rows: list[tuple[str, ast.Call, bool]] = []
    for source in astwalk.package_sources():
        tree = astwalk.parsed(source)
        for qualified, node, covered in halt_sites(tree, astwalk.module_name(source)):
            rows.append((f"{astwalk.label(source)}::{qualified}", node, covered))
    return rows


class TestEveryHaltOpensAnItem:
    def test_the_halt_census_is_pinned(self) -> None:
        counts: dict[str, int] = {}
        for key, _, _ in _package_halts():
            counts[key] = counts.get(key, 0) + 1
        assert counts == EXPECTED_HALTS, (
            "kstrl/ constructs a SpecBlockerError somewhere new, or a count "
            "moved: a new decompose halt path. Call "
            f"decisions.open_escalation_item before it. Found: {counts}"
        )

    def test_every_halt_opens_an_item_first(self) -> None:
        uncovered = [
            f"{key}:{node.lineno}" for key, node, covered in _package_halts() if not covered
        ]
        assert uncovered == [], (
            f"{uncovered} halt decompose without calling open_escalation_item "
            "first, so the owner's inbox never hears of it (#449)."
        )

    @pytest.mark.parametrize("control", [CONTROL_LOCAL, CONTROL_ALIAS], ids=["local", "alias"])
    def test_the_net_fires(self, control: str) -> None:
        """One control per disjunct: a census that matches is also what a
        switched-off net returns."""
        assert [covered for _, _, covered in halt_sites(astwalk.parse(control))] == [False]

    @pytest.mark.parametrize(
        ("source", "covered"),
        [(CONTROL_WITH, True), (CONTROL_AFTER, False), (CONTROL_NESTED, False)],
        ids=["emitter-first", "emitter-after", "emitter-nested"],
    )
    def test_the_coverage_check_fires_both_ways(self, source: str, covered: bool) -> None:
        sites = halt_sites(astwalk.parse(source))
        assert [flag for _, _, flag in sites] == [covered]
