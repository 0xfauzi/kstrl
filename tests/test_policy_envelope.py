"""R8.1 policy envelope tests.

Covers a planted violation in every category (paths_deny, size caps,
deps_allow_new, secrets, enforcement-machinery halt, license gating), the
config load/env/hash surface, the manifest policy_hash round-trip,
license resolution (uv cache + PyPI, both injected), and a real-git
end-to-end through ``check_policy_envelope``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from kstrl import git, licensing
from kstrl.git import _normalize_numstat_path
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.policy import (
    DEFAULT_PATHS_DENY,
    ENFORCEMENT_MACHINERY_PATHS,
    PolicyConfig,
    PolicyConfigError,
    _glob_to_regex,
    _match_glob,
    classify_license,
    evaluate_policy,
    parse_added_lines,
    parse_new_dependencies,
)
from kstrl.verify import check_policy_envelope, run_mechanical_verification


# --------------------------------------------------------------------------
# Glob matcher
# --------------------------------------------------------------------------
class TestGlobMatcher:
    @pytest.mark.parametrize(
        "path,pattern,expected",
        [
            (".github/workflows/ci.yml", ".github/workflows/**", True),
            (".github/workflows/a/b.yml", ".github/workflows/**", True),
            ("key.pem", "**/*.pem", True),  # zero leading dirs
            ("a/b/key.pem", "**/*.pem", True),
            (".env", "**/.env*", True),
            ("cfg/.env.local", "**/.env*", True),
            ("kstrl.toml", "kstrl.toml", True),
            (".kstrl/queue/item", ".kstrl/**", True),
            ("src/main.py", "**/*.pem", False),
            ("src/main.py", "kstrl.toml", False),
            ("notenv/file", "**/.env*", False),
            ("a/b/c.py", "a/*/c.py", True),  # single-segment star
            ("a/x/y/c.py", "a/*/c.py", False),  # star does not cross '/'
        ],
    )
    def test_matches(self, path: str, pattern: str, expected: bool) -> None:
        assert bool(re.match(_glob_to_regex(pattern), path)) is expected

    def test_match_glob_returns_matching_pattern(self) -> None:
        assert _match_glob("a/b.pem", ["src/**", "**/*.pem"]) == "**/*.pem"
        assert _match_glob("src/ok.py", ["**/*.pem"]) is None


# --------------------------------------------------------------------------
# Diff parsing helpers
# --------------------------------------------------------------------------
class TestDiffParsing:
    def test_parse_added_lines_tracks_path(self) -> None:
        diff = (
            "diff --git a/src/x.py b/src/x.py\n"
            "--- a/src/x.py\n"
            "+++ b/src/x.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+import os\n"
            "-removed line\n"
            " context\n"
            "diff --git a/y.txt b/y.txt\n"
            "--- a/y.txt\n"
            "+++ b/y.txt\n"
            "+hello\n"
        )
        assert parse_added_lines(diff) == [
            ("src/x.py", "import os"),
            ("y.txt", "hello"),
        ]

    def test_added_content_rendered_as_header_is_not_a_header(self) -> None:
        # An added line whose content is '++ x' renders as the diff line
        # '+++ x'. Without a preceding '--- ' it is content, not a header,
        # so it must stay attributed to the real file.
        diff = (
            "--- a/notes.md\n+++ b/notes.md\n@@ -0,0 +1,2 @@\n+++ still notes content\n+real line\n"
        )
        parsed = parse_added_lines(diff)
        assert ("notes.md", "++ still notes content") in parsed
        assert ("notes.md", "real line") in parsed
        # No path was ever set to the bogus header target.
        assert all(path == "notes.md" for path, _ in parsed)

    def test_parse_added_lines_ignores_dev_null_target(self) -> None:
        diff = "--- a/gone.txt\n+++ /dev/null\n+orphan\n"
        assert parse_added_lines(diff) == []

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("src/a.py", "src/a.py"),
            ("old.py => new.py", "new.py"),
            ("foo/{old => new}/bar.py", "foo/new/bar.py"),
            ("{old => new}/bar.py", "new/bar.py"),
        ],
    )
    def test_normalize_numstat_path(self, raw: str, expected: str) -> None:
        assert _normalize_numstat_path(raw) == expected


# --------------------------------------------------------------------------
# evaluate_policy: one planted violation per category
# --------------------------------------------------------------------------
class TestEvaluatePolicy:
    def test_clean_change_passes(self) -> None:
        ev = evaluate_policy(
            ["src/ok.py"],
            [(3, 1, "src/ok.py")],
            "--- a/src/ok.py\n+++ b/src/ok.py\n+x = 1\n",
            PolicyConfig(),
        )
        assert ev.ok and not ev.machinery_hit and ev.details == []

    def test_enforcement_machinery_halt(self) -> None:
        ev = evaluate_policy(
            [".github/workflows/ci.yml"],
            [(1, 0, ".github/workflows/ci.yml")],
            "",
            PolicyConfig(),
        )
        assert not ev.ok and ev.machinery_hit
        assert "HALT" in ev.details[0]

    def test_machinery_halt_is_non_overridable(self) -> None:
        # Even with paths_deny emptied, machinery edits still halt.
        cfg = PolicyConfig(paths_deny=[])
        ev = evaluate_policy(
            ["kstrl.toml"],
            [(1, 0, "kstrl.toml")],
            "",
            cfg,
        )
        assert not ev.ok and ev.machinery_hit

    def test_paths_deny_violation(self) -> None:
        ev = evaluate_policy(
            ["secrets/key.pem"],
            [(1, 0, "secrets/key.pem")],
            "",
            PolicyConfig(),
        )
        assert not ev.ok and not ev.machinery_hit
        assert any("Denied paths" in d for d in ev.details)

    def test_max_files_changed(self) -> None:
        files = [f"src/f{i}.py" for i in range(5)]
        numstat = [(1, 0, f) for f in files]
        cfg = PolicyConfig(max_files_changed=3)
        ev = evaluate_policy(files, numstat, "", cfg)
        assert not ev.ok
        assert any("Too many files" in d for d in ev.details)

    def test_max_lines_changed_excludes_lockfiles(self) -> None:
        # 2000 lockfile lines are excluded; 5 real lines are under the cap.
        numstat = [(2000, 0, "uv.lock"), (5, 0, "src/a.py")]
        ev = evaluate_policy(
            ["uv.lock", "src/a.py"],
            numstat,
            "",
            PolicyConfig(),
        )
        assert ev.ok, ev.details

    def test_max_lines_changed_violation(self) -> None:
        ev = evaluate_policy(
            ["src/big.py"],
            [(2000, 0, "src/big.py")],
            "",
            PolicyConfig(),
        )
        assert not ev.ok
        assert any("Too many lines" in d for d in ev.details)

    def test_negative_cap_disables(self) -> None:
        cfg = PolicyConfig(max_files_changed=-1, max_lines_changed=-1)
        files = [f"src/f{i}.py" for i in range(100)]
        numstat = [(999, 0, f) for f in files]
        ev = evaluate_policy(files, numstat, "", cfg)
        assert ev.ok

    def test_new_dependency_blocked(self) -> None:
        diff = (
            "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n"
            '+[[package]]\n+name = "requests"\n+version = "2.0"\n'
        )
        ev = evaluate_policy(["uv.lock"], [(3, 0, "uv.lock")], diff, PolicyConfig())
        assert not ev.ok
        assert any("New dependencies" in d and "requests" in d for d in ev.details)

    def test_new_dependency_allowed_when_enabled(self) -> None:
        diff = '--- a/uv.lock\n+++ b/uv.lock\n+name = "requests"\n'
        cfg = PolicyConfig(deps_allow_new=True)
        ev = evaluate_policy(["uv.lock"], [(1, 0, "uv.lock")], diff, cfg)
        assert ev.ok

    def test_inline_uvlock_dep_ref_is_not_a_new_package(self) -> None:
        # Indented `{ name = "x" }` inside a dependencies array must not
        # count as a new top-level package.
        diff = '--- a/uv.lock\n+++ b/uv.lock\n+    { name = "existing" },\n'
        ev = evaluate_policy(["uv.lock"], [(1, 0, "uv.lock")], diff, PolicyConfig())
        assert ev.ok, ev.details

    def test_secret_in_added_line_any_file(self) -> None:
        diff = '--- a/config.yaml\n+++ b/config.yaml\n+token = "AKIAABCDEFGHIJKLMNOP"\n'
        ev = evaluate_policy(
            ["config.yaml"],
            [(1, 0, "config.yaml")],
            diff,
            PolicyConfig(),
        )
        assert not ev.ok
        assert any("secrets" in d and "config.yaml" in d for d in ev.details)

    def test_bad_secret_regex_raises(self) -> None:
        cfg = PolicyConfig(secret_patterns=["(unclosed"])
        with pytest.raises(PolicyConfigError):
            evaluate_policy(
                ["a.py"],
                [(1, 0, "a.py")],
                "+++ b/a.py\n+x\n",
                cfg,
            )


# --------------------------------------------------------------------------
# PolicyConfig load / env / hash
# --------------------------------------------------------------------------
class TestPolicyConfig:
    def test_defaults(self) -> None:
        cfg = PolicyConfig()
        assert cfg.enabled is False
        assert cfg.deps_allow_new is False
        assert cfg.max_files_changed == 40
        assert list(cfg.paths_deny) == list(DEFAULT_PATHS_DENY)

    def test_load_reads_policy_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[policy]\n"
            "enabled = true\n"
            "max_files_changed = 7\n"
            "deps_allow_new = true\n"
            'paths_deny = ["dist/**"]\n'
        )
        cfg = PolicyConfig.load(tmp_path)
        assert cfg.enabled is True
        assert cfg.max_files_changed == 7
        assert cfg.deps_allow_new is True
        assert cfg.paths_deny == ["dist/**"]

    def test_env_overrides_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[policy]\nenabled = false\nmax_files_changed = 7\n")
        monkeypatch.setenv("KSTRL_POLICY_ENABLED", "1")
        monkeypatch.setenv("KSTRL_POLICY_MAX_FILES", "99")
        cfg = PolicyConfig.load(tmp_path)
        assert cfg.enabled is True
        assert cfg.max_files_changed == 99

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_POLICY_ENABLED", "1")
        monkeypatch.setenv("KSTRL_POLICY_DEPS_ALLOW_NEW", "1")
        cfg = PolicyConfig.from_env()
        assert cfg.enabled is True and cfg.deps_allow_new is True

    def test_envelope_hash_deterministic_and_sensitive(self) -> None:
        h1 = PolicyConfig().envelope_hash()
        assert h1 == PolicyConfig().envelope_hash()
        assert len(h1) == 64
        assert h1 != PolicyConfig(max_files_changed=41).envelope_hash()
        assert h1 != PolicyConfig(paths_deny=["a"]).envelope_hash()

    def test_machinery_paths_frozen_constant(self) -> None:
        # kstrl.toml and CI workflows must be in the hardcoded set.
        assert "kstrl.toml" in ENFORCEMENT_MACHINERY_PATHS
        assert ".github/workflows/**" in ENFORCEMENT_MACHINERY_PATHS


# --------------------------------------------------------------------------
# Manifest policy_hash round-trip
# --------------------------------------------------------------------------
class TestManifestPolicyHash:
    def _manifest(self) -> Manifest:
        return Manifest(
            version="1",
            spec_file="s",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    id="main",
                    title="t",
                    description="d",
                    dependencies=[],
                    prd_path="prd.json",
                    branch_name="kstrl/x",
                    status=ComponentStatus.PENDING.value,
                )
            ],
            policy_hash="deadbeef",
        )

    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        self._manifest().save(path)
        loaded = Manifest.load(path)
        assert loaded.policy_hash == "deadbeef"

    def test_default_empty_and_loadable(self, tmp_path: Path) -> None:
        # A manifest without policyHash (pre-R8.1) still loads.
        path = tmp_path / "manifest.json"
        m = self._manifest()
        m.policy_hash = ""
        m.save(path)
        data = path.read_text()
        assert '"policyHash": ""' in data
        assert Manifest.load(path).policy_hash == ""

    def test_validate_rejects_non_string(self) -> None:
        errors = Manifest.validate_schema(
            {
                "version": "1",
                "specFile": "s",
                "projectName": "p",
                "baseBranch": "main",
                "singlePr": False,
                "components": [],
                "policyHash": 123,
            }
        )
        assert any("policyHash" in e for e in errors)


# --------------------------------------------------------------------------
# check_policy_envelope (verify.py) - patched git
# --------------------------------------------------------------------------
class TestCheckPolicyEnvelope:
    def test_passes_when_clean(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            git,
            "get_diff_content",
            lambda *a, **k: "--- a/src/a.py\n+++ b/src/a.py\n+x=1\n",
        )
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["src/a.py"])
        monkeypatch.setattr(git, "get_diff_numstat", lambda *a, **k: [(1, 0, "src/a.py")])
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert res.passed and res.name == "policy_envelope"

    def test_fails_on_violation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(git, "get_diff_content", lambda *a, **k: "")
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["a/b.pem"])
        monkeypatch.setattr(git, "get_diff_numstat", lambda *a, **k: [(1, 0, "a/b.pem")])
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert not res.passed
        assert any("Denied paths" in d for d in res.details)

    def test_fails_closed_on_git_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(*a: object, **k: object) -> str:
            raise git.GitDiffError("bad ref")

        monkeypatch.setattr(git, "get_diff_content", _raise)
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert not res.passed
        assert "infrastructure error" in res.message

    def test_fails_closed_on_bad_regex(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(git, "get_diff_content", lambda *a, **k: "+++ b/a.py\n+x\n")
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["a.py"])
        monkeypatch.setattr(git, "get_diff_numstat", lambda *a, **k: [(1, 0, "a.py")])
        cfg = PolicyConfig(enabled=True, secret_patterns=["(bad"])
        res = check_policy_envelope(tmp_path, "main", cfg)
        assert not res.passed and "misconfigured" in res.message


# --------------------------------------------------------------------------
# run_mechanical_verification gating
# --------------------------------------------------------------------------
class TestRunMechanicalVerificationGating:
    def _prd(self, tmp_path: Path) -> Path:
        prd = tmp_path / "prd.json"
        prd.write_text('{"stories": [{"id": "S1", "description": "d"}]}')
        return prd

    def _stub_git(self, monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: names)
        monkeypatch.setattr(git, "get_diff_content", lambda *a, **k: "")
        monkeypatch.setattr(git, "get_diff_numstat", lambda *a, **k: [(1, 0, n) for n in names])

    def _config(self):  # type: ignore[no-untyped-def]
        from kstrl.verify import VerifyConfig

        # Disable the LLM/subprocess checks that need a real project.
        return VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
        )

    def test_disabled_policy_not_appended(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._stub_git(monkeypatch, ["kstrl.toml"])
        result = run_mechanical_verification(
            tmp_path,
            self._prd(tmp_path),
            "main",
            None,
            self._config(),
            policy_config=PolicyConfig(enabled=False),
        )
        assert "policy_envelope" not in {c.name for c in result.checks}

    def test_enabled_policy_appended_and_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._stub_git(monkeypatch, ["kstrl.toml"])  # machinery halt
        result = run_mechanical_verification(
            tmp_path,
            self._prd(tmp_path),
            "main",
            None,
            self._config(),
            policy_config=PolicyConfig(enabled=True),
        )
        policy = [c for c in result.checks if c.name == "policy_envelope"]
        assert policy and not policy[0].passed
        assert not result.passed


# --------------------------------------------------------------------------
# Real-git end-to-end
# --------------------------------------------------------------------------
def _git_cmd(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git_cmd(["init"], root)
    _git_cmd(["symbolic-ref", "HEAD", "refs/heads/main"], root)
    _git_cmd(["config", "user.email", "t@example.com"], root)
    _git_cmd(["config", "user.name", "tester"], root)
    (root / "README.md").write_text("base\n")
    _git_cmd(["add", "."], root)
    _git_cmd(["commit", "-m", "base"], root)
    _git_cmd(["checkout", "-b", "feature"], root)


class TestEndToEndRealGit:
    def test_clean_feature_passes(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n")
        _git_cmd(["add", "."], tmp_path)
        _git_cmd(["commit", "-m", "feat"], tmp_path)
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert res.passed, res.details

    def test_denied_pem_file_fails(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "server.pem").write_text("cert\n")
        _git_cmd(["add", "."], tmp_path)
        _git_cmd(["commit", "-m", "add cert"], tmp_path)
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert not res.passed
        assert any("Denied paths" in d for d in res.details)

    def test_machinery_edit_halts(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("name: ci\n")
        _git_cmd(["add", "."], tmp_path)
        _git_cmd(["commit", "-m", "ci"], tmp_path)
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert not res.passed
        assert any("HALT" in d for d in res.details)


# --------------------------------------------------------------------------
# License classification (pure)
# --------------------------------------------------------------------------
_ALLOW = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC"]
_DENY = ["GPL", "AGPL", "SSPL", "Commons-Clause"]


class TestClassifyLicense:
    @pytest.mark.parametrize(
        "license_str,expected",
        [
            ("MIT", "allowed"),
            ("mit", "allowed"),  # case-insensitive
            ("Apache-2.0 OR BSD-3-Clause", "allowed"),  # compound, all allowed
            ("GPL-3.0-only", "denied"),
            ("AGPL-3.0-or-later", "denied"),
            ("LGPL-3.0-only", "denied"),  # 'GPL' substring wins
            ("GPL-2.0 WITH Classpath-exception-2.0", "denied"),
            ("MPL-2.0", "unknown"),  # neither allowed nor denied
            ("Apache-2.0 OR Proprietary", "unknown"),  # one atom not allowed
            (None, "unknown"),  # unresolved
            ("", "unknown"),
        ],
    )
    def test_classify(self, license_str: str | None, expected: str) -> None:
        assert classify_license(license_str, _ALLOW, _DENY) == expected


class TestParseNewDependencies:
    def test_pairs_name_and_version(self) -> None:
        diff = (
            "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n"
            '+[[package]]\n+name = "requests"\n+version = "2.32.0"\n'
            '+[[package]]\n+name = "anyio"\n+version = "4.14.2"\n'
            '+    { name = "inline-ref" },\n'
        )
        assert parse_new_dependencies(parse_added_lines(diff)) == [
            ("requests", "2.32.0"),
            ("anyio", "4.14.2"),
        ]

    def test_version_bump_only_is_not_new(self) -> None:
        # Only a version line added (name line is unchanged context).
        diff = '--- a/uv.lock\n+++ b/uv.lock\n+version = "9.9.9"\n'
        assert parse_new_dependencies(parse_added_lines(diff)) == []


# --------------------------------------------------------------------------
# License resolution (kstrl.licensing) - uv cache + PyPI, both injectable
# --------------------------------------------------------------------------
class TestLicenseResolution:
    def test_metadata_text_expression_wins(self) -> None:
        text = (
            "Name: foo\nVersion: 1.0\n"
            "License-Expression: Apache-2.0 OR BSD-3-Clause\n"
            "Classifier: License :: OSI Approved :: MIT License\n\nbody"
        )
        assert licensing.license_from_metadata_text(text) == "Apache-2.0 OR BSD-3-Clause"

    def test_metadata_text_classifier_mapping(self) -> None:
        text = "Classifier: License :: OSI Approved :: BSD License\n\n"
        assert licensing.license_from_metadata_text(text) == "BSD-3-Clause"

    def test_metadata_text_dual_classifiers_join_or(self) -> None:
        text = (
            "Classifier: License :: OSI Approved :: Apache Software License\n"
            "Classifier: License :: OSI Approved :: BSD License\n\n"
        )
        assert licensing.license_from_metadata_text(text) == "Apache-2.0 OR BSD-3-Clause"

    def test_metadata_text_short_license_field(self) -> None:
        assert licensing.license_from_metadata_text("License: MPL-2.0\n\n") == "MPL-2.0"

    def test_metadata_text_multiline_license_field_ignored(self) -> None:
        text = "License: Copyright...\n full license text across\n many lines\n\n"
        assert licensing.license_from_metadata_text(text) is None

    def test_resolve_from_uv_cache(self, tmp_path: Path) -> None:
        d = tmp_path / "cache" / "archive-v0" / "h" / "foo-1.2.3.dist-info"
        d.mkdir(parents=True)
        (d / "METADATA").write_text("Name: foo\nVersion: 1.2.3\nLicense-Expression: MIT\n\nbody")
        cache = tmp_path / "cache"
        assert licensing.resolve_from_uv_cache("foo", "1.2.3", cache) == "MIT"
        assert licensing.resolve_from_uv_cache("foo", "9.9.9", cache) is None
        assert licensing.resolve_from_uv_cache("foo", "1.2.3", None) is None

    def test_uv_cache_name_variant(self, tmp_path: Path) -> None:
        # uv.lock name "my-pkg" but dist-info dir uses "my_pkg".
        d = tmp_path / "c" / "my_pkg-1.0.dist-info"
        d.mkdir(parents=True)
        (d / "METADATA").write_text("License-Expression: Apache-2.0\n\n")
        assert licensing.resolve_from_uv_cache("my-pkg", "1.0", tmp_path / "c") == "Apache-2.0"

    def test_resolve_from_pypi(self) -> None:
        import json

        payload = json.dumps({"info": {"license_expression": "BSD-3-Clause"}}).encode()
        got = licensing.resolve_from_pypi("x", "1", http_get=lambda url, to: payload)
        assert got == "BSD-3-Clause"

    def test_pypi_network_error_returns_none(self) -> None:
        def boom(url: str, timeout: float) -> bytes:
            raise OSError("no network")

        assert licensing.resolve_from_pypi("x", "1", http_get=boom) is None

    def test_resolve_license_prefers_cache(self, tmp_path: Path) -> None:
        d = tmp_path / "cache" / "foo-1.0.dist-info"
        d.mkdir(parents=True)
        (d / "METADATA").write_text("License-Expression: MIT\n\n")

        def unexpected(url: str, timeout: float) -> bytes:  # pragma: no cover
            raise AssertionError("PyPI must not be called on a cache hit")

        got = licensing.resolve_license(
            "foo",
            "1.0",
            uv_cache=tmp_path / "cache",
            http_get=unexpected,
        )
        assert got == "MIT"

    def test_resolve_license_offline_miss_is_none(self, tmp_path: Path) -> None:
        got = licensing.resolve_license(
            "foo",
            "1.0",
            uv_cache=tmp_path,
            use_pypi=False,
        )
        assert got is None


# --------------------------------------------------------------------------
# License gate wired through check_policy_envelope
# --------------------------------------------------------------------------
def _uvlock_add_diff(name: str, version: str) -> str:
    return (
        "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n"
        f'+[[package]]\n+name = "{name}"\n+version = "{version}"\n'
    )


class TestCheckPolicyEnvelopeLicense:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        resolved: str | None,
        config: PolicyConfig,
    ) -> object:
        monkeypatch.setattr(
            git,
            "get_diff_content",
            lambda *a, **k: _uvlock_add_diff("newpkg", "1.0"),
        )
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["uv.lock"])
        monkeypatch.setattr(git, "get_diff_numstat", lambda *a, **k: [(3, 0, "uv.lock")])
        monkeypatch.setattr(licensing, "uv_cache_dir", lambda: None)
        monkeypatch.setattr(licensing, "resolve_license", lambda *a, **k: resolved)
        return check_policy_envelope(tmp_path, "main", config)

    def _cfg(self) -> PolicyConfig:
        # deps_allow_new=True isolates the license outcome from the
        # new-dependency block.
        return PolicyConfig(enabled=True, deps_allow_new=True)

    def test_allowed_license_passes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        res = self._run(monkeypatch, tmp_path, "MIT", self._cfg())
        assert res.passed  # type: ignore[attr-defined]

    def test_denied_license_blocks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        res = self._run(monkeypatch, tmp_path, "GPL-3.0-only", self._cfg())
        assert not res.passed  # type: ignore[attr-defined]
        assert any("denied license" in d for d in res.details)  # type: ignore[attr-defined]

    def test_unknown_resolved_license_blocks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        res = self._run(monkeypatch, tmp_path, "MPL-2.0", self._cfg())
        assert not res.passed  # type: ignore[attr-defined]
        assert any("not in license_allow" in d for d in res.details)  # type: ignore[attr-defined]

    def test_unresolved_license_blocks_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # PR #173 review: an unprovable license is not demonstrably inside
        # the envelope, so the explicit-allowlist posture blocks it. Opting
        # into advisory is covered in TestLicenseUnresolvedPosture.
        res = self._run(monkeypatch, tmp_path, None, self._cfg())
        assert not res.passed  # type: ignore[attr-defined]
        assert any(
            "could not be resolved" in d
            for d in res.details  # type: ignore[attr-defined]
        )

    def test_empty_allowlist_disables_gate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = PolicyConfig(enabled=True, deps_allow_new=True, license_allow=[])
        # Even a GPL dep passes when the license gate is unconfigured.
        res = self._run(monkeypatch, tmp_path, "GPL-3.0-only", cfg)
        assert res.passed  # type: ignore[attr-defined]


class TestPolicyConfigLicenseFields:
    def test_defaults_present(self) -> None:
        cfg = PolicyConfig()
        assert "MIT" in cfg.license_allow
        assert "GPL" in cfg.license_deny_partial

    def test_load_reads_license_lists(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[policy]\nlicense_allow = ["MIT", "MPL-2.0"]\nlicense_deny_partial = ["AGPL"]\n'
        )
        cfg = PolicyConfig.load(tmp_path)
        assert cfg.license_allow == ["MIT", "MPL-2.0"]
        assert cfg.license_deny_partial == ["AGPL"]

    def test_envelope_hash_covers_license(self) -> None:
        base = PolicyConfig().envelope_hash()
        assert base != PolicyConfig(license_allow=["MIT"]).envelope_hash()
        assert base != PolicyConfig(license_deny_partial=["GPL"]).envelope_hash()


# --------------------------------------------------------------------------
# Review regressions (PR #173)
# --------------------------------------------------------------------------
# 1. The non-overridable halt must cover verifier code, not just CI+config.
VERIFIER_CODE_PATHS = [
    "kstrl/verify.py",
    "kstrl/policy.py",
    "kstrl/licensing.py",
    "kstrl/guards.py",
    "kstrl/fixtures.py",
]


class TestEnforcementMachineryCoversVerifierCode:
    @pytest.mark.parametrize("path", VERIFIER_CODE_PATHS)
    def test_verifier_code_halts_with_empty_paths_deny(self, path: str) -> None:
        # The exact reviewer repro: paths_deny emptied must not permit an
        # agent to rewrite the code that enforces the envelope.
        ev = evaluate_policy(
            [path],
            [(1, 0, path)],
            "",
            PolicyConfig(paths_deny=[]),
        )
        assert not ev.ok, f"{path} did not block"
        assert ev.machinery_hit
        assert ev.violations[0].category == "enforcement_machinery"
        assert ev.violations[0].severity == "critical"

    @pytest.mark.parametrize("path", VERIFIER_CODE_PATHS)
    def test_nested_checkout_also_halts(self, path: str) -> None:
        nested = f"vendor/pkg/{path}"
        ev = evaluate_policy(
            [nested],
            [(1, 0, nested)],
            "",
            PolicyConfig(paths_deny=[]),
        )
        assert not ev.ok and ev.machinery_hit

    def test_ci_and_config_still_halt(self) -> None:
        for path in (".github/workflows/ci.yml", "kstrl.toml", "ralph.toml"):
            ev = evaluate_policy(
                [path],
                [(1, 0, path)],
                "",
                PolicyConfig(paths_deny=[]),
            )
            assert not ev.ok and ev.machinery_hit, path

    def test_enforcement_paths_extra_is_additive(self) -> None:
        cfg = PolicyConfig(paths_deny=[], enforcement_paths_extra=["ci/**"])
        ev = evaluate_policy(["ci/gate.sh"], [(1, 0, "ci/gate.sh")], "", cfg)
        assert not ev.ok and ev.machinery_hit

    def test_extras_cannot_shrink_hardcoded_set(self) -> None:
        # Supplying extras must not displace the built-in protections.
        cfg = PolicyConfig(paths_deny=[], enforcement_paths_extra=["ci/**"])
        ev = evaluate_policy(
            ["kstrl/verify.py"],
            [(1, 0, "kstrl/verify.py")],
            "",
            cfg,
        )
        assert not ev.ok and ev.machinery_hit

    def test_ordinary_source_file_does_not_halt(self) -> None:
        # Guard against over-broad matching: normal code is unaffected.
        for path in ("kstrl/pipeline.py", "src/app/verify.py", "verify.py"):
            ev = evaluate_policy(
                [path],
                [(1, 0, path)],
                "",
                PolicyConfig(paths_deny=[]),
            )
            assert ev.ok, f"{path} should not halt"


# 2. Metadata reads must fail closed, not evaluate as 0 files / 0 lines.
class TestGitMetadataFailsClosed:
    def test_get_diff_names_strict_raises_on_nonzero_exit(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(git.GitDiffError):
            # tmp_path is not a git repo -> nonzero exit.
            git.get_diff_names("main", tmp_path, strict=True)

    def test_get_diff_numstat_strict_raises_on_nonzero_exit(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(git.GitDiffError):
            git.get_diff_numstat("main", tmp_path, strict=True)

    def test_lenient_default_preserved_for_existing_callers(
        self,
        tmp_path: Path,
    ) -> None:
        # check_diff_scope and friends rely on the [] contract.
        assert git.get_diff_names("main", tmp_path) == []
        assert git.get_diff_numstat("main", tmp_path) == []

    @pytest.mark.parametrize("helper", ["get_diff_names", "get_diff_numstat"])
    def test_strict_raises_on_timeout(
        self,
        helper: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        def _timeout(*a: object, **k: object) -> object:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

        monkeypatch.setattr(git, "resolve_base_ref", lambda *a, **k: "main")
        monkeypatch.setattr(git.subprocess, "run", _timeout)
        with pytest.raises(git.GitDiffError):
            getattr(git, helper)("main", tmp_path, strict=True)

    def test_policy_check_fails_closed_when_names_read_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Reviewer repro: a kstrl.toml diff passed as "0 files, 0 lines"
        # when the metadata helpers returned empty.
        def _boom(*a: object, **k: object) -> list[str]:
            raise git.GitDiffError("simulated nonzero exit")

        monkeypatch.setattr(git, "get_diff_content", lambda *a, **k: "")
        monkeypatch.setattr(git, "get_diff_names", _boom)
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert not res.passed
        assert "infrastructure error" in res.message
        assert any(f.is_infrastructure_error for f in res.findings)

    def test_policy_check_fails_closed_when_numstat_read_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(*a: object, **k: object) -> list[object]:
            raise git.GitDiffError("simulated timeout")

        monkeypatch.setattr(git, "get_diff_content", lambda *a, **k: "")
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["a.py"])
        monkeypatch.setattr(git, "get_diff_numstat", _boom)
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert not res.passed
        assert "infrastructure error" in res.message


# 3. License posture: unresolved blocks by default; toggles are hashed.
class TestLicenseUnresolvedPosture:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        config: PolicyConfig,
    ) -> object:
        monkeypatch.setattr(
            git,
            "get_diff_content",
            lambda *a, **k: _uvlock_add_diff("newpkg", "1.0"),
        )
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["uv.lock"])
        monkeypatch.setattr(
            git,
            "get_diff_numstat",
            lambda *a, **k: [(3, 0, "uv.lock")],
        )
        monkeypatch.setattr(licensing, "uv_cache_dir", lambda: None)
        monkeypatch.setattr(licensing, "resolve_license", lambda *a, **k: None)
        return check_policy_envelope(tmp_path, "main", config)

    def test_unresolved_blocks_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = PolicyConfig(enabled=True, deps_allow_new=True)
        assert cfg.license_unresolved == "block"
        res = self._run(monkeypatch, tmp_path, cfg)
        assert not res.passed  # type: ignore[attr-defined]
        assert any(
            "could not be resolved" in d
            for d in res.details  # type: ignore[attr-defined]
        )

    def test_unresolved_advisory_when_opted_in(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = PolicyConfig(
            enabled=True,
            deps_allow_new=True,
            license_unresolved="advisory",
        )
        res = self._run(monkeypatch, tmp_path, cfg)
        assert res.passed  # type: ignore[attr-defined]
        assert any("advisory" in d for d in res.details)  # type: ignore[attr-defined]

    def test_offline_mode_names_the_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = PolicyConfig(
            enabled=True,
            deps_allow_new=True,
            license_use_network=False,
        )
        res = self._run(monkeypatch, tmp_path, cfg)
        assert not res.passed  # type: ignore[attr-defined]
        assert any(
            "network resolution disabled" in d
            for d in res.details  # type: ignore[attr-defined]
        )

    def test_network_toggle_changes_envelope_hash(self) -> None:
        # A run that skipped the network must not claim an unchanged hash.
        assert (
            PolicyConfig().envelope_hash()
            != PolicyConfig(license_use_network=False).envelope_hash()
        )

    def test_unresolved_posture_changes_envelope_hash(self) -> None:
        assert (
            PolicyConfig().envelope_hash()
            != PolicyConfig(license_unresolved="advisory").envelope_hash()
        )

    def test_invalid_unresolved_value_rejected(self) -> None:
        with pytest.raises(PolicyConfigError):
            PolicyConfig(license_unresolved="maybe")

    def test_env_toggles_land_in_config_and_hash(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_POLICY_LICENSE_NET", "0")
        monkeypatch.setenv("KSTRL_POLICY_LICENSE_UNRESOLVED", "advisory")
        cfg = PolicyConfig.load(tmp_path)
        assert cfg.license_use_network is False
        assert cfg.license_unresolved == "advisory"
        assert cfg.envelope_hash() != PolicyConfig().envelope_hash()

    def test_config_disables_network_resolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # license_use_network=False must reach resolve_license as use_pypi.
        seen: dict[str, object] = {}

        def _spy(name: str, version: str, **kwargs: object) -> str | None:
            seen.update(kwargs)
            return "MIT"

        monkeypatch.setattr(
            git,
            "get_diff_content",
            lambda *a, **k: _uvlock_add_diff("newpkg", "1.0"),
        )
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["uv.lock"])
        monkeypatch.setattr(
            git,
            "get_diff_numstat",
            lambda *a, **k: [(3, 0, "uv.lock")],
        )
        monkeypatch.setattr(licensing, "uv_cache_dir", lambda: None)
        monkeypatch.setattr(licensing, "resolve_license", _spy)
        check_policy_envelope(
            tmp_path,
            "main",
            PolicyConfig(
                enabled=True,
                deps_allow_new=True,
                license_use_network=False,
            ),
        )
        assert seen["use_pypi"] is False


# 4. Typed Findings for policy violations (#148 acceptance criterion).
class TestPolicyFindings:
    def _res(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        names: list[str],
        config: PolicyConfig,
    ) -> object:
        monkeypatch.setattr(git, "get_diff_content", lambda *a, **k: "")
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: names)
        monkeypatch.setattr(
            git,
            "get_diff_numstat",
            lambda *a, **k: [(1, 0, n) for n in names],
        )
        return check_policy_envelope(tmp_path, "main", config)

    def test_machinery_halt_emits_critical_finding(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        res = self._res(
            monkeypatch,
            tmp_path,
            ["kstrl/verify.py"],
            PolicyConfig(enabled=True),
        )
        findings = res.findings  # type: ignore[attr-defined]
        assert findings
        f = findings[0]
        assert f.phase == "policy"
        assert f.category == "policy_enforcement_machinery"
        assert f.severity == "critical"
        assert "policy" in f.tags

    def test_paths_deny_emits_high_finding(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        res = self._res(
            monkeypatch,
            tmp_path,
            ["a/b.pem"],
            PolicyConfig(enabled=True),
        )
        findings = res.findings  # type: ignore[attr-defined]
        assert [f.category for f in findings] == ["policy_paths_deny"]
        assert findings[0].severity == "high"
        assert findings[0].suggestion

    def test_clean_change_emits_no_findings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        res = self._res(
            monkeypatch,
            tmp_path,
            ["src/ok.py"],
            PolicyConfig(enabled=True),
        )
        assert res.passed  # type: ignore[attr-defined]
        assert res.findings == []  # type: ignore[attr-defined]

    def test_findings_are_not_infrastructure_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A real violation must not masquerade as "the check broke".
        res = self._res(
            monkeypatch,
            tmp_path,
            ["a/b.pem"],
            PolicyConfig(enabled=True),
        )
        assert not any(
            f.is_infrastructure_error
            for f in res.findings  # type: ignore[attr-defined]
        )

    def test_blocking_details_precede_advisories(self) -> None:
        # as_context() slices details[:10]; advisories must not crowd out
        # the blocking reason the retry prompt needs.
        from kstrl.policy import PolicyViolation

        blocking = PolicyViolation(category="paths_deny", explanation="BLOCK")
        advisory = PolicyViolation(
            category="license_unresolved",
            explanation="ADVISORY",
            severity="advisory",
        )
        assert blocking.blocking and not advisory.blocking
