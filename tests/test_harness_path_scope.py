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
from typing import Any, cast, get_args
from unittest.mock import patch

import pytest

from kstrl.cli import _understand_core
from kstrl.commandrun import CommandRun
from kstrl.config import component_harness_paths
from kstrl.decompose import _SCOPE_HAZARD_ADVICE
from kstrl.events import EventBus
from kstrl.factory import (
    _SCOPE_HAZARD_REASONS,
    ComponentResult,
    FactoryConfig,
    _preflight_component_scope,
    _run_component,
    run_factory,
)
from kstrl.guards import (
    ScopeHazard,
    check_violations,
    path_is_allowed,
    scope_entry_hazard,
)
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


STORY: dict[str, Any] = {
    "id": "US-001",
    "title": "Parse a document",
    "acceptanceCriteria": ["AC-1", "AC-2"],
    "priority": 1,
    "passes": True,
    "notes": "",
}


def _write_prd(
    path: Path,
    allowed: list[str] | None,
    *,
    stories: list[dict[str, Any]] | None = None,
    branch: str | None = None,
    fixtures: list[dict[str, Any]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "branchName": branch or f"kstrl/factory/{COMPONENT_ID}",
        "userStories": [dict(STORY)] if stories is None else stories,
    }
    if allowed is not None:
        body["allowedPaths"] = allowed
    if fixtures is not None:
        body["fixtures"] = fixtures
    path.write_text(json.dumps(body))


def _spy_run_loop(
    completed: bool = True,
    guard_violations: tuple[str, ...] = (),
) -> tuple[list[Any], Any]:
    """A run_loop stand-in plus the list it records into.

    Every test here asks the same question of the same seam - what
    ``guard_ignored_paths`` did the caller pass? - so the closure is
    written once.
    """
    seen: list[Any] = []

    def fake(*args: Any, **kwargs: Any) -> LoopResult:
        seen.append(kwargs.get("guard_ignored_paths"))
        return LoopResult(
            completed=completed,
            iterations=1,
            exit_code=0 if completed else 1,
            duration_seconds=0.0,
            guard_violations=guard_violations,
        )

    return seen, fake


def _setup_project(root: Path, allowed: list[str] | None = AUTHORED) -> None:
    """``allowed`` means what it means in _write_prd: None OMITS the
    field. An empty list is not a way to say that - PRD.validate_schema
    rejects `"allowedPaths": []` outright, so writing one produced an
    unloadable PRD and silently exercised the fallback path instead.
    """
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}')
    (root / "kstrl.toml").write_text("[knowledge]\nenabled = false\n")
    _write_prd(root / PRD_REL, allowed)


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

    def test_a_non_relative_path_is_kept_and_is_harmless(self) -> None:
        """An absolute [paths] setting is supported (see
        config.reconcile_progress_config): the file lives outside every
        worktree, so it never appears in a component diff and can never
        BE a violation. The entry rides along inertly rather than being
        filtered, and it matches nothing.
        """
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
        seen, fake_run_loop = _spy_run_loop()

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

        _, fake_run_loop = _spy_run_loop(
            completed=False,
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
        _setup_project(tmp_path, allowed=None)
        assert (
            _preflight_component_scope(
                _manifest([_component()]),
                tmp_path,
                _base_config(tmp_path),
            )
            == []
        )

    @pytest.mark.parametrize(
        ("entry", "hazard"),
        [
            ("/abs/src/", "absolute"),
            ("../outside/", "traversal"),
            (".", "root"),
            ("./", "root"),
            ("/", "root"),
            (" src/", "whitespace"),
            ("src/ ", "whitespace"),
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

    @pytest.mark.parametrize("entry", ["/abs/src/", "../outside/", ".", " src/"])
    def test_the_classifier_agrees_with_the_matcher(self, entry: str) -> None:
        """The predicate and the matcher must judge the SAME string.

        Classifying ``entry.strip()`` while ``path_is_allowed`` matched
        the raw entry let `` src/`` pass as safe and authorise nothing,
        in a check whose entire job is catching exactly that (#268
        review).
        """
        assert not path_is_allowed("src/document.py", [entry])

    def test_every_hazard_code_has_a_sentence_in_both_consumers(self) -> None:
        """The mechanism the shared predicate was missing.

        Adding "whitespace" in the #268 review meant editing three files
        by hand, and nothing would have failed if one had been missed:
        the operator or the architect would have got a KeyError or a
        silently unhandled case. Literal alone does not force
        exhaustiveness, so this asserts it.
        """
        codes = set(get_args(ScopeHazard))
        assert codes == set(_SCOPE_HAZARD_REASONS)
        assert codes == set(_SCOPE_HAZARD_ADVICE)

    @pytest.mark.parametrize(
        ("entry", "reason"),
        [
            ("{root}/src/writers_room/", "absolute path"),
            (" src/writers_room/", "whitespace"),
            ("../src/writers_room/", "traverses outside"),
        ],
    )
    def test_an_unmatchable_authored_entry_is_refused(
        self,
        tmp_path: Path,
        entry: str,
        reason: str,
    ) -> None:
        """The hand-written-manifest backstop. decompose validates
        architect output; nothing validated a hand-edited PRD, so an
        entry like this silently authorised nothing and every file it
        was meant to cover became a violation - the same unwinnable
        loop, from the other end."""
        _setup_project(tmp_path, allowed=[entry.format(root=tmp_path)])
        errors = _preflight_component_scope(
            _manifest([_component()]),
            tmp_path,
            _base_config(tmp_path),
        )
        assert len(errors) == 1
        assert "allowedPaths entry" in errors[0]
        assert reason in errors[0]
        assert f"plus harness artifacts: {', '.join(HARNESS)}" in errors[0]

    def test_one_bad_entry_is_reported_once(self, tmp_path: Path) -> None:
        """_component_scope falls back to the run-wide --allowed-paths
        flag, so one bad entry there is shared by every component and
        must not repeat its paragraph once per component."""
        _setup_project(tmp_path, allowed=None)
        base = _base_config(tmp_path)
        base.allowed_paths = ["/abs/src/"]
        manifest = _manifest([_component(), _component(), _component()])
        assert len(_preflight_component_scope(manifest, tmp_path, base)) == 1

    def test_an_absolute_harness_path_is_supported_not_refused(
        self,
        tmp_path: Path,
    ) -> None:
        """config.reconcile_progress_config documents an absolute
        progress path outside root as SUPPORTED and self-consistent:
        joining it onto a worktree is a no-op, so writer and reader land
        on one shared file in the main checkout. Such a file is outside
        every worktree, never appears in a component's git diff, and so
        can never BE a scope violation. The preflight refused it until
        the #268 review; two docstrings in one codebase said opposite
        things.
        """
        _setup_project(tmp_path)
        base = _base_config(tmp_path)
        base.progress_file = Path("/var/log/shared/progress.txt")
        base.codebase_map_file = Path("/elsewhere/codebase_map.md")
        assert _preflight_component_scope(_manifest([_component()]), tmp_path, base) == []

    def test_run_factory_refuses_without_paying_for_an_engineer_call(
        self,
        tmp_path: Path,
    ) -> None:
        """The whole point: $14.49 and 41 minutes become exit 2 in the
        first second."""
        _setup_project(tmp_path, allowed=[f"{tmp_path}/src/writers_room/"])
        base = _base_config(tmp_path)
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
# The standalone paths, not only the factory
# ---------------------------------------------------------------------------


class TestStandaloneLoops:
    """`ks run` and `ks understand` write the same harness files, so a
    carve-out only the factory applied would leave #264 reproducible on
    the commands a first-time user reaches for first.
    """

    def test_ks_run_carries_the_carve_out_through_the_factory(
        self,
        tmp_path: Path,
    ) -> None:
        """`ks run` is a single-component factory invocation
        (Manifest.from_prd -> run_factory), so it reaches
        _run_component and gets the carve-out. Driven here the way the
        CLI drives it, with the run-wide --allowed-paths flag and a PRD
        that carries no allowedPaths of its own, which is exactly the
        `ks run --allowed-paths src/` shape.
        """
        _setup_project(tmp_path, allowed=None)
        base = _base_config(tmp_path)
        base.allowed_paths = ["src/writers_room/"]
        seen, fake_run_loop = _spy_run_loop()

        with (
            patch("kstrl.loop.run_loop", side_effect=fake_run_loop),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
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
                PlainUI(no_color=True, file=io.StringIO()),
                tmp_path,
            )

        assert seen, "the engineer loop never ran"
        assert seen[0] == HARNESS
        assert path_is_allowed(PRD_REL, [*base.allowed_paths, *seen[0]])
        assert path_is_allowed(MAP_REL, [*base.allowed_paths, *seen[0]])

    def test_ks_understand_carries_the_carve_out(self, tmp_path: Path) -> None:
        """`ks understand` calls run_loop directly. Its default
        allowed_paths already names the codebase map, but an operator
        who passes --allowed-paths REPLACES that default, and the
        in-loop guard then reverts the one file the understand prompt
        calls the only file the agent may edit.
        """
        _setup_project(tmp_path)
        config = _base_config(tmp_path)
        config.allowed_paths = ["src/writers_room/"]
        config.codebase_map_file = tmp_path / MAP_REL
        seen, fake_run_loop = _spy_run_loop()

        run = CommandRun(
            run_id="run-test",
            kind="understand",
            bus=EventBus(run_id="run-test"),
            paths=None,
        )
        with patch("kstrl.cli.run_loop", side_effect=fake_run_loop):
            _understand_core(
                config,
                cast(Any, None),
                tmp_path,
                PlainUI(no_color=True, file=io.StringIO()),
                run=run,
            )

        assert seen, "the understand loop never ran"
        assert MAP_REL in seen[0]
        assert path_is_allowed(MAP_REL, [*config.allowed_paths, *seen[0]])


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
