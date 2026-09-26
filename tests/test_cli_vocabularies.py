"""No CLI option restates a vocabulary by hand (#565).

Before #565 ``--agent-type`` was a hand-written list that left out two
values ``[agent] type`` accepts, ``--review-mode``, ``--security-mode`` and
``--contract-check`` were copies of their enums, and the ``--ui`` list was
written out fifteen times. Two layers.

Layer 1 walks the real click tree and is closed by construction over
OPTIONS: every option whose type is a ``click.Choice`` must be classified,
either in ``FLAG_FIELDS`` (it sets a CLOSED config field; its choices must
be that field's accepted values, and ``tests/test_cli_flag_vocabularies.py``
proves the flag and the field agree value by value) or in
``CHOICE_VOCABULARIES`` below (no config field; its choices must equal the
kstrl constant its use site reads). An option named after a CLOSED field
must be in ``FLAG_FIELDS`` whatever its type, which is how ``ks config show
--agent-type`` taking any string fails here.

Layer 2 walks ``kstrl/`` and FLAGS every ``Choice(...)`` whose choices are
written as a list, tuple or set display: the shape of a hand-written copy.
It also pins how many ``Choice`` calls each module holds, so a new one is a
census delta somebody has to look at.

Blind spots, pinned by strict xfails. A copy bound to a name first and
then passed to ``Choice`` is not a display, and Layer 1 compares values,
so the copy passes both layers while it still agrees. A ``Choice``
subclass that hands a display to ``super().__init__`` is not a ``Choice``
call (``_AgentTypeChoice`` in ``kstrl/cli.py`` passes a constant, and
Layer 1 compares its choices). An option that sets a CLOSED field under a
name the naming rule does not derive (``--contract-check`` sets
``[contract] mode``) is found only if someone writes its row.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

import click
import pytest

from kstrl import baseline
from kstrl.autonomy import DEMOTION_TRIGGER_LABELS
from kstrl.cli import cli
from kstrl.config_report import UI_MODES
from kstrl.serve import LAUNCHD_MODES
from kstrl.workqueue import ItemState
from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    leaf_name,
    package_sources,
    parse,
)
from tests.helpers.closed_vocabulary import CLOSED, FLAG_FIELDS

#: Choice options that set no config field: param name -> the constant
#: their choices must equal.
CHOICE_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "ui": UI_MODES,
    "output_format": baseline.OUTPUT_FORMATS,
    "trigger": DEMOTION_TRIGGER_LABELS,
    "plist_mode": LAUNCHD_MODES,
    "states": tuple(state.value for state in ItemState),
}

#: Re-derived by running ``len(choice_options(cli))`` on this tree, never
#: edited to match.
EXPECTED_CHOICE_OPTIONS = 48

#: Re-derived by running the Layer 2 census on this tree.
EXPECTED_CHOICE_CALLS: dict[str, int] = {"cli.py": 23}

#: The options the naming rule finds on this tree. Re-derived by running
#: ``test_the_naming_rule_finds_the_known_flags``.
EXPECTED_NAMED_FLAGS = [
    "ks config show --agent-type",
    "ks decompose --agent-type",
    "ks factory --agent-type",
    "ks factory --review-mode",
    "ks factory --security-fail-threshold",
    "ks factory --security-mode",
]

Option = tuple[tuple[str, ...], click.Option]


def all_options(command: click.Command, path: tuple[str, ...] = ()) -> Iterator[Option]:
    """Every option of ``command`` and of every command under it."""
    for param in command.params:
        if isinstance(param, click.Option):
            yield path, param
    if isinstance(command, click.Group):
        for name in sorted(command.commands):
            yield from all_options(command.commands[name], (*path, name))


def choice_options(command: click.Command) -> list[Option]:
    return [(p, o) for p, o in all_options(command) if isinstance(o.type, click.Choice)]


def _long(option: click.Option) -> str:
    return next(opt for opt in option.opts if opt.startswith("--"))


def closed_field_names() -> set[str]:
    """The option names that say they set a CLOSED field.

    For ``[security] fail_threshold`` on ``SecurityConfig.fail_threshold``:
    ``fail_threshold`` and ``security_fail_threshold``.
    """
    names: set[str] = set()
    for (_cls, name), field in CLOSED.items():
        names |= {name, field.key, f"{field.section}_{field.key}", f"{field.section}_{name}"}
    return names


def _where(path: tuple[str, ...], option: click.Option) -> str:
    return f"ks {' '.join(path)} {_long(option)}"


class TestEveryChoiceOptionReadsItsVocabulary:
    def test_the_walk_sees_a_choice_option(self) -> None:
        """Control: the walk is live, so an empty offender list is a measurement."""

        @click.group()
        def top() -> None: ...

        @top.command()
        @click.option("--colour", type=click.Choice(["red"]))
        def paint(colour: str) -> None: ...

        found = choice_options(top)
        assert [(p, _long(o)) for p, o in found] == [(("paint",), "--colour")]

    def test_the_choice_option_count_is_pinned(self) -> None:
        assert len(choice_options(cli)) == EXPECTED_CHOICE_OPTIONS

    def test_every_choice_option_is_classified(self) -> None:
        unclassified = [
            _where(path, option)
            for path, option in choice_options(cli)
            if (path, _long(option)) not in FLAG_FIELDS and option.name not in CHOICE_VOCABULARIES
        ]
        assert unclassified == [], (
            "a click.Choice option is in neither FLAG_FIELDS "
            "(tests/helpers/closed_vocabulary.py) nor CHOICE_VOCABULARIES: say which "
            f"kstrl constant its values come from: {unclassified}"
        )

    def test_every_choice_equals_its_vocabulary(self) -> None:
        wrong: list[str] = []
        for path, option in choice_options(cli):
            assert isinstance(option.type, click.Choice)
            choices = tuple(option.type.choices)
            key = FLAG_FIELDS.get((path, _long(option)))
            if key is not None:
                if set(choices) != set(CLOSED[key].accepted):
                    wrong.append(f"{_where(path, option)}: {choices} != {CLOSED[key].accepted}")
            elif option.name in CHOICE_VOCABULARIES:
                if choices != CHOICE_VOCABULARIES[option.name]:
                    wrong.append(f"{_where(path, option)}: {choices}")
        assert wrong == [], wrong

    def test_every_flag_field_row_is_a_choice_in_the_tree(self) -> None:
        found = {(path, _long(option)): option for path, option in all_options(cli)}
        missing = [row for row in FLAG_FIELDS if row not in found]
        assert missing == [], f"stale FLAG_FIELDS rows: {missing}"
        free = [row for row in FLAG_FIELDS if not isinstance(found[row].type, click.Choice)]
        assert free == [], f"these flags set a closed field but take any string: {free}"

    def test_an_option_named_for_a_closed_field_is_a_flag_field(self) -> None:
        names = closed_field_names()
        unlisted = [
            _where(path, option)
            for path, option in all_options(cli)
            if option.name in names and (path, _long(option)) not in FLAG_FIELDS
        ]
        assert unlisted == [], (
            "an option named after a CLOSED config field is not in FLAG_FIELDS, so "
            f"nothing proves it accepts what the field accepts: {unlisted}"
        )

    def test_the_naming_rule_finds_the_known_flags(self) -> None:
        """Control: the rule above is live on this tree. Re-derive by running it."""
        names = closed_field_names()
        found = sorted(_where(p, o) for p, o in all_options(cli) if o.name in names)
        assert found == EXPECTED_NAMED_FLAGS

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="blind spot: an option named unlike its field is found only by its row",
    )
    def test_an_option_named_unlike_its_field_is_seen(self) -> None:
        blind_spot(lambda name: name in closed_field_names(), "contract_check")


def _is_choice_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and leaf_name(node.func) == "Choice"


def _choices_arg(node: ast.Call) -> ast.expr | None:
    if node.args:
        return node.args[0]
    return next((kw.value for kw in node.keywords if kw.arg == "choices"), None)


def _is_hand_written_choice(node: ast.AST) -> bool:
    """A ``Choice(...)`` whose choices are a list, tuple or set display."""
    if not _is_choice_call(node):
        return False
    assert isinstance(node, ast.Call)
    return isinstance(_choices_arg(node), ast.List | ast.Tuple | ast.Set)


def _sees_hand_written(source: str) -> bool:
    return any(_is_hand_written_choice(node) for node in all_nodes(parse(source)))


class TestNoChoiceIsWrittenOutByHand:
    def test_no_choice_is_given_a_display(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=_is_hand_written_choice,
            expected={},
            control=(
                'click.Choice(["hard", "advisory", "skip"])',
                'Choice(("a", "b"))',
                'click.Choice(choices={"a"})',
                "click.Choice([MODE_A, MODE_B])",
            ),
            message=(
                "A click.Choice in kstrl/ lists its values by hand. Pass the constant "
                "the config field and the use site read instead (#565)."
            ),
        )

    def test_every_choice_call_is_counted(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=_is_choice_call,
            expected=EXPECTED_CHOICE_CALLS,
            control=("click.Choice(UI_MODES)", "Choice(VALID)"),
            message=(
                "The number of click.Choice calls changed. Re-derive "
                "EXPECTED_CHOICE_CALLS by running the census, after checking the new "
                "call reads a kstrl constant."
            ),
        )

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="blind spot: a copy bound to a name first is not a display",
    )
    def test_a_copy_bound_to_a_name_is_seen(self) -> None:
        blind_spot(_sees_hand_written, '_MODES = ["hard", "skip"]\nclick.Choice(_MODES)\n')

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="blind spot: a Choice subclass hands its values to super().__init__",
    )
    def test_a_subclass_passing_a_display_is_seen(self) -> None:
        blind_spot(
            _sees_hand_written,
            "class _C(click.Choice):\n"
            "    def __init__(self):\n"
            '        super().__init__(["a", "b"])\n',
        )
