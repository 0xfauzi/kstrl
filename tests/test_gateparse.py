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
from kstrl.parsers import parse_mypy_output, parse_pytest_output, parse_ruff_output, strip_ansi
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

    @pytest.mark.parametrize(
        ("gate", "owner", "raw"),
        [
            (GATE_TEST, "pytest", PYTEST_FAILURES),
            (GATE_TEST, "vitest", VITEST_FAILURES),
            (GATE_TEST, "vitest", VITEST_SUITE_ERROR),
            (GATE_TYPECHECK, "tsc", TSC_PLAIN),
            (GATE_TYPECHECK, "tsc", TSC_PRETTY),
            (GATE_LINT, "eslint", ESLINT_STYLISH),
            (GATE_LINT, "eslint", ESLINT_UNIX),
            (GATE_LINT, "eslint", ESLINT_COMPACT),
            (GATE_LINT, "eslint", ESLINT_9_STYLISH),
        ],
    )
    def test_only_the_owning_parser_claims_a_fixture(self, gate: str, owner: str, raw: str) -> None:
        # `_union` concatenates without deduplicating, so two parsers
        # matching the same line double the failures the engineer sees
        # and double total_errors, on the DEFAULT path. That invariant
        # is currently held by one hand-tightened regex (ruff's rule
        # slot); this is what says so out loud, for every pair.
        for name in GATE_TOOLS[gate]:
            found = TOOL_PARSERS[name](strip_ansi(raw)).failures
            assert (found != []) is (name == owner), (
                f"{name} should {'' if name == owner else 'not '}claim this fixture"
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

    def test_a_gate_no_parser_read_still_produces_a_signature(self) -> None:
        # No codes means the message slug, which is the pre-existing
        # fallback and the only thing left when nothing parsed.
        sigs = self._signatures("cargo test: 2 failed\n", GATE_TEST, "test_suite")
        assert sigs == ["test_suite:test-suite-failed"]
