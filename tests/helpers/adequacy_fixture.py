"""The shared fixture for R8.5 Layers 1 and 2 (#152 simplify pass, D1).

``tests/test_patch_coverage.py``'s module docstring claims the fixture in
``tests/test_diff_mutation.py`` is shared "on purpose, it is already
measured there", and until this module existed nothing enforced that:
editing one file's copy left both suites green while the sentence became
false. What was actually duplicated, verbatim or near it, across the two
files: the four source constants (``BASE_MOD``, ``BASE_TEST``,
``FEAT_MOD``, ``FEAT_TEST``), the ``_repo`` builder, the "one gap, no row"
assertion (``_only_gap``), and most of ``_run``'s body - Layer 2's ``_run``
is a strict superset of Layer 1's, adding ``mutation_timeout``,
``diff_mutation`` and ``read_only``.

``_repo`` is NOT hoisted whole: the two files commit a DIFFERENT
``conftest.py`` on the base branch (Layer 1's counts test-command
invocations and can stall the coverage spawn for its own timeout test;
Layer 2 needs neither and uses an empty one), so ``base_files`` differs
per caller and cannot be a shared module-level constant. What is
identical between the two files is the BUILDER's body - `make_review_repo`
called with a base commit and a feature commit - so :func:`repo_builder`
is that body, closed over the caller's own ``base_files``/``feat_files``,
returned as a ready-to-call ``_repo``.

``_only_gap`` keeps ``check`` as its second positional parameter, matching
every existing call site in ``tests/test_diff_mutation.py`` (#152's own
history is call sites written against three positional arguments, and
this file changes what backs them, not their shape); a caller that wants
only ONE check name, over and over - ``tests/test_patch_coverage.py`` -
writes the two-line forward that fixes it, rather than :func:`only_gap`
growing a mode for a caller count of one.

``run_adequacy`` matches R8.5 Layer 2's own ``_run`` exactly, since that
file's version is the superset: Layer 1's tests import it directly
(``diff_mutation`` defaults ``False``, the harness default too, so their
behaviour is unchanged bit for bit), and Layer 2's tests bind a
``functools.partial`` that flips the ONE default it needs - every
parameter past ``root`` is keyword-only, so the partial's bound keyword
can never collide with a positional fill the way ``_only_gap``'s
``check`` could.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.adequacy import AdequacyConfig
from kstrl.verify import VerifyConfig, run_mechanical_verification
from tests.conftest import make_review_repo

if TYPE_CHECKING:
    from kstrl.verify import NotMeasured, VerificationResult

BASE_MOD = "def covered_before(n):\n    return n + 1\n"
BASE_TEST = (
    "from mod import covered_before\n\n\ndef test_before():\n    assert covered_before(1) == 2\n"
)
FEAT_MOD = (
    "def covered_before(n):\n    return n + 1\n\n\ndef added_covered(n):\n    return n * 2\n"
    "\n\ndef added_missing(n):\n    x = n - 1\n    y = x * 3\n    return y\n"
)
FEAT_TEST = (
    "from mod import added_covered, covered_before\n\n\ndef test_before():\n"
    "    assert covered_before(1) == 2\n\n\ndef test_added():\n    assert added_covered(3) == 6\n"
)


def repo_builder(base_files: dict[str, str], feat_files: dict[str, str]) -> Callable[..., None]:
    """Bind ``base_files``/``feat_files`` and return a ``_repo(tmp_path,
    files=None)`` closure - the one builder body both test files wrote by
    hand, now written once. ``files`` defaults to ``feat_files`` (the
    calling convention both files already used), overridden per test for
    the branches that commit something else (a comment-only change, a
    pre-existing ``.bak``, and so on)."""

    def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> None:
        make_review_repo(
            tmp_path, base_files=base_files, files=files if files is not None else feat_files
        )

    return _repo


def only_gap(result: VerificationResult, check: str, reason: str) -> NotMeasured:
    """The single ``check`` gap in ``result``: no row alongside it,
    exactly one gap, and it carries ``reason``. Positional
    ``(result, check, reason)`` to match every call site
    ``tests/test_diff_mutation.py`` already wrote."""
    assert [c for c in result.checks if c.name == check] == []
    gaps = [g for g in result.not_measured if g.check == check]
    assert len(gaps) == 1, gaps
    assert gaps[0].reason == reason, gaps[0]
    return gaps[0]


def run_adequacy(
    root: Path,
    *,
    base_branch: str = "main",
    test_command: str | None = None,
    subprocess_timeout: float = 120.0,
    mutation_timeout: float = 120.0,
    enabled: bool = True,
    patch_coverage: bool = True,
    diff_mutation: bool = False,
    read_only: bool = False,
    mutation_testing: bool = False,
    mutation_threshold: float = 50.0,
) -> VerificationResult:
    """Drive the real ``run_mechanical_verification`` over ``root`` with
        R8.5's config, every knob either file needs. ``diff_mutation`` and
        ``read_only`` default to the harness's own defaults (``False``), so
        ``tests/test_patch_coverage.py`` uses this directly; Layer 2's tests
        bind ``functools.partial(run_adequacy, diff_mutation=True)`` instead
        of redefining the body to flip one default.

    ``mutation_testing`` and ``mutation_threshold`` (#391) are the third
    caller of this fixture: ``[verify] mutation_testing`` is Layer 1's own
    gate, and ``tests/test_mutation_score.py`` needs both knobs to drive it
    through ``run_mechanical_verification`` rather than calling
    ``check_mutation_score`` directly.
    """
    return run_mechanical_verification(
        root,
        None,
        base_branch,
        None,
        VerifyConfig(
            test_command=test_command or f"{shlex.quote(sys.executable)} -m pytest",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=subprocess_timeout,
            mutation_timeout=mutation_timeout,
            mutation_testing=mutation_testing,
            mutation_threshold=mutation_threshold,
        ),
        adequacy_config=AdequacyConfig(
            enabled=enabled, patch_coverage=patch_coverage, diff_mutation=diff_mutation
        ),
        read_only=read_only,
    )
