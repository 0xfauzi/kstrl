"""#261: what `ks init` scaffolds, and what the generated docs claim.

The run-time half of the contract lives in
``tests/test_verify_command_contract.py``. This module covers the
surfaces init writes and an operator reads: the generated CLAUDE.md,
the scaffolded ``kstrl.toml [verify]`` block, and the config reference
``scripts/gen_docs.py`` renders.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from kstrl.init_cmd import (
    _VERIFY_KEYS,
    DEFAULT_KSTRL_TOML,
    _generate_claude_md,
    kstrl_toml_for,
)
from kstrl.verify import (
    VerifyConfig,
    resolve_verify_commands,
    scrub_stale_verify_commands,
)


class TestGeneratedClaudeMd:
    """`ks init` no longer writes a second copy of the gate commands."""

    def _generated(self) -> str:
        return _generate_claude_md(
            {"name": "demo", "language": "Python", "framework": "FastAPI"},
        )

    def test_it_states_no_verification_command(self) -> None:
        generated = self._generated()
        for label in ("**Test**", "**Typecheck**", "**Lint**"):
            assert label not in generated

    def test_none_of_the_three_wrong_literals_survive(self) -> None:
        generated = self._generated()
        for literal in (
            "pytest tests/ -v --tb=short",
            "mypy src/ --strict",
            "ruff check src/",
        ):
            assert literal not in generated

    def test_it_still_tells_a_human_where_verification_lives(self) -> None:
        generated = self._generated()
        assert "## Verification" in generated
        assert "[verify]" in generated
        assert "test_command" in generated

    def test_the_proposals_anchor_heading_is_untouched(self) -> None:
        """proposals.py:194 errors when this literal heading is absent."""
        assert "## Agent Learnings" in self._generated()

    def test_a_scrub_of_a_freshly_generated_file_finds_nothing_to_do(
        self,
        tmp_path: Path,
    ) -> None:
        """The end state: init's own output cannot diverge, because it
        states nothing that could."""
        scrubbed = scrub_stale_verify_commands(
            self._generated(),
            resolve_verify_commands(VerifyConfig(), tmp_path),
        )
        assert scrubbed.divergences == []


class TestScaffoldSeedsTheDetectedToolchain:
    """#261 removed the per-language command guesses from the generated
    CLAUDE.md, where they were a second copy of a fact the gate owned.
    kstrl.toml [verify] IS that source, so the detected toolchain is
    recorded there instead of discarded: without it, `ks init` in a Rust
    repo left nothing pointing at `cargo test` and Phase 1 resolved to
    the Python defaults.
    """

    def _verify_lines(self, text: str) -> dict[str, str]:
        lines = {}
        for key in _VERIFY_KEYS:
            for line in text.splitlines():
                if line.startswith(f"# {key} = "):
                    lines[key] = line.split(" = ", 1)[1].strip('"')
                    break
        return lines

    @pytest.mark.parametrize(
        ("marker", "expected_test_command"),
        [
            ("Cargo.toml", "cargo test"),
            ("go.mod", "go test ./..."),
            ("package.json", "npm test"),
            ("pom.xml", "mvn test"),
        ],
    )
    def test_each_toolchain_is_recorded(
        self,
        tmp_path: Path,
        marker: str,
        expected_test_command: str,
    ) -> None:
        (tmp_path / marker).write_text("{}" if marker.endswith(".json") else "")
        lines = self._verify_lines(kstrl_toml_for(tmp_path))
        assert lines["test_command"] == expected_test_command

    def test_all_three_commands_are_recorded(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        assert self._verify_lines(kstrl_toml_for(tmp_path)) == {
            "test_command": "cargo test",
            "typecheck_command": "cargo check",
            "lint_command": "cargo clippy -- -D warnings",
        }

    def test_gradle_beats_maven_when_the_wrapper_is_present(self, tmp_path: Path) -> None:
        """The one case that needs the tree, not just the language."""
        (tmp_path / "build.gradle").write_text("")
        (tmp_path / "gradlew").write_text("")
        assert self._verify_lines(kstrl_toml_for(tmp_path))["test_command"] == "./gradlew test"

    def test_python_is_left_alone(self, tmp_path: Path) -> None:
        """The harness defaults are already right, so a seeded copy
        would be the duplication this whole change removes."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        assert kstrl_toml_for(tmp_path) == DEFAULT_KSTRL_TOML

    def test_an_unrecognised_tree_is_left_alone(self, tmp_path: Path) -> None:
        assert kstrl_toml_for(tmp_path) == DEFAULT_KSTRL_TOML

    def test_the_seed_stays_commented_out(self, tmp_path: Path) -> None:
        """`ks init` must never change an effective value."""

        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        data = tomllib.loads(kstrl_toml_for(tmp_path))
        assert all(section == {} for section in data.values())

    def test_uncommenting_the_seed_still_parses(self, tmp_path: Path) -> None:
        """One uncomment is the operator's opt-in, so it has to be
        valid TOML with no duplicate key."""

        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        text = kstrl_toml_for(tmp_path).replace(
            '# test_command = "cargo test"',
            'test_command = "cargo test"',
        )
        assert tomllib.loads(text)["verify"] == {"test_command": "cargo test"}
