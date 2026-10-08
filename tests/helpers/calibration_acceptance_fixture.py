"""The verification designer's calibration fixtures (#700 slice 7).

A fixture is a directory under ``tests/adversarial_fixtures/acceptance/``:
``meta.json`` (the component, its criteria, the ``[stack]`` and what each
planted head gets wrong), ``spec.md``, ``base/`` (the repository before the
change), ``heads/<name>/`` (each laid over the base as its own commit:
``correct`` and the planted ones), and ``reference.json``, a plan entry an
operator wrote that tells every planted head from the correct one.

A run asks the designer through
:func:`kstrl.acceptance_design.design_component` (the call the factory
makes) in a repository that holds the base commit and nothing else
(:func:`base_checkout`), so no branch or commit of a head is in the
repository it reads. It then materialises the fixture with every head as a second
repository and scores the entry by running it:
:func:`kstrl.acceptance.replay_base` on the base, then
:func:`kstrl.acceptance.judge_head` on each head, which is the factory's
own reading. No model judges a check.

Planted heads record under :data:`ACCEPTANCE_ROLE`: a run catches the head
when the plan held on the base and at least one check failed on it (a
check that did not run is never a failure). The correct head records under
:data:`ACCEPTANCE_CLEAN_ROLE`: a run counts when the plan held on the base
and every check passed every run on it.
"""

from __future__ import annotations

import io
import json
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl.acceptance import (
    DESIGNER_FILE,
    FAIL,
    PASS,
    RECORD_FILE,
    evidence_dir,
    judge_head,
    pin_plan,
    replay_base,
)
from kstrl.acceptance_design import build_design_prompt, design_component
from kstrl.factory import FactoryConfig
from kstrl.manifest import Component, Manifest
from kstrl.stack import Stack
from kstrl.ui.plain import PlainUI
from tests.helpers.calibration_repo_fixture import FIXTURES_DIR
from tests.helpers.gitrepo import GIT_TIMEOUT_SECONDS, git_in, set_identity

ACCEPTANCE_FIXTURES_DIR = FIXTURES_DIR / "acceptance"

#: The role a planted head records under: "caught" is at least one red check.
ACCEPTANCE_ROLE = "acceptance"

#: The role the correct head records under: "caught" is every check green.
ACCEPTANCE_CLEAN_ROLE = "acceptance_clean"

CORRECT = "correct"

_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc")


@dataclass(frozen=True)
class AcceptanceFixture:
    directory: Path
    meta: dict[str, Any]

    @property
    def fixture_id(self) -> str:
        return str(self.meta["fixture_id"])

    @property
    def component(self) -> Component:
        return Component(
            id=str(self.meta["component"]),
            title=str(self.meta["title"]),
            description=str(self.meta["description"]),
            dependencies=[],
            prd_path="",
            branch_name="",
        )

    @property
    def criteria(self) -> list[str]:
        return [str(line) for line in self.meta["criteria"]]

    @property
    def spec(self) -> str:
        return (self.directory / "spec.md").read_text(encoding="utf-8")

    @property
    def stack(self) -> Stack:
        """The fixture's ``[stack]``, confirmed: its own checks are never run."""
        stack = self.meta["stack"]
        return Stack(
            instructions=str(stack["instructions"]),
            setup=str(stack["setup"]),
            checks=(("tests", "true"),),
            env=(),
            up=str(stack["up"]),
            unconfirmed="",
        )

    @property
    def planted(self) -> tuple[str, ...]:
        """The planted heads, each named in ``meta.json`` with what it gets wrong."""
        return tuple(sorted(self.meta["planted"]))

    @property
    def heads(self) -> tuple[str, ...]:
        return (CORRECT, *self.planted)

    @property
    def reference(self) -> dict[str, Any]:
        entry: dict[str, Any] = json.loads(
            (self.directory / "reference.json").read_text(encoding="utf-8")
        )
        return entry


def load_acceptance_fixtures() -> list[AcceptanceFixture]:
    """Every fixture, in directory-name order."""
    return [
        AcceptanceFixture(path.parent, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(ACCEPTANCE_FIXTURES_DIR.glob("*/meta.json"))
    ]


@dataclass(frozen=True)
class FixtureRepo:
    path: Path
    base_sha: str
    heads: dict[str, str]


def _rev(repo: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return done.stdout.strip()


def base_checkout(fixture: AcceptanceFixture, dest: Path) -> Path:
    """A git repository at ``dest`` holding the base as its only commit: where
    the designer is asked. The heads are never committed here, so a designer
    that reads git history or other branches finds no implementation."""
    shutil.copytree(fixture.directory / "base", dest, ignore=_IGNORED)
    git_in(dest, "init", "-q", "-b", "main")
    set_identity(dest)
    git_in(dest, "add", "-A")
    git_in(dest, "commit", "-q", "-m", "base")
    return dest


def materialize(fixture: AcceptanceFixture, dest: Path) -> FixtureRepo:
    """A git repository at ``dest``: ``main`` holds the base, and each head
    is one commit on its own branch from it. ``main`` is checked out."""
    base_checkout(fixture, dest)
    base_sha = _rev(dest)
    heads: dict[str, str] = {}
    for head in fixture.heads:
        git_in(dest, "checkout", "-q", "-b", f"head-{head}", "main")
        shutil.copytree(
            fixture.directory / "heads" / head, dest, ignore=_IGNORED, dirs_exist_ok=True
        )
        git_in(dest, "add", "-A")
        git_in(dest, "commit", "-q", "-m", head)
        heads[head] = _rev(dest)
        git_in(dest, "checkout", "-q", "main")
    return FixtureRepo(dest, base_sha, heads)


def acceptance_slot(fixture: AcceptanceFixture, tmp_path: Path) -> Path:
    """A fresh directory for ONE run of one fixture: the runs of a gate share
    ``tmp_path`` and can operate at the same time (#750), so the name comes
    from ``mkdtemp``, not from a count (``calibration_integration_fixture.run_slot``'s rule)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{fixture.fixture_id}-", dir=tmp_path))


def _no_budget() -> str:
    """A calibration run has no adversarial call budget: every ask is made."""
    return ""


def design(
    fixture: AcceptanceFixture, agent: Any, cwd: Path, *, timeout: float | None
) -> tuple[dict[str, Any] | None, int, list[str]]:
    """The designer's entry for ``fixture``, asked as the factory asks it, in
    ``cwd``, which must be a :func:`base_checkout` and never the scored
    repository (that one holds the heads)."""
    # #639 slice 5: no fixture carries requirements, so the prompt lists none
    # and no criterion has to cite one.
    prompt = build_design_prompt(
        fixture.component, fixture.criteria, fixture.spec, fixture.stack, requirements=()
    )
    return design_component(
        agent,
        prompt,
        cwd,
        fixture.component.id,
        timeout=timeout,
        spend=_no_budget,
        requirement_ids=(),
    )


@dataclass(frozen=True)
class Scored:
    """What running one plan entry found: why the base refused it, or each
    head's verdict per check id."""

    refused: list[str] = field(default_factory=list)
    verdicts: dict[str, dict[str, str]] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)


def score(
    fixture: AcceptanceFixture, repo: FixtureRepo, entry: Any, plan_dir: Path, *, designed: bool
) -> Scored:
    """Run ``entry`` the way kstrl runs a plan: pinned, replayed on the base,
    then judged on every head. ``plan_dir`` must not exist and must be outside
    the repository. ``designed`` marks the plan as the verification
    designer's, as ``ks factory --design-acceptance`` does: the base then
    removes a designed check that passes there and keeps the others (#700,
    owner decision of 2026-10-06), where an operator's plan is refused. A
    designed plan left with no check scores as refused."""
    comp = fixture.component
    plan_dir.mkdir(parents=True)
    plan = {"components": {comp.id: entry}}
    (plan_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    if designed:
        (plan_dir / DESIGNER_FILE).write_text(json.dumps({"calibration": True}), encoding="utf-8")
    manifest = Manifest("1", "", fixture.fixture_id, "main", False, [comp])
    stack = fixture.stack
    pinned, refused = pin_plan(repo.path, manifest, str(plan_dir), stack)
    if pinned is None:
        return Scored(refused)
    config = FactoryConfig()
    config.project_stack = stack
    config.acceptance_plan = pinned
    ui = PlainUI(no_color=True, file=io.StringIO())
    run_id = f"calibration-{secrets.token_hex(4)}"
    config.acceptance_base, refused = replay_base(
        repo.path, stack, pinned, "main", run_id, config, ui
    )
    if refused:
        return Scored(refused)
    base = config.acceptance_base
    assert base is not None
    kept = base.kept.get(comp.id)
    removed = sorted(
        {c.id for c in pinned.components[comp.id].checks} - {c.id for c in kept.checks}
        if kept is not None
        else {c.id for c in pinned.components[comp.id].checks}
    )
    if kept is None:
        return Scored([f"{comp.id}: {base.said.get(comp.id, 'no check left')}"], removed=removed)
    verdicts: dict[str, dict[str, str]] = {}
    for attempt, head in enumerate(fixture.heads, start=1):
        judge_head(
            repo.path, config, comp.id, repo.heads[head], run_id=run_id, attempt=attempt, ui=ui
        )
        path = evidence_dir(repo.path, run_id) / comp.id / f"attempt-{attempt}" / RECORD_FILE
        record = json.loads(path.read_text(encoding="utf-8"))
        verdicts[head] = {row["id"]: row["verdict"] for row in record["checks"]}
    return Scored([], verdicts, removed)


def caught(scored: Scored, head: str) -> tuple[bool, str]:
    """``(caught, detail)`` for a planted head: the plan held on the base
    and at least one check failed on the head."""
    if scored.refused:
        return False, "the base refused the plan: " + "; ".join(scored.refused)
    red = sorted(check for check, verdict in scored.verdicts[head].items() if verdict == FAIL)
    return bool(red), f"{head}: failed {red}; verdicts {scored.verdicts[head]}"


def clean(scored: Scored) -> tuple[bool, str]:
    """``(clean, detail)`` for the correct head: the plan held on the base
    and every check passed every run on it."""
    if scored.refused:
        return False, "the base refused the plan: " + "; ".join(scored.refused)
    verdicts = scored.verdicts[CORRECT]
    return all(v == PASS for v in verdicts.values()), f"{CORRECT}: verdicts {verdicts}"
