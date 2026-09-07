"""One table, five surfaces (#229 SHOULD-FIX 8).

``config.STRING_KEYS`` is the declared set of kstrl.toml keys that
overlay one ``KstrlConfig`` field from a string. Three consumers derive
from it (the TOML overlay, the env overlay, ``KstrlConfig.anchored``) and
two used to hand-copy it: ``config_report.show_sections``, which is what
``ks config`` prints, and ``scripts/gen_docs.py``, which is what the
README's generated config reference is built from.

Nothing tied the copies. ``grep -rn SHOW_SECTIONS tests/`` returned
nothing, and both KstrlConfig sections in gen_docs declare
``probe_undocumented_fields=False``, so its behavioural probe does not
catch a live key that is missing from the dict either. The PR that added
``golden_patterns`` hand-edited all three tables, which is the defect
class demonstrated live: a tenth row added to ``STRING_KEYS`` alone
dropped out of ``ks config`` and out of the generated README with every
gate green.

The test is the mutation. It adds a tenth row at run time and asserts it
appears on both surfaces with no other edit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from kstrl import config as config_mod
from kstrl import config_report
from kstrl.config import STRING_KEYS

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A tenth row. It aliases an existing FIELD on purpose: the point under
#: test is that the KEY travels, and inventing a dataclass field would
#: test a KstrlConfig this repo does not ship.
TENTH_ROW = ("paths", "tenth_row_probe", "KSTRL_TENTH_ROW_PROBE", "codebase_map_file", True)


@pytest.fixture
def gen_docs() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_docs_string_keys", REPO_ROOT / "scripts" / "gen_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_docs_string_keys"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tenth_row(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str, str, bool]:
    monkeypatch.setattr(config_mod, "STRING_KEYS", (*STRING_KEYS, TENTH_ROW))
    return TENTH_ROW


class TestATenthRowNeedsNoHandEdit:
    def test_it_reaches_the_ks_config_table(
        self,
        tenth_row: tuple[str, str, str, str, bool],
    ) -> None:
        section, key, _env, field_name, _is_path = tenth_row

        rows = config_report.show_sections()

        assert (key, field_name) in dict(rows)[section]

    def test_it_reaches_the_generated_readme_table(
        self,
        gen_docs: ModuleType,
        tenth_row: tuple[str, str, str, str, bool],
    ) -> None:
        section, key, _env, field_name, _is_path = tenth_row

        specs = {spec.section: spec for spec in gen_docs._section_specs()}

        assert specs[section].keys[key] == field_name

    def test_without_the_row_neither_surface_mentions_it(self, gen_docs: ModuleType) -> None:
        """The control. Both assertions above pass trivially if the probe
        key were somehow already there."""
        _section, key, _env, _field, _is_path = TENTH_ROW

        assert key not in dict(config_report.show_sections())["paths"]
        assert key not in {sp.section: sp for sp in gen_docs._section_specs()}["paths"].keys

    def test_the_shipped_rows_are_all_on_both_surfaces(self, gen_docs: ModuleType) -> None:
        """And the derivation is not merely PRESENT, it is complete: every
        string key kstrl ships shows up on both, in the table's order."""
        sections = dict(config_report.show_sections())
        specs = {spec.section: spec for spec in gen_docs._section_specs()}

        for section in ("agent", "paths"):
            declared = [
                (key, field_name) for sec, key, _e, field_name, _p in STRING_KEYS if sec == section
            ]
            assert sections[section][: len(declared)] == declared
            for key, field_name in declared:
                assert specs[section].keys[key] == field_name
