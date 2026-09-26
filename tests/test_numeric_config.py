"""A numeric setting that cannot bound anything is refused where it is read (#571).

Three layers.

Layer 1, the refusal, is behavioural and closed by construction over
types: every numeric field of every loaded config class
(``tests/helpers/numeric_config.py``) is written into a real kstrl.toml
as ``nan``, ``inf``, ``-inf`` and ``-1``, and the real entry check
(``config_preflight.collect_config_problems``) must return exactly one
line naming the key. ``-1`` is accepted only for a field whose dataclass
metadata is ``config_numbers.SIGNED``. The field's own default must
return nothing, so a blanket refusal cannot pass.

Layer 2, the loader returns, is structural: every ``return`` in the
``load`` of every loaded config class must return ``check_numbers(...)``.
That is what covers the environment door for every field: Layer 1 drives
kstrl.toml only, and ``check_numbers`` checks the value the loader ends
with, wherever it came from. It FLAGS, so it may over-match.

Layer 3, the command line: every numeric option of ``ks factory`` and
``ks retry`` has the type ``config_numbers.LimitNumber``, and each one
refuses ``nan`` (a float option) and ``-1``.

Two blind spots, strict xfails below: a loader that drops a bad number
from the environment before it lands in the field (the shape
``[agent] budget_usd`` had before #571), and a number stored in a field
whose type is not a number.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap

import click
import pytest

from kstrl.cli import factory, retry
from kstrl.config_numbers import (
    SIGNED,
    BudgetConfigError,
    LimitNumber,
    check_numbers,
    refuse_non_finite,
)
from kstrl.config_preflight import ConfigSection, collect_config_problems, config_sections
from tests.helpers.numeric_config import class_name, numeric_field_census, toml_door

#: Re-derived by running ``numeric_field_census()`` on this tree, never
#: edited to match.
EXPECTED_NUMERIC_FIELDS = 54

#: The fields whose negative values mean something. Adding one is a
#: decision, so it is written here as well as on the field.
EXPECTED_SIGNED = {
    ("PolicyConfig", "max_files_changed"),
    ("PolicyConfig", "max_lines_changed"),
    ("GitHubIntakeConfig", "default_priority"),
}

#: Re-derived by running ``_load_returns()`` on this tree: one per loaded
#: class, and two in ``LearningConfig.load``.
EXPECTED_LOAD_RETURNS = 26

#: Re-derived by running ``_numeric_options()`` on this tree.
EXPECTED_NUMERIC_OPTIONS = {
    "factory": {
        "max_parallel",
        "max_retries",
        "mutation_threshold",
        "agent_timeout",
        "component_timeout",
        "max_adversarial_calls",
        "max_total_tokens",
        "max_cost_usd",
        "sleep",
    },
    "retry": {
        "max_cost_usd",
        "max_total_tokens",
        "max_adversarial_calls",
        "agent_timeout",
        "component_timeout",
        "max_parallel",
    },
}

_CENSUS = numeric_field_census()
_IDS = [f"{class_name(s)}.{f.name}" for s, f in _CENSUS]


def _lines(tmp_path: object, toml: str) -> list[str]:
    """Every problem and every warning the entry check reports for ``toml``.

    ``[evolution]`` degrades rather than stops the command, so its
    refusal is a warning; both count.
    """
    from pathlib import Path

    root = Path(str(tmp_path))
    (root / "kstrl.toml").write_text(toml, encoding="utf-8")
    warnings: list[str] = []
    problems = collect_config_problems(root, warn=warnings.append)
    return problems + warnings


def _toml_literal(value: object) -> str:
    return "0" if value is None else repr(value)


class TestTheCensus:
    def test_the_census_count_is_pinned(self) -> None:
        assert len(_CENSUS) == EXPECTED_NUMERIC_FIELDS

    def test_the_signed_fields_are_the_declared_ones(self) -> None:
        signed = {(class_name(s), f.name) for s, f in _CENSUS if f.metadata.get("signed")}
        assert signed == EXPECTED_SIGNED


@pytest.mark.parametrize(("section", "field"), _CENSUS, ids=_IDS)
class TestEveryNumericFieldAtTheTomlDoor:
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_a_non_finite_value_is_refused(
        self, tmp_path: object, section: ConfigSection, field: dataclasses.Field[object], value: str
    ) -> None:
        table, key = toml_door(section, field.name)

        lines = _lines(tmp_path, f"[{table}]\n{key} = {value}\n")

        assert len(lines) == 1, lines
        assert f"{key} must be a finite number, got {value}" in lines[0], lines
        assert "which no kstrl setting reads" not in lines[0], lines

    def test_a_negative_value_is_refused_unless_signed(
        self, tmp_path: object, section: ConfigSection, field: dataclasses.Field[object]
    ) -> None:
        table, key = toml_door(section, field.name)

        lines = _lines(tmp_path, f"[{table}]\n{key} = -1\n")

        if (class_name(section), field.name) in EXPECTED_SIGNED:
            assert lines == []
            return
        # Not the value: [linear] and [signals] refused negatives before
        # #571 with their own message, "must be positive", which names none.
        assert len(lines) == 1, lines
        assert key in lines[0] or field.name in lines[0], lines
        assert "which no kstrl setting reads" not in lines[0], lines

    def test_the_default_is_accepted(
        self, tmp_path: object, section: ConfigSection, field: dataclasses.Field[object]
    ) -> None:
        """Control: the refusals above are not a blanket refusal of the key."""
        table, key = toml_door(section, field.name)

        lines = _lines(tmp_path, f"[{table}]\n{key} = {_toml_literal(field.default)}\n")

        assert lines == []


class TestCheckNumbers:
    @dataclasses.dataclass
    class _Probe:
        count: int = 0
        seconds: float = 0.0
        optional: float | None = None
        flag: bool = False
        priority: int = dataclasses.field(default=0, metadata=SIGNED)

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("seconds", float("nan")),
            ("seconds", float("inf")),
            ("seconds", float("-inf")),
            ("seconds", -1.0),
            ("count", -1),
            ("optional", float("nan")),
            ("optional", -0.5),
            ("priority", float("nan")),
        ],
    )
    def test_a_value_that_cannot_bound_anything_is_refused(self, name: str, value: float) -> None:
        probe = self._Probe(**{name: value})
        with pytest.raises(BudgetConfigError, match=name):
            check_numbers(probe)

    def test_zero_none_a_bool_and_a_signed_negative_pass(self) -> None:
        probe = self._Probe(count=0, seconds=0.0, optional=None, flag=True, priority=-5)
        assert check_numbers(probe) is probe

    def test_refuse_non_finite_looks_inside_tables_and_arrays(self) -> None:
        with pytest.raises(BudgetConfigError, match=r"outer\.inner"):
            refuse_non_finite({"inner": [1.0, float("inf")]}, "outer")
        refuse_non_finite({"inner": [1.0, -2.0, "nan"]}, "outer")


def _unwrapped_returns(source: str, method: str = "load") -> list[int]:
    """Line of every ``return`` in ``method`` that does not return ``check_numbers(...)``.

    Nested functions are skipped: their returns are not the loader's.
    """
    tree = ast.parse(textwrap.dedent(source))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == method)
    offenders: list[int] = []
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        if isinstance(node, ast.Return):
            value = node.value
            wrapped = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "check_numbers"
            )
            if not wrapped:
                offenders.append(node.lineno)
        stack.extend(ast.iter_child_nodes(node))
    return offenders


def _load_returns() -> dict[str, tuple[int, list[int]]]:
    """class -> (returns in its load, the ones not through check_numbers)."""
    found: dict[str, tuple[int, list[int]]] = {}
    for section in config_sections():
        source = inspect.getsource(section.loader.__self__.load)  # type: ignore[attr-defined]
        tree = ast.parse(textwrap.dedent(source))
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        total = 0
        stack: list[ast.AST] = list(func.body)
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
                continue
            total += isinstance(node, ast.Return)
            stack.extend(ast.iter_child_nodes(node))
        found[class_name(section)] = (total, _unwrapped_returns(source))
    return found


class TestEveryLoaderReturnsThroughCheckNumbers:
    def test_no_loader_returns_around_it(self) -> None:
        offenders = {name: lines for name, (_, lines) in _load_returns().items() if lines}
        assert offenders == {}

    def test_the_return_count_is_pinned(self) -> None:
        """The walk saw every return, so an empty offender list is not a switched-off walk."""
        assert sum(total for total, _ in _load_returns().values()) == EXPECTED_LOAD_RETURNS

    def test_control_a_plain_return_is_flagged(self) -> None:
        source = """
        class C:
            @classmethod
            def load(cls, root):
                config = cls()
                if root is None:
                    return check_numbers(config)
                def helper():
                    return 1
                return config
        """
        assert _unwrapped_returns(source) == [10]


def _numeric_options(command: click.Command) -> tuple[set[str], list[str]]:
    """(options typed LimitNumber, numeric options that are not)."""
    limit: set[str] = set()
    plain: list[str] = []
    for param in command.params:
        if not isinstance(param, click.Option) or param.name is None:
            continue
        if isinstance(param.type, LimitNumber):
            limit.add(param.name)
        elif isinstance(param.type, click.types.IntParamType | click.types.FloatParamType):
            plain.append(param.name)
    return limit, plain


_COMMANDS = {"factory": factory, "retry": retry}


class TestEveryNumericOptionIsALimitNumber:
    @pytest.mark.parametrize("name", sorted(_COMMANDS))
    def test_no_numeric_option_uses_a_plain_click_number(self, name: str) -> None:
        limit, plain = _numeric_options(_COMMANDS[name])
        assert plain == []
        assert limit == EXPECTED_NUMERIC_OPTIONS[name]

    @pytest.mark.parametrize(
        ("name", "option"),
        [
            (n, o)
            for n in sorted(EXPECTED_NUMERIC_OPTIONS)
            for o in sorted(EXPECTED_NUMERIC_OPTIONS[n])
        ],
    )
    def test_each_refuses_what_cannot_bound_anything(self, name: str, option: str) -> None:
        param = next(p for p in _COMMANDS[name].params if p.name == option)
        assert isinstance(param.type, LimitNumber)
        bad = ["-1"] + (
            ["nan", "inf"] if isinstance(param.type.base, click.types.FloatParamType) else []
        )
        for value in bad:
            with pytest.raises(click.BadParameter):
                param.type.convert(value, param, None)
        assert param.type.convert("0", param, None) == 0

    def test_control_click_float_alone_accepts_nan(self) -> None:
        """Why the type exists: click's FLOAT, and FloatRange(min=0), take nan."""
        value = click.FLOAT.convert("nan", None, None)
        assert value != value
        ranged = click.FloatRange(min=0).convert("nan", None, None)
        assert ranged != ranged


class TestBlindSpots:
    @pytest.mark.xfail(
        strict=True,
        reason="blind spot: a loader that drops a bad env number before it lands "
        "returns through check_numbers and still accepts it; Layer 1 drives only kstrl.toml",
    )
    def test_a_number_dropped_before_it_lands_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @dataclasses.dataclass
        class _Dropper:
            budget: float | None = None

            @classmethod
            def load(cls) -> _Dropper:
                config = cls()
                raw = float(__import__("os").environ["KSTRL_PROBE_571"])
                if raw > 0:
                    config.budget = raw
                return check_numbers(config)

        monkeypatch.setenv("KSTRL_PROBE_571", "-1")
        with pytest.raises(BudgetConfigError):
            _Dropper.load()

    @pytest.mark.xfail(
        strict=True,
        reason="blind spot: check_numbers and the census see int and float fields "
        "only, not a number inside a dict, list or string field",
    )
    def test_a_number_in_a_non_numeric_field_is_refused(self) -> None:
        @dataclasses.dataclass
        class _Nested:
            limits: dict[str, float] = dataclasses.field(default_factory=dict)

        with pytest.raises(BudgetConfigError):
            check_numbers(_Nested(limits={"seconds": float("nan")}))


class TestTheConfigReportSurvivesARefusedSection:
    """The TUI home and config screens call ``build_config_report`` with no
    entry check in front of it. A section refused for a ``nan`` must read as
    unresolved, like a section refused for any other reason, and must not
    take the whole report down (#571)."""

    @pytest.mark.parametrize(
        ("toml", "unresolved"),
        [
            ("[verify]\nsubprocess_timeout = nan\n", ("verify",)),
            ("[verify]\nsubprocess_timeout = 5\n", ()),
        ],
        ids=["nan", "control-finite"],
    )
    def test_the_report_lists_the_section_as_unresolved(
        self, tmp_path: object, toml: str, unresolved: tuple[str, ...]
    ) -> None:
        from pathlib import Path

        from kstrl.config_report import build_config_report

        root = Path(str(tmp_path))
        (root / "kstrl.toml").write_text(toml, encoding="utf-8")

        report = build_config_report(root)

        assert report.unresolved == unresolved
