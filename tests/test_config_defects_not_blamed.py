"""The config entry points name a broken config and never blame it for a kstrl defect.

``config_preflight.resolve_or_report`` returns a loaded section or the line
the entry check would have printed for it. This file pins the line it
draws, and the census that keeps that line derived from ``kstrl/``:

- a ``RuntimeError`` that kstrl defines is operator input and is reported;
- a ``RuntimeError`` that kstrl did not define (a bare one, a
  ``NotImplementedError``, a ``RecursionError``) is a defect in kstrl and
  is raised again with its traceback, never blamed on the operator's file;
- the rule is derived from the package by walking it, so no list goes stale.

The file was ``test_tui_config_guard.py`` until the TUI went (#863 A1).
Its screen tests went with the TUI. The tests that remain call
``resolve_or_report``, which ``RunEnvelope`` still calls.
"""

from __future__ import annotations

import ast
import importlib
import os
import stat
import sys
from pathlib import Path

import pytest

from kstrl.config_preflight import (
    collect_config_problems,
    raise_if_defect,
    resolve_or_report,
)
from kstrl.evolution_config import EvolutionConfig
from tests.helpers import astwalk

BAD_KNOB = '[evolution]\nlookback_runs = "many"\n'
BAD_DOCUMENT = "[evolution\nlookback_runs = 5\n"
GOOD_KNOB = "[evolution]\nlookback_runs = 5\n"


# --- the domain rule, derived from kstrl/ rather than listed --------------

#: The base ``raise_if_defect`` draws its line at.
_DOMAIN_BASE = "RuntimeError"

#: Every node in ``kstrl/`` that writes the identifier ``RuntimeError``:
#: the net under the walk below, enumerating no node types, since a
#: class cannot subclass what its module never spells. Rows that are not
#: a class base are bare raises and one ``isinstance``, pinned anyway
#: because separating them by shape is the guessing #324 costs.
EXPECTED_RUNTIMEERROR_SPELLINGS: dict[str, int] = {
    # #700 slice 7: two `except (OSError, RuntimeError, ValueError)` clauses,
    # one around the designer's reply (a timeout or output cap is a
    # RuntimeError) and one around starting it. Caught, never subclassed.
    "acceptance_design.py": 2,
    # AgentOutputTooLarge, and AgentTimedOut since #603.
    "agent_output.py": 2,
    "autonomy.py": 1,
    # One bare raise: `_resolve_factory_run_lock` refuses a handed
    # `RunLock` whose own `release()` has already run (#597 fix round).
    "cli_preflight.py": 1,
    "config_preflight.py": 2,  # the rule itself, and its docstring
    # DecisionRegisterError, added by #332.
    "decisions.py": 1,
    # #632: each refuses to import a table whose rows disagree, a bare raise.
    # One subclass, four bare raises. #543 added two: a dependency branch
    # that will not merge into a dependent's worktree, and a HEAD that
    # cannot be read when recording what the dependent is judged against.
    # #566 added one: a single_pr retry with no recorded start commit.
    # #601's own review round found the run_level-is-None raise in
    # _human_rejections unreachable (the ladder is None only when
    # autonomy is disabled) and deleted it, so the census stays at 6.
    # #776 F2 moved four of the five to factory_worktrees.py.
    "factory.py": 1,
    "factory_lock.py": 1,
    "factory_worktrees.py": 4,
    "git.py": 1,
    "inbox.py": 1,
    # IntakeError (the subclass). #776 I1 moved it out of intake_github.py.
    "intake_gh.py": 1,
    # A bare raise in `_run_command` when a `SteerCommand` reaches no
    # handler in `_STEER_HANDLERS` - the closed-by-construction guard's
    # unreachable branch (A3), which is operator-unreachable but still counted.
    # #776 I2 moved it out of intake_github.py.
    "intake_steering.py": 1,
    # OwnerAnswerError (#639 slice 4).
    "owner_answers.py": 1,
    "pr.py": 4,  # no subclass: four bare raises
    # One bare raise, plus four from the R10.7 open-PR bound (#228), all
    # in count_open_kstrl_prs: a failed gh result, the two bad-payload
    # shapes, and a row that is not a PR record. check_open_pr_bound
    # catches `Exception`, not `RuntimeError`, so it contributes none.
    # The transport failures belong to run_gh, counted against
    # intake_gh.py rather than here.
    # #700 slice 2: ProvenRung.command raises a bare RuntimeError when a
    # refused rung, or one with no policy, reaches a command.
    "rung.py": 1,
    # ChildOutputDecodeError, added by #416.
    "scrubbed_run.py": 1,
    "serve_config.py": 1,
    "serve_github.py": 4,
    # SignalsError, R8.8 slice 1 / #155.
    "signals.py": 1,
    # One since #232: ControlLockedError and ControlUnavailableError now
    # derive from a shared ControlStateError, which is the only class in
    # the module that spells RuntimeError. They are still kstrl's, so
    # raise_if_defect still treats them as operator input, and the walk
    # below follows them by subclass rather than by spelling.
    "statedir.py": 1,
    "temp_worktree.py": 1,
    "workqueue_items.py": 1,
}


def _domain_subclass_names(tree: ast.Module, module: str) -> list[str]:
    """The classes in one module that derive DIRECTLY from RuntimeError.

    The base is resolved through the module's own imports before its
    last segment is read, so ``class X(builtins.RuntimeError)`` and an
    aliased import both count; matching ``ast.Name`` alone, which is
    what this walk did until #324, missed both.
    """
    table = astwalk.bindings(tree, module=module)
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for base in node.bases
        if (table.resolve(base) or astwalk.dotted(base) or "").rsplit(".", 1)[-1] == _DOMAIN_BASE
    ]


def _kstrl_domain_errors() -> list[type[BaseException]]:
    """Every ``RuntimeError`` subclass ``kstrl/`` declares, imported.

    TRANSITIVELY, since #232 gave ``statedir`` a shared
    ``ControlStateError`` base: the AST walk above finds the classes that
    SPELL ``RuntimeError``, which is the census this file pins, and the
    two that now derive from one of those would otherwise have dropped
    out of the behaviour check below without any of them changing what
    ``raise_if_defect`` does with them.

    Every module in the package is imported FIRST, on its own line,
    rather than as a side effect of the comprehension below. The
    comprehension imports one module per NAME it finds, so a module that
    declares no direct ``RuntimeError`` subclass was never imported by
    it: measured on this package, 27 of 130 modules stayed unimported in
    a process that already had 100 of them loaded for other reasons. A
    domain error declared in one of those was invisible to
    ``__subclasses__``, so the sentence claiming this walk is exact was
    the only thing making it exact.
    """
    for path in astwalk.package_sources():
        importlib.import_module(astwalk.module_name(path))
    direct = [
        getattr(importlib.import_module(astwalk.module_name(path)), name)
        for path in astwalk.package_sources()
        for name in _domain_subclass_names(astwalk.parsed(path), astwalk.module_name(path))
    ]
    seen: dict[str, type[BaseException]] = {}
    pending = list(direct)
    while pending:
        cls = pending.pop()
        key = f"{cls.__module__}.{cls.__qualname__}"
        if key in seen:
            continue
        seen[key] = cls
        pending.extend(
            sub
            for sub in cls.__subclasses__()
            if sub.__module__.split(".")[0] == "kstrl"  # ours, not a dependency's
        )
    return list(seen.values())


class TestSharedGuard:
    def test_the_screen_says_exactly_what_the_command_says(self, tmp_path: Path) -> None:
        """The point of #289: one file, one wording, two surfaces."""
        (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
        from_cli = collect_config_problems(
            tmp_path,
            warn=lambda _m: None,
            required=frozenset({"evolution"}),
        )
        _config, from_screen = resolve_or_report(
            EvolutionConfig.load,
            tmp_path,
            blame_env=True,
        )
        assert from_cli == [from_screen]

    def test_env_blame_is_skipped_while_a_run_is_in_flight(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Measuring the blame clears os.environ process-wide.

        A launched home-shell session runs the factory on another
        thread of THIS process and its subprocesses inherit the
        environment, so the variable's name is given up rather than the
        run corrupted. Everything else in the line survives.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "lots")
        _config, problem = resolve_or_report(EvolutionConfig.load, tmp_path, blame_env=False)
        assert problem is not None
        assert "[evolution]" in problem
        assert "invalid literal for int() with base 10: 'lots'" in problem
        assert "set by" not in problem

        _again, blamed = resolve_or_report(EvolutionConfig.load, tmp_path, blame_env=True)
        assert blamed is not None
        assert "set by KSTRL_EVOLUTION_LOOKBACK_RUNS=lots" in blamed

    def test_a_clean_config_returns_the_object_and_no_problem(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[evolution]\nlookback_runs = 3\n", encoding="utf-8")
        config, problem = resolve_or_report(EvolutionConfig.load, tmp_path, blame_env=True)
        assert problem is None
        assert config is not None
        assert config.lookback_runs == 3

    def test_an_unenrolled_loader_is_a_defect_not_a_message(self, tmp_path: Path) -> None:
        """The label comes from config_sections(), so a screen cannot
        invent a section the entry check does not know.

        Raised on the FAILURE path only: the lookup moved into the
        except branch because `config_sections()` costs a measured
        6.2 ms on its first call and the happy path never needs a
        label. An unenrolled loader that resolves cleanly has nothing
        to mislabel.
        """

        def _rejecting_unenrolled_loader(_root: Path) -> int:
            raise ValueError("nope")

        with pytest.raises(LookupError):
            resolve_or_report(_rejecting_unenrolled_loader, tmp_path, blame_env=False)

        def _clean_unenrolled_loader(_root: Path) -> int:
            return 1

        assert resolve_or_report(_clean_unenrolled_loader, tmp_path, blame_env=False) == (1, None)

    def test_a_bare_runtimeerror_is_re_raised_not_blamed_on_the_operator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-one review, finding 10.

        SURFACE_REJECTIONS names RuntimeError for the domain errors
        that DERIVE from it, which are operator input. A bare one is a
        kstrl defect, and rendering it as "configuration unreadable"
        would blame the operator's file and eat the traceback. This is
        the same line EvolutionConfig.load_or_none draws.
        """
        import kstrl.evolution_config

        def _explode(root_dir: Path | None = None) -> None:
            raise RuntimeError("journal config exploded")

        monkeypatch.setattr(kstrl.evolution_config.EvolutionConfig, "load", _explode)
        with pytest.raises(RuntimeError, match="journal config exploded"):
            resolve_or_report(EvolutionConfig.load, tmp_path, blame_env=False)

    @pytest.mark.parametrize(
        "defect",
        [
            RuntimeError("bare"),
            NotImplementedError("abstract loader"),
            RecursionError("cycle in the config graph"),
        ],
        ids=["bare", "not_implemented", "recursion"],
    )
    def test_every_runtimeerror_kstrl_did_not_define_is_re_raised(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        defect: RuntimeError,
    ) -> None:
        """Round-two review, finding 2.

        The first cut of the test above wrote ``type(exc) is
        RuntimeError``, which is true only of a BARE one.
        NotImplementedError and RecursionError are direct RuntimeError
        subclasses and unambiguously defects, and both were reported to
        the operator as "configuration unreadable" with the traceback
        eaten. RecursionError is the one that hurts: a cycle in the
        config graph would have been rendered as their broken file.
        """
        import kstrl.evolution_config

        def _explode(root_dir: Path | None = None) -> None:
            raise defect

        monkeypatch.setattr(kstrl.evolution_config.EvolutionConfig, "load", _explode)
        with pytest.raises(type(defect)):
            resolve_or_report(EvolutionConfig.load, tmp_path, blame_env=False)

    def test_the_domain_rule_is_derived_from_kstrl_not_listed(self) -> None:
        """No ledger to go stale.

        The reviewer's suggested fix was a tuple of the four domain
        errors named in REJECTIONS' docstring. kstrl defines TEN
        RuntimeError subclasses, so that tuple would have been wrong on
        the day it was written and staler with each new one. The rule
        asks a derivable question instead - did kstrl define this class
        - and this test walks the package to check the answer holds for
        every one of them.
        """
        subclasses = _kstrl_domain_errors()
        assert len(subclasses) >= 10, subclasses
        # Closed under subclassing within kstrl: a domain error that
        # derives from another domain error rather than from
        # RuntimeError directly is still the operator's input, and
        # #232 added the first two of those. Derived, not listed.
        for cls in subclasses:
            for descendant in cls.__subclasses__():
                if descendant.__module__.split(".")[0] == "kstrl":
                    assert descendant in subclasses, descendant
        for cls in subclasses:
            raise_if_defect(cls("operator input"))  # must not raise
        for defect in (RuntimeError, NotImplementedError, RecursionError):
            with pytest.raises(defect):
                raise_if_defect(defect("ours"))
        # And nothing outside RuntimeError is this rule's business.
        raise_if_defect(ValueError("a knob"))
        raise_if_defect(OSError("a file"))

    def test_the_domain_walk_imports_the_whole_package(self) -> None:
        """The property the transitive closure rests on, asserted.

        ``__subclasses__`` only knows about classes that have been
        imported, so a domain error in a module nothing pulled in is
        invisible to the loop above. The comprehension imports one module
        per NAME it finds, which is 11 of this package's 130; the rest
        are imported on their own line. Without that line this walk is
        exact only by luck of what some other test already loaded, and
        the mutation that removes it fails here rather than nowhere.
        """
        _kstrl_domain_errors()
        missing = [
            name
            for path in astwalk.package_sources()
            if (name := astwalk.module_name(path)) not in sys.modules
        ]
        assert missing == [], missing

    def test_the_walk_sees_a_base_it_has_to_resolve_to_recognise(self) -> None:
        """The control: an empty subclass list and a switched-off
        matcher read the same, and until #324 this walk matched a bare
        ``ast.Name``, so both spellings below were invisible."""
        dotted = astwalk.parse("class X(builtins.RuntimeError): pass\n")
        aliased = astwalk.parse("from builtins import RuntimeError as RE\nclass Y(RE): pass\n")
        assert _domain_subclass_names(dotted, "kstrl.made_up") == ["X"]
        assert _domain_subclass_names(aliased, "kstrl.made_up") == ["Y"]
        # ... and an unrelated base is still not this rule's business.
        assert _domain_subclass_names(astwalk.parse("class Z(ValueError): pass\n"), "m") == []

    def test_every_module_that_spells_the_domain_base_is_pinned(self) -> None:
        """The net: no class subclasses what its module never spells."""
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=astwalk.spells(_DOMAIN_BASE),
            expected=EXPECTED_RUNTIMEERROR_SPELLINGS,
            control="class X(RuntimeError):\n    pass\n",
            message="the set of places kstrl/ spells RuntimeError changed. Add the row.",
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_subclass_of_a_subclass_is_a_disclosed_limit(self) -> None:
        """``class Deeper(ServeError)`` is not seen, and need not be.

        Direct bases only. The bound: the rule asks
        ``type(exc).__module__``, so a transitive subclass declared in
        ``kstrl/`` is operator input at run time either way. Coverage is
        what is lost, not correctness. Measured: zero exist today.
        """
        astwalk.blind_spot(
            lambda source: _domain_subclass_names(astwalk.parse(source), "kstrl.made_up"),
            "from kstrl.serve import ServeError\nclass Deeper(ServeError):\n    pass\n",
        )

    def test_a_domain_runtimeerror_is_still_reported(self, tmp_path: Path) -> None:
        """The other side of the same line: ServeError and friends are
        operator input and must still reach the banner."""
        from kstrl.serve_config import ServeConfig

        (tmp_path / "kstrl.toml").write_text(
            "[serve]\nmax_consecutive_poison = 0\n",
            encoding="utf-8",
        )
        _config, problem = resolve_or_report(ServeConfig.load, tmp_path, blame_env=False)
        assert problem is not None
        assert "[serve]" in problem

    @pytest.mark.skipif(os.name == "nt" or os.getuid() == 0, reason="root reads a 0000 file")
    def test_an_unreadable_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        """OSError is in scope for a surface, not for the entry check.

        The entry check reads the document itself before any loader
        runs; a screen re-reading minutes later has no such pass in
        front of it, and a chmod between two refreshes lands here.
        """
        toml = tmp_path / "kstrl.toml"
        toml.write_text(GOOD_KNOB, encoding="utf-8")
        toml.chmod(0o000)
        try:
            _config, problem = resolve_or_report(
                EvolutionConfig.load,
                tmp_path,
                blame_env=False,
            )
        finally:
            toml.chmod(stat.S_IRUSR | stat.S_IWUSR)
        assert problem is not None
        assert "[evolution]" in problem
        assert "Permission denied" in problem
