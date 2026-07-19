"""Tests for the SpecKit artifact-set intake (R7.5).

``ralph decompose --spec`` accepts a SpecKit directory
(spec.md [+ plan.md] [+ tasks.md]); the artifacts are concatenated with
visible provenance headers and flow through the ordinary
injection-separation delimiters. The EARS-directive DETECTION quality
is a calibration concern (H2: the user runs the calibration commands);
these tests pin the intake mechanics and the prompt's directive text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_py.decompose import (
    DECOMPOSE_PROMPT,
    decompose_spec,
    load_spec_input,
)
from ralph_py.ui.plain import PlainUI
from tests.test_decompose import (
    SequenceAgent,
    _single_component_output,
    _story,
)


def _speckit_dir(tmp_path: Path, artifacts: dict[str, str]) -> Path:
    spec_dir = tmp_path / "specs" / "001-feature"
    spec_dir.mkdir(parents=True)
    for name, content in artifacts.items():
        (spec_dir / name).write_text(content)
    return spec_dir


class TestLoadSpecInput:
    def test_plain_file_reads_verbatim(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# My spec\n\nbody\n")
        assert load_spec_input(spec) == "# My spec\n\nbody\n"

    def test_full_artifact_set_concatenates_in_order(
        self, tmp_path: Path,
    ) -> None:
        spec_dir = _speckit_dir(tmp_path, {
            "spec.md": "SPEC-BODY",
            "plan.md": "PLAN-BODY",
            "tasks.md": "TASKS-BODY",
        })
        content = load_spec_input(spec_dir)
        for body in ("SPEC-BODY", "PLAN-BODY", "TASKS-BODY"):
            assert body in content
        assert content.index("SPEC-BODY") < content.index("PLAN-BODY")
        assert content.index("PLAN-BODY") < content.index("TASKS-BODY")
        # Every artifact is attributed via a visible provenance header.
        assert "SpecKit artifact: spec.md" in content
        assert "SpecKit artifact: plan.md" in content
        assert "SpecKit artifact: tasks.md" in content

    def test_spec_only_directory(self, tmp_path: Path) -> None:
        spec_dir = _speckit_dir(tmp_path, {"spec.md": "SPEC-ONLY"})
        content = load_spec_input(spec_dir)
        assert "SPEC-ONLY" in content
        assert "SpecKit artifact: spec.md" in content
        assert "plan.md" not in content
        assert "tasks.md" not in content

    def test_directory_without_spec_md_is_rejected(
        self, tmp_path: Path,
    ) -> None:
        spec_dir = _speckit_dir(tmp_path, {"plan.md": "PLAN-ONLY"})
        with pytest.raises(ValueError, match="no.*spec\\.md"):
            load_spec_input(spec_dir)

    def test_missing_path_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            load_spec_input(tmp_path / "ghost")


class TestEarsDirective:
    def test_prompt_demands_ears_acceptance_criteria(self) -> None:
        """Pin the directive text (the detection-quality claim belongs
        to calibration, not this test)."""
        assert 'EARS form: "WHEN <condition> THE SYSTEM' in DECOMPOSE_PROMPT
        assert "SHALL <behavior>" in DECOMPOSE_PROMPT
        # The JSON example itself models the form.
        assert "WHEN <typical valid input or trigger> THE SYSTEM SHALL" in (
            DECOMPOSE_PROMPT
        )


class TestDecomposeSpecIntegration:
    def test_speckit_directory_reaches_the_architect_prompt(
        self, tmp_path: Path,
    ) -> None:
        spec_dir = _speckit_dir(tmp_path, {
            "spec.md": "SPEC-BODY-UNIQUE",
            "plan.md": "PLAN-BODY-UNIQUE",
            "tasks.md": "TASKS-BODY-UNIQUE",
        })
        root = tmp_path / "project"
        root.mkdir()
        agent = SequenceAgent([_single_component_output([_story()])])

        manifest = decompose_spec(
            spec_path=spec_dir,
            project_name="speckit-proj",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=root,
        )

        assert len(manifest.components) == 1
        prompt = agent.prompts[0]
        for body in (
            "SPEC-BODY-UNIQUE", "PLAN-BODY-UNIQUE", "TASKS-BODY-UNIQUE",
        ):
            assert body in prompt
        # The artifact set sits INSIDE the injection-separation
        # delimiters: data, not instructions.
        begin = prompt.index("BEGIN SPECIFICATION")
        end = prompt.index("END SPECIFICATION")
        assert begin < prompt.index("SPEC-BODY-UNIQUE") < end
        assert begin < prompt.index("TASKS-BODY-UNIQUE") < end
