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
        "gen_docs",
        REPO_ROOT / "scripts" / "gen_docs.py",
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
    @pytest.fixture(autouse=True)
    def _shipped_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The README documents the SHIPPED defaults. The suite-wide
        short_waits fixture (tests/conftest.py) zeroes the factory retry
        delay through the env door the generator reads, so it is undone
        here or the regenerated table would read 0.0 against the
        committed 5.0."""
        monkeypatch.delenv("FACTORY_RETRY_DELAY", raising=False)

    def test_generated_sections_match_committed_readme(self, gen_docs: ModuleType) -> None:
        """The committed README equals its own regeneration (the drift gate)."""
        current = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert gen_docs.render_readme(current) == current, (
            "README.md generated sections are stale; run: uv run python scripts/gen_docs.py"
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
        from kstrl.cli import cli

        reference = gen_docs.build_cli_reference()
        for name in cli.commands:
            assert f"ks {name}" in reference

    def test_no_fictional_commands(self, gen_docs: ModuleType) -> None:
        """The pre-R2.5 README documented commands that never existed."""
        reference = gen_docs.build_cli_reference()
        for fiction in ("ks prd", "--legacy", "Launch TUI"):
            assert fiction not in reference


class TestConfigProbing:
    def test_documented_dead_key_fails_generation(self, gen_docs: ModuleType) -> None:
        """A documented toml key the loader ignores must break generation,
        not silently ship wrong docs."""
        from kstrl.config import KstrlConfig

        spec = gen_docs.SectionSpec(
            section="agent",
            title="broken",
            keys={"no_such_key": "max_iterations"},
            loader=lambda root: KstrlConfig.load(root_dir=root),
            defaults=KstrlConfig(),
            probe_undocumented_fields=False,
        )
        with pytest.raises(SystemExit, match="no_such_key"):
            gen_docs._verify_sections([spec])

    def test_config_reference_covers_all_example_sections(self, gen_docs: ModuleType) -> None:
        """Every [section] in kstrl.toml.example appears in the generated
        reference and vice versa - the two surfaces stay in lockstep."""
        import tomllib

        reference = gen_docs.build_config_reference()
        example = tomllib.loads((REPO_ROOT / "kstrl.toml.example").read_text(encoding="utf-8"))
        generated_sections = {s.section for s in gen_docs._section_specs()}
        assert set(example) == generated_sections
        for section in generated_sections:
            assert f"[{section}]" in reference

    def test_a_provenance_field_is_not_probed_as_a_toml_key(self, gen_docs: ModuleType) -> None:
        """#195: ``explicit_fields`` records which keys the operator set.

        It has no toml key, no env var and no flag, so probing it as an
        undocumented key would report an error about a field nobody can
        write. It was already excluded, but by TYPE: ``_scalar_fields``
        admits ``None/bool/int/float/str/Path/list`` and a ``frozenset``
        falls out on its own. That is an accident of the field's type,
        not a decision, and the next provenance field may be a scalar.
        The exclusion is now declared, and this is what holds it.
        """
        from kstrl.factory import FactoryConfig

        @dataclasses.dataclass
        class Probe:
            """A SCALAR provenance field, which is what the type list admits."""

            ordinary: int = 0
            recorded: int = dataclasses.field(default=0, metadata={"provenance": True})

        assert gen_docs._scalar_fields(Probe()) == {"ordinary"}

        defaults = FactoryConfig()
        provenance = {f.name for f in dataclasses.fields(defaults) if f.metadata.get("provenance")}
        assert provenance, "no provenance field left to test; delete this test with the field"
        assert provenance & gen_docs._scalar_fields(defaults) == set()

    def test_a_provenance_field_never_reaches_the_readme(self) -> None:
        """The end the operator sees, asserted on the committed file."""
        from kstrl.factory import FactoryConfig

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for f in dataclasses.fields(FactoryConfig()):
            if f.metadata.get("provenance"):
                assert f.name not in readme, f.name

    def test_example_toml_keys_are_all_documented(self, gen_docs: ModuleType) -> None:
        """kstrl.toml.example must not name keys the loaders ignore."""
        import tomllib

        example = tomllib.loads((REPO_ROOT / "kstrl.toml.example").read_text(encoding="utf-8"))
        documented = {
            (spec.section, key) for spec in gen_docs._section_specs() for key in spec.keys
        }
        for section, values in example.items():
            for key in values:
                assert (section, key) in documented, (
                    f"kstrl.toml.example documents [{section}] {key} "
                    "but gen_docs does not know it as a live loader key"
                )


class TestExampleProjectContract:
    def test_example_prompt_is_the_current_engineer_contract(self) -> None:
        """examples/uv-python ships the same engineer prompt `ks init`
        scaffolds, so the example cannot drift behind the contract again
        (pre-R2.5 it lacked the Self-Critique block)."""
        from kstrl.init_cmd import DEFAULT_PROMPT

        example = (
            REPO_ROOT / "examples" / "uv-python" / "scripts" / "kstrl" / "prompt.md"
        ).read_text(encoding="utf-8")
        assert example == DEFAULT_PROMPT

    def test_example_gitignore_is_the_one_init_scaffolds(self) -> None:
        """examples/uv-python ships the same ignore block `ks init`
        writes. Before #201 it ignored uv.lock, which hid the lockfile
        from the scope guard by hiding it from git - a workaround the
        example then taught to everyone who copied it."""
        from kstrl.init_cmd import gitignore_block

        example = (REPO_ROOT / "examples" / "uv-python" / ".gitignore").read_text(encoding="utf-8")
        assert example == gitignore_block("Python")

    def test_example_lockfile_is_tracked(self) -> None:
        """The example stopped ignoring uv.lock in #201, so the file has
        to be IN GIT: an un-ignored untracked lockfile is the exact
        condition #201 is about, and leaving one in kstrl's own tree
        would have the fix reproduce the bug it fixes. Existence is not
        enough - an untracked file exists too."""
        import subprocess

        relative = "examples/uv-python/uv.lock"
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=30,
        )

        assert tracked.returncode == 0, f"run `uv lock` in examples/uv-python and commit {relative}"

    def test_example_lockfile_is_current(self) -> None:
        """#636: the example depends on kstrl as an editable path, so its
        uv.lock pins kstrl's own dependencies. kstrl gained gepa and the
        example's lock was not regenerated, so every `uv run` in the
        example rewrote the lock and a `--locked` run failed. This runs
        the real `uv lock --check` there. Offline, because a current lock
        needs nothing from the network (measured with an empty cache).

        The variables that point uv at another project or environment are
        removed, so the check can only judge the example's own lock. UV_FROZEN
        is removed too: with it set, `uv lock --check` only validates the file
        and exits 0 on a stale lock."""
        import os
        import shutil
        import subprocess

        uv = shutil.which("uv")
        assert uv is not None, "uv is not on PATH; the suite runs under `uv run`"
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("VIRTUAL_ENV", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "UV_FROZEN")
        }
        example = REPO_ROOT / "examples" / "uv-python"
        result = subprocess.run(
            [uv, "lock", "--check", "--offline"],
            cwd=example,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, (
            "examples/uv-python/uv.lock is stale: run `uv lock` in examples/uv-python "
            f"(never `uv lock --upgrade`) and commit it.\n{result.stderr}"
        )

    def test_example_prd_prompt_allows_allowed_paths(self) -> None:
        text = (
            REPO_ROOT / "examples" / "uv-python" / "scripts" / "kstrl" / "prd_prompt.txt"
        ).read_text(encoding="utf-8")
        assert "allowedPaths" in text
        assert 'exactly these keys: "branchName", "userStories"' not in text


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
