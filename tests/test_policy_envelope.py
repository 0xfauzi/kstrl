"""R8.1 policy envelope tests, driven through real git and real files.

What remains here reaches the policy envelope the way the factory does:
``check_policy_envelope`` over a real temporary repository with a planted
clean change, a denied ``.pem`` file and an enforcement-machinery edit; the
#399 unquote round trip measured against real ``git diff`` output for every
path spelling git C-quotes; ``PolicyConfig.load`` over a real ``kstrl.toml``
(toml, env overlay, license lists); the manifest ``policyHash`` round trip
through ``Manifest.save``/``Manifest.load``; license resolution against a
real on-disk uv cache layout; and the strict/lenient contract of the git
metadata readers against a directory that is not a repository (PR #173).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kstrl import git, licensing
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.policy import PolicyConfig, parse_added_lines
from kstrl.verify import check_policy_envelope
from tests.helpers import gitrepo


# --------------------------------------------------------------------------
# Diff path unquoting, measured against real git
# --------------------------------------------------------------------------
class TestDiffParsing:
    def test_the_unquote_round_trip_matches_get_diff_names_on_a_real_repo(
        self, tmp_path: Path
    ) -> None:
        """The claim A1 exists to prove, driven end to end: after
        unquoting, the paths ``parse_added_lines`` reports are the SAME set
        ``git diff --name-status`` (the lenient reader ``get_diff_names``
        wraps) already reports, for every trigger that makes git quote a
        path. Real git, not simulated - what a quoted path decodes to is
        not something worth guessing at.
        """
        gitrepo.git_in(tmp_path, "init", "-q", "-b", "main")
        gitrepo.set_identity(tmp_path)
        (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
        gitrepo.git_in(tmp_path, "add", "-A")
        gitrepo.git_in(tmp_path, "commit", "-q", "-m", "base")
        gitrepo.git_in(tmp_path, "checkout", "-q", "-b", "work")
        tricky_names = {
            "café.py",  # octal-escaped non-ASCII byte
            'we"ird.py',  # double quote
            "a\\b.py",  # backslash
            "a\tb.py",  # tab
        }
        for name in tricky_names:
            (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
        gitrepo.git_in(tmp_path, "add", "-A")
        gitrepo.git_in(tmp_path, "commit", "-q", "-m", "add four tricky files")

        lenient_names = set(git.get_diff_names("main", tmp_path))
        diff_text = git.get_diff_content("main", tmp_path)
        parsed_paths = {path for path, _line in parse_added_lines(diff_text)}

        assert lenient_names == tricky_names
        assert parsed_paths == tricky_names

    def test_the_unquote_round_trip_survives_a_quote_and_an_accent_on_one_path(
        self, tmp_path: Path
    ) -> None:
        """#399 blocker 1: a path holding BOTH a double quote (which makes
        git quote the header at all) and a non-ASCII character (which the
        octal-escape trigger tests exercise separately) used to raise
        ``UnicodeEncodeError`` out of ``unquote_diff_path``, because the
        round trip started with ``.encode("ascii")`` on text that
        ``unicode_escape``-decoding had already turned back into real
        (non-ASCII) characters for the unescaped run of bytes.
        ``core.quotepath=false`` is set explicitly: it does not change
        whether this path is quoted (a double quote alone forces quoting
        regardless), but it is the configuration the blocker report
        measured against, so this test matches it rather than the default.
        """
        gitrepo.git_in(tmp_path, "init", "-q", "-b", "main")
        gitrepo.set_identity(tmp_path)
        gitrepo.git_in(tmp_path, "config", "core.quotepath", "false")
        (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
        gitrepo.git_in(tmp_path, "add", "-A")
        gitrepo.git_in(tmp_path, "commit", "-q", "-m", "base")
        gitrepo.git_in(tmp_path, "checkout", "-q", "-b", "work")
        tricky_name = 'we"ird-café.py'
        (tmp_path / tricky_name).write_text("x = 1\n", encoding="utf-8")
        gitrepo.git_in(tmp_path, "add", "-A")
        gitrepo.git_in(tmp_path, "commit", "-q", "-m", "add a quoted, accented file")

        lenient_names = set(git.get_diff_names("main", tmp_path))
        diff_text = git.get_diff_content("main", tmp_path)
        parsed_paths = {path for path, _line in parse_added_lines(diff_text)}

        assert lenient_names == {tricky_name}
        assert parsed_paths == lenient_names


# --------------------------------------------------------------------------
# PolicyConfig.load over a real kstrl.toml
# --------------------------------------------------------------------------
class TestPolicyConfig:
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

    def test_load_reads_license_lists(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[policy]\nlicense_allow = ["MIT", "MPL-2.0"]\nlicense_deny_partial = ["AGPL"]\n'
        )
        cfg = PolicyConfig.load(tmp_path)
        assert cfg.license_allow == ["MIT", "MPL-2.0"]
        assert cfg.license_deny_partial == ["AGPL"]


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


# --------------------------------------------------------------------------
# Real-git end-to-end
# --------------------------------------------------------------------------
def _git_cmd(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git_cmd(["init"], root)
    _git_cmd(["symbolic-ref", "HEAD", "refs/heads/main"], root)
    gitrepo.set_identity(root)
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
# License resolution (kstrl.licensing) against a real uv cache layout
# --------------------------------------------------------------------------
class TestLicenseResolution:
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
# PR #173: metadata reads fail closed, not as 0 files / 0 lines
# --------------------------------------------------------------------------
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
