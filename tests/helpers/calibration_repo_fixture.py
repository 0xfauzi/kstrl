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
them by ``(role, fixture_id)`` and an arm going dark is a reported
regression rather than a key nothing can compare.

This module owns the prompt and the working directory for each arm, so
the always-run structural tests exercise the same code the paid run does.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.decompose import build_decompose_prompt

SPECS_DIR = Path(__file__).resolve().parents[1] / "adversarial_fixtures" / "specs"

#: The calibration role both arms record under. One role with two
#: fixtures, not two roles: ``compare_baselines`` applies a per-role
#: floor, and the role rate is then the mean of the two arms.
REUSE_ROLE = "architect_reuse"

#: The suffix every fixture repository directory carries, which is what
#: ``specs/conftest.py`` keeps out of collection.
REPO_DIR_SUFFIX = "_repo"


@dataclass(frozen=True)
class Arm:
    """One side of the repository comparison."""

    arm: str
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
        return tuple(
            Arm(
                arm=str(entry["arm"]),
                fixture_id=str(entry["fixture_id"]),
                expect_module_named=bool(entry["expect_module_named"]),
            )
            for entry in self.meta["arms"]
        )


def load_repo_spec_fixtures() -> list[RepoSpecFixture]:
    """Every spec fixture graded on reuse, in path order."""
    fixtures: list[RepoSpecFixture] = []
    for spec_path in sorted(SPECS_DIR.glob("*.md")):
        meta_path = spec_path.with_suffix(".meta.json")
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if "must_reuse" not in meta:
            continue
        fixtures.append(RepoSpecFixture(spec_path=spec_path, meta=meta))
    return fixtures


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
    """Copy the fixture repository to ``dest`` and return it.

    A copy, not the checked-in tree: the calibration agent is not
    read-only, and a run that edits the fixture changes what every later
    run measures.

    ``dest`` must not exist. ``copytree`` raising ``FileExistsError`` is
    the loud version of handing a second run a tree the first one has
    already written into; an earlier draft returned the existing
    directory instead, which is the silent version.
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
    existing = len([p for p in tmp_path.glob(f"{arm.arm}-*") if p.is_dir()])
    slot = tmp_path / f"{arm.arm}-{existing}"
    slot.mkdir()
    if arm.expect_module_named:
        return materialize_repo(fixture, slot / "repo")
    return slot


def names_existing_module(
    decompose_output: Any,
    must_reuse: dict[str, Any],
) -> tuple[bool, str]:
    """Does the decomposition name the module the repository already has?

    The markers are full module paths, not the class name: a class name
    is guessable from the spec alone, a path inside an invented package
    is not, so only the path distinguishes reading the tree from writing
    down the conventional answer.
    """
    blob = json.dumps(decompose_output, sort_keys=True)
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
    """
    named, detail = names_existing_module(decompose_output, must_reuse)
    caught = named if arm.expect_module_named else not named
    return caught, f"{arm.arm}: {detail}"


# --- census helpers used by the map-discrimination test --------------------

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
