"""`ks doctor` Tier A: the verdict, the ten checks, and the report (#198).

Every test drives the real click command through `CliRunner`, so the
preflight seam, the option parsing and the exit code are all in the
path. One test drives `python -m kstrl doctor` as a real subprocess,
because a `SystemExit` captured by a runner is not proof of a process
exit code.

A stub `gh` is first on PATH for EVERY test in this module
(`_gh` is autouse). `pr.is_gh_available` shells out to
`gh auth status`, and without the stub these tests would report the
developer's own GitHub session on one machine and a failure on a
runner.
"""

from __future__ import annotations

import ast
import inspect
import json
import pkgutil
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

import kstrl
from kstrl import doctor
from kstrl.cli import cli
from kstrl.init_cmd import build_manifest_ok_reason, gitignore_block
from tests.helpers.fakegh import put_gh_on_path
from tests.helpers.gitrepo import git_in, set_identity
from tests.helpers.stack_confirmation import confirm_stack, write_stack

#: `gh auth status` succeeding and failing. Bodies rather than the
#: real binary: what these tests are about is what the doctor SAYS
#: about gh, not whether this machine happens to be logged in.
GH_OK = "#!/bin/sh\nexit 0\n"
GH_UNAUTHENTICATED = "#!/bin/sh\nexit 1\n"

#: The eight rows the report carries, in order. The test's own
#: literal, not a constant imported from `kstrl.doctor`: each name is
#: written once in production, inside its own check function, and a
#: comparison against a second copy the module also owns would pass
#: whenever both copies moved together.
EXPECTED_CHECK_NAMES = (
    "git_repo",
    "git_clean",
    "github_cli",
    "kstrl_config",
    "build_manifest",
    "verify_commands",
    "gitignore",
    "protected_paths",
)


@pytest.fixture(autouse=True)
def _gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put_gh_on_path(tmp_path, monkeypatch, GH_OK)


def ready_repo(tmp_path: Path, name: str = "demo") -> Path:
    """A repository every Tier A check passes on.

    Committed, so the tree is clean; `pyproject.toml` so a build
    manifest is at the root; a test file; `.kstrl/` ignored; an origin
    remote so `git.get_origin_slug` answers.
    """
    root = tmp_path / name
    root.mkdir()
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    git_in(root, "remote", "add", "origin", "https://github.com/acme/demo.git")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / ".gitignore").write_text(gitignore_block(), encoding="utf-8")
    pkg = root / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(
        "def test_widget() -> None:\n    assert True\n", encoding="utf-8"
    )
    write_stack(root)
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "initial")
    confirm_stack(root)
    return root


def run_doctor(root: Path, *args: str) -> Result:
    """Drive the real command; returns the click `Result`."""
    return CliRunner().invoke(cli, ["doctor", "--root", str(root), *args])


def test_a_ready_repo_is_ready(tmp_path: Path) -> None:
    root = ready_repo(tmp_path)
    result = run_doctor(root)
    assert result.exit_code == 0, result.output
    assert "ks doctor: ready" in result.output
    assert "[warn]" not in result.output
    assert "[fail]" not in result.output
    assert "Fix first:" not in result.output


def test_a_directory_that_is_not_a_git_repo_is_not_ready(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_doctor(plain)
    assert result.exit_code == 1, result.output
    assert "ks doctor: not-ready" in result.output
    assert "[fail] git_repo" in result.output
    assert "Fix first:" in result.output


def test_the_process_exit_code_is_one_for_not_ready(tmp_path: Path) -> None:
    """This one does NOT get the stub gh, because a subprocess inherits
    the real environment; it asserts only the exit code and the
    verdict, both of which a `git_repo` failure decides on its own
    whatever gh says.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "kstrl", "doctor", "--root", str(plain)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "not-ready" in proc.stdout


def test_an_unauthenticated_gh_is_a_warning_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ready_repo(tmp_path)
    put_gh_on_path(tmp_path, monkeypatch, GH_UNAUTHENTICATED)
    result = run_doctor(root)
    assert result.exit_code == 0, result.output
    assert "ks doctor: ready-with-warnings" in result.output
    assert "[warn] github_cli" in result.output
    assert "[fail]" not in result.output


def test_a_repo_that_does_not_ignore_the_state_dir_warns_with_the_line_to_add(
    tmp_path: Path,
) -> None:
    root = ready_repo(tmp_path)
    # A rule of the project's own stays, so the only thing missing is `.kstrl/`.
    (root / ".gitignore").write_text("dist/\n", encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "drop ignore")
    result = run_doctor(root)
    assert result.exit_code == 0, result.output
    assert "[warn] gitignore" in result.output
    assert ".kstrl/" in result.output
    assert "scripts/kstrl/" not in result.output
    # Pinned on the fix LINE specifically, not just anywhere in the
    # report: the probe path in the check's own detail also contains
    # ".kstrl/", so a fix text that lost the leading dot would still
    # pass a bare substring check on the whole report.
    assert "Add the line `.kstrl/` to .gitignore" in result.output


def test_ci_and_migrations_are_suggested_as_paths_deny_entries(tmp_path: Path) -> None:
    root = ready_repo(tmp_path)
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    migrations = root / "migrations"
    migrations.mkdir()
    (migrations / "0001_init.sql").write_text("-- init\n", encoding="utf-8")
    # Appended, not replaced: overwriting kstrl.toml would drop the
    # confirmed [stack] `ready_repo` wrote and refuse every later `ks
    # doctor` call on a different digest (#696 flag day).
    stack_toml_text = (root / "kstrl.toml").read_text(encoding="utf-8")
    (root / "kstrl.toml").write_text(
        stack_toml_text + "[policy]\nenabled = true\n", encoding="utf-8"
    )
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "add ci and migrations")
    result = run_doctor(root)
    assert result.exit_code == 0, result.output
    assert "[warn] protected_paths" in result.output
    assert '"migrations/**"' in result.output

    (root / "kstrl.toml").write_text(
        stack_toml_text + "[policy]\nenabled = false\n", encoding="utf-8"
    )
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "disable policy")
    second = run_doctor(root)
    assert "[warn] protected_paths" in second.output
    assert "enabled" in second.output


def test_a_broken_kstrl_toml_is_a_failed_check_not_a_refusal(tmp_path: Path) -> None:
    root = ready_repo(tmp_path)
    (root / "kstrl.toml").write_bytes(b"[verify\n")
    result = run_doctor(root)
    assert result.exit_code == 1, result.output
    assert "[fail] kstrl_config" in result.output
    assert "kstrl.toml" in result.output
    # the exemption is the point: the other checks still ran
    assert "[ok] git_repo" in result.output
    # `check_verify_commands` and `check_protected_paths` also fail to
    # load this file, but they point at kstrl_config instead of
    # repeating its own parse-error text: one row carries it, not
    # three.
    assert result.output.count("Invalid TOML") == 1
    assert result.output.count("not evaluated: kstrl.toml did not load (see kstrl_config)") == 2
    assert "[fail] verify_commands" in result.output
    assert "[fail] protected_paths" in result.output


def test_the_report_lands_under_the_state_dir_and_matches_the_json(tmp_path: Path) -> None:
    root = ready_repo(tmp_path)
    result = run_doctor(root, "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == doctor.DOCTOR_SCHEMA_VERSION
    assert document["verdict"] == "ready"
    written = Path(document["report_path"])
    # `root.resolve()`, not `root`: the command resolves --root, and
    # on a machine whose temp directory is a symlink the two spell
    # the same directory differently.
    assert written.parent == root.resolve() / ".kstrl" / "doctor"
    assert json.loads(written.read_text(encoding="utf-8")) == document
    assert [c["name"] for c in document["checks"]] == list(EXPECTED_CHECK_NAMES)


def test_a_dirty_tree_warns(tmp_path: Path) -> None:
    root = ready_repo(tmp_path)
    (root / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    result = run_doctor(root)
    assert result.exit_code == 0, result.output
    assert "[warn] git_clean" in result.output
    assert "scratch.py" in result.output


# #696 flag day: test_verify_commands_warn_when_there_is_no_project_for_uv_run
# was deleted. Its subject was the "uv run" Python-default command the
# verify_commands row used to guess with no pyproject.toml - that guessing
# is gone (decision: a stack is the only source of verification commands),
# so there is no reachable subject left for it to warn about.


def test_measure_on_a_broken_kstrl_toml_is_a_failed_row_not_a_crash(tmp_path: Path) -> None:
    """#654: `--measure` loads the verify and factory sections, which raise
    on this file. It does not measure, and points at kstrl_config."""
    root = ready_repo(tmp_path)
    (root / "kstrl.toml").write_bytes(b"[verify\n")
    result = run_doctor(root, "--measure")
    assert result.exit_code == 1, result.output
    assert "[fail] base_gates: not evaluated: kstrl.toml did not load" in result.output
    assert not (root / ".kstrl" / "contract").exists()


def test_every_report_carries_the_fit_boundaries(tmp_path: Path) -> None:
    root = ready_repo(tmp_path)
    plain = run_doctor(root)
    js = run_doctor(root, "--json")
    assert "not spec-readiness" in plain.output
    assert "cross-cutting" in plain.output
    assert json.loads(js.stdout)["fit_boundaries"] == list(doctor.FIT_BOUNDARIES)


def test_a_root_that_is_not_a_directory_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """The assertions are on the message and on the absence of the
    directory because what the guard buys is that write_report's
    mkdir(parents=True) never runs on a path the operator mistyped.
    """
    missing = tmp_path / "nope" / "deeper"
    result = run_doctor(missing)
    assert result.exit_code == 2, result.output
    assert "root is not a directory" in result.output
    assert not (tmp_path / "nope").exists()
    assert "ks doctor:" not in result.output


def test_the_refusals_carry_the_same_json_envelope_as_check(tmp_path: Path) -> None:
    """`ks doctor`'s refusal of a bad `--root` routes through the same
    `_check_error` helper `ks check` uses, so `--json` prints the same
    one-key document on both commands, naming the doctor's own schema
    version rather than check's.
    """
    missing = tmp_path / "nope"
    bad_root = run_doctor(missing, "--json")
    assert bad_root.exit_code == 2, bad_root.output
    assert "error:" in bad_root.output
    document = json.loads(bad_root.stdout)
    assert document["schema_version"] == doctor.DOCTOR_SCHEMA_VERSION
    assert "root is not a directory" in document["error"]


# --- #628: what doctor says about a repository that is not Python ---------
#
# #696 slice 8 deleted the test_root row and, with it, the Rust fixture
# whose inline tests no tracked path named.

# #696 flag day: test_verify_commands_shows_the_resolved_contract_command,
# test_verify_commands_shows_a_contract_command_set_in_kstrl_toml,
# test_verify_commands_points_at_kstrl_config_when_phase_3_does_not_resolve,
# and the config_show_contract_test_command helper they shared, were
# deleted. Their subject was `[contract] test_command` resolution: decision
# 5 unifies Phase 1 and Phase 3 onto the same confirmed [stack] checks
# (`check_verify_commands` no longer reads `[contract]` at all), so there is
# no reachable subject left for any of the three to exercise.


def test_doctor_on_a_python_repo_is_unchanged(tmp_path: Path) -> None:
    """The control: on the Python fixture every row is still ok."""
    document = json.loads(run_doctor(ready_repo(tmp_path), "--json").stdout)
    rows = {check["name"]: check for check in document["checks"]}
    assert document["verdict"] == "ready"
    assert {name: row["status"] for name, row in rows.items()} == dict.fromkeys(
        EXPECTED_CHECK_NAMES, "ok"
    )
    # #696 flag day: a confirmed [stack] is the only source of verification
    # commands now, so the row reads the stack `ready_repo` wrote and
    # confirmed rather than guessing a Python default.
    assert rows["verify_commands"]["detail"] == (
        "Phase 1 and Phase 3 will run the [stack] checks: tests `true`, "
        "typecheck `true`, lint `true`"
    )


# --- #452 item 10: the report names no Python module ---------------------

#: Every module and package name under kstrl/, longest first so the
#: alternation prefers `config_preflight` over `config`.
_MODULE_NAMES = sorted(
    {info.name.rsplit(".", 1)[-1] for info in pkgutil.walk_packages(kstrl.__path__, "kstrl.")},
    key=len,
    reverse=True,
)
_ALTERNATION = "|".join(re.escape(name) for name in _MODULE_NAMES)
_MODULE_PATH = re.compile(rf"\bkstrl\.(?:{_ALTERNATION})\b|\b(?:{_ALTERNATION})\.([A-Za-z_]\w*)")
#: `kstrl.toml` and `report.json` are file names, not module paths.
_FILE_SUFFIXES = frozenset(
    {"json", "jsonl", "lock", "md", "py", "toml", "tsv", "txt", "yaml", "yml"}
)


def module_paths(text: str) -> list[str]:
    """Every Python module path in ``text``, such as `adequacy.is_test_path`.

    ``kstrl/stack.py`` means `"stack"` is itself a genuine module name, so
    the TOML table ``[stack.checks]`` the #696 flag day's refusal text
    names matches the same pattern by coincidence. That occurrence is
    bracketed on both sides (TOML notation an operator reads literally,
    never a leaked internal path), so a match whose surrounding characters
    are exactly ``[`` and ``]`` is excluded rather than counted.
    """
    return [
        m.group(0)
        for m in _MODULE_PATH.finditer(text)
        if m.group(1) not in _FILE_SUFFIXES
        and not (text[m.start() - 1 : m.start()] == "[" and text[m.end() : m.end() + 1] == "]")
    ]


def _non_docstring_strings(tree: ast.AST) -> list[str]:
    """String constants in ``tree`` that are not docstrings: what can print."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_module_path_matcher_tells_a_module_from_a_file() -> None:
    """The control for both tests below."""
    assert module_paths("see adequacy.is_test_path, and kstrl.pr pushes") == [
        "adequacy.is_test_path",
        "kstrl.pr",
    ]
    assert module_paths("kstrl.toml resolves; wrote .kstrl/doctor/report.json") == []


@pytest.mark.parametrize("state", ["empty", "ready"])
def test_the_report_names_no_python_module(tmp_path: Path, state: str) -> None:
    """#452: a doctor row once printed a kstrl module path to the operator.
    An empty repository reaches the warn branches and a ready one the ok
    branches, so between them every check prints."""
    if state == "ready":
        root = ready_repo(tmp_path)
    else:
        root = tmp_path / "empty"
        root.mkdir()
        git_in(root, "init", "-q")
    result = run_doctor(root)
    assert "protected_paths" in result.output
    assert module_paths(result.output) == []


def test_no_string_the_doctor_can_print_names_a_python_module() -> None:
    """The static half, for the branches no fixture above reaches.
    Docstrings are exempt: they are for the next maintainer."""
    doctor_tree = ast.parse(Path(doctor.__file__).read_text(encoding="utf-8"))
    ok_reason_tree = ast.parse(textwrap.dedent(inspect.getsource(build_manifest_ok_reason)))
    hits = [
        (text[:60], path)
        for tree in (doctor_tree, ok_reason_tree)
        for text in _non_docstring_strings(tree)
        for path in module_paths(text)
    ]
    assert hits == []
