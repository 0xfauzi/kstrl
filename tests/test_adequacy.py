"""R8.5 Layer 0 tests: test-diff discipline and oracle-signal linting.

The layer exists because agent-written tests cannot be assumed adequate
(80.2% of 86k agent-authored test patches carry weak or no oracle
signals, arXiv:2606.18168), so these tests are mostly about the two
failure modes that matter: a diff that WEAKENS the suite, and a new test
that asserts nothing falsifiable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kstrl.adequacy import (
    AdequacyConfig,
    FindingKind,
    OracleStrength,
    analyze_test_diff,
    evaluate_layer0,
    is_test_path,
    layer0_blocks,
    lint_test_source,
)
from kstrl.verify import check_test_adequacy


# --------------------------------------------------------------------------
# Oracle strength
# --------------------------------------------------------------------------
class TestOracleClassification:
    @pytest.mark.parametrize(
        "body,expected",
        [
            ("assert add(2, 2) == 4", OracleStrength.STRONG),
            ("assert result == {'a': 1}", OracleStrength.STRONG),
            ("assert 'x' in render()", OracleStrength.STRONG),
            ("assert compute() != 0", OracleStrength.STRONG),
            # Weak: passes for a plausible-looking WRONG answer.
            ("assert result is not None", OracleStrength.WEAK),
            ("assert result", OracleStrength.WEAK),
            ("assert True", OracleStrength.WEAK),
            ("assert isinstance(result, dict)", OracleStrength.WEAK),
            ("assert hasattr(obj, 'run')", OracleStrength.WEAK),
        ],
    )
    def test_single_assertion(self, body: str, expected: OracleStrength) -> None:
        report = lint_test_source("t/test_x.py", f"def test_a():\n    {body}\n")
        assert report.strength is expected

    def test_raises_context_is_strong(self) -> None:
        source = (
            "import pytest\n"
            "def test_a():\n"
            "    with pytest.raises(ValueError):\n"
            "        parse('')\n"
        )
        assert lint_test_source("t/test_x.py", source).strength is (
            OracleStrength.STRONG
        )

    def test_no_assertion_at_all(self) -> None:
        report = lint_test_source("t/test_x.py", "def test_a():\n    run()\n")
        assert report.strength is OracleStrength.NONE
        assert report.without_assertions == ["test_a"]

    def test_boolop_takes_the_strongest_operand(self) -> None:
        # `assert x is not None and x == 3` can still fail on the value.
        source = "def test_a():\n    assert r is not None and r == 3\n"
        assert lint_test_source("t/test_x.py", source).strength is (
            OracleStrength.STRONG
        )

    def test_one_strong_test_carries_the_file(self) -> None:
        source = (
            "def test_weak():\n    assert r is not None\n"
            "def test_strong():\n    assert r == 3\n"
        )
        report = lint_test_source("t/test_x.py", source)
        assert report.strength is OracleStrength.STRONG
        assert report.weak_only == ["test_weak"]

    def test_non_test_functions_are_ignored(self) -> None:
        source = "def helper():\n    assert True\ndef test_a():\n    assert r == 1\n"
        report = lint_test_source("t/test_x.py", source)
        assert report.tests == 1

    def test_syntax_error_fails_open(self) -> None:
        # The test runner reports this far better than a guess here would.
        report = lint_test_source("t/test_x.py", "def test_a(:\n")
        assert report.parse_error
        assert report.tests == 0

    def test_skip_markers_are_collected(self) -> None:
        source = (
            "import pytest\n"
            "@pytest.mark.skip(reason='later')\n"
            "def test_a():\n    assert r == 1\n"
        )
        assert lint_test_source("t/test_x.py", source).skipped == [
            ("test_a", "pytest.mark.skip"),
        ]


class TestIsTestPath:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("tests/test_a.py", True),
            ("test/test_a.py", True),
            ("src/test_helpers.py", True),
            ("src/helpers_test.py", True),
            ("kstrl/verify.py", False),
            ("src/latest.py", False),
        ],
    )
    def test_paths(self, path: str, expected: bool) -> None:
        assert is_test_path(path) is expected


# --------------------------------------------------------------------------
# Diff discipline
# --------------------------------------------------------------------------
class TestDiffDiscipline:
    def test_deleted_test_is_reported(self) -> None:
        diff = "--- a/tests/t.py\n+++ b/tests/t.py\n-def test_gone():\n-    assert x == 1\n"
        assert analyze_test_diff(diff).deleted_tests() == [("tests/t.py", "test_gone")]

    def test_modified_test_is_not_a_deletion(self) -> None:
        # Crying wolf on every edited test would train people to ignore it.
        diff = (
            "--- a/tests/t.py\n+++ b/tests/t.py\n"
            "-def test_a():\n-    assert f() == 1\n"
            "+def test_a():\n+    assert f() == 2\n"
        )
        assert analyze_test_diff(diff).deleted_tests() == []

    def test_added_skip_is_reported(self) -> None:
        diff = (
            "--- a/tests/t.py\n+++ b/tests/t.py\n"
            "+@pytest.mark.xfail(reason='flaky')\n"
        )
        skips = analyze_test_diff(diff).added_skips
        assert skips and "xfail" in skips[0][1]

    def test_net_assertion_loss_only_counts_a_decrease(self) -> None:
        diff = (
            "--- a/tests/t.py\n+++ b/tests/t.py\n"
            "-    assert a == 1\n-    assert b == 2\n+    assert a == 1\n"
        )
        assert analyze_test_diff(diff).net_assertion_loss() == {"tests/t.py": 1}

    def test_added_assertions_are_not_a_loss(self) -> None:
        diff = (
            "--- a/tests/t.py\n+++ b/tests/t.py\n"
            "-    assert a == 1\n+    assert a == 1\n+    assert b == 2\n"
        )
        assert analyze_test_diff(diff).net_assertion_loss() == {}

    def test_non_test_files_are_ignored(self) -> None:
        diff = "--- a/src/x.py\n+++ b/src/x.py\n-    assert cond\n-def test_thing():\n"
        result = analyze_test_diff(diff)
        assert result.removed_assertions == {}
        assert result.deleted_tests() == []


# --------------------------------------------------------------------------
# Config and level gating
# --------------------------------------------------------------------------
class TestConfigAndLevels:
    def test_disabled_by_default(self) -> None:
        assert AdequacyConfig().enabled is False
        assert AdequacyConfig().layer0 == "advisory"

    def test_invalid_layer0_rejected(self) -> None:
        with pytest.raises(ValueError, match="layer0"):
            AdequacyConfig(layer0="maybe")

    def test_load_reads_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[adequacy]\nenabled = true\nlayer0 = "block"\n'
        )
        config = AdequacyConfig.load(tmp_path)
        assert config.enabled is True and config.layer0 == "block"

    def test_env_overrides_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[adequacy]\nenabled = false\n")
        monkeypatch.setenv("KSTRL_ADEQUACY_ENABLED", "1")
        assert AdequacyConfig.load(tmp_path).enabled is True

    def test_ladder_off_stays_advisory(self) -> None:
        assert layer0_blocks(AdequacyConfig(enabled=True), 0) is False

    def test_explicit_block_wins_without_the_ladder(self) -> None:
        assert layer0_blocks(AdequacyConfig(enabled=True, layer0="block"), 0) is True

    @pytest.mark.parametrize("level", [1, 2, 3, 4])
    def test_ladder_makes_layer0_blocking_from_l1(self, level: int) -> None:
        # Autonomy may tighten a gate, never loosen one.
        assert layer0_blocks(AdequacyConfig(enabled=True), level) is True


# --------------------------------------------------------------------------
# evaluate_layer0
# --------------------------------------------------------------------------
class TestEvaluateLayer0:
    def test_clean_change_has_no_findings(self) -> None:
        diff = "--- a/tests/t.py\n+++ b/tests/t.py\n+    assert f() == 1\n"
        sources = {"tests/t.py": "def test_a():\n    assert f() == 1\n"}
        assert evaluate_layer0(diff, sources, AdequacyConfig(enabled=True)) == []

    def test_weak_oracle_file_is_flagged(self) -> None:
        sources = {"tests/t.py": "def test_a():\n    assert f() is not None\n"}
        findings = evaluate_layer0("", sources, AdequacyConfig(enabled=True))
        assert [f.kind for f in findings] == [FindingKind.WEAK_ORACLE]

    def test_require_strong_oracle_can_be_disabled(self) -> None:
        sources = {"tests/t.py": "def test_a():\n    assert f() is not None\n"}
        config = AdequacyConfig(enabled=True, require_strong_oracle=False)
        assert evaluate_layer0("", sources, config) == []

    def test_assertionless_test_is_flagged(self) -> None:
        sources = {"tests/t.py": "def test_a():\n    run()\n"}
        kinds = {
            f.kind
            for f in evaluate_layer0("", sources, AdequacyConfig(enabled=True))
        }
        assert FindingKind.NO_ORACLE in kinds

    def test_unparseable_file_produces_no_finding(self) -> None:
        sources = {"tests/t.py": "def test_a(:\n"}
        assert evaluate_layer0("", sources, AdequacyConfig(enabled=True)) == []


# --------------------------------------------------------------------------
# End to end through the verifier, against a real repo
# --------------------------------------------------------------------------
def _repo(root: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        )

    run("init")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "tester")
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
            ["git", *args], cwd=root, check=True, capture_output=True, text=True,
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
            tmp_path, "main", AdequacyConfig(enabled=True), autonomy_level=0,
        )
        assert result.passed, "advisory must not fail the check"
        assert result.findings
        assert all(f.severity == "advisory" for f in result.findings)
        categories = {f.category for f in result.findings}
        assert "adequacy_test_deleted" in categories     # test_subs left
        assert "adequacy_test_skipped" in categories     # xfail added
        assert "adequacy_no_oracle" in categories        # test_new asserts nothing

    def test_l1_blocks_on_the_same_diff(self, tmp_path: Path) -> None:
        _repo(tmp_path)
        _weaken(tmp_path)
        result = check_test_adequacy(
            tmp_path, "main", AdequacyConfig(enabled=True), autonomy_level=1,
        )
        assert not result.passed
        assert all(f.severity == "high" for f in result.findings)

    def test_clean_change_passes_with_no_findings(self, tmp_path: Path) -> None:
        _repo(tmp_path)
        subprocess.run(
            ["git", "checkout", "-b", "clean"], cwd=tmp_path,
            check=True, capture_output=True, text=True,
        )
        (tmp_path / "tests" / "test_more.py").write_text(
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        for args in (["add", "."], ["commit", "-m", "add a real test"]):
            subprocess.run(
                ["git", *args], cwd=tmp_path, check=True,
                capture_output=True, text=True,
            )
        result = check_test_adequacy(
            tmp_path, "main", AdequacyConfig(enabled=True), autonomy_level=1,
        )
        assert result.passed
        assert result.findings == []

    def test_unreadable_diff_fails_closed(self, tmp_path: Path) -> None:
        # No git repo: the check must not report adequacy as satisfied.
        result = check_test_adequacy(
            tmp_path, "main", AdequacyConfig(enabled=True),
        )
        assert not result.passed
        assert "infrastructure error" in result.message
        assert any(f.is_infrastructure_error for f in result.findings)
