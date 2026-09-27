"""Every inbox filing path reaches ``[notify] on_inbox_item`` (#600).

Before #600 the hook fired from one caller of ``Inbox.add``, the
pipeline's ``_inbox_add``. The architect's escalation, an autonomy
demotion and everything ``ks serve`` files opened items that
``notifiable()`` selects and pushed nothing. The firing now lives in
``Inbox.add`` itself, on the branch that opens a new item, through
``kstrl.inbox_notify.push_opened_item``.

Each test drives a real entry point (``ks decompose`` as a subprocess,
``python -m kstrl.calibration compare`` in process, the factory's
outcome fold, one ``ks serve`` poll, ``run_factory``) with the hook set
through ``KSTRL_NOTIFY_ON_INBOX_ITEM`` to a command that appends one
line per firing, and asserts on those lines.

The rule under test: fire when ``add`` OPENS an item, never on an
occurrence bump of a still-open one. ``ks serve`` re-files the same
condition every poll, so firing on a bump would page once a minute.

The census at the bottom ties every ``Inbox.add`` site in ``kstrl/`` to
the test here that drives it, reusing the walk
``tests/test_inbox_write_guards.py`` already owns, so a seventh filing
path fails until someone writes its test. What it does NOT see, stated
rather than left implicit: an item built through ``cls(...)`` or
``dataclasses.replace`` rather than by the name ``InboxItem``.
``InboxItem.from_dict`` is the one ``cls(...)`` today, and it is the
read side.
"""

from __future__ import annotations

import ast
import json
import sys
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
    "pipeline.py::ComponentPipeline._inbox_add::add": (
        "test_pipeline_fires_once_per_kind_and_not_on_a_repeat"
    ),
    "serve.py::_file_inbox_item::add": (
        "test_serve_filing_fires_the_inbox_hook_once_per_opened_item"
    ),
}


def test_every_inbox_add_site_is_driven_by_a_path_test() -> None:
    found = {key for key in _all_rows("mutations") if key.endswith("::add")}
    assert found == set(FILING_PATHS)
    module = sys.modules[__name__]
    assert all(callable(getattr(module, name, None)) for name in FILING_PATHS.values())


def test_items_are_opened_only_in_inbox_add() -> None:
    """An ``InboxItem`` built anywhere but ``Inbox.add`` would bypass the push."""
    found: set[str] = set()
    for source in astwalk.package_sources():
        for scope, qualified in astwalk.scopes(astwalk.parsed(source)):
            for node in astwalk.own_nodes(scope):
                if isinstance(node, ast.Call) and astwalk.leaf_name(node.func) == "InboxItem":
                    found.add(f"{astwalk.label(source)}::{qualified}")
    assert found == {"inbox.py::Inbox.add"}
