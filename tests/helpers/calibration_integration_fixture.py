"""The integration review calibration fixtures (#482; design #480 section 8, Layer B).

A fixture is a ``<stem>.meta.json`` plus a ``<stem>_repo/`` directory holding
two trees: ``base/`` is the repository at the feature base, and ``feature/``
is laid over it as the feature's one commit. A run materialises that as a
real git repository, reviews it through
:func:`kstrl.integration_phase.review_commit` (the call the factory makes)
and reads the result with :func:`kstrl.integration.integration_outcome` (the
only place the design 3.3 rules are written). So the number a calibration
run records is the factory's reading of the reviewer, not a second reading
written for the harness.

Positives record under :data:`INTEGRATION_ROLE`: a run detects the planted
defect when the fixture's story fails and that finding cites a file of every
component the meta names. Clean twins record under
:data:`INTEGRATION_CLEAN_ROLE`: a run counts when the round opens nothing and
is not red. The always-run tests in
``tests/test_calibration_integration_fixture.py`` drive this module with a
scripted reviewer; the paid tests in ``tests/test_calibration.py`` drive it
with the real one.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl import git
from kstrl.agents.proc import TIMEOUT_MESSAGE_PREFIX
from kstrl.integration import (
    ExpectedStory,
    IntegrationOutcome,
    OpenedFinding,
    integration_outcome,
    integration_stories,
    scope_locations,
)
from kstrl.integration_phase import review_commit
from kstrl.review import ReviewResult, normalize_story_id, run_review
from kstrl.ui.plain import PlainUI
from tests.helpers.calibration_repo_fixture import FIXTURES_DIR
from tests.helpers.gitrepo import GIT_TIMEOUT_SECONDS, git_in, set_identity

INTEGRATION_DIR = FIXTURES_DIR / "integration"

#: The role a positive records under. Its floor in
#: ``kstrl.calibration.MIN_ROLE_DETECTION_RATE`` is design section 8's
#: "each positive at or above 0.65".
INTEGRATION_ROLE = "integration"

#: The role a clean twin records under, with "caught" meaning "opened
#: nothing". Its floor is 1.0: section 8 allows no clean twin to open a
#: finding in any run.
INTEGRATION_CLEAN_ROLE = "integration_clean"

KIND_POSITIVE = "positive"
KIND_CLEAN = "clean"

#: What ``integration/conftest.py`` keeps out of collection.
REPO_DIR_SUFFIX = "_repo"

_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc")


@dataclass(frozen=True)
class IntegrationFixture:
    """One Layer B fixture, as its meta file states it."""

    meta_path: Path
    meta: dict[str, Any]

    @property
    def fixture_id(self) -> str:
        return str(self.meta["fixture_id"])

    @property
    def design_label(self) -> str:
        return str(self.meta["design_label"])

    @property
    def role(self) -> str:
        return str(self.meta["role"])

    @property
    def kind(self) -> str:
        return str(self.meta["kind"])

    @property
    def story(self) -> str:
        return str(self.meta["story"])

    @property
    def twin_of(self) -> str | None:
        value = self.meta.get("twin_of")
        return None if value is None else str(value)

    @property
    def repo_dir(self) -> Path:
        return self.meta_path.parent / str(self.meta["repo_dir"])

    @property
    def components(self) -> dict[str, tuple[str, ...]]:
        return {
            str(name): tuple(str(p) for p in paths)
            for name, paths in self.meta["components"].items()
        }

    @property
    def planted(self) -> tuple[tuple[str, str], ...]:
        """``(path, text)``: text the positive holds at ``path`` and its clean twin does not."""
        return tuple((str(e["path"]), str(e["contains"])) for e in self.meta.get("planted", []))


@dataclass(frozen=True)
class FixtureRepo:
    path: Path
    base_sha: str
    head_sha: str


@dataclass(frozen=True)
class FixtureRound:
    """One review of one fixture: what was reviewed, what the reviewer said, and the reading."""

    repo: FixtureRepo
    stories: tuple[ExpectedStory, ...]
    result: ReviewResult
    outcome: IntegrationOutcome
    #: Every file tracked at the reviewed commit, as the factory reads it.
    tracked: frozenset[str]


def load_integration_fixtures() -> list[IntegrationFixture]:
    """Every Layer B fixture, in file-name order."""
    return [
        IntegrationFixture(path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(INTEGRATION_DIR.glob("*.meta.json"))
    ]


def integration_positives() -> list[IntegrationFixture]:
    return [f for f in load_integration_fixtures() if f.kind == KIND_POSITIVE]


def integration_clean_twins() -> list[IntegrationFixture]:
    return [f for f in load_integration_fixtures() if f.kind == KIND_CLEAN]


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


def materialize(fixture: IntegrationFixture, dest: Path) -> FixtureRepo:
    """A git repository at ``dest`` whose ``main`` holds two commits: the
    fixture's ``base/`` tree, then ``feature/`` laid over it. ``dest`` must
    not exist. The reviewer reads a copy, never the checked-in fixture."""
    shutil.copytree(fixture.repo_dir / "base", dest, ignore=_IGNORED)
    git_in(dest, "init", "-q", "-b", "main")
    set_identity(dest)
    git_in(dest, "add", "-A")
    git_in(dest, "commit", "-q", "-m", "base")
    base_sha = _rev(dest)
    shutil.copytree(fixture.repo_dir / "feature", dest, ignore=_IGNORED, dirs_exist_ok=True)
    git_in(dest, "add", "-A")
    git_in(dest, "commit", "-q", "-m", "feature")
    return FixtureRepo(dest, base_sha, _rev(dest))


def run_slot(fixture: IntegrationFixture, tmp_path: Path) -> Path:
    """A fresh directory for ONE run of one fixture.

    The N runs of a consistency gate share one ``tmp_path``; a shared
    repository would hand run 2 whatever run 1 left behind. The slot number
    comes off the filesystem, so nothing here carries process state (the
    #401 ``arm_cwd`` rule)."""
    existing = len([p for p in tmp_path.glob(f"{fixture.fixture_id}-*") if p.is_dir()])
    slot = tmp_path / f"{fixture.fixture_id}-{existing}"
    slot.mkdir(parents=True)
    return slot


class BoundedAgent:
    """The calibration agent, with a hang bound and a record of how its run ended.

    The factory passes the integration reviewer no timeout; the bound is a
    calibration-harness artifact, like ``AGENT_RUN_TIMEOUT_S`` in
    ``tests/test_calibration.py``. ``run_review`` turns an agent crash or a
    timeout into ``infrastructure_error``, which ``integration_outcome``
    reads as red and a positive would score as a miss. This record is what
    lets the harness exclude such a run instead, as every other role does.
    Every other attribute (``name``, ``final_message``) is the wrapped agent's.
    """

    def __init__(self, inner: Any, timeout: float) -> None:
        self._inner = inner
        self._timeout = timeout
        self.started = False
        self.failure = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.started = True
        bound = self._timeout if timeout is None else timeout
        last = ""
        try:
            for line in self._inner.run(prompt, cwd=cwd, timeout=bound):
                last = line
                yield line
        except Exception as exc:
            self.failure = f"agent raised {type(exc).__name__}: {exc}"
            raise
        if last.startswith(TIMEOUT_MESSAGE_PREFIX):
            self.failure = last


def agent_failure(agent: BoundedAgent, result: ReviewResult) -> str:
    """Why the run observed no model output, or "" when it did.

    Raises AssertionError when the review never reached the agent: that is
    a fault in the fixture or in this harness (the base did not resolve, the
    PRD did not load), and excluding it as unavailable would hide it."""
    if not agent.started:
        raise AssertionError(f"the review never reached the agent: {result.overall_notes}")
    return agent.failure


def review_fixture(fixture: IntegrationFixture, agent: Any, slot: Path) -> FixtureRound:
    """One integration review of ``fixture`` in a fresh repository under ``slot``.

    ``integration_outcome`` gets no Phase 3 test result, the value a
    contract-mode-skip run passes: the calibration measures the reviewer."""
    repo = materialize(fixture, slot / "repo")
    stories = integration_stories(repo.base_sha)
    result = review_commit(
        agent,
        run_review,
        repo.path,
        repo.base_sha,
        stories,
        slot / "evidence" / "prd-1.json",
        PlainUI(no_color=True, file=io.StringIO()),
    )
    tracked = git.tracked_files_at(repo.head_sha, repo.path)
    outcome = integration_outcome(None, result, stories, tracked=tracked)
    return FixtureRound(repo, stories, result, outcome, tracked)


def _label(finding: OpenedFinding) -> str:
    return f"{finding.kind}:{finding.story_id or finding.category}"


def cited_files(finding: OpenedFinding, tracked: frozenset[str]) -> tuple[str, ...]:
    """The files a finding names that exist at the reviewed commit.

    For a code finding this is ``finding.locations``, computed by the same
    rule. A register finding (IC5) carries no locations because it is never
    scoped, so the rule is applied to its text here."""
    return scope_locations(f"{finding.text} {finding.suggestion}", tracked)[0]


def detected(fixture: IntegrationFixture, review_round: FixtureRound) -> tuple[bool, str]:
    """``(caught, detail)`` for a positive: the fixture's story failed, and
    that finding cites a file of every component the meta names."""
    outcome = review_round.outcome
    if outcome.errors:
        return False, "red: " + "; ".join(outcome.errors)
    wanted = normalize_story_id(fixture.story)
    failed = [f for f in outcome.opened if f.story_id and normalize_story_id(f.story_id) == wanted]
    if not failed:
        return False, f"{fixture.story} did not fail; opened {[_label(f) for f in outcome.opened]}"
    detail = ""
    for finding in failed:
        cited = cited_files(finding, review_round.tracked)
        uncovered = [
            name for name, paths in fixture.components.items() if not set(paths) & set(cited)
        ]
        if not uncovered:
            return True, f"{fixture.story} failed citing {list(cited)}"
        detail = f"{fixture.story} failed but cited no file of {uncovered}; cited {list(cited)}"
    return False, detail


def opened_nothing(review_round: FixtureRound) -> tuple[bool, str]:
    """``(clean, detail)`` for a clean twin. A red round is not clean: it
    opens nothing, but it is a stop, and an unreadable review cannot show
    that the tree is clean."""
    outcome = review_round.outcome
    if outcome.errors:
        return False, "red: " + "; ".join(outcome.errors)
    if outcome.opened:
        return False, f"opened {[_label(f) for f in outcome.opened]}"
    return True, f"opened nothing; recorded {len(outcome.recorded)}"
