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
  ``compare_baselines``, so an arm that stops being detected is a
  reported regression.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl import calibration, calibration_baseline
from kstrl.decompose import ARCHITECT_REPO_SOURCE_PROMPT
from kstrl.feedforward import extract_public_interfaces
from tests.helpers.astwalk import REPO_ROOT, TESTS_DIR, parsed
from tests.helpers.calibration_repo_fixture import (
    REPO_DIR_SUFFIX,
    REUSE_ROLE,
    SPECS_DIR,
    Arm,
    RepoSpecFixture,
    arm_cwd,
    arm_params,
    arm_prompt,
    load_repo_spec_fixtures,
    materialize_repo,
    reuse_caught,
)

FIXTURES = load_repo_spec_fixtures()
FIXTURE_PARAMS = [pytest.param(f, id=f.fixture_id) for f in FIXTURES]
ARM_PARAMS = [pytest.param(f, a, id=a.fixture_id) for f, a in arm_params()]

# --- census helpers used by the map-discrimination test below (#401
# addendum C3: moved here from tests/helpers/calibration_repo_fixture.py,
# beside their only callers) -------------------------------------------

_PY_TOKEN = re.compile(r"[A-Za-z0-9_./-]+\.py")
_SYMBOL = re.compile(r"(?:class|def) ([A-Za-z_][A-Za-z0-9_]*)")


def python_paths_mentioned(text: str) -> set[str]:
    """Every ``*.py`` path token in a map, leading ``./`` stripped."""
    return {token.lstrip("./") for token in _PY_TOKEN.findall(text)}


def public_symbol_names(interfaces: str) -> set[str]:
    """Symbol names out of an ``extract_public_interfaces`` rendering."""
    return set(_SYMBOL.findall(interfaces))


def interface_paths(interfaces: str) -> set[str]:
    """Module paths out of an ``extract_public_interfaces`` rendering."""
    return {line.split(":", 1)[0].strip() for line in interfaces.splitlines() if ":" in line}


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
    # existing_module/existing_symbol are not asserted again here: the
    # test above already pins that they are in interface_paths/
    # public_symbol_names, and missing_paths/missing_symbols above cover
    # every entry of those (#401 addendum C4).


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
    by_arm = {arm.name: arm_prompt(fixture, arm) for arm in fixture.arms}
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
    """A second call for the same arm and ``tmp_path`` gets a clean directory (see ``arm_cwd``)."""
    first = arm_cwd(fixture, arm, tmp_path)
    (first / "written-by-run-1.txt").write_text("x", encoding="utf-8")
    second = arm_cwd(fixture, arm, tmp_path)
    assert second != first
    assert not (second / "written-by-run-1.txt").exists()
    # second's own content is not re-checked here: it is what
    # test_each_arm_runs_in_the_directory_its_prompt_describes already
    # pins for any call to arm_cwd (#401 addendum C4).


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


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_naming_the_module_only_as_a_rejected_alternative_is_not_named(
    fixture: RepoSpecFixture,
) -> None:
    """A module named ONLY as ``decisions[].alternative`` is the option
    the architect rejected, not the one it took (#401 addendum A2). If
    the matcher searched the raw output it would score this as reuse on
    the ``repo_present`` arm, which is the opposite of what happened.
    """
    must = fixture.must_reuse
    rejected_only = {
        "components": [{"id": "ingest-limits", "description": "Add a limiter"}],
        "decisions": [
            {
                "issue": "rate-limit-approach",
                "disposition": "decided",
                "reason": "a fresh limiter is simpler to reason about",
                "alternative": f"reuse {must['existing_module']}",
            }
        ],
    }
    for arm in fixture.arms:
        assert reuse_caught(rejected_only, arm, must)[0] is (not arm.expect_module_named)


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_naming_the_module_in_a_component_field_is_named(
    fixture: RepoSpecFixture,
) -> None:
    """The other half of A2: a module named in a component's
    ``allowedPaths`` or description, where a rejected ``alternative`` is
    ALSO present, still counts. A2 must remove only the rejected-option
    fields, not stop the matcher from seeing a real answer.
    """
    must = fixture.must_reuse
    named_in_component = {
        "components": [
            {
                "id": "ingest-limits",
                "description": f"Extend {must['existing_module']}",
                "allowedPaths": [must["existing_module"]],
            }
        ],
        "decisions": [
            {
                "issue": "rate-limit-approach",
                "disposition": "decided",
                "reason": "extend what is there",
                "alternative": "build a new limiter from scratch",
            }
        ],
    }
    for arm in fixture.arms:
        assert reuse_caught(named_in_component, arm, must)[0] is arm.expect_module_named


@pytest.mark.parametrize("fixture", FIXTURE_PARAMS)
def test_a_halted_run_is_labelled_as_a_halt(fixture: RepoSpecFixture) -> None:
    """``components: []`` is a halt (kstrl/decompose.py lines 1010-1023),
    not a silent miss, and the detail says so (#401 addendum A3). The
    caught value still follows the same sign as silence: the module is
    not named either way.
    """
    must = fixture.must_reuse
    halted = {"components": [], "decisions": [{"issue": "x", "disposition": "escalated"}]}
    for arm in fixture.arms:
        caught, detail = reuse_caught(halted, arm, must)
        assert caught is (not arm.expect_module_named)
        assert "halted (no components); " in detail, detail

    live = {"components": [{"id": "ingest-limits", "description": "Add a limiter"}]}
    for arm in fixture.arms:
        _caught, detail = reuse_caught(live, arm, must)
        assert "halted" not in detail, detail


def test_the_paid_arm_test_records_under_these_ids() -> None:
    """The ids the calibration run writes into a baseline come from
    ``arm_params`` and nowhere else, so what the pairing test below
    proves about those ids is true of the recorded ones.

    Also reads the ids back out of a FRESH interpreter (#401 addendum
    C6: moved here from the per-fixture pairing test, so a second
    fixture does not spawn a second identical child process for the
    same check). An id built from anything that varies per run pairs
    with nothing, and inside one process a per-import value would look
    stable.
    """
    from tests.test_calibration import REUSE_ARM_PARAMS

    assert [p.id for p in REUSE_ARM_PARAMS] == [arm.fixture_id for _f, arm in arm_params()]
    assert REUSE_ARM_PARAMS, "the paid arm test is parametrized over nothing"

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json;"
            "from tests.helpers.calibration_repo_fixture import arm_params;"
            "print(json.dumps([[a.fixture_id, a.name] for _f, a in arm_params()]))",
        ],
        capture_output=True,
        text=True,
        check=True,
        # The interpreter is exec'd directly, not through a wrapper, so
        # this deadline kills the process that holds the work.
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    assert json.loads(probe.stdout) == [[a.fixture_id, a.name] for _f, a in arm_params()], (
        "arm ids differ between two processes, so a saved baseline cannot carry them"
    )


def _arm_cwd_calls(node: ast.AST) -> int:
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "arm_cwd"
    )


def _measure_detection_call(node: ast.AST) -> ast.Call | None:
    return next(
        (
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_measure_detection"
        ),
        None,
    )


def _category_keyword_is_arm_name(call: ast.Call) -> bool:
    """Is ``category=arm.name`` passed by keyword? A hard-coded string
    here would defeat the point: the category has to carry the PER-ARM
    value, not a constant that happens to match one arm.
    """
    value = next((kw.value for kw in call.keywords if kw.arg == "category"), None)
    return (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "arm"
        and value.attr == "name"
    )


def _records_under_the_arm_id(call: ast.Call) -> bool:
    """The first two positional arguments are the (role, fixture_id) key a
    baseline carries. ``fixture.fixture_id`` collapses both arms onto one
    key and ``compare_baselines`` then reports no ``newly_missed`` at all.
    """
    if len(call.args) < 2:
        return False
    role, fixture_id = call.args[0], call.args[1]
    return (
        isinstance(role, ast.Name)
        and role.id == "REUSE_ROLE"
        and isinstance(fixture_id, ast.Attribute)
        and isinstance(fixture_id.value, ast.Name)
        and fixture_id.value.id == "arm"
        and fixture_id.attr == "fixture_id"
    )


def test_the_paid_arm_test_takes_its_directory_per_run() -> None:
    """``arm_cwd`` is called INSIDE ``run_once``, never above it, and the
    ``_measure_detection`` call pins ``category`` to the per-arm field
    (#401 addendum A4) and the (role, fixture_id) key it records under.

    Hoisted above the closure it is called once per arm, every run of
    that arm shares one directory, and the fresh-directory rule above
    buys nothing. Nothing else in the suite fails when that happens,
    because the paid test is skipped without KSTRL_RUN_CALIBRATION.
    This guard fails red when it cannot find what it checks.
    """
    # astwalk.calls_to is not used here: it resolves calls module-wide,
    # and this guard counts calls inside one nested FunctionDef only.
    tree = parsed(TESTS_DIR / "test_calibration.py")
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

    call = _measure_detection_call(paid)
    assert call is not None, "_measure_detection is gone, so this guard now checks nothing"
    assert _category_keyword_is_arm_name(call), (
        "_measure_detection must be called with category=arm.name (the "
        "per-arm attribute), not a hard-coded or missing category"
    )
    assert _records_under_the_arm_id(call), (
        "_measure_detection must record under (REUSE_ROLE, arm.fixture_id): "
        "fixture.fixture_id collapses both arms onto one baseline key and "
        "newly_missed then reports nothing"
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

    ``test_the_paid_arm_test_records_under_these_ids`` is what proves the
    ids survive a fresh interpreter; this test builds baselines from
    ``arm_params`` directly and does not re-spawn a process to check it.
    """

    def records(detected_present: bool) -> list[dict[str, object]]:
        return [
            {
                "role": REUSE_ROLE,
                "fixture_id": arm.fixture_id,
                "category": arm.name,
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
        calibration_baseline.load_baseline(old),
        calibration_baseline.load_baseline(new),
    )
    present = next(a for a in fixture.arms if a.expect_module_named)
    assert f"{REUSE_ROLE}/{present.fixture_id}" in comparison.newly_missed
    # The CATEGORY drop is the mechanism that fails a comparison, not the
    # role floor: one arm of two that stops being detected leaves the
    # role rate at 0.50, which is not BELOW the 0.50 default, so a floor
    # failure never fires on this shape. Pin the failure that does.
    assert any(
        failure.startswith(f"category {REUSE_ROLE}/{present.name} ")
        for failure in comparison.failures
    ), comparison.failures


def _specs_conftest_collect_ignore_glob() -> list[str]:
    """The actual ``collect_ignore_glob`` list from
    ``tests/adversarial_fixtures/specs/conftest.py``, read from the file
    rather than restated (#401 addendum C5): ``REPO_DIR_SUFFIX`` claims
    to be what that glob uses, and this is what makes the claim checked
    instead of two spellings that could silently drift apart.
    """
    conftest_path = SPECS_DIR / "conftest.py"
    spec = importlib.util.spec_from_file_location("_specs_conftest_probe", conftest_path)
    assert spec is not None and spec.loader is not None, f"cannot load {conftest_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.collect_ignore_glob)


def test_every_repo_directory_under_specs_is_a_declared_fixture() -> None:
    """Both sides of ``specs/conftest.py``'s ignore glob: every declared
    repository is covered by it, it covers nothing else, and the glob it
    actually uses is the one ``REPO_DIR_SUFFIX`` claims (#401 addendum
    C5)."""
    declared = {f.repo_dir for f in FIXTURES}
    assert all(d.name.endswith(REPO_DIR_SUFFIX) for d in declared), declared
    on_disk = {p for p in SPECS_DIR.glob(f"*{REPO_DIR_SUFFIX}") if p.is_dir()}
    assert on_disk == declared
    assert _specs_conftest_collect_ignore_glob() == [f"*{REPO_DIR_SUFFIX}"]


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
    # 5 is pytest's EXIT_NOTESTSCOLLECTED, which already means no tests
    # were collected; a collection failure exits 2, not 5. Checking the
    # code alone is stronger than also scanning the text for "error" or
    # "no tests collected": either substring can appear in an unrelated
    # warning and would silently pass regardless (#401 addendum C4).
    assert proc.returncode == 5, f"collector exited {proc.returncode}\n{combined}"
