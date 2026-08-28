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
            "import pytest\ndef test_a():\n    with pytest.raises(ValueError):\n        parse('')\n"
        )
        assert lint_test_source("t/test_x.py", source).strength is (OracleStrength.STRONG)

    def test_no_assertion_at_all(self) -> None:
        report = lint_test_source("t/test_x.py", "def test_a():\n    run()\n")
        assert report.strength is OracleStrength.NONE
        assert report.without_assertions == ["test_a"]

    def test_boolop_takes_the_strongest_operand(self) -> None:
        # `assert x is not None and x == 3` can still fail on the value.
        source = "def test_a():\n    assert r is not None and r == 3\n"
        assert lint_test_source("t/test_x.py", source).strength is (OracleStrength.STRONG)

    def test_one_strong_test_carries_the_file(self) -> None:
        source = (
            "def test_weak():\n    assert r is not None\ndef test_strong():\n    assert r == 3\n"
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
            "import pytest\n@pytest.mark.skip(reason='later')\ndef test_a():\n    assert r == 1\n"
        )
        assert lint_test_source("t/test_x.py", source).skipped == [
            ("test_a", "pytest.mark.skip"),
        ]

    # -- P1-b: truthiness is not an oracle, whatever it is wrapped in ----
    @pytest.mark.parametrize(
        "body",
        [
            # `bool(...)` is the same assertion as `assert result`.
            "assert bool(result)",
            # An `or` is satisfied by its WEAKEST operand: any non-None
            # wrong value short-circuits before the comparison.
            "assert result is not None or result == 3",
            "assert result or result == 3",
            # A bare call is checked for truthiness, not for a value.
            "assert compute()",
            "assert obj.is_ready()",
            "assert not ready()",
        ],
    )
    def test_truthiness_is_weak(self, body: str) -> None:
        report = lint_test_source("t/test_x.py", f"def test_a():\n    {body}\n")
        assert report.strength is OracleStrength.WEAK, body

    @pytest.mark.parametrize(
        "body",
        [
            # Property oracles: the comparison lives inside the call.
            "assert all(x > 0 for x in items)",
            "assert any(v == 3 for v in vs)",
            "assert all(len(r) == 2 for r in rows)",
            # A comparison anywhere at the top level.
            "assert 'x' in render()",
            "assert sorted(xs) == [1, 2]",
            "assert compute() != 0",
            # `and` still takes its strongest operand.
            "assert r is not None and r == 3",
            # A comparison inside a call argument states an expectation.
            "assert bool(result == 3)",
            # `not` inverts nothing about strength.
            "assert not (a == b)",
        ],
    )
    def test_stated_expectations_stay_strong(self, body: str) -> None:
        report = lint_test_source("t/test_x.py", f"def test_a():\n    {body}\n")
        assert report.strength is OracleStrength.STRONG, body

    # -- P2-c: unittest and mock assertion methods are oracles ----------
    @pytest.mark.parametrize(
        "call",
        [
            "self.assertEqual(compute(), 3)",
            "self.assertIn('x', render())",
            "self.assertRaises(ValueError, parse, '')",
            "self.assertDictEqual(result, {'a': 1})",
            "m.assert_called_once_with(3)",
            "m.assert_called_with(3)",
            "m.assert_not_called()",
            "m.assert_has_calls([call(1)])",
            # An expectation stated inside a weak-family method upgrades it.
            "self.assertTrue(compute() == 3)",
        ],
    )
    def test_value_pinning_methods_are_strong(self, call: str) -> None:
        report = lint_test_source(
            "t/test_x.py",
            f"def test_a(self, m):\n    {call}\n",
        )
        assert report.strength is OracleStrength.STRONG, call
        assert report.without_assertions == []

    @pytest.mark.parametrize(
        "call",
        [
            # Satisfied by any truthy value, or any call with any args.
            "self.assertTrue(compute())",
            "self.assertIsNotNone(compute())",
            "self.assertIsInstance(compute(), dict)",
            "m.assert_called()",
            "m.assert_called_once()",
        ],
    )
    def test_presence_only_methods_are_weak(self, call: str) -> None:
        report = lint_test_source(
            "t/test_x.py",
            f"def test_a(self, m):\n    {call}\n",
        )
        assert report.strength is OracleStrength.WEAK, call

    def test_unittest_class_file_is_not_assertionless(self) -> None:
        # The worst false positive available: a whole TestCase file has no
        # bare `assert` statement and used to read as "asserts nothing".
        source = (
            "import unittest\n"
            "class TestCore(unittest.TestCase):\n"
            "    def test_adds(self):\n"
            "        self.assertEqual(add(2, 2), 4)\n"
        )
        report = lint_test_source("t/test_x.py", source)
        assert report.without_assertions == []
        assert report.strength is OracleStrength.STRONG

    # -- P2-e: skips that are not decorators ----------------------------
    def test_unconditional_body_skip_is_collected(self) -> None:
        source = "import pytest\ndef test_a():\n    pytest.skip('disabled')\n    assert r == 1\n"
        assert lint_test_source("t/test_x.py", source).skipped == [
            ("test_a", "pytest.skip"),
        ]

    def test_guarded_body_skip_is_not_a_finding(self) -> None:
        # A platform guard is not a disabled test, and flagging every one
        # would bury the case that matters.
        source = (
            "import pytest\n"
            "def test_a():\n"
            "    if sys.platform == 'win32':\n        pytest.skip('posix')\n"
            "    assert r == 1\n"
        )
        assert lint_test_source("t/test_x.py", source).skipped == []

    def test_module_level_pytestmark_is_collected(self) -> None:
        source = (
            "import pytest\n"
            "pytestmark = pytest.mark.skipif(WINDOWS, reason='posix only')\n"
            "def test_a():\n    assert r == 1\n"
        )
        assert lint_test_source("t/test_x.py", source).skipped == [
            ("<module>", "pytest.mark.skipif"),
        ]

    def test_a_test_named_after_skipping_is_not_a_skip(self) -> None:
        source = "def test_skip_behaviour():\n    assert runner.skip_count == 1\n"
        assert lint_test_source("t/test_x.py", source).skipped == []


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
        diff = "--- a/tests/t.py\n+++ b/tests/t.py\n+@pytest.mark.xfail(reason='flaky')\n"
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

    # -- P1-a: a deleted FILE is the loudest deletion there is ----------
    def test_whole_file_deletion_reports_every_test(self, tmp_path: Path) -> None:
        # Against a REAL git diff, because the bug was in reading git's
        # `+++ /dev/null` header, not in the counting.
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
        diff = subprocess.run(
            ["git", "diff", "main...HEAD"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "+++ /dev/null" in diff, "fixture must be a real deletion"
        result = analyze_test_diff(diff)
        assert sorted(result.deleted_tests()) == [
            ("tests/test_core.py", "test_adds"),
            ("tests/test_core.py", "test_subs"),
        ]
        assert result.net_assertion_loss() == {"tests/test_core.py": 2}

    def test_added_file_still_reads_from_the_new_side(self) -> None:
        diff = (
            "diff --git a/tests/t.py b/tests/t.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/tests/t.py\n"
            "+def test_a():\n+    assert f() == 1\n"
        )
        result = analyze_test_diff(diff)
        assert result.added_tests == {("tests/t.py", "test_a")}
        assert result.deleted_tests() == []

    # -- P2-e: the skip forms a decorator-only regex cannot see ---------
    @pytest.mark.parametrize(
        "line",
        [
            "    pytest.skip('disabled')",
            "pytestmark = pytest.mark.skip(reason='wip')",
            "pytestmark = [pytest.mark.xfail(reason='wip')]",
            "    pytest.param(1, marks=pytest.mark.xfail(reason='x')),",
            "    self.skipTest('disabled')",
            "@pytest.mark.skip(reason='later')",
            "@unittest.skip('later')",
        ],
    )
    def test_added_skip_forms_are_detected(self, line: str) -> None:
        diff = f"--- a/tests/t.py\n+++ b/tests/t.py\n+{line}\n"
        assert analyze_test_diff(diff).added_skips, line

    @pytest.mark.parametrize(
        "line",
        [
            "    assert row.skip == 1",
            "def test_skip_behaviour():",
            "    result = runner.skip_count",
            "    parser.add_argument('--skip')",
        ],
    )
    def test_ordinary_lines_are_not_skips(self, line: str) -> None:
        diff = f"--- a/tests/t.py\n+++ b/tests/t.py\n+{line}\n"
        assert analyze_test_diff(diff).added_skips == [], line


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
    """``new_paths`` is a required keyword since the P2-d fix: the
    whole-file oracle floor is a rule about NEW test files, so every
    caller has to say which paths those are. These cases pass the file
    under test as new, which is the behaviour they always asserted.
    """

    def test_clean_change_has_no_findings(self) -> None:
        diff = "--- a/tests/t.py\n+++ b/tests/t.py\n+    assert f() == 1\n"
        sources = {"tests/t.py": "def test_a():\n    assert f() == 1\n"}
        assert (
            evaluate_layer0(
                diff,
                sources,
                AdequacyConfig(enabled=True),
                new_paths={"tests/t.py"},
            )
            == []
        )

    def test_weak_oracle_file_is_flagged(self) -> None:
        sources = {"tests/t.py": "def test_a():\n    assert f() is not None\n"}
        findings = evaluate_layer0(
            "",
            sources,
            AdequacyConfig(enabled=True),
            new_paths={"tests/t.py"},
        )
        assert [f.kind for f in findings] == [FindingKind.WEAK_ORACLE]

    def test_require_strong_oracle_can_be_disabled(self) -> None:
        sources = {"tests/t.py": "def test_a():\n    assert f() is not None\n"}
        config = AdequacyConfig(enabled=True, require_strong_oracle=False)
        assert (
            evaluate_layer0(
                "",
                sources,
                config,
                new_paths={"tests/t.py"},
            )
            == []
        )

    def test_assertionless_test_is_flagged(self) -> None:
        sources = {"tests/t.py": "def test_a():\n    run()\n"}
        kinds = {
            f.kind
            for f in evaluate_layer0(
                "",
                sources,
                AdequacyConfig(enabled=True),
                new_paths={"tests/t.py"},
            )
        }
        assert FindingKind.NO_ORACLE in kinds

    def test_unparseable_file_produces_no_finding(self) -> None:
        sources = {"tests/t.py": "def test_a(:\n"}
        assert (
            evaluate_layer0(
                "",
                sources,
                AdequacyConfig(enabled=True),
                new_paths={"tests/t.py"},
            )
            == []
        )

    # -- P2-d: the whole-file floor is a NEW-file rule -------------------
    def test_modified_file_is_not_held_to_the_oracle_floor(self) -> None:
        # Editing a legacy file whose tests predate the gate must not
        # block: the oracles it is being judged on are not this change's.
        sources = {"tests/t.py": "def test_a():\n    assert f() is not None\n"}
        diff = "--- a/tests/t.py\n+++ b/tests/t.py\n+    # tidy up\n"
        assert (
            evaluate_layer0(
                diff,
                sources,
                AdequacyConfig(enabled=True),
                new_paths=set(),
            )
            == []
        )

    def test_modified_file_still_gets_diff_discipline(self) -> None:
        diff = "--- a/tests/t.py\n+++ b/tests/t.py\n-def test_gone():\n-    assert f() == 1\n"
        sources = {"tests/t.py": "def test_a():\n    assert f() is not None\n"}
        kinds = [
            f.kind
            for f in evaluate_layer0(
                diff,
                sources,
                AdequacyConfig(enabled=True),
                new_paths=set(),
            )
        ]
        assert kinds == [
            FindingKind.TEST_DELETED,
            FindingKind.ASSERTION_REMOVED,
        ]

    def test_assertionless_test_added_to_a_modified_file_is_flagged(
        self,
    ) -> None:
        # The def is new even though the file is not, so it is fair game.
        diff = "--- a/tests/t.py\n+++ b/tests/t.py\n+def test_new():\n+    run()\n"
        sources = {
            "tests/t.py": ("def test_old():\n    build()\ndef test_new():\n    run()\n"),
        }
        findings = evaluate_layer0(
            diff,
            sources,
            AdequacyConfig(enabled=True),
            new_paths=set(),
        )
        assert [(f.kind, f.symbol) for f in findings] == [
            (FindingKind.NO_ORACLE, "test_new"),
        ]

    # -- P2-e: collected skips are emitted, not dropped ------------------
    def test_skipped_test_in_a_new_file_is_emitted(self) -> None:
        sources = {
            "tests/t.py": (
                "import pytest\n"
                "@pytest.mark.skip(reason='later')\n"
                "def test_a():\n    assert f() == 1\n"
            ),
        }
        findings = evaluate_layer0(
            "",
            sources,
            AdequacyConfig(enabled=True),
            new_paths={"tests/t.py"},
        )
        assert [(f.kind, f.symbol) for f in findings] == [
            (FindingKind.TEST_SKIPPED, "test_a"),
        ]

    def test_module_level_pytestmark_is_emitted(self) -> None:
        sources = {
            "tests/t.py": (
                "import pytest\n"
                "pytestmark = pytest.mark.skip(reason='wip')\n"
                "def test_a():\n    assert f() == 1\n"
            ),
        }
        findings = evaluate_layer0(
            "",
            sources,
            AdequacyConfig(enabled=True),
            new_paths={"tests/t.py"},
        )
        assert [f.kind for f in findings] == [FindingKind.TEST_SKIPPED]
        assert "every test in this file" in findings[0].detail

    def test_pre_existing_skip_in_a_modified_file_is_quiet(self) -> None:
        # Editing a file that already had a skipped test is not this
        # change disabling anything; the diff-level check covers what is.
        sources = {
            "tests/t.py": (
                "import pytest\n"
                "@pytest.mark.skip(reason='old')\n"
                "def test_a():\n    assert f() == 1\n"
            ),
        }
        assert (
            evaluate_layer0(
                "--- a/tests/t.py\n+++ b/tests/t.py\n+    # note\n",
                sources,
                AdequacyConfig(enabled=True),
                new_paths=set(),
            )
            == []
        )


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
