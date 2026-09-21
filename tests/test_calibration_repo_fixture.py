"""The architect calibration fixture that carries a repository (#401).

Every test here runs without KSTRL_RUN_CALIBRATION and makes no LLM call.
What they check is that the fixture is capable of measuring something:

- the repository is a real one, so ``extract_public_interfaces`` produces
  a genuine section rather than a "(none: ...)" line;
- the map describes THAT repository, so replacing it with filler of the
  same length is a red test rather than an equally-valid fixture;
- the two arms render different prompts and run in different directories,
  so repository content can move the measured number;
- the ids a run records under are stable across processes and pair under
  ``compare_baselines``, so an arm going dark is a reported regression.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl import calibration
from kstrl.decompose import ARCHITECT_REPO_SOURCE_PROMPT
from kstrl.feedforward import extract_public_interfaces
from tests.helpers.calibration_repo_fixture import (
    REPO_DIR_SUFFIX,
    REUSE_ROLE,
    SPECS_DIR,
    Arm,
    RepoSpecFixture,
    arm_cwd,
    arm_params,
    arm_prompt,
    interface_paths,
    load_repo_spec_fixtures,
    materialize_repo,
    public_symbol_names,
    python_paths_mentioned,
    reuse_caught,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = load_repo_spec_fixtures()
FIXTURE_PARAMS = [pytest.param(f, id=f.fixture_id) for f in FIXTURES]
ARM_PARAMS = [pytest.param(f, a, id=a.fixture_id) for f, a in arm_params()]


def test_at_least_one_spec_fixture_carries_a_repository() -> None:
    """#401's whole subject. An empty list is the pre-fix state, in which
    no architect prompt change that depends on repository content can be
    measured at all."""
    assert FIXTURES, "no spec fixture declares a repo_dir"


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_the_repository_yields_a_genuine_public_interface_section(
    fixture: RepoSpecFixture,
) -> None:
    interfaces = extract_public_interfaces(fixture.repo_dir)
    assert not interfaces.startswith("(none"), interfaces
    must = fixture.must_reuse
    assert must["existing_module"] in interface_paths(interfaces), interfaces
    assert must["existing_symbol"] in public_symbol_names(interfaces), interfaces


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_the_codebase_map_describes_this_repository(fixture: RepoSpecFixture) -> None:
    """Content, not size. The map must name every module and every public
    symbol the repository has, and may name no file it does not, so filler
    of the same length fails here instead of measuring nothing."""
    text = fixture.codebase_map.read_text(encoding="utf-8")
    interfaces = extract_public_interfaces(fixture.repo_dir)

    missing_paths = sorted(p for p in interface_paths(interfaces) if p not in text)
    assert not missing_paths, f"map does not name {missing_paths}"

    missing_symbols = sorted(s for s in public_symbol_names(interfaces) if s not in text)
    assert not missing_symbols, f"map does not name {missing_symbols}"

    invented = sorted(
        token for token in python_paths_mentioned(text) if not (fixture.repo_dir / token).is_file()
    )
    assert not invented, f"map names files the repository does not have: {invented}"

    assert fixture.must_reuse["existing_module"] in text
    assert fixture.must_reuse["existing_symbol"] in text


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_the_spec_does_not_name_what_the_fixture_grades(fixture: RepoSpecFixture) -> None:
    """The graded observable has to come from the repository.

    If the spec names the module, the dotted module path or the symbol,
    then both arms can produce it from the spec alone and the repository
    moves nothing, which is the same zero-by-construction the issue is
    about. Whitespace is collapsed before the symbol check so a prose
    spelling ("token bucket") is caught as well as the identifier.
    """
    spec = fixture.spec_text
    must = fixture.must_reuse
    for marker in must["module_markers"]:
        assert str(marker) not in spec, f"the spec hands the architect {marker!r}"
    assert str(must["existing_module"]) not in spec
    squashed = "".join(spec.lower().split())
    assert str(must["existing_symbol"]).lower() not in squashed, must["existing_symbol"]


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_the_two_arms_render_different_prompts(fixture: RepoSpecFixture) -> None:
    by_arm = {arm.arm: arm_prompt(fixture, arm) for arm in fixture.arms}
    head = ARCHITECT_REPO_SOURCE_PROMPT.splitlines()[0]
    assert head in by_arm["repo_present"]
    assert head not in by_arm["repo_absent"]
    assert fixture.codebase_map_path in by_arm["repo_present"]
    assert fixture.codebase_map_path not in by_arm["repo_absent"]


@pytest.mark.parametrize("fixture,arm", ARM_PARAMS)
def test_each_arm_runs_in_the_directory_its_prompt_describes(
    fixture: RepoSpecFixture,
    arm: Arm,
    tmp_path: Path,
) -> None:
    cwd = arm_cwd(fixture, arm, tmp_path)
    assert (cwd / fixture.must_reuse["existing_module"]).is_file() is arm.expect_module_named
    assert (cwd / fixture.codebase_map_path).is_file() is arm.expect_module_named


@pytest.mark.parametrize("fixture,arm", ARM_PARAMS)
def test_every_run_of_an_arm_gets_a_clean_working_directory(
    fixture: RepoSpecFixture,
    arm: Arm,
    tmp_path: Path,
) -> None:
    """One paid arm runs ``CALIBRATION_RUNS`` times (default 3) against
    ONE function-scoped ``tmp_path``. If the runs shared a directory,
    run 1 would measure the fixture and runs 2 and 3 would measure the
    fixture plus whatever the agent left behind, and the mean of the
    three would still look like a measurement."""
    first = arm_cwd(fixture, arm, tmp_path)
    (first / "written-by-run-1.txt").write_text("x", encoding="utf-8")
    second = arm_cwd(fixture, arm, tmp_path)
    assert second != first
    assert not (second / "written-by-run-1.txt").exists()
    assert (second / fixture.must_reuse["existing_module"]).is_file() is arm.expect_module_named


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_the_materialised_repository_is_a_copy(
    fixture: RepoSpecFixture,
    tmp_path: Path,
) -> None:
    """The calibration agent is not read-only. A run that wrote into the
    checked-in fixture would change what every later run measures."""
    repo = materialize_repo(fixture, tmp_path / "repo")
    (repo / "written-by-a-run.txt").write_text("x", encoding="utf-8")
    assert not (fixture.repo_dir / "written-by-a-run.txt").exists()


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_the_reuse_matcher_resolves_on_both_arms(fixture: RepoSpecFixture) -> None:
    """A decomposition built to satisfy the meta scores as reuse, and a
    silent one does not, with the sign flipped on the no-repository arm."""
    must = fixture.must_reuse
    naming = {
        "components": [
            {
                "id": "ingest-limits",
                "description": f"Reuse {must['existing_module']} on the ingest route",
                "allowedPaths": ["src/"],
            }
        ]
    }
    silent = {"components": [{"id": "ingest-limits", "description": "Add a limiter"}]}
    for arm in fixture.arms:
        assert reuse_caught(naming, arm, must)[0] is arm.expect_module_named
        assert reuse_caught(silent, arm, must)[0] is (not arm.expect_module_named)


def test_the_paid_arm_test_records_under_these_ids() -> None:
    """The ids the calibration run writes into a baseline come from
    ``arm_params`` and nowhere else, so what the pairing test below
    proves about those ids is true of the recorded ones."""
    from tests.test_calibration import REUSE_ARM_PARAMS

    assert [p.id for p in REUSE_ARM_PARAMS] == [arm.fixture_id for _f, arm in arm_params()]
    assert REUSE_ARM_PARAMS, "the paid arm test is parametrized over nothing"


def _arm_cwd_calls(node: ast.AST) -> int:
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "arm_cwd"
    )


def test_the_paid_arm_test_takes_its_directory_per_run() -> None:
    """``arm_cwd`` is called INSIDE ``run_once``, never above it.

    Hoisted above the closure it is called once per arm, every run of
    that arm shares one directory, and the fresh-directory rule above
    buys nothing. Nothing else in the suite fails when that happens,
    because the paid test is skipped without KSTRL_RUN_CALIBRATION.
    This guard fails red when it cannot find what it checks.
    """
    tree = ast.parse((REPO_ROOT / "tests" / "test_calibration.py").read_text(encoding="utf-8"))
    paid = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "test_architect_reuses_what_the_repository_already_has"
        ),
        None,
    )
    assert paid is not None, "the paid arm test is gone, so this guard now checks nothing"
    run_once = next(
        (
            node
            for node in ast.walk(paid)
            if isinstance(node, ast.FunctionDef) and node.name == "run_once"
        ),
        None,
    )
    assert run_once is not None, "run_once is gone, so this guard now checks nothing"
    assert _arm_cwd_calls(run_once) == 1, "run_once must call arm_cwd exactly once"
    assert _arm_cwd_calls(paid) == 1, (
        "arm_cwd is also called outside run_once, so every run of the arm "
        "shares one working directory"
    )


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_compare_baselines_pairs_the_arms_across_two_runs(
    fixture: RepoSpecFixture,
    tmp_path: Path,
) -> None:
    """Two baselines, a fixture id in both, and an arm that was detected
    in the old one and is not in the new one: a reported regression.

    PR #397's arm could not do this. Its ids existed in no saved baseline,
    and ``compare_baselines`` reports ``newly_missed`` only for a key
    present on BOTH sides, so a new key can never produce a signal.

    The ids are read back out of a FRESH interpreter first. An id built
    from anything that varies per run pairs with nothing, and inside one
    process a per-import value would look stable.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json;"
            "from tests.helpers.calibration_repo_fixture import arm_params;"
            "print(json.dumps([[a.fixture_id, a.arm] for _f, a in arm_params()]))",
        ],
        capture_output=True,
        text=True,
        check=True,
        # The interpreter is exec'd directly, not through a wrapper, so
        # this deadline kills the process that holds the work.
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    assert json.loads(probe.stdout) == [[a.fixture_id, a.arm] for _f, a in arm_params()], (
        "arm ids differ between two processes, so a saved baseline cannot carry them"
    )

    def records(detected_present: bool) -> list[dict[str, object]]:
        return [
            {
                "role": REUSE_ROLE,
                "fixture_id": arm.fixture_id,
                "category": arm.arm,
                "cwe": None,
                "caught": detected_present or not arm.expect_module_named,
                "error": False,
                "detail": "",
            }
            for arm in fixture.arms
        ]

    old = calibration.save_report(
        calibration.build_report(
            records(True), model="m", timestamp="20260101-000000", runs_per_fixture=1
        ),
        tmp_path,
    )
    new = calibration.save_report(
        calibration.build_report(
            records(False), model="m", timestamp="20260102-000000", runs_per_fixture=1
        ),
        tmp_path,
    )
    comparison = calibration.compare_baselines(
        calibration.load_baseline(old),
        calibration.load_baseline(new),
    )
    present = next(a for a in fixture.arms if a.expect_module_named)
    assert f"{REUSE_ROLE}/{present.fixture_id}" in comparison.newly_missed
    # The CATEGORY drop is the mechanism that fails a comparison, not the
    # role floor: one arm of two going dark leaves the role rate at 0.50,
    # which is not BELOW the 0.50 default, so a floor failure never fires
    # on this shape. Pin the failure that does.
    assert any(
        failure.startswith(f"category {REUSE_ROLE}/{present.arm} ")
        for failure in comparison.failures
    ), comparison.failures


def test_every_repo_directory_under_specs_is_a_declared_fixture() -> None:
    """Both sides of ``specs/conftest.py``'s ignore glob: every declared
    repository is covered by it, and it covers nothing else."""
    declared = {f.repo_dir for f in FIXTURES}
    assert all(d.name.endswith(REPO_DIR_SUFFIX) for d in declared), declared
    on_disk = {p for p in SPECS_DIR.glob(f"*{REPO_DIR_SUFFIX}") if p.is_dir()}
    assert on_disk == declared


def test_the_fixture_repositories_are_not_collected_by_our_own_suite() -> None:
    """The real collector over the fixture tree. Without the conftest the
    fixture repository's own tests are imported and the run dies on
    ModuleNotFoundError before any kstrl test executes."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/adversarial_fixtures",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO_ROOT),
    )
    combined = proc.stdout + proc.stderr
    # 5 is pytest's EXIT_NOTESTSCOLLECTED. Checked as a number first,
    # because "no 'error' in the text" also passes when the collector
    # never ran; a collection failure exits 2, not 5.
    assert proc.returncode == 5, f"collector exited {proc.returncode}\n{combined}"
    assert "error" not in combined.lower(), combined
    assert "no tests collected" in combined, combined
