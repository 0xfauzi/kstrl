"""R2.4 `ks config show`: resolved config with per-value sources.

The command is the observability surface for the R2.1 control plane:
every documented knob prints with the source that produced its value
(flag / env / toml / default).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli

ALL_SECTIONS = [
    "[agent]",
    "[run]",
    "[paths]",
    "[git]",
    "[ui]",
    "[factory]",
    "[verify]",
    "[security]",
    "[contract]",
    "[codebase_scan]",
    "[knowledge]",
    "[evolution]",
    "[timeout]",
    "[notify]",
    "[linear]",
]


def _line_for(output: str, key: str) -> str:
    matches = [line for line in output.splitlines() if line.strip().startswith(f"{key} = ")]
    assert matches, f"no output line for key {key!r}:\n{output}"
    assert len(matches) == 1, f"ambiguous key {key!r}: {matches}"
    return matches[0]


class TestConfigShowSources:
    def _invoke(self, root: Path, *extra: str) -> str:
        result = CliRunner().invoke(
            cli,
            ["config", "show", "--root", str(root), *extra],
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_toml_env_flag_default_sources(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[run]\nmax_iterations = 42\n\n[factory]\nmax_parallel = 9\n"
        )
        # This test asserts the SHIPPED retry_delay default and its source;
        # the suite-wide short_waits fixture (tests/conftest.py) sets the
        # env var, so it is undone here.
        monkeypatch.delenv("FACTORY_RETRY_DELAY", raising=False)
        monkeypatch.setenv("SLEEP_SECONDS", "9.5")
        monkeypatch.setenv("FACTORY_MAX_RETRIES", "7")

        output = self._invoke(tmp_path, "--model", "flagmodel")

        # KstrlConfig-backed sections
        line = _line_for(output, "max_iterations")
        assert "42" in line and "(toml)" in line
        line = _line_for(output, "sleep_seconds")
        assert "9.5" in line and "(env)" in line
        # "model" also exists under [security]; scope to the [agent] slice.
        agent_slice = output.split("[agent]")[1].split("[run]")[0]
        line = _line_for(agent_slice, "model")
        assert "'flagmodel'" in line and "(flag)" in line
        line = _line_for(output, "interactive")
        assert "(default)" in line

        # Phase sections resolved through the R2.1 loaders
        line = _line_for(output, "max_parallel")
        assert "9" in line and "(toml)" in line
        line = _line_for(output, "max_retries")
        assert "7" in line and "(env)" in line
        line = _line_for(output, "retry_delay")
        assert "5.0" in line and "(default)" in line

    def test_all_sections_present(self, tmp_path: Path) -> None:
        output = self._invoke(tmp_path)
        for section in ALL_SECTIONS:
            assert section in output, f"missing section {section}"

    def test_phase_env_source_detected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_TIMEOUT_AGENT_ITERATION", "123")
        monkeypatch.setenv("KSTRL_SECURITY_MODE", "hard")

        output = self._invoke(tmp_path)

        line = _line_for(output, "agent_iteration")
        assert "123" in line and "(env)" in line
        # [security] and [contract] both have a "mode" key; scope to the
        # security section slice.
        security_slice = output.split("[security]")[1].split("[contract]")[0]
        line = _line_for(security_slice, "mode")
        assert "'hard'" in line and "(env)" in line

    def test_env_does_not_leak_into_process(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The scrubbed-environ probe restores os.environ afterwards."""
        import os

        monkeypatch.setenv("KSTRL_CONFIG_SHOW_CANARY", "1")
        self._invoke(tmp_path)
        assert os.environ.get("KSTRL_CONFIG_SHOW_CANARY") == "1"

    def test_malformed_toml_fails_cleanly(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[run\nmax_iterations = 1\n")
        result = CliRunner().invoke(
            cli,
            ["config", "show", "--root", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "error:" in result.output


# ---------------------------------------------------------------------------
# #649: every documented kstrl.toml key has a row in `ks config show`.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The two ``[ui]`` rows with no kstrl.toml key: ``--ui``/``KSTRL_UI`` and
#: ``NO_COLOR`` set them, and ``show_sections`` renders them on purpose.
UI_ONLY_ROWS = {("ui", "ui_mode"), ("ui", "no_color")}

#: One key per section the report used to leave out (#649), plus the four
#: the issue measured: ``(section, key) -> (toml literal, printed value)``.
#: Each value differs from its default except ``[inbox] enabled``, which the
#: issue names and whose default is already true; its source still says toml.
SET_IN_TOML: dict[tuple[str, str], tuple[str, str]] = {
    ("agent", "budget_usd"): ("5", "5.0"),
    ("factory", "claim_agreement"): ('"block"', "'block'"),
    ("inbox", "enabled"): ("true", "True"),
    ("autonomy", "enabled"): ("true", "True"),
    ("policy", "max_files_changed"): ("7", "7"),
    ("divergence", "growth_steps"): ("5", "5"),
    ("breaker", "no_progress_iterations"): ("6", "6"),
    ("sandbox", "allow_network"): ("true", "True"),
    ("queue", "max_attempts"): ("5", "5"),
    ("release", "environment"): ('"staging"', "'staging'"),
    ("signals", "new_issue_events"): ("4", "4"),
}


def _ks_config_show(root: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """``ks config show`` in a subprocess: ``{(section, key): (value, source)}``.

    ``KSTRL_*`` and ``FACTORY_*`` are removed from the child's environment
    so every source it prints is the file's or the default's.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    done = subprocess.run(
        [sys.executable, "-m", "kstrl", "config", "show", "--root", str(root)],
        cwd=root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    rows: dict[tuple[str, str], tuple[str, str]] = {}
    section = ""
    for line in done.stdout.splitlines():
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif line.startswith("  ") and " = " in line:
            key, _, rest = line.strip().partition(" = ")
            value, _, source = rest.rpartition("  (")
            rows[(section, key)] = (value, source.removesuffix(")"))
    return rows


def _git_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=30)
    return root


def _documented_keys() -> set[tuple[str, str]]:
    """Every ``(section, key)`` scripts/gen_docs.py documents."""
    spec = importlib.util.spec_from_file_location(
        "gen_docs_config_show", REPO_ROOT / "scripts" / "gen_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves postponed annotations through sys.modules.
    sys.modules["gen_docs_config_show"] = module
    spec.loader.exec_module(module)
    return {(s.section, key) for s in module._section_specs() for key in s.keys}


class TestEveryDocumentedKeyIsShown:
    def test_the_rows_are_the_documented_keys(self, tmp_path: Path) -> None:
        """The census: on an empty repository the report's rows are exactly
        the documented keys plus the two UI-only rows. A key documented and
        not shown, or shown and not documented, fails here by name."""
        shown = set(_ks_config_show(_git_repo(tmp_path)))
        documented = _documented_keys()
        assert sorted(documented - shown) == []
        assert sorted(shown - documented) == sorted(UI_ONLY_ROWS)

    def test_a_key_set_in_kstrl_toml_shows_its_value_and_toml(self, tmp_path: Path) -> None:
        root = _git_repo(tmp_path)
        tables: dict[str, list[str]] = {}
        for (section, key), (literal, _printed) in SET_IN_TOML.items():
            tables.setdefault(section, []).append(f"{key} = {literal}")
        (root / "kstrl.toml").write_text(
            "".join(f"[{name}]\n" + "\n".join(lines) + "\n\n" for name, lines in tables.items()),
            encoding="utf-8",
        )

        rows = _ks_config_show(root)

        assert {key: rows.get(key) for key in SET_IN_TOML} == {
            key: (printed, "toml") for key, (_literal, printed) in SET_IN_TOML.items()
        }

    def test_an_unset_agent_budget_says_no_limit(self, tmp_path: Path) -> None:
        rows = _ks_config_show(_git_repo(tmp_path))
        assert rows.get(("agent", "budget_usd")) == ("no limit", "default")
