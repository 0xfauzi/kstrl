"""A config read on the daemon's poll path may refuse; it may not escape.

``serve`` has no per-cycle handler. ``serve_cycle`` runs inside
``serve._cycle`` with nothing above it, so an exception raised by a
config read on the poll path leaves ``serve()`` entirely and stops the
daemon; under launchd it is relaunched on ``LAUNCHD_THROTTLE_SECONDS``
and dies again on the same key. That is #318's lesson stated for this
module, and ``check_open_pr_bound`` already carries a paragraph about it.

Two layers, because a single one of these has gone blind eleven times in
this repo.

LAYER 1 is a census that enumerates no node types: every spelling of
``load`` in ``kstrl/serve.py``, counted. A new way to obtain a config -
through a class held in a dict, through an alias, through a shape
nobody modelled - moves that number even when layer 2's pattern cannot
see it.

LAYER 2 is the classifier. Every ``.load`` call is partitioned into a
config read, a read of something else, or a receiver the walk could not
name, with no fourth bucket; the config reads are pinned by enclosing
function and each is decided GUARDED or UNGUARDED; and every UNGUARDED
one must appear in :data:`UNGUARDED_LEDGER` with a reason.

The classifier CLEARS as well as flags, so it is built to the narrow
rule: it credits a ``try`` only when it can prove the handler is broad
AND that no clause of that ``try`` re-raises. Anything it cannot prove
is UNGUARDED, which costs a ledger row somebody reads rather than a
silent clearance. ``TestTheClassifierCanFail`` mutates it four ways.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.serve import OpenPrCount, RunOutcome, serve
from kstrl.workqueue import ItemState, MergeDisposition, Queue, QueueConfig
from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    all_nodes,
    assert_census,
    bindings,
    blind_spot,
    handler_clauses,
    own_nodes,
    parse,
    parsed,
    scope_of,
    spells,
    try_body_nodes,
)

SERVE_SOURCE = KSTRL_PACKAGE / "serve.py"

# --------------------------------------------------------------------------
# Layer 1: every spelling of `load`, enumerating no node types
# --------------------------------------------------------------------------

#: How many nodes in ``kstrl/serve.py`` write the identifier ``load``
#: anywhere the AST can hold a string. Not individually meaningful; the
#: DELTA is the signal, and it moves for a shape layer 2 does not model
#: as readily as for one it does.
EXPECTED_LOAD_SPELLINGS = {"serve.py": 17}


def test_every_spelling_of_load_in_serve_is_counted() -> None:
    assert_census(
        sources=[SERVE_SOURCE],
        sees=spells("load"),
        expected=EXPECTED_LOAD_SPELLINGS,
        control="Cfg.load(root)\n",
        message=(
            "kstrl/serve.py gained or lost a spelling of `load`. If it is a "
            "new config read on the poll path it needs a guard and a row in "
            "this module's census; if layer 2 cannot see it, that is the "
            "case this layer exists for."
        ),
    )


# --------------------------------------------------------------------------
# Layer 2: the classifier
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadSite:
    """One ``.load`` call, and what the walk could decide about it."""

    lineno: int
    scope: str
    receiver: str
    guarded: bool

    @property
    def label(self) -> str:
        return f"{self.scope}::{self.receiver}:{self.lineno}"


@dataclass(frozen=True)
class LoadScan:
    """The complete answer about one module's ``.load`` calls.

    Three buckets and no fourth. ``undecided`` is a receiver the walk
    could not name, which is a ROW rather than an absence: a call it
    cannot read must not read as a call it does not have to.
    """

    config: tuple[LoadSite, ...] = ()
    other: tuple[str, ...] = ()
    undecided: tuple[str, ...] = ()


def _broad_and_total(node: ast.Try | ast.TryStar, table: Any) -> bool:
    """Does this ``try`` catch everything the document can raise?

    Two conditions, both required, because this answer CLEARS a site.

    A clause naming ``Exception`` exactly. ``BaseException`` and a bare
    ``except:`` do not count: everything about a malformed DOCUMENT
    derives from ``Exception``, while ``KeyboardInterrupt`` and
    ``SystemExit`` are about the process, so catching them is a different
    and worse thing rather than a broader version of the same one. An
    enumeration of types (``except (OSError, ValueError)``) does not
    count either - that is the defect #318 shipped three times.

    And NO clause of this ``try`` re-raises. Siblings do not chain: a
    narrow clause above that re-raises lets its own type out past the
    broad one below, and a broad clause that re-raises guards nothing at
    all. The walk cannot decide which exception a re-raising ladder still
    lets through, so it declines to clear.
    """
    clauses = handler_clauses(node, table)
    if not any(clause.decided and "Exception" in clause.names for clause in clauses):
        return False
    for handler in node.handlers:
        for child in [handler, *own_nodes(handler)]:
            if isinstance(child, ast.Raise):
                return False
    return True


def _guarding_tries(tree: ast.Module, table: Any) -> set[int]:
    """Node ids covered by a ``try`` this walk can prove catches everything.

    ``try_body_nodes`` and not ``ast.walk``: a helper DEFINED in the body
    and called elsewhere runs under no handler of this ``try``, and
    crediting it would be the clearing direction of the same defect.
    """
    covered: set[int] = set()
    for node in all_nodes(tree):
        if not isinstance(node, ast.Try | ast.TryStar):
            continue
        if not _broad_and_total(node, table):
            continue
        covered |= {id(child) for child in try_body_nodes(node)}
    return covered


def scan_loads(tree: ast.Module, *, module: str = "") -> LoadScan:
    """Partition every ``.load`` call in one module.

    Candidacy is the attribute name, which is the identifier a reader
    greps for. The receiver decides the bucket: a bare ``Name`` ending in
    ``Config`` is a config read, any other bare ``Name`` is somebody
    else's ``load``, and a receiver that is not a bare ``Name`` at all is
    UNDECIDED. There are none of the third kind in ``kstrl/serve.py``
    today, which is exactly why the bucket has to exist and be asserted
    rather than assumed away.
    """
    table = bindings(tree, module=module)
    owner = scope_of(tree)
    covered = _guarding_tries(tree, table)
    config: list[LoadSite] = []
    other: list[str] = []
    undecided: list[str] = []
    for node in all_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "load":
            continue
        scope = owner.get(id(node), "<module>")
        if not isinstance(func.value, ast.Name):
            undecided.append(f"{scope}:{node.lineno} {ast.dump(func.value)[:60]}")
            continue
        receiver = func.value.id
        if receiver.endswith("Config"):
            config.append(LoadSite(node.lineno, scope, receiver, id(node) in covered))
        else:
            other.append(receiver)
    return LoadScan(tuple(config), tuple(sorted(other)), tuple(sorted(undecided)))


def _serve_scan() -> LoadScan:
    return scan_loads(parsed(SERVE_SOURCE), module="kstrl.serve")


#: Every ``<Name>Config.load(...)`` in ``kstrl/serve.py``, by the function
#: that holds it. Two in ``resolve_merge_gate`` because it consults the
#: ladder and the policy; two ``ServeConfig`` sites because ``serve``
#: loads once at start and ``serve_cycle`` loads when called on its own.
EXPECTED_CONFIG_LOADS = {
    "_file_inbox_item": 1,
    "_merge_gate": 1,
    "_report_remote_outcome": 1,
    "_run_intake": 1,
    "check_inbox_cap": 1,
    "check_open_pr_bound": 1,
    "resolve_merge_gate": 2,
    "serve": 1,
    "serve_cycle": 2,
}

#: The other ``.load`` receivers in the module, counted. Pinned so a new
#: one is a row somebody adds deliberately rather than a call the config
#: census silently does not cover.
EXPECTED_OTHER_LOADS = {
    "AutonomyState": 1,
    "Manifest": 2,
    "OpenPrCountStreak": 1,
}

#: Every config read the classifier could not prove guarded, and why it
#: is allowed to stay that way. A site absent from here fails by name; a
#: row here with no site fails too, because a give-up nobody needs is a
#: give-up nobody re-examines.
UNGUARDED_LEDGER = {
    "_file_inbox_item": (
        "narrower on purpose, and pinned that way by another guard. "
        "`tests/test_inbox_write_guards.py` requires this handler to name "
        "`ControlStateError` by ORIGIN and `TypeError` by name, because "
        "`Inbox._append` takes the control lock and `int()` on a TOML date "
        "is not a ValueError; `except Exception` was tried here and makes "
        "both names disappear, so that guard fires. It catches strictly "
        "more, and the enumeration is complete over what was MEASURED to "
        "escape `InboxConfig.load`: a ConfigError, a plain ValueError and a "
        "TypeError. A deeply nested array does not escape as a "
        "RecursionError, because `config.load_toml_document` normalises "
        "every parser fault to ConfigError first. Reconciling the two "
        "guards' rules - one wants the whole surface, the other wants the "
        "names - is a decision for the owner, not a side effect of this "
        "change."
    ),
    "serve": (
        "daemon entry. `serve` is not in `_PREFLIGHT_EXEMPT`, so a malformed "
        "document is refused at command entry with exit 2 before this runs, "
        "and `serve` passes the loaded ServeConfig down so no poll re-reads "
        "[serve]. Measured on this branch and at origin/main 037f0e1: a "
        "[serve] section made malformed AFTER daemon start does not reach a "
        "load, and the cycle completes."
    ),
    "serve_cycle": (
        "`ServeConfig` here is reached only when `serve_cycle` is called "
        "directly, which is a command entry and preflighted, because "
        "`serve` passes its own down. `QueueConfig` IS re-read every poll, "
        "and it is the frame a malformed [queue] section - or a document "
        "that is not TOML at all - dies in: measured through a real "
        "serve(once=True) at origin/main 037f0e1 and on this branch, "
        "identically at both, so it is a pre-existing hole recorded here "
        "rather than one this change introduces. Not fixed with the others "
        "because a cycle that cannot read the queue config has no queue to "
        "record a refusal in, so its failure mode is a decision about the "
        "cycle rather than about the merge gate."
    ),
}


class TestTheConfigReadsAreInventoried:
    def test_the_config_load_census_is_pinned(self) -> None:
        scan = _serve_scan()
        counted: dict[str, int] = {}
        for site in scan.config:
            counted[site.scope] = counted.get(site.scope, 0) + 1
        assert counted == EXPECTED_CONFIG_LOADS, (
            "kstrl/serve.py's config reads moved. A read on the poll path "
            "must be inside `except Exception` and must refuse rather than "
            "clear or escape. Sites: "
            f"{[site.label for site in scan.config]}"
        )

    def test_the_other_load_receivers_are_pinned(self) -> None:
        scan = _serve_scan()
        counted: dict[str, int] = {}
        for receiver in scan.other:
            counted[receiver] = counted.get(receiver, 0) + 1
        assert counted == EXPECTED_OTHER_LOADS, (
            "a `.load` receiver in kstrl/serve.py that is not a config "
            "appeared or went away. If it is a config under another name, "
            "the config census above does not cover it."
        )

    def test_no_load_receiver_is_left_unnamed(self) -> None:
        scan = _serve_scan()
        assert scan.undecided == (), (
            "a `.load` call in kstrl/serve.py has a receiver this walk "
            "cannot name, so it is in neither census. Decide it here rather "
            "than leaving it out: "
            f"{scan.undecided}"
        )

    def test_every_unguarded_config_read_is_in_the_ledger(self) -> None:
        scan = _serve_scan()
        unguarded = sorted({site.scope for site in scan.config if not site.guarded})
        missing = [scope for scope in unguarded if scope not in UNGUARDED_LEDGER]
        assert missing == [], (
            "a config read in kstrl/serve.py is not inside an "
            "`except Exception` that refuses. The daemon has no per-cycle "
            "handler, so this stops the loop on an operator's typo in an "
            "unrelated key. Guard it and make the failure a refusal, or add "
            f"a row to UNGUARDED_LEDGER saying why not: {missing}"
        )

    def test_the_ledger_has_no_row_without_a_site(self) -> None:
        scan = _serve_scan()
        unguarded = {site.scope for site in scan.config if not site.guarded}
        stale = sorted(scope for scope in UNGUARDED_LEDGER if scope not in unguarded)
        assert stale == [], (
            "UNGUARDED_LEDGER excuses a site that is now guarded or gone. "
            "Drop the row: a give-up nobody needs is a give-up nobody "
            f"re-examines. {stale}"
        )

    def test_the_merge_gate_reads_are_guarded(self) -> None:
        """The two functions #195 put on the poll path, named."""
        scan = _serve_scan()
        by_scope = {site.scope: site for site in scan.config if site.scope != "serve_cycle"}
        assert by_scope["_merge_gate"].guarded
        assert all(site.guarded for site in scan.config if site.scope == "resolve_merge_gate")
        assert by_scope["check_inbox_cap"].guarded


class TestTheClassifierCanFail:
    """A guard you did not mutate is a guard you did not test.

    Four mutations against in-memory sources, one per way the classifier
    could go quiet: no ``try`` at all, a narrowed handler, a re-raising
    handler, and a receiver it cannot name.
    """

    @staticmethod
    def _one(source: str) -> LoadScan:
        return scan_loads(parse(source))

    def test_a_broad_handler_clears(self) -> None:
        scan = self._one("try:\n    c = FooConfig.load(r)\nexcept Exception:\n    pass\n")
        assert [site.guarded for site in scan.config] == [True]

    def test_a_read_with_no_try_is_unguarded(self) -> None:
        scan = self._one("c = FooConfig.load(r)\n")
        assert [site.guarded for site in scan.config] == [False]

    def test_a_narrowed_handler_is_unguarded(self) -> None:
        scan = self._one("try:\n    c = FooConfig.load(r)\nexcept ConfigError:\n    pass\n")
        assert [site.guarded for site in scan.config] == [False]

    def test_an_enumeration_of_types_is_unguarded(self) -> None:
        scan = self._one(
            "try:\n    c = FooConfig.load(r)\nexcept (OSError, ValueError):\n    pass\n"
        )
        assert [site.guarded for site in scan.config] == [False]

    def test_base_exception_is_not_broad_enough_to_clear(self) -> None:
        scan = self._one("try:\n    c = FooConfig.load(r)\nexcept BaseException:\n    pass\n")
        assert [site.guarded for site in scan.config] == [False]

    def test_a_bare_except_is_not_broad_enough_to_clear(self) -> None:
        scan = self._one("try:\n    c = FooConfig.load(r)\nexcept:\n    pass\n")
        assert [site.guarded for site in scan.config] == [False]

    def test_a_reraising_broad_handler_is_unguarded(self) -> None:
        scan = self._one("try:\n    c = FooConfig.load(r)\nexcept Exception:\n    raise\n")
        assert [site.guarded for site in scan.config] == [False]

    def test_a_narrow_sibling_that_reraises_is_unguarded(self) -> None:
        scan = self._one(
            "try:\n    c = FooConfig.load(r)\n"
            "except OSError:\n    raise\n"
            "except Exception:\n    pass\n"
        )
        assert [site.guarded for site in scan.config] == [False]

    def test_a_helper_defined_in_the_body_is_not_credited(self) -> None:
        """The call runs where the helper is CALLED, not where it is written."""
        scan = self._one(
            "try:\n    def read():\n        return FooConfig.load(r)\nexcept Exception:\n    pass\n"
        )
        assert [site.guarded for site in scan.config] == [False]

    def test_a_handler_named_through_an_import_alias_does_not_clear(self) -> None:
        """A spelling is not an identity: ``Exception`` can be rebound."""
        scan = self._one(
            "from json import JSONDecodeError as Exception\n"
            "try:\n    c = FooConfig.load(r)\nexcept Exception:\n    pass\n"
        )
        assert [site.guarded for site in scan.config] == [False]

    def test_a_receiver_the_walk_cannot_name_is_undecided(self) -> None:
        scan = self._one("c = TABLE['cfg'].load(r)\n")
        assert scan.config == ()
        assert len(scan.undecided) == 1

    def test_a_non_config_receiver_is_not_a_config_read(self) -> None:
        scan = self._one("s = Manifest.load(r)\n")
        assert scan.config == ()
        assert scan.other == ("Manifest",)

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_config_class_not_spelled_config_is_a_disclosed_limit(self) -> None:
        """Candidacy is the RECEIVER'S NAME, so ``Settings.load`` is missed.

        Layer 1 is what covers it: the spelling census counts the call
        whatever the receiver is called.
        """
        blind_spot(
            lambda text: bool(self._one(text).config),
            "c = Settings.load(r)\n",
        )


# --------------------------------------------------------------------------
# The behaviour: a section made malformed after the daemon started
# --------------------------------------------------------------------------

CLEAN_TOML = "[autonomy]\nenabled = true\n[serve]\nmax_open_prs = 0\n"

#: ``(label, the whole document, the section the message must name)``.
#: Whole documents rather than appended fragments: appending a second
#: ``[autonomy]`` table makes the DOCUMENT invalid, which measures a
#: different fault from the one under test.
MALFORMED_SECTIONS = [
    pytest.param(
        CLEAN_TOML + '[factory]\npause_before_pr_merge = "false"\n',
        "[factory]",
        id="factory-strict-bool",
    ),
    pytest.param(
        CLEAN_TOML + '[factory]\nmax_parallel = "two"\n',
        "[factory]",
        id="factory-int-cast",
    ),
    pytest.param(
        '[autonomy]\nenabled = "yes"\n[serve]\nmax_open_prs = 0\n',
        "[autonomy]",
        id="autonomy-strict-bool",
    ),
    pytest.param(
        CLEAN_TOML + '[policy]\nmax_files_changed = "ten"\n',
        "[policy]",
        id="policy-int-cast",
    ),
    pytest.param(
        CLEAN_TOML + '[inbox]\nenabled = true\nopen_item_cap = "ten"\n',
        "[inbox]",
        id="inbox-int-cast",
    ),
]

#: The subset `resolve_merge_gate` itself reads. Derived, not written out
#: again: a new row in MALFORMED_SECTIONS joins this list automatically,
#: and `[inbox]` is the one exception because an unreadable `[inbox]` is
#: refused by `check_inbox_cap`, which returns an `Admission` and never
#: reaches a `MergeGate`.
MERGE_GATE_SECTIONS = [case for case in MALFORMED_SECTIONS if case.values[1] != "[inbox]"]


def _stub_run(**kwargs: object) -> RunOutcome:
    root = kwargs["root_dir"]
    assert isinstance(root, Path)
    run_dir = root / ".kstrl" / "runs" / "factory-20260730-000000.000000-aaa"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").touch()
    return RunOutcome(0)


class TestAMalformedSectionDoesNotStopTheDaemon:
    """The reachable case is an operator editing kstrl.toml mid-run.

    ``ks serve`` is not in ``_PREFLIGHT_EXEMPT``, so a malformed document
    at daemon START is refused at command entry with exit 2. What is left
    is the case ``check_open_pr_bound`` was written for, and it is
    modelled here at the seam where it really happens: the document is
    rewritten between ``serve``'s own ``ServeConfig.load`` and the first
    poll.

    ``[serve] max_open_prs = 0`` is documented three times as the
    supported way to switch the open-PR bound off, and it is the
    configuration that matters, because it is what turns off the one
    guarded ``[factory]`` read that would otherwise absorb the fault.
    """

    @staticmethod
    def _run(tmp_path: Path, document: str) -> tuple[object, Queue]:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(CLEAN_TOML, encoding="utf-8")
        queue = Queue(tmp_path, QueueConfig())
        queue.add("# Spec\n\nDo the thing.\n", merge_disposition=MergeDisposition.STOP_AT_PR)

        from kstrl import serve as serve_module

        real = serve_module._load_pr_count_streak

        def edit_then_load(root_dir: Path, observer: Any) -> Any:
            toml.write_text(document, encoding="utf-8")
            return real(root_dir, observer)

        with patch("kstrl.serve._load_pr_count_streak", edit_then_load):
            with patch(
                "kstrl.serve.count_open_kstrl_prs",
                lambda cwd, limit=100: OpenPrCount(count=0, saturated=False),
            ):
                results = serve(tmp_path, runner=_stub_run, once=True)
        return results[0], queue

    @pytest.mark.parametrize("document,section", MALFORMED_SECTIONS)
    def test_the_cycle_completes_and_the_item_waits(
        self, tmp_path: Path, document: str, section: str
    ) -> None:
        result, queue = self._run(tmp_path, document)
        assert section in result.skipped, (
            "the cycle's own message must name the section an operator has "
            f"to fix. Got: {result.skipped!r}"
        )
        assert result.ran_item == ""
        states = [item.state for item in queue.items()]
        assert states == [ItemState.QUEUED], (
            "an unreadable config is an operator typo that clears when the "
            "file is fixed, so the item WAITS. Poisoning it is terminal and "
            f"would cost one item per poll. Got: {states}"
        )

    def test_a_clean_document_still_runs_the_item(self, tmp_path: Path) -> None:
        """The control: the seam itself does not stop anything."""
        result, queue = self._run(tmp_path, CLEAN_TOML)
        assert result.ran_item != ""
        assert result.skipped == ""


class TestTheRefusingGateIsFailClosed:
    """The gate itself, not only what the cycle does with it.

    Written because a mutation stayed GREEN without it. Setting
    ``pause_before_pr_merge=False`` on the refusing gate changed no
    behavioural outcome, because ``serve_cycle`` branches on
    ``unreadable_section`` and the item never runs either way. That makes
    it an equivalent mutant TODAY and a hole tomorrow: the flag is what
    reaches the child as ``--pause-before-pr-merge``, so a gate that says
    False while refusing is one consumer away from dropping a human merge
    gate. A control that CLEARS must be narrow, so the fail-closed shape
    is asserted where it is decided rather than inferred from the fact
    that nothing currently reads it.
    """

    @staticmethod
    def _gate(tmp_path: Path, document: str) -> Any:
        from kstrl.serve import resolve_merge_gate

        (tmp_path / "kstrl.toml").write_text(document, encoding="utf-8")
        queue = Queue(tmp_path, QueueConfig())
        item = queue.add("# Spec\n\nDo the thing.\n", merge_disposition=MergeDisposition.STOP_AT_PR)
        return resolve_merge_gate(item, tmp_path)

    @pytest.mark.parametrize("document,section", MERGE_GATE_SECTIONS)
    def test_an_unreadable_section_resolves_to_a_gate_that_is_on(
        self, tmp_path: Path, document: str, section: str
    ) -> None:
        gate = self._gate(tmp_path, document)
        assert gate.pause_before_pr_merge is True, (
            "a config read that could not complete must fail CLOSED. This "
            "flag is what reaches the child as --pause-before-pr-merge."
        )
        assert gate.unreadable_section == section.strip("[]")
        assert section in gate.refusal

    def test_a_clean_document_resolves_to_no_unreadable_section(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path, CLEAN_TOML)
        assert gate.unreadable_section == ""
        assert gate.refusal == ""
