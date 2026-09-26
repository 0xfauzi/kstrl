"""Every kstrl.toml key sets a field that some code reads (#525).

Five ``[timeout]`` keys loaded into ``TimeoutConfig``, were scaffolded
by ``ks init`` and printed by ``ks config show``, and no code read them.
The entry check cannot see that shape: a loader DID ask for the key. So
this census asks the other question, over the source: for every live key
the config reference documents (``scripts/gen_docs.py`` proves each one
behaviourally), is the field it sets read anywhere in ``kstrl/``?

A read is an attribute load of the field's name. Two kinds do not count:
one inside a function that builds or validates the config
(:data:`BUILDERS`), and one in a module that only reports it
(:data:`REPORTERS`). What this cannot see is disclosed below and pinned
as a strict xfail: the match is by NAME, so a read of the same name on
an unrelated object counts as a reader.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping

import pytest

from tests.helpers import astwalk
from tests.test_gen_docs import _load_gen_docs

#: Functions that set, overlay or validate a config field. A read inside
#: one is how the value gets IN, not how it takes effect.
BUILDERS = frozenset(
    {
        "load",
        "from_env",
        "load_or_none",
        "load_or_anchored",
        "anchored",
        "_apply_toml_overrides",
        "_apply_env_overrides",
        "_overlay_toml_section",
        "__post_init__",
    }
)

#: Modules whose reads only report a value (``ks config show`` and the
#: entry check). Labels as ``astwalk.label`` gives them.
REPORTERS = frozenset({"config_report.py", "config_preflight.py"})

#: Keys a loader sets that nothing reads yet, each with its reason. The
#: census must equal this exactly, so the row fails the day its reader
#: lands and has to be deleted in that diff.
UNREAD_BY_DESIGN: dict[tuple[str, str], str] = {
    ("learning", "consume"): (
        "the opt-out ships ahead of its reader: #217 slices 8 and 9 put "
        "playbook lessons into prompts, and nothing does so today"
    ),
}


def unread(
    fields: Mapping[tuple[str, str], str], trees: Iterable[ast.Module]
) -> set[tuple[str, str]]:
    """The ``(section, key)`` rows whose field no tree reads."""
    names: set[str] = set()
    for tree in trees:
        building = {
            id(node)
            for scope in ast.walk(tree)
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and scope.name in BUILDERS
            for node in ast.walk(scope)
        }
        names.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and id(node) not in building
        )
    return {row for row, field in fields.items() if field not in names}


def _live_keys() -> dict[tuple[str, str], str]:
    gen_docs = _load_gen_docs()
    return {
        (spec.section, key): field
        for spec in gen_docs._section_specs()
        for key, field in spec.keys.items()
    }


def test_every_kstrl_toml_key_sets_a_field_some_code_reads() -> None:
    fields = _live_keys()
    assert len(fields) > 100, fields
    trees = [
        astwalk.parsed(source)
        for source in astwalk.package_sources()
        if astwalk.label(source) not in REPORTERS
    ]
    assert unread(fields, trees) == set(UNREAD_BY_DESIGN), (
        "a kstrl.toml key sets a field no code in kstrl/ reads, so an operator "
        "can set it and change nothing. Wire a reader, or remove the field, its "
        "scaffold line and its documentation."
    )


ROW = ("section", "key")


class TestTheCensusCanFail:
    def test_a_read_counts(self) -> None:
        assert unread({ROW: "zz_field"}, [astwalk.parse("value = config.zz_field\n")]) == set()

    def test_no_read_is_flagged(self) -> None:
        assert unread({ROW: "zz_field"}, [astwalk.parse("value = config.other\n")]) == {ROW}

    def test_a_write_is_not_a_read(self) -> None:
        assert unread({ROW: "zz_field"}, [astwalk.parse("config.zz_field = 1\n")]) == {ROW}

    def test_a_read_inside_a_builder_is_not_a_reader(self) -> None:
        source = "def load(cls):\n    return defaults.zz_field\n"
        assert unread({ROW: "zz_field"}, [astwalk.parse(source)]) == {ROW}

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_same_named_read_on_another_object_clears_a_field(self) -> None:
        """Disclosed: the census matches by name and cannot tell whose
        ``mode`` was read."""
        astwalk.blind_spot(
            lambda source: unread({ROW: "mode"}, [astwalk.parse(source)]) == {ROW},
            "def elsewhere(other):\n    return other.mode\n",
        )
