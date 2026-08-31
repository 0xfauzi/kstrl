"""Scope is resolved once, before the engineer runs, and cannot move (#269).

#264 put kstrl's own files inside every component's effective scope, and
#268 made the component PRD agent-writable as a consequence, then
defended it by COMPARING the worktree copy against the pre-run one. That
comparison had a cost of its own, which ``kstrl.scope`` records.

The answer here is structural instead. ``scope.RunScope`` resolves every
component's scope from the pre-run tree before the first engineer call,
and both guards read that one snapshot: the in-loop guard through
``factory._run_component`` -> ``loop.run_loop``, and the Phase 1 gate
through ``pipeline.ComponentPipeline`` -> ``verify.check_diff_scope``.
Nothing downstream re-reads a PRD to answer the scope question, so
there is no widening to detect.

The tests here pin four things:

1. Both guards receive the SAME snapshot in a real ``run_factory`` run,
   captured at the two seams rather than re-derived.
2. A PRD rewritten while the agent is running reaches neither guard.
3. The run-wide ``--allowed-paths`` fallback now behaves identically at
   both. It used to apply in-loop and NOT at Phase 1, so one run could
   enforce two different allowlists.
4. ``use_worktrees=False`` is covered rather than excepted. There is no
   isolation boundary in that mode and so no pre-run copy distinct from
   the live one, which is exactly why #268's comparison could not cover
   it; a value read before the agent starts does not need one. Every
   test in ``TestBothGuardsReadOneSnapshot`` runs in that mode, so the
   coverage claim is the default here rather than a special case.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.events import CallbackSink, ComponentScopeResolved, Event, EventBus
from kstrl.factory import FactoryConfig, _record_run_scope, run_factory
from kstrl.loop import LoopResult
from kstrl.manifest import Component, Manifest
from kstrl.scope import ComponentScope, RunScope
from kstrl.ui.plain import PlainUI
from kstrl.verify import (
    CheckResult,
    VerificationResult,
    VerifyConfig,
    run_mechanical_verification,
)
from tests.test_harness_path_scope import (
    AUTHORED,
    COMPONENT_ID,
    HARNESS,
    PRD_REL,
    _component,
    _manifest,
    _setup_project,
    _write_prd,
)
from tests.test_progress_scope import _base_config, _pipeline

WIDENED = [*AUTHORED, "kstrl/"]


class _Seams:
    """What each guard was handed, captured at the two real seams."""

    def __init__(self) -> None:
        self.loop_allowed: list[list[str] | None] = []
        self.loop_harness: list[list[str] | None] = []
        self.phase1_allowed: list[list[str] | None] = []
        self.phase1_harness: list[list[str] | None] = []


def _run(
    root: Path,
    seams: _Seams,
    base: KstrlConfig | None = None,
    manifest: Manifest | None = None,
    on_loop: Any = None,
) -> Any:
    """One ``run_factory`` pass with both guard seams instrumented.

    ``kstrl.loop.run_loop`` is patched rather than ``_run_component``:
    the worker itself is what assembles the in-loop guard's arguments,
    so stubbing it out would test the test. ``on_loop`` runs inside the
    fake loop, which is where an agent's writes would land.
    """

    def fake_run_loop(*args: Any, **kwargs: Any) -> LoopResult:
        seams.loop_allowed.append(list(args[0].allowed_paths) or None)
        seams.loop_harness.append(kwargs.get("guard_ignored_paths"))
        if on_loop is not None:
            on_loop()
        return LoopResult(completed=True, iterations=1, exit_code=0, duration_seconds=0.0)

    def fake_verify(*args: Any, **kwargs: Any) -> VerificationResult:
        seams.phase1_allowed.append(args[3])
        seams.phase1_harness.append(kwargs.get("harness_paths"))
        return VerificationResult(passed=True, checks=[CheckResult("diff_scope", True, "ok")])

    with (
        patch("kstrl.loop.run_loop", side_effect=fake_run_loop),
        patch("kstrl.factory.run_mechanical_verification", side_effect=fake_verify),
        patch("kstrl.git.get_diff_content", return_value=""),
    ):
        return run_factory(
            manifest or _manifest([_component()]),
            FactoryConfig(
                use_worktrees=False,
                create_prs=False,
                max_parallel=1,
                max_retries=0,
                retry_delay=0,
                review_mode="skip",
                progress_log_path=root / "progress.jsonl",
            ),
            base or _base_config(root),
            PlainUI(no_color=True, file=io.StringIO()),
            root,
        )


class TestBothGuardsReadOneSnapshot:
    """End to end, in ``use_worktrees=False`` - the mode #268's tamper
    check documented as uncovered, because the 'worktree' IS the project
    root and there is no pre-run copy to compare against. A plan-time
    snapshot needs no second copy, so this is the strictest place to
    make the claim.
    """

    def test_both_guards_are_handed_the_same_scope(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        seams = _Seams()
        _run(tmp_path, seams)

        assert seams.loop_allowed == [AUTHORED]
        assert seams.phase1_allowed == [AUTHORED]
        assert seams.loop_harness == [HARNESS]
        assert seams.phase1_harness == [HARNESS]

    def test_a_prd_rewritten_mid_run_reaches_neither_guard(
        self,
        tmp_path: Path,
    ) -> None:
        """The whole point. Under ``use_worktrees=False`` the file the
        agent edits IS the file scope used to be read from, at both
        guards - the in-loop guard read it at submit time and Phase 1
        re-read it afterwards. The snapshot was taken before the loop
        ran, so the widening is inert rather than detected.
        """
        _setup_project(tmp_path)
        seams = _Seams()

        def widen() -> None:
            _write_prd(tmp_path / PRD_REL, WIDENED)

        _run(tmp_path, seams, on_loop=widen)

        assert json.loads((tmp_path / PRD_REL).read_text())["allowedPaths"] == WIDENED
        assert seams.phase1_allowed == [AUTHORED], "Phase 1 read the rewritten scope"
        assert "kstrl/" not in (seams.phase1_allowed[0] or [])

    def test_the_run_wide_flag_is_the_fallback_at_both_guards(
        self,
        tmp_path: Path,
    ) -> None:
        """The pre-existing asymmetry, settled.

        ``factory._component_scope`` fell back to ``--allowed-paths``
        and ``_resolve_verify_scope`` did not, so a legacy PRD plus the
        flag left the engineer held to the operator's list in-loop while
        Phase 1 enforced nothing at all. Keeping the fallback and
        applying it at both is the direction that removes no authority:
        the flag is the operator's, it is not agent-writable, and it is
        the only scope source ``ks run --allowed-paths`` has.
        """
        _setup_project(tmp_path, allowed=None)
        base = _base_config(tmp_path)
        base.allowed_paths = ["fallback/"]
        seams = _Seams()
        _run(tmp_path, seams, base=base)

        assert seams.loop_allowed == [["fallback/"]]
        assert seams.phase1_allowed == [["fallback/"]]

    def test_a_component_prd_still_beats_the_run_wide_flag(
        self,
        tmp_path: Path,
    ) -> None:
        """Agreement must not be bought by ignoring the architect: the
        per-component list is the more specific authority and still
        wins, at both guards."""
        _setup_project(tmp_path)
        base = _base_config(tmp_path)
        base.allowed_paths = ["fallback/"]
        seams = _Seams()
        _run(tmp_path, seams, base=base)

        assert seams.loop_allowed == [AUTHORED]
        assert seams.phase1_allowed == [AUTHORED]

    def test_a_legacy_prd_with_no_flag_stays_unconstrained(
        self,
        tmp_path: Path,
    ) -> None:
        """The historical behaviour, unchanged: no authored scope and no
        flag means no constraint, which ``check_diff_scope`` passes. The
        in-loop guard does not fire either, so the two still agree."""
        _setup_project(tmp_path, allowed=None)
        seams = _Seams()
        _run(tmp_path, seams)

        assert seams.loop_allowed == [None]
        assert seams.phase1_allowed == [None]


class TestSnapshotResolution:
    """The unit-level properties the end-to-end tests rest on."""

    def test_the_snapshot_is_taken_from_the_pre_run_tree(
        self,
        tmp_path: Path,
    ) -> None:
        _setup_project(tmp_path)
        wt = tmp_path / "wt"
        _write_prd(wt / PRD_REL, WIDENED)
        scope = ComponentScope.resolve(_component(), tmp_path, _base_config(tmp_path))
        assert scope.allowed_paths == AUTHORED

    def test_phase_1_ignores_a_widened_worktree_copy(self, tmp_path: Path) -> None:
        """The worktree half of the same property. Phase 1 used to load
        ``wt_path / comp.prd_path`` and take allowedPaths off it, which
        is the copy the carve-out lets the agent write; it now reads no
        file at all.
        """
        _setup_project(tmp_path)
        wt = tmp_path / "wt"
        _write_prd(wt / PRD_REL, WIDENED)
        comp = _component()
        scope = _pipeline(tmp_path, comp, wt).run_scope.for_component(comp.id)
        assert scope.allowed_paths == AUTHORED
        assert scope.harness_paths == HARNESS

    def test_every_component_is_snapshotted_not_only_the_pending_ones(
        self,
        tmp_path: Path,
    ) -> None:
        """A retried or resumed component must be judged against the
        scope this run recorded, so filtering by status here would leave
        it looking up a key that is not there."""
        _setup_project(tmp_path)
        comp = _component()
        comp.status = "completed"
        run_scope = RunScope.resolve(_manifest([comp]), tmp_path, _base_config(tmp_path))
        assert run_scope.for_component(COMPONENT_ID).allowed_paths == AUTHORED

    def test_an_unknown_component_fails_closed(self, tmp_path: Path) -> None:
        """Unreachable while the pipeline and the snapshot are built
        from one manifest - which is a claim about today's call graph,
        not a guarantee. The stand-in reports MORE (an empty carve-out)
        and carries an error that fails Phase 1 closed.
        """
        run_scope = RunScope.resolve(_manifest([]), tmp_path, _base_config(tmp_path))
        scope = run_scope.for_component("never-planned")
        assert scope.allowed_paths is None
        assert scope.harness_paths == []
        assert scope.source == "unresolved"
        assert scope.error is not None

    @pytest.mark.parametrize(
        ("allowed", "flag", "source", "origin"),
        [
            (AUTHORED, [], "component_prd", PRD_REL),
            (None, ["fallback/"], "run_flag", "--allowed-paths"),
            # Nothing supplied a list, so nothing is named as having
            # supplied one (#293 review). Recording the PRD here made
            # the audit trail say the file was the origin of a list it
            # did not provide, which is the opposite of what the record
            # is for.
            (None, [], "unconstrained", ""),
        ],
    )
    def test_provenance_names_the_authority(
        self,
        tmp_path: Path,
        allowed: list[str] | None,
        flag: list[str],
        source: str,
        origin: str,
    ) -> None:
        _setup_project(tmp_path, allowed=allowed)
        base = _base_config(tmp_path)
        base.allowed_paths = flag
        scope = ComponentScope.resolve(_component(), tmp_path, base)
        assert scope.source == source
        assert scope.origin == origin

    def test_an_unreadable_prd_beats_the_run_wide_flag(
        self,
        tmp_path: Path,
    ) -> None:
        """A scope that could not be READ is not a scope that does not
        exist (#293 review).

        The flag used to win here, silently: the component's authored
        list, possibly narrow, was replaced by the operator's run-wide
        one, possibly much broader, with `error=None` so nothing warned
        and nothing failed closed. The two cases are not the same
        thing, and only one of them is knowledge.
        """
        _setup_project(tmp_path, allowed=None)
        (tmp_path / PRD_REL).write_text("{not valid json")
        base = _base_config(tmp_path)
        base.allowed_paths = ["fallback/"]

        scope = ComponentScope.resolve(_component(), tmp_path, base)

        assert scope.source == "unresolved"
        assert scope.allowed_paths is None
        assert scope.error is not None and "failed to parse" in scope.error

    def test_a_legacy_prd_still_takes_the_flag(self, tmp_path: Path) -> None:
        """The other half of that rule, so the fix cannot be read as
        'the flag no longer works'. A PRD that LOADS and carries no
        allowedPaths is knowledge, and the operator's list is the
        intended authority for it."""
        _setup_project(tmp_path, allowed=None)
        base = _base_config(tmp_path)
        base.allowed_paths = ["fallback/"]

        scope = ComponentScope.resolve(_component(), tmp_path, base)

        assert scope.source == "run_flag"
        assert scope.allowed_paths == ["fallback/"]
        assert scope.error is None


class TestAnUnresolvedScopeCannotBeSwitchedOff:
    """The fail-closed signal does not depend on an unrelated toggle.

    ``[verify] check_diff_scope = false`` turns off the scope
    COMPARISON. It used to also drop the report that no trustworthy
    scope could be established, because ``allowed_paths_error`` was only
    consumed inside that gate - and with no authored list the in-loop
    guard is inert too, so the component ran and merged with no scope
    enforcement at all and nothing said (#293 review). Same argument
    ``check_prd_stories`` makes for carrying the tamper refusal.

    #294 moved the refusal onto its own check, ``scope_source``. The
    toggle still cannot reach it: what changed is the NAME the failure
    reports under, not whether it reports.
    """

    def _verify(
        self,
        root: Path,
        *,
        check_diff_scope: bool,
        allowed_paths: list[str] | None = None,
        error: str | None = "pre-run PRD not found: nothing to trust",
    ) -> VerificationResult:
        return run_mechanical_verification(
            root,
            root / PRD_REL,
            "main",
            allowed_paths,
            VerifyConfig(
                check_diff_scope=check_diff_scope,
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_bad_patterns=False,
                subprocess_timeout=30.0,
            ),
            allowed_paths_error=error,
        )

    @pytest.mark.parametrize("check_diff_scope", [False, True])
    def test_the_refusal_does_not_depend_on_the_toggle(
        self,
        tmp_path: Path,
        check_diff_scope: bool,
    ) -> None:
        _setup_project(tmp_path)
        result = self._verify(tmp_path, check_diff_scope=check_diff_scope)
        assert not result.passed
        assert [c.name for c in result.checks if not c.passed] == ["scope_source"]
        # Not merely renamed: the comparison is not reported at all, so
        # nothing claims a passing scope beside the refusal.
        assert "diff_scope" not in [c.name for c in result.checks]

    def test_the_toggle_still_removes_the_comparison(self, tmp_path: Path) -> None:
        """It must not become "diff_scope always runs": with no error
        the toggle is exactly as off as it was."""
        _setup_project(tmp_path)
        result = self._verify(
            tmp_path,
            check_diff_scope=False,
            allowed_paths=list(AUTHORED),
            error=None,
        )
        assert "diff_scope" not in [c.name for c in result.checks]


class TestScopeIsRecorded:
    """A decision made once has to be written down once, or the only way
    to answer 'why was this component allowed to write that?' after the
    run is to re-derive it from files the run has since changed."""

    def _events(self, root: Path, manifest: Manifest) -> list[ComponentScopeResolved]:
        seen: list[Event] = []
        bus = EventBus(CallbackSink(seen.append), run_id="run-test")
        _record_run_scope(
            RunScope.resolve(manifest, root, _base_config(root)),
            bus,
            PlainUI(no_color=True, file=io.StringIO()),
        )
        return [e for e in seen if isinstance(e, ComponentScopeResolved)]

    def test_one_event_per_component_carries_both_lists(
        self,
        tmp_path: Path,
    ) -> None:
        _setup_project(tmp_path)
        events = self._events(tmp_path, _manifest([_component()]))
        assert len(events) == 1
        assert events[0].component == COMPONENT_ID
        assert events[0].scope_source == "component_prd"
        assert events[0].origin == PRD_REL
        assert list(events[0].allowed_paths) == AUTHORED
        assert list(events[0].harness_paths) == HARNESS
        assert events[0].error == ""

    def test_an_unresolved_scope_records_why(self, tmp_path: Path) -> None:
        """The case that will fail Phase 1 closed later: the record has
        to say what could not be established, not just that nothing
        was."""
        comp = Component("orphan", "O", "D", [], "scripts/kstrl/feature/orphan/prd.json", "b")
        events = self._events(tmp_path, _manifest([comp]))
        assert events[0].scope_source == "unresolved"
        assert "PRD not found" in events[0].error

    def test_the_run_says_it_resolved_a_scope(self, tmp_path: Path) -> None:
        """The terminal gets a summary, not one paragraph per
        component, but it must not get silence."""
        _setup_project(tmp_path)
        out = io.StringIO()
        _record_run_scope(
            RunScope.resolve(_manifest([_component()]), tmp_path, _base_config(tmp_path)),
            EventBus(CallbackSink(lambda _e: None), run_id="run-test"),
            PlainUI(no_color=True, file=out),
        )
        assert "Scope resolved for 1 component(s): 1 from component_prd" in out.getvalue()
