"""Shared fixtures for the ``ks check --compare-baseline`` tests (R10.6, #227).

``tests/test_check_baseline_document.py`` (the artifact on disk and every way
reading it fails) and ``tests/test_check_baseline_cli.py`` (the command
surface) both build baselines from these. They lived in
``tests/test_check_baseline.py`` until that file's unit tests were folded into
the CLI tests; the helpers moved here so both siblings keep one definition.
"""

from __future__ import annotations

from pathlib import Path

from kstrl import baseline

#: The digest both sides of a comparison carry unless a test is about the
#: refusal itself. A literal, because `verify_digest` is what produces a real
#: one and pinning its output here would make this helper a second
#: implementation of it.
DIGEST = "0" * 16

#: The project both sides carry unless a test is about the mismatch note.
PROJECT = "0xfauzi/kstrl"

#: `docs/baseline.md`, shared with `tests/test_check_baseline_cli.py`, which
#: imports this constant rather than defining its own (#400).
BASELINE_DOC = Path(__file__).resolve().parents[2] / "docs" / "baseline.md"


def _baseline(
    signatures: dict[str, int] | None = None,
    *,
    # Sorted, because that is the order the document round-trips through:
    # `to_document` sorts every collection so the file diffs cleanly.
    measured: tuple[str, ...] = ("linter", "test_suite", "typecheck"),
    unmeasured: tuple[str, ...] = (),
    reasons: dict[str, str] | None = None,
    check_schema_version: int = 2,
    base_ref: str | None = "0123456789abcdef",
    passed: bool = False,
    project: str = PROJECT,
    digest: str = DIGEST,
) -> baseline.Baseline:
    return baseline.Baseline(
        generated_at="2026-09-06T00:00:00Z",
        base_ref=base_ref,
        project=project,
        passed=passed,
        check_schema_version=check_schema_version,
        verify_digest=digest,
        measured_checks=measured,
        unmeasured_checks=unmeasured,
        # Defaulted from `unmeasured` so a test that names a hole does not have
        # to spell the reason twice; `from_document` requires the two key sets
        # to match, and this helper keeps that true by construction.
        unmeasured_reasons=reasons or {name: "did not run" for name in unmeasured},
        signatures=dict(signatures or {}),
    )
