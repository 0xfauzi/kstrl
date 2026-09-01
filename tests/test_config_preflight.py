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
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result

import kstrl.cli as cli_mod
from kstrl.cli import cli
from kstrl.config import ConfigError, load_toml_document, toml_parse_scope
from kstrl.config_preflight import config_sections, preflight_config
from kstrl.factory import FactoryResult
from tests.conftest import REPO_ROOT
from tests.spine_utils import component, make_manifest

MALFORMED_TOML = "[verify\ntest_command = 'pytest'\n"

#: Syntactically perfect TOML that is not utf-8: one 0xe9, the byte an
#: editor set to ISO-8859-1 writes for an e-acute. ``tomllib.load``
#: decodes the stream itself, so this fails before the lexer ever runs,
#: and it fails with ``UnicodeDecodeError`` - a ``ValueError``, not a
#: ``TOMLDecodeError`` (#318). Real bytes rather than a patched decoder:
#: the whole defect is which exception the standard library actually
#: raises here.
NON_UTF8_TOML = b'[agent]\nname = "\xe9"\n'

#: Both shapes a kstrl.toml can be broken in, each with the fragment
#: the operator is shown for it. One list so a surface that has to
#: survive a broken file is asserted against every kind of broken file
#: there is, rather than against whichever one was current when it was
#: written.
BAD_TOML: list[tuple[str | bytes, str]] = [
    (MALFORMED_TOML, "Invalid TOML"),
    (NON_UTF8_TOML, "not valid UTF-8"),
]

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
    :data:`NON_UTF8_TOML`. One write for both, so the ``str`` cases are
    utf-8 on a machine whose locale is not.
    """
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        (root / "s.md").write_text("# spec\n")
        make_manifest([component("comp-a")]).save(root / "m.json")
        if toml is not None:
            (root / "kstrl.toml").write_bytes(toml.encode() if isinstance(toml, str) else toml)
        return runner.invoke(cli, args, catch_exceptions=True)


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


def _class_defs(path: Path) -> list[ast.ClassDef]:
    """Every class defined in one file.

    A file that will not parse yields none: that is a defect for mypy
    and ruff to report, not a reason for this test to fail obscurely.
    ``tests/test_prompt_versions.py``'s walk tolerates it the same way.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    return [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


def _defines_load(node: ast.ClassDef) -> bool:
    return any(isinstance(child, ast.FunctionDef) and child.name == "load" for child in node.body)


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
        assert result.exit_code == 1
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
        assert result.exit_code == 1
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
        assert result.exit_code == 1
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

        assert result.exit_code == 1
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

        assert result.exit_code == 1
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
    def _other_checkout(tmp_path: Path, toml: str) -> Path:
        """A second project, with a prompt file at the layout
        ``_resolve_root`` recognises."""
        other = tmp_path / "other"
        (other / "scripts" / "kstrl").mkdir(parents=True)
        (other / "kstrl.toml").write_text(toml)
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

        assert result.exit_code == 1
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

        assert result.exit_code == 1
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
        assert result.exit_code == 1
        assert "[linear]" in result.output

    def test_a_broken_config_under_root_is_found_from_a_clean_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        elsewhere = tmp_path / "project"
        elsewhere.mkdir()
        (elsewhere / "kstrl.toml").write_text(MALFORMED_TOML)
        built = _no_agents(monkeypatch)

        result = _invoke([*DECOMPOSE_ARGS, "--root", str(elsewhere)])

        assert built == []
        assert result.exit_code == 1
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

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            root = Path(fs)
            (root / "kstrl.toml").write_text(MALFORMED_TOML)
            make_manifest([component("comp-a")]).save(clean / "m.json")
            result = runner.invoke(
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


#: Every command measured crashing on :data:`NON_UTF8_TOML` before
#: #318, with the exit code each one documents for a rejected
#: configuration. That set is exactly the commands NOT exempt from the
#: seam, which is the point: the blast radius of the escaping
#: ``UnicodeDecodeError`` was the seam itself, not any command's own
#: config handling. ``test_the_table_names_every_command_the_seam_guards``
#: keeps the list honest rather than a count in a comment doing it.
NON_UTF8_CRASHED: list[tuple[list[str], int]] = [
    (["autonomy", "status"], 1),
    (["dash"], 1),
    (DECOMPOSE_ARGS, 1),
    (["evolve"], 1),
    (FACTORY_ARGS, 1),
    (["feature", "--prd", "s.md", "--agent-cmd", "true"], 1),
    (["inbox", "ls"], 1),
    (["queue", "ls"], 1),
    (["retry", "comp-a"], 1),
    (["run", "--agent-cmd", "true"], 1),
    (["serve", "--print-plist", "--no-color"], 2),
    (["status"], 1),
    (["understand", "--agent-cmd", "true"], 1),
]


class TestANonUtf8ConfigIsReportedNotCrashed:
    """#318: ``tomllib.load`` decodes the stream as utf-8 itself, so one
    latin-1 byte in kstrl.toml raises ``UnicodeDecodeError``. That is a
    ``ValueError`` and not a ``TOMLDecodeError``, so it walked past the
    only handler ``load_toml_document`` had and out of the seam that
    every non-exempt command sits behind."""

    @pytest.mark.parametrize(
        ("args", "exit_code"),
        NON_UTF8_CRASHED,
        ids=[args[0] for args, _ in NON_UTF8_CRASHED],
    )
    def test_the_command_reports_the_file_instead_of_a_traceback(
        self,
        args: list[str],
        exit_code: int,
    ) -> None:
        result = _invoke(args, toml=NON_UTF8_TOML)

        # The regression itself, asserted as the escaped exception and
        # not only as an exit code: before #318 every one of these ended
        # with a UnicodeDecodeError out of `tomllib.load`, and an exit
        # code of 1 is what click reports for that too.
        assert not isinstance(result.exception, UnicodeDecodeError)
        assert result.exit_code == exit_code, result.output
        assert "error:" in result.output
        assert "not valid UTF-8" in result.output

    def test_no_agent_is_constructed_on_the_paid_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The seam's whole promise, restated for this fault: `decompose`
        stops before the architect rather than after it."""
        built = _no_agents(monkeypatch)

        result = _invoke(DECOMPOSE_ARGS, toml=NON_UTF8_TOML)

        assert built == []
        assert result.exit_code == 1

    def test_the_table_names_every_command_the_seam_guards(self) -> None:
        """The drift guard, in the shape ``TestEverySectionIsEnrolled``
        and ``TestTheSeamCannotBeBypassedByDeclaration`` already use: a
        command added later is covered, or this fails. One shared seam
        serves all of them, so the table is the record of what was
        measured broken rather than of that many independent paths - but
        it is checked against the live registry, not against a number
        somebody wrote in a comment.

        The three exempt commands are covered by
        ``TestTheCommandsThatMustSurviveABrokenConfig``, which runs its
        cases over :data:`BAD_TOML`.
        """
        covered = {args[0] for args, _ in NON_UTF8_CRASHED}

        assert covered == set(cli.commands) - cli_mod._PREFLIGHT_EXEMPT


class TestTheCommandsThatMustSurviveABrokenConfig:
    """Three exemptions, each of which would otherwise take away the tool
    the operator recovers with, or replace a machine contract with a
    weaker one.

    The three parametrized cases run against BOTH shapes a kstrl.toml
    can be broken in, because an exemption that survives one and not the
    other is not an exemption. #318 was exactly that: `config show`
    printed the codec message with no path in it while the syntax-error
    case named the file.
    """

    @pytest.mark.parametrize(("toml", "fragment"), BAD_TOML, ids=[f for _, f in BAD_TOML])
    def test_config_show_still_explains_the_file_it_cannot_load(
        self,
        toml: str | bytes,
        fragment: str,
    ) -> None:
        result = _invoke(["config", "show"], toml=toml)

        assert result.exit_code == 1
        assert fragment in result.output
        # Which file, not just what is wrong with it: an operator with
        # more than one checkout cannot act on the codec message alone.
        assert "kstrl.toml" in result.output

    @pytest.mark.parametrize(("toml", "fragment"), BAD_TOML, ids=[f for _, f in BAD_TOML])
    def test_init_still_scaffolds_next_to_a_broken_file(
        self,
        toml: str | bytes,
        fragment: str,
    ) -> None:
        result = _invoke(["init", "--ui", "plain", "--no-color"], toml=toml)

        # Whatever init decides about an existing project, it is not
        # allowed to be "cannot parse the file I am here to write".
        assert result.exit_code == 0, result.output
        assert fragment not in result.output
        assert "Created prompt.md" in result.output

    @pytest.mark.parametrize(("toml", "fragment"), BAD_TOML, ids=[f for _, f in BAD_TOML])
    def test_sense_keeps_its_exit_2_and_its_json_envelope(
        self,
        toml: str | bytes,
        fragment: str,
    ) -> None:
        result = _invoke(["sense", "--json"], toml=toml)

        assert result.exit_code == 2
        assert fragment in json.loads(result.stdout)["error"]

    def test_sense_checks_sections_it_does_not_itself_read(self) -> None:
        """`sense` loads four sections of its own. An exemption that
        checked only those would keep the "depends which section you
        typo'd" property inside itself, so it runs the whole preflight
        under its own contract."""
        result = _invoke(["sense", "--json"], toml='[linear]\ntimeout_seconds = "soon"\n')

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
        assert code == 1
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

        assert refused.exit_code == 1
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
    ``*_PROMPT`` constants and fails on one that is not enrolled: a
    config dataclass added later must not be able to reintroduce the
    lazily-parsed section this issue removed.
    """

    @staticmethod
    def _config_classes_with_a_loader() -> set[str]:
        return {
            node.name
            for path in sorted((REPO_ROOT / "kstrl").rglob("*.py"))
            for node in _class_defs(path)
            if node.name.endswith("Config") and _defines_load(node)
        }

    def test_no_config_dataclass_is_missing_from_the_registry(self) -> None:
        registered = {
            getattr(section.loader, "__self__", type(None)).__name__
            for section in config_sections()
        }

        assert self._config_classes_with_a_loader() - registered == set()

    def test_the_registry_names_a_real_toml_section_for_each_loader(self) -> None:
        """A section name is what the error line points the operator at,
        so a typo in the registry would name a table that does not
        exist. Every name here appears in the shipped example."""
        example = (REPO_ROOT / "kstrl.toml.example").read_text()
        names = {name for section in config_sections() for name in section.sections}

        assert {name for name in names if f"[{name}]" not in example} == set()
