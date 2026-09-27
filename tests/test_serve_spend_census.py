"""#587's guard: every run `ks factory` opens is one `ks serve` can charge.

`ks serve` charges a launch for the run directories its `ks factory` child
wrote: the factory run by its kind, and the decompose run the architect ran
as (#567) by the pid its ``factory_started`` event names. Two changes would
lose that charge with nothing failing, and each layer below is closed over
what it counts rather than over a list of known offenders.

LAYER 1, THE CENSUS. Every ``open_command_run`` call and every
``RunStarted`` construction in ``kstrl/`` is counted per function. A new
one is an unexplained delta, so the diff that adds it is where somebody
says whether `ks serve` can charge the run it opens.

LAYER 2, THE RULES. (a) The runs the `ks factory` command opens itself are
exactly one, of ``ARCHITECT_RUN_KIND``: a second one, or another kind, is
spend ``owned_run_spend`` never reads. (b) Every ``RunStarted`` names its
process with ``pid=os.getpid()``: a decompose run that does not cannot be
told from an operator's, so `ks serve` never charges it.

Both rules FLAG: a kind or a pid they cannot prove is a hit. Every pin is
DERIVED BY RUNNING: empty the dict, run the test, and read the ``Found:``
dict out of the failure. Never edit a count by hand.

WHAT IT DOES NOT SEE. A ``RunStarted`` or ``open_command_run`` reached
through another name (``Started = RunStarted``) is not counted, and a run
the `ks factory` command opens through a helper it calls is not rule (a)'s,
only the census's; the blind-spot test below pins the first.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from kstrl.serve import ARCHITECT_RUN_KIND, SPAWNED_RUN_KIND
from tests.helpers.astwalk import blind_spot

KSTRL = Path(__file__).resolve().parent.parent / "kstrl"
CLI = KSTRL / "cli.py"

OPENER = "open_command_run"
STARTED = "RunStarted"

#: The names a kind argument may spell, and what each is. Exact names only,
#: because a match here CLEARS an argument.
KIND_NAMES = {"ARCHITECT_RUN_KIND": ARCHITECT_RUN_KIND, "SPAWNED_RUN_KIND": SPAWNED_RUN_KIND}

#: ``"<file relative to kstrl/>:<function> <callee>"`` -> count.
EXPECTED_SITES: dict[str, int] = {
    "cli.py:_understand_core RunStarted": 1,
    "cli.py:decompose open_command_run": 1,
    "cli.py:decompose._target open_command_run": 1,
    "cli.py:factory open_command_run": 1,
    "cli.py:feature open_command_run": 1,
    "cli.py:feature._target open_command_run": 1,
    "cli.py:understand open_command_run": 1,
    "cli.py:understand._target open_command_run": 1,
    "decompose.py:_decompose_spec_impl RunStarted": 1,
    "factory.py:_run_factory_locked RunStarted": 1,
    "feature_cmd.py:run_feature RunStarted": 1,
    "tui/session.py:_prepare_decompose.build.target open_command_run": 1,
}


def _callee(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def sites(tree: ast.AST, where: str) -> list[tuple[str, ast.Call]]:
    """Every opener call and ``RunStarted`` construction, keyed by function."""
    found: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, f"{scope}.{child.name}" if scope else child.name)
                continue
            if isinstance(child, ast.Call) and _callee(child) in (OPENER, STARTED):
                found.append((f"{where}:{scope or '<module>'} {_callee(child)}", child))
            visit(child, scope)

    visit(tree, "")
    return found


def _kind(call: ast.Call) -> str | None:
    """The kind an ``open_command_run`` call passes; None when unprovable."""
    keyword = next((word.value for word in call.keywords if word.arg == "kind"), None)
    arg = call.args[2] if len(call.args) > 2 else keyword
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return KIND_NAMES.get(arg.id)
    return None


def kinds_opened(function: ast.AST) -> list[str | None]:
    """The kind each ``open_command_run`` in ``function`` passes."""
    return [
        _kind(node)
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _callee(node) == OPENER
    ]


def names_its_process(call: ast.Call) -> bool:
    """Whether a ``RunStarted`` construction passes ``pid=os.getpid()``."""
    value = next((word.value for word in call.keywords if word.arg == "pid"), None)
    return (
        isinstance(value, ast.Call)
        and not value.args
        and not value.keywords
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "getpid"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "os"
    )


def _package_sites() -> list[tuple[str, ast.Call]]:
    return [
        site
        for path in sorted(KSTRL.rglob("*.py"))
        for site in sites(
            ast.parse(path.read_text(encoding="utf-8")), path.relative_to(KSTRL).as_posix()
        )
    ]


def _factory_command() -> ast.FunctionDef:
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    (command,) = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "factory"
    ]
    return command


class TestTheCensus:
    def test_every_run_opening_is_counted(self) -> None:
        found = dict(Counter(key for key, _call in _package_sites()))

        assert found == EXPECTED_SITES, (
            "A run is opened somewhere new, or no longer where it was. Decide "
            "whether `ks serve` charges the run it opens (serve.owned_run_spend), "
            f"then re-derive this pin by running. Found: {found}"
        )

    def test_the_census_sees_both_shapes(self) -> None:
        source = (
            "def launch():\n"
            "    open_command_run(ui, root, 'decompose')\n"
            "    bus.emit(ev.RunStarted(project='p'))\n"
        )

        keys = [key for key, _call in sites(ast.parse(source), "m.py")]

        assert keys == ["m.py:launch open_command_run", "m.py:launch RunStarted"]


class TestTheFactoryOpensOnlyWhatServeCharges:
    def test_the_factory_command_opens_one_architect_run(self) -> None:
        assert kinds_opened(_factory_command()) == [ARCHITECT_RUN_KIND]

    @pytest.mark.parametrize(
        ("call", "expected"),
        [
            ("open_command_run(ui, root, 'understand')", ["understand"]),
            ("open_command_run(ui, root, kind=ARCHITECT_RUN_KIND)", [ARCHITECT_RUN_KIND]),
            ("open_command_run(ui, root, kind)", [None]),
            ("open_command_run(ui, root, KIND)", [None]),
        ],
    )
    def test_a_kind_is_read_or_flagged(self, call: str, expected: list[str | None]) -> None:
        assert kinds_opened(ast.parse(f"def factory():\n    {call}\n")) == expected


class TestEveryRunStartedNamesItsProcess:
    def test_every_construction_passes_its_pid(self) -> None:
        started = [call for key, call in _package_sites() if key.endswith(f" {STARTED}")]
        missing = [ast.unparse(call) for call in started if not names_its_process(call)]

        assert started, "the walk found no RunStarted at all, so it checked nothing"
        assert missing == [], missing

    @pytest.mark.parametrize(
        ("call", "names"),
        [
            ("RunStarted(project='p', pid=os.getpid())", True),
            ("RunStarted(project='p')", False),
            ("RunStarted(project='p', pid=0)", False),
            ("RunStarted(project='p', pid=process_id)", False),
            ("RunStarted(**fields)", False),
        ],
    )
    def test_a_pid_is_read_or_flagged(self, call: str, names: bool) -> None:
        node = ast.parse(call, mode="eval").body
        assert isinstance(node, ast.Call)
        assert names_its_process(node) is names


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_blind_spot_a_run_started_under_another_name() -> None:
    blind_spot(
        lambda source: bool(sites(ast.parse(source), "m.py")),
        "Started = RunStarted\nStarted(project='p', pid=0)\n",
    )
