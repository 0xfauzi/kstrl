"""Every inbox filing path reaches ``[notify] on_inbox_item`` (#600).

Before #600 the hook fired from one caller of ``Inbox.add``, the
pipeline's ``_inbox_add``. The architect's escalation, an autonomy
demotion and everything ``ks serve`` files opened items that
``notifiable()`` selects and pushed nothing. The firing now lives in
``Inbox.add`` itself, on the branch that opens a new item, through
``kstrl.inbox_notify.push_opened_item``.

Each behavioural test drives a real entry point (``ks decompose`` as a
subprocess, ``python -m kstrl.calibration compare`` in process, the
factory's outcome fold, one ``ks serve`` poll, ``run_factory``) and
asserts on the lines a command appended, one per firing. Most set the
hook through ``KSTRL_NOTIFY_ON_INBOX_ITEM``; the pipeline test passes it
directly as ``NotifyConfig(on_inbox_item=...)``, and the kstrl.toml test
sets it in the project's own ``[notify]`` section instead of the
environment.

The rule under test: fire when ``add`` OPENS an item, never on an
occurrence bump of a still-open one, and never for a repeat filed while
the row is still snoozed. ``ks serve`` re-files the same condition every
poll, so firing on a bump would page once a minute.

Two censuses close the bottom of the file. The first ties every
``Inbox.add`` call site in ``kstrl/`` to the test above that drives it,
reusing the walk ``tests/test_inbox_write_guards.py`` already owns: a
new filing path makes it fail until its test is added to
``FILING_PATHS``. The second walks every ``InboxItem`` construction in
``kstrl/`` and asserts the only one is inside ``Inbox.add``, so nothing
can open an item without going through the push. What it does NOT see,
stated rather than left implicit: an item built through ``cls(...)`` or
``dataclasses.replace`` rather than by the name ``InboxItem``.
``InboxItem.from_dict`` is the one ``cls(...)`` today, and it is the
read side; that gap is pinned below as a disclosed blind spot.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.autonomy import AutonomyLevel, AutonomyState
from kstrl.factory import ComponentResult, run_factory
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.observability import NotifyConfig
from kstrl.serve import ServeConfig, SpendLedger, serve_cycle
from kstrl.ui.plain import PlainUI
from tests.helpers import astwalk
from tests.helpers.demotion import (
    MISSED_IN_NEW,
    NEW_TS,
    OLD_TS,
    baseline_pair,
    fake_health,
    inbox_items,
    make_breach,
    run_compare,
    run_outcome,
    write_config,
)
from tests.test_escalation_inbox import ESCALATED, _decompose, _project
from tests.test_inbox_write_guards import _all_rows
from tests.test_notify import (
    _count_cmd,
    _lines,
    _plain_base_config,
    _plain_factory_config,
    _setup_plain_project,
    _two_component_manifest,
)


def _hook_into(monkeypatch: pytest.MonkeyPatch, lines: Path) -> None:
    """Set the hook to append ``<event>|<run id>`` to ``lines`` per firing."""
    monkeypatch.setenv(
        "KSTRL_NOTIFY_ON_INBOX_ITEM",
        f"echo \"$KSTRL_NOTIFY_EVENT|$KSTRL_NOTIFY_RUN_ID\" >> '{lines}'",
    )


def _corrupt_ledger(root: Path) -> None:
    """Make ``serve_cycle`` file its unreadable-ledger ``budget_overrun`` item."""
    ledger = SpendLedger(root)
    ledger.charge(1.0, covered_calls=1, total_calls=1)
    ledger.path.write_text("{corrupt", encoding="utf-8")


def _poll(root: Path) -> tuple[str, ...]:
    """One ``ks serve`` poll; returns the inbox item ids it reported."""
    return serve_cycle(root, config=ServeConfig()).inbox_items


def test_architect_escalation_fires_the_inbox_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ks decompose`` halted on the owner pushes once; the repeat halt does not."""
    lines = tmp_path / "hook.txt"
    _hook_into(monkeypatch, lines)
    root = _project(tmp_path)

    run_id = _decompose(root, ESCALATED)
    _decompose(root, ESCALATED)

    escalations = [i for i in Inbox(root).items() if i.kind is ItemKind.SPEC_ESCALATION]
    assert [i.occurrences for i in escalations] == [2]
    assert _lines(lines) == [f"inbox_spec_escalation|{run_id}"]


def test_demotion_fires_the_inbox_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A calibration regression files drift (silent) and a demotion (pushed)."""
    lines = tmp_path / "hook.txt"
    _hook_into(monkeypatch, lines)
    write_config(tmp_path, demote_on_calibration=True)
    AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
    old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

    assert run_compare(tmp_path, old, new) == 1

    assert len(inbox_items(tmp_path, ItemKind.CALIBRATION_DRIFT)) == 1
    assert len(inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE)) == 1
    assert _lines(lines) == [f"inbox_demotion_notice|calibration:{OLD_TS}:{NEW_TS}"]


def test_health_breach_is_silent_and_its_demotion_is_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory's health seam files a breach (silent) and a demotion (pushed)."""
    lines = tmp_path / "hook.txt"
    _hook_into(monkeypatch, lines)
    monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
    write_config(tmp_path, demote_on_health=True)
    AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)

    run_outcome(tmp_path, run_id="r-health")

    assert len(inbox_items(tmp_path, ItemKind.HEALTH_BREACH)) == 1
    assert len(inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE)) == 1
    assert _lines(lines) == ["inbox_demotion_notice|r-health"]


def test_serve_filing_fires_the_inbox_hook_once_per_opened_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ``ks serve`` polls over one unreadable ledger: one item, one push."""
    lines = tmp_path / "hook.txt"
    _hook_into(monkeypatch, lines)
    _corrupt_ledger(tmp_path)

    first = _poll(tmp_path)
    second = _poll(tmp_path)

    assert first == second
    assert len(first) == 1 and first[0]
    overruns = inbox_items(tmp_path, ItemKind.BUDGET_OVERRUN)
    assert [i.occurrences for i in overruns] == [2]
    assert _lines(lines) == ["inbox_budget_overrun|"]


def test_a_snoozed_items_repeat_opens_a_fresh_row_but_does_not_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat filed while the row is still snoozed must not page again."""
    lines = tmp_path / "hook.txt"
    _hook_into(monkeypatch, lines)
    _corrupt_ledger(tmp_path)

    first = _poll(tmp_path)
    Inbox(tmp_path, InboxConfig()).snooze(first[0], actor="op", hours=24)
    second = _poll(tmp_path)

    assert len(first) == 1 and first[0]
    assert len(second) == 1 and second[0] and second[0] != first[0]
    assert _lines(lines) == ["inbox_budget_overrun|"]


def test_a_lapsed_snoozed_items_repeat_pages_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat filed once the earlier row's snooze has lapsed must page again."""
    lines = tmp_path / "hook.txt"
    _hook_into(monkeypatch, lines)
    _corrupt_ledger(tmp_path)

    first = _poll(tmp_path)
    box = Inbox(tmp_path, InboxConfig())
    box.snooze(first[0], actor="op", hours=24)
    stored = box.get(first[0])
    assert stored is not None
    stored.snooze_until = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    box._append_unlocked(stored)
    second = _poll(tmp_path)

    assert len(first) == 1 and first[0]
    assert len(second) == 1 and second[0] and second[0] != first[0]
    assert _lines(lines) == ["inbox_budget_overrun|", "inbox_budget_overrun|"]


def test_an_approved_items_repeat_pages_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat filed once the earlier row was approved must page again."""
    lines = tmp_path / "hook.txt"
    _hook_into(monkeypatch, lines)
    _corrupt_ledger(tmp_path)

    first = _poll(tmp_path)
    Inbox(tmp_path, InboxConfig()).approve(first[0], actor="op")
    second = _poll(tmp_path)

    assert len(first) == 1 and first[0]
    assert len(second) == 1 and second[0] and second[0] != first[0]
    assert _lines(lines) == ["inbox_budget_overrun|", "inbox_budget_overrun|"]


def test_a_hook_that_files_an_item_runs_after_the_add_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#648: the hook runs after ``add`` leaves the control lock.

    ``add`` folds and appends inside one lock hold, and the hook is a
    subprocess of the operator's choosing. Here it files an inbox item
    itself, a second writer that needs the same lock. ``exec`` makes the
    hook's timeout kill that writer rather than only its shell, so a hook
    run inside the hold is killed at the timeout and files nothing.
    """
    script = (
        "import sys; from pathlib import Path; from kstrl.inbox import Inbox, ItemKind; "
        "Inbox(Path(sys.argv[1])).add(ItemKind.HEALTH_BREACH, 'filed by the hook', "
        "dedupe_key='hook')"
    )
    monkeypatch.setenv(
        "KSTRL_NOTIFY_ON_INBOX_ITEM", f"exec '{sys.executable}' -c \"{script}\" '{tmp_path}'"
    )
    monkeypatch.setenv("KSTRL_NOTIFY_HOOK_TIMEOUT", "10")
    _corrupt_ledger(tmp_path)

    first = _poll(tmp_path)

    assert len(first) == 1 and first[0]
    filed = inbox_items(tmp_path, ItemKind.HEALTH_BREACH)
    assert [i.title for i in filed] == ["filed by the hook"]


def test_pipeline_fires_once_per_kind_and_not_on_a_repeat(tmp_path: Path) -> None:
    """Run 1 opens two halted_run items and pushes once; run 2 bumps both and pushes nothing."""
    root = _setup_plain_project(tmp_path)
    lines = tmp_path / "inbox-count.txt"
    config = _plain_factory_config(notify_config=NotifyConfig(on_inbox_item=_count_cmd(lines)))

    def fake_run(component_id: str, *args: object, **kwargs: object) -> ComponentResult:
        return ComponentResult(component_id, success=False, error="boom")

    for expected_occurrences in (1, 2):
        with patch("kstrl.factory._run_component", side_effect=fake_run):
            run_factory(
                _two_component_manifest(),
                config,
                _plain_base_config(root),
                PlainUI(no_color=True),
                root,
            )
        halted = inbox_items(root, ItemKind.HALTED_RUN)
        assert [i.occurrences for i in halted] == [expected_occurrences] * 2
        assert _lines(lines) == ["inbox_halted_run a"]


def test_notify_action_required_off_silences_every_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[inbox] notify_action_required = false`` records the items and pushes none."""
    lines = tmp_path / "hook.txt"
    _hook_into(monkeypatch, lines)
    monkeypatch.setenv("KSTRL_INBOX_NOTIFY", "0")
    write_config(tmp_path, demote_on_calibration=True)
    AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
    old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)
    _corrupt_ledger(tmp_path)

    assert run_compare(tmp_path, old, new) == 1
    assert len(_poll(tmp_path)) == 1

    assert len(inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE)) == 1
    assert len(inbox_items(tmp_path, ItemKind.BUDGET_OVERRUN)) == 1
    assert _lines(lines) == []


def test_an_unreadable_notify_config_keeps_the_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A ``[notify]`` that will not load costs the push, never the item or its id."""
    _hook_into(monkeypatch, tmp_path / "hook.txt")
    monkeypatch.setenv("KSTRL_NOTIFY_HOOK_TIMEOUT", "not-a-number")
    _corrupt_ledger(tmp_path)

    ids = _poll(tmp_path)

    assert len(ids) == 1 and ids[0]
    assert Inbox(tmp_path, InboxConfig()).get(ids[0]) is not None
    assert "notify hook did not run" in capfd.readouterr().err


def test_a_hook_that_raises_keeps_the_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A type nobody would list in an enumerated ``except`` still costs only the push."""
    _hook_into(monkeypatch, tmp_path / "hook.txt")
    _corrupt_ledger(tmp_path)

    class Unforeseen(Exception):
        """No builtin category: only ``except Exception`` catches it."""

    with patch(
        "kstrl.observability.NotifyHooks.fire_inbox_item",
        side_effect=Unforeseen("unforeseen"),
    ):
        ids = _poll(tmp_path)

    assert len(ids) == 1 and ids[0]
    assert Inbox(tmp_path, InboxConfig()).get(ids[0]) is not None
    assert "notify hook did not run: unforeseen" in capfd.readouterr().err


def test_the_hook_in_kstrl_toml_fires_after_the_item_is_on_disk(tmp_path: Path) -> None:
    """``[notify]`` in the project's kstrl.toml is read, and the item is written first."""
    lines = tmp_path / "hook.txt"
    inbox_file = Inbox(tmp_path, InboxConfig()).path
    command = f"grep -c budget_overrun '{inbox_file}' >> '{lines}'"
    (tmp_path / "kstrl.toml").write_text(
        f"[notify]\non_inbox_item = {json.dumps(command)}\n", encoding="utf-8"
    )
    _corrupt_ledger(tmp_path)

    assert len(_poll(tmp_path)) == 1

    assert _lines(lines) == ["1"]


def test_a_hook_run_outside_a_run_writes_nothing_to_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The fallback hook discards its output: a TUI may own the terminal."""
    ran = tmp_path / "ran.txt"
    monkeypatch.setenv(
        "KSTRL_NOTIFY_ON_INBOX_ITEM",
        f"echo HOOK-OUTPUT-LEAK; echo HOOK-ERROR-LEAK >&2; echo ran >> '{ran}'",
    )
    _corrupt_ledger(tmp_path)

    _poll(tmp_path)

    captured = capfd.readouterr()
    assert _lines(ran) == ["ran"]
    assert "HOOK-OUTPUT-LEAK" not in captured.out
    assert "HOOK-ERROR-LEAK" not in captured.err


def test_a_failing_hook_outside_a_run_is_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A fallback hook that exits nonzero is reported on stderr, never swallowed."""
    monkeypatch.setenv("KSTRL_NOTIFY_ON_INBOX_ITEM", "exit 3")
    _corrupt_ledger(tmp_path)

    assert len(_poll(tmp_path)) == 1

    assert "notify hook 'inbox_budget_overrun' exited 3 (non-fatal)" in capfd.readouterr().err


def test_a_plan_decided_at_the_prompt_pages_nobody_and_a_parked_one_pages_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#602: the L1 plan gate files an item for an answer given at the prompt
    (so a resumed run finds it) and pages nobody for it; a plan it parks
    pages once, through the pipeline."""
    from tests.test_l1_plan_gate import _answerable_repo, _Channel, _in_process, _manifest_path

    lines = tmp_path / "inbox-hook.txt"
    _hook_into(monkeypatch, lines)
    root = _answerable_repo(tmp_path)

    _in_process(root, _Channel(answered=True, choice=0), monkeypatch)

    assert [str(i.status) for i in inbox_items(root, ItemKind.PLAN_GATE)] == ["approved"]
    assert _lines(lines) == []

    data = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
    data["components"][0]["title"] = "a changed plan"
    _manifest_path(root).write_text(json.dumps(data), encoding="utf-8")

    _in_process(root, _Channel(answered=False, choice=0), monkeypatch)

    assert [line.split("|")[0] for line in _lines(lines)] == ["inbox_plan_gate"]


def test_an_unconfirmed_stack_pages_once_however_often_it_is_refused(tmp_path: Path) -> None:
    """#696 slice 3: `ks factory` refusing an unconfirmed [stack] opens its
    stack_confirmation item and pages once; the second refusal bumps the
    same open item and pages nobody."""
    from tests.test_stack_e2e import _factory, _repo, _stack

    lines = tmp_path / "inbox-hook.txt"
    hook = {
        "KSTRL_NOTIFY_ON_INBOX_ITEM": (
            f"echo \"$KSTRL_NOTIFY_EVENT|$KSTRL_NOTIFY_RUN_ID\" >> '{lines}'"
        )
    }
    root = _repo(tmp_path, _stack({"tests": "true"}), confirm=False)

    first = _factory(tmp_path, root, env=hook)
    second = _factory(tmp_path, root, env=hook)

    assert (first.code, second.code) == (2, 2), first.out + second.out
    assert [line.split("|")[0] for line in _lines(lines)] == ["inbox_stack_confirmation"]
    (item,) = inbox_items(root, ItemKind.STACK_CONFIRMATION)
    assert item.occurrences == 2


# --- census: every filing path is driven by a test above -----------------

#: Every ``Inbox.add`` site in ``kstrl/``, keyed as
#: ``tests/test_inbox_write_guards.py`` keys it, mapped to the test in
#: this module that drives it through a real entry point.
FILING_PATHS = {
    "autonomy.py::apply_demotion::add": "test_demotion_fires_the_inbox_hook",
    "calibration_ladder.py::_open_drift_item::add": "test_demotion_fires_the_inbox_hook",
    "decisions.py::open_escalation_item::add": "test_architect_escalation_fires_the_inbox_hook",
    "factory.py::_open_health_breach_items::add": (
        "test_health_breach_is_silent_and_its_demotion_is_pushed"
    ),
    "plan_gate.py::_record::add": (
        "test_a_plan_decided_at_the_prompt_pages_nobody_and_a_parked_one_pages_once"
    ),
    "pipeline.py::ComponentPipeline._inbox_add::add": (
        "test_pipeline_fires_once_per_kind_and_not_on_a_repeat"
    ),
    "serve.py::_file_inbox_item::add": (
        "test_serve_filing_fires_the_inbox_hook_once_per_opened_item"
    ),
    "stack.py::file_stack_item::add": (
        "test_an_unconfirmed_stack_pages_once_however_often_it_is_refused"
    ),
}


def test_every_inbox_add_site_is_driven_by_a_path_test() -> None:
    found = {key for key in _all_rows("mutations") if key.endswith("::add")}
    assert found == set(FILING_PATHS)
    module = sys.modules[__name__]
    assert all(callable(getattr(module, name, None)) for name in FILING_PATHS.values())


INBOX_ITEM = "kstrl.inbox.InboxItem"

#: Two disjuncts, one control each: a local shadow (the bare leaf name,
#: which needs no resolution) and an aliased import or a rebind (which
#: resolves only through ``Bindings``). ``set()`` on both was the miss:
#: the old census matched only the leaf name.
CONTROL_LOCAL_ITEM = """
class InboxItem:
    pass

def build():
    return InboxItem(id="x")
"""

CONTROL_ALIAS_ITEM = """
from kstrl.inbox import InboxItem as Item

def build():
    return Item(id="x")
"""

CONTROL_REBIND_ITEM = """
from kstrl.inbox import InboxItem

Make = InboxItem

def build():
    return Make(id="x")
"""

#: Disclosed and out of scope for the census below: a classmethod's
#: ``cls(...)``. ``cls`` is a parameter, not an assignment ``bindings``
#: walks, so neither disjunct resolves it. ``InboxItem.from_dict`` is
#: the one ``cls(...)`` today, and it is the read side.
CONTROL_CLS_ITEM = """
class InboxItem:
    @classmethod
    def from_dict(cls, data):
        return cls(id=data["id"])
"""


def _is_inbox_item(node: ast.AST, table: astwalk.Bindings) -> bool:
    """A call that constructs ``InboxItem``.

    FLAGGING, so it may over-match. Two disjuncts: the bare leaf name,
    which is how ``inbox.py`` spells its own class, and the resolved
    origin, which is how an aliased import or a rebind spells it.
    """
    if not isinstance(node, ast.Call):
        return False
    return astwalk.leaf_name(node.func) == "InboxItem" or table.resolve(node.func) == INBOX_ITEM


def _inbox_item_sites(tree: ast.Module, module: str = "") -> list[tuple[str, ast.Call]]:
    table = astwalk.bindings(tree, module=module)
    found: list[tuple[str, ast.Call]] = []
    for scope, qualified in astwalk.scopes(tree):
        for node in astwalk.own_nodes(scope):
            if _is_inbox_item(node, table):
                assert isinstance(node, ast.Call)
                found.append((qualified, node))
    return found


def test_items_are_opened_only_in_inbox_add() -> None:
    """An ``InboxItem`` built anywhere but ``Inbox.add`` would bypass the push."""
    found: set[str] = set()
    for source in astwalk.package_sources():
        tree = astwalk.parsed(source)
        for qualified, _ in _inbox_item_sites(tree, astwalk.module_name(source)):
            found.add(f"{astwalk.label(source)}::{qualified}")
    assert found == {"inbox.py::Inbox.add"}


@pytest.mark.parametrize(
    "control",
    [CONTROL_LOCAL_ITEM, CONTROL_ALIAS_ITEM, CONTROL_REBIND_ITEM],
    ids=["local", "alias", "rebind"],
)
def test_the_inbox_item_net_fires(control: str) -> None:
    """One control per disjunct: a construction the census must not miss."""
    assert len(_inbox_item_sites(astwalk.parse(control))) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_cls_construction_is_a_disclosed_blind_spot() -> None:
    astwalk.blind_spot(
        lambda source: bool(_inbox_item_sites(astwalk.parse(source))),
        CONTROL_CLS_ITEM,
    )
