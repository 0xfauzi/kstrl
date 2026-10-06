"""What every toolchain surface says today, pinned before #635 moves it.

#635 replaces the language tables in ``kstrl/init_cmd.py`` and the gate
defaults in ``kstrl/verify.py`` with one record per ecosystem, and its
acceptance is that nothing a Python project sees changes. This file is
that acceptance, captured at the commit before the move (7404b803): it
drives the real ``ks`` CLI as subprocesses on one throwaway repository per
fixture and compares everything the CLI says about the toolchain with a
golden file under ``tests/fixtures/toolchain_identity/``.

The non-Python fixtures are here too. Later #635 slices change what they
say on purpose, and the golden diff of that PR is then the statement of
exactly what changed.

A stub ``uv`` first on PATH appends its arguments to a log and exits 0.
PATH is on the scrubbed environment's allowlist (``kstrl/verify.py``), so
the log records the commands the gates really ran. It matters because a
passing ``ks check`` row carries no command: the JSON document alone stays
the same when the test command changes. A stub ``gh`` that exits 1 makes
the doctor's github_cli row the same on every machine.

Normalisation is limited to what differs between two runs of one tree:
the temporary directory, the run id in a worktree path, the HEAD sha and
report path and timestamp in the doctor report, and the ``path`` and
``duration_seconds`` fields of ``ks check --json``. ``ks config show`` is
cut to the sections that can carry a toolchain command, because the other
sections hold unrelated keys and the ``[policy]`` secret patterns.

Regenerate with ``KSTRL_REGEN_TOOLCHAIN_GOLDENS=1``. A regenerating run
writes the files and then fails, so it can never pass in CI. A PR that
must not change Python behaviour shows no change under
``tests/fixtures/toolchain_identity/`` in its diff.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.gitrepo import git_in, set_identity
from tests.helpers.procs import kill_group
from tests.test_root_checkout_merge import _stub_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "toolchain_identity"
REGENERATE = os.environ.get("KSTRL_REGEN_TOOLCHAIN_GOLDENS") == "1"

#: Bound on one ``ks`` subprocess. The slowest measured is ``ks factory``.
KS_TIMEOUT = 240.0

PYPROJECT = '[project]\nname = "demo"\nversion = "0.1.0"\n'
CARGO = '[package]\nname = "rustapp"\nversion = "0.1.0"\nedition = "2021"\n'
GO_MOD = "module example.com/goapp\n\ngo 1.22\n"

#: Fixture name -> (files at the seed, (path, text) of the file the
#: feature branch adds for ``ks check``).
FIXTURES: dict[str, tuple[dict[str, str], tuple[str, str]]] = {
    "python-fresh": (
        {
            "pyproject.toml": PYPROJECT,
            "src/demo/__init__.py": "X = 1\n",
            "tests/test_demo.py": "def test_x() -> None:\n    assert True\n",
        },
        ("src/demo/extra.py", "Y = 2\n"),
    ),
    "python-mypy-scoped": (
        {
            "pyproject.toml": PYPROJECT + '\n[tool.mypy]\nfiles = ["src"]\n',
            "src/demo/__init__.py": "X = 1\n",
        },
        ("src/demo/extra.py", "Y = 2\n"),
    ),
    "python-setup-py": (
        {
            "setup.py": "from setuptools import setup\n\nsetup(name='demo')\n",
            "demo/__init__.py": "X = 1\n",
        },
        ("demo/extra.py", "Y = 2\n"),
    ),
    "rust": (
        {"Cargo.toml": CARGO, "src/lib.rs": "pub fn f() -> u8 { 1 }\n"},
        ("src/extra.rs", "pub fn g() -> u8 { 2 }\n"),
    ),
    "typescript": (
        {
            "package.json": '{"name": "webapp", "devDependencies": {"typescript": "5"}}\n',
            "tsconfig.json": "{}\n",
            "src/a.ts": "export const a = 1;\n",
        },
        ("src/b.ts", "export const b = 2;\n"),
    ),
    "typescript-dep-only": (
        {
            "package.json": '{"name": "webapp", "devDependencies": {"typescript": "5"}}\n',
            "src/a.ts": "export const a = 1;\n",
        },
        ("src/b.ts", "export const b = 2;\n"),
    ),
    "typescript-tsconfig-only": (
        {
            "package.json": '{"name": "webapp"}\n',
            "tsconfig.json": "{}\n",
            "src/a.ts": "export const a = 1;\n",
        },
        ("src/b.ts", "export const b = 2;\n"),
    ),
    "javascript": (
        {"package.json": '{"name": "jsapp"}\n', "src/a.js": "export const a = 1;\n"},
        ("src/b.js", "export const b = 2;\n"),
    ),
    "go": (
        {"go.mod": GO_MOD, "main.go": "package main\n\nfunc main() {}\n"},
        ("extra.go", "package main\n\nfunc g() int { return 2 }\n"),
    ),
    "java": (
        {"pom.xml": "<project></project>\n", "src/main/java/A.java": "class A {}\n"},
        ("src/main/java/B.java", "class B {}\n"),
    ),
    "java-gradlew": (
        {
            "build.gradle": "\n",
            "gradlew": "#!/bin/sh\nexit 0\n",
            "src/main/java/A.java": "class A {}\n",
        },
        ("src/main/java/B.java", "class B {}\n"),
    ),
    "kotlin": (
        {"build.gradle.kts": "\n", "src/main/kotlin/A.kt": "fun a() = 1\n"},
        ("src/main/kotlin/B.kt", "fun b() = 2\n"),
    ),
    "unknown": (
        {"Gemfile": "source 'https://rubygems.org'\n", "lib/a.rb": "A = 1\n"},
        ("lib/b.rb", "B = 2\n"),
    ),
    "maturin": (
        {
            "pyproject.toml": (
                '[project]\nname = "mat"\nversion = "0.1.0"\n\n'
                '[build-system]\nrequires = ["maturin>=1"]\nbuild-backend = "maturin"\n'
            ),
            "Cargo.toml": (
                '[package]\nname = "mat"\nversion = "0.1.0"\nedition = "2021"\n\n'
                '[lib]\ncrate-type = ["cdylib"]\n'
            ),
            "src/lib.rs": "pub fn f() -> u8 { 1 }\n",
            "python/mat/__init__.py": "X = 1\n",
        },
        ("python/mat/extra.py", "Y = 2\n"),
    ),
    "go-plus-package-json": (
        {
            "go.mod": GO_MOD,
            "main.go": "package main\n\nfunc main() {}\n",
            "package.json": '{"name": "tools", "devDependencies": {"prettier": "3"}}\n',
        },
        ("extra.go", "package main\n\nfunc g() int { return 2 }\n"),
    ),
    "polyglot": (
        {
            "pyproject.toml": PYPROJECT,
            "src/demo/__init__.py": "X = 1\n",
            "web/package.json": '{"name": "web", "devDependencies": {"typescript": "5"}}\n',
            "web/src/a.ts": "export const a = 1;\n",
        },
        ("web/src/b.ts", "export const b = 2;\n"),
    ),
}

#: The fixture ``ks factory`` runs on.
FACTORY_FIXTURES = frozenset({"python-fresh"})

#: ``ks config show`` sections kept whole, beside every ``*command`` row.
CONFIG_SECTIONS = frozenset({"verify", "contract", "breaker", "sandbox"})

#: The per-run directory under ``.kstrl/worktrees/`` in the engineer prompt.
RUN_ID = re.compile(r"factory-\d{8}-\d{6}\.\d{6}-[0-9a-f]+")

#: The wall-clock column of the architect usage table ``ks decompose`` prints.
USAGE_SECONDS = re.compile(r"(?m)^(  (?:@architect|TOTAL) .*\s)\d+$")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stub ``uv`` and ``gh`` first on PATH, no forced colour, and git
    reading no config of the machine's. Returns the log the stub ``uv``
    appends to."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "uv-argv.log"
    _write_executable(stubs / "uv", f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n")
    _write_executable(stubs / "gh", "#!/bin/sh\nexit 1\n")
    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    (tmp_path / "xdg-config").mkdir()
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    for var in ("VIRTUAL_ENV", "FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setenv("KSTRL_NO_TUI", "1")
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return log


def _ks(root: Path, *args: str) -> tuple[int, str, str]:
    """``python -m kstrl <args>`` in ``root``, in its own process group,
    killed on timeout."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=KS_TIMEOUT)
    except subprocess.TimeoutExpired:
        kill_group(proc.pid)
        out, err = proc.communicate()
        raise AssertionError(f"ks {' '.join(args)} did not finish in {KS_TIMEOUT}s") from None
    return proc.returncode, out, err


def _commit(root: Path, message: str) -> None:
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", message)


def _scrub(text: str, root: Path, tmp_path: Path) -> str:
    for path, token in ((root, "<root>"), (tmp_path, "<tmp>")):
        # Longest spelling first: an unresolved path can sit inside the
        # resolved one (/var/... inside /private/var/...).
        for spelling in sorted({str(path.resolve()), str(path)}, key=len, reverse=True):
            text = text.replace(spelling, token)
    return USAGE_SECONDS.sub(r"\g<1><s>", RUN_ID.sub("<run-id>", text))


def _lines(text: str, root: Path, tmp_path: Path) -> list[str]:
    return _scrub(text, root, tmp_path).splitlines()


def _seed(name: str, tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    for rel, text in FIXTURES[name][0].items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    if (root / "gradlew").exists():
        (root / "gradlew").chmod(0o755)
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    _commit(root, "seed")
    return root


def _decompose_before_init(root: Path, tmp_path: Path) -> dict[str, Any]:
    """``ks decompose --spec`` on the seed. The architect touches a
    sentinel and exits 97, so the sentinel says whether the preflight let
    it spend."""
    sentinel = tmp_path / "architect-ran"
    agent = tmp_path / "architect"
    _write_executable(agent, f"#!/bin/sh\ncat >/dev/null\ntouch '{sentinel}'\nexit 97\n")
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\nBuild a greeter.\n", encoding="utf-8")
    rc, out, err = _ks(
        root,
        "decompose",
        "--spec",
        str(spec),
        "--project-name",
        "demo",
        "--agent-cmd",
        str(agent),
        "--ui",
        "plain",
        "--no-color",
        "--no-tui",
    )
    return {
        "exit": rc,
        "architect_ran": sentinel.exists(),
        "stdout": _lines(out, root, tmp_path),
        "stderr": _lines(err, root, tmp_path),
    }


def _config_rows(text: str) -> list[str]:
    """The ``ks config show`` lines of ``CONFIG_SECTIONS``, and every
    ``*command`` row of the other sections, each under its header."""
    kept: list[str] = []
    section = ""
    for line in text.splitlines():
        header = re.fullmatch(r"\[([a-z_]+)\]", line)
        if header:
            section = header.group(1)
            continue
        key = line.strip().split(" = ", 1)[0]
        if line.startswith("  ") and (section in CONFIG_SECTIONS or key.endswith("command")):
            kept.append(f"[{section}] {line.strip()}")
    return kept


def _doctor_report(out: str, root: Path, tmp_path: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(_scrub(out, root, tmp_path))
    for key in ("generated_at", "report_path"):
        assert key in report, sorted(report)
        report[key] = f"<{key}>"
    for check in report["checks"]:
        check["detail"] = re.sub(r"HEAD [0-9a-f]{12}", "HEAD <sha>", check["detail"])
    return report


def _check_report(out: str, root: Path, tmp_path: Path) -> dict[str, Any]:
    """The addendum's normaliser: ``path`` and every
    ``checks[].duration_seconds``, each asserted present first."""
    report: dict[str, Any] = json.loads(out)
    if sorted(report) == ["error", "schema_version"]:
        # #696 slice 4: a refusal (no confirmed [stack]) measures nothing.
        refused: dict[str, Any] = json.loads(_scrub(json.dumps(report), root, tmp_path))
        return refused
    assert "path" in report, sorted(report)
    report["path"] = "<root>"
    for check in report["checks"]:
        assert "duration_seconds" in check, check
        check["duration_seconds"] = 0
    scrubbed: dict[str, Any] = json.loads(_scrub(json.dumps(report), root, tmp_path))
    return scrubbed


def _factory(root: Path, tmp_path: Path, log: Path) -> dict[str, Any]:
    """One ``ks factory --spec`` iteration with Phase 1 on: the engineer
    prompt the stub engineer received and the commands the gates ran."""
    spec = root / "spec.md"
    spec.write_text("# Spec\nBuild a greeter.\n", encoding="utf-8")
    _commit(root, "spec")
    dump = tmp_path / "engineer-prompt.txt"
    log.write_text("", encoding="utf-8")
    rc, out, err = _ks(
        root,
        "factory",
        "--root",
        str(root),
        "--spec",
        str(spec),
        "--project-name",
        "demo",
        "--no-prs",
        "--agent-cmd",
        str(_stub_agent(tmp_path, prompt_dump=dump)),
        "--review-mode",
        "skip",
        "--contract-check",
        "skip",
        "--max-parallel",
        "1",
        "--max-retries",
        "0",
        "--ui",
        "plain",
        "--no-tui",
        "-y",
    )
    if not dump.is_file():
        # #696 slice 4: no confirmed [stack] refuses before the engineer.
        assert rc == 2, out + err
        refusal = [line for line in (out + err).splitlines() if line.startswith("ERROR")]
        return {"exit": rc, "refusal": _lines("\n".join(refusal), root, tmp_path)}
    return {
        "exit": rc,
        "engineer_prompt": _lines(dump.read_text(encoding="utf-8"), root, tmp_path),
        "uv_argv": log.read_text(encoding="utf-8").splitlines(),
    }


def _surfaces(name: str, tmp_path: Path, log: Path) -> dict[str, Any]:
    root = _seed(name, tmp_path)
    doc: dict[str, Any] = {"decompose_before_init": _decompose_before_init(root, tmp_path)}

    rc, out, err = _ks(root, "init", ".", "--ui", "plain", "--no-color")
    doc["init"] = {
        "exit": rc,
        "stdout": _lines(out, root, tmp_path),
        "stderr": _lines(err, root, tmp_path),
    }
    doc["files"] = {
        rel: (root / rel).read_text(encoding="utf-8").splitlines()
        for rel in ("kstrl.toml", "CLAUDE.md", ".gitignore")
    }
    git_in(root, "add", "-A")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-status"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    doc["staged_after_init"] = sorted(staged.splitlines())
    _commit(root, "ks init")

    rc, out, err = _ks(root, "config", "show")
    doc["config_show"] = {"exit": rc, "rows": _config_rows(_scrub(out, root, tmp_path))}

    rc, out, err = _ks(root, "doctor", "--json")
    doc["doctor"] = {"exit": rc, "report": _doctor_report(out, root, tmp_path)}

    git_in(root, "checkout", "-q", "-b", "feature")
    rel, text = FIXTURES[name][1]
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")
    _commit(root, "feature")
    log.write_text("", encoding="utf-8")
    rc, out, err = _ks(root, "check", "--base", "main", "--json")
    doc["check"] = {"exit": rc, "report": _check_report(out, root, tmp_path)}
    doc["check_uv_argv"] = log.read_text(encoding="utf-8").splitlines()

    if name in FACTORY_FIXTURES:
        git_in(root, "checkout", "-q", "main")
        doc["factory"] = _factory(root, tmp_path, log)
    return doc


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_every_toolchain_surface_matches_its_golden(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _env(tmp_path, monkeypatch)
    surfaces = _surfaces(name, tmp_path, log)
    actual = json.dumps(surfaces, indent=2, sort_keys=True) + "\n"
    golden = GOLDEN_DIR / f"{name}.json"
    if REGENERATE:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="utf-8")
        pytest.fail(f"regenerated {golden.relative_to(REPO_ROOT)}; unset the variable and rerun")
    assert golden.is_file(), f"no golden at {golden}; run with KSTRL_REGEN_TOOLCHAIN_GOLDENS=1"
    expected = golden.read_text(encoding="utf-8")
    if actual != expected:
        stored = json.loads(expected)
        changed = sorted(k for k in stored.keys() | surfaces if stored.get(k) != surfaces.get(k))
        diff = difflib.unified_diff(
            expected.splitlines(), actual.splitlines(), "golden", "now", lineterm="", n=1
        )
        pytest.fail(f"{name}: surfaces changed: {changed}\n" + "\n".join(list(diff)[:80]))
