"""An inbox item closes when its component COMPLETES, whatever requeued it (#438).

Two commands requeue a failed component and only one of them closed the
item: ``ks inbox retry <item>`` resolves the item it was given, and
``ks retry <component>`` never touched the inbox. A retry that completed
the component therefore left ``ks inbox ls`` listing a halted_run for a
component ``ks status`` reported merged. The fix resolves on the fact:
every place a component becomes COMPLETED resolves every undecided item
that names it, with a comment naming the run that completed it.

Two layers, each with a different reason to fail.

The BEHAVIOUR layer. The end-to-end tests drive the real
``run_factory`` twice over a saved manifest (the engineer, verification
and review stubbed), with the real ``prepare_retry`` or the real
``ks inbox retry`` CLI in between, and read the inbox back through the
real ``ks inbox ls``. The repoll test drives
``ComponentPipeline.repoll_merge_pending``, the second completion site,
where a parked merge is confirmed.

The CENSUS layer counts every expression in ``kstrl/`` that spells the
COMPLETED status, as the identifier ``COMPLETED`` or the string
``"completed"``, and that is not only READ: an operand of a comparison,
an element of a literal collection that is one, and the enum member
under a ``.value`` on either are cleared, and everything else is
counted per ``module::scope``. It enumerates no write shapes, so a new
completion written as ``setattr``, a keyword argument, a dict entry or
``dataclasses.replace`` moves the count the same way an assignment
does. Every row is classified, either as a completion site, which must
call the resolver, or as another vocabulary with a reason. What it does
not see is disclosed at the bottom and pinned with a strict xfail: a
status copied from somewhere that already holds it.
"""

from __future__ import annotations

import ast
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, FactoryResult, run_factory
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind, ItemStatus
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.pr import MergeConfirmation
from kstrl.retry_plan import prepare_retry
from kstrl.review import ReviewResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.helpers import astwalk, gitrepo
from tests.helpers.component_prd import write_component_prd
from tests.test_pipeline import _factory_config, _make_pipeline

COMP = "comp-a"
FAILING_RUN = "run-438-fail"
RETRY_RUN = "run-438-retry"


# --- the end-to-end harness -------------------------------------------------


def _project(root: Path) -> Path:
    """A git repo holding one component, its PRD and a saved manifest."""
    gitrepo.git_in(root, "init")
    gitrepo.git_in(root, "symbolic-ref", "HEAD", "refs/heads/main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    gitrepo.git_in(root, "add", ".")
    gitrepo.git_in(root, "commit", "-m", "base")
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("p", encoding="utf-8")
    prd = f"scripts/kstrl/feature/{COMP}/prd.json"
    write_component_prd(root, prd)
    (root / "kstrl.toml").write_text(
        "[autonomy]\nenabled = false\n[inbox]\nenabled = true\n", encoding="utf-8"
    )
    manifest_path = kstrl_dir / "manifest.json"
    Manifest(
        version="1",
        spec_file="s",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[Component(COMP, "A", "D", [], prd, f"kstrl/factory/{COMP}")],
    ).save(manifest_path)
    return manifest_path


def _run(
    root: Path,
    manifest_path: Path,
    *,
    passed: bool,
    run_id: str,
    out: io.StringIO | None = None,
) -> FactoryResult:
    """One real ``run_factory`` over the saved manifest.

    Verification passes or fails as asked; nothing else about the run is
    stubbed beyond what the other inbox emitter tests stub.
    """
    kstrl_dir = root / "scripts" / "kstrl"
    config = FactoryConfig(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    verification = VerificationResult(
        passed=passed,
        checks=[CheckResult("test_suite", passed, "ok" if passed else "boom")],
    )
    with (
        patch(
            "kstrl.factory._run_component",
            return_value=ComponentResult(COMP, success=True, iterations=1),
        ),
        patch("kstrl.factory.run_mechanical_verification", return_value=verification),
        patch("kstrl.factory.run_review", return_value=ReviewResult(passed=True, mode="hard")),
        patch("kstrl.pr.is_gh_available", return_value=False),
    ):
        return run_factory(
            Manifest.load(manifest_path),
            config,
            base,
            PlainUI(no_color=True, file=out) if out is not None else PlainUI(no_color=True),
            root,
            manifest_path=manifest_path,
            run_id=run_id,
        )


def _halted(root: Path, manifest_path: Path) -> InboxItem:
    """Run 1: the component fails in verify and opens one halted_run."""
    _run(root, manifest_path, passed=False, run_id=FAILING_RUN)
    items = Inbox(root, InboxConfig()).open_items()
    assert [(i.kind, i.component) for i in items] == [(ItemKind.HALTED_RUN, COMP)]
    return items[0]


def _ks_retry(root: Path, manifest_path: Path, *, out: io.StringIO | None = None) -> None:
    """What ``ks retry <component>`` does: ``prepare_retry``, then the factory.

    The two calls, not the CLI command, because the command's config
    assembly is not this issue's subject and the fix does not live there.
    """
    prepare_retry(Manifest.load(manifest_path), COMP, manifest_path, root, PlainUI(no_color=True))
    _run(root, manifest_path, passed=True, run_id=RETRY_RUN, out=out)


def _ks_inbox_retry(root: Path, manifest_path: Path, item: InboxItem) -> None:
    """``ks inbox retry <item>`` through the real CLI, then the factory."""
    result = CliRunner().invoke(cli, ["inbox", "retry", item.id, "--root", str(root)])
    assert result.exit_code == 0, result.output
    _run(root, manifest_path, passed=True, run_id=RETRY_RUN)


def _completed(manifest_path: Path) -> bool:
    comp = Manifest.load(manifest_path).get_component(COMP)
    return comp is not None and comp.status == ComponentStatus.COMPLETED.value


def _halted_with_others(root: Path, manifest_path: Path, *, keep: set[str]) -> InboxItem:
    """Run 1 with two unrelated items already open; returns the new halted_run."""
    _run(root, manifest_path, passed=False, run_id=FAILING_RUN)
    fresh = [i for i in Inbox(root, InboxConfig()).open_items() if i.id not in keep]
    assert [(i.kind, i.component) for i in fresh] == [(ItemKind.HALTED_RUN, COMP)]
    return fresh[0]


def _status(root: Path, item_id: str) -> ItemStatus:
    item = Inbox(root, InboxConfig()).get(item_id)
    assert item is not None, item_id
    return item.status


# --- layer 1: the behaviour -------------------------------------------------


class TestACompletedComponentClosesItsItems:
    def test_ks_retry_resolves_the_item_and_names_the_run(self, tmp_path: Path) -> None:
        """The issue's acceptance, red first: ``ks inbox ls`` agrees with the manifest."""
        manifest_path = _project(tmp_path)
        item = _halted(tmp_path, manifest_path)
        _ks_retry(tmp_path, manifest_path)
        assert _completed(manifest_path)
        decided = Inbox(tmp_path, InboxConfig()).get(item.id)
        assert decided is not None
        assert decided.status is ItemStatus.RESOLVED, decided.status
        assert decided.decided_by == "system"
        assert RETRY_RUN in decided.decision_comment, decided.decision_comment
        listed = CliRunner().invoke(cli, ["inbox", "ls", "--root", str(tmp_path)])
        assert listed.exit_code == 0, listed.output
        assert item.id[:8] not in listed.output, listed.output
        assert "Inbox clear" in listed.output, listed.output

    @pytest.mark.parametrize("command", ["ks retry", "ks inbox retry"])
    def test_both_retry_commands_end_in_the_same_state(self, tmp_path: Path, command: str) -> None:
        """The failure summary's ``retry with: ks retry <id>`` and the inbox's
        own ``retry`` leave the component completed and its item resolved."""
        manifest_path = _project(tmp_path)
        item = _halted(tmp_path, manifest_path)
        if command == "ks retry":
            _ks_retry(tmp_path, manifest_path)
        else:
            _ks_inbox_retry(tmp_path, manifest_path, item)
        assert _completed(manifest_path)
        box = Inbox(tmp_path, InboxConfig())
        decided = box.get(item.id)
        assert decided is not None and decided.status is ItemStatus.RESOLVED
        assert [i.id for i in box.open_items() if i.component == COMP] == []

    def test_a_snoozed_item_is_resolved_too(self, tmp_path: Path) -> None:
        """A snooze returns when its TTL lapses; it must not return to ask
        about a component that has since completed."""
        manifest_path = _project(tmp_path)
        item = _halted(tmp_path, manifest_path)
        Inbox(tmp_path, InboxConfig()).snooze(item.id, actor="operator", hours=24)
        _ks_retry(tmp_path, manifest_path)
        decided = Inbox(tmp_path, InboxConfig()).get(item.id)
        assert decided is not None
        assert decided.status is ItemStatus.RESOLVED, decided.status

    def test_items_for_anything_else_stay_open(self, tmp_path: Path) -> None:
        """Only items naming the completed component: another component's
        item and a run-level item carry no component match and stay open."""
        manifest_path = _project(tmp_path)
        box = Inbox(tmp_path, InboxConfig())
        other = box.add(ItemKind.HALTED_RUN, "comp-b halted", component="comp-b")
        run_level = box.add(ItemKind.DEMOTION_NOTICE, "Autonomy demoted L2 -> L1")
        item = _halted_with_others(tmp_path, manifest_path, keep={other.id, run_level.id})
        _ks_retry(tmp_path, manifest_path)
        assert _status(tmp_path, item.id) is ItemStatus.RESOLVED
        assert _status(tmp_path, other.id) is ItemStatus.OPEN
        assert _status(tmp_path, run_level.id) is ItemStatus.OPEN

    def test_a_disabled_inbox_is_left_alone(self, tmp_path: Path) -> None:
        """An operator who turned the inbox off gets no writes to it: the
        component completes and the item it opened earlier is untouched."""
        manifest_path = _project(tmp_path)
        item = _halted(tmp_path, manifest_path)
        (tmp_path / "kstrl.toml").write_text(
            "[autonomy]\nenabled = false\n[inbox]\nenabled = false\n", encoding="utf-8"
        )
        _ks_retry(tmp_path, manifest_path)
        assert _completed(manifest_path)
        assert _status(tmp_path, item.id) is ItemStatus.OPEN

    def test_a_failed_resolve_warns_and_the_run_still_completes(self, tmp_path: Path) -> None:
        """Decided: never fatal, never silent. The component's work is done
        and saved, so the run completes; the item stays open, which is the
        state the operator already saw; the run's output names the
        component and the error."""
        manifest_path = _project(tmp_path)
        item = _halted(tmp_path, manifest_path)
        out = io.StringIO()
        with patch("kstrl.pipeline.Inbox.resolve", side_effect=OSError("disk full")):
            _ks_retry(tmp_path, manifest_path, out=out)
        assert _completed(manifest_path)
        still = Inbox(tmp_path, InboxConfig()).get(item.id)
        assert still is not None and still.status is ItemStatus.OPEN
        text = out.getvalue()
        assert f"WARN:   Inbox resolve for {COMP} failed" in text, text
        assert "disk full" in text, text

    def test_every_kind_is_resolved_and_a_decision_is_kept(self, tmp_path: Path) -> None:
        """Every undecided item naming the component is resolved, whatever
        its kind; an item a human already approved or rejected keeps that
        decision and its author."""
        manifest_path = _project(tmp_path)
        box = Inbox(tmp_path, InboxConfig())
        budget = box.add(ItemKind.BUDGET_OVERRUN, "comp-a over budget", component=COMP)
        approved = box.add(ItemKind.MERGE_GATE, "comp-a merge", component=COMP)
        box.approve(approved.id, actor="operator")
        rejected = box.add(ItemKind.POLICY_EXCEPTION, "comp-a policy", component=COMP)
        box.reject(rejected.id, actor="operator", comment="no")
        keep = {budget.id, approved.id, rejected.id}
        item = _halted_with_others(tmp_path, manifest_path, keep=keep)
        _ks_retry(tmp_path, manifest_path)
        assert _status(tmp_path, item.id) is ItemStatus.RESOLVED
        assert _status(tmp_path, budget.id) is ItemStatus.RESOLVED
        after = Inbox(tmp_path, InboxConfig())
        for item_id, status in (
            (approved.id, ItemStatus.APPROVED),
            (rejected.id, ItemStatus.REJECTED),
        ):
            decided = after.get(item_id)
            assert decided is not None
            assert (decided.status, decided.decided_by) == (status, "operator"), decided

    def test_a_completion_the_manifest_did_not_save_resolves_nothing(self, tmp_path: Path) -> None:
        """The resolve runs after the save: when saving the COMPLETED
        status fails, the item stays open, because nothing on disk says
        the component completed."""
        manifest_path = _project(tmp_path)
        item = _halted(tmp_path, manifest_path)
        real_save = Manifest.save

        def save_fails_on_completion(self: Manifest, path: Path) -> None:
            if any(
                c.id == COMP and c.status == ComponentStatus.COMPLETED.value
                for c in self.components
            ):
                raise OSError("disk full on save")
            real_save(self, path)

        with (
            patch.object(Manifest, "save", save_fails_on_completion),
            contextlib.suppress(OSError),
        ):
            _ks_retry(tmp_path, manifest_path)
        assert not _completed(manifest_path)
        assert _status(tmp_path, item.id) is ItemStatus.OPEN


class TestAConfirmedMergeClosesEveryItemForTheComponent:
    def test_repoll_resolves_the_gate_and_an_earlier_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second completion site: a parked merge confirmed on re-poll."""
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.wait_for_merge", lambda *a, **k: MergeConfirmation(state="merged")
        )
        monkeypatch.setattr("kstrl.git.fetch_base_branch", lambda *a, **k: None)
        pipeline, manifest, _result, _calls = _make_pipeline(
            tmp_path, config=_factory_config(create_prs=True)
        )
        comp = manifest.get_component(COMP)
        assert comp is not None
        comp.status = ComponentStatus.MERGE_PENDING.value
        comp.pr_number = 7
        comp.pr_url = "https://x/pull/7"
        box = Inbox(tmp_path, InboxConfig())
        gate = box.add(
            ItemKind.MERGE_GATE, "merge unconfirmed", component=COMP, dedupe_key=f"merge:{COMP}"
        )
        halt = box.add(ItemKind.HALTED_RUN, "halted earlier", component=COMP)
        pipeline.repoll_merge_pending()
        assert comp.status == ComponentStatus.COMPLETED.value
        for item_id in (gate.id, halt.id):
            decided = box.get(item_id)
            assert decided is not None
            assert decided.status is ItemStatus.RESOLVED, (item_id, decided.status)
            # _make_pipeline's run id, and the merge that completed it.
            assert "run-test" in decided.decision_comment, decided.decision_comment
            assert "PR #7 merged" in decided.decision_comment, decided.decision_comment


class TestAnOperatorDecisionSurvivesTheCompletionRace:
    """#438 B1: the pipeline's ``undecided`` list is a snapshot, the write is not.

    Before the fix, ``Inbox.resolve`` (via ``_decide``) re-read the item
    with ``get()`` outside any lock and then unconditionally overwrote its
    status. An operator's ``approve``/``reject``/``snooze`` landing after
    ``_inbox_resolve_component``'s ``items()`` selection and before that
    write was silently lost - the lane's ``race.py`` demonstrated it
    ending ``resolved by system``. The fix moves the precondition to the
    writer: ``resolve(..., only_from=UNDECIDED)`` re-folds the FRESH row
    and the write itself and its check inside one ``control_lock`` hold,
    so a decision that has already landed by then survives.

    The race is injected at ``Inbox.get`` - the fresh fold ``_decide``'s
    ``only_from`` branch performs - rather than by spinning up a second
    ``Inbox`` and calling ``approve()`` on it: that fold runs inside the
    lock ``resolve()`` already holds, and ``flock`` is not reentrant
    across a second open of the same lock file even in one process (a
    second acquire would self-deadlock). Writing the operator's decision
    with ``_append_unlocked`` instead is the same effect on the log a
    genuinely concurrent writer would have produced by the time this
    process's lock-protected fold runs.
    """

    def test_an_approve_landing_between_selection_and_write_survives(self, tmp_path: Path) -> None:
        pipeline, _manifest, _result, _calls = _make_pipeline(tmp_path)
        box = Inbox(tmp_path, InboxConfig())
        gate = box.add(ItemKind.MERGE_GATE, "merge unconfirmed", component=COMP)
        real_get = Inbox.get
        landed = False

        def get_with_operator_race(self: Inbox, item_id: str) -> InboxItem | None:
            nonlocal landed
            if item_id == gate.id and not landed:
                landed = True
                operator_copy = real_get(self, item_id)
                assert operator_copy is not None
                operator_copy.status = ItemStatus.APPROVED
                operator_copy.decided_by = "operator"
                operator_copy.decision_comment = "ship it"
                self._append_unlocked(operator_copy)
            return real_get(self, item_id)

        with patch.object(Inbox, "get", get_with_operator_race):
            pipeline._inbox_resolve_component(COMP)

        final = Inbox(tmp_path, InboxConfig()).get(gate.id)
        assert final is not None
        assert (final.status, final.decided_by, final.decision_comment) == (
            ItemStatus.APPROVED,
            "operator",
            "ship it",
        ), final


# --- layer 2: the census ----------------------------------------------------

#: The identifier and the value of the COMPLETED status. The identifier
#: also names ``Transition.COMPLETED`` in the pipeline, which is why the
#: pipeline's own rows below count it.
_IDENT = astwalk.spells("COMPLETED")
_VALUE = astwalk.folds_to("completed")

#: The method every completion site must call.
RESOLVER = "_inbox_resolve_component"

#: One control per disjunct of the net, and one for the read it clears.
CONTROL_IDENT = "comp.status = ComponentStatus.COMPLETED.value\n"
CONTROL_VALUE = "setattr(comp, 'status', 'completed')\n"
CONTROL_READ = (
    "if comp.status == ComponentStatus.COMPLETED.value or comp.status in {'completed'}:\n    pass\n"
)


def _compared(operand: ast.expr) -> set[int]:
    """One comparison operand, the elements of a literal collection that is
    one, and the enum member under a ``.value`` on any of them."""
    parts = operand.elts if isinstance(operand, ast.Set | ast.Tuple | ast.List) else [operand]
    found = {id(part) for part in parts}
    found.update(
        id(part.value) for part in parts if isinstance(part, ast.Attribute) and part.attr == "value"
    )
    return found


def _read_only(tree: ast.AST) -> set[int]:
    """Nodes that are only compared against, by ``id``.

    CLEARING, so narrow: see :func:`_compared`. A comparison cannot
    assign, so each is provably a read. Anything else that spells the
    status is counted, including a collection built first and compared
    later, which over-matches in the safe direction and is one row of
    ``NOT_A_COMPLETION``.
    """
    found: set[int] = set()
    for node in astwalk.all_nodes(tree):
        if isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                found |= _compared(operand)
    return found


def status_spellings(tree: ast.AST) -> list[ast.AST]:
    """Every node in one tree that spells COMPLETED and is not only read."""
    reads = _read_only(tree)
    return [
        node
        for node in astwalk.all_nodes(tree)
        if (_IDENT(node) or _VALUE(node)) and id(node) not in reads
    ]


def completion_census() -> dict[str, int]:
    """``module::scope`` -> how many spellings that scope holds."""
    rows: dict[str, int] = {}
    for source in astwalk.package_sources():
        tree = astwalk.parsed(source)
        owner = astwalk.scope_of(tree)
        for node in status_spellings(tree):
            key = f"{astwalk.label(source)}::{owner[id(node)]}"
            rows[key] = rows.get(key, 0) + 1
    return rows


#: Every scope in ``kstrl/`` that spells the COMPLETED status other than
#: by comparing against it, and how many times. A new row, or a count
#: that moved, is a place the status may now be written: classify it
#: below before adding it here.
EXPECTED_STATUS_SPELLINGS: dict[str, int] = {
    "autonomy_replay.py::load_runs": 1,
    "cli.py::evolve": 1,
    "context.py::IterationContext.format_for_prompt": 1,
    "evolution.py::<module>": 1,
    "manifest.py::<module>": 2,
    "manifest.py::Manifest.reset_for_retry": 1,
    "observability.py::ProgressLog.factory_completed": 1,
    "pipeline.py::<module>": 2,
    "pipeline.py::ComponentPipeline.complete": 2,
    "pipeline.py::ComponentPipeline.repoll_merge_pending": 1,
    "pr.py::create_single_pr": 1,
    "reducer.py::apply": 1,
    "tui/screens/evolve.py::EvolveScreen._trend_cells": 1,
    "tui/theme.py::<module>": 1,
    "workqueue.py::Queue.finish_ok": 1,
}

#: The rows above where a manifest component's status becomes COMPLETED.
#: Each must call ``self._inbox_resolve_component``.
COMPLETION_SITES = frozenset(
    {
        # comp.status = COMPLETED, and the Transition.COMPLETED it returns.
        "pipeline.py::ComponentPipeline.complete",
        # A parked merge confirmed on re-poll.
        "pipeline.py::ComponentPipeline.repoll_merge_pending",
    }
)

#: Every other row, and why it is not a component becoming COMPLETED.
NOT_A_COMPLETION: dict[str, str] = {
    "autonomy_replay.py::load_runs": "reads the 'completed' count column of a trend row",
    "cli.py::evolve": "prints the 'completed' count column of a trend row",
    "context.py::IterationContext.format_for_prompt": "a label in the engineer's retry context",
    "evolution.py::<module>": "the name of the 'completed' trend column",
    "manifest.py::<module>": "the ComponentStatus.COMPLETED declaration, name and value",
    "manifest.py::Manifest.reset_for_retry": "builds the set a dependency's status is compared to",
    "observability.py::ProgressLog.factory_completed": "the count key of a progress event",
    "pipeline.py::<module>": "the Transition.COMPLETED declaration, name and value",
    "pr.py::create_single_pr": "a status label in the single-PR body",
    "reducer.py::apply": "the TUI's view of a ComponentCompleted event, not the manifest",
    "tui/screens/evolve.py::EvolveScreen._trend_cells": "the 'completed' trend column",
    "tui/theme.py::<module>": "the TUI glyph for the status",
    "workqueue.py::Queue.finish_ok": "a work-queue item's finish reason",
}


def _scope(row: str) -> ast.AST:
    module, _, qualified = row.partition("::")
    for source in astwalk.package_sources():
        if astwalk.label(source) != module:
            continue
        for node, name in astwalk.scopes(astwalk.parsed(source)):
            if name == qualified:
                return node
    raise AssertionError(f"no scope {row!r} in kstrl/")


def calls_the_resolver(scope: ast.AST) -> bool:
    """``self._inbox_resolve_component(...)`` in this scope's own body.

    CLEARING, so narrow: the attribute on ``self`` by exact name, and a
    call inside a nested function does not count.
    """
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == RESOLVER
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        for node in astwalk.own_nodes(scope)
    )


class TestEveryCompletionSiteResolves:
    @pytest.mark.parametrize("control", [CONTROL_IDENT, CONTROL_VALUE])
    def test_the_net_fires(self, control: str) -> None:
        """One control per disjunct: a census that matches is also what a
        switched-off net returns."""
        assert status_spellings(astwalk.parse(control)), control

    def test_a_comparison_is_cleared(self) -> None:
        """The clearing half's own control: both spellings, compared, count zero."""
        assert status_spellings(astwalk.parse(CONTROL_READ)) == []

    def test_the_census_is_pinned(self) -> None:
        found = completion_census()
        assert found == EXPECTED_STATUS_SPELLINGS, (
            "kstrl/ spells the COMPLETED status somewhere new, or a count "
            "moved. If a component's status can now become COMPLETED there, "
            f"add it to COMPLETION_SITES and call {RESOLVER}; otherwise add "
            f"it to NOT_A_COMPLETION with the reason. Found: {found}"
        )

    def test_every_row_is_classified_once(self) -> None:
        assert not COMPLETION_SITES & set(NOT_A_COMPLETION)
        assert COMPLETION_SITES | set(NOT_A_COMPLETION) == set(EXPECTED_STATUS_SPELLINGS)

    @pytest.mark.parametrize("row", sorted(COMPLETION_SITES))
    def test_each_completion_site_calls_the_resolver(self, row: str) -> None:
        assert calls_the_resolver(_scope(row)), (
            f"{row} makes a component COMPLETED without calling "
            f"self.{RESOLVER}, so its open inbox items outlive it (#438)."
        )

    def test_the_resolver_check_fires_both_ways(self) -> None:
        with_call = astwalk.parse(f"def f(self):\n    self.{RESOLVER}(comp.id)\n")
        without = astwalk.parse("def f(self):\n    self._inbox_resolve('merge:x', 'r')\n")
        assert calls_the_resolver(with_call.body[0])
        assert not calls_the_resolver(without.body[0])

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_status_copied_from_another_holder_is_not_seen(self) -> None:
        """Disclosed: the net finds a SPELLING, and a copy has none."""
        astwalk.blind_spot(
            lambda source: status_spellings(astwalk.parse(source)),
            "comp.status = other.status\n",
        )
