"""Every kstrl.toml name either takes effect or is named at entry (#525).

Three shapes used to load without error and change nothing: a section
written as a value (``learning = false``), a key or section no loader
reads (a misspelling), and five ``[timeout]`` keys that ``ks init``
scaffolded and ``ks config show`` printed as limits while no code read
them. Each test here drives the real CLI in a subprocess, or the real
loader on a real file, and asserts on what the operator sees.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.config import ConfigError, KstrlConfig, load_toml_section
from kstrl.config_preflight import collect_config_problems
from kstrl.playbook import LearningConfig
from tests.test_gen_docs import _load_gen_docs

REFUSED = "configuration rejected before anything was started"


def _ks(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``ks <args>`` in ``root``, with no KSTRL_ variable inherited."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("KSTRL_")}
    env["KSTRL_NO_TUI"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )


def _status(tmp_path: Path, toml: str) -> str:
    (tmp_path / "kstrl.toml").write_text(toml, encoding="utf-8")
    return _ks(tmp_path, "status").stdout


class TestTheEntryCheckNamesIt:
    def test_known_names_pass_the_entry_check(self, tmp_path: Path) -> None:
        """The control: the same command, a file of names kstrl reads,
        and the command gets past the entry check to its own work."""
        output = _status(
            tmp_path, "[factory]\nmax_parallel = 2\n\n[learning]\ncontribute = false\n"
        )
        assert REFUSED not in output, output
        assert "No manifest found" in output, output

    def test_a_misspelled_key_is_refused_by_name(self, tmp_path: Path) -> None:
        output = _status(tmp_path, "[factory]\nmax_cost_ud = 5.0\n")
        assert REFUSED in output, output
        assert "names [factory] max_cost_ud, which no kstrl setting reads" in output, output

    def test_an_unknown_section_is_refused_by_name(self, tmp_path: Path) -> None:
        output = _status(tmp_path, "[factroy]\nmax_parallel = 2\n")
        assert REFUSED in output, output
        assert "names [factroy], which no kstrl setting reads" in output, output

    def test_a_section_written_as_a_value_is_refused(self, tmp_path: Path) -> None:
        """PR #512's handoff: ``learning = false`` left contribute on."""
        output = _status(tmp_path, "learning = false\n")
        assert REFUSED in output, output
        assert "sets learning = False, but kstrl reads [learning] as a table" in output, output

    def test_a_kstrl_config_section_written_as_a_value_is_refused(self, tmp_path: Path) -> None:
        """``KstrlConfig`` reads its five sections from the document
        itself, not through ``load_toml_section``; same refusal."""
        output = _status(tmp_path, 'agent = "codex"\n')
        assert REFUSED in output, output
        assert "sets agent = 'codex', but kstrl reads [agent] as a table" in output, output

    def test_doctor_names_a_misspelled_key(self, tmp_path: Path) -> None:
        """``ks doctor`` is exempt from the entry seam and reports the
        file itself; the same name reaches its kstrl_config check."""
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_cost_ud = 5.0\n", encoding="utf-8")
        output = _ks(tmp_path, "doctor").stdout
        assert "[fail] kstrl_config" in output, output
        assert "names [factory] max_cost_ud, which no kstrl setting reads" in output, output


class TestTheRemovedTimeoutKeys:
    def test_config_show_neither_prints_nor_accepts_a_removed_key(self, tmp_path: Path) -> None:
        """``[timeout] review_agent = 600`` printed ``600.0 (toml)`` while
        the reviewer call ran with no limit."""
        (tmp_path / "kstrl.toml").write_text("[timeout]\nreview_agent = 600\n", encoding="utf-8")
        result = _ks(tmp_path, "config", "show", "--ui", "plain", "--no-color")
        assert result.returncode == 1, result.stdout
        assert "review_agent = " not in result.stdout, result.stdout
        assert "names [timeout] review_agent, which no kstrl setting reads" in result.stdout


class TestTheLoadersRefuseAValue:
    def test_load_toml_section_refuses_a_section_written_as_a_value(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "kstrl.toml"
        toml_path.write_text('factory = "hi"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match=r"sets factory = 'hi'"):
            load_toml_section(toml_path, "factory")

    def test_learning_written_as_a_value_does_not_contribute(self, tmp_path: Path) -> None:
        """A run reading config after the entry check fails closed: the
        opt-out may be in the value it could not use."""
        (tmp_path / "kstrl.toml").write_text("learning = false\n", encoding="utf-8")
        assert LearningConfig.load(tmp_path).contribute is False

    def test_kstrl_config_refuses_a_section_written_as_a_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KstrlConfig`` reads its five sections from the document, not
        through ``load_toml_section``. The entry check would name
        ``agent = "codex"`` even if this loader skipped it, so only a
        direct load shows that a run re-reading config fails too."""
        for name in [k for k in os.environ if k.startswith("KSTRL_")]:
            monkeypatch.delenv(name)
        (tmp_path / "kstrl.toml").write_text('agent = "codex"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match=r"sets agent = 'codex'"):
            KstrlConfig.load(root_dir=tmp_path)


def test_a_rejected_section_is_not_also_called_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loader that raises stops asking part way, so the keys after the
    bad one were never asked for. Its rejection is the one line."""
    for name in [k for k in os.environ if k.startswith("KSTRL_")]:
        monkeypatch.delenv(name)
    (tmp_path / "kstrl.toml").write_text(
        '[factory]\nmax_parallel = "many"\nmax_retries = 2\n', encoding="utf-8"
    )
    problems = collect_config_problems(tmp_path, lambda _message: None)
    assert len(problems) == 1, problems
    assert "max_parallel" in problems[0]


def test_every_documented_key_at_its_default_passes_the_entry_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over-refusal is the other way to be wrong: a key a loader reads
    only on some branch would be named as unread. Every key the config
    reference documents, set to its default, must pass."""
    for name in [k for k in os.environ if k.startswith("KSTRL_")]:
        monkeypatch.delenv(name)
    gen_docs = _load_gen_docs()
    lines: list[str] = []
    written = 0
    for spec in gen_docs._section_specs():
        lines.append(f"[{spec.section}]")
        for key, field in spec.keys.items():
            value = getattr(spec.defaults, field)
            if value is None:
                continue
            lines.append(f"{key} = {_toml_literal(value)}")
            written += 1
    assert written > 100, written
    (tmp_path / "kstrl.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    warnings: list[str] = []
    assert collect_config_problems(tmp_path, warnings.append) == []
    assert warnings == []


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
