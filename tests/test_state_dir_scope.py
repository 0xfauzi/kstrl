"""kstrl's own state directory is carved out at the guard (#274).

The in-loop scope guard counts UNTRACKED files against a component's
``allowedPaths``. kstrl writes its run journals, locks, queue and
worktrees into ``<root>/.kstrl/`` WHILE that walk is happening, so in a
repository that does not ignore ``.kstrl/`` the harness trips over its
own artifacts and the operator pays an engineer iteration for it.

``ks init`` scaffolds a ``.gitignore`` carrying ``.kstrl/`` (#273), which
cannot reach a repository that already exists. This carve-out travels
with the harness instead, and the tests here pin the four things that
keep it from becoming the blanket bypass ``check_violations`` warns
against:

1. It names only the entries kstrl itself creates. ``.kstrl/notes.md``
   is still a violation, so an agent cannot invent a hiding place under
   the state directory.
2. The enumeration is checked against the CODE, not maintained by hand:
   ``TestTheEnumerationMatchesTheCode`` AST-walks ``kstrl/`` for the
   ``.kstrl/<entry>`` names the package spells out and fails on one that
   is missing. That scan is what found ``snapshots`` and the legacy
   control files while this change was being written, and one test pins
   the spellings it CANNOT see so the net is not sold as more than it is.
3. It is empty unless the tree the guard walks is the same directory as
   the state root the CALLER declares. A component worktree therefore
   gets nothing, because a ``.kstrl/`` there can only be the agent's.
4. It is reported apart from the operator's authored allowlist in the
   guard's failure block.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl import git, guards, statedir
from kstrl.config import KstrlConfig
from kstrl.guards import check_violations, path_is_allowed
from kstrl.loop import COMPLETION_MARKER, run_loop
from kstrl.statedir import STATE_FILES, STATE_SUBDIRS, state_dir_carve_out
from kstrl.ui.plain import PlainUI
from kstrl.verify import _diff_scope_details, check_diff_scope
from tests.test_loop import MockAgent

PROJECT = Path("/project")
WORKTREE = PROJECT / ".kstrl" / "worktrees" / "run-1" / "comp-a"

# The authored scope: product code only, as an architect writes it.
AUTHORED = ["src/", "tests/"]

# One file per entry kstrl creates under the state directory, spelled the
# way `git ls-files --others` reports it. Written to disk by the `repo`
# fixture, so the real-git tests measure the real walk.
STATE_ARTIFACTS = (
    ".kstrl/autonomy.json",
    ".kstrl/contract/merge-ab12/README.md",
    ".kstrl/control_relocated",
    ".kstrl/debug/run-1/comp-a/prompt.txt",
    ".kstrl/evolution.jsonl",
    ".kstrl/experiments.tsv",
    ".kstrl/factory.lock",
    ".kstrl/inbox.jsonl",
    ".kstrl/knowledge/comp-a/run-1/fact.md",
    ".kstrl/logs/feature_x/understand.log",
    ".kstrl/progress.jsonl",
    ".kstrl/proposals/p1.json",
    ".kstrl/queue/new/item-1/item.json",
    ".kstrl/queue/pause.json",
    ".kstrl/runs/run-1/events.jsonl",
    ".kstrl/snapshots/fixture-1.json",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed repository with NO ``.gitignore`` at all.

    The case this change exists for, and the one PR #273 cannot reach: a
    project scaffolded before the ``.gitignore`` shipped, or one whose
    operator curates their own. Nothing here is ignored, so every file
    kstrl writes under ``.kstrl/`` reaches the guard's walk.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    assert not (root / ".gitignore").exists()
    for rel in STATE_ARTIFACTS:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    return root


# ---------------------------------------------------------------------------
# The carve-out itself
# ---------------------------------------------------------------------------


class TestStateDirCarveOut:
    def test_it_names_the_entries_kstrl_creates(self) -> None:
        assert state_dir_carve_out(PROJECT, PROJECT) == [
            ".kstrl/autonomy.json",
            ".kstrl/contract/",
            ".kstrl/control_relocated",
            ".kstrl/debug/",
            ".kstrl/evolution.jsonl",
            ".kstrl/experiments.tsv",
            ".kstrl/factory.lock",
            ".kstrl/inbox.jsonl",
            ".kstrl/knowledge/",
            ".kstrl/logs/",
            ".kstrl/progress.jsonl",
            ".kstrl/proposals/",
            ".kstrl/queue/",
            ".kstrl/runs/",
            ".kstrl/snapshots/",
            ".kstrl/worktrees/",
        ]

    def test_the_state_directory_itself_is_never_an_entry(self) -> None:
        """The whole difference between this and the bypass the
        ``check_violations`` docstring refuses. ``.kstrl/`` as a bare
        prefix would authorise anything under it;
        ``decompose._ALLOWED_PATHS_EXCLUDE`` will not even let an
        architect write that entry into ``allowedPaths``."""
        entries = state_dir_carve_out(PROJECT, PROJECT)
        assert ".kstrl/" not in entries
        assert ".kstrl" not in entries
        for invented in (
            ".kstrl/notes.md",
            ".kstrl/payload.py",
            ".kstrl/.env",
            ".kstrl/runs.txt",
        ):
            assert not path_is_allowed(invented, entries), invented

    def test_a_lookalike_directory_is_not_covered(self) -> None:
        entries = state_dir_carve_out(PROJECT, PROJECT)
        assert not path_is_allowed(".kstrl-backup/runs/x", entries)
        assert not path_is_allowed("sub/.kstrl/runs/x", entries)
        assert not path_is_allowed("kstrl/factory.py", entries)

    def test_the_legacy_in_tree_control_files_are_covered(self) -> None:
        """R8.9 moved these to XDG state, but a repository that has not
        migrated still has them in the tree, and ``migrate_control_state``
        only moves them once somebody runs a command that reads control
        state. Derived from ``legacy_control_paths`` rather than copied,
        so a control file added later is covered without a second edit."""
        entries = state_dir_carve_out(PROJECT, PROJECT)
        legacy = statedir.legacy_control_paths(PROJECT)
        for path in legacy.values():
            rel = path.relative_to(PROJECT).as_posix()
            assert path_is_allowed(rel, entries), rel

    def test_a_walk_root_that_is_not_the_state_root_gets_nothing(self) -> None:
        """The tightening, and the reason the function takes both paths.

        A component worktree is a different directory from the project
        root, and kstrl writes its journals, locks and queue only at the
        root - so a ``.kstrl/`` inside a worktree can only be the
        agent's. Carving it out there would hide files and clear
        nothing.
        """
        assert state_dir_carve_out(WORKTREE, PROJECT) == []
        assert state_dir_carve_out(PROJECT, WORKTREE) == []
        assert state_dir_carve_out(Path("/other"), PROJECT) == []

    def test_an_undeclared_state_root_gets_nothing(self) -> None:
        """The safe default. A caller that does not declare where its
        state directory lives gets the pre-#274 behaviour, which fails
        loudly on kstrl's own artifacts - never a carve-out applied to a
        tree kstrl does not own."""
        assert state_dir_carve_out(PROJECT, None) == []

    def test_the_two_paths_are_compared_as_directories_not_strings(
        self,
        tmp_path: Path,
    ) -> None:
        """macOS reaches ``/tmp`` through a symlink and the loop's
        ``cwd`` routinely arrives spelled differently from the project
        root it was derived from, so a string comparison would silently
        disable the carve-out on the platform this is developed on."""
        real = tmp_path / "project"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        assert state_dir_carve_out(link, real) != []
        assert state_dir_carve_out(real / "." / "sub" / "..", real) != []


# ---------------------------------------------------------------------------
# The enumeration is checked against the code, not maintained by hand
# ---------------------------------------------------------------------------


_EMBEDDED = re.compile(r"\.kstrl/([A-Za-z0-9_][A-Za-z0-9_.-]*)")

#: Entries a caller may name under the state directory that kstrl does
#: NOT create there. ``control.lock`` lives in the XDG control directory
#: (``statedir.control_dir``), never in the tree, so carving it out would
#: authorise a path the harness never writes.
_NOT_IN_TREE = frozenset({statedir.CONTROL_LOCK_FILENAME})


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments."""
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                out[target.id] = node.value.value
    return out


def _is_state_anchor(node: ast.expr) -> bool:
    """Whether ``node`` evaluates to the state directory itself."""
    if isinstance(node, ast.Constant) and node.value == statedir.STATE_DIR_NAME:
        return True
    if isinstance(node, ast.Name) and node.id == "STATE_DIR_NAME":
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == "state_dir"
        if isinstance(func, ast.Attribute):
            return func.attr == "state_dir"
    return False


def _named_entries(source: str) -> set[str]:
    """Every ``.kstrl/<entry>`` name this module's CODE spells out.

    Two forms, because those are the two the package uses: a ``/`` join
    off the state directory (``root / ".kstrl" / "runs"``,
    ``state_dir(root) / QUEUE_DIR_NAME``), and a literal ``.kstrl/...``
    inside a string (config defaults such as
    ``Path(".kstrl/snapshots")``, and ``policy.py``'s glob patterns).

    Deliberately no more than that. A local alias, an f-string or an
    ``os.path.join`` would slip past, so this is a net under the current
    idiom rather than a proof about every possible one - which is what
    ``STATE_SUBDIRS``'s own comment says, so the claim and the mechanism
    agree.
    """
    tree = ast.parse(source)
    constants = _module_constants(tree)
    # Strings used as a bare statement are docstrings, and a comment
    # about a directory kstrl archived in 2026 is not a directory kstrl
    # writes: without this the scan reported `.kstrl/archive/` from a
    # two-line comment in evolution.py. Collected in the SAME walk as
    # the joins below rather than in a pass of its own - measured at
    # 85ms per package walk, and there are two walkers here.
    prose: set[int] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            prose.add(id(node.value))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # ast.walk reaches every BinOp in a `a / b / c` chain, so
            # each one only has to look one step left for its anchor.
            left = node.left
            anchor = (
                left.right if isinstance(left, ast.BinOp) and isinstance(left.op, ast.Div) else left
            )
            if _is_state_anchor(anchor):
                following = node.right
                if isinstance(following, ast.Constant) and isinstance(following.value, str):
                    names.add(following.value)
                elif isinstance(following, ast.Name) and following.id in constants:
                    names.add(constants[following.id])
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in prose:
                names.update(match.group(1) for match in _EMBEDDED.finditer(node.value))
    return names


def _package_entries() -> dict[str, set[str]]:
    """Every state-dir entry ``kstrl/`` names, mapped to its modules.

    Module-scoped so the two tests below parse the package once
    between them rather than once each.
    """
    global _PACKAGE_ENTRIES
    if _PACKAGE_ENTRIES is None:
        found: dict[str, set[str]] = {}
        for module in sorted(Path(statedir.__file__).parent.rglob("*.py")):
            for name in _named_entries(module.read_text(encoding="utf-8")):
                found.setdefault(name, set()).add(module.name)
        _PACKAGE_ENTRIES = found
    return _PACKAGE_ENTRIES


_PACKAGE_ENTRIES: dict[str, set[str]] | None = None


class TestTheEnumerationMatchesTheCode:
    """The regression net that keeps the list honest.

    ``STATE_SUBDIRS`` and ``STATE_FILES`` are hand-written, and a
    hand-written list of what a package writes is exactly the thing that
    rots. This walks every module for the entries the code actually
    names and fails if one is not carved out, so a subtree added later
    reintroduces the untracked-file failure loudly rather than in a paid
    run.
    """

    def test_every_entry_the_package_names_is_carved_out(self) -> None:
        entries = state_dir_carve_out(PROJECT, PROJECT)
        missing = sorted(
            name
            for name in _package_entries()
            if name not in _NOT_IN_TREE and not path_is_allowed(f".kstrl/{name}", entries)
        )
        assert missing == [], (
            f"kstrl writes these under .kstrl/ but the carve-out does not "
            f"cover them: {missing}. Add each to "
            f"statedir.STATE_SUBDIRS or STATE_FILES."
        )

    def test_the_scan_would_notice_a_new_subtree(self) -> None:
        """The net itself, tested. Without this the scan could be
        silently matching nothing and every run would pass."""
        source = 'from pathlib import Path\nP = Path("/x") / ".kstrl" / "brand-new"\n'
        assert _named_entries(source) == {"brand-new"}
        assert not path_is_allowed(".kstrl/brand-new", state_dir_carve_out(PROJECT, PROJECT))

    def test_the_scan_reads_a_constant_rather_than_its_name(self) -> None:
        source = 'from pathlib import Path\nQ = "queue"\nP = Path("/x") / ".kstrl" / Q\n'
        assert _named_entries(source) == {"queue"}

    def test_the_scan_ignores_prose(self) -> None:
        source = '"""Wave 1 archived the old journals to .kstrl/archive/."""\n'
        assert _named_entries(source) == set()

    def test_the_scan_does_not_claim_to_see_every_spelling(self) -> None:
        """The limit of the net, pinned so the claim cannot drift.

        These four idioms would slip past. None appears in ``kstrl/``
        today, and ``STATE_SUBDIRS``'s comment says exactly this - the
        test exists so an edit that broadens the comment has to broaden
        the scan too, and an edit that broadens the scan makes a test
        here fail rather than passing silently.
        """
        assert _named_entries('S = state_dir(r)\nP = S / "cache"\n') == set()
        assert _named_entries('P = base / f".kstrl/{name}"\n') == set()
        assert _named_entries('P = os.path.join(root, ".kstrl", "cache")\n') == set()
        assert _named_entries('P = self.state_dir / "cache"\n') == set()

    def test_every_declared_entry_is_reachable(self) -> None:
        """The inverse: nothing in the lists that the package never
        writes. A carve-out entry with no writer is authorisation
        granted for nothing, which is the same defect the entries are
        meant to remove."""
        declared = set(STATE_SUBDIRS) | set(STATE_FILES)
        assert declared - set(_package_entries()) == set()


# ---------------------------------------------------------------------------
# A real repository with no .gitignore: the case #273 cannot reach
# ---------------------------------------------------------------------------


class TestRealRepositoryWithoutAGitignore:
    def test_without_the_carve_out_every_state_file_is_a_violation(
        self,
        repo: Path,
    ) -> None:
        """The bug, measured against real git rather than a fixture."""
        changed = git.get_changed_files(repo)
        violations = check_violations(changed, AUTHORED)
        assert sorted(violations) == sorted(STATE_ARTIFACTS)

    def test_with_the_carve_out_the_run_is_clean(self, repo: Path) -> None:
        changed = git.get_changed_files(repo)
        assert check_violations(changed, AUTHORED, state_dir_carve_out(repo, repo)) == []

    def test_a_registered_worktree_is_covered(self, repo: Path) -> None:
        """git reports a linked worktree as one directory entry with a
        trailing slash, not as its contents. Measured: without
        ``.kstrl/worktrees/`` in the carve-out that entry is a
        violation, so every factory run in an unignored repository trips
        on the worktree it just created."""
        _git(repo, "branch", "comp-a")
        _git(repo, "worktree", "add", "-q", ".kstrl/worktrees/run-1/comp-a", "comp-a")
        changed = git.get_changed_files(repo)
        assert any(f.startswith(".kstrl/worktrees/") for f in changed)
        assert check_violations(changed, AUTHORED, state_dir_carve_out(repo, repo)) == []

    def test_an_agent_file_under_the_state_dir_is_still_a_violation(
        self,
        repo: Path,
    ) -> None:
        """The hiding case. The carve-out stops the guard counting what
        kstrl wrote; it does not stop it counting what the agent wrote
        next to it."""
        (repo / ".kstrl" / "notes.md").write_text("x\n")
        (repo / ".kstrl" / "runs.txt").write_text("x\n")
        changed = git.get_changed_files(repo)
        assert check_violations(changed, AUTHORED, state_dir_carve_out(repo, repo)) == [
            ".kstrl/notes.md",
            ".kstrl/runs.txt",
        ]

    def test_it_never_creates_a_scope_where_none_was_configured(
        self,
        repo: Path,
    ) -> None:
        changed = git.get_changed_files(repo)
        assert check_violations(changed, [], state_dir_carve_out(repo, repo)) == []

    def test_product_code_outside_scope_is_still_caught(self, repo: Path) -> None:
        (repo / "pyproject.toml").write_text("x\n")
        changed = git.get_changed_files(repo)
        assert check_violations(changed, AUTHORED, state_dir_carve_out(repo, repo)) == [
            "pyproject.toml",
        ]


# ---------------------------------------------------------------------------
# Phase 1 is deliberately NOT given the carve-out
# ---------------------------------------------------------------------------


class TestPhase1KeepsSeeingTheStateDir:
    def test_a_committed_state_dir_file_still_fails_diff_scope(
        self,
        repo: Path,
    ) -> None:
        """``check_diff_scope`` judges ``git diff base...HEAD``: only
        what the agent COMMITTED. Nothing kstrl writes at ``<root>``
        appears in a component worktree's diff, so Phase 1 never needed
        the carve-out - and leaving it out is what keeps a ``.kstrl/``
        file the agent committed from riding into the PR. That is the
        backstop for the one iteration the in-loop guard now lets pass.
        """
        _git(repo, "checkout", "-q", "-b", "work")
        _git(repo, "add", "-A", "-f")
        _git(repo, "commit", "-q", "-m", "work")
        result = check_diff_scope(repo, "main", AUTHORED)
        assert result.passed is False
        assert ".kstrl/runs/run-1/events.jsonl" in "\n".join(result.details)


# ---------------------------------------------------------------------------
# The loop actually applies it, and only where the state dir is kstrl's
# ---------------------------------------------------------------------------


def _loop_config(root: Path, allowed: list[str]) -> KstrlConfig:
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}')
    return KstrlConfig(
        max_iterations=1,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        allowed_paths=allowed,
        kstrl_branch="",
        kstrl_branch_explicit=True,
    )


def _captured_ignored_paths(cwd: Path, config: KstrlConfig, **kwargs: Any) -> list[str]:
    """Run one loop iteration and return what the guard was handed."""
    seen: list[list[str]] = []

    def fake(*args: Any, **call_kwargs: Any) -> tuple[bool, list[str]]:
        seen.append(list(call_kwargs.get("ignored_paths") or ()))
        return True, []

    with patch.object(guards, "enforce_allowed_paths", side_effect=fake):
        run_loop(
            config,
            PlainUI(no_color=True),
            MockAgent(["working", COMPLETION_MARKER]),
            cwd,
            **kwargs,
        )
    assert len(seen) == 1
    return seen[0]


class TestTheLoopAppliesIt:
    def test_the_guard_is_handed_the_state_carve_out(self, repo: Path) -> None:
        captured = _captured_ignored_paths(
            repo,
            _loop_config(repo, AUTHORED),
            guard_state_root=repo,
        )
        assert captured == state_dir_carve_out(repo, repo)

    def test_the_callers_own_harness_files_come_first_and_survive(
        self,
        repo: Path,
    ) -> None:
        """#264's per-component files and #274's state directory are two
        carve-outs, not one. The loop unions them; neither replaces the
        other."""
        harness = ["scripts/kstrl/feature/comp-a/prd.json"]
        captured = _captured_ignored_paths(
            repo,
            _loop_config(repo, AUTHORED),
            guard_ignored_paths=harness,
            guard_state_root=repo,
        )
        assert captured == [*harness, *state_dir_carve_out(repo, repo)]

    def test_a_component_worktree_gets_only_the_callers_files(
        self,
        repo: Path,
    ) -> None:
        """The tightening, driven through the real loop.

        The factory hands ``cwd=<worktree>`` and
        ``guard_state_root=<root>``, so the loop adds nothing and an
        agent-written ``.kstrl/`` inside the worktree stays visible to
        the guard. The ``.kstrl/runs/<run_id>/`` entry this site used to
        append unconditionally, worktree or not, is gone with it.
        """
        _git(repo, "branch", "comp-a")
        _git(repo, "worktree", "add", "-q", ".kstrl/worktrees/run-1/comp-a", "comp-a")
        worktree = repo / ".kstrl" / "worktrees" / "run-1" / "comp-a"
        harness = ["scripts/kstrl/feature/comp-a/prd.json"]
        captured = _captured_ignored_paths(
            worktree,
            _loop_config(worktree, AUTHORED),
            guard_ignored_paths=harness,
            guard_state_root=repo,
        )
        assert captured == harness

    def test_a_caller_that_declares_nothing_gets_nothing(self, repo: Path) -> None:
        """No implicit default. The loop never guesses that its ``cwd``
        owns a state directory, so the carve-out cannot be applied to a
        tree the caller did not name."""
        captured = _captured_ignored_paths(repo, _loop_config(repo, AUTHORED))
        assert captured == []


# ---------------------------------------------------------------------------
# The four production callers declare it
# ---------------------------------------------------------------------------


class TestEveryCallerDeclaresTheStateRoot:
    """The cost of the safe default, paid once here.

    ``guard_state_root=None`` means "no carve-out", so a caller that
    forgets it gets the pre-#274 bug back rather than a carve-out
    applied to a tree kstrl does not own. That is the right failure
    direction, and it is only safe because something checks the callers
    actually pass it. This is that check, by source inspection: driving
    all four flows end to end would cost four agent harnesses to assert
    one keyword.
    """

    def test_the_four_run_loop_call_sites_pass_it(self) -> None:
        package = Path(statedir.__file__).parent
        callers: dict[str, int] = {}
        declared: dict[str, int] = {}
        for module in sorted(package.rglob("*.py")):
            if module.name == "loop.py":
                continue
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name != "run_loop":
                    continue
                callers[module.name] = callers.get(module.name, 0) + 1
                if any(kw.arg == "guard_state_root" for kw in node.keywords):
                    declared[module.name] = declared.get(module.name, 0) + 1
        assert callers == {"cli.py": 1, "factory.py": 1, "feature_cmd.py": 3}
        assert declared == callers


# ---------------------------------------------------------------------------
# Reported apart from the operator's authored allowlist
# ---------------------------------------------------------------------------


class TestTheFailureBlockSeparatesTheTwoSets:
    def test_it_names_what_the_operator_authorised_and_what_kstrl_added(
        self,
        repo: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (repo / "pyproject.toml").write_text("x\n")
        config = _loop_config(repo, AUTHORED)
        config.interactive = False
        ok, violations = guards.enforce_allowed_paths(
            config,
            PlainUI(no_color=True),
            repo,
            ignored_paths=state_dir_carve_out(repo, repo),
        )
        assert ok is False
        assert "pyproject.toml" in violations
        assert not any(v.startswith(".kstrl/") for v in violations)
        printed = capsys.readouterr()
        lines = (printed.out + printed.err).splitlines()
        allowed_line = next(line for line in lines if "ALLOWED_PATHS" in line)
        harness_line = next(line for line in lines if "HARNESS_PATHS" in line)
        assert allowed_line.endswith("src/, tests/")
        assert ".kstrl/runs/" in harness_line
        assert ".kstrl" not in allowed_line

    def test_it_makes_the_same_claim_the_other_two_guards_make(
        self,
        repo: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A retry agent must not be told two different things about the
        same files depending on which guard fired.

        Compared against what Phase 1 actually PRINTS, not against its
        source, so an edit to either wording breaks this rather than an
        edit to either layout. The factory's third copy of the sentence
        is pinned by ``tests/test_harness_path_scope.py``.
        """
        (repo / "pyproject.toml").write_text("x\n")
        config = _loop_config(repo, AUTHORED)
        config.interactive = False
        guards.enforce_allowed_paths(
            config,
            PlainUI(no_color=True),
            repo,
            ignored_paths=state_dir_carve_out(repo, repo),
        )
        printed = capsys.readouterr()
        claim = "already in scope, no need to widen allowedPaths"
        assert claim in printed.out + printed.err
        phase_1 = _diff_scope_details("main", AUTHORED, [".kstrl/runs/"], ["pyproject.toml"])
        assert claim in "\n".join(phase_1)

    def test_nothing_is_printed_when_there_is_no_carve_out(
        self,
        repo: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (repo / "pyproject.toml").write_text("x\n")
        config = _loop_config(repo, AUTHORED)
        config.interactive = False
        guards.enforce_allowed_paths(config, PlainUI(no_color=True), repo)
        printed = capsys.readouterr()
        assert "HARNESS_PATHS" not in printed.out + printed.err
