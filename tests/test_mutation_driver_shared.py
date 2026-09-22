"""Driver-behaviour cases R8.5 Layer 1 (``[verify] mutation_testing``) and
Layer 2 (``[adequacy] diff_mutation``) answer identically, because #391
made them reach mutmut through ONE shared driver (:func:`kstrl.verify._mutmut_measure`
and its two pre-flights). Before the #391 simplify pass on PR #392 (B1),
these lived as near-duplicate test bodies in ``tests/test_mutation_score.py``
and ``tests/test_diff_mutation.py`` - one of them, the fatal-exit case, was
byte-identical apart from the check-name string, and three more measured at
0.82 to 1.00 similarity. Forking a shared property into two test bodies is
the exact duplication #391 exists to delete, one layer out.

Every case is parametrised over ``(check, run)``: ``check`` is the row/gap
name the assertions key on, and ``run`` is ``run_adequacy`` bound with
ONLY that check's own gate on - the identical partials
``tests/test_mutation_score.py`` and ``tests/test_diff_mutation.py`` each
already bind for their OWN, file-specific tests. Binding them again here,
rather than importing the sibling file's private ``_run``, keeps neither
test file a dependency of the other.

B2's third case - a ``[verify] test_command`` mutmut's ``--runner`` cannot
wrap - is NOT here: Layer 2 cannot reach its own copy of that refusal
directly. ``AdequacyConfig`` refuses ``diff_mutation=true`` with
``patch_coverage=false``, and ``check_patch_coverage`` validates the
identical ``test_command`` through its own ``_pytest_tokens_or_gap`` call
FIRST, so a bad command always gaps ``patch_coverage`` before
``_diff_mutation_preflight`` is ever reached - see
``test_the_layer_one_gap_reason_is_inherited_not_guessed`` in
``tests/test_diff_mutation.py``. That case is Layer-1-only, in
``tests/test_mutation_score.py::test_a_test_command_mutmuts_runner_cannot_wrap_is_tool_missing``.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.helpers.adequacy_fixture import (
    EMPTY_CONFTEST_BASE_FILES,
    FEAT_FILES,
    no_spawn_gap,
    only_gap,
    only_row,
    repo_builder,
    run_adequacy,
)
from tests.helpers.fakemutmut import junit, put_failing_mutmut, put_mutmut_on_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from kstrl.verify import VerificationResult

    _Run = Callable[..., VerificationResult]

#: The identical repo both mutation-driver test files build (#391
#: simplify pass on PR #392, B5): a base commit with a plain, empty
#: ``conftest.py`` and the shared feature commit.
_repo = repo_builder(EMPTY_CONFTEST_BASE_FILES, FEAT_FILES)

#: ``run_adequacy`` with ONLY Layer 1's gate on - identical to
#: ``tests/test_mutation_score.py``'s own ``_run``.
_run_layer1 = functools.partial(
    run_adequacy, mutation_testing=True, patch_coverage=False, enabled=False
)

#: ``run_adequacy`` with ONLY Layer 2's gate on - identical to
#: ``tests/test_diff_mutation.py``'s own ``_run``.
_run_layer2 = functools.partial(run_adequacy, diff_mutation=True)

#: ``(check, run)``, one row per mutation-backed check. The check name is
#: also the ``ids=`` label, so a failure names the check directly rather
#: than an opaque index.
_CASES: list[tuple[str, _Run]] = [
    ("mutation_testing", _run_layer1),
    ("diff_mutation", _run_layer2),
]
_CASE_IDS = [check for check, _run in _CASES]


@pytest.mark.parametrize(("check", "run"), _CASES, ids=_CASE_IDS)
def test_a_fatal_mutmut_exit_is_a_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str,
    run: _Run,
) -> None:
    """D1/D11: a fatal mutmut exit (bit 1 of its return code) is
    ``command_failed``, and no report spawn follows a run that never
    produced one. The real failure measured without the ``whatthepatch``
    extra (measurements.md section 2a) - exit 1, before any test runs."""
    _repo(tmp_path)
    recdir = put_failing_mutmut(
        tmp_path,
        monkeypatch,
        stderr=(
            "ImportError: The --use-patch feature requires the whatthepatch "
            'library. Run "pip install --force-reinstall mutmut[patch]"'
        ),
        exit_code=1,
    )
    result = run(tmp_path)
    gap = only_gap(result, check, "command_failed")
    assert "whatthepatch" in gap.detail
    assert not (recdir / "argv-junitxml.txt").exists()


@pytest.mark.parametrize(("check", "run"), _CASES, ids=_CASE_IDS)
def test_an_unparseable_report_is_a_sidecar_not_a_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str,
    run: _Run,
) -> None:
    """CLAUDE.md, and the exact defect #260 shipped twice: what cannot be
    parsed is rejected, never counted as zero. The report spawn ran (the
    run itself succeeded), so this is a ``command_failed`` gap AFTER a
    spawn, not a pre-flight refusal - the detail names why."""
    _repo(tmp_path)
    put_mutmut_on_path(tmp_path, monkeypatch, junit="not xml at all")
    result = run(tmp_path)
    gap = only_gap(result, check, "command_failed")
    assert "the mutation report could not be read" in gap.detail


@pytest.mark.parametrize(("check", "run"), _CASES, ids=_CASE_IDS)
def test_a_preexisting_backup_refuses_before_any_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str,
    run: _Run,
) -> None:
    """D7 step 1, plant 14: a pre-existing ``.bak`` beside a target is
    refused, never silently overwritten - mutmut writes ``<file>.bak``
    before it mutates, so kstrl cannot tell its backup from this one, and
    refuses rather than risk overwriting the project's own file."""
    _repo(tmp_path)
    (tmp_path / "mod.py.bak").write_bytes(b"not mutmut's\n")
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = run(tmp_path)
    no_spawn_gap(result, recdir, check, "command_failed", "mod.py.bak")
    assert (tmp_path / "mod.py.bak").read_bytes() == b"not mutmut's\n"


@pytest.mark.parametrize(("check", "run"), _CASES, ids=_CASE_IDS)
def test_read_only_is_a_sidecar_because_mutmut_rewrites_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str,
    run: _Run,
) -> None:
    """mutmut REWRITES the file it mutates, so neither check can run
    under ``ks check`` (``read_only=True``) at all - the byte-identical
    ``_MUTMUT_READ_ONLY_DETAIL`` both checks return
    (``kstrl/verify.py``)."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = run(tmp_path, read_only=True)
    no_spawn_gap(result, recdir, check, "read_only", "mutmut rewrites")


@pytest.mark.parametrize(("check", "run"), _CASES, ids=_CASE_IDS)
def test_a_project_with_a_mutmut_config_hook_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str,
    run: _Run,
) -> None:
    """A ``pre_mutation`` hook in a project's own ``mutmut_config.py`` is
    the ONLY route to mutmut's ``skipped`` status (measurements.md 2g),
    and ``mutmut/cache.py::create_junitxml_report`` renders a skipped
    mutant IDENTICALLY to a killed one - so a report from such a project
    cannot be trusted, and both checks refuse before any spawn instead
    (``_mutmut_tree_preflight``, shared since #391).

    Before B2 (#391 simplify pass on PR #392) this was tested for
    ``diff_mutation`` only: deleting the refusal from the shared
    ``_mutmut_tree_preflight`` reddened only that one file."""
    _repo(tmp_path)
    (tmp_path / "mutmut_config.py").write_text("def pre_mutation(context):\n    pass\n")
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = run(tmp_path)
    no_spawn_gap(result, recdir, check, "command_failed", "mutmut_config.py")


@pytest.mark.parametrize(("check", "run"), _CASES, ids=_CASE_IDS)
def test_a_stale_cache_is_deleted_before_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str,
    run: _Run,
) -> None:
    """The BEFORE-the-run delete, whose hazard is a stale cache from an
    earlier run being read as THIS run's inventory (``cache.init_db``
    hard-codes the path, so there is no relocation knob) - shared since
    #391's ``_mutmut_measure``.

    Before B2 (#391 simplify pass on PR #392) this was tested for
    ``mutation_testing`` only: deleting the before-the-run delete from
    the shared ``_mutmut_measure`` reddened only that one file."""
    _repo(tmp_path)
    (tmp_path / ".mutmut-cache").write_bytes(b"stale")
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = run(tmp_path)
    assert (recdir / "cache-before-run.txt").read_text(encoding="utf-8").split() == ["absent"]
    only_row(result, check)
    assert not (tmp_path / ".mutmut-cache").exists()
