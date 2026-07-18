"""R2.4 `ralph config show`: resolved config with per-value sources.

The command is the observability surface for the R2.1 control plane:
every documented knob prints with the source that produced its value
(flag / env / toml / default).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from ralph_py.cli import cli

ALL_SECTIONS = [
    "[agent]", "[run]", "[paths]", "[git]", "[ui]",
    "[factory]", "[verify]", "[security]", "[contract]",
    "[feedforward]", "[knowledge]", "[evolution]", "[timeout]",
]


def _line_for(output: str, key: str) -> str:
    matches = [
        line for line in output.splitlines()
        if line.strip().startswith(f"{key} = ")
    ]
    assert matches, f"no output line for key {key!r}:\n{output}"
    assert len(matches) == 1, f"ambiguous key {key!r}: {matches}"
    return matches[0]


class TestConfigShowSources:
    def _invoke(self, root: Path, *extra: str) -> str:
        result = CliRunner().invoke(
            cli, ["config", "show", "--root", str(root), *extra],
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_toml_env_flag_default_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "ralph.toml").write_text(
            "[run]\nmax_iterations = 42\n\n[factory]\nmax_parallel = 9\n"
        )
        monkeypatch.setenv("SLEEP_SECONDS", "9.5")
        monkeypatch.setenv("FACTORY_MAX_RETRIES", "7")

        output = self._invoke(tmp_path, "--model", "flagmodel")

        # RalphConfig-backed sections
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RALPH_TIMEOUT_AGENT_ITERATION", "123")
        monkeypatch.setenv("RALPH_SECURITY_MODE", "advisory")

        output = self._invoke(tmp_path)

        line = _line_for(output, "agent_iteration")
        assert "123" in line and "(env)" in line
        # [security] and [contract] both have a "mode" key; scope to the
        # security section slice.
        security_slice = output.split("[security]")[1].split("[contract]")[0]
        line = _line_for(security_slice, "mode")
        assert "'advisory'" in line and "(env)" in line

    def test_env_does_not_leak_into_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The scrubbed-environ probe restores os.environ afterwards."""
        import os

        monkeypatch.setenv("RALPH_CONFIG_SHOW_CANARY", "1")
        self._invoke(tmp_path)
        assert os.environ.get("RALPH_CONFIG_SHOW_CANARY") == "1"

    def test_malformed_toml_fails_cleanly(self, tmp_path: Path) -> None:
        (tmp_path / "ralph.toml").write_text("[run\nmax_iterations = 1\n")
        result = CliRunner().invoke(
            cli, ["config", "show", "--root", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "error:" in result.output
