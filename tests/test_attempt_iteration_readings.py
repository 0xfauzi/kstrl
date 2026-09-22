"""#233: the journal records every engineer-loop attempt's iterations,
not only the last one, and a strict reader turns that into a verdict on
#233's entry criterion.

``avg_iterations`` in ``experiments.tsv`` is the LAST attempt's count
per component (``kstrl/pipeline.py:2276`` assigns rather than
accumulates), so a run that executed 6 iterations records 3.00. The
factory already writes a per-attempt journal row at the moment an
attempt is superseded (``findings_superseded``), correctly tagged with
the attempt number, but it did not carry the reading and a guard in
front of it dropped the row entirely when the attempt produced no
``Finding``. This module tests the fix (``kstrl/pipeline.py``) and the
strict reader that turns the corrected journal into a verdict
(``kstrl/evolution.py``), plus the census guard that pins every place
an attempt boundary is produced, so a future site cannot silently skip
the journal write.

``TestAttemptSiteCensus`` is that guard, closed by construction in the
CLAUDE.md sense: it inventories every place the quantity is OBTAINED
(every write to a ``retries`` target, every ``_end_attempt`` call), so
a new shape shows up as an unexplained census delta. Both layers go
through ``tests/helpers/astwalk`` (#324) and every predicate carries a
control per disjunct, because a pinned inventory that matches is also
what a switched-off predicate returns.
"""

from __future__ import annotations

import ast
import functools
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kstrl import events as ev
from kstrl.evolution import (
    FINDINGS_SUPERSEDED_EVENT,
    IterationReading,
    iteration_criterion_verdict,
    read_attempt_iterations,
)
from kstrl.factory import FactoryConfig, run_factory
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers import gitrepo
from tests.helpers.astwalk import (
    Sees,
    assert_census,
    label,
    package_sources,
    parsed,
)
from tests.test_event_stream import (
    _component,
    _make_base_config,
    _make_manifest,
    _setup_project,
)
from tests.test_pipeline import _make_pipeline


def _journal_entries(root: Path) -> list[dict[str, Any]]:
    path = root / ".kstrl" / "evolution.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _experiments_row(root: Path) -> dict[str, str]:
    lines = (root / ".kstrl" / "experiments.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    return dict(zip(header, lines[1].split("\t"), strict=False))


@pytest.fixture(scope="module")
def two_attempt_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real factory run: 2 attempts x 3 iterations = 6, by construction.

    max_iterations=3, agent_cmd="echo working" (never emits the completion
    marker), test_command="false" (always fails), max_retries=1. So attempt 1
    runs 3 iterations and is superseded by a retry, and attempt 2 runs 3 and
    fails for exhausted retries. Returns the project root.
    """
    root = _setup_project(tmp_path_factory.mktemp("two-attempt"), ["comp-a"])
    gitrepo.git_in(root, "init", "-q")
    gitrepo.set_identity(root)
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-qm", "base")

    manifest = _make_manifest([_component("comp-a")])
    base = _make_base_config(root)
    base.agent_cmd = "echo working"
    base.max_iterations = 3
    config = FactoryConfig(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=1,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="false",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
        progress_log_path=root / "progress.jsonl",
    )
    run_factory(manifest, config, base, PlainUI(no_color=True), root)
    return root


class TestJournalRecordsEveryAttempt:
    def test_two_attempts_of_three_iterations_yield_the_ordered_series(
        self, two_attempt_run: Path
    ) -> None:
        root = two_attempt_run
        run_dir = sorted((root / ".kstrl" / "runs").iterdir())[-1]
        engineer = run_dir / "components" / "comp-a" / "engineer.jsonl"
        done = [e for e in ev.read_events(engineer) if isinstance(e, ev.IterationCompleted)]
        # ground truth, by construction: 2 attempts x 3 iterations
        assert len(done) == 6
        assert [e.iteration for e in done] == [1, 2, 3, 1, 2, 3]

        entries = _journal_entries(root)
        superseded = [e for e in entries if e["event_type"] == FINDINGS_SUPERSEDED_EVENT]
        results = [e for e in entries if e["event_type"] == "component_result"]
        series = sorted(
            [(e["attempt"], e["iteration_count"]) for e in superseded]
            + [(e["retries"] + 1, e["iteration_count"]) for e in results]
        )
        assert series == [(1, 3), (2, 3)]

    def test_avg_iterations_column_still_reads_exactly_three(self, two_attempt_run: Path) -> None:
        assert _experiments_row(two_attempt_run)["avg_iterations"] == "3.00"

    def test_an_attempt_with_no_findings_still_writes_a_row(self, two_attempt_run: Path) -> None:
        entries = _journal_entries(two_attempt_run)
        superseded = [e for e in entries if e["event_type"] == FINDINGS_SUPERSEDED_EVENT]
        assert len(superseded) == 1
        assert superseded[0]["attempt"] == 1
        assert superseded[0]["findings"] == []
        assert superseded[0]["iteration_count"] == 3


class TestStrictReader:
    def test_a_clean_two_attempt_journal_measures_six(self, two_attempt_run: Path) -> None:
        row = _experiments_row(two_attempt_run)
        reading = read_attempt_iterations(
            _journal_entries(two_attempt_run), row["run_id"], int(row["components_total"])
        )
        assert reading.measured is True
        assert reading.iterations_total == 6
        assert reading.attempts_total == 2
        assert reading.avg_all_attempts == 6.00

    def test_a_missing_attempt_reading_is_refused(self) -> None:
        entries = [
            {
                "event_type": "component_result",
                "run_id": "r1",
                "component_id": "comp-a",
                "retries": 1,
                "iteration_count": 1,
            }
        ]
        reading = read_attempt_iterations(entries, "r1", 1)
        assert reading.measured is False
        assert "attempts [1] have no reading (expected 1..2)" in reading.reason

    def test_an_entry_without_an_iteration_count_is_refused(self) -> None:
        entries = [
            {
                "event_type": "component_result",
                "run_id": "r1",
                "component_id": "comp-a",
                "retries": 1,
                "iteration_count": 3,
            },
            {
                "event_type": FINDINGS_SUPERSEDED_EVENT,
                "run_id": "r1",
                "component_id": "comp-a",
                "attempt": 1,
            },
        ]
        reading = read_attempt_iterations(entries, "r1", 1)
        assert reading.measured is False
        assert "attempt 1 carries no iteration_count" in reading.reason

    def test_a_component_count_mismatch_is_refused(self) -> None:
        entries = [
            {
                "event_type": "component_result",
                "run_id": "r1",
                "component_id": "comp-a",
                "retries": 0,
                "iteration_count": 1,
            },
            {
                "event_type": "component_result",
                "run_id": "r1",
                "component_id": "comp-b",
                "retries": 0,
                "iteration_count": 1,
            },
        ]
        reading = read_attempt_iterations(entries, "r1", 3)
        assert reading.measured is False
        assert "2 component_result entries for 3 component(s) in the run" in reading.reason

    def test_a_duplicate_attempt_is_refused(self) -> None:
        entries = [
            {
                "event_type": "component_result",
                "run_id": "r1",
                "component_id": "comp-a",
                "retries": 2,
                "iteration_count": 1,
            },
            {
                "event_type": FINDINGS_SUPERSEDED_EVENT,
                "run_id": "r1",
                "component_id": "comp-a",
                "attempt": 1,
                "iteration_count": 1,
            },
            {
                "event_type": FINDINGS_SUPERSEDED_EVENT,
                "run_id": "r1",
                "component_id": "comp-a",
                "attempt": 1,
                "iteration_count": 1,
            },
        ]
        reading = read_attempt_iterations(entries, "r1", 1)
        assert reading.measured is False
        assert "attempt 1 recorded twice" in reading.reason

    def test_a_second_run_in_the_same_journal_does_not_pollute_the_reading(self) -> None:
        """``.kstrl/evolution.jsonl`` is append-only across runs, so a second
        run of the same component leaves the first run's rows on disk. The
        two lines in ``read_attempt_iterations`` that skip an entry whose
        ``run_id`` does not match the run under read are what keep the
        second run's reading from seeing the first run's rows too; without
        them the reader would see attempt 1 recorded twice for every run
        after the first and refuse every one of them.
        """
        entries = []
        for run in ("r1", "r2"):
            entries.append(
                {
                    "event_type": FINDINGS_SUPERSEDED_EVENT,
                    "run_id": run,
                    "component_id": "comp-a",
                    "attempt": 1,
                    "iteration_count": 3,
                }
            )
            entries.append(
                {
                    "event_type": "component_result",
                    "run_id": run,
                    "component_id": "comp-a",
                    "retries": 1,
                    "iteration_count": 3,
                }
            )
        for run in ("r1", "r2"):
            reading = read_attempt_iterations(entries, run, 1)
            assert reading.measured is True
            assert reading.iterations_total == 6
            assert reading.attempts_total == 2

    def test_two_components_in_one_run_do_not_share_superseded_rows(self) -> None:
        """The ``findings_superseded`` rows are grouped by ``component_id``
        before ``_component_attempt_readings`` sees them, which is what
        keeps one component's superseded rows out of another's attempt
        set. Every existing test above exercises exactly one component per
        run, so none of them observes the grouping: with a single
        component there is only one component's rows to hand out, grouped
        or not. Without the grouping, every multi-component run with a
        retry reads both components' ``attempt: 1`` rows into the SAME
        attempt set, ``attempt 1`` is then seen twice, and the run is
        refused as "attempt 1 recorded twice" even though each component
        retried exactly once. This test is the minimal shape (two
        components, one retry each) that would fail that way if the
        grouping were removed.
        """
        entries = []
        for cid in ("comp-a", "comp-b"):
            entries.append(
                {
                    "event_type": FINDINGS_SUPERSEDED_EVENT,
                    "run_id": "r1",
                    "component_id": cid,
                    "attempt": 1,
                    "iteration_count": 3,
                }
            )
            entries.append(
                {
                    "event_type": "component_result",
                    "run_id": "r1",
                    "component_id": cid,
                    "retries": 1,
                    "iteration_count": 3,
                }
            )
        reading = read_attempt_iterations(entries, "r1", 2)
        assert reading.measured is True
        assert reading.iterations_total == 12
        assert reading.attempts_total == 4
        assert reading.components_ran == 2

    def test_a_boolean_iteration_count_is_refused(self) -> None:
        entries = [
            {
                "event_type": "component_result",
                "run_id": "r1",
                "component_id": "comp-a",
                "retries": 0,
                "iteration_count": True,
            },
        ]
        reading = read_attempt_iterations(entries, "r1", 1)
        assert reading.measured is False
        assert "iteration_count is not a non-negative integer" in reading.reason

    def test_a_run_that_inherited_a_retry_counter_is_refused_not_guessed(self) -> None:
        """A DISCLOSED limitation, pinned so nobody later "fixes" it into a
        silent pass. ``retries`` is a manifest-lifetime counter, not a
        per-run one. ``ks retry`` zeroes it through
        ``Manifest.reset_for_retry`` (``kstrl/manifest.py:687``), but a
        second ``ks factory`` over a manifest whose component is PENDING
        with ``retries=1`` records ``retries=2`` while that run's journal
        holds only that run's superseded rows. The reader must REFUSE, not
        average what it can see.
        """
        entries = [
            {
                "event_type": "component_result",
                "run_id": "r1",
                "component_id": "comp-a",
                "retries": 2,
                "iteration_count": 3,
            },
            {
                "event_type": FINDINGS_SUPERSEDED_EVENT,
                "run_id": "r1",
                "component_id": "comp-a",
                "attempt": 2,
                "iteration_count": 3,
            },
        ]
        reading = read_attempt_iterations(entries, "r1", 1)
        assert reading.measured is False
        assert "attempts [1] have no reading (expected 1..3)" in reading.reason


class TestVerdict:
    def test_a_refused_reading_never_yields_a_met_or_not_met_clause_one(self) -> None:
        rows: list[dict[str, Any]] = [{"project": "a"}, {"project": "a"}, {"project": "a"}]
        readings = [
            IterationReading("r1", 6, 2, 1, 6.0, True, "measured"),
            IterationReading("r2", 6, 2, 1, 6.0, True, "measured"),
            IterationReading("r3", 0, 0, 0, 0.0, False, "refused"),
        ]
        verdict = iteration_criterion_verdict(rows, readings)
        assert verdict.clause1 == "REFUSED"
        assert "2 of 3" in verdict.clause1_detail

    def test_clause_one_needs_a_strict_majority(self) -> None:
        rows: list[dict[str, Any]] = [{"project": "a"}] * 4
        readings = [
            IterationReading("r1", 6, 2, 1, 6.0, True, "measured"),
            IterationReading("r2", 6, 2, 1, 6.0, True, "measured"),
            IterationReading("r3", 1, 1, 1, 1.0, True, "measured"),
            IterationReading("r4", 1, 1, 1, 1.0, True, "measured"),
        ]
        verdict = iteration_criterion_verdict(rows, readings)
        assert verdict.clause1 == "NOT MET"
        assert "a majority needs 3" in verdict.clause1_detail

    def test_clause_two_is_met_when_the_ledger_holds_two_projects(self) -> None:
        rows: list[dict[str, Any]] = [{"project": "a"}, {"project": "a"}, {"project": "b"}]
        verdict = iteration_criterion_verdict(rows, [])
        assert verdict.clause2 == "MET"
        assert "a" in verdict.clause2_detail
        assert "b" in verdict.clause2_detail

    def test_clause_two_refuses_rather_than_failing_on_a_single_project_ledger(self) -> None:
        """One ledger cannot disprove a clause about several projects. Two
        distinct values in one ledger PROVE it met; fewer prove nothing,
        and printing NOT MET there would assert a failure the surface
        cannot see. Same rule as clause 1's refusal, one clause over."""
        rows: list[dict[str, Any]] = [{"project": "a"}, {"project": "a"}]
        verdict = iteration_criterion_verdict(rows, [])
        assert verdict.clause2 == "REFUSED"
        assert "MET" not in verdict.clause2.replace("REFUSED", "")
        assert "reads one project root" in verdict.clause2_detail


# ---------------------------------------------------------------------------
# TestAttemptSiteCensus: the guard, closed by construction over every place
# an attempt boundary is produced (#233; the mechanism CLAUDE.md's "static
# guard" and "gate that keys on parsed output" rules describe).
# ---------------------------------------------------------------------------


@functools.cache
def _function_spans(source_file: Path) -> tuple[tuple[int, int, str], ...]:
    """(start, end, name) for every function in one module, memoised."""
    return tuple(
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(parsed(source_file))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def _enclosing_function(
    tree: ast.Module, line: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The innermost function or async function whose span contains ``line``."""
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end and (best is None or node.lineno > best.lineno):
            best = node
    return best


def owner_row(source_file: Path, node: ast.AST) -> str:
    """``"<module>:<innermost enclosing function or '<module>'>"``.

    A function name rather than a line number, because a line number moves
    on every edit above it and a pin that moves is a pin nobody reads.
    """
    line = getattr(node, "lineno", 0)
    best: tuple[int, int, str] | None = None
    for start, end, name in _function_spans(source_file):
        if start <= line <= end and (best is None or start > best[0]):
            best = (start, end, name)
    return f"{label(source_file)}:{best[2] if best else '<module>'}"


#: Every write to a ``retries`` target in ``kstrl/``, keyed
#: "<module relative to kstrl/>:<enclosing function or '<module>'>".
#: Closed by construction over the quantity the reader joins on: the
#: expected attempt set for a component is range(1, retries + 2), so a
#: new writer of ``retries`` is a new attempt number, and a new attempt
#: number with no journal row beside it is the defect this guard exists
#: for. Four sites at a98ff21, two of them in-run, plus the fifth this
#: PR adds.
EXPECTED_RETRIES_WRITE_SITES: dict[str, str] = {
    "factory.py:_run_factory_locked": (
        "the contract-breaker retry; journal_superseded_findings(breaker) runs one line above it"
    ),
    "manifest.py:<module>": "the Component.retries field declaration",
    "manifest.py:reset_for_retry": (
        "the --reset path; zeroes the counter rather than advancing an attempt"
    ),
    "pipeline.py:retry_or_fail": (
        "the scheduled retry; journal_superseded_findings(comp) runs four lines above it"
    ),
    "evolution.py:_component_attempt_readings": (
        "a local holding the value READ off a journal row, not a write to "
        "any component's counter. It is in the census because the net is "
        "wider than the subject on purpose: a flagging guard that narrows "
        "to attribute targets goes blind on the day somebody rebinds the "
        "counter through a local (#233)"
    ),
}

#: The two of those that advance an attempt inside a run.
IN_RUN_RETRY_SITES = frozenset({"factory.py:_run_factory_locked", "pipeline.py:retry_or_fail"})

#: Every ``self._end_attempt(`` call in kstrl/, same key shape. The
#: repo's own notion of an attempt ending. It is a SECOND census, not
#: the load-bearing one, and the divergence between six endings and two
#: increments is the thing a future reader has to explain rather than
#: discover: four of these end a component for good, so no further
#: attempt number is ever produced for it.
EXPECTED_END_ATTEMPT_SITES: dict[str, str] = {
    "pipeline.py:retry_or_fail": "a retry; the reading is journalled first",
    "pipeline.py:fail": "terminal; the reading reaches component_result",
    "pipeline.py:complete": "terminal; the reading reaches component_result",
    "pipeline.py:_park_merge_pending": "terminal; the reading reaches component_result",
    "pipeline.py:_fail_pr_flow": "terminal; the reading reaches component_result",
    "pipeline.py:fail_scheduler_backstop": (
        "terminal, and the disclosed blind spot: process_result never ran "
        "for this attempt, so iteration_count is stale or 0. See "
        "TestDisclosedBlindSpots."
    ),
}


def _flattened_targets(targets: list[ast.expr]) -> list[ast.expr]:
    """Assignment targets with tuple, list and star packing opened up.

    ``retries, reason = _int_field(...)`` is an ``ast.Assign`` whose only
    target is an ``ast.Tuple``. A walk that looks at ``node.targets``
    directly does not see the name at all, which is how this PR's own
    reader would have been invisible to this guard: it goes blind in the
    skip direction on the very code it ships with (#324's class).
    """
    opened: list[ast.expr] = []
    for target in targets:
        if isinstance(target, ast.Tuple | ast.List):
            opened.extend(_flattened_targets(list(target.elts)))
        elif isinstance(target, ast.Starred):
            opened.extend(_flattened_targets([target.value]))
        else:
            opened.append(target)
    return opened


def writes_a_retries_target() -> Sees:
    """Does this ONE node bind something spelled ``retries``?"""

    def sees(node: ast.AST) -> bool:
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        else:
            return False
        return any(
            (isinstance(t, ast.Attribute) and t.attr == "retries")
            or (isinstance(t, ast.Name) and t.id == "retries")
            for t in _flattened_targets(targets)
        )

    return sees


def calls_end_attempt() -> Sees:
    return lambda node: (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_end_attempt"
    )


def _statement_blocks(node: ast.AST) -> Iterator[list[ast.stmt]]:
    for parent in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and all(isinstance(s, ast.stmt) for s in block):
                yield block


def _block_containing(func: ast.AST, node: ast.stmt) -> tuple[list[ast.stmt], int] | None:
    """The statement block holding ``node``, and its index in that block."""
    for block in _statement_blocks(func):
        for index, statement in enumerate(block):
            if statement is node:
                return block, index
    return None


def _preceding_journal_call(block: list[ast.stmt], index: int, obj: str) -> bool:
    """Does any statement before ``index`` call
    ``journal_superseded_findings(obj)``?"""
    calls = (
        call
        for statement in block[:index]
        for call in ast.walk(statement)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    )
    return any(
        call.func.attr == "journal_superseded_findings"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == obj
        for call in calls
        if isinstance(call.func, ast.Attribute)
    )


def _in_run_retry_sites(
    tree: ast.Module, source_file: Path, sees: Sees
) -> Iterator[tuple[str, ast.AugAssign]]:
    """Every in-run ``retries`` increment in this module, with its owner key."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AugAssign) and sees(node):
            key = owner_row(source_file, node)
            if key in IN_RUN_RETRY_SITES:
                yield key, node


def _assert_journal_write_precedes(tree: ast.Module, node: ast.AugAssign, key: str) -> None:
    """The load-bearing assertion for one in-run ``retries`` increment: a
    ``journal_superseded_findings`` call on the SAME object, earlier in the
    SAME statement block."""
    target = node.target
    assert isinstance(target, ast.Attribute), key
    assert isinstance(target.value, ast.Name), key
    obj = target.value.id
    func = _enclosing_function(tree, node.lineno)
    assert func is not None, key
    found = _block_containing(func, node)
    assert found is not None, key
    block, index = found
    assert _preceding_journal_call(block, index, obj), (
        f"{key}: `{obj}.retries` is incremented at line {node.lineno} "
        f"with no journal_superseded_findings({obj}) call earlier in "
        "the same block. That attempt's iteration reading is "
        "discarded and the reader will refuse the whole run (#233)."
    )


class TestAttemptSiteCensus:
    def test_every_write_to_a_retries_target_is_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=writes_a_retries_target(),
            key=owner_row,
            expected=dict.fromkeys(EXPECTED_RETRIES_WRITE_SITES, 1),
            control=(
                "comp.retries = 1\n",  # Assign, attribute target
                "comp.retries += 1\n",  # AugAssign
                "retries: int = 0\n",  # AnnAssign, name target
                "retries = 0\n",  # Assign, name target
                "retries, reason = f()\n",  # Assign, tuple-unpacked name target
            ),
            message=(
                "The set of places that bind a `retries` target changed. If this is "
                "a new attempt boundary, a journal_superseded_findings call on the "
                "same object has to precede it in the same block, or the reader "
                "refuses the whole run (#233). If it is a local holding a value "
                "read off a row, add it with a reason."
            ),
        )

    def test_every_end_attempt_site_is_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=calls_end_attempt(),
            key=owner_row,
            expected=dict.fromkeys(EXPECTED_END_ATTEMPT_SITES, 1),
            control="self._end_attempt(comp)\n",
            message=(
                "The set of places that end an attempt changed. Add the row to "
                "EXPECTED_END_ATTEMPT_SITES with a reason."
            ),
        )

    def test_every_in_run_retries_increment_is_preceded_by_a_journal_write(self) -> None:
        """Layer 2, the load-bearing one: a ``journal_superseded_findings``
        call on the SAME object, EARLIER IN THE SAME STATEMENT BLOCK, not
        merely somewhere above in the function (which
        ``_run_factory_locked`` makes hundreds of lines long).

        The trailing ``checked`` assertion is not decoration: every
        assertion above it sits inside a loop, which passes vacuously if
        the loop never runs. Counting what the walk actually reached is
        what makes this fail red instead of going blind (CLAUDE.md).
        """
        sees = writes_a_retries_target()
        checked = 0
        for source_file in package_sources():
            tree = parsed(source_file)
            for key, node in _in_run_retry_sites(tree, source_file, sees):
                _assert_journal_write_precedes(tree, node, key)
                checked += 1
        assert checked == len(IN_RUN_RETRY_SITES), (
            f"layer 2 checked {checked} in-run site(s), expected "
            f"{len(IN_RUN_RETRY_SITES)}. A walk that finds nothing passes every "
            "assertion above it, so the count is the control."
        )


class TestDisclosedBlindSpots:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Disclosed blind spot: fail_scheduler_backstop (kstrl/pipeline.py:2088) "
            "ends an attempt for which process_result never ran, so "
            "comp.iteration_count still holds the PREVIOUS attempt's value, or 0. "
            "The reading is unknowable there, not zero, and nothing records that. "
            "A fix zeroes it or marks it unmeasured; this test then XPASSes and "
            "fails loudly so the blind-spot record is retired with it."
        ),
    )
    def test_the_scheduler_backstop_does_not_record_a_stale_reading(self, tmp_path: Path) -> None:
        pipeline, manifest, _, _ = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        comp.iteration_count = 2  # attempt 1's reading
        comp.retries = 1  # the backstop is ending attempt 2
        pipeline.fail_scheduler_backstop("comp-a", 10.0)
        assert comp.iteration_count != 2
