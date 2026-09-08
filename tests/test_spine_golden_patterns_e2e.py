"""R10.8 end to end: `ks init` writes it, `ks run` decides whether to send it.

BLOCKER 1 of review round 1 was reproduced exactly this way and not by a
unit test, because the defect lived in the JOIN between two correct
halves: `ks init` wrote a skeleton that was not absent, not empty and not
whitespace-only, and the loader suppressed only those three. Both halves
had tests; nothing ran them together, and the measured result was 479
characters of angle-bracket placeholders at the head of every engineer
prompt of every component of every attempt of every newly initialised
project, under a header asserting the operator had authored them.

So this file runs the real ``run_init``, the real ``ks run`` command
group, and a real agent subprocess whose whole job is to write its stdin
to a file. Nothing here is mocked. ``ks run`` forces
``use_worktrees=False`` and ``create_prs=False``
(``kstrl/cli.py``), which is why one temp repo is enough.
"""

from __future__ import annotations

import io
import json
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.init_cmd import run_init
from kstrl.ui.plain import PlainUI
from tests.spine_utils import git

pytestmark = pytest.mark.spine

GOLDEN_REL = "scripts/kstrl/golden-patterns.md"
MEMORY_REL = "scripts/kstrl/memory.md"
PRD = {
    "branchName": "kstrl/golden",
    "userStories": [
        {
            "id": "US-001",
            "title": "Do the thing",
            "acceptanceCriteria": ["it is done"],
            "priority": 1,
            "passes": False,
            "notes": "",
        }
    ],
}


def _initialised_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "e2e@test", cwd=root)
    git("config", "user.name", "E2E Test", cwd=root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "README.md", cwd=root)
    git("commit", "-q", "-m", "seed", cwd=root)

    buffer = io.StringIO()
    assert run_init(root, PlainUI(no_color=True, file=buffer)) == 0
    (root / "scripts" / "kstrl" / "prd.json").write_text(
        json.dumps(PRD, indent=2) + "\n", encoding="utf-8"
    )
    # Phase 2 off, and this is not a speed optimisation. `--agent-cmd`
    # configures the ENGINEER; the reviewer keeps the default agent, so
    # a `ks run` here calls a real LLM. Measured on the first draft of
    # this file: 81 of its 83 seconds, and $0.51 of real spend, in the
    # review phase, for a role that says nothing about golden patterns.
    toml = root / "kstrl.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            "# review_mode =", 'review_mode = "skip"\n# review_mode =', 1
        ),
        encoding="utf-8",
    )
    return root


def _captured_prompts(root: Path, capture: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run `ks run 1` with an agent that records every prompt it is given.

    APPEND, not overwrite. One `ks run` calls the same ``--agent-cmd``
    for more than one role, so a truncating capture holds whichever call
    ran last, and the first version of this test asserted against the
    knowledge distiller's prompt instead of the engineer's. Appending
    also makes the negative assertion stronger: the block appears in NO
    prompt of the run, not merely in the last one.

    Knowledge distillation is switched off because it is the slowest
    role here and says nothing about golden patterns.
    """
    agent = f"cat >> {shlex.quote(str(capture))}; printf '<promise>COMPLETE</promise>\\n'"
    monkeypatch.chdir(root)
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    result = CliRunner().invoke(
        cli,
        ["run", "1", "--sleep", "0", "--no-verify", "--branch", "", "--agent-cmd", agent],
    )
    assert capture.exists(), f"the agent never ran: {result.output}"
    return capture.read_text(encoding="utf-8")


class TestTheScaffoldGoesNowhereUntilItIsEdited:
    def test_a_fresh_project_sends_no_golden_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _initialised_project(tmp_path)
        scaffold = (root / GOLDEN_REL).read_text(encoding="utf-8")
        assert "<pattern>: see" in scaffold, "ks init stopped writing the skeleton"

        prompt = _captured_prompts(root, tmp_path / "prompt.txt", monkeypatch)

        assert "=== GOLDEN PATTERNS" not in prompt
        # Not just the delimiter: none of the skeleton's own text either.
        assert "<pattern>: see" not in prompt
        assert "## Follow these" not in prompt

    def test_an_edited_file_is_sent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _initialised_project(tmp_path)
        (root / GOLDEN_REL).write_text(
            "# Golden patterns\n\n## Follow these\n\n- atomic writes: see `kstrl/atomicio.py`\n",
            encoding="utf-8",
        )

        prompt = _captured_prompts(root, tmp_path / "prompt.txt", monkeypatch)

        assert "=== GOLDEN PATTERNS (operator-authored) KSTRL-DATA-" in prompt
        assert "- atomic writes: see `kstrl/atomicio.py`" in prompt

    def test_one_appended_line_to_the_scaffold_is_sent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The realistic edit: the operator keeps the headings and fills
        one bullet in. The digest changes, so the whole file goes."""
        root = _initialised_project(tmp_path)
        with (root / GOLDEN_REL).open("a", encoding="utf-8") as handle:
            handle.write("- atomic writes: see `kstrl/atomicio.py`\n")

        prompt = _captured_prompts(root, tmp_path / "prompt.txt", monkeypatch)

        assert "=== GOLDEN PATTERNS (operator-authored) KSTRL-DATA-" in prompt
        assert "- atomic writes: see `kstrl/atomicio.py`" in prompt


class TestTheMemoryScaffoldGoesNowhereUntilItIsEdited:
    """R10.9 through the shipped CLI path, not through `_run_component`.

    Same join as the class above and the same reason for testing it here:
    `ks init` writes a body that is not absent, not empty and not
    whitespace-only, and only the ledger row stops it reaching the
    engineer. Both halves have unit tests; this runs them together.
    """

    def test_a_fresh_project_sends_no_memory_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _initialised_project(tmp_path)
        scaffold = (root / MEMORY_REL).read_text(encoding="utf-8")
        assert "## Guidance" in scaffold, "ks init stopped writing the skeleton"

        prompt = _captured_prompts(root, tmp_path / "prompt.txt", monkeypatch)

        assert "=== MEMORY" not in prompt
        assert "Standing feedback for kstrl runs" not in prompt

    def test_an_edited_file_reaches_the_engineer_after_the_claude_md_check(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`ks run` is the second command that reads the file, and the
        acceptance criterion names it. The retry context is absent on
        attempt 1 of a one-iteration run, so the ORDER assertion lives in
        tests/test_spine_engineer_loop.py where a retry context can be
        constructed; what this pins is that the block arrives at all
        through the real CLI, and lands before the CLAUDE.md prepend.
        """
        root = _initialised_project(tmp_path)
        with (root / MEMORY_REL).open("a", encoding="utf-8") as handle:
            handle.write("- never touch the migrations directory\n")

        prompt = _captured_prompts(root, tmp_path / "prompt.txt", monkeypatch)

        assert "=== MEMORY (standing feedback) KSTRL-DATA-" in prompt
        assert "- never touch the migrations directory" in prompt
        assert prompt.index("=== END MEMORY (standing feedback) KSTRL-DATA-") < prompt.index(
            "# Project Context (from CLAUDE.md)"
        )
