"""Which parser a verify gate's output goes through (#258).

Each Phase 1 gate runs ONE operator-configured command, and that command
is not required to be a single tool: ``run_scrubbed`` shells out, so
``test_command = "uv run pytest && (cd web && npm run test)"`` is a
supported and common way to gate a polyglot repo. The gate therefore
cannot assume a toolchain, and until this module existed it assumed one
anyway - pytest for tests, mypy for typecheck, ruff for lint, whatever
had actually run.

The dispatch is a per-gate ``tool`` config key (``[verify] test_tool`` /
``typecheck_tool`` / ``lint_tool``). Unset means auto, and auto runs
EVERY parser registered for the gate and unions what they find.

Three designs were considered and two rejected:

- **Sniff the command string.** Rejected by the case that motivated the
  issue: ``"uv run pytest -q && cd web && npm run test"`` contains both
  ``pytest`` and ``npm``, so a substring rule must pick one and is wrong
  half the time. It also fails the most common invocations of all -
  ``npm run test``, ``make test``, ``just test`` - where no tool name
  appears anywhere. A command string is not evidence of the tool.
- **Try every parser and pick a winner.** A wrong guess is a silent
  semantic substitution, and no tie-break can serve the chain case,
  where one command legitimately emits two formats and both are real.
- **Union what every parser found.** No tie-break to get wrong, the
  chain case works by construction, and a parser that understood
  nothing contributes nothing. This is what auto does.

The floor from the labelling half of #258 is unchanged: when no parser
finds a failure, the primary parser's result is returned, tail fallback
and all, and ``ParsedOutput.prompt_label`` names the COMMAND rather than
claiming a tool parsed output it never saw.
"""

from __future__ import annotations

from collections.abc import Callable

from kstrl.parsers import (
    ParsedOutput,
    parse_mypy_output,
    parse_pytest_output,
    parse_ruff_output,
    strip_ansi,
)
from kstrl.parsers_web import parse_eslint_output, parse_tsc_output, parse_vitest_output

#: Gate names, matching the ``CheckResult.name`` each gate reports under.
GATE_TEST = "test_suite"
GATE_TYPECHECK = "typecheck"
GATE_LINT = "linter"

#: Every parser kstrl can dispatch to, by the name the config key takes.
TOOL_PARSERS: dict[str, Callable[[str], ParsedOutput]] = {
    "pytest": parse_pytest_output,
    "vitest": parse_vitest_output,
    "mypy": parse_mypy_output,
    "tsc": parse_tsc_output,
    "ruff": parse_ruff_output,
    "eslint": parse_eslint_output,
}

#: Which parsers each gate tries on the auto path. The first entry is the
#: gate's PRIMARY: its result is what a caller gets when no parser found
#: anything, so it is the one whose raw-tail fallback the engineer sees.
GATE_TOOLS: dict[str, tuple[str, ...]] = {
    GATE_TEST: ("pytest", "vitest"),
    GATE_TYPECHECK: ("mypy", "tsc"),
    GATE_LINT: ("ruff", "eslint"),
}


def validate_tool(gate: str, tool: str | None) -> str | None:
    """``tool`` if the gate can dispatch to it, None when unset (auto).

    Raises ``ValueError`` naming the accepted values otherwise. An
    unknown name must not quietly become auto: the operator wrote it to
    stop kstrl guessing, and silently guessing anyway is the failure the
    key exists to prevent.
    """
    if not tool:
        return None
    if tool not in GATE_TOOLS[gate]:
        accepted = ", ".join(GATE_TOOLS[gate])
        raise ValueError(f"unknown tool {tool!r} for the {gate} gate; expected one of: {accepted}")
    return tool


def _union(results: list[ParsedOutput]) -> ParsedOutput:
    """Merge the parsers that each found something into one result.

    Correct for one result as well as several, which is why there is no
    separate single-parser path: joining one name yields that name, and
    summing one count yields that count.
    """
    merged = ParsedOutput(tool="+".join(r.tool for r in results))
    for result in results:
        merged.failures.extend(result.failures)
    merged.total_errors = sum(r.total_errors for r in results)
    merged.raw_summary = "\n".join(r.raw_summary for r in results if r.raw_summary)
    return merged


def parse_gate_output(raw: str, gate: str, tool: str | None = None) -> ParsedOutput:
    """Parse one gate's raw output, dispatching per this gate's config."""
    text = strip_ansi(raw)

    chosen = validate_tool(gate, tool)
    if chosen is not None:
        return TOOL_PARSERS[chosen](text)

    results = [TOOL_PARSERS[name](text) for name in GATE_TOOLS[gate]]
    found = [result for result in results if result.failures]
    if not found:
        # Nobody understood it. The primary's result carries the raw
        # tail, which is the only honest thing left to show.
        return results[0]
    return _union(found)
