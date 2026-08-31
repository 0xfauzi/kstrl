"""The agent may not rewrite the PRD it is graded against (#264, #268, #269).

The harness carve-out puts the component PRD inside every component's
effective scope BY DESIGN - the engineer has to write it to set
``passes``. Phase 1 is not its only reader: ``check_prd_stories``
re-reads the stories, ``check_fixtures_from_prd`` re-reads the fixtures,
the reviewer is handed the acceptance criteria and the R10.3 set-point
sensor re-reads the claims. Without a refusal an agent could delete a
story's acceptance criteria, neuter an executable fixture, and pass
gates it authored.

``pipeline.prd_tamper_error`` compares the worktree copy against the
pre-run copy at ``root_dir``, which is outside every worktree and so is
not agent-writable. Everything is pinned except ``passes`` and
``notes``.

What is NO LONGER compared is ``allowedPaths``. #269 resolves each
component's scope once, before the first engineer call, and hands the
same snapshot to both guards, so an edit to that field changes nothing
and refusing one could only ever be a false positive - which, being a
Phase 1 failure, strands the component. The comparison is gone rather
than relaxed; ``kstrl.scope`` records the measured history behind that.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from kstrl.prd import UserStory
from kstrl.verify import VerifyConfig, check_prd_stories, run_mechanical_verification
from tests.test_harness_path_scope import (
    AUTHORED,
    PRD_REL,
    STORY,
    _write_prd,
)

FIXTURE: dict[str, Any] = {
    "description": "round trip",
    "fixture_type": "function",
    "input_data": {"module": "m", "function": "f", "args": []},
    "expected": {"returns": 1},
}


class TestPrdTamper:
    """The PRD is agent-writable BY DESIGN once the carve-out lands, and
    it is not only Phase 1 that trusts it. Everything is pinned against
    the pre-run copy except ``passes`` and ``notes``, which are the
    engineer's to write.
    """

    def _error(self, root: Path, wt: Path) -> str | None:
        """The refusal as Phase 1 produces it, or None.

        Through ``check_prd_stories``, which is where the comparison
        lives (#269): the check that reads the stories is the one that
        has to refuse a rewritten copy of them. Every story here passes,
        so a failure is the tamper refusal and nothing else.
        """
        result = check_prd_stories(wt / PRD_REL, root / PRD_REL)
        if result.passed:
            return None
        return "\n".join([result.message, *result.details])

    # -- scope is settled elsewhere now -------------------------------------

    def test_a_rewritten_scope_is_not_this_check_s_business(
        self,
        tmp_path: Path,
    ) -> None:
        """The check that used to fire here, and must not any more.

        A widened ``allowedPaths`` in the worktree is inert: the scope
        both guards enforce came from the plan-time snapshot, so this
        edit changes nothing and refusing it would strand the component
        for a change with no effect. ``tests/test_scope_snapshot.py``
        pins the other half - that the widening really does not reach
        either guard.
        """
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, [*AUTHORED, "kstrl/"])
        assert self._error(tmp_path, wt) is None

    @pytest.mark.parametrize(
        "worktree_scope",
        [
            ["tests/", "src/writers_room/"],  # a benign reorder
            ["src/writers_room/"],  # a narrowing
            None,  # the field dropped entirely
        ],
    )
    def test_no_shape_of_scope_edit_can_strand_a_component(
        self,
        tmp_path: Path,
        worktree_scope: list[str] | None,
    ) -> None:
        """Every false positive this check could produce is a hard stop,
        because the refusal becomes a Phase 1 failure and the retry
        reproduces the same file. The list version of this comparison
        failed the first two of these."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, worktree_scope)
        assert self._error(tmp_path, wt) is None

    # -- the rest of the PRD, which the snapshot does not cover --------------

    def test_passes_and_notes_are_the_engineers_to_write(
        self,
        tmp_path: Path,
    ) -> None:
        """The two mutable fields. Setting ``passes`` is the whole job,
        and they are also the only fields
        review.revert_unconfirmed_stories touches, so the harness's own
        set-point write cannot trip this check."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED, stories=[{**STORY, "passes": False}])
        _write_prd(
            wt / PRD_REL,
            AUTHORED,
            stories=[{**STORY, "passes": True, "notes": "implemented"}],
        )
        assert self._error(tmp_path, wt) is None

    @pytest.mark.parametrize(
        ("mutation", "field"),
        [
            # check_prd_stories re-reads this file, so an agent that
            # deletes a criterion passes a gate it authored.
            ({"acceptanceCriteria": ["AC-1"]}, "acceptance_criteria"),
            ({"title": "Something easier"}, "title"),
            ({"priority": 9}, "priority"),
            ({"id": "US-999"}, None),
        ],
    )
    def test_a_rewritten_story_is_refused(
        self,
        tmp_path: Path,
        mutation: dict[str, Any],
        field: str | None,
    ) -> None:
        """The message names the field that moved, so the retry agent is
        told what to restore rather than that 'something' changed."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, AUTHORED, stories=[{**STORY, **mutation}])
        error = str(self._error(tmp_path, wt))
        if field is None:
            # A changed id is a changed story SET, not a rewritten story.
            assert "story set" in error
        else:
            assert f"rewrote {field} on story {STORY['id']}" in error

    def test_a_removed_story_is_refused(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, AUTHORED, stories=[])
        assert "story set" in str(self._error(tmp_path, wt))

    def test_a_field_added_to_userstory_is_pinned_by_default(self) -> None:
        """The fail-closed direction. _pinned_stories blanks the two
        writable fields and compares whole stories, so a NEW UserStory
        field is pinned unless someone deliberately exempts it - the
        opposite of a hand-listed tuple, where forgetting a field
        silently opens a hole.
        """
        writable = {"passes", "notes"}
        pinned = {f.name for f in fields(UserStory)} - writable
        assert pinned == {"id", "title", "acceptance_criteria", "priority"}, (
            "UserStory gained or lost a field; confirm PRD._pinned_stories "
            "still pins everything the engineer may not rewrite"
        )

    def test_a_neutered_fixture_is_refused(self, tmp_path: Path) -> None:
        """check_fixtures_from_prd runs these as executable oracles."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED, fixtures=[FIXTURE])
        _write_prd(
            wt / PRD_REL,
            AUTHORED,
            fixtures=[{**FIXTURE, "expected": {"returns": 999}}],
        )
        assert "approved fixtures" in str(self._error(tmp_path, wt))

    def test_a_changed_branch_name_is_refused(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, AUTHORED, branch="kstrl/factory/elsewhere")
        assert "branchName" in str(self._error(tmp_path, wt))

    # -- the uncovered cases, stated rather than implied ---------------------

    def test_no_pre_run_copy_skips_the_comparison(self, tmp_path: Path) -> None:
        """Nothing to compare against. A missing root copy is a harness
        or operator condition - the root tree is outside every worktree,
        so an agent cannot arrange it."""
        wt = tmp_path / "wt"
        _write_prd(wt / PRD_REL, AUTHORED)
        assert self._error(tmp_path, wt) is None

    def test_an_unreadable_worktree_copy_fails_on_the_load(
        self,
        tmp_path: Path,
    ) -> None:
        """The gate is held either way. A PRD that will not parse cannot
        be compared, and the check refuses it on the load rather than on
        a comparison it could not run."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        (wt / PRD_REL).parent.mkdir(parents=True)
        (wt / PRD_REL).write_text("{not json")
        result = check_prd_stories(wt / PRD_REL, tmp_path / PRD_REL)
        assert not result.passed
        assert "Failed to load PRD" in result.message

    def test_without_worktrees_there_is_nothing_to_compare(
        self,
        tmp_path: Path,
    ) -> None:
        """use_worktrees=False makes the 'worktree' root_dir itself, so
        the two paths name one file. This comparison cannot be the thing
        that gives that mode an isolation boundary - but scope IS
        covered there, because #269 reads it before the agent starts
        instead of comparing afterwards.
        """
        _write_prd(tmp_path / PRD_REL, AUTHORED, stories=[{**STORY, "title": "rewritten"}])
        assert self._error(tmp_path, tmp_path) is None

    def test_the_refusal_reaches_phase_1_through_prd_stories(
        self,
        tmp_path: Path,
    ) -> None:
        """The check that carries the refusal is the one that reads the
        stories, not diff_scope: scope stopped being the question, and
        diff_scope can be switched off while this check cannot.
        """
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, AUTHORED, stories=[{**STORY, "acceptanceCriteria": ["AC-1"]}])

        result = check_prd_stories(wt / PRD_REL, tmp_path / PRD_REL)
        assert result.name == "prd_stories"
        assert result.passed is False
        assert "not the one this run started with" in result.message
        details = "\n".join(result.details)
        assert "rewrote acceptance_criteria" in details
        assert "do not treat this as permission" in details

    def test_the_gate_that_reads_it_runs_even_with_diff_scope_off(
        self,
        tmp_path: Path,
    ) -> None:
        """Why this check and not diff_scope: `[verify] check_diff_scope
        = false` drops that one from the run entirely, while
        prd_stories runs whenever there is a PRD at all. Defence in
        depth an unrelated toggle can disable is not defence in depth.
        """
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, AUTHORED, fixtures=[{**FIXTURE, "expected": {"returns": 0}}])
        result = run_mechanical_verification(
            wt,
            wt / PRD_REL,
            "main",
            None,
            VerifyConfig(
                check_diff_scope=False,
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                subprocess_timeout=30.0,
            ),
            pre_run_prd_path=tmp_path / PRD_REL,
        )
        assert not result.passed
        failed = [c for c in result.checks if not c.passed]
        assert [c.name for c in failed] == ["prd_stories"]
