"""The agent may not rewrite the PRD it is graded against (#264, #268).

The harness carve-out puts the component PRD inside every component's
effective scope BY DESIGN - the engineer has to write it to set
``passes``. Phase 1 is not its only reader: ``check_prd_stories``
re-reads the stories and ``check_fixtures_from_prd`` re-reads the
fixtures, so without this an agent could delete a story's acceptance
criteria, neuter an executable fixture, or widen its own
``allowedPaths``, and pass gates it authored.

``pipeline.prd_tamper_error`` compares the worktree copy against the
pre-run copy at ``root_dir``, which is outside every worktree and so is
not agent-writable. Everything is pinned except ``passes`` and
``notes``.

Scope is compared as SETS and only ADDITIONS fail. The first version
compared lists, so a benign re-serialisation failed Phase 1 closed, the
component retried, the agent re-serialised the same way, and it failed
identically forever: #264's own unwinnable retry loop, rebuilt by its
own fix. Narrowing is not widening.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from kstrl.prd import UserStory
from kstrl.verify import check_diff_scope
from tests.test_harness_path_scope import (
    AUTHORED,
    HARNESS,
    PRD_REL,
    STORY,
    _component,
    _write_prd,
)
from tests.test_progress_scope import _pipeline

FIXTURE: dict[str, Any] = {
    "description": "round trip",
    "fixture_type": "function",
    "input_data": {"module": "m", "function": "f", "args": []},
    "expected": {"returns": 1},
}


class TestPrdTamper:
    """The PRD is agent-writable BY DESIGN once the carve-out lands, and
    it is not only Phase 1 that trusts it: ``check_prd_stories`` re-reads
    the stories and ``check_fixtures_from_prd`` re-reads the fixtures.
    Everything is pinned against the pre-run copy except ``passes`` and
    ``notes``, which are the engineer's to write.
    """

    def _scope(self, root: Path, wt: Path) -> Any:
        comp = _component()
        return _pipeline(root, comp, wt)._resolve_verify_scope(comp, wt)

    def _error(self, root: Path, wt: Path) -> str | None:
        error = self._scope(root, wt).error
        return str(error) if error is not None else None

    # -- scope: sets, and only additions ------------------------------------

    def test_an_unchanged_prd_is_accepted(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, AUTHORED)
        scope = self._scope(tmp_path, wt)
        assert scope.error is None
        assert scope.allowed_paths == AUTHORED
        assert scope.harness_paths == HARNESS

    def test_a_reordered_scope_is_not_tampering(self, tmp_path: Path) -> None:
        """THE regression from the #268 review. List equality failed a
        benign re-serialisation, that became allowed_paths_error, the
        component retried, the agent re-serialised the same way, and it
        failed identically forever - #264's own unwinnable loop rebuilt
        by its own fix.
        """
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, ["src/writers_room/", "tests/"])
        _write_prd(wt / PRD_REL, ["tests/", "src/writers_room/"])
        assert self._error(tmp_path, wt) is None

    def test_a_narrowed_scope_is_not_tampering(self, tmp_path: Path) -> None:
        """Narrowing is not widening. A stricter worktree scope makes
        Phase 1 reject MORE, not less, so there is nothing to refuse."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, ["src/writers_room/"])
        assert self._error(tmp_path, wt) is None

    @pytest.mark.parametrize(
        "worktree_scope",
        [
            [*AUTHORED, "kstrl/"],
            # Set semantics must not become a way to smuggle an entry in
            # behind a shuffle.
            ["kstrl/", "tests/", "src/writers_room/"],
        ],
    )
    def test_a_genuine_addition_fails_closed(
        self,
        tmp_path: Path,
        worktree_scope: list[str],
    ) -> None:
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, worktree_scope)
        assert "added kstrl/ to allowedPaths" in str(self._error(tmp_path, wt))

    def test_dropping_allowed_paths_is_the_maximal_widening(
        self,
        tmp_path: Path,
    ) -> None:
        """check_diff_scope treats an absent allowedPaths as 'no
        constraint', so deleting the field authorises everything."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, None)
        assert "disables the scope check" in str(self._error(tmp_path, wt))

    def test_a_run_that_started_unconstrained_has_nothing_to_widen(
        self,
        tmp_path: Path,
    ) -> None:
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, None)
        _write_prd(wt / PRD_REL, AUTHORED)
        assert self._error(tmp_path, wt) is None

    # -- the rest of the PRD ------------------------------------------------

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
        scope = self._scope(tmp_path, wt)
        assert scope.error is None
        assert scope.allowed_paths == AUTHORED

    def test_without_worktrees_there_is_nothing_to_compare(
        self,
        tmp_path: Path,
    ) -> None:
        """use_worktrees=False makes the 'worktree' root_dir itself, so
        the two paths name one file. That mode has no isolation boundary
        at all, and this check cannot be the thing that gives it one."""
        _write_prd(tmp_path / PRD_REL, [*AUTHORED, "kstrl/"])
        assert self._error(tmp_path, tmp_path) is None

    def test_the_fail_closed_message_says_what_to_restore(
        self,
        tmp_path: Path,
    ) -> None:
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, [*AUTHORED, "kstrl/"])
        scope = self._scope(tmp_path, wt)
        result = check_diff_scope(
            tmp_path,
            "main",
            scope.allowed_paths,
            allowed_paths_error=scope.error,
        )
        assert result.passed is False
        assert "failing closed" in result.message
        assert "do not treat this as permission to widen the diff" in "\n".join(result.details)
