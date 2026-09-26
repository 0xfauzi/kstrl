"""The architect calibration fixture that carries a repository (#401).

Every other architect fixture is a standalone ``.md`` rendered against an
empty directory, so the repository-reading half of the architect prompt
never reaches a measured prompt and repository content cannot move a
measured number. A fixture here is a spec PLUS a small real repository,
run twice: once standing in that repository, once with no repository at
all. Both arms are graded on ONE observable, whether the decomposition
names the module the repository already has, with opposite signs: with
the repository that is reuse, without it that is fabrication.

Both arms record under one role (:data:`REUSE_ROLE`) and under stable
fixture ids that come from the fixture's own meta, so two baselines pair
them by ``(role, fixture_id)`` and an arm that stops being detected is a
reported regression rather than a key nothing can compare.

This module owns the prompt and the working directory for each arm, so
the always-run structural tests exercise the same code the paid run does.
"""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.calibration_score import (
    FixtureMetaError,
    check_fixture_meta,
    refuse_unreadable,
    reuse_errors,
)
from kstrl.decompose import build_decompose_prompt

#: Root of every calibration fixture, security and reviewer fixtures
#: included (#401 addendum B2). Lives here, not in tests/test_calibration.py,
#: because tests/helpers/ may not import a tests.test_* module
#: (tests/test_helper_import_direction.py), and this loader is shared.
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "adversarial_fixtures"
SPECS_DIR = FIXTURES_DIR / "specs"

#: The calibration role both arms record under. One role with two
#: fixtures, not two roles: ``compare_baselines`` applies a per-role
#: floor, and the role rate is then the mean of the two arms.
REUSE_ROLE = "architect_reuse"

#: The suffix every fixture repository directory carries, which is what
#: ``specs/conftest.py`` keeps out of collection.
REPO_DIR_SUFFIX = "_repo"


@dataclass(frozen=True)
class Arm:
    """One side of the repository comparison.

    ``name`` is "repo_present" or "repo_absent" (#401 addendum C1: the
    field was called ``arm``, which read as ``arm.arm`` everywhere; the
    meta.json key it is read from stays "arm").
    """

    name: str
    fixture_id: str
    expect_module_named: bool


@dataclass(frozen=True)
class RepoSpecFixture:
    """A spec fixture with a repository beside it."""

    spec_path: Path
    meta: dict[str, Any]

    @property
    def fixture_id(self) -> str:
        return str(self.meta["fixture_id"])

    @property
    def spec_text(self) -> str:
        return self.spec_path.read_text(encoding="utf-8")

    @property
    def repo_dir(self) -> Path:
        return self.spec_path.parent / str(self.meta["repo_dir"])

    @property
    def codebase_map_path(self) -> str:
        """Root-relative, the shape the operator's config carries."""
        return str(self.meta["codebase_map_path"])

    @property
    def codebase_map(self) -> Path:
        return self.repo_dir / self.codebase_map_path

    @property
    def must_reuse(self) -> dict[str, Any]:
        return dict(self.meta["must_reuse"])

    @property
    def arms(self) -> tuple[Arm, ...]:
        arms = tuple(
            Arm(
                name=str(entry["arm"]),
                fixture_id=str(entry["fixture_id"]),
                expect_module_named=entry["expect_module_named"],
            )
            for entry in self.meta["arms"]
        )
        # ``bool("false")`` is True, so a quoted flag used to grade the
        # repo_absent arm as reuse (#564).
        for index, arm in enumerate(arms):
            if not isinstance(arm.expect_module_named, bool):
                raise FixtureMetaError(
                    f"{self.fixture_id}: arms[{index}].expect_module_named: must be true "
                    f"or false, got {arm.expect_module_named!r}"
                )
        return arms


def load_fixtures(subdir: str, suffix: str) -> list[tuple[Path, dict[str, Any]]]:
    """Return list of (artifact_path, meta_dict) for each fixture.

    The one loader every calibration fixture kind uses (#401 addendum
    B2): security, reviewer-concern and spec fixtures alike. Moved here
    from tests/test_calibration.py, which now imports it, rather than
    kept as two implementations of the same glob-plus-meta-lookup.

    Every meta is read by :func:`kstrl.calibration_score.check_fixture_meta`
    as it loads, so one no matcher can read is refused here, naming its
    file and field, before any test is collected or any agent is called
    (#564).
    """
    base = FIXTURES_DIR / subdir
    fixtures: list[tuple[Path, dict[str, Any]]] = []
    for artifact in sorted(base.glob(f"*{suffix}")):
        meta_path = artifact.with_suffix(".meta.json")
        if not meta_path.exists():
            # Try alternate: <stem>.meta.json regardless of artifact suffix
            meta_path = artifact.parent / f"{artifact.stem}.meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        check_fixture_meta(meta, meta_path.relative_to(FIXTURES_DIR).as_posix())
        fixtures.append((artifact, meta))
    return fixtures


def load_repo_spec_fixtures() -> list[RepoSpecFixture]:
    """Every spec fixture graded on reuse, in path order."""
    return [
        RepoSpecFixture(spec_path=spec_path, meta=meta)
        for spec_path, meta in load_fixtures("specs", ".md")
        if "must_reuse" in meta
    ]


def arm_params() -> list[tuple[RepoSpecFixture, Arm]]:
    """Every (fixture, arm) pair, in a stable order.

    The one source of the ids a run records under. The paid arm test and
    the structural tests both parametrize from here, so an id that a
    saved baseline cannot carry is an id both of them see.
    """
    return [(fixture, arm) for fixture in load_repo_spec_fixtures() for arm in fixture.arms]


def arm_prompt(fixture: RepoSpecFixture, arm: Arm) -> str:
    """The architect prompt for one arm.

    Passing ``codebase_map_path`` is what selects the repository branch
    of ``build_decompose_prompt``; passing nothing selects the
    no-repository line. The choice lives here rather than in the paid
    test so the always-run suite can check it.
    """
    if arm.expect_module_named:
        return build_decompose_prompt(
            fixture.fixture_id,
            fixture.spec_text,
            codebase_map_path=fixture.codebase_map_path,
        )
    return build_decompose_prompt(fixture.fixture_id, fixture.spec_text)


def materialize_repo(fixture: RepoSpecFixture, dest: Path) -> Path:
    """Copy the fixture repository to ``dest`` and return it. ``dest``
    must not exist; ``copytree`` raises ``FileExistsError`` otherwise.
    """
    shutil.copytree(fixture.repo_dir, dest)
    return dest


def arm_cwd(fixture: RepoSpecFixture, arm: Arm, tmp_path: Path) -> Path:
    """A FRESH directory for ONE RUN of one arm.

    Called once per run, not once per arm. ``_measure_detection`` calls
    ``run_once`` ``CALIBRATION_RUNS`` times (default 3) against one
    function-scoped ``tmp_path``. One shared directory would hand run 2
    whatever the agent wrote during run 1, so run 1 measures the fixture
    and runs 2 and 3 measure the fixture plus some leftovers, which is a
    number that looks exactly like a measurement. Each call takes its own
    numbered slot under ``tmp_path``; the count comes off the filesystem,
    so nothing here carries process state.
    """
    existing = len([p for p in tmp_path.glob(f"{arm.name}-*") if p.is_dir()])
    slot = tmp_path / f"{arm.name}-{existing}"
    slot.mkdir()
    if arm.expect_module_named:
        return materialize_repo(fixture, slot / "repo")
    return slot


def _strip_field(entries: Any, key: str) -> None:
    """Remove ``key`` from every dict entry of ``entries``, in place.

    Tolerant of ``entries`` not being a list, and of an entry not being
    a dict: this backs a search projection, not a validator.
    """
    if not isinstance(entries, list):
        return
    for entry in entries:
        if isinstance(entry, dict):
            entry.pop(key, None)


def _without_rejected_options(decompose_output: Any) -> Any:
    """A deep copy of ``decompose_output`` with the fields that record a
    REJECTED option removed (#401 addendum A2).

    ``decisions[].alternative`` is the option the architect did NOT take
    (kstrl/decompose.py near line 200) and ``spec_issues[].suggestion`` is
    a proposed resolution the architect raised, not one it acted on.
    Searching either lets naming the existing module as the option it
    rejected score as reuse on the ``repo_present`` arm, which is the
    opposite of what happened. Never raises on a shape it did not
    expect: it is a search projection, not a validator.
    """
    projected = copy.deepcopy(decompose_output)
    if not isinstance(projected, dict):
        return projected
    _strip_field(projected.get("decisions"), "alternative")
    _strip_field(projected.get("spec_issues"), "suggestion")
    return projected


def _is_halted(decompose_output: Any) -> bool:
    """A halt is ``components`` missing or an empty list, paired with an
    escalated decision (kstrl/decompose.py lines 1010-1023). Only the
    ``components`` shape is checked here (#401 addendum A3): the detail
    string says which shape the run was, rather than folding a halt and
    a silent miss into the same "did not name it" message.
    """
    if not isinstance(decompose_output, dict):
        return False
    components = decompose_output.get("components")
    return not isinstance(components, list) or len(components) == 0


def names_existing_module(
    decompose_output: Any,
    must_reuse: dict[str, Any],
) -> tuple[bool, str]:
    """Does the decomposition name the module the repository already has?

    The markers are full module paths, not the class name: a class name
    is guessable from the spec alone, a path inside an invented package
    is not, so only the path distinguishes reading the tree from writing
    down the conventional answer. Searches a projection that has had
    every rejected-option field removed first (#401 addendum A2), so
    naming the module only as an alternative it rejected does not count.
    """
    refuse_unreadable(reuse_errors("must_reuse", must_reuse))
    blob = json.dumps(_without_rejected_options(decompose_output), sort_keys=True)
    for marker in must_reuse["module_markers"]:
        if str(marker) in blob:
            return True, f"names {marker!r}"
    return False, f"names none of {list(must_reuse['module_markers'])!r}"


def reuse_caught(
    decompose_output: Any,
    arm: Arm,
    must_reuse: dict[str, Any],
) -> tuple[bool, str]:
    """``(caught, detail)`` for one arm.

    Repository present: naming the existing module is the reuse the
    fixture measures. Repository absent: the module is not there to be
    read, so naming it is fabrication and silence is the right answer.
    A halted run (``components: []``) scores the same way silence does,
    but the detail is prefixed so a halt is distinguishable from a run
    that simply did not name the module (#401 addendum A3).
    """
    named, detail = names_existing_module(decompose_output, must_reuse)
    caught = named if arm.expect_module_named else not named
    if _is_halted(decompose_output):
        detail = f"halted (no components); {detail}"
    return caught, f"{arm.name}: {detail}"
