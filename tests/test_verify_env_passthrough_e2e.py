"""What reaches a verification command's environment, end to end (#623).

Each test drives ``ks check --json`` on a real git repository whose
``[verify]`` test, typecheck and lint commands each run a probe script.
The probe exits 0 when the environment it was started with matches what
the test expects and 3 otherwise, printing the names it saw, so the three
gate rows of the JSON document are the observable outcome. The last test renders the
launchd plist through ``ks serve --print-plist``.
"""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers import gitrepo
from tests.spine_utils import git

_GATES = ("test_suite", "typecheck", "linter")

#: argv: pairs of NAME and EXPECTED, where EXPECTED is the value the
#: variable must hold, or "-" for "must be absent".
_PROBE = """\
import os
import sys

args = sys.argv[1:]
wrong = []
for name, expected in zip(args[::2], args[1::2]):
    actual = os.environ.get(name)
    if (expected == "-" and actual is not None) or (expected != "-" and actual != expected):
        wrong.append(f"{name}={actual!r} (expected {expected!r})")
if wrong:
    print("environment mismatch: " + "; ".join(wrong))
    print("names seen: " + " ".join(sorted(os.environ)))
    sys.exit(3)
"""


def _repo(tmp_path: Path, expect: dict[str, str], verify_extra: str = "") -> Path:
    """A one-commit repository whose three gate commands each probe ``expect``."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    argv = " ".join(f"{name} {value}" for name, value in expect.items())
    command = json.dumps(f"{sys.executable} {probe} {argv}")
    root = tmp_path / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "kstrl.toml").write_text(
        "[verify]\n"
        f"test_command = {command}\n"
        f"typecheck_command = {command}\n"
        f"lint_command = {command}\n" + verify_extra,
        encoding="utf-8",
    )
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root


def _gate_rows(root: Path) -> list[dict[str, Any]]:
    """The test_suite, typecheck and linter rows of ``ks check --json``."""
    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--json"])
    document: dict[str, Any] = json.loads(result.stdout)
    assert "checks" in document, document
    rows = [row for row in document["checks"] if row["name"] in _GATES]
    assert len(rows) == len(_GATES), document
    return rows


def _all_passed(rows: list[dict[str, Any]]) -> bool:
    return all(row["passed"] is True for row in rows)


@pytest.fixture(autouse=True)
def _no_operator_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env door of ``[verify] env_passthrough`` stays shut unless a test opens it."""
    monkeypatch.delenv("KSTRL_VERIFY_ENV_PASSTHROUGH", raising=False)


def test_a_ca_bundle_variable_reaches_the_verification_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No configuration at all: the built-in set carries it."""
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/corp/ca.pem")
    monkeypatch.setenv("JAVA_HOME", "/opt/jdk-21")
    root = _repo(tmp_path, {"NODE_EXTRA_CA_CERTS": "/etc/corp/ca.pem", "JAVA_HOME": "/opt/jdk-21"})

    rows = _gate_rows(root)

    assert _all_passed(rows), rows


def test_a_secret_named_variable_still_does_not_reach_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named in env_passthrough, and still dropped: the fragment filter wins."""
    monkeypatch.setenv("FOO_TOKEN", "hunter2")
    monkeypatch.setenv("FOO_REGION", "eu-west-1")
    # npm reads its auth token from a lower-case npm_config_ name.
    monkeypatch.setenv("npm_config__authToken", "npm-secret")
    monkeypatch.setenv("npm_config_registry", "https://npm.corp.invalid")
    root = _repo(
        tmp_path,
        {
            "FOO_TOKEN": "-",
            "FOO_REGION": "eu-west-1",
            "npm_config__authToken": "-",
            "npm_config_registry": "https://npm.corp.invalid",
        },
        verify_extra='env_passthrough = ["FOO_TOKEN", "FOO_*", "npm_config_*"]\n',
    )

    rows = _gate_rows(root)

    assert _all_passed(rows), rows


def test_env_passthrough_adds_a_named_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact name and a NAME* prefix, neither on the built-in list."""
    monkeypatch.setenv("APP_DATABASE_URL", "postgres://localhost/test")
    monkeypatch.setenv("MYCORP_REGISTRY", "https://npm.mycorp.invalid")
    monkeypatch.setenv("MYCORPX_OTHER", "not-named")
    root = _repo(
        tmp_path,
        {
            "APP_DATABASE_URL": "postgres://localhost/test",
            "MYCORP_REGISTRY": "https://npm.mycorp.invalid",
            "MYCORPX_OTHER": "-",
        },
        verify_extra='env_passthrough = ["APP_DATABASE_URL", "MYCORP_*"]\n',
    )
    # The environment door: the same names from KSTRL_VERIFY_ENV_PASSTHROUGH
    # and no toml key.
    env_door = _repo(tmp_path / "env_door", {"APP_DATABASE_URL": "postgres://localhost/test"})

    rows = _gate_rows(root)
    monkeypatch.setenv("KSTRL_VERIFY_ENV_PASSTHROUGH", "APP_DATABASE_URL")
    env_door_rows = _gate_rows(env_door)

    assert _all_passed(rows), rows
    assert _all_passed(env_door_rows), env_door_rows


def test_a_whole_environment_passthrough_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``*`` would hand the project's commands every secret kstrl
    holds. Both doors refuse it before any command runs (exit 2)."""
    toml_door = _repo(tmp_path / "toml_door", {}, verify_extra='env_passthrough = ["*"]\n')
    env_door = _repo(tmp_path / "env_door", {})
    runner = CliRunner()

    from_toml = runner.invoke(cli, ["check", "--root", str(toml_door), "--json"])
    monkeypatch.setenv("KSTRL_VERIFY_ENV_PASSTHROUGH", "*")
    from_env = runner.invoke(cli, ["check", "--root", str(env_door), "--json"])

    for result in (from_toml, from_env):
        document = json.loads(result.stdout)
        assert result.exit_code == 2, document
        assert "checks" not in document, document
        assert "env_passthrough" in document["error"] or "ENV_PASSTHROUGH" in document["error"]


def _proxy_url(userinfo: str = "") -> str:
    """A proxy URL, with ``user:pass@`` when ``userinfo`` is given. Built from
    parts so the source holds no literal credentialed URL for a secret
    scanner to report."""
    return "http://" + userinfo + "proxy.corp.invalid:3128"


def test_a_credentialed_proxy_url_is_handled_as_decided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy URL with no userinfo passes by default. One carrying
    ``user:pass@`` is dropped, and reaches the command only when the
    operator names the variable in env_passthrough."""
    alice = _proxy_url("alice" + ":" + "s3cret" + "@")
    monkeypatch.setenv("HTTP_PROXY", _proxy_url())
    monkeypatch.setenv("HTTPS_PROXY", alice)
    monkeypatch.setenv("http_proxy", _proxy_url())
    monkeypatch.setenv("https_proxy", _proxy_url("bob" + ":" + "hunter2" + "@"))
    default = _repo(
        tmp_path / "default",
        {
            "HTTP_PROXY": _proxy_url(),
            "http_proxy": _proxy_url(),
            "HTTPS_PROXY": "-",
            "https_proxy": "-",
        },
    )
    named = _repo(
        tmp_path / "named",
        {"HTTPS_PROXY": alice},
        verify_extra='env_passthrough = ["HTTPS_PROXY"]\n',
    )

    default_rows = _gate_rows(default)
    named_rows = _gate_rows(named)

    assert _all_passed(default_rows), default_rows
    assert _all_passed(named_rows), named_rows


def test_the_launchd_plist_path_contains_the_configured_extra_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PATH of the shell that printed the plist, kept to the absolute
    directories that exist, comes first in the job's PATH. A relative entry
    is dropped even when it names a directory that exists from where the
    plist was printed: launchd would resolve it against the project root."""
    cargo_bin = tmp_path / "cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    (tmp_path / "relative" / "bin").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "gone" / "bin"
    monkeypatch.setenv("PATH", f"{cargo_bin}:{missing}:relative/bin::/usr/bin:/bin")
    root = tmp_path / "proj"
    root.mkdir()

    result = CliRunner().invoke(cli, ["serve", "--print-plist", "--root", str(root)])

    assert result.exit_code == 0, result.output
    parts = plistlib.loads(result.output.encode())["EnvironmentVariables"]["PATH"].split(":")
    assert parts[0] == str(cargo_bin), parts
    assert str(missing) not in parts
    assert "relative/bin" not in parts
    assert "" not in parts
    assert parts.count("/usr/bin") == 1
