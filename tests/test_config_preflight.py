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
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

import kstrl.cli as cli_mod
from kstrl.cli import cli
from kstrl.config import ConfigError
from kstrl.config_preflight import config_sections, preflight_config
from kstrl.factory import FactoryResult
from kstrl.manifest import Component, Manifest

MALFORMED_TOML = "[verify\ntest_command = 'pytest'\n"

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


def _write_manifest(path: Path) -> None:
    Manifest(
        version="1",
        spec_file="s.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a",
                "Comp A",
                "Desc",
                [],
                "scripts/kstrl/feature/comp-a/prd.json",
                "kstrl/factory/comp-a",
            )
        ],
    ).save(path)


def _invoke(args: list[str], *, toml: str | None = None) -> Result:
    """Run a command in an isolated checkout holding a spec and manifest."""
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        (root / "s.md").write_text("# spec\n")
        _write_manifest(root / "m.json")
        if toml is not None:
            (root / "kstrl.toml").write_text(toml)
        return runner.invoke(cli, args, catch_exceptions=True)


def _no_agents(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Record every agent construction and every architect call."""
    built: list[tuple[Any, ...]] = []

    def fake_get_agent(*args: Any, **kwargs: Any) -> Any:
        built.append(("get_agent", args))
        raise AssertionError("an agent was constructed after a rejected config")

    def fake_decompose_spec(*args: Any, **kwargs: Any) -> Any:
        built.append(("decompose_spec", args))
        raise AssertionError("the architect was invoked after a rejected config")

    monkeypatch.setattr(cli_mod, "get_agent", fake_get_agent)
    monkeypatch.setattr(cli_mod, "decompose_spec", fake_decompose_spec)
    return built


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
        monkeypatch.setattr(cli_mod, "run_factory", lambda *a, **k: FactoryResult())

        result = _invoke(["factory", "--manifest", "m.json", "--agent-cmd", "true", "--yes"])

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

    ARGS = ["factory", "--manifest", "m.json", "--agent-cmd", "true", "--yes"]

    def test_a_bad_evolution_knob_warns_and_the_run_still_happens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ran: list[bool] = []
        monkeypatch.setattr(
            cli_mod,
            "run_factory",
            lambda *a, **k: (ran.append(True), FactoryResult())[1],
        )

        result = _invoke(self.ARGS, toml='[evolution]\nlookback_runs = "many"\n')

        assert result.exit_code == 0, result.output
        assert ran == [True]
        assert "[evolution]" in result.output
        assert "continuing without it" in result.output

    def test_evolve_treats_the_same_evolution_knob_as_fatal(self) -> None:
        """Degrading means "the audit trail is dropped from work that is
        about something else". `ks evolve` IS the journal, so the value
        the preflight warns about has to stop THAT command - with an
        error line, not the traceback the warning would otherwise be
        followed by two lines later."""
        result = _invoke(["evolve", "--status"], toml='[evolution]\nlookback_runs = "many"\n')

        assert result.exit_code == 1
        assert "[evolution] configuration is unusable" in result.output
        assert not isinstance(result.exception, ValueError), result.exception

    def test_a_bad_verify_knob_stops_the_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ran: list[bool] = []
        monkeypatch.setattr(
            cli_mod,
            "run_factory",
            lambda *a, **k: (ran.append(True), FactoryResult())[1],
        )

        result = _invoke(self.ARGS, toml='[verify]\nmutation_threshold = "many"\n')

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

    def test_a_clean_config_raises_nothing(self, tmp_path: Path) -> None:
        """The example config ships as documentation; it has to pass."""
        example = Path(__file__).resolve().parents[1] / "kstrl.toml.example"
        (tmp_path / "kstrl.toml").write_text(example.read_text())

        warnings: list[str] = []
        preflight_config(tmp_path, warn=warnings.append)

        assert warnings == []


class TestTheRootIsTheOneTheCommandWillUse:
    """Why the check sits on the COMMAND and not on the group: at group
    level click has not parsed ``--root`` yet, so a preflight there would
    read the config of whatever directory the operator happened to be
    standing in."""

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
        monkeypatch.setattr(cli_mod, "run_factory", lambda *a, **k: FactoryResult())

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            root = Path(fs)
            (root / "kstrl.toml").write_text(MALFORMED_TOML)
            _write_manifest(clean / "m.json")
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


class TestTheCommandsThatMustSurviveABrokenConfig:
    """Three exemptions, each of which would otherwise take away the tool
    the operator recovers with, or replace a machine contract with a
    weaker one."""

    def test_config_show_still_explains_the_file_it_cannot_load(self) -> None:
        result = _invoke(["config", "show"], toml=MALFORMED_TOML)

        assert "Invalid TOML" in result.output
        assert result.exit_code == 1

    def test_init_still_scaffolds_next_to_a_broken_file(self) -> None:
        result = _invoke(["init", "--ui", "plain", "--no-color"], toml=MALFORMED_TOML)

        # Whatever init decides about an existing project, it is not
        # allowed to be "cannot parse the file I am here to write".
        assert result.exit_code == 0, result.output
        assert "Invalid TOML" not in result.output
        assert "Created prompt.md" in result.output

    def test_sense_keeps_its_exit_2_and_its_json_envelope(self) -> None:
        result = _invoke(["sense", "--json"], toml=MALFORMED_TOML)

        assert result.exit_code == 2
        assert "error" in json.loads(result.stdout)

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


class TestEverySectionIsEnrolled:
    """The registry is only a guarantee while it is complete.

    Mirrors ``tests/test_prompt_versions.py``, which AST-walks for
    ``*_PROMPT`` constants and fails on one that is not enrolled: a
    config dataclass added later must not be able to reintroduce the
    lazily-parsed section this issue removed.
    """

    @staticmethod
    def _config_classes_with_a_loader() -> set[str]:
        found: set[str] = set()
        for path in sorted((Path(__file__).resolve().parents[1] / "kstrl").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef) or not node.name.endswith("Config"):
                    continue
                loaders = {
                    child.name
                    for child in node.body
                    if isinstance(child, ast.FunctionDef) and child.name == "load"
                }
                if loaders:
                    found.add(node.name)
        return found

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
        example = (Path(__file__).resolve().parents[1] / "kstrl.toml.example").read_text()
        names = {name for section in config_sections() for name in section.sections}

        assert {name for name in names if f"[{name}]" not in example} == set()
