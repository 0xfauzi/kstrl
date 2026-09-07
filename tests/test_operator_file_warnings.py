"""The operator hears about their own context file, once (#229 S6/S7).

``operator_context``'s ``logger.warning`` runs inside a pool worker.
There is no ``basicConfig`` anywhere in ``kstrl/``, so it goes to
``logging.lastResort`` on stderr, and ``factory`` dup2s the worker's
stderr into ``.kstrl/runs/<id>/components/<id>/engineer.log``. Measured
in review round 1: a truncated or unreadable golden-patterns file left
no mark on the terminal, the TUI, the event stream or the PR body.

So the notice is derived in the PARENT, where a ``ui`` exists. Once per
RUN and not once per component, which is only possible because the
loader reads the repo root: while the worker resolved its own worktree
first, there was no path the parent could read that every worker was
guaranteed to agree with.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl import operator_context
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.init_cmd import DEFAULT_GOLDEN_PATTERNS
from kstrl.manifest import Component, Manifest
from kstrl.operator_context import GOLDEN_PATTERNS_SUBJECT
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

GOLDEN_REL = "scripts/kstrl/golden-patterns.md"


class RecordingUI(PlainUI):
    """A PlainUI that keeps every warning it was asked to print."""

    def __init__(self) -> None:
        super().__init__(no_color=True)
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _project(tmp_path: Path, component_ids: tuple[str, ...]) -> Path:
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("test prompt", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}', encoding="utf-8"
    )
    for comp_id in component_ids:
        feature = kstrl_dir / "feature" / comp_id
        feature.mkdir(parents=True)
        (feature / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    return tmp_path


def _manifest(component_ids: tuple[str, ...]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="proj",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=comp_id,
                title=f"Component {comp_id}",
                description="d",
                dependencies=[],
                prd_path=f"scripts/kstrl/feature/{comp_id}/prd.json",
                branch_name=f"kstrl/{comp_id}",
            )
            for comp_id in component_ids
        ],
    )


def _run(root: Path, component_ids: tuple[str, ...], golden: Path | None = None) -> RecordingUI:
    factory_config = FactoryConfig(
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
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig.anchored(root)
    base.sleep_seconds = 0
    base.agent_cmd = "echo test"
    base.kstrl_branch = ""
    base.kstrl_branch_explicit = True
    if golden is not None:
        base.golden_patterns_file = golden
    ui = RecordingUI()

    def done(*args: Any, **kwargs: Any) -> ComponentResult:
        return ComponentResult(str(args[0]), success=True, iterations=1)

    with (
        patch("kstrl.factory._run_component", side_effect=done),
        patch("kstrl.git.get_diff_content", return_value=""),
    ):
        run_factory(_manifest(component_ids), factory_config, base, ui, root)
    return ui


def _golden_warnings(ui: RecordingUI) -> list[str]:
    return [w for w in ui.warnings if "Golden patterns" in w]


class TestTheNoticeReachesTheOperatorExactlyOnce:
    def test_a_truncated_file_warns_once_for_a_three_component_run(
        self,
        tmp_path: Path,
    ) -> None:
        """The count is the finding. Warning from the loader would give
        one per component per iteration, into a log nobody opens."""
        root = _project(tmp_path, ("comp-a", "comp-b", "comp-c"))
        (root / GOLDEN_REL).write_text(("x" * 19 + "\n") * 500, encoding="utf-8")

        warnings = _golden_warnings(_run(root, ("comp-a", "comp-b", "comp-c")))

        assert len(warnings) == 1
        assert "truncated:" in warnings[0]
        assert "of 10000 characters shown" in warnings[0]
        assert GOLDEN_REL in warnings[0]

    def test_an_unreadable_file_warns_once(self, tmp_path: Path) -> None:
        root = _project(tmp_path, ("comp-a",))
        (root / GOLDEN_REL).mkdir(parents=True)

        warnings = _golden_warnings(_run(root, ("comp-a",)))

        assert len(warnings) == 1
        assert "could not read" in warnings[0]

    def test_a_misspelled_explicit_path_warns_once_and_names_it(self, tmp_path: Path) -> None:
        """S7. The loader is handed a path and cannot tell an explicit
        setting from the default, so the check lives where the config is."""
        root = _project(tmp_path, ("comp-a",))
        typo = root / "scripts" / "kstrl" / "gloden-patterns.md"

        warnings = _golden_warnings(_run(root, ("comp-a",), golden=typo))

        assert len(warnings) == 1
        assert str(typo) in warnings[0]
        assert "golden_patterns" in warnings[0]

    def test_a_programmatically_built_config_is_not_accused_of_a_typo(
        self,
        tmp_path: Path,
    ) -> None:
        """``KstrlConfig()`` leaves its path defaults RELATIVE, and the
        SDK, embedders and most of this suite build one that way. The
        misconfigured-path check therefore has to resolve both sides
        against the root before comparing them; comparing the raw values
        made every such run warn about its own untouched default."""
        root = _project(tmp_path, ("comp-a",))
        unanchored = KstrlConfig()
        assert not unanchored.golden_patterns_file.is_absolute()

        warnings = _golden_warnings(_run(root, ("comp-a",), golden=unanchored.golden_patterns_file))

        assert warnings == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses file permissions",
    )
    def test_a_mode_000_parent_directory_warns_once_and_the_run_finishes(
        self,
        tmp_path: Path,
    ) -> None:
        """Review round 2's blocker, at the level it escaped from.

        The tests above chmod the FILE, whose ``os.stat`` succeeds and
        whose ``open`` raises inside the guard. A mode-000 PARENT is the
        case ``Path.exists`` re-raises (EACCES is not in CPython's
        ``pathlib._ignore_error`` set), and the pre-check that used to
        sit outside the guard let it out of ``run_factory`` and out of
        the CLI: exit 1, and no component ever started.
        """
        root = _project(tmp_path, ("comp-a",))
        locked = root / "scripts" / "kstrl" / "locked"
        locked.mkdir()
        (locked / "g.md").write_text("- a real pattern\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
            warnings = _golden_warnings(_run(root, ("comp-a",), golden=locked / "g.md"))
        finally:
            locked.chmod(0o755)

        assert len(warnings) == 1
        assert "could not read" in warnings[0]

    def test_a_name_the_filesystem_will_not_take_warns_once_and_the_run_finishes(
        self,
        tmp_path: Path,
    ) -> None:
        """The other errno ``Path.exists`` re-raises: ENAMETOOLONG."""
        root = _project(tmp_path, ("comp-a",))
        too_long = root / "scripts" / "kstrl" / ("g" * 400 + ".md")

        warnings = _golden_warnings(_run(root, ("comp-a",), golden=too_long))

        assert len(warnings) == 1
        assert "could not read" in warnings[0]

    def test_the_subject_comes_off_the_row_and_not_off_the_printer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should-fix 5: ``_report_operator_files`` used to print the
        literal ``Golden patterns:`` in front of every message, so
        R10.9's memory file would have announced itself under the
        golden-patterns name the day its row landed.

        The subject is MOVED for this case rather than merely read back.
        Asserting the shipped subject appears would pass against a
        hardcoded printer, which is the defect: it is the same shape as
        the guard that clears on everything (CLAUDE.md, rule 2).
        """
        root = _project(tmp_path, ("comp-a",))
        (root / GOLDEN_REL).write_text(("x" * 19 + "\n") * 500, encoding="utf-8")
        monkeypatch.setattr(operator_context, "GOLDEN_PATTERNS_SUBJECT", "Second row")

        ui = _run(root, ("comp-a",))

        assert [w for w in ui.warnings if "truncated:" in w] == [
            w for w in ui.warnings if w.startswith("  Second row: ")
        ]
        assert GOLDEN_PATTERNS_SUBJECT not in " ".join(ui.warnings)

    @pytest.mark.parametrize(
        "body",
        [None, "", "- a real pattern\n", DEFAULT_GOLDEN_PATTERNS],
        ids=["absent", "empty", "written", "unedited-scaffold"],
    )
    def test_the_ordinary_states_say_nothing(self, tmp_path: Path, body: str | None) -> None:
        """Four states an operator should never be nagged about. The last
        is the one that matters: an untouched `ks init` scaffold is the
        default state of every new project, and a warning there would
        train the operator to ignore the warnings that do matter."""
        root = _project(tmp_path, ("comp-a",))
        if body is not None:
            (root / GOLDEN_REL).write_text(body, encoding="utf-8")

        assert _golden_warnings(_run(root, ("comp-a",))) == []
