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

Layer 1 also writes ``"lots"`` into every numeric key: before #583
``[agent] budget_usd = "lots"`` read as no ceiling.

Layer 3, the command line, is closed over every command (#583): every
numeric option and argument of every command is either bounded
(``config_numbers.LimitNumber``, or an ``IntRange`` whose minimum is at
least 0) or written in :data:`NOT_LIMITS` with the reason. Each bounded
one refuses ``-1`` and, for a float, ``nan`` and ``inf``.

Layer 4, the environment (#583): every variable kstrl reads
(``tests/test_env_vars_documented.py``) that sets a numeric field, found
by setting it, must be refused by the real entry check when set to
``lots``, ``nan`` or (unless signed) ``-1``. Before #583
``KSTRL_AGENT_BUDGET_USD=lots`` read as no ceiling.

Four blind spots, strict xfails below: a loader that drops a bad number
before it lands, seen by ``check_numbers`` alone (Layer 4 drives that
shape for the real loaders); a number stored in a field whose type is
not a number; a variable whose value lands scaled; and a variable whose
name is built at run time.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap
from pathlib import Path
from typing import Any

import click
import pytest

from kstrl.cli import cli
from kstrl.config_keys import RETIRED_ENV_VARS
from kstrl.config_numbers import (
    SIGNED,
    BudgetConfigError,
    LimitNumber,
    check_numbers,
    refuse_non_finite,
)
from kstrl.config_preflight import ConfigSection, collect_config_problems, config_sections
from tests.helpers.numeric_config import (
    class_name,
    env_number_doors,
    numeric_field_census,
    toml_door,
)
from tests.test_env_vars_documented import NOT_KSTRL_SETTINGS, names_read_by_kstrl

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

#: Re-derived by running ``_numeric_parameters(_commands())`` on this tree:
#: every numeric option and argument, of every command, whose type refuses
#: a value that cannot bound anything.
EXPECTED_LIMIT_PARAMETERS = {
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
    "run": {"max_iterations", "sleep"},
    "understand": {"max_iterations", "sleep"},
    "feature": {"understand_iterations", "sleep", "repair_max_runs", "repair_iterations"},
    "serve": {"max_cycles", "plist_interval"},
    "queue add": {"max_attempts"},
}

#: Numeric parameters that are not limits, and why. Each one is a decision,
#: so a new numeric parameter fails the census until it is typed
#: ``LimitNumber`` or written here (#583).
NOT_LIMITS: dict[tuple[str, str], str] = {
    ("config show", "max_iterations"): "shown, not run: the command spends nothing",
    ("config show", "sleep"): "shown, not run: the command spends nothing",
    ("dash", "poll"): "a screen refresh interval; the command spends nothing",
    ("inbox snooze", "hours"): "an inbox item's snooze; the command spends nothing",
    ("queue add", "priority"): "an ordering, where a negative value means something",
    ("status", "interval"): "a refresh interval; the command spends nothing",
}

#: Re-derived by running ``_env_doors()`` on this tree: one per variable
#: that sets a numeric field.
EXPECTED_ENV_DOORS = 53

#: The numeric fields no environment variable sets, re-derived by running.
EXPECTED_NO_ENV_DOOR = {("EvolutionConfig", "min_pattern_frequency")}

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
        assert len(lines) == 1, lines
        assert key in lines[0] or field.name in lines[0], lines
        assert "-1" in lines[0], lines
        assert "which no kstrl setting reads" not in lines[0], lines

    def test_a_value_that_is_not_a_number_is_refused(
        self, tmp_path: object, section: ConfigSection, field: dataclasses.Field[object]
    ) -> None:
        """#583: ``[agent] budget_usd = "lots"`` read as no ceiling."""
        table, key = toml_door(section, field.name)

        lines = _lines(tmp_path, f'[{table}]\n{key} = "lots"\n')

        assert len(lines) == 1, lines
        assert f"{key} = 'lots'" in lines[0], lines

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


def _commands(group: click.Group = cli, prefix: str = "") -> dict[str, click.Command]:
    """Every command and group under ``group``, keyed by its path ("queue add")."""
    found: dict[str, click.Command] = {}
    for name, command in group.commands.items():
        path = f"{prefix} {name}".strip()
        found[path] = command
        if isinstance(command, click.Group):
            found.update(_commands(command, path))
    return found


def _bounded(kind: click.ParamType[Any, Any]) -> bool:
    """True for a type that refuses nan, inf and a negative number.

    This CLEARS a parameter, so it is narrow: ``LimitNumber``, or an
    ``IntRange`` with a lower bound of at least 0 (an int cannot be nan
    or inf). ``FloatRange(min=0)`` is not here: it accepts nan.
    """
    if isinstance(kind, LimitNumber):
        return True
    return isinstance(kind, click.IntRange) and kind.min is not None and kind.min >= 0


def _numeric_parameters(
    commands: dict[str, click.Command],
) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """(bounded parameters by command, every other numeric parameter)."""
    bounded: dict[str, set[str]] = {}
    other: set[tuple[str, str]] = set()
    for path, command in commands.items():
        for param in command.params:
            if param.name is None:
                continue
            if _bounded(param.type):
                bounded.setdefault(path, set()).add(param.name)
            elif isinstance(param.type, click.types.IntParamType | click.types.FloatParamType):
                other.add((path, param.name))
    return bounded, other


_LIMIT_CASES = [
    (c, n) for c in sorted(EXPECTED_LIMIT_PARAMETERS) for n in sorted(EXPECTED_LIMIT_PARAMETERS[c])
]


class TestEveryNumericParameterIsALimit:
    """Layer 3: every numeric option and argument of every command (#583)."""

    def test_each_is_a_limit_or_a_recorded_decision(self) -> None:
        bounded, other = _numeric_parameters(_commands())
        assert other == set(NOT_LIMITS)
        assert bounded == EXPECTED_LIMIT_PARAMETERS

    @pytest.mark.parametrize(
        ("path", "name"), _LIMIT_CASES, ids=[f"{c}:{n}" for c, n in _LIMIT_CASES]
    )
    def test_each_refuses_what_cannot_bound_anything(self, path: str, name: str) -> None:
        param = next(p for p in _commands()[path].params if p.name == name)
        kind = param.type
        base = kind.base if isinstance(kind, LimitNumber) else kind
        bad = ["-1"] + (["nan", "inf"] if isinstance(base, click.types.FloatParamType) else [])
        for value in bad:
            with pytest.raises(click.BadParameter):
                kind.convert(value, param, None)
        if isinstance(kind, LimitNumber):
            assert kind.convert("0", param, None) == 0

    def test_an_argument_is_named_in_its_refusal(self) -> None:
        param = next(p for p in _commands()["run"].params if p.name == "max_iterations")
        with pytest.raises(click.BadParameter, match="MAX_ITERATIONS must be >= 0, got -1"):
            param.type.convert("-1", param, None)

    def test_control_a_plain_number_and_an_unsafe_range_are_flagged(self) -> None:
        @click.group()
        def group() -> None: ...

        @group.command()
        @click.option("--a", type=float)
        @click.option("--b", type=click.FloatRange(min=0))
        @click.option("--c", type=click.IntRange(max=5))
        @click.option("--d", type=LimitNumber(click.FLOAT))
        @click.option("--e", type=click.IntRange(min=0))
        def leaf(a: float, b: float, c: int, d: float, e: int) -> None: ...

        bounded, other = _numeric_parameters(_commands(group))
        assert bounded == {"leaf": {"d", "e"}}
        assert other == {("leaf", "a"), ("leaf", "b"), ("leaf", "c")}

    def test_control_click_float_alone_accepts_nan(self) -> None:
        """Why the type exists: click's FLOAT, and FloatRange(min=0), take nan."""
        value = click.FLOAT.convert("nan", None, None)
        assert value != value
        ranged = click.FloatRange(min=0).convert("nan", None, None)
        assert ranged != ranged


def _env_doors(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, ConfigSection, dataclasses.Field[object]]]:
    """The census, run with every name unset, so an exported value on the
    machine running the tests can neither move the baseline nor be lost."""
    names = [
        n
        for n in names_read_by_kstrl()
        if n not in NOT_KSTRL_SETTINGS and n not in RETIRED_ENV_VARS
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    return env_number_doors(names, config_sections(), Path(str(tmp_path)))


class TestEveryNumericEnvironmentVariable:
    """Layer 4: every variable kstrl reads that sets a numeric field (#583).

    The variables are every name ``tests/test_env_vars_documented.py``
    finds in ``kstrl/``; the ones that set a numeric field are found by
    setting them (``env_number_doors``). Each must be refused by the real
    entry check, in one line naming it, when set to ``lots``, ``nan`` or
    (unless the field is signed) ``-1``. Before #583,
    ``KSTRL_AGENT_BUDGET_USD=lots`` read as no ceiling.
    """

    def test_the_census_is_pinned(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        doors = _env_doors(tmp_path, monkeypatch)
        assert len(doors) == EXPECTED_ENV_DOORS
        reached = {(class_name(s), f.name) for _, s, f in doors}
        assert {(class_name(s), f.name) for s, f in _CENSUS} - reached == EXPECTED_NO_ENV_DOOR

    def test_each_refuses_what_cannot_bound_anything(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        offenders: dict[str, list[str]] = {}
        for var, _section, field in _env_doors(tmp_path, monkeypatch):
            for value in ["lots", "nan"] + ([] if field.metadata.get("signed") else ["-1"]):
                monkeypatch.setenv(var, value)
                lines = _lines(tmp_path, "")
                monkeypatch.delenv(var)
                if len(lines) != 1 or var not in lines[0] or value not in lines[0]:
                    offenders[f"{var}={value}"] = lines
        assert offenders == {}

    def test_control_the_census_finds_a_door_and_skips_a_string(self, tmp_path: object) -> None:
        @dataclasses.dataclass
        class _Probe:
            seconds: float = 0.0
            label: str = ""

            @classmethod
            def load(cls, root: object) -> _Probe:
                import os

                return cls(
                    seconds=float(os.environ.get("KSTRL_PROBE_583_S", "0")),
                    label=os.environ.get("KSTRL_PROBE_583_L", ""),
                )

        doors = env_number_doors(
            ["KSTRL_PROBE_583_S", "KSTRL_PROBE_583_L"],
            [ConfigSection(("probe",), _Probe.load)],
            Path(str(tmp_path)),
        )
        assert [(var, f.name) for var, _, f in doors] == [("KSTRL_PROBE_583_S", "seconds")]


class TestBlindSpots:
    @pytest.mark.xfail(
        strict=True,
        reason="blind spot: check_numbers sees the value a loader ends with, so a loader "
        "that drops a bad env number before it lands passes it; Layer 4 drives that shape "
        "only for a loader config_sections() lists",
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

    @pytest.mark.xfail(
        strict=True,
        reason="blind spot: a variable whose value lands scaled (hours read into a "
        "seconds field) is not found by env_number_doors, so Layer 4 does not drive it",
    )
    def test_a_variable_that_lands_scaled_is_a_door(self, tmp_path: object) -> None:
        @dataclasses.dataclass
        class _Scaled:
            seconds: float = 0.0

            @classmethod
            def load(cls, root: object) -> _Scaled:
                import os

                return cls(seconds=3600 * float(os.environ.get("KSTRL_PROBE_583", "0")))

        doors = env_number_doors(
            ["KSTRL_PROBE_583"], [ConfigSection(("probe",), _Scaled.load)], Path(str(tmp_path))
        )
        assert [(var, f.name) for var, _, f in doors] == [("KSTRL_PROBE_583", "seconds")]

    @pytest.mark.xfail(
        strict=True,
        reason="blind spot: a variable whose name is built at run time is not a name "
        "the census of tests/test_env_vars_documented.py can read, so Layer 4 never sets it",
    )
    def test_a_name_built_at_run_time_is_in_the_census(self) -> None:
        from tests.test_env_vars_documented import kstrl_name_constants, literal_reads

        source = 'import os\nsuffix = "LIMIT"\nx = os.environ[f"KSTRL_{suffix}"]\n'
        assert "KSTRL_LIMIT" in {*kstrl_name_constants(source), *literal_reads(source)}


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


class TestTheTomlDoorLooksInsideTablesAndArrays:
    """``section_table`` refuses a nan or inf nested in a table or an array.

    No loader reads a nested number today, so Layer 1 cannot see this door
    reaching inside a value; it is pinned here so a nested numeric setting
    added later is refused before any loader coerces it (#571).
    """

    @pytest.mark.parametrize(
        ("value", "source"),
        [
            ({"inner": [1.0, float("nan")]}, "probe.inner"),
            ([1.0, float("inf")], "probe"),
            ({"inner": {"deeper": float("-inf")}}, "probe.inner.deeper"),
        ],
        ids=["array-in-table", "array", "table-in-table"],
    )
    def test_a_nested_non_finite_value_is_refused(
        self, tmp_path: object, value: object, source: str
    ) -> None:
        from pathlib import Path

        from kstrl.config_toml import section_table

        document = {"timeout": {"probe": value}}
        with pytest.raises(BudgetConfigError) as caught:
            section_table(document, "timeout", Path(str(tmp_path)) / "kstrl.toml")
        assert str(caught.value).startswith(f"{source} must be a finite number, got ")

    def test_nested_finite_values_and_strings_pass(self, tmp_path: object) -> None:
        from pathlib import Path

        from kstrl.config_toml import section_table

        document = {"timeout": {"probe": {"inner": [1.0, -2.0, "nan"]}}}
        table = section_table(document, "timeout", Path(str(tmp_path)) / "kstrl.toml")
        assert table == {"probe": {"inner": [1.0, -2.0, "nan"]}}
