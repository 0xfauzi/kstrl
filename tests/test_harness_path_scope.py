"""kstrl's own files are in scope at both guards, and never wider (#264).

A real paid run put every line of product code inside ``allowedPaths``
and still failed: the three violations were the component PRD, the
component progress log and ``scripts/kstrl/codebase_map.md`` - files
kstrl's own checks require the engineer to write. Measured: $14.49
across three byte-identical attempts, 2001 lines of passing work
reported as ``Completed: 0, Failed: 1``.

The tests here pin four things:

1. The carve-out is EXACT files, never a directory prefix. Operators
   currently reach for ``scripts/kstrl/``, which exposes the manifest
   and every sibling component; the fix must be narrower than the
   workaround it replaces, not wider.
2. Both guards apply it. The in-loop guard fires FIRST (loop.py returns
   early on a violation), so Phase 1's ``check_diff_scope`` was never
   even reached in the recorded run - fixing one site fixes nothing.
3. Both guards report the two sets SEPARATELY. An operator reading a
   scope failure must still see what they authorised.
4. The PRD is now agent-writable by design, so Phase 1 refuses a PRD
   whose ``allowedPaths`` moved since the run started, and the plan-time
   preflight refuses a component that cannot pass at all - before any
   engineer call is paid for.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config import component_harness_paths
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    _preflight_component_scope,
    _run_component,
    run_factory,
)
from kstrl.guards import check_violations, path_is_allowed, scope_entry_hazard
from kstrl.loop import LoopResult
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import check_diff_scope
from tests.test_progress_scope import _base_config, _pipeline

COMPONENT_ID = "document-format"
FEATURE_DIR = f"scripts/kstrl/feature/{COMPONENT_ID}"
PRD_REL = f"{FEATURE_DIR}/prd.json"
PROGRESS_REL = f"{FEATURE_DIR}/progress.txt"
MAP_REL = "scripts/kstrl/codebase_map.md"

# The authored scope from the recorded run: product code only.
AUTHORED = ["src/writers_room/", "tests/"]

# The three files the harness itself requires, in the order
# component_harness_paths returns them (sorted).
HARNESS = sorted([PRD_REL, PROGRESS_REL, MAP_REL])


def _component(prd_path: str = PRD_REL) -> Component:
    return Component(
        COMPONENT_ID,
        "Document format",
        "Parse and serialize documents",
        [],
        prd_path,
        f"kstrl/factory/{COMPONENT_ID}",
    )


def _manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="writers-room",
        base_branch="main",
        single_pr=False,
        components=components,
    )


def _write_prd(path: Path, allowed: list[str] | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "branchName": f"kstrl/factory/{COMPONENT_ID}",
        "userStories": [
            {
                "id": "US-001",
                "title": "Parse a document",
                "acceptanceCriteria": ["AC"],
                "priority": 1,
                "passes": True,
                "notes": "",
            }
        ],
    }
    if allowed is not None:
        body["allowedPaths"] = allowed
    path.write_text(json.dumps(body))


def _setup_project(root: Path, allowed: list[str] | None = None) -> None:
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}')
    (root / "kstrl.toml").write_text("[knowledge]\nenabled = false\n")
    _write_prd(root / PRD_REL, AUTHORED if allowed is None else allowed)


# ---------------------------------------------------------------------------
# The carve-out itself
# ---------------------------------------------------------------------------


class TestComponentHarnessPaths:
    def test_it_names_the_three_files_the_harness_requires(self) -> None:
        assert component_harness_paths(PRD_REL, PROGRESS_REL, MAP_REL) == HARNESS

    def test_every_entry_is_an_exact_file_never_a_prefix(self) -> None:
        """The narrowness that is the whole point.

        A trailing slash would make ``scripts/kstrl/codebase_map.md``
        behave like ``scripts/kstrl/``, which is the blanket prefix
        operators resort to today and which DECOMPOSE_PROMPT rule 12
        refuses because it exposes the manifest and sibling components.
        """
        entries = component_harness_paths(
            PRD_REL,
            f"{FEATURE_DIR}/",
            "scripts/kstrl/",
        )
        assert all(not e.endswith("/") for e in entries)

    def test_the_carve_out_does_not_reach_a_sibling_component(self) -> None:
        entries = component_harness_paths(PRD_REL, PROGRESS_REL, MAP_REL)
        assert not path_is_allowed("scripts/kstrl/feature/other/prd.json", entries)
        assert not path_is_allowed("scripts/kstrl/manifest.json", entries)
        assert not path_is_allowed("kstrl/factory.py", entries)

    def test_the_single_component_layout_collapses_to_two_entries(self) -> None:
        """``ks run``'s PRD sits beside the map, and the progress log
        beside the PRD; duplicates are folded, not repeated."""
        assert component_harness_paths(
            "scripts/kstrl/prd.json",
            "scripts/kstrl/prd.json",
            MAP_REL,
        ) == [MAP_REL, "scripts/kstrl/prd.json"]

    def test_a_non_relative_path_is_kept_for_the_preflight_to_name(self) -> None:
        """Inert here (it can never match a diff name) and refused by
        _preflight_component_scope before any spend."""
        entries = component_harness_paths(PRD_REL, PROGRESS_REL, "/etc/map.md")
        assert "/etc/map.md" in entries
        assert not path_is_allowed("etc/map.md", entries)


# ---------------------------------------------------------------------------
# Seam 1: the in-loop guard
# ---------------------------------------------------------------------------


class TestInLoopGuard:
    def test_check_violations_clears_the_harness_files(self) -> None:
        changed = {
            "src/writers_room/document.py",
            "tests/test_document.py",
            PRD_REL,
            PROGRESS_REL,
            MAP_REL,
        }
        assert check_violations(changed, AUTHORED) == HARNESS
        assert check_violations(changed, AUTHORED, HARNESS) == []

    def test_it_still_catches_a_real_escape(self) -> None:
        changed = {MAP_REL, "kstrl/factory.py"}
        assert check_violations(changed, AUTHORED, HARNESS) == ["kstrl/factory.py"]

    def test_run_component_hands_the_carve_out_to_the_loop(
        self,
        tmp_path: Path,
    ) -> None:
        """The site the recorded failure actually fired from.

        loop.run_loop returns early on a guard violation, so Phase 1
        never ran. The carve-out has to arrive here or the fix is
        unreachable.
        """
        _setup_project(tmp_path)
        seen: list[Any] = []

        def fake_run_loop(*args: Any, **kwargs: Any) -> LoopResult:
            seen.append(kwargs.get("guard_ignored_paths"))
            return LoopResult(
                completed=True,
                iterations=1,
                exit_code=0,
                duration_seconds=0.0,
            )

        with patch("kstrl.loop.run_loop", side_effect=fake_run_loop):
            _run_component(
                component_id=COMPONENT_ID,
                prd_path_str=PRD_REL,
                worktree_path_str=str(tmp_path),
                root_dir_str=str(tmp_path),
                prompt_file_str="scripts/kstrl/prompt.md",
                agent_cmd="echo test",
                model=None,
                reasoning=None,
                agent_type=None,
                sleep_seconds=0.0,
                allowed_paths=list(AUTHORED),
                redirect_output=False,
            )

        assert seen and seen[0] == HARNESS

    def test_the_halt_message_reports_the_two_sets_separately(
        self,
        tmp_path: Path,
    ) -> None:
        """An operator reading the failure must still see what THEY
        authorised, and the retry agent must not read its own PRD as the
        thing it has to stop writing."""
        _setup_project(tmp_path)

        def fake_run_loop(*args: Any, **kwargs: Any) -> LoopResult:
            return LoopResult(
                completed=False,
                iterations=1,
                exit_code=1,
                duration_seconds=0.0,
                guard_violations=("kstrl/factory.py",),
            )

        with patch("kstrl.loop.run_loop", side_effect=fake_run_loop):
            result = _run_component(
                component_id=COMPONENT_ID,
                prd_path_str=PRD_REL,
                worktree_path_str=str(tmp_path),
                root_dir_str=str(tmp_path),
                prompt_file_str="scripts/kstrl/prompt.md",
                agent_cmd="echo test",
                model=None,
                reasoning=None,
                agent_type=None,
                sleep_seconds=0.0,
                allowed_paths=list(AUTHORED),
                redirect_output=False,
            )

        assert result.error is not None
        assert "Allowed paths (complete list): src/writers_room/, tests/" in result.error
        assert "plus harness artifacts" in result.error
        assert MAP_REL in result.error


# ---------------------------------------------------------------------------
# Seam 2: Phase 1's check_diff_scope
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A branch whose diff against main touches product code AND the
    three harness files, exactly as the recorded run's branch did."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "work")
    for rel in ("src/writers_room/document.py", "tests/test_document.py", *HARNESS):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "work")
    return root


class TestPhase1DiffScope:
    def test_without_the_carve_out_the_state_is_unwinnable(self, repo: Path) -> None:
        """The bug, pinned: writing the files makes diff_scope fail, not
        writing them makes prd_stories and self_critique fail. With no
        carve-out there is also no harness-artifacts line to print."""
        result = check_diff_scope(repo, "main", AUTHORED)
        assert result.passed is False
        assert "3 files outside allowed scope" in result.message
        assert not any(d.startswith("Plus harness artifacts") for d in result.details)

    def test_the_carve_out_makes_it_winnable(self, repo: Path) -> None:
        result = check_diff_scope(repo, "main", AUTHORED, harness_paths=HARNESS)
        assert result.passed is True

    def test_a_real_escape_fails_and_names_the_two_sets_separately(
        self,
        repo: Path,
    ) -> None:
        (repo / "kstrl").mkdir()
        (repo / "kstrl" / "factory.py").write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "escape")
        result = check_diff_scope(repo, "main", AUTHORED, harness_paths=HARNESS)
        assert result.passed is False
        assert "kstrl/factory.py" in "\n".join(result.details)
        authored_line = next(
            d for d in result.details if d.startswith("Allowed paths (complete list):")
        )
        harness_line = next(d for d in result.details if d.startswith("Plus harness artifacts"))
        assert authored_line == "Allowed paths (complete list): src/writers_room/, tests/"
        assert MAP_REL in harness_line
        assert MAP_REL not in authored_line

    def test_harness_paths_never_create_a_scope_where_none_was_set(
        self,
        repo: Path,
    ) -> None:
        """An unconstrained component stays unconstrained: the carve-out
        widens an existing allowlist, it does not install one."""
        result = check_diff_scope(repo, "main", None, harness_paths=HARNESS)
        assert result.passed is True
        assert result.message == "No scope constraints (allowed_paths not set)"


# ---------------------------------------------------------------------------
# The self-widening hole the carve-out would otherwise open
# ---------------------------------------------------------------------------


class TestScopeSelfWidening:
    def _scope(self, root: Path, wt: Path) -> Any:
        comp = _component()
        return _pipeline(root, comp, wt)._resolve_verify_scope(comp, wt)

    def test_an_unchanged_prd_is_accepted(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, AUTHORED)
        scope = self._scope(tmp_path, wt)
        assert scope.error is None
        assert scope.allowed_paths == AUTHORED
        assert scope.harness_paths == HARNESS

    def test_a_widened_prd_fails_closed(self, tmp_path: Path) -> None:
        """Phase 1 reads allowedPaths from the worktree - a file the
        agent must write to set ``passes``. Without this the agent could
        authorise its own scope and pass a check it wrote."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, [*AUTHORED, "kstrl/"])
        scope = self._scope(tmp_path, wt)
        assert scope.error is not None
        assert "no longer match the pre-run copy" in scope.error
        assert "kstrl/" in scope.error

    def test_a_narrowed_prd_is_refused_too(self, tmp_path: Path) -> None:
        """Any rewrite is refused, not only a widening: the check is
        'the scope you are judged against is the one the run started
        with', and a narrowing still means the two guards disagree."""
        wt = tmp_path / "wt"
        _write_prd(tmp_path / PRD_REL, AUTHORED)
        _write_prd(wt / PRD_REL, ["src/writers_room/"])
        assert self._scope(tmp_path, wt).error is not None

    def test_no_pre_run_copy_skips_the_comparison(self, tmp_path: Path) -> None:
        """Nothing to compare against. A missing root copy is a harness
        or operator condition - the root tree is outside every worktree,
        so an agent cannot arrange it."""
        wt = tmp_path / "wt"
        _write_prd(wt / PRD_REL, AUTHORED)
        scope = self._scope(tmp_path, wt)
        assert scope.error is None
        assert scope.allowed_paths == AUTHORED

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


# ---------------------------------------------------------------------------
# The plan-time preflight: no spend
# ---------------------------------------------------------------------------


class TestPreflightComponentScope:
    def test_a_compliant_component_passes(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        assert (
            _preflight_component_scope(
                _manifest([_component()]),
                tmp_path,
                _base_config(tmp_path),
            )
            == []
        )

    def test_an_unconstrained_component_skips_the_authored_arm(
        self,
        tmp_path: Path,
    ) -> None:
        """No allowedPaths means no scope, so there is nothing for a file
        to fall outside of."""
        _setup_project(tmp_path, allowed=[])
        assert (
            _preflight_component_scope(
                _manifest([_component()]),
                tmp_path,
                _base_config(tmp_path),
            )
            == []
        )

    def test_an_unconstrained_component_is_still_harness_checked(
        self,
        tmp_path: Path,
    ) -> None:
        """An engineer pointed outside its own worktree is a problem with
        or without an allowlist, so that arm is not gated on
        allowedPaths."""
        _setup_project(tmp_path, allowed=[])
        base = _base_config(tmp_path)
        base.codebase_map_file = Path("/elsewhere/codebase_map.md")
        errors = _preflight_component_scope(_manifest([_component()]), tmp_path, base)
        assert len(errors) == 1

    def test_one_broken_config_path_is_reported_once(self, tmp_path: Path) -> None:
        """The map and the progress log come from ONE [paths] setting
        shared by the whole run, so a three-component manifest must not
        produce three copies of the same error."""
        _setup_project(tmp_path)
        base = _base_config(tmp_path)
        base.codebase_map_file = Path("/elsewhere/codebase_map.md")
        manifest = _manifest([_component(), _component(), _component()])
        assert len(_preflight_component_scope(manifest, tmp_path, base)) == 1

    @pytest.mark.parametrize(
        ("entry", "hazard"),
        [
            ("/abs/src/", "absolute"),
            ("../outside/", "traversal"),
            (".", "root"),
            ("./", "root"),
            ("/", "root"),
            ("src/", None),
            ("tests/", None),
            (MAP_REL, None),
            ("a/b/c.py", None),
        ],
    )
    def test_the_shared_hazard_classifier(self, entry: str, hazard: str | None) -> None:
        """The predicate this preflight shares with
        decompose._validate_allowed_path_entry, so a hazard added for the
        architect is caught for hand-written manifests too."""
        assert scope_entry_hazard(entry) == hazard

    def test_an_unmatchable_authored_entry_is_refused(self, tmp_path: Path) -> None:
        """The hand-written-manifest backstop. decompose validates
        architect output; nothing validated a hand-edited PRD, so an
        absolute entry silently authorised nothing and every file under
        it became a violation - the same unwinnable loop, from the other
        end."""
        _setup_project(tmp_path, allowed=[f"{tmp_path}/src/writers_room/"])
        errors = _preflight_component_scope(
            _manifest([_component()]),
            tmp_path,
            _base_config(tmp_path),
        )
        assert len(errors) == 1
        assert "allowedPaths entry" in errors[0]
        assert "absolute path" in errors[0]
        assert f"plus harness artifacts: {', '.join(HARNESS)}" in errors[0]

    def test_a_non_relative_harness_path_is_refused(self, tmp_path: Path) -> None:
        """``[paths] codebase_map`` pointing outside the repo puts the
        engineer's writes outside its own worktree AND makes the
        carve-out unmatchable. Refused before any spend, and the error
        names the two sets separately."""
        _setup_project(tmp_path)
        base = _base_config(tmp_path)
        base.codebase_map_file = Path("/elsewhere/codebase_map.md")
        errors = _preflight_component_scope(
            _manifest([_component()]),
            tmp_path,
            base,
        )
        assert len(errors) == 1
        assert "/elsewhere/codebase_map.md" in errors[0]
        assert "absolute path" in errors[0]
        assert "Harness artifacts for this component" in errors[0]

    def test_a_traversing_harness_path_is_refused(self, tmp_path: Path) -> None:
        _setup_project(tmp_path)
        base = _base_config(tmp_path)
        base.progress_file = Path("../outside/progress.txt")
        errors = _preflight_component_scope(
            _manifest([_component()]),
            tmp_path,
            base,
        )
        assert len(errors) == 1
        assert "traverses outside" in errors[0]

    def test_run_factory_refuses_without_paying_for_an_engineer_call(
        self,
        tmp_path: Path,
    ) -> None:
        """The whole point: $14.49 and 41 minutes become exit 2 in the
        first second."""
        _setup_project(tmp_path)
        base = _base_config(tmp_path)
        base.codebase_map_file = Path("/elsewhere/codebase_map.md")
        calls: list[Any] = []

        def fake_component(*args: Any, **kwargs: Any) -> ComponentResult:
            calls.append(args)
            return ComponentResult(COMPONENT_ID, success=True, iterations=1)

        out = io.StringIO()
        ui = PlainUI(no_color=True, file=out)
        with patch("kstrl.factory._run_component", side_effect=fake_component):
            result = run_factory(
                _manifest([_component()]),
                FactoryConfig(
                    use_worktrees=False,
                    create_prs=False,
                    max_parallel=1,
                    max_retries=0,
                    retry_delay=0,
                    review_mode="skip",
                    progress_log_path=tmp_path / "progress.jsonl",
                ),
                base,
                ui,
                tmp_path,
            )

        assert result.exit_code == 2
        assert calls == [], "an engineer call was paid for after the refusal"
        assert "Refusing to run: components cannot pass the scope check" in out.getvalue()

    def test_a_compliant_manifest_still_runs(self, tmp_path: Path) -> None:
        """The preflight must not become a new way to fail a good run."""
        _setup_project(tmp_path)
        calls: list[Any] = []

        def fake_component(*args: Any, **kwargs: Any) -> ComponentResult:
            calls.append(args)
            return ComponentResult(COMPONENT_ID, success=True, iterations=1)

        with (
            patch("kstrl.factory._run_component", side_effect=fake_component),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                _manifest([_component()]),
                FactoryConfig(
                    use_worktrees=False,
                    create_prs=False,
                    max_parallel=1,
                    max_retries=0,
                    retry_delay=0,
                    review_mode="skip",
                    progress_log_path=tmp_path / "progress.jsonl",
                ),
                _base_config(tmp_path),
                PlainUI(no_color=True, file=io.StringIO()),
                tmp_path,
            )

        assert result.exit_code != 2
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# The two seams agree
# ---------------------------------------------------------------------------


def test_both_guards_judge_the_same_carve_out(tmp_path: Path) -> None:
    """The in-loop guard and Phase 1 must carve out the same set, or the
    component dies at whichever is stricter.

    Neither side is re-derived here. The in-loop value is captured from
    the ``guard_ignored_paths`` the worker actually hands ``run_loop``,
    and the Phase 1 value from the pipeline's own scope resolution, so a
    drift in either site's argument assembly fails this test.
    """
    _setup_project(tmp_path)
    wt = tmp_path / "wt"
    _write_prd(wt / PRD_REL, AUTHORED)
    seen: list[Any] = []

    def fake_run_loop(*args: Any, **kwargs: Any) -> LoopResult:
        seen.append(kwargs.get("guard_ignored_paths"))
        return LoopResult(completed=True, iterations=1, exit_code=0, duration_seconds=0.0)

    with patch("kstrl.loop.run_loop", side_effect=fake_run_loop):
        _run_component(
            component_id=COMPONENT_ID,
            prd_path_str=PRD_REL,
            worktree_path_str=str(tmp_path),
            root_dir_str=str(tmp_path),
            prompt_file_str="scripts/kstrl/prompt.md",
            agent_cmd="echo test",
            model=None,
            reasoning=None,
            agent_type=None,
            sleep_seconds=0.0,
            progress_file_str=_base_config(tmp_path).component_progress_file(
                PRD_REL,
                tmp_path,
            ),
            allowed_paths=list(AUTHORED),
            redirect_output=False,
        )

    comp = _component()
    phase1 = _pipeline(tmp_path, comp, wt)._resolve_verify_scope(comp, wt)
    assert seen and seen[0] == phase1.harness_paths == HARNESS
