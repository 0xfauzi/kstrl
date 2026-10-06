"""#272: the whole configuration is resolved at command entry.

kstrl.toml used to be parsed by whichever loader reached its section
first, so a typo's blast radius depended on which section it was in and
which command was run. On the decompose path one of those loaders is
``LinearConfig.load``, which runs after the architect has been invoked
and paid for - 119 to 210 seconds against a frontier model, measured on
a real spec.

Every test here asserts a property of that entry check: that it fires
before anything is constructed, that it covers the environment as well
as the file, that it names what to change, and that the one section
classified as degrading still degrades.

``TestConfigToml`` and ``TestConfigTomlFull`` (#593 slice 4) fold in the
integration tests from tests/test_config_toml.py and
tests/test_config_toml_full.py: real kstrl.toml files on disk driving
``KstrlConfig.load(root)``, ``config_preflight.collect_config_problems``
and the per-section ``Config.load(root)`` classmethods - the entry
points the factory's own phases call. The lower-level ``from_toml`` /
``from_env`` mapping tests and the direct ``load_toml_document`` parser
tests from those files were dropped as unit (class/function pins) per
the owner's end-to-end rule.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result

import kstrl.cli as cli_mod
from kstrl.cli import cli
from kstrl.config import STRING_KEYS, ConfigError, KstrlConfig, load_toml_document, toml_parse_scope
from kstrl.config_preflight import collect_config_problems, config_sections, preflight_config
from kstrl.contract import ContractConfig, ContractMode
from kstrl.evolution import EvolutionConfig
from kstrl.factory import FactoryConfig, FactoryResult
from kstrl.feedforward import CodebaseScanConfig
from kstrl.security import SecurityConfig, SecurityMode
from kstrl.verify import VerifyConfig
from tests.conftest import REPO_ROOT
from tests.helpers import astwalk
from tests.helpers.bad_toml import MALFORMED_TOML, TOML_PARSE_FAULTS
from tests.spine_utils import component, make_manifest

DECOMPOSE_ARGS = [
    "decompose",
    "--spec",
    "s.md",
    "--project-name",
    "p",
    # --agent-cmd keeps the case independent of what is on PATH: without
    # it the run can stop on agent detection and pass for the wrong
    # reason.
    "--agent-cmd",
    "true",
]

FACTORY_ARGS = ["factory", "--manifest", "m.json", "--agent-cmd", "true", "--yes"]


def _invoke(args: list[str], *, toml: str | bytes | None = None) -> Result:
    """Run a command in an isolated checkout holding a spec and manifest.

    ``toml`` takes ``bytes`` as well as ``str`` so a case can put a
    kstrl.toml on disk that no encoding of a ``str`` would produce; see
    :data:`~tests.helpers.bad_toml.TOML_PARSE_FAULTS`. One write for
    both, so the ``str`` cases are utf-8 on a machine whose locale is
    not.

    The cwd IS the checkout. ``conftest.isolate_kstrl_state`` is autouse
    and chdirs every test into its own empty ``tmp_path``, so the
    relative ``s.md`` / ``m.json`` in the arg tables above resolve here
    and a kstrl.toml written here is the one a cwd-rooted preflight
    reads.
    """
    root = Path.cwd()
    (root / "s.md").write_text("# spec\n")
    make_manifest([component("comp-a")]).save(root / "m.json")
    if toml is not None:
        (root / "kstrl.toml").write_bytes(toml.encode() if isinstance(toml, str) else toml)
    return CliRunner().invoke(cli, args, catch_exceptions=True)


def _no_agents(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every agent construction and every architect call."""
    built: list[str] = []

    def fake_get_agent(*args: Any, **kwargs: Any) -> Any:
        built.append("get_agent")
        raise AssertionError("an agent was constructed after a rejected config")

    def fake_decompose_spec(*args: Any, **kwargs: Any) -> Any:
        built.append("decompose_spec")
        raise AssertionError("the architect was invoked after a rejected config")

    monkeypatch.setattr(cli_mod, "get_agent", fake_get_agent)
    monkeypatch.setattr(cli_mod, "decompose_spec", fake_decompose_spec)
    return built


#: The ``*Config`` classes whose loader this walk cannot read, per
#: module. One: ``config.py``'s ``ProgressReaderConfig`` is a Protocol
#: with no loader at all. A class whose ``load`` a decorator or a base
#: supplies lands here too, which is how the shape layer 2 cannot read
#: still fails loudly instead of passing as "no loader".
EXPECTED_LOADERLESS_CONFIGS: dict[str, int] = {"config.py": 1}


def _supplies_load(item: ast.stmt) -> bool:
    """``def load``, ``async def load`` or ``load = _impl``. Round 1 read
    ``FunctionDef`` alone; ``astwalk.assignment_parts`` sees the third."""
    named = isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name == "load"
    return named or "load" in astwalk.assignment_parts(item)[0]


def _config_class(node: ast.AST) -> bool:
    """A ``*Config`` class statement, whatever its body holds."""
    return isinstance(node, ast.ClassDef) and node.name.endswith("Config")


def _no_readable_loader(node: ast.AST) -> bool:
    """Layer 1's predicate: a config class this walk finds no loader in."""
    return _config_class(node) and not any(map(_supplies_load, node.body))  # type: ignore[attr-defined]


def _classes_with_a_loader(tree: ast.Module) -> set[str]:
    """``*Config`` classes in one module whose own body supplies a loader."""
    return {n.name for n in ast.walk(tree) if _config_class(n) and not _no_readable_loader(n)}


def _stub_run_factory(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stop at the factory boundary, recording whether we got there."""
    ran: list[str] = []

    def fake_run_factory(*args: Any, **kwargs: Any) -> FactoryResult:
        ran.append("run_factory")
        return FactoryResult()

    monkeypatch.setattr(cli_mod, "run_factory", fake_run_factory)
    return ran


class TestTheDecomposePathFailsBeforeTheArchitect:
    """The failure #272 was filed about, at the command it was filed
    about, asserted as "nothing was built" rather than as an exit code
    that a later abort would also produce."""

    def test_malformed_toml_stops_before_any_agent_is_constructed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        built = _no_agents(monkeypatch)

        result = _invoke(DECOMPOSE_ARGS, toml=MALFORMED_TOML)

        assert built == []
        assert result.exit_code == 2
        assert "error:" in result.output
        assert "Invalid TOML" in result.output
        # The line and the column, so the operator can go straight there.
        assert "line 1" in result.output

    def test_a_bad_linear_value_stops_before_any_agent_is_constructed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``LinearConfig.load`` is the loader that used to run AFTER the
        architect on this path, which is what made a typo in a section
        the architect never needed cost an architect call."""
        built = _no_agents(monkeypatch)

        result = _invoke(
            DECOMPOSE_ARGS,
            toml='[linear]\ntimeout_seconds = "soon"\n',
        )

        assert built == []
        assert result.exit_code == 2
        assert "[linear]" in result.output
        assert "timeout_seconds" in result.output
        assert "'soon'" in result.output


class TestTheEnvironmentIsCheckedInTheSamePass:
    """Both failures measured on main for #272 are env vars, not toml.

    A file-only preflight would have caught neither, which is the reason
    the check calls each dataclass's own ``load`` - env is overlaid on
    toml in there, by the same coercion that would have raised mid-run.
    """

    @pytest.mark.parametrize(
        ("var", "section"),
        [
            ("KSTRL_MUTATION_THRESHOLD", "[verify]"),
            ("KSTRL_SECURITY_TIMEOUT", "[security]"),
        ],
    )
    def test_a_bad_env_value_is_an_error_line_not_a_traceback(
        self,
        var: str,
        section: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reproduced against main before the fix: exit 1 with a raw
        ValueError traceback and no error line."""
        monkeypatch.setenv(var, "many")
        _stub_run_factory(monkeypatch)

        result = _invoke(FACTORY_ARGS)

        assert not isinstance(result.exception, ValueError), result.exception
        assert result.exit_code == 2
        assert "error:" in result.output
        assert section in result.output
        # Named by REMOVAL: the variable whose absence makes the load
        # succeed, not a variable that happens to look related.
        assert f"set by {var}=many" in result.output


class TestFatalVersusDegrading:
    """The distinction the fix turns on. ``[evolution]`` configures an
    optional audit trail, so continuing without it is honest; ``[verify]``
    configures a GATE, and substituting a default for a check the
    operator configured would report success for a run measured by
    something else.
    """

    def test_a_bad_evolution_knob_warns_and_the_run_still_happens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ran = _stub_run_factory(monkeypatch)

        result = _invoke(FACTORY_ARGS, toml='[evolution]\nlookback_runs = "many"\n')

        assert result.exit_code == 0, result.output
        assert ran == ["run_factory"]
        assert "[evolution]" in result.output
        assert "continuing without it" in result.output

    def test_evolve_treats_the_same_evolution_knob_as_fatal(self) -> None:
        """Degrading means "the audit trail is dropped from work that is
        about something else". `ks evolve` IS the journal, so the value
        the preflight warns about elsewhere has to stop THAT command -
        with the same error line, key and value, rather than a warning
        followed two lines later by the traceback it promised was not
        coming."""
        result = _invoke(["evolve", "--status"], toml='[evolution]\nlookback_runs = "many"\n')

        assert result.exit_code == 2
        assert "error:" in result.output
        assert "[evolution] lookback_runs = 'many'" in result.output
        # Promoted at the seam, so the warning it would otherwise have
        # printed never happens: one report, not two.
        assert "continuing without it" not in result.output
        assert not isinstance(result.exception, ValueError), result.exception

    def test_a_bad_verify_knob_stops_the_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ran = _stub_run_factory(monkeypatch)

        result = _invoke(FACTORY_ARGS, toml='[verify]\nmutation_threshold = "many"\n')

        assert result.exit_code == 2
        assert ran == []
        assert "[verify]" in result.output
        assert "mutation_threshold" in result.output


class TestWhatItNames:
    """A rejection the operator cannot act on is only half a fix."""

    def test_the_toml_key_and_value_are_quoted_from_the_file(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[security]\ntimeout_seconds = "many"\n')

        with pytest.raises(ConfigError) as caught:
            preflight_config(tmp_path, warn=lambda _message: None)

        assert "[security] timeout_seconds = 'many'" in str(caught.value)

    def test_two_wrong_inputs_name_the_section_without_guessing_a_key(
        self,
        tmp_path: Path,
    ) -> None:
        """Attribution is by measurement, so it stays silent when the
        measurement is ambiguous - two keys holding the same bad value
        cannot be told apart, and neither is named."""
        (tmp_path / "kstrl.toml").write_text(
            '[knowledge]\nmax_core_tokens = "many"\nmax_sibling_tokens = "many"\n'
        )

        with pytest.raises(ConfigError) as caught:
            preflight_config(tmp_path, warn=lambda _message: None)

        message = str(caught.value)
        assert "[knowledge]" in message
        assert "max_core_tokens" not in message
        assert "max_sibling_tokens" not in message

    def test_two_variables_that_each_fix_it_blame_neither(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Removing either variable satisfies the loader, so neither is
        "the one to change". Naming the alphabetically first one made
        the tool blame KSTRL_LINEAR_ENABLED while the message beside it
        told the operator to set KSTRL_LINEAR_TEAM_ID."""
        (tmp_path / "kstrl.toml").write_text('[linear]\nteam_id = "abc"\n')
        monkeypatch.setenv("KSTRL_LINEAR_ENABLED", "1")
        monkeypatch.setenv("KSTRL_LINEAR_TEAM_ID", "")

        with pytest.raises(ConfigError) as caught:
            preflight_config(tmp_path, warn=lambda _message: None)

        message = str(caught.value)
        assert "[linear]" in message
        assert "set by KSTRL_LINEAR_ENABLED" not in message
        assert "set by KSTRL_LINEAR_TEAM_ID" not in message

    def test_one_variable_that_fixes_it_is_still_named(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The uniqueness rule must not cost the attribution it guards."""
        monkeypatch.setenv("KSTRL_SECURITY_TIMEOUT", "many")

        with pytest.raises(ConfigError) as caught:
            preflight_config(tmp_path, warn=lambda _message: None)

        assert "set by KSTRL_SECURITY_TIMEOUT=many" in str(caught.value)

    def test_a_rejected_ceiling_does_not_hide_a_later_section(
        self,
        tmp_path: Path,
    ) -> None:
        """`[factory]` is second in the traversal, so re-raising its
        BudgetConfigError abandoned every section after it: the operator
        fixed the ceiling, re-ran, and met `[verify]` for the first
        time."""
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\nmax_cost_usd = nan\n\n[verify]\nmutation_threshold = 'many'\n"
        )

        with pytest.raises(ConfigError) as caught:
            preflight_config(tmp_path, warn=lambda _message: None)

        message = str(caught.value)
        assert "max_cost_usd" in message
        assert "[verify]" in message

    def test_a_clean_config_raises_nothing(self, tmp_path: Path) -> None:
        """The example config ships as documentation; it has to pass."""
        example = REPO_ROOT / "kstrl.toml.example"
        (tmp_path / "kstrl.toml").write_text(example.read_text())

        warnings: list[str] = []
        preflight_config(tmp_path, warn=warnings.append)

        assert warnings == []


class TestTheRootIsTheOneTheCommandWillUse:
    """Why the check sits on the COMMAND and not on the group: at group
    level click has not parsed ``--root`` yet, so a preflight there would
    read the config of whatever directory the operator happened to be
    standing in."""

    @staticmethod
    def _other_checkout(tmp_path: Path, toml: str | bytes) -> Path:
        """A second project, with a prompt file at the layout
        ``_resolve_root`` recognises.

        Takes ``bytes`` as well as ``str``, and writes bytes either way,
        for the reason ``_invoke`` does.
        """
        other = tmp_path / "other"
        (other / "scripts" / "kstrl").mkdir(parents=True)
        (other / "kstrl.toml").write_bytes(toml.encode() if isinstance(toml, str) else toml)
        (other / "scripts" / "kstrl" / "prompt.md").write_text("# prompt\n")
        return other

    def test_a_stale_prompt_file_does_not_redirect_a_command_that_ignores_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only `run`, `understand` and `feature` derive a root from a
        prompt path; `status` and the rest use ``root or cwd`` and never
        read PROMPT_FILE. Reading it for them made one stale export in a
        shell profile refuse `ks status` on an unrelated checkout's
        broken file."""
        other = self._other_checkout(tmp_path, MALFORMED_TOML)
        monkeypatch.setenv("PROMPT_FILE", str(other / "scripts" / "kstrl" / "prompt.md"))

        result = _invoke(["status"], toml="[factory]\nmax_parallel = 2\n")

        assert "Invalid TOML" not in result.output
        assert "configuration rejected" not in result.output

    def test_a_stale_prompt_file_does_not_hide_the_commands_own_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same bug's other half, and the dangerous one: the
        redirect made the check validate the OTHER checkout, so a
        command whose own config was broken PASSED. A preflight that
        passes is invisible."""
        other = self._other_checkout(tmp_path, "[factory]\nmax_parallel = 2\n")
        monkeypatch.setenv("PROMPT_FILE", str(other / "scripts" / "kstrl" / "prompt.md"))

        result = _invoke(["status"], toml='[verify]\nmutation_threshold = "many"\n')

        assert result.exit_code == 2
        assert "[verify] could not convert string to float: 'many'" in result.output

    def test_a_command_that_declares_prompt_still_reads_the_env_var(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fix narrows the inputs to what a command declares; it
        does not drop env support for the three commands that resolve
        their root that way."""
        other = self._other_checkout(tmp_path, MALFORMED_TOML)
        monkeypatch.setenv("PROMPT_FILE", str(other / "scripts" / "kstrl" / "prompt.md"))

        result = _invoke(["run", "--agent-cmd", "true"])

        assert result.exit_code == 2
        assert "Invalid TOML" in result.output

    def test_the_prompt_option_feature_actually_uses_derives_the_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`ks feature` names its prompt option ``--understand-prompt``
        and feeds THAT to ``_resolve_root``. Reading only ``prompt``
        made the check validate the cwd while the command loaded another
        checkout's config, and the failure mode was the worst kind: a
        preflight that PASSES, which no other test here can catch.
        """
        project = tmp_path / "project"
        (project / "scripts" / "kstrl").mkdir(parents=True)
        # A section the command body does NOT load early. Malformed TOML
        # would not distinguish anything: `KstrlConfig.load` is the
        # command's second statement, so the operator gets a clean error
        # either way. [linear] is the section #272 was filed about
        # precisely because nothing reads it until after the agent.
        (project / "kstrl.toml").write_text('[linear]\ntimeout_seconds = "soon"\n')
        prompt = project / "scripts" / "kstrl" / "understand_prompt.md"
        prompt.write_text("# understand\n")
        built = _no_agents(monkeypatch)

        result = _invoke(["feature", "--understand-prompt", str(prompt), "--agent-cmd", "true"])

        assert built == []
        assert result.exit_code == 2
        assert "[linear]" in result.output

    def test_a_broken_config_under_root_is_found_from_a_clean_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        elsewhere = tmp_path / "project"
        elsewhere.mkdir()
        (elsewhere / "kstrl.toml").write_bytes(MALFORMED_TOML)
        built = _no_agents(monkeypatch)

        result = _invoke([*DECOMPOSE_ARGS, "--root", str(elsewhere)])

        assert built == []
        assert result.exit_code == 2
        assert "Invalid TOML" in result.output

    def test_a_broken_config_in_the_cwd_does_not_fail_another_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other half of the same property: the file that is NOT the
        command's config must not stop it."""
        clean = tmp_path / "project"
        clean.mkdir()
        _stub_run_factory(monkeypatch)

        # The cwd is the SUBJECT here, so this test sets its own rather
        # than reusing the autouse one: the broken file has to sit in a
        # directory that is not `clean` and not an ancestor of it, which
        # is what the two siblings under tmp_path give.
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
        (cwd / "kstrl.toml").write_bytes(MALFORMED_TOML)
        monkeypatch.chdir(cwd)
        make_manifest([component("comp-a")]).save(clean / "m.json")
        result = CliRunner().invoke(
            cli,
            [
                "factory",
                "--manifest",
                str(clean / "m.json"),
                "--root",
                str(clean),
                "--agent-cmd",
                "true",
                "--yes",
            ],
            catch_exceptions=True,
        )

        assert result.exit_code == 0, result.output


#: Every command the entry seam guards, with the exit code each one
#: documents for a rejected configuration. That set is exactly the
#: commands NOT exempt from the seam, which is the point: the blast
#: radius of an escaping ``ValueError`` is the seam itself, not any
#: command's own config handling, so every one of these was measured
#: crashing in BOTH rounds of #318.
#: ``test_the_table_names_every_command_the_seam_guards`` keeps the list
#: honest rather than a count in a comment doing it.
SEAM_COMMANDS: list[tuple[list[str], int]] = [
    (["autonomy", "status"], 2),
    (["ci", "poll"], 2),
    (["dash"], 2),
    (DECOMPOSE_ARGS, 2),
    (["evolve"], 2),
    (FACTORY_ARGS, 2),
    (["feature", "--prd", "s.md", "--agent-cmd", "true"], 2),
    (["health"], 2),
    (["inbox", "ls"], 2),
    (["learn", "playbook"], 2),
    (["queue", "ls"], 2),
    (["recheck", "record.json"], 2),
    (["retry", "comp-a"], 2),
    (["run", "--agent-cmd", "true"], 2),
    (["serve", "--print-plist", "--no-color"], 2),
    (["signals", "poll"], 2),
    (["status"], 2),
    (["understand", "--agent-cmd", "true"], 2),
]


class TestAConfigThatWillNotParseIsReportedNotCrashed:
    """#318, both rounds, across every command the seam guards.

    ``tomllib.load`` raises ``ValueError`` for a family of bad input,
    and two members of that family escaped ``load_toml_document`` in
    turn: ``UnicodeDecodeError`` past a handler naming only
    ``TOMLDecodeError`` (round 1), then a plain ``ValueError`` from the
    integer-digit limit past both (round 2). Same 13 commands, same raw
    traceback, twice. So the matrix runs every command against every
    fault rather than against the one that was current when it was
    written - which is the property that would have caught round 2
    before it shipped.
    """

    @pytest.mark.parametrize(("toml", "fragment"), TOML_PARSE_FAULTS)
    @pytest.mark.parametrize(
        ("args", "exit_code"),
        SEAM_COMMANDS,
        ids=[args[0] for args, _ in SEAM_COMMANDS],
    )
    def test_the_command_reports_the_file_instead_of_a_traceback(
        self,
        args: list[str],
        exit_code: int,
        toml: bytes,
        fragment: str,
    ) -> None:
        result = _invoke(args, toml=toml)

        # The regression itself, asserted as the escaped exception and
        # not only as an exit code: every one of these ended with a raw
        # ValueError out of `tomllib.load`, and an exit code of 1 is
        # what click reports for that too. `ConfigError` IS a
        # ValueError, so this only passes because the group handler
        # turned it into an exit - which is exactly the claim.
        assert not isinstance(result.exception, ValueError), result.exception
        assert result.exit_code == exit_code, result.output
        assert "error:" in result.output
        assert fragment in result.output
        # Which file. The parser's own message names no path, and an
        # operator may have more than one checkout.
        assert "kstrl.toml" in result.output

    @pytest.mark.parametrize(("toml", "fragment"), TOML_PARSE_FAULTS)
    def test_no_agent_is_constructed_on_the_paid_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        toml: bytes,
        fragment: str,
    ) -> None:
        """The seam's whole promise, restated for each fault: `decompose`
        stops before the architect rather than after it."""
        built = _no_agents(monkeypatch)

        result = _invoke(DECOMPOSE_ARGS, toml=toml)

        assert built == []
        assert result.exit_code == 2
        assert fragment in result.output

    @pytest.mark.parametrize(
        ("args", "exit_code"),
        SEAM_COMMANDS,
        ids=[args[0] for args, _ in SEAM_COMMANDS],
    )
    def test_a_file_that_cannot_be_opened_is_reported_as_unreadable_not_as_a_parse_fault(
        self,
        args: list[str],
        exit_code: int,
    ) -> None:
        """Rule 3 of the tomllib reader, driven through every command: the
        open happens OUTSIDE the parse guard, so an ``OSError`` (here a
        kstrl.toml that is a directory) is reported as "could not be read"
        and never relabelled as a TOML parse failure. This is the
        end-to-end carrier for the folded loader unit tests
        ``test_open_failures_are_not_relabelled_as_parse_failures`` and
        ``test_an_unreadable_file_still_raises_oserror_not_configerror``.
        """
        root = Path.cwd()
        (root / "s.md").write_text("# spec\n")
        make_manifest([component("comp-a")]).save(root / "m.json")
        (root / "kstrl.toml").mkdir()
        result = CliRunner().invoke(cli, args, catch_exceptions=True)

        assert not isinstance(result.exception, (ValueError, OSError)), result.exception
        assert result.exit_code == exit_code, result.output
        assert "error:" in result.output
        assert "kstrl.toml could not be read" in result.output, result.output
        assert "Is a directory" in result.output, result.output
        assert "parse" not in result.output.lower(), result.output

    def test_the_table_names_every_command_the_seam_guards(self) -> None:
        """The drift guard, in the shape ``TestEverySectionIsEnrolled``
        and ``TestTheSeamCannotBeBypassedByDeclaration`` already use: a
        command added later is covered, or this fails. One shared seam
        serves all of them, so the table is the record of what was
        measured broken rather than of that many independent paths - but
        it is checked against the live registry, not against a number
        somebody wrote in a comment.

        The four exempt commands are covered by
        ``TestTheCommandsThatMustSurviveABrokenConfig``, which runs its
        cases over ``TOML_PARSE_FAULTS``.
        """
        covered = {args[0] for args, _ in SEAM_COMMANDS}

        assert covered == set(cli.commands) - cli_mod._PREFLIGHT_EXEMPT


class TestTheCommandsThatMustSurviveABrokenConfig:
    """Four exemptions, each of which would otherwise take away the tool
    the operator recovers with, or replace a machine contract with a
    weaker one.

    The four parametrized cases run against EVERY shape a kstrl.toml
    can fail to parse in (``TOML_PARSE_FAULTS``), because an exemption
    that survives one fault and not another is not an exemption. #318 was
    exactly that, twice: round 1, `config show` printed the codec
    message with no path in it while the syntax case named the file;
    round 2, a third fault escaped everything the first round added.
    """

    @pytest.mark.parametrize(("toml", "fragment"), TOML_PARSE_FAULTS)
    def test_config_show_still_explains_the_file_it_cannot_load(
        self,
        toml: bytes,
        fragment: str,
    ) -> None:
        result = _invoke(["config", "show"], toml=toml)

        assert result.exit_code == 1
        assert fragment in result.output
        # Which file, not just what is wrong with it: an operator with
        # more than one checkout cannot act on the codec message alone.
        assert "kstrl.toml" in result.output

    @pytest.mark.parametrize(("toml", "fragment"), TOML_PARSE_FAULTS)
    def test_init_still_scaffolds_next_to_a_broken_file(
        self,
        toml: bytes,
        fragment: str,
    ) -> None:
        result = _invoke(["init", "--ui", "plain", "--no-color"], toml=toml)

        # Whatever init decides about an existing project, it is not
        # allowed to be "cannot parse the file I am here to write".
        assert result.exit_code == 0, result.output
        assert fragment not in result.output
        assert "Created prompt.md" in result.output

    @pytest.mark.parametrize(("toml", "fragment"), TOML_PARSE_FAULTS)
    def test_check_keeps_its_exit_2_and_its_json_envelope(
        self,
        toml: bytes,
        fragment: str,
    ) -> None:
        result = _invoke(["check", "--json"], toml=toml)

        assert result.exit_code == 2
        assert fragment in json.loads(result.stdout)["error"]

    @pytest.mark.parametrize(("toml", "fragment"), TOML_PARSE_FAULTS)
    def test_doctor_reports_a_broken_config_instead_of_refusing_to_run(
        self,
        toml: bytes,
        fragment: str,
    ) -> None:
        """The doctor is the surface an operator diagnoses WITH. Under the
        seam it would print one refusal and none of its nine checks, for
        the config it exists to report on."""
        result = _invoke(["doctor"], toml=toml)

        assert result.exit_code == 1
        assert "[fail] kstrl_config" in result.output
        assert "[ok] git_repo" not in result.output  # the tmp_path cwd is not a repo
        assert fragment in result.output

    def test_check_checks_sections_it_does_not_itself_read(self) -> None:
        """`check` loads four sections of its own. An exemption that
        checked only those would keep the "depends which section you
        typo'd" property inside itself, so it runs the whole preflight
        under its own contract."""
        result = _invoke(["check", "--json"], toml='[linear]\ntimeout_seconds = "soon"\n')

        assert result.exit_code == 2
        assert "[linear]" in json.loads(result.stdout)["error"]

    def test_config_show_reports_a_section_its_own_rows_do_not_cover(self) -> None:
        """`config_report` renders 15 of the 26 sections. Without this,
        the tool the seam exempts so an operator can DIAGNOSE a refusal
        would print rows and exit 0 for the very config that refuses
        every other command."""
        result = _invoke(["config", "show"], toml='[queue]\nmax_attempts = "many"\n')

        assert result.exit_code == 1
        assert "[queue]" in result.output
        # It still renders what it can before saying so.
        assert "[agent]" in result.output

    def test_help_still_renders_for_a_command_that_is_not_exempt(self) -> None:
        """Reading the help is part of fixing the file. click handles
        ``--help`` while parsing, before ``Command.invoke``, so this is a
        property of where the check sits rather than of the exemptions."""
        result = _invoke(["factory", "--help"], toml=MALFORMED_TOML)

        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_serve_print_plist_keeps_the_documented_exit_2(self) -> None:
        """`--print-plist` returns before the config load, so it used to
        skip the check and then exit 1 through the group's ConfigError
        handler - contradicting this command's own documented exit 2 for
        a bad kstrl.toml, on the one path an operator uses while setting
        up an unattended daemon."""
        result = _invoke(["serve", "--print-plist", "--no-color"], toml=MALFORMED_TOML)

        assert result.exit_code == 2
        assert "Invalid TOML" in result.output

    def test_serve_checks_the_whole_config_under_its_own_exit_2(self) -> None:
        """Exempt from the seam, not from the guarantee: the daemon calls
        the preflight itself, so a section it never reads still stops it
        before it spawns children that would each be classified as
        poison for the same reason."""
        result = _invoke(
            ["serve", "--once", "--no-color"],
            toml='[verify]\nmutation_threshold = "many"\n',
        )

        assert result.exit_code == 2
        assert "[verify]" in result.output


class TestTheHomeShellIsNotAFifthExemption:
    """Bare `ks` on a TTY runs the GROUP callback, so
    ``_KstrlCommand.invoke`` never fires for it. That made the home shell
    an undocumented fifth exemption, and the most expensive one: the TUI
    launches runs IN-PROCESS (``tui/session.py`` calls ``run_factory``
    and ``decompose_spec`` directly), so a bad ``[linear]`` value paid
    for the architect and then aborted. The original #272 defect, on the
    path a user reaches by typing `ks`.

    ``ks doctor`` (#198) made the documented exempt list five, so the
    shell would now be a sixth; the name is kept because it records
    the #272 defect rather than a live count.
    """

    @staticmethod
    def _bare_ks(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        toml: str,
    ) -> tuple[int, list[Path]]:
        import kstrl.tui.home as home_mod

        (tmp_path / "kstrl.toml").write_text(toml)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KSTRL_NO_TUI", raising=False)
        opened: list[Path] = []

        def _fake_home_shell(root: Path) -> int:
            opened.append(root)
            return 0

        monkeypatch.setattr(home_mod, "run_home_shell", _fake_home_shell)
        # The live streams, so the refusal is still visible to capsys.
        # Replacing them wholesale would swallow the message this branch
        # exists to print.
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

        try:
            cli.main([], standalone_mode=False)
        except SystemExit as exc:
            return int(exc.code or 0), opened
        return 0, opened

    def test_a_rejected_section_stops_the_shell_before_it_opens(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code, opened = self._bare_ks(
            tmp_path,
            monkeypatch,
            '[linear]\ntimeout_seconds = "soon"\n',
        )

        assert opened == []
        assert code == 2
        # And the operator is told which section, not just refused.
        assert "[linear]" in capsys.readouterr().err

    def test_a_usable_config_still_opens_the_shell(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guard must not cost the entry point it protects."""
        code, opened = self._bare_ks(tmp_path, monkeypatch, "[factory]\nmax_parallel = 2\n")

        assert opened == [Path.cwd()]
        assert code == 0


class TestConfigShowIsTheSurfaceThatAlwaysWorks:
    """Every command refuses on an unusable section, so one command has
    to always run and always explain. Before this it was the LEAST
    informative surface in the CLI: ``build_config_report`` raised before
    a single row printed, and `ks config show` said
    ``error: could not convert string to float: 'many'`` while every
    other command named the section, the key and the value.
    """

    def test_a_rejected_rendered_section_costs_its_rows_not_the_report(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`[verify]` is one of the 15 sections this report RENDERS, so
        it is the case the earlier covering test missed by using
        `[queue]`, which is one of the 11 it does not."""
        result = _invoke(["config", "show"], toml='[verify]\nmutation_threshold = "many"\n')

        assert result.exit_code == 1
        # Rows first, for everything that resolved.
        assert "[agent]" in result.output
        assert "  type = " in result.output
        # Then the verdict, in the words every other command uses.
        assert "[verify] could not convert string to float: 'many'" in result.output
        assert "mutation_threshold = 'many'" in result.output

    def test_it_still_explains_when_every_other_command_refuses(self) -> None:
        """The escape hatch is a command, not a flag: universal fatality
        is only defensible while one surface always answers."""
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("KSTRL_LINEAR_ENABLED", "1")
            refused = _invoke(["status"])
            explained = _invoke(["config", "show"])

        assert refused.exit_code == 2
        assert explained.exit_code == 1
        assert "[agent]" in explained.output
        assert "[linear]" in explained.output
        assert "KSTRL_LINEAR_TEAM_ID" in explained.output

    def test_a_base_section_failure_is_reported_in_the_seam_s_words(self) -> None:
        """No rows are possible when the base config is rejected, but the
        message still names the section, the key and the value rather
        than the bare coercion error."""
        result = _invoke(["config", "show"], toml='[run]\nmax_iterations = "many"\n')

        assert result.exit_code == 1
        assert "[run] max_iterations = 'many'" in result.output

    def test_a_clean_config_still_exits_zero(self) -> None:
        result = _invoke(["config", "show"], toml="[factory]\nmax_parallel = 2\n")

        assert result.exit_code == 0, result.output
        assert "Rejected sections" not in result.output


class TestTheSeamCannotBeBypassedByDeclaration:
    """Two drift guards, both for the same failure: a command that gets
    no check, or gets one against the wrong root, and says nothing.

    Mirrors ``tests/test_prompt_versions.py``, which fails on a prompt
    constant that was added without being enrolled.
    """

    @staticmethod
    def _walk(group: click.Group, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
        found: list[tuple[tuple[str, ...], Any]] = []
        for name, command in group.commands.items():
            here = (*path, name)
            if isinstance(command, click.Group):
                found.append((here, command))
                found.extend(TestTheSeamCannotBeBypassedByDeclaration._walk(command, here))
            else:
                found.append((here, command))
        return found

    def test_every_command_goes_through_the_seam(self) -> None:
        """A command that is not a ``_KstrlCommand`` never reaches the
        check, which is how the home shell became a fifth exemption."""
        leaves = {
            path: type(command).__name__
            for path, command in self._walk(cli)
            if not isinstance(command, click.Group)
        }

        assert {p: n for p, n in leaves.items() if n != "_KstrlCommand"} == {}

    def test_only_the_root_group_runs_without_a_subcommand(self) -> None:
        """``invoke_without_command`` means the GROUP callback does work,
        and a group callback is not a ``_KstrlCommand``. The root one is
        the home shell, which preflights explicitly; a second such group
        would silently reintroduce #272."""
        groups = [path for path, command in self._walk(cli) if isinstance(command, click.Group)]

        assert [p for p in groups if cli.commands[p[0]].invoke_without_command] == []
        assert cli.invoke_without_command is True

    def test_a_command_declaring_a_root_option_records_a_decision(self) -> None:
        """`_preflight_root` reads --prompt / --prd / --understand-prompt
        only for the commands that DERIVE their root from them. A command
        that declares one without being listed either way would be
        checked against a root it does not use, and pass."""
        declaring = {
            path[0]
            for path, command in self._walk(cli)
            if not isinstance(command, click.Group)
            and {p.name for p in command.params} & {"prompt", "prd", "understand_prompt"}
        }

        # `ks config show` declares --prompt and --prd as [paths]
        # OVERRIDES and still roots itself at the cwd, which is why the
        # rule is keyed by command rather than by "declares the option".
        assert declaring - cli_mod._ROOT_FROM_PROMPT == {"config"}


class TestTheExemptionKeysOffTheTopLevelName:
    """Both seam tables are keyed by the command directly under the root
    group. Keyed by any name in the chain instead, a later ``ks queue
    init`` or ``ks inbox serve`` would be exempted purely because of its
    leaf name, which is not a decision anybody would have made.
    """

    @staticmethod
    def _name_for(args: list[str]) -> str:
        seen: list[str] = []

        def record(self: Any, ctx: click.Context) -> Any:
            seen.append(cli_mod._KstrlCommand._top_level_name(ctx))
            return None

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(cli_mod._KstrlCommand, "invoke", record)
            CliRunner().invoke(cli, args, catch_exceptions=True)
        return seen[0]

    def test_a_top_level_command_names_itself(self) -> None:
        assert self._name_for(["status"]) == "status"

    def test_a_subcommand_names_its_group(self) -> None:
        assert self._name_for(["config", "show"]) == "config"
        assert self._name_for(["queue", "ls"]) == "queue"


class TestTheParseScope:
    """The check resolves 22 sections, and each loader reparses the file.
    ``toml_parse_scope`` makes that one parse. The property that matters
    is how far the reuse reaches: inside the block, not beyond it.
    """

    def test_a_document_is_parsed_once_inside_the_scope(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "kstrl.toml"
        toml_path.write_text("[factory]\nmax_parallel = 2\n")

        with toml_parse_scope():
            first = load_toml_document(toml_path)
            toml_path.write_text("[factory]\nmax_parallel = 9\n")
            assert load_toml_document(toml_path) is first

    def test_the_scope_does_not_outlive_its_block(self, tmp_path: Path) -> None:
        """The half that keeps this honest. A process-wide snapshot
        would freeze the file for surfaces built to re-read it: the TUI
        config screen's refresh action, and `ks serve` re-reading per
        queue item."""
        toml_path = tmp_path / "kstrl.toml"
        toml_path.write_text("[factory]\nmax_parallel = 2\n")

        with toml_parse_scope():
            load_toml_document(toml_path)
        toml_path.write_text("[factory]\nmax_parallel = 9\n")

        assert load_toml_document(toml_path)["factory"]["max_parallel"] == 9


class TestEverySectionIsEnrolled:
    """The registry is only a guarantee while it is complete.

    Mirrors ``tests/test_prompt_versions.py``, which AST-walks for
    ``*_PROMPT`` constants and fails on one not enrolled. Two
    inventories, and every ``*Config`` class in ``kstrl/`` is in exactly
    one: it has a loader this walk can read and must be registered, or it
    has not and must be pinned with the reason. Round 1 had one
    inventory and no control: its walk swallowed ``SyntaxError`` and its
    assertion was a difference against zero, so an unparseable module
    made it PASS.
    """

    def test_a_config_class_with_no_readable_loader_is_pinned(self) -> None:
        """The answer to a loader supplied from outside the class body:
        it fails HERE rather than reading below as "no loader"."""
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=_no_readable_loader,
            expected=EXPECTED_LOADERLESS_CONFIGS,
            control="class ZzzConfig:\n    pass\n",
            message=(
                "A *Config class in kstrl/ has no loader this walk can read. If a "
                "decorator or a base supplies one, register the section in "
                "config_sections() (#272) and pin the row with that reason."
            ),
        )

    def test_no_config_dataclass_is_missing_from_the_registry(self) -> None:
        """Equality, not a difference against zero: an empty left side is
        what a walk that stopped matching returns."""
        found = {
            name
            for source_file in astwalk.package_sources()
            for name in _classes_with_a_loader(astwalk.parsed(source_file))
        }
        registered = {
            getattr(section.loader, "__self__", type(None)).__name__
            for section in config_sections()
        }

        assert found == registered, f"config_sections() misses {found - registered} (#272)"

    @pytest.mark.parametrize(
        "body", ["def load(cls, p): ...", "async def load(cls, p): ...", "load = _impl"]
    )
    def test_a_loader_in_any_of_its_three_shapes_is_found(self, body: str) -> None:
        """The control round 1 had none of: all 22 live loaders are a
        plain ``def``, so the equality above cannot be it."""
        assert _classes_with_a_loader(astwalk.parse(f"class ZzzConfig:\n    {body}\n")) == {
            "ZzzConfig"
        }

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_config_class_the_interpreter_builds_is_a_known_miss(self) -> None:
        """The one residual both inventories share."""
        astwalk.blind_spot(
            lambda src: [n for n in ast.walk(astwalk.parse(src)) if _config_class(n)],
            'ZzzConfig = type("ZzzConfig", (), {"load": _impl})\n',
        )

    def test_the_registry_names_a_real_toml_section_for_each_loader(self) -> None:
        """A section name is what the error line points the operator at,
        so a typo in the registry would name a table that does not
        exist. Every name here appears in the shipped example."""
        example = (REPO_ROOT / "kstrl.toml.example").read_text()
        names = {name for section in config_sections() for name in section.sections}

        assert {name for name in names if f"[{name}]" not in example} == set()


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _clear_env(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)


#: #562: a STRING_KEYS row whose field takes a closed vocabulary is refused
#: at load for any other value, so the precedence test writes real ones.
_VALID_STRING_VALUES: dict[str, tuple[str, str]] = {"agent_type": ("claude", "codex")}


class TestConfigToml:
    """Folded from tests/test_config_toml.py (#593 slice 4).

    Every test kept here writes a real kstrl.toml on ``tmp_path`` and
    drives ``KstrlConfig.load(root)`` or
    ``config_preflight.collect_config_problems`` - the entry points the
    rest of the factory actually calls. Dropped as unit (a class or
    function works, not the system end to end): the ``from_toml`` /
    ``from_env`` section-mapping tests, three pure introspection checks
    over ``STRING_KEYS`` with no file on disk at all, and six tests that
    called ``load_toml_document`` (a parser) directly - #318's ordering
    and message-wording pins, superseded by ``TestAConfigThatWillNotParseIsReportedNotCrashed``
    above (the same ``TOML_PARSE_FAULTS`` table exercised through every
    real CLI command) and by the structural handler-shape guard in
    tests/test_toml_readers.py.
    """

    def test_from_toml_leaves_unknown_names_to_the_entry_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loader reads the names it knows and passes over the rest;
        the entry check is what names the rest (#525). Before #525 this
        test pinned the silence itself."""
        for name in [k for k in os.environ if k.startswith("KSTRL_")]:
            monkeypatch.delenv(name)
        toml_path = tmp_path / "kstrl.toml"
        _write_toml(
            toml_path,
            """
[agent]
type = "claude"
unknown_field = "ignored"

[unknown_section]
foo = "bar"
""",
        )
        config = KstrlConfig.from_toml(toml_path, tmp_path)
        assert config.agent_type == "claude"
        problems = collect_config_problems(tmp_path, lambda _message: None)
        assert len(problems) == 2, problems
        assert "names [agent] unknown_field, which no kstrl setting reads" in problems[0]
        assert "names [unknown_section], which no kstrl setting reads" in problems[1]

    def test_load_env_overrides_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        toml_path = tmp_path / "kstrl.toml"
        _write_toml(
            toml_path,
            """
[run]
max_iterations = 25

[agent]
model = "sonnet"
""",
        )
        monkeypatch.setenv("MAX_ITERATIONS", "99")
        monkeypatch.setenv("MODEL", "opus")
        config = KstrlConfig.load(tmp_path)
        assert config.max_iterations == 99
        assert config.model == "opus"

    @pytest.mark.parametrize(
        ("section", "toml_key", "env_var", "field_name", "is_path"),
        STRING_KEYS,
        ids=[f"{section}.{key}" for section, key, _e, _f, _p in STRING_KEYS],
    )
    def test_every_string_key_follows_the_same_precedence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        section: str,
        toml_key: str,
        env_var: str,
        field_name: str,
        is_path: bool,
    ) -> None:
        """One rule for every row: env beats kstrl.toml beats the field default,
        an empty toml value means unset rather than an error, and a path row's
        default is anchored against the root by all three entry points.
        Parametrized over the table rather than written against one key
        (R10.8's), so a row added later is covered the day it is added."""
        monkeypatch.delenv(env_var, raising=False)
        default = getattr(KstrlConfig(), field_name)
        unset = tmp_path / default if is_path and default is not None else default
        toml_path = tmp_path / "kstrl.toml"

        _write_toml(toml_path, f'\n[{section}]\n{toml_key} = ""\n')
        assert getattr(KstrlConfig.load(tmp_path), field_name) == unset
        assert getattr(KstrlConfig.from_env(tmp_path), field_name) == unset
        assert getattr(KstrlConfig.from_toml(toml_path, tmp_path), field_name) == unset

        toml_value, env_value = _VALID_STRING_VALUES.get(field_name, ("from-toml", "from-env"))
        _write_toml(toml_path, f'\n[{section}]\n{toml_key} = "{toml_value}"\n')
        from_toml = tmp_path / toml_value if is_path else toml_value
        assert getattr(KstrlConfig.load(tmp_path), field_name) == from_toml

        monkeypatch.setenv(env_var, env_value)
        from_env = tmp_path / env_value if is_path else env_value
        assert getattr(KstrlConfig.load(tmp_path), field_name) == from_env

    @pytest.mark.parametrize(
        ("env_var", "field_name"),
        [(e, f) for _s, _k, e, f, is_path in STRING_KEYS if not is_path],
        ids=[f"{s}.{k}" for s, k, _e, _f, is_path in STRING_KEYS if not is_path],
    )
    def test_an_empty_env_var_is_an_explicit_empty_value(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        env_var: str,
        field_name: str,
    ) -> None:
        """``AGENT_CMD=""`` means "no command", not "unset" (review round 1,
        nit 11).

        The env overlay tests membership (``os.environ.get(...) is not
        None``), and the TOML overlay tests truthiness, and the asymmetry is
        deliberate: an exported empty string is something somebody typed,
        while ``command = ""`` in the shipped kstrl.toml example is a
        placeholder nobody filled in. Measured: switching the env overlay to
        truthiness left the whole of this file green, so the rule the PR body
        claims to preserve had no test at all. Path rows are excluded because
        ``_resolve_path("", root)`` is the root directory, which is a
        different question from this one.
        """
        monkeypatch.setenv(env_var, "")
        assert getattr(KstrlConfig.load(tmp_path), field_name) == ""
        assert getattr(KstrlConfig.from_env(tmp_path), field_name) == ""

    @pytest.mark.parametrize(
        ("env_var", "field_name"),
        [(e, f) for _s, _k, e, f, is_path in STRING_KEYS if is_path],
        ids=[f"{s}.{k}" for s, k, _e, _f, is_path in STRING_KEYS if is_path],
    )
    def test_an_empty_path_env_var_resolves_to_the_repo_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        env_var: str,
        field_name: str,
    ) -> None:
        """The half the case above excludes, characterised rather than fixed.

        ``_resolve_path("", root)`` is ``root / Path("")``, which is ``root``,
        so an exported empty path key names the repository directory. Every
        reader that then opens it gets EISDIR: measured on the memory row,
        ``Memory: could not read <root>: [Errno 21] Is a directory`` on every
        run of the project.

        This is NOT an endorsement. It is #229 round 2's nit 14 and R10.9
        round 1's nit 4, consciously preserved twice, and it was recorded
        both times in a review report and held by nothing that runs. Pinned
        over EVERY path row rather than over the one the review happened to
        export, so the count is closed by construction and is not written
        down: the parametrization IS the set, every row in it behaves
        identically, and that is what makes this inherited behaviour rather
        than something the memory row introduced. Round 2 (nit 3) found the
        sentence saying "all four" over six rows, on a set the same PR had
        grown. Whoever decides to change it changes this case deliberately
        and sees every other row it holds.
        """
        monkeypatch.setenv(env_var, "")

        assert getattr(KstrlConfig.load(tmp_path), field_name) == tmp_path
        assert getattr(KstrlConfig.from_env(tmp_path), field_name) == tmp_path

    def test_an_empty_toml_value_stays_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of nit 11's asymmetry, so neither side can drift
        into the other without a named failure."""
        for _section, _key, env_var, _field, _is_path in STRING_KEYS:
            monkeypatch.delenv(env_var, raising=False)
        toml_path = tmp_path / "kstrl.toml"
        _write_toml(toml_path, '\n[agent]\ncommand = ""\n')
        assert KstrlConfig.load(tmp_path).agent_cmd is None

    def test_load_toml_wins_over_defaults_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Clear env vars that might leak from the test environment
        for var in ("MAX_ITERATIONS", "MODEL", "SLEEP_SECONDS", "INTERACTIVE"):
            monkeypatch.delenv(var, raising=False)
        toml_path = tmp_path / "kstrl.toml"
        _write_toml(
            toml_path,
            """
[run]
max_iterations = 25
""",
        )
        config = KstrlConfig.load(tmp_path)
        assert config.max_iterations == 25

    def test_load_defaults_when_no_toml_and_no_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in (
            "MAX_ITERATIONS",
            "MODEL",
            "SLEEP_SECONDS",
            "INTERACTIVE",
            "ALLOWED_PATHS",
            "AGENT_CMD",
            "MODEL_REASONING_EFFORT",
            "KSTRL_AGENT_TYPE",
            "KSTRL_BRANCH",
            "KSTRL_ASCII",
        ):
            monkeypatch.delenv(var, raising=False)
        config = KstrlConfig.load(tmp_path)
        assert config.max_iterations == 10
        assert config.sleep_seconds == 2.0
        assert config.agent_type is None
        assert config.agent_cmd is None
        assert config.kstrl_branch is None
        assert config.kstrl_branch_explicit is False

    def test_load_auto_discovers_kstrl_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("MAX_ITERATIONS",):
            monkeypatch.delenv(var, raising=False)
        _write_toml(
            tmp_path / "kstrl.toml",
            """
[run]
max_iterations = 7
""",
        )
        config = KstrlConfig.load(tmp_path)
        assert config.max_iterations == 7

    def test_load_missing_toml_falls_back_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("MAX_ITERATIONS",):
            monkeypatch.delenv(var, raising=False)
        config = KstrlConfig.load(tmp_path)
        assert config.max_iterations == 10

    def test_load_env_branch_marks_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_BRANCH", "")
        config = KstrlConfig.load(tmp_path)
        assert config.kstrl_branch == ""
        assert config.kstrl_branch_explicit is True

    def test_load_env_paths_resolved_against_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPT_FILE", "custom/prompt.md")
        config = KstrlConfig.load(tmp_path)
        assert config.prompt_file == tmp_path / "custom/prompt.md"

    def test_load_toml_empty_branch_does_not_mark_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """kstrl.toml.example documents `branch = ""` as 'empty = use PRD
        branchName'. An empty TOML branch must therefore NOT mark explicit,
        so loop.determine_branch falls through to PRD lookup instead of
        skipping checkout. Env var KSTRL_BRANCH="" retains its historical
        explicit-skip meaning - that path is tested elsewhere."""
        for var in ("KSTRL_BRANCH",):
            monkeypatch.delenv(var, raising=False)
        _write_toml(
            tmp_path / "kstrl.toml",
            """
[git]
branch = ""
""",
        )
        config = KstrlConfig.load(tmp_path)
        assert config.kstrl_branch is None
        assert config.kstrl_branch_explicit is False

    def test_load_toml_nonempty_branch_marks_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("KSTRL_BRANCH",):
            monkeypatch.delenv(var, raising=False)
        _write_toml(
            tmp_path / "kstrl.toml",
            """
[git]
branch = "feature/foo"
""",
        )
        config = KstrlConfig.load(tmp_path)
        assert config.kstrl_branch == "feature/foo"
        assert config.kstrl_branch_explicit is True


class TestConfigTomlFull:
    """Folded from tests/test_config_toml_full.py (#593 slice 4).

    Every per-section ``Config.load(root)`` classmethod ([factory],
    [verify], [contract], [codebase_scan], [evolution], [security]),
    each driven against a real kstrl.toml on disk - the entry point
    every phase of the factory calls to read its own section. Dropped
    as unit: ``VerifyConfig.from_env()`` called with no file and no
    ``.load()`` in the path.
    """

    class TestFactoryConfigLoad:
        def test_reads_factory_section(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _clear_env(monkeypatch, "FACTORY_MAX_PARALLEL", "FACTORY_MAX_RETRIES")
            _write_toml(
                tmp_path / "kstrl.toml",
                """
[factory]
max_parallel = 8
max_retries = 5
review_mode = "advisory"
""",
            )
            config = FactoryConfig.load(tmp_path)
            assert config.max_parallel == 8
            assert config.max_retries == 5
            assert config.review_mode == "advisory"

        def test_env_overrides_toml(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _write_toml(tmp_path / "kstrl.toml", "[factory]\nmax_parallel = 8\n")
            monkeypatch.setenv("FACTORY_MAX_PARALLEL", "99")
            config = FactoryConfig.load(tmp_path)
            assert config.max_parallel == 99

    class TestVerifyConfigLoad:
        def test_reads_verify_section(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _clear_env(
                monkeypatch,
                "KSTRL_VERIFY_TEST_CMD",
                "KSTRL_VERIFY_TYPECHECK_CMD",
                "KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE",
            )
            _write_toml(
                tmp_path / "kstrl.toml",
                """
[verify]
test_command = "pytest -x"
typecheck_command = "mypy ."
require_self_critique = true
self_critique_min_bullets = 5
""",
            )
            config = VerifyConfig.load(tmp_path)
            assert config.test_command == "pytest -x"
            assert config.typecheck_command == "mypy ."
            assert config.require_self_critique is True
            assert config.self_critique_min_bullets == 5

        def test_tool_keys_default_to_auto(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            # #258: None is "run every parser for the gate and union the
            # failures", which is what makes a chained command work.
            _clear_env(monkeypatch, "KSTRL_VERIFY_TEST_TOOL", "KSTRL_VERIFY_LINT_TOOL")
            _write_toml(tmp_path / "kstrl.toml", '[verify]\ntest_command = "pytest"\n')
            config = VerifyConfig.load(tmp_path)
            assert (config.test_tool, config.typecheck_tool, config.lint_tool) == (
                None,
                None,
                None,
            )

        def test_reads_the_tool_keys(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _clear_env(
                monkeypatch,
                "KSTRL_VERIFY_TEST_TOOL",
                "KSTRL_VERIFY_TYPECHECK_TOOL",
                "KSTRL_VERIFY_LINT_TOOL",
            )
            _write_toml(
                tmp_path / "kstrl.toml",
                """
[verify]
test_tool = "vitest"
typecheck_tool = "tsc"
lint_tool = "eslint"
""",
            )
            config = VerifyConfig.load(tmp_path)
            assert (config.test_tool, config.typecheck_tool, config.lint_tool) == (
                "vitest",
                "tsc",
                "eslint",
            )

        def test_env_overrides_the_toml_tool(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _write_toml(tmp_path / "kstrl.toml", '[verify]\ntest_tool = "vitest"\n')
            monkeypatch.setenv("KSTRL_VERIFY_TEST_TOOL", "pytest")
            assert VerifyConfig.load(tmp_path).test_tool == "pytest"

        def test_an_unknown_tool_raises_rather_than_falling_back_to_auto(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            # Silently reverting to auto would be the failure the key exists
            # to prevent: the operator wrote it to stop kstrl guessing.
            _clear_env(monkeypatch, "KSTRL_VERIFY_TEST_TOOL")
            _write_toml(tmp_path / "kstrl.toml", '[verify]\ntest_tool = "jest"\n')
            with pytest.raises(ValueError, match="unknown tool 'jest'"):
                VerifyConfig.load(tmp_path)

    class TestContractConfigLoad:
        def test_reads_contract_section(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _clear_env(monkeypatch, "KSTRL_CONTRACT_MODE", "KSTRL_CONTRACT_TEST_CMD")
            _write_toml(
                tmp_path / "kstrl.toml",
                """
[contract]
mode = "final"
test_command = "pytest tests/"
""",
            )
            config = ContractConfig.load(tmp_path)
            assert config.mode == ContractMode.FINAL.value
            assert config.test_command == "pytest tests/"

        def test_invalid_mode_raises(self, tmp_path: Path) -> None:
            _write_toml(tmp_path / "kstrl.toml", '[contract]\nmode = "always"\n')
            with pytest.raises(ValueError, match="Invalid ContractConfig.mode"):
                ContractConfig.load(tmp_path)

    class TestCodebaseScanConfigLoad:
        def test_reads_codebase_scan_section(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _clear_env(
                monkeypatch,
                "KSTRL_CODEBASE_SCAN_ENABLED",
                "KSTRL_CODEBASE_SCAN_MAX_TOKENS",
            )
            _write_toml(
                tmp_path / "kstrl.toml",
                """
[codebase_scan]
enabled = false
module_map = false
max_context_tokens = 8000
""",
            )
            config = CodebaseScanConfig.load(tmp_path)
            assert config.enabled is False
            assert config.module_map is False
            assert config.max_context_tokens == 8000

        def test_env_overrides(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _write_toml(tmp_path / "kstrl.toml", "[codebase_scan]\nenabled = false\n")
            monkeypatch.setenv("KSTRL_CODEBASE_SCAN_ENABLED", "true")
            config = CodebaseScanConfig.load(tmp_path)
            assert config.enabled is True

    class TestEvolutionConfigLoad:
        def test_reads_evolution_section(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _clear_env(
                monkeypatch,
                "KSTRL_EVOLUTION_ENABLED",
                "KSTRL_EVOLUTION_JOURNAL_PATH",
                "KSTRL_EVOLUTION_LOOKBACK_RUNS",
            )
            _write_toml(
                tmp_path / "kstrl.toml",
                """
[evolution]
enabled = false
lookback_runs = 25
""",
            )
            config = EvolutionConfig.load(tmp_path)
            assert config.enabled is False
            assert config.lookback_runs == 25

        def test_resolves_journal_path(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _clear_env(monkeypatch, "KSTRL_EVOLUTION_JOURNAL_PATH")
            _write_toml(
                tmp_path / "kstrl.toml",
                """
[evolution]
journal_path = "custom/evolution.jsonl"
""",
            )
            config = EvolutionConfig.load(tmp_path)
            assert config.journal_path == tmp_path / "custom/evolution.jsonl"

    class TestSecurityConfigLoad:
        def test_reads_security_section(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            _clear_env(
                monkeypatch,
                "KSTRL_SECURITY_MODE",
                "KSTRL_SECURITY_FAIL_THRESHOLD",
            )
            _write_toml(
                tmp_path / "kstrl.toml",
                """
[security]
mode = "hard"
fail_threshold = "critical"
""",
            )
            config = SecurityConfig.load(tmp_path)
            assert config.mode == SecurityMode.HARD.value
            assert config.fail_threshold == "critical"

        def test_invalid_mode_in_toml_raises(self, tmp_path: Path) -> None:
            _write_toml(tmp_path / "kstrl.toml", '[security]\nmode = "blocky"\n')
            with pytest.raises(ValueError, match="Invalid SecurityConfig.mode"):
                SecurityConfig.load(tmp_path)

        def test_invalid_threshold_in_toml_raises(self, tmp_path: Path) -> None:
            _write_toml(tmp_path / "kstrl.toml", '[security]\nfail_threshold = "scary"\n')
            with pytest.raises(ValueError, match="Invalid SecurityConfig.fail_threshold"):
                SecurityConfig.load(tmp_path)

        def test_invalid_threshold_in_env_raises(
            self,
            tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            monkeypatch.setenv("KSTRL_SECURITY_FAIL_THRESHOLD", "critcial")
            with pytest.raises(ValueError):
                SecurityConfig.load(tmp_path)

    @pytest.mark.parametrize(
        "loader",
        [
            FactoryConfig.load,
            VerifyConfig.load,
            ContractConfig.load,
            CodebaseScanConfig.load,
            EvolutionConfig.load,
            SecurityConfig.load,
        ],
    )
    def test_malformed_toml_raises_value_error(
        self,
        loader: Any,
        tmp_path: Path,
    ) -> None:
        _write_toml(tmp_path / "kstrl.toml", "this is = not = valid = [ toml\n")
        with pytest.raises(ValueError, match="Invalid TOML"):
            loader(tmp_path)
