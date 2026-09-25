"""#427: a JSON reader may not enumerate the exceptions ``json.loads`` raises.

The rule: the only module in ``kstrl/`` that may call ``json.load`` or
``json.loads`` is ``kstrl/jsonread.py``. Every other reader calls
``read_json`` or ``read_json_file``, which normalises everything the
parser raises into ``json.JSONDecodeError`` or a subclass of it, so no
site's exception handler had to change.

TWO LAYERS, the same split ``tests/test_toml_readers.py`` uses, and each
catches what the other cannot. LAYER 1 (``test_no_module_gets_hold_of_
json_without_appearing_here``) is a census of every expression in
``kstrl/`` that SPELLS ``json``, per module: it enumerates no node types,
so it reaches a parser obtained through ``importlib.import_module("json")``
or through a table, in a module whose count then moves. LAYER 2
(``test_every_json_parse_in_the_package_is_inside_the_owner``) resolves
names and says which call, in which module, resolves to a real parse: it
catches a new ``json.loads`` appearing in a module that is ALREADY on
layer 1's list, which moves that module's count without saying where the
parse went.

The walk is UNGATED, unlike the tomllib guard's ``"tomllib" in text``
precondition. Measured over ``kstrl/``: gating on ``"json" in text``
would drop only three files from the walk (69 of 144 contain the token,
against 3 of 127 for ``tomllib``) while saving 0.32 s (366 ms ungated
against 48 ms gated) and it would also cost three of the fifteen
undecided rows below, which the gate would make invisible rather than
reported. The saving is not worth the blind spot it buys, so this walk
carries no substring gate at all.

``handler_verdict`` and ``guarded_parses`` are IMPORTED from
``tests/test_toml_readers``, not re-implemented: the rule about what a
parse handler may catch, and the walk that finds every ``try`` around a
resolved parse, each has one definition in this repo and this guard
consults those. An earlier version of this file re-walked for the FIRST
matching ``try`` and stopped there, which let a mutant owner enumerate
its exceptions in an inner ``try`` under an outer bare ``except
Exception: raise`` and clear the check on the outer clause alone (#427
simplify pass, group A).

Disclosed blind spot, not covered by either layer: a ``json.loads`` call
that lives inside a Python source string built for a subprocess, such as
``kstrl/fixtures.py``'s embedded function-fixture runner, is invisible
to a walk of ``kstrl/``'s own AST, because the walk sees a string
constant there, not a call.
"""

from __future__ import annotations

import json
import pickle

import pytest

from tests.helpers.astwalk import (
    Sites,
    assert_census,
    assert_sites,
    bindings,
    blind_spot,
    calls_to,
    label,
    module_name,
    package_sources,
    parse,
    parsed,
    spells,
)
from tests.test_toml_readers import guarded_parses, handler_verdict

JSON_MODULE = "json"
JSON_PARSE_TARGETS = frozenset({"json.load", "json.loads"})
OWNER = "jsonread.py"


#: Every expression in ``kstrl/`` that spells ``json``, per module.
#: DERIVED BY RUNNING, not copied from any measurement doc: write this
#: dict empty, run the test, and read the ``Found:`` dict out of the
#: failure. It must be smaller than the pre-change anchor of 38 modules
#: and 164 nodes, and it must contain a row for ``jsonread.py``.
EXPECTED_JSON_SPELLINGS: dict[str, int] = {
    "agents/claude_code.py": 4,
    "agents/claude_sdk.py": 3,
    "agents/liveness.py": 3,
    "agents/sdk_runner.py": 4,
    "atomicio.py": 2,
    "autonomy.py": 2,
    "calibration_baseline.py": 2,
    "cli.py": 7,
    "context.py": 2,
    "baseline.py": 2,
    "decompose.py": 4,
    "events.py": 2,
    "evolution.py": 2,
    "feature_cmd.py": 3,
    "fixtures.py": 4,
    "fixtures_snapshot.py": 2,
    "inbox.py": 4,
    "init_cmd.py": 3,
    "init_wizard.py": 2,
    "intake_github.py": 7,
    "jsonread.py": 4,
    "knowledge.py": 6,
    # #508: the import and one json.dumps of the scorer's report; no parse.
    "learning_fixture.py": 2,
    "linear.py": 5,
    "observability.py": 4,
    "policy.py": 2,
    "prd.py": 2,
    "sandbox.py": 3,
    "serve.py": 5,
    "signals.py": 3,
    "statedir.py": 2,
    "verify.py": 1,
    "workqueue.py": 9,
}


#: Every parse layer 2 resolves, keyed by module and origin, with line
#: numbers dropped. One row: the owner's own parse.
EXPECTED_JSON_PARSES: tuple[str, ...] = ("jsonread.py json.loads",)


#: The fifteen ``.load``/parenless calls layer 2 cannot type, re-derived
#: by running the walk at 6a354cc. None of them is a json parse and none
#: of them moves under this change; a row ARRIVING means the walk stopped
#: being able to decide a call it used to decide. Line numbers dropped,
#: which is what ``.without_line_numbers()`` does to the found side too.
EXPECTED_UNDECIDED_CALLS: tuple[str, ...] = (
    "autonomy.py AutonomyState.load",
    "config.py cls.load",
    "evolution.py cls.load",
    "gateparse.py TOOL_PARSERS[chosen]",
    "gateparse.py TOOL_PARSERS[name]",
    "intake_github.py ProcessedLedger(root_dir).load",
    "serve.py OpenPrCountStreak.load",
    "serve.py ServeConfig.load",
    "tui/app.py initial_screens_for_kind(kind, observe_only=False)",
    "tui/app.py initial_screens_for_kind(kind, observe_only=True)",
    "tui/screens/evolve.py self.query_one(ConfigProblemBanner).load",
    "tui/screens/inbox.py self.query_one(ConfigProblemBanner).load",
)


def _spellings(source: str) -> int:
    """How many nodes in one snippet spell ``json``. Layer 1, on text."""
    sees = spells(JSON_MODULE)
    return sum(1 for node in __import__("ast").walk(parse(source)) if sees(node))


class TestTheWalkSeesWhatItClaimsTo:
    """The guard's own guard, against planted source strings, so the walk
    is exercised by something other than a repo that must report nothing."""

    PARSE_FORMS: list[tuple[str, str, str]] = [
        ("literal", "import json\n", "json.loads(s)"),
        ("module_alias", "import json as _j\n", "_j.loads(s)"),
        ("from_import", "from json import loads\n", "loads(s)"),
        ("from_import_alias", "from json import loads as _l\n", "_l(s)"),
        ("module_rebind", "import json\n_p = json\n", "_p.loads(s)"),
        ("function_rebind", "import json\n_l = json.loads\n", "_l(s)"),
        ("annotated_module_rebind", "import json\n_p: object = json\n", "_p.loads(s)"),
        ("getattr", "import json\n", 'getattr(json, "loads")(s)'),
        ("load_handle", "import json\n", "json.load(fh)"),
    ]

    @pytest.mark.parametrize(
        ("name", "prologue", "call"), PARSE_FORMS, ids=[n for n, _, _ in PARSE_FORMS]
    )
    def test_a_parse_is_found_through_every_alias_form(
        self, name: str, prologue: str, call: str
    ) -> None:
        source = f"{prologue}def f(s, fh):\n    return {call}\n"
        sites = calls_to(parse(source), JSON_PARSE_TARGETS)

        assert len(sites.seen) == 1, f"{name}: walk did not see the parse"
        assert sites.undecided == ()

    def test_a_local_module_of_the_same_name_is_not_the_stdlib(self) -> None:
        sites = calls_to(
            parse("from .json import loads\nloads(s)\n"),
            JSON_PARSE_TARGETS,
            module="kstrl.config",
        )

        assert sites == Sites()

    def test_a_parse_through_a_name_the_walk_cannot_follow_is_undecided(self) -> None:
        source = 'import json\nT = {"f": json.loads}\nT["f"](s)\n'
        sites = calls_to(parse(source), JSON_PARSE_TARGETS)

        assert len(sites.undecided) == 1
        assert sites.seen == ()

    def test_the_package_walk_reaches_the_owner(self) -> None:
        """If kstrl stops parsing JSON at all, this guard is decoration
        and should be deleted rather than left."""
        modules = {label(source) for source in package_sources()}

        assert OWNER in modules

        owner_source = next(s for s in package_sources() if label(s) == OWNER)
        tree = parsed(owner_source)
        sites = calls_to(
            tree, JSON_PARSE_TARGETS, where=label(owner_source), module="kstrl.jsonread"
        )

        assert len(sites.seen) == 1, "the owner's own parse is the single-parse pin"


class TestNoJsonReaderEnumeratesItsExceptions:
    """The class of defect #427 is about, caught structurally."""

    def test_no_module_gets_hold_of_json_without_appearing_here(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=spells(JSON_MODULE),
            expected=EXPECTED_JSON_SPELLINGS,
            control="import json\njson.loads(s)\n",
            message=(
                "The set of places that name the json module changed. A new JSON READ must go "
                "through kstrl.jsonread: json.loads raises JSONDecodeError, plain ValueError "
                "(CPython's 4300-digit integer-string limit) AND RecursionError (a RuntimeError, "
                "nested arrays), and the taxonomy is json's to extend. A new json.dumps is fine; "
                "update the row in the same diff and say which it was."
            ),
        )

    def test_every_json_parse_in_the_package_is_inside_the_owner(self) -> None:
        found = Sites()
        for source in package_sources():
            tree = parsed(source)
            found += calls_to(
                tree, JSON_PARSE_TARGETS, where=label(source), module=module_name(source)
            )

        assert_sites(
            found.sorted().without_line_numbers(),
            seen=EXPECTED_JSON_PARSES,
            undecided=EXPECTED_UNDECIDED_CALLS,
            message=(
                "The set of json parses in kstrl/ changed. kstrl.jsonread is the only module that "
                "may call json.load or json.loads; every other reader calls read_json or "
                "read_json_file, which normalises everything the parse raises."
            ),
        )

    def test_the_owner_ends_on_a_bare_exception_clause(self) -> None:
        """Judges EVERY ``try`` around a resolved parse in the owner, not
        just the first one found.

        An earlier version of this test walked for the first matching
        ``try`` and stopped, which a mutant owner cleared by wrapping its
        enumeration in an INNER ``try: ... except (ValueError,
        RecursionError)`` under an OUTER bare ``except Exception: raise``:
        the outer clause is what a first-match walk sees, and it judges
        compliant. ``guarded_parses`` (shared with the tomllib guard,
        parameterised by target set) collects every ``try`` holding a
        resolved parse, so the inner one is judged too.
        """
        owner_source = next(s for s in package_sources() if label(s) == OWNER)
        tree = parsed(owner_source)
        table = bindings(tree, module="kstrl.jsonread")

        guarded, _ = guarded_parses(tree, table, JSON_PARSE_TARGETS)
        assert guarded, "no try in the owner holds a resolved json parse"
        offenders = [
            f"line {lineno}: {verdict}"
            for lineno, clauses in guarded
            if (verdict := handler_verdict(clauses)) is not None
        ]
        assert not offenders, (
            f"{offenders[0]}. The owner must end on a bare `except Exception`, above which "
            "sits a pass-through `except json.JSONDecodeError: raise`, the way "
            "kstrl.config_toml.load_toml_document does, and EVERY try around a resolved "
            "parse must satisfy this, not just the first one found."
        )


class TestTheOwnerNormalisesWhatTheParserRaises:
    """Behavioural, and the real control on the structural handler check."""

    def test_a_deeply_nested_document_is_a_json_decode_error(self) -> None:
        from kstrl.jsonread import JsonDocumentError, read_json

        deeply_nested = "[" * 100000 + "]" * 100000

        with pytest.raises(json.JSONDecodeError) as excinfo:
            read_json(deeply_nested)

        assert type(excinfo.value) is JsonDocumentError
        assert "RecursionError" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, RecursionError)

    def test_an_overlong_integer_is_a_json_decode_error(self) -> None:
        from kstrl.jsonread import JsonDocumentError, read_json

        with pytest.raises(json.JSONDecodeError) as excinfo:
            read_json("1" * 5000)

        assert type(excinfo.value) is JsonDocumentError
        assert isinstance(excinfo.value.__cause__, ValueError)
        assert "4300 digits" in str(excinfo.value)

    def test_a_plain_syntax_error_comes_out_unchanged(self) -> None:
        from kstrl.jsonread import read_json

        with pytest.raises(json.JSONDecodeError) as excinfo:
            read_json("{not json")

        assert type(excinfo.value) is json.JSONDecodeError
        assert "line 1 column 2 (char 1)" in str(excinfo.value)

    def test_the_wrapper_message_carries_no_fabricated_position(self) -> None:
        from kstrl.jsonread import JsonDocumentError

        assert str(JsonDocumentError("boom")) == "boom"

    def test_bytes_and_str_both_parse(self) -> None:
        from kstrl.jsonread import read_json

        assert read_json('{"a": 1}') == read_json(b'{"a": 1}') == {"a": 1}

    def test_read_json_file_reads_the_handle_outside_the_guard(self, tmp_path: object) -> None:
        from kstrl.jsonread import JsonDocumentError, read_json_file

        deeply_nested = "[" * 100000 + "]" * 100000
        path = tmp_path / "deep.json"  # type: ignore[attr-defined]
        path.write_text(deeply_nested, encoding="utf-8")

        with open(path, encoding="utf-8") as handle:
            with pytest.raises(JsonDocumentError):
                read_json_file(handle)

        class _RaisesOSError:
            def read(self) -> str:
                raise OSError("disk fell over")

        with pytest.raises(OSError):
            read_json_file(_RaisesOSError())

    def test_a_keyboard_interrupt_is_not_relabelled(self) -> None:
        from kstrl.jsonread import read_json_file

        class _RaisesKeyboardInterrupt:
            def read(self) -> str:
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            read_json_file(_RaisesKeyboardInterrupt())

    def test_the_error_survives_a_pickle_round_trip(self) -> None:
        # Safe: round-trips an exception this test constructs itself, the
        # same pattern tests/test_phase_c_coverage.py's C8 test uses. No
        # untrusted data crosses pickle here.
        from kstrl.jsonread import JsonDocumentError

        back = pickle.loads(pickle.dumps(JsonDocumentError("boom")))

        assert type(back) is JsonDocumentError
        assert str(back) == "boom"


class TestTheDisclosedLimits:
    """Two strict xfails, matching the toml guard's pattern."""

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason="layer 1 folds, it does not run")
    def test_a_module_name_the_interpreter_has_to_build_is_missed(self) -> None:
        blind_spot(
            _spellings,
            'import importlib\nimportlib.import_module("".join(("js", "on"))).loads(s)\n',
        )

    @pytest.mark.xfail(
        strict=True, raises=AssertionError, reason="a call inside a string constant is not a call"
    )
    def test_a_parse_inside_a_subprocess_source_string_is_missed(self) -> None:
        blind_spot(
            _spellings,
            '_RUNNER = "import json\\ndef f(s):\\n    return json.loads(s)\\n"\n',
        )
