"""R8.5 Layer 0 end-to-end tests: test-diff discipline and oracle-signal
linting, exercised through `check_test_adequacy` against real git repos.

The layer exists because agent-written tests cannot be assumed adequate
(80.2% of 86k agent-authored test patches carry weak or no oracle
signals, arXiv:2606.18168), so `TestCheckEndToEnd` drives the whole
pipeline (diff discipline plus oracle-strength linting) against real
commits: a diff that WEAKENS the suite, and a new test that asserts
nothing falsifiable, must surface as findings and, at L1+, block.
`TestConfigLoad` covers the two ways `AdequacyConfig` is populated from
a real project directory (`kstrl.toml` and env override) since a wrong
read there silently disables or misconfigures the whole gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kstrl.adequacy import AdequacyConfig
from kstrl.verify import check_test_adequacy
from tests.helpers import gitrepo


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------
class TestConfigLoad:
    def test_load_reads_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text('[adequacy]\nenabled = true\nlayer0 = "block"\n')
        config = AdequacyConfig.load(tmp_path)
        assert config.enabled is True and config.layer0 == "block"

    def test_env_overrides_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[adequacy]\nenabled = false\n")
        monkeypatch.setenv("KSTRL_ADEQUACY_ENABLED", "1")
        assert AdequacyConfig.load(tmp_path).enabled is True


# --------------------------------------------------------------------------
# End to end through the verifier, against a real repo
# --------------------------------------------------------------------------
def _repo(root: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

    run("init")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    gitrepo.set_identity(root)
    (root / "tests").mkdir()
    (root / "tests" / "test_core.py").write_text(
        "def test_adds():\n    assert add(2, 2) == 4\n\n"
        "def test_subs():\n    assert sub(4, 2) == 2\n"
    )
    (root / "README.md").write_text("base\n")
    run("add", ".")
    run("commit", "-m", "base")
    run("checkout", "-b", "feature")


def _weaken(root: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

    (root / "tests" / "test_core.py").write_text(
        "import pytest\n\n@pytest.mark.xfail(reason='flaky')\n"
        "def test_adds():\n    assert add(2, 2) is not None\n"
    )
    (root / "tests" / "test_new.py").write_text("def test_it_runs():\n    build()\n")
    run("add", ".")
    run("commit", "-m", "weaken")


class TestCheckEndToEnd:
    def test_advisory_records_without_blocking(self, tmp_path: Path) -> None:
        _repo(tmp_path)
        _weaken(tmp_path)
        result = check_test_adequacy(
            tmp_path,
            "main",
            AdequacyConfig(enabled=True),
            autonomy_level=0,
        )
        assert result.passed, "advisory must not fail the check"
        assert result.findings
        assert all(f.severity == "advisory" for f in result.findings)
        categories = {f.category for f in result.findings}
        assert "adequacy_test_deleted" in categories  # test_subs left
        assert "adequacy_test_skipped" in categories  # xfail added
        assert "adequacy_no_oracle" in categories  # test_new asserts nothing

    def test_l1_blocks_on_the_same_diff(self, tmp_path: Path) -> None:
        _repo(tmp_path)
        _weaken(tmp_path)
        result = check_test_adequacy(
            tmp_path,
            "main",
            AdequacyConfig(enabled=True),
            autonomy_level=1,
        )
        assert not result.passed
        assert all(f.severity == "high" for f in result.findings)

    def test_clean_change_passes_with_no_findings(self, tmp_path: Path) -> None:
        _repo(tmp_path)
        subprocess.run(
            ["git", "checkout", "-b", "clean"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        (tmp_path / "tests" / "test_more.py").write_text(
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        for args in (["add", "."], ["commit", "-m", "add a real test"]):
            subprocess.run(
                ["git", *args],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            )
        result = check_test_adequacy(
            tmp_path,
            "main",
            AdequacyConfig(enabled=True),
            autonomy_level=1,
        )
        assert result.passed
        assert result.findings == []

    def test_editing_a_weak_legacy_file_does_not_block(
        self,
        tmp_path: Path,
    ) -> None:
        # P2-d end to end: the file's tests predate the gate and its diff
        # weakens nothing, so a one-line edit must survive L1.
        _repo(tmp_path)
        (tmp_path / "tests" / "test_legacy.py").write_text(
            "def test_a():\n    assert build() is not None\n"
        )
        for args in (["add", "-A"], ["commit", "-m", "legacy"]):
            subprocess.run(
                ["git", *args],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "merge", "feature"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", "edit"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        (tmp_path / "tests" / "test_legacy.py").write_text(
            "def test_a():\n    # tidy up\n    assert build() is not None\n"
        )
        for args in (["add", "-A"], ["commit", "-m", "tidy"]):
            subprocess.run(
                ["git", *args],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            )
        result = check_test_adequacy(
            tmp_path,
            "main",
            AdequacyConfig(enabled=True),
            autonomy_level=1,
        )
        assert result.passed, result.details
        assert result.findings == []

    def test_a_new_weak_file_still_blocks(self, tmp_path: Path) -> None:
        # The other direction of the same rule: an ADDED file with no
        # falsifiable assertion is exactly what the floor is for.
        _repo(tmp_path)
        (tmp_path / "tests" / "test_new.py").write_text(
            "def test_a():\n    assert build() is not None\n"
        )
        for args in (["add", "-A"], ["commit", "-m", "add a weak file"]):
            subprocess.run(
                ["git", *args],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            )
        result = check_test_adequacy(
            tmp_path,
            "main",
            AdequacyConfig(enabled=True),
            autonomy_level=1,
        )
        assert not result.passed
        assert "adequacy_weak_oracle" in {f.category for f in result.findings}

    def test_deleting_a_whole_test_file_is_caught(self, tmp_path: Path) -> None:
        _repo(tmp_path)
        (tmp_path / "tests" / "test_core.py").unlink()
        for args in (["add", "-A"], ["commit", "-m", "delete the file"]):
            subprocess.run(
                ["git", *args],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            )
        result = check_test_adequacy(
            tmp_path,
            "main",
            AdequacyConfig(enabled=True),
            autonomy_level=1,
        )
        assert not result.passed
        categories = {f.category for f in result.findings}
        assert "adequacy_test_deleted" in categories
        assert "adequacy_assertion_removed" in categories

    def test_unreadable_diff_fails_closed(self, tmp_path: Path) -> None:
        # No git repo: the check must not report adequacy as satisfied.
        result = check_test_adequacy(
            tmp_path,
            "main",
            AdequacyConfig(enabled=True),
        )
        assert not result.passed
        assert "infrastructure error" in result.message
        assert any(f.is_infrastructure_error for f in result.findings)
