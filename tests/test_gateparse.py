"""Per-gate parser dispatch and the non-Python parsers (#258).

Every assertion here runs against output captured from the real tool at
a named version (``tests/tool_output``, provenance in
``tests/helpers/tool_output.py``). That is deliberate and it is the
point of the exercise: the issue reasoned from the code that tsc and
eslint would be "close to free" because they emit ``file:line:col``, and
measurement said otherwise for tsc and half-otherwise for eslint. A
hand-written fixture would have encoded the reasoning and passed.
"""

from __future__ import annotations

import pytest

from kstrl.evolution import signatures_from_verification
from kstrl.gateparse import (
    GATE_LINT,
    GATE_TEST,
    GATE_TOOLS,
    GATE_TYPECHECK,
    TOOL_PARSERS,
    parse_gate_output,
    validate_tool,
)
from kstrl.parsers import (
    exception_code,
    parse_mypy_output,
    parse_pytest_output,
    parse_ruff_output,
    strip_ansi,
)
from kstrl.verify import CheckResult
from tests.helpers.tool_output import tool_output

PYTEST_FAILURES = tool_output("pytest-9.1.1-two-failures.txt")
PYTEST_PASSING = tool_output("pytest-9.1.1-passing.txt")
VITEST_FAILURES = tool_output("vitest-2.1.9-two-failures.txt")
VITEST_SUITE_ERROR = tool_output("vitest-2.1.9-suite-load-error.txt")
TSC_PLAIN = tool_output("tsc-5.6.3-plain.txt")
TSC_PRETTY = tool_output("tsc-5.6.3-pretty.txt")
ESLINT_STYLISH = tool_output("eslint-8.57.1-stylish.txt")
ESLINT_UNIX = tool_output("eslint-8.57.1-unix.txt")
ESLINT_COMPACT = tool_output("eslint-8.57.1-compact.txt")
ESLINT_9_STYLISH = tool_output("eslint-9.39.5-stylish.txt")
RUFF_FULL = tool_output("ruff-0.16.1-full.txt")
RUFF_CONCISE = tool_output("ruff-0.16.1-concise.txt")
PYTEST_LONG_NAMES = tool_output("pytest-9.1.1-long-names.txt")
PYTEST_CROSS_FILE = tool_output("pytest-9.1.1-raised-in-another-file.txt")

# mypy needs no capture of its own: two lines is the whole format, and
# the footer is the half that matters here, because tsc opens its own
# with the same five words.
MYPY_FAILURE = (
    'a.py:2: error: Incompatible return value type (got "int", expected "str")'
    "  [return-value]\n"
    "Found 1 error in 1 file (checked 1 source file)\n"
)


class TestVitest:
    """The failure the issue was filed about: 0 parsed, everything dropped."""

    def test_the_old_behaviour_is_the_bug(self) -> None:
        # The baseline this PR moves. Fed to the pytest parser, which is
        # what the gate did unconditionally, real vitest output yields
        # nothing: no file, no line, no test name, no assertion.
        assert parse_pytest_output(VITEST_FAILURES).failures == []

    def test_both_failures_parse_with_file_line_test_and_message(self) -> None:
        parsed = parse_gate_output(VITEST_FAILURES, GATE_TEST)

        assert parsed.tool == "vitest"
        assert [(f.file, f.line) for f in parsed.failures] == [
            ("tests/failing.test.ts", 5),
            ("tests/summary.test.ts", 5),
        ]
        assert parsed.failures[0].rule_or_test == (
            "word counter > counts the words in a draft title"
        )
        assert parsed.failures[0].message == (
            "AssertionError: expected 3 to be 4 // Object.is equality"
        )
        assert parsed.total_errors == 2

    def test_summary_reports_the_failing_half_only(self) -> None:
        parsed = parse_gate_output(VITEST_FAILURES, GATE_TEST)
        assert "2 failed" in parsed.raw_summary
        # Timing noise is not a failure summary; #258 measured a retry
        # prompt whose entire content was vitest's Duration line.
        assert "Duration" not in parsed.raw_summary

    def test_a_suite_that_never_loaded_keeps_the_error_and_refuses_a_wrong_line(self) -> None:
        # Measured: vitest's only stack frame for a failed import points
        # into node_modules/vite. Sending the engineer to a line inside
        # vite's bundle is worse than sending it no line, so the parser
        # takes a location only from a frame in the failing file itself.
        parsed = parse_gate_output(VITEST_SUITE_ERROR, GATE_TEST)

        assert len(parsed.failures) == 1
        failure = parsed.failures[0]
        assert failure.file == "tests/broken-import.test.ts"
        assert failure.line == 0
        assert failure.message.startswith("Error: Failed to load url ../src/render-draft")
        assert "node_modules" not in failure.file


class TestTsc:
    """The issue's own prediction about tsc, disproven by measurement."""

    def test_no_existing_parser_reads_real_tsc_output(self) -> None:
        # The issue expected tsc to emit `file:line:col: message` and so
        # to be nearly free through the mypy or ruff parser. It emits
        # `src/broken.ts(7,9): error TS2322:` through a pipe. All three
        # of the Python parsers find nothing in it.
        assert parse_mypy_output(TSC_PLAIN).failures == []
        assert parse_ruff_output(TSC_PLAIN).failures == []
        assert parse_pytest_output(TSC_PLAIN).failures == []

    @pytest.mark.parametrize("raw", [TSC_PLAIN, TSC_PRETTY], ids=["plain", "pretty"])
    def test_both_output_modes_parse_identically(self, raw: str) -> None:
        # Piped and --pretty are different formats, not different data.
        # An npm script may pass --pretty, so the gate has to read both.
        parsed = parse_gate_output(raw, GATE_TYPECHECK)

        assert parsed.tool == "tsc"
        assert [(f.file, f.line, f.code) for f in parsed.failures] == [
            ("src/broken.ts", 7, "TS2322"),
            ("src/broken.ts", 12, "TS2339"),
            ("src/index.ts", 4, "TS2353"),
        ]
        assert parsed.failures[0].message == "Type 'string' is not assignable to type 'number'."

    def test_pretty_output_still_carries_its_ansi_escapes(self) -> None:
        # Guards the fixture rather than the parser: if a hook ever
        # strips the escapes from that file, the test above stops
        # exercising strip_ansi and silently proves nothing.
        assert "\x1b[" in TSC_PRETTY
        assert "\x1b[" not in strip_ansi(TSC_PRETTY)


class TestEslint:
    """The rule slot, which the ruff parser filled with the wrong token."""

    def test_the_old_ruff_regex_no_longer_claims_eslint_output(self) -> None:
        # Measured before the fix: `--format unix` DID match the ruff
        # pattern, three of three with the right file and line, and put
        # the message's first word in the rule slot - `'cache'`,
        # `Expected`, `'missingHelper'`. Those slugs were then harvested
        # by evolution.py as failure signatures. Requiring a ruff-shaped
        # code (letters then digits) is what stops it, and it is also
        # what keeps the two parsers from double-reporting on the auto
        # path below.
        assert parse_ruff_output(ESLINT_UNIX).failures == []

    @pytest.mark.parametrize(
        "raw",
        [ESLINT_STYLISH, ESLINT_UNIX, ESLINT_COMPACT],
        ids=["stylish", "unix", "compact"],
    )
    def test_every_format_yields_the_real_rule_ids(self, raw: str) -> None:
        parsed = parse_gate_output(raw, GATE_LINT)

        assert parsed.tool == "eslint"
        assert [f.code for f in parsed.failures] == [
            "no-unused-vars",
            "prefer-const",
            "eqeqeq",
            "no-undef",
            "no-unused-vars",
        ]
        assert [f.line for f in parsed.failures] == [4, 4, 5, 6, 3]

    def test_stylish_attaches_diagnostics_to_the_file_header_above_them(self) -> None:
        # stylish is the DEFAULT, which is what `npm run lint` prints and
        # what the issue's suggested `--format compact` is not. It names
        # the file once and indents the rows beneath it.
        parsed = parse_gate_output(ESLINT_STYLISH, GATE_LINT)
        assert [f.file for f in parsed.failures] == [
            "/repo/lint/draft.js",
            "/repo/lint/draft.js",
            "/repo/lint/draft.js",
            "/repo/lint/draft.js",
            "/repo/lint/render.js",
        ]

    def test_eslint_9_stylish_parses_the_same_way(self) -> None:
        # eslint 9 removed the unix and compact formatters from core, so
        # stylish is the only one a current install is guaranteed to
        # have. Measured against 9.39.5: byte-for-byte the same shape.
        parsed = parse_gate_output(ESLINT_9_STYLISH, GATE_LINT)
        assert [f.code for f in parsed.failures] == [
            f.code for f in parse_gate_output(ESLINT_STYLISH, GATE_LINT).failures
        ]

    def test_warnings_are_kept(self) -> None:
        # `--max-warnings 0` makes a warning fail the gate, and dropping
        # warnings would then hand the engineer an empty parse for a
        # gate that genuinely failed.
        codes = [f.code for f in parse_gate_output(ESLINT_STYLISH, GATE_LINT).failures]
        assert "prefer-const" in codes


class TestRuff:
    """The lint gate's PRIMARY parser, measured against ruff 0.16.1."""

    @pytest.mark.parametrize("raw", [RUFF_CONCISE, RUFF_FULL], ids=["concise", "full"])
    def test_both_output_formats_parse_identically(self, raw: str) -> None:
        # `full` is ruff's DEFAULT since 0.9, so it is what kstrl's own
        # DEFAULT_LINT_COMMAND (`uv run ruff check .`) produces, and it
        # is two lines per diagnostic rather than one. Measured before
        # the fix: 0 failures parsed, and the whole retry detail was
        # `[uv run ruff check .] Found 4 errors.` The gate's primary
        # parser could not read its own tool's default output.
        parsed = parse_gate_output(raw, GATE_LINT)

        # Messages are compared too, which is what pins that ruff's
        # `[*]` autofix marker never reaches the engineer: it describes
        # the fix, not the defect, and it sits between the rule and the
        # message in both formats.
        assert parsed.tool == "ruff"
        assert [(f.file, f.line, f.code, f.message) for f in parsed.failures] == [
            ("draft.py", 1, "F401", "`os` imported but unused"),
            ("draft.py", 2, "F401", "`sys` imported but unused"),
            ("draft.py", 6, "F841", "Local variable `cache` is assigned to but never used"),
            ("loader.py", 1, "invalid-syntax", "Expected `)`, found newline"),
        ]

    @pytest.mark.parametrize("raw", [RUFF_CONCISE, RUFF_FULL], ids=["concise", "full"])
    def test_a_syntax_error_keeps_its_file_and_line(self, raw: str) -> None:
        # Ruff reports a file it cannot parse as `invalid-syntax`, with
        # no rule code. Requiring a code-shaped rule slot dropped these
        # entirely, which is the worst class to drop: a syntax error's
        # file and line are the two things an agent cannot guess from
        # the message.
        syntax = [f for f in parse_gate_output(raw, GATE_LINT).failures if f.file == "loader.py"]

        assert len(syntax) == 1
        assert (syntax[0].line, syntax[0].code) == (1, "invalid-syntax")
        assert syntax[0].message == "Expected `)`, found newline"

    def test_the_two_formats_do_not_double_count_each_other(self) -> None:
        # The parser runs a concise pass and a full pass over the same
        # text. Neither format contains the other's shape, so a repo
        # that somehow emitted both would still count each once.
        assert len(parse_gate_output(RUFF_CONCISE + RUFF_FULL, GATE_LINT).failures) == 8


class TestPytestTracebacks:
    """The summary line alone cannot carry the failure (#258 review)."""

    def test_long_test_names_still_yield_a_code_and_a_line(self) -> None:
        # pytest truncates the short summary to the terminal width, 80
        # columns under a pipe, and truncates the MESSAGE first. In this
        # captured run two of five failures carry no message at all and
        # one carries `AssertionErro...`, so reading the summary alone
        # made the evolution signature depend on how long somebody had
        # made the test name. The traceback block has it untruncated.
        parsed = parse_gate_output(PYTEST_LONG_NAMES, GATE_TEST)

        assert parsed.tool == "pytest"
        assert [(f.line, f.code) for f in parsed.failures] == [
            (20, "assertion-error"),
            (11, "file-not-found-error"),
            (30, "assertion-error"),
            (30, "assertion-error"),
            (16, "runtime-error"),
        ]

    def test_a_parametrized_id_with_spaces_parses(self) -> None:
        # `FAILED f.py::test_x[a draft] - ...` did not match at all: the
        # test id was `[^\s]+`. Every parametrized failure whose id
        # contained a space was dropped from the parse in silence.
        tests = [f.rule_or_test for f in parse_gate_output(PYTEST_LONG_NAMES, GATE_TEST).failures]
        assert "test_every_title_has_exactly_one_word[a draft]" in tests

    def test_a_class_based_test_is_matched_to_its_block(self) -> None:
        # The short summary spells it `TestClass::test_method` and the
        # traceback header spells it `TestClass.test_method`.
        failure = next(
            f
            for f in parse_gate_output(PYTEST_LONG_NAMES, GATE_TEST).failures
            if f.rule_or_test.startswith("TestDraftLoading")
        )
        assert (failure.line, failure.message) == (11, "FileNotFoundError: index.md")

    def test_a_setup_error_is_matched_to_its_block(self) -> None:
        # Its block header reads `ERROR at setup of <name>`.
        failure = next(
            f
            for f in parse_gate_output(PYTEST_LONG_NAMES, GATE_TEST).failures
            if f.rule_or_test == "test_uses_a_fixture_that_cannot_build"
        )
        assert failure.code == "runtime-error"

    def test_file_and_line_come_from_the_same_frame(self) -> None:
        # The summary names the file the TEST lives in; the traceback
        # names the file the exception was RAISED in. Taking the line
        # from one and the file from the other produced `test_cross.py:17`
        # for a 7-line test file, and add_source_context would have
        # printed whatever sat at line 17 of some longer file. The
        # raising frame is also the better instruction.
        parsed = parse_gate_output(PYTEST_CROSS_FILE, GATE_TEST)

        assert len(parsed.failures) == 1
        failure = parsed.failures[0]
        assert (failure.file, failure.line) == ("loader.py", 17)
        assert failure.rule_or_test == "test_loads_a_draft_from_a_helper_module_in_another_file"
        assert failure.code == "file-not-found-error"

    def test_a_summary_with_no_traceback_keeps_what_it_had(self) -> None:
        # Nothing is fabricated when the block is absent: `--tb=no` and
        # `-q` runs still parse, with line 0 as before.
        raw = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_a.py::test_x - AssertionError: nope\n"
            "========================= 1 failed in 0.10s ==========================\n"
        )
        parsed = parse_gate_output(raw, GATE_TEST)

        assert len(parsed.failures) == 1
        assert parsed.failures[0].line == 0
        assert parsed.failures[0].code == "assertion-error"


class TestExceptionCode:
    """A bare `Error:` is the commonest shape in JavaScript."""

    def test_a_bare_error_yields_a_code(self) -> None:
        # The class-name prefix used to be mandatory, so `Error` itself
        # could not match. That is every vitest suite-load failure and
        # every plain `throw new Error(...)`.
        assert exception_code("Error: Failed to load url ../src/x") == "error"

    def test_the_vitest_suite_load_fixture_carries_a_code(self) -> None:
        parsed = parse_gate_output(VITEST_SUITE_ERROR, GATE_TEST)
        assert [f.code for f in parsed.failures] == ["error"]

    def test_a_word_merely_starting_with_error_is_not_a_code(self) -> None:
        assert exception_code("Errors were found in the config") == ""


class TestAutoDispatch:
    """Unset `tool` means: run every parser for the gate, union the result."""

    def test_a_chained_command_yields_both_toolchains_failures(self) -> None:
        # The case that kills command-string sniffing. One command,
        # `uv run pytest && (cd web && npm run test)`, two formats, both
        # real. A dispatcher that picks ONE parser is wrong half the
        # time; the union is right by construction.
        parsed = parse_gate_output(PYTEST_FAILURES + VITEST_FAILURES, GATE_TEST)

        assert parsed.tool == "pytest+vitest"
        assert [f.file for f in parsed.failures] == [
            "test_drafts.py",
            "test_drafts.py",
            "tests/failing.test.ts",
            "tests/summary.test.ts",
        ]
        assert parsed.total_errors == 4

    def test_the_passing_half_does_not_stand_in_for_the_failing_one(self) -> None:
        # pytest passes, vitest fails. Before #258's labelling half the
        # engineer was told "5 passed" by a gate that had just failed;
        # now the pytest parser contributes nothing and only the failing
        # toolchain is reported.
        parsed = parse_gate_output(PYTEST_PASSING + VITEST_FAILURES, GATE_TEST)

        assert parsed.tool == "vitest"
        assert len(parsed.failures) == 2
        assert "5 passed" not in "".join(parsed.format_for_prompt())

    def test_a_single_toolchain_is_not_labelled_as_a_union(self) -> None:
        assert parse_gate_output(PYTEST_FAILURES, GATE_TEST).tool == "pytest"
        assert parse_gate_output(TSC_PLAIN, GATE_TYPECHECK).tool == "tsc"

    def test_output_nobody_understands_falls_back_to_the_primary(self) -> None:
        # The floor the labelling half of #258 put in, unchanged: the
        # primary parser's result, tail and all, and prompt_label names
        # the COMMAND rather than claiming a tool parsed it.
        raw = "cargo test\nerror: could not compile `draft` due to 2 previous errors\n"
        parsed = parse_gate_output(raw, GATE_TEST)

        assert parsed.tool == "pytest"
        assert parsed.failures == []
        parsed.command = "cargo test"
        assert parsed.format_for_prompt()[0].startswith("[cargo test]")

    def test_ansi_coloured_output_is_parsed(self) -> None:
        # Whichever parser gets it, it gets it clean: the strip happens
        # once at the dispatcher rather than in six regexes.
        assert len(parse_gate_output(TSC_PRETTY, GATE_TYPECHECK).failures) == 3

    @pytest.mark.parametrize("gate", [GATE_TEST, GATE_TYPECHECK, GATE_LINT])
    def test_every_gate_tool_has_a_registered_parser(self, gate: str) -> None:
        # A name in GATE_TOOLS with no parser behind it is a KeyError at
        # gate time, on the failure path, in a paid run.
        for name in GATE_TOOLS[gate]:
            assert name in TOOL_PARSERS


class TestParserContract:
    """The two rules auto dispatch depends on, enforced rather than assumed."""

    @pytest.mark.parametrize("tool", sorted(TOOL_PARSERS))
    def test_unrecognised_output_still_leaves_a_summary(self, tool: str) -> None:
        # `parse_gate_output` returns the gate's PRIMARY parser when
        # nothing matched, so that parser's raw tail becomes the entire
        # retry detail. A parser that returns an empty summary makes the
        # gate report a failure with nothing in it, silently. Every
        # parser is checked because any of them may be promoted to
        # primary by a reordering of GATE_TOOLS.
        parsed = TOOL_PARSERS[tool]("gradle build FAILED\nsee the report for details\n")

        assert parsed.failures == []
        assert parsed.raw_summary != ""

    # Every parser of every gate appears as an owner, so the loop below
    # also asserts the negative for each of its gate-mates. The earlier
    # version listed only pytest, vitest, tsc and eslint, which left
    # "eslint does not claim ruff output" and "tsc does not claim mypy
    # output" asserted nowhere. The second of those was in fact broken.
    _OWNED = [
        (GATE_TEST, "pytest", PYTEST_FAILURES),
        (GATE_TEST, "pytest", PYTEST_LONG_NAMES),
        (GATE_TEST, "vitest", VITEST_FAILURES),
        (GATE_TEST, "vitest", VITEST_SUITE_ERROR),
        (GATE_TYPECHECK, "mypy", MYPY_FAILURE),
        (GATE_TYPECHECK, "tsc", TSC_PLAIN),
        (GATE_TYPECHECK, "tsc", TSC_PRETTY),
        (GATE_LINT, "ruff", RUFF_CONCISE),
        (GATE_LINT, "ruff", RUFF_FULL),
        (GATE_LINT, "eslint", ESLINT_STYLISH),
        (GATE_LINT, "eslint", ESLINT_UNIX),
        (GATE_LINT, "eslint", ESLINT_COMPACT),
        (GATE_LINT, "eslint", ESLINT_9_STYLISH),
    ]

    @pytest.mark.parametrize(("gate", "owner", "raw"), _OWNED)
    def test_only_the_owning_parser_claims_a_fixture(self, gate: str, owner: str, raw: str) -> None:
        # `_union` concatenates without deduplicating, so two parsers
        # matching the same line double the failures the engineer sees
        # and double total_errors, on the DEFAULT path.
        for name in GATE_TOOLS[gate]:
            found = TOOL_PARSERS[name](strip_ansi(raw)).failures
            assert (found != []) is (name == owner), (
                f"{name} should {'' if name == owner else 'not '}claim this fixture"
            )

    @pytest.mark.parametrize(("gate", "owner", "raw"), _OWNED)
    def test_a_foreign_parser_claims_no_summary_either(
        self, gate: str, owner: str, raw: str
    ) -> None:
        # The failures check above does not cover this, and it is not
        # harmless: `_union` joins every non-empty summary, so a footer
        # two parsers both claim reaches the engineer TWICE. Measured on
        # a chained `mypy && tsc` gate, where tsc's `^Found \d+ error`
        # also matched mypy's footer, and tsc's own count went missing.
        #
        # A parser that understood nothing is still allowed its raw tail
        # fallback, which is several lines; what it may not do is single
        # out one line of another tool's output and call it a summary.
        for name in GATE_TOOLS[gate]:
            if name == owner:
                continue
            summary = TOOL_PARSERS[name](strip_ansi(raw)).raw_summary
            assert "\n" in summary or summary == "", (
                f"{name} claimed a single line of {owner} output as its summary: {summary!r}"
            )


class TestExplicitTool:
    """`tool` set pins the gate to one parser, and a bad name halts."""

    def test_an_override_suppresses_the_other_parser(self) -> None:
        parsed = parse_gate_output(PYTEST_FAILURES + VITEST_FAILURES, GATE_TEST, tool="vitest")

        assert parsed.tool == "vitest"
        assert [f.file for f in parsed.failures] == [
            "tests/failing.test.ts",
            "tests/summary.test.ts",
        ]

    def test_an_override_that_cannot_read_the_output_reports_nothing_rather_than_guessing(
        self,
    ) -> None:
        # Pinning to the wrong tool is the operator's error, and the
        # result is an honest empty parse plus the raw tail, not a
        # silent fallback to a parser they told kstrl not to use.
        parsed = parse_gate_output(VITEST_FAILURES, GATE_TEST, tool="pytest")
        assert parsed.tool == "pytest"
        assert parsed.failures == []

    @pytest.mark.parametrize("absent", [None, ""])
    def test_absent_means_auto(self, absent: str | None) -> None:
        assert validate_tool(GATE_TEST, absent) is None

    def test_an_unknown_name_raises_and_names_the_accepted_values(self) -> None:
        # It must not quietly become auto: the operator wrote the key to
        # stop kstrl guessing, so guessing anyway is the failure the key
        # exists to prevent.
        with pytest.raises(ValueError, match="unknown tool 'jest'.*pytest, vitest"):
            validate_tool(GATE_TEST, "jest")

    def test_a_name_from_another_gate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown tool 'eslint'"):
            validate_tool(GATE_TYPECHECK, "eslint")


class TestEvolutionSignatures:
    """evolution.py reads a capability now, not a tool name (#258)."""

    @staticmethod
    def _signatures(raw: str, gate: str, check: str) -> list[str]:
        parsed = parse_gate_output(raw, gate)
        return signatures_from_verification(
            [CheckResult(name=check, passed=False, message=f"{check} failed", parsed=parsed)]
        )

    def test_every_supported_tool_resolves_to_its_own_codes(self) -> None:
        assert self._signatures(TSC_PLAIN, GATE_TYPECHECK, "typecheck") == [
            "typecheck:TS2322",
            "typecheck:TS2339",
            "typecheck:TS2353",
        ]
        assert self._signatures(ESLINT_STYLISH, GATE_LINT, "linter") == [
            "linter:no-unused-vars",
            "linter:prefer-const",
            "linter:eqeqeq",
            "linter:no-undef",
        ]
        assert self._signatures(VITEST_FAILURES, GATE_TEST, "test_suite") == [
            "test_suite:assertion-error"
        ]

    def test_a_unioned_label_still_resolves(self) -> None:
        # The exact-string switch this replaced compared `parsed.tool`
        # against "pytest", so the joined "pytest+vitest" matched nothing
        # and every signature fell through to a prose slug of the check
        # message. Both halves contribute now.
        sigs = self._signatures(PYTEST_FAILURES + VITEST_FAILURES, GATE_TEST, "test_suite")
        assert sigs == ["test_suite:assertion-error"]
        assert not any("failed" in s.split(":", 1)[1] for s in sigs)

    def test_a_syntax_error_signs_as_itself(self) -> None:
        # `invalid-syntax` reaching the journal at all depends on the
        # ruff rule slot accepting a non-code diagnostic.
        assert "linter:invalid-syntax" in self._signatures(RUFF_FULL, GATE_LINT, "linter")

    def test_long_test_names_do_not_change_the_signature(self) -> None:
        # The whole point of reading the traceback: two runs of the same
        # suite must sign the same way whatever the tests are called.
        # Before, a name long enough to truncate the summary line left
        # the code empty and the signature fell back to a prose slug.
        assert self._signatures(PYTEST_LONG_NAMES, GATE_TEST, "test_suite") == [
            "test_suite:assertion-error",
            "test_suite:file-not-found-error",
            "test_suite:runtime-error",
        ]

    def test_a_gate_no_parser_read_still_produces_a_signature(self) -> None:
        # No codes means the message slug, which is the pre-existing
        # fallback and the only thing left when nothing parsed.
        sigs = self._signatures("cargo test: 2 failed\n", GATE_TEST, "test_suite")
        assert sigs == ["test_suite:test-suite-failed"]
