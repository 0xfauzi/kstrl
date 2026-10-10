"""What one iteration is told: project context, last fast-check measurement, prompt assembly."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.verify import VerificationResult, VerifyConfig, run_fast_checks

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: H3 (#233): the block that hands the next engineer iteration the
#: failures ``[verify] fast_iteration_checks`` measured after this one.
#: Engineer-facing context, enrolled in tests/helpers/builder_prompts.py;
#: the calibration suite scores no fixture for it, so it carries the H3
#: obligation and no H2 obligation the suite can discharge.
LAST_ITERATION_MEASUREMENT_PROMPT_VERSION = "1.0.0"

LAST_ITERATION_MEASUREMENT_PROMPT = (
    "=== LAST ITERATION MEASUREMENT ===\n{failures}\n=== END LAST ITERATION MEASUREMENT ==="
)


def fast_iteration_reading(
    verify_config: VerifyConfig | None,
    cwd: Path,
    ui: UI,
    *,
    completed: bool,
    timed_out: bool,
) -> VerificationResult | None:
    """Run ``[verify] fast_iteration_checks`` after an iteration (#233).

    None, and nothing runs, when no gate is configured, when no gate runs
    for this loop at all (``verify_config`` None), or when the iteration
    completed or was killed: a completed iteration goes to Phase 1, and a
    killed one left a tree nobody finished writing.
    """
    if completed or timed_out or verify_config is None:
        return None
    if not verify_config.fast_iteration_checks:
        return None
    reading = run_fast_checks(cwd, verify_config)
    ui.info(
        "Fast checks: "
        + ", ".join(f"{c.name} {'pass' if c.passed else 'FAIL'}" for c in reading.checks)
    )
    return reading


def failed_fast_checks(reading: VerificationResult | None) -> tuple[str, ...]:
    """The names of the gates that failed in ``reading``, in run order."""
    if reading is None:
        return ()
    return tuple(check.name for check in reading.checks if not check.passed)


def measurement_block(reading: VerificationResult | None) -> str:
    """The prompt block for ``reading``'s failures, or "" when none failed."""
    if reading is None or reading.passed:
        return ""
    return LAST_ITERATION_MEASUREMENT_PROMPT.format(failures=reading.as_context())


def assemble_prompt(retry_context: str | None, measurement: str, body: str) -> str:
    """One iteration's prompt: the retry context, then the last iteration's
    measurement, then ``body`` (project context plus the prompt template).

    An empty part is left out with its separator, so with no measurement
    this is byte for byte the assembly before #233. The measurement is
    passed in whole every iteration, so a later reading REPLACES an
    earlier one rather than being appended to it.
    """
    head = [part for part in (retry_context, measurement) if part]
    return "\n\n".join([*head, body])


def build_project_context(
    cwd: Path,
    ui: UI,
    verify_config: VerifyConfig | None = None,
    *,
    context_root: Path | None = None,
) -> str:
    """Assemble the project-context prefix of the engineer prompt.

    Two sections: the project's CLAUDE.md, if it has one, and the
    ``[stack]`` block naming the checks kstrl will run (#696).

    ``context_root`` is where CLAUDE.md is read, ``cwd`` when None. Only the
    factory passes a different root (#569).

    ``verify_config`` is the config the checks will run with, and ``None``
    means NOTHING runs them for this invocation, so no checks are stated.
    None is the default on purpose: a default that assumed a gate told a
    read-only mapping run to execute the whole test suite on every pass.
    Only a caller that can name what it will run gets to make the claim.

    Who names one, as of #288:

    - ``pipeline._phase_verify`` (the factory) passes the exact object
      its gate reads. It HALTS on a failure.
    - ``feature_cmd`` passes the object its report reads, to the
      implement and repair loops only. It does NOT halt: a failing check
      is reported and the flow proceeds.
    - ``ks understand``, and ``ks feature``'s understand loop, still pass
      None. Nothing checks an understand file, so None is still true
      there.

    A config with no ``[stack]`` states nothing either: there is no check
    to name, and Phase 1 fails closed on it.
    """
    sections: list[str] = []
    claude_root = cwd if context_root is None else context_root
    claude_md_path = claude_root / "CLAUDE.md"
    if claude_md_path.exists():
        claude_md = claude_md_path.read_text(encoding="utf-8")
        sections.append("# Project Context (from CLAUDE.md)\n\n" + claude_md)
    if verify_config is not None and verify_config.project_stack is not None:
        sections.append(verify_config.project_stack.format_for_prompt())
    return "\n\n".join(sections)
