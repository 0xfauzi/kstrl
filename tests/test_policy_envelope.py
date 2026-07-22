"""R8.1 policy envelope tests.

Covers a planted violation in every PR1 category (paths_deny, size caps,
deps_allow_new, secrets, enforcement-machinery halt), the config
load/env/hash surface, the manifest policy_hash round-trip, and a
real-git end-to-end through ``check_policy_envelope``. License gating is
a follow-up and is not exercised here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from kstrl import git
from kstrl.git import _normalize_numstat_path
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.policy import (
    DEFAULT_PATHS_DENY,
    ENFORCEMENT_MACHINERY_PATHS,
    PolicyConfig,
    PolicyConfigError,
    _glob_to_regex,
    _match_glob,
    evaluate_policy,
    parse_added_lines,
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
            ("key.pem", "**/*.pem", True),          # zero leading dirs
            ("a/b/key.pem", "**/*.pem", True),
            (".env", "**/.env*", True),
            ("cfg/.env.local", "**/.env*", True),
            ("kstrl.toml", "kstrl.toml", True),
            (".kstrl/queue/item", ".kstrl/**", True),
            ("src/main.py", "**/*.pem", False),
            ("src/main.py", "kstrl.toml", False),
            ("notenv/file", "**/.env*", False),
            ("a/b/c.py", "a/*/c.py", True),         # single-segment star
            ("a/x/y/c.py", "a/*/c.py", False),      # star does not cross '/'
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
            "--- a/notes.md\n"
            "+++ b/notes.md\n"
            "@@ -0,0 +1,2 @@\n"
            "+++ still notes content\n"
            "+real line\n"
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
            ["src/ok.py"], [(3, 1, "src/ok.py")],
            "--- a/src/ok.py\n+++ b/src/ok.py\n+x = 1\n", PolicyConfig(),
        )
        assert ev.ok and not ev.machinery_hit and ev.details == []

    def test_enforcement_machinery_halt(self) -> None:
        ev = evaluate_policy(
            [".github/workflows/ci.yml"],
            [(1, 0, ".github/workflows/ci.yml")], "", PolicyConfig(),
        )
        assert not ev.ok and ev.machinery_hit
        assert "HALT" in ev.details[0]

    def test_machinery_halt_is_non_overridable(self) -> None:
        # Even with paths_deny emptied, machinery edits still halt.
        cfg = PolicyConfig(paths_deny=[])
        ev = evaluate_policy(
            ["kstrl.toml"], [(1, 0, "kstrl.toml")], "", cfg,
        )
        assert not ev.ok and ev.machinery_hit

    def test_paths_deny_violation(self) -> None:
        ev = evaluate_policy(
            ["secrets/key.pem"], [(1, 0, "secrets/key.pem")], "", PolicyConfig(),
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
            ["uv.lock", "src/a.py"], numstat, "", PolicyConfig(),
        )
        assert ev.ok, ev.details

    def test_max_lines_changed_violation(self) -> None:
        ev = evaluate_policy(
            ["src/big.py"], [(2000, 0, "src/big.py")], "", PolicyConfig(),
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
        diff = "--- a/uv.lock\n+++ b/uv.lock\n+name = \"requests\"\n"
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
            ["config.yaml"], [(1, 0, "config.yaml")], diff, PolicyConfig(),
        )
        assert not ev.ok
        assert any("secrets" in d and "config.yaml" in d for d in ev.details)

    def test_bad_secret_regex_raises(self) -> None:
        cfg = PolicyConfig(secret_patterns=["(unclosed"])
        with pytest.raises(PolicyConfigError):
            evaluate_policy(
                ["a.py"], [(1, 0, "a.py")], "+++ b/a.py\n+x\n", cfg,
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
            version="1", spec_file="s", project_name="p",
            base_branch="main", single_pr=False,
            components=[Component(
                id="main", title="t", description="d", dependencies=[],
                prd_path="prd.json", branch_name="kstrl/x",
                status=ComponentStatus.PENDING.value,
            )],
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
        errors = Manifest.validate_schema({
            "version": "1", "specFile": "s", "projectName": "p",
            "baseBranch": "main", "singlePr": False, "components": [],
            "policyHash": 123,
        })
        assert any("policyHash" in e for e in errors)


# --------------------------------------------------------------------------
# check_policy_envelope (verify.py) - patched git
# --------------------------------------------------------------------------
class TestCheckPolicyEnvelope:
    def test_passes_when_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            git, "get_diff_content",
            lambda *a, **k: "--- a/src/a.py\n+++ b/src/a.py\n+x=1\n",
        )
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["src/a.py"])
        monkeypatch.setattr(git, "get_diff_numstat", lambda *a, **k: [(1, 0, "src/a.py")])
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert res.passed and res.name == "policy_envelope"

    def test_fails_on_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(git, "get_diff_content", lambda *a, **k: "")
        monkeypatch.setattr(git, "get_diff_names", lambda *a, **k: ["a/b.pem"])
        monkeypatch.setattr(git, "get_diff_numstat", lambda *a, **k: [(1, 0, "a/b.pem")])
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert not res.passed
        assert any("Denied paths" in d for d in res.details)

    def test_fails_closed_on_git_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(*a: object, **k: object) -> str:
            raise git.GitDiffError("bad ref")

        monkeypatch.setattr(git, "get_diff_content", _raise)
        res = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
        assert not res.passed
        assert "infrastructure error" in res.message

    def test_fails_closed_on_bad_regex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
            test_command="true", typecheck_command="true", lint_command="true",
            check_diff_scope=False, check_bad_patterns=False,
        )

    def test_disabled_policy_not_appended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._stub_git(monkeypatch, ["kstrl.toml"])
        result = run_mechanical_verification(
            tmp_path, self._prd(tmp_path), "main", None, self._config(),
            policy_config=PolicyConfig(enabled=False),
        )
        assert "policy_envelope" not in {c.name for c in result.checks}

    def test_enabled_policy_appended_and_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._stub_git(monkeypatch, ["kstrl.toml"])  # machinery halt
        result = run_mechanical_verification(
            tmp_path, self._prd(tmp_path), "main", None, self._config(),
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
