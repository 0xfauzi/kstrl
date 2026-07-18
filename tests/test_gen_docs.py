"""R2.5: the README's generated sections must match what the generator
emits from the current code, so CLI/config docs cannot drift.

The same check runs in CI as `uv run python scripts/gen_docs.py --check`;
this test keeps the gate enforceable locally via plain pytest.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_gen_docs() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_docs", REPO_ROOT / "scripts" / "gen_docs.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The @dataclass decorator resolves the module's postponed annotations
    # via sys.modules[cls.__module__]; register before exec or it crashes.
    sys.modules["gen_docs"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen_docs() -> ModuleType:
    return _load_gen_docs()


class TestReadmeCurrent:
    def test_generated_sections_match_committed_readme(self, gen_docs: ModuleType) -> None:
        """The committed README equals its own regeneration (the drift gate)."""
        current = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert gen_docs.render_readme(current) == current, (
            "README.md generated sections are stale; "
            "run: uv run python scripts/gen_docs.py"
        )

    def test_generation_is_idempotent(self, gen_docs: ModuleType) -> None:
        current = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        once = gen_docs.render_readme(current)
        assert gen_docs.render_readme(once) == once

    def test_markers_present(self, gen_docs: ModuleType) -> None:
        current = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("cli-reference", "config-reference"):
            assert gen_docs._marker(name) in current
            assert gen_docs._marker(name, end=True) in current

    def test_missing_marker_is_loud(self, gen_docs: ModuleType) -> None:
        with pytest.raises(SystemExit, match="markers"):
            gen_docs._splice("no markers here", "cli-reference", "generated")


class TestCliReference:
    def test_every_click_command_is_documented(self, gen_docs: ModuleType) -> None:
        from ralph_py.cli import cli

        reference = gen_docs.build_cli_reference()
        for name in cli.commands:
            assert f"ralph {name}" in reference

    def test_no_fictional_commands(self, gen_docs: ModuleType) -> None:
        """The pre-R2.5 README documented commands that never existed."""
        reference = gen_docs.build_cli_reference()
        for fiction in ("ralph prd", "--legacy", "Launch TUI"):
            assert fiction not in reference


class TestConfigProbing:
    def test_documented_dead_key_fails_generation(self, gen_docs: ModuleType) -> None:
        """A documented toml key the loader ignores must break generation,
        not silently ship wrong docs."""
        from ralph_py.config import RalphConfig

        spec = gen_docs.SectionSpec(
            section="agent",
            title="broken",
            keys={"no_such_key": "max_iterations"},
            loader=lambda root: RalphConfig.load(root_dir=root),
            defaults=RalphConfig(),
            probe_undocumented_fields=False,
        )
        with pytest.raises(SystemExit, match="no_such_key"):
            gen_docs._verify_sections([spec])

    def test_config_reference_covers_all_example_sections(self, gen_docs: ModuleType) -> None:
        """Every [section] in ralph.toml.example appears in the generated
        reference and vice versa - the two surfaces stay in lockstep."""
        import tomllib

        reference = gen_docs.build_config_reference()
        example = tomllib.loads(
            (REPO_ROOT / "ralph.toml.example").read_text(encoding="utf-8")
        )
        generated_sections = {s.section for s in gen_docs._section_specs()}
        assert set(example) == generated_sections
        for section in generated_sections:
            assert f"[{section}]" in reference

    def test_example_toml_keys_are_all_documented(self, gen_docs: ModuleType) -> None:
        """ralph.toml.example must not name keys the loaders ignore."""
        import tomllib

        example = tomllib.loads(
            (REPO_ROOT / "ralph.toml.example").read_text(encoding="utf-8")
        )
        documented = {
            (spec.section, key)
            for spec in gen_docs._section_specs()
            for key in spec.keys
        }
        for section, values in example.items():
            for key in values:
                assert (section, key) in documented, (
                    f"ralph.toml.example documents [{section}] {key} "
                    "but gen_docs does not know it as a live loader key"
                )


class TestExampleProjectContract:
    def test_example_prompt_is_the_current_engineer_contract(self) -> None:
        """examples/uv-python ships the same engineer prompt `ralph init`
        scaffolds, so the example cannot drift behind the contract again
        (pre-R2.5 it lacked the Self-Critique block)."""
        from ralph_py.init_cmd import DEFAULT_PROMPT

        example = (
            REPO_ROOT / "examples" / "uv-python" / "scripts" / "ralph" / "prompt.md"
        ).read_text(encoding="utf-8")
        assert example == DEFAULT_PROMPT

    def test_example_prd_prompt_allows_allowed_paths(self) -> None:
        text = (
            REPO_ROOT / "examples" / "uv-python" / "scripts" / "ralph" / "prd_prompt.txt"
        ).read_text(encoding="utf-8")
        assert "allowedPaths" in text
        assert "exactly these keys: \"branchName\", \"userStories\"" not in text


class TestSectionSpecShape:
    def test_all_documented_keys_have_descriptions(self, gen_docs: ModuleType) -> None:
        for spec in gen_docs._section_specs():
            for key in spec.keys:
                assert (spec.section, key) in gen_docs.KEY_DESCRIPTIONS

    def test_key_fields_exist_on_dataclasses(self, gen_docs: ModuleType) -> None:
        for spec in gen_docs._section_specs():
            field_names = {f.name for f in dataclasses.fields(spec.defaults)}
            for key, field_name in spec.keys.items():
                assert field_name in field_names, (
                    f"[{spec.section}] {key} maps to missing field {field_name}"
                )
