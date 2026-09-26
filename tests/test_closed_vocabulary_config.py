"""A closed-vocabulary config field is refused where it is read (#562).

Three layers.

Layer 1, the census, is closed by construction over types: every
string-valued field of every loaded config class must be classified in
``tests/helpers/closed_vocabulary.py``, so a new field fails here until
someone decides whether its values are closed.

Layer 2, the refusal, is behavioural: for every CLOSED field, at the
kstrl.toml door and at the environment door, the real entry check
(``config_preflight.collect_config_problems``) must return one line that
names the field, the value and every accepted value. A valid value must
return nothing, so a blanket refusal cannot pass.

Layer 3, the use sites, flags every ``SomeStrEnum(x.field)`` call in
``kstrl/`` whose field is not CLOSED. That is the shape #562 was:
``ReviewMode(self.factory_config.review_mode)`` in Phase 2, with nothing
refusing the value earlier. It FLAGS, so it may over-match. It does not
see a use site that checks the value with ``in`` or ``==``, and it does
not see a closed vocabulary stored in a non-string field; both are
strict xfails below.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

import pytest

from kstrl.agents import canonical_agent_type
from kstrl.config_preflight import collect_config_problems, config_sections
from tests.helpers.closed_vocabulary import (
    BAD_VALUE,
    CLOSED,
    NOT_READ,
    OPEN,
    ClosedField,
    string_field_census,
    string_fields,
    typed_closed_fields,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Re-derived by running ``string_field_census()`` on this tree, never
#: edited to match: 16 closed, 38 open, 1 not read.
EXPECTED_STRING_FIELDS = 55

#: Re-derived by running ``use_site_parses()`` on this tree.
EXPECTED_USE_SITE_PARSES = {("kstrl/pipeline.py", "ReviewMode", "review_mode")}


class _Colour(StrEnum):
    RED = "red"


@dataclass
class _TypedProbe:
    """Module level because ``get_type_hints`` cannot see a local class."""

    colour: _Colour = _Colour.RED
    shade: Literal["light", "dark"] = "light"
    many: tuple[str, ...] = ()
    count: int = 0


@dataclass
class _IntProbe:
    level: int = 0


def _toml_literal(field: ClosedField, value: str) -> str:
    return json.dumps([value] if field.is_list else value)


def _problems(root: Path) -> list[str]:
    warnings: list[str] = []
    problems = collect_config_problems(root, warn=warnings.append)
    assert warnings == [], warnings
    return problems


def _assert_one_refusal(problems: list[str], field: ClosedField, names: str) -> None:
    assert len(problems) == 1, problems
    line = problems[0]
    assert f"[{field.section}]" in line, line
    assert names in line, line
    assert repr(BAD_VALUE) in line, line
    for value in field.accepted:
        assert value in line, (value, line)
    assert "which no kstrl setting reads" not in line, line


_DOORS = [(key, "toml") for key in CLOSED] + [
    (key, "env") for key, field in CLOSED.items() if field.env is not None
]
_IDS = [f"{cls}.{name}-{door}" for (cls, name), door in _DOORS]


class TestEveryStringFieldIsClassified:
    def test_the_census_matches_the_ledger(self) -> None:
        found = string_field_census()
        ledger = set(CLOSED) | set(OPEN) | set(NOT_READ)
        assert found - ledger == set(), (
            "string-valued config field(s) not classified in "
            "tests/helpers/closed_vocabulary.py; decide whether the values "
            f"are closed: {sorted(found - ledger)}"
        )
        assert ledger - found == set(), f"stale ledger rows: {sorted(ledger - found)}"

    def test_the_census_count_is_pinned(self) -> None:
        assert len(string_field_census()) == EXPECTED_STRING_FIELDS

    def test_the_three_tables_do_not_overlap(self) -> None:
        assert set(CLOSED).isdisjoint(OPEN)
        assert set(CLOSED).isdisjoint(NOT_READ)
        assert set(OPEN).isdisjoint(NOT_READ)

    def test_every_open_row_says_why(self) -> None:
        assert [key for key, reason in OPEN.items() if not reason.strip()] == []

    def test_the_type_rule_sees_enum_and_literal_fields(self) -> None:
        """Control: a field typed as a StrEnum or a Literal is counted,
        so typing a new closed field precisely cannot hide it."""
        assert string_fields(_TypedProbe) == ["colour", "shade", "many"]

    def test_a_field_typed_as_its_vocabulary_is_closed(self) -> None:
        """A field annotated with an Enum or a Literal cannot be OPEN."""
        typed = {
            (section.loader.__self__.__name__, name)
            for section in config_sections()
            for name in typed_closed_fields(section.loader.__self__)
        }
        assert typed - set(CLOSED) == set(), sorted(typed - set(CLOSED))

    def test_the_vocabulary_type_rule_is_live(self) -> None:
        """Control: the rule above is live, so an empty set is a measurement."""
        assert typed_closed_fields(_TypedProbe) == ["colour", "shade"]


class TestAClosedFieldIsRefusedWhereItIsRead:
    @pytest.mark.parametrize(("key", "door"), _DOORS, ids=_IDS)
    def test_a_bad_value_is_one_line_naming_field_value_and_choices(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        key: tuple[str, str],
        door: str,
    ) -> None:
        field = CLOSED[key]
        if door == "toml":
            (tmp_path / "kstrl.toml").write_text(
                f"[{field.section}]\n{field.key} = {_toml_literal(field, BAD_VALUE)}\n",
                encoding="utf-8",
            )
            names = field.key
        else:
            assert field.env is not None
            monkeypatch.setenv(field.env, BAD_VALUE)
            names = field.env
        _assert_one_refusal(_problems(tmp_path), field, names)

    @pytest.mark.parametrize(("key", "door"), _DOORS, ids=_IDS)
    def test_a_good_value_is_accepted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        key: tuple[str, str],
        door: str,
    ) -> None:
        """Control: the refusal above is about the value, not the key.

        EVERY accepted value, not one: a load check written against a
        narrower hand-copied vocabulary than the use site's must fail here.
        """
        field = CLOSED[key]
        for good in field.accepted:
            if door == "toml":
                (tmp_path / "kstrl.toml").write_text(
                    f"[{field.section}]\n{field.key} = {_toml_literal(field, good)}\n",
                    encoding="utf-8",
                )
            else:
                assert field.env is not None
                monkeypatch.setenv(field.env, good)
            assert _problems(tmp_path) == [], good

    @pytest.mark.parametrize(
        ("key", "door"),
        [(k, d) for k in CLOSED if k[1] == "agent_type" for d in ("toml", "env")],
        ids=lambda v: v if isinstance(v, str) else f"{v[0]}.{v[1]}",
    )
    @pytest.mark.parametrize("spelling", ["Claude", " CODEX "], ids=["title-case", "padded-upper"])
    def test_an_agent_type_spelling_the_use_site_accepts_is_accepted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        key: tuple[str, str],
        door: str,
        spelling: str,
    ) -> None:
        """Control: ``canonical_agent_type`` strips and lowercases, so the
        load check must too. A check written as ``value in
        VALID_AGENT_TYPES`` refuses a spelling every use site accepts."""
        assert canonical_agent_type(spelling) is not None
        field = CLOSED[key]
        if door == "toml":
            (tmp_path / "kstrl.toml").write_text(
                f"[{field.section}]\n{field.key} = {_toml_literal(field, spelling)}\n",
                encoding="utf-8",
            )
        else:
            assert field.env is not None
            monkeypatch.setenv(field.env, spelling)
        assert _problems(tmp_path) == []

    @pytest.mark.parametrize("key", sorted(NOT_READ), ids=lambda k: f"{k[0]}.{k[1]}")
    def test_a_field_no_loader_reads_has_no_toml_key(
        self, tmp_path: Path, key: tuple[str, str]
    ) -> None:
        section, toml_key = NOT_READ[key]
        (tmp_path / "kstrl.toml").write_text(
            f'[{section}]\n{toml_key} = "codex"\n', encoding="utf-8"
        )
        problems = _problems(tmp_path)
        assert len(problems) == 1, problems
        assert f"[{section}] {toml_key}, which no kstrl setting reads" in problems[0]


def _is_str_enum(node: ast.ClassDef) -> bool:
    bases = {
        base.id if isinstance(base, ast.Name) else getattr(base, "attr", "") for base in node.bases
    }
    return "StrEnum" in bases or {"str", "Enum"} <= bases


def _str_enum_names(trees: list[ast.Module]) -> set[str]:
    """Every class in ``kstrl/`` that subclasses StrEnum, or str and Enum."""
    return {
        node.name
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and _is_str_enum(node)
    }


def enum_parses(tree: ast.Module, enums: set[str]) -> list[tuple[str, str]]:
    """(enum, attribute) for every ``Enum(x.attribute)`` call in ``tree``."""
    hits: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        arg = node.args[0]
        if name in enums and isinstance(arg, ast.Attribute):
            hits.append((name, arg.attr))
    return hits


def use_site_parses() -> set[tuple[str, str, str]]:
    """(file, enum, field) for every StrEnum parse of a string config field."""
    field_names = {name for _cls, name in string_field_census()}
    paths = sorted((REPO_ROOT / "kstrl").rglob("*.py"))
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in paths}
    enums = _str_enum_names(list(trees.values()))
    return {
        (path.relative_to(REPO_ROOT).as_posix(), enum, attr)
        for path, tree in trees.items()
        for enum, attr in enum_parses(tree, enums)
        if attr in field_names
    }


class TestAUseSiteParseIsOfAClosedField:
    def test_every_enum_parse_of_a_config_field_is_closed(self) -> None:
        closed_names = {name for _cls, name in CLOSED}
        offenders = sorted(hit for hit in use_site_parses() if hit[2] not in closed_names)
        assert offenders == [], (
            "a config field is parsed with a StrEnum at its use site but is not "
            "CLOSED in tests/helpers/closed_vocabulary.py, so nothing proves it is "
            f"refused at load: {offenders}"
        )

    def test_the_walk_finds_the_known_site(self) -> None:
        """Control: the walk is live. Re-derive the pin by running it."""
        assert use_site_parses() == EXPECTED_USE_SITE_PARSES

    def test_the_walk_flags_the_562_shape(self) -> None:
        tree = ast.parse("mode = ReviewMode(self.factory_config.review_mode)\n")
        assert enum_parses(tree, {"ReviewMode"}) == [("ReviewMode", "review_mode")]

    @pytest.mark.xfail(
        strict=True,
        reason="blind spot: a use-site check written with `in` or `==` is not a parse",
    )
    def test_a_membership_check_at_the_use_site_is_seen(self) -> None:
        tree = ast.parse('if cfg.review_mode not in ("hard", "advisory"):\n    raise ValueError\n')
        assert enum_parses(tree, {"ReviewMode"}) != []

    @pytest.mark.xfail(
        strict=True,
        reason="blind spot: a closed vocabulary stored in an int field is not in the census",
    )
    def test_a_closed_int_field_is_in_the_census(self) -> None:
        assert string_fields(_IntProbe) == ["level"]
