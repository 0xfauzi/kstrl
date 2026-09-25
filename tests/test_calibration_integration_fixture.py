"""The integration review calibration fixtures (#482; design #480 section 8, Layer B).

Every test here runs without KSTRL_RUN_CALIBRATION and makes no LLM call.
They check that the fixtures can measure something, and that the paid tests
score what section 8 says they score:

- the nine fixtures section 8 lists exist, each with the story it grades
  and the components a detection must cite;
- each fixture materialises as a two-commit repository the factory's own
  base resolution and diff measurement accept;
- each positive holds its planted defect at the stated location, and each
  clean twin is its positive without it;
- driven end to end through the real ``run_review`` and
  ``integration_outcome`` by a scripted reviewer, a detection needs the
  right story to fail AND a cited file in every named component, and a
  clean twin passes only when the round opens nothing and is not red;
- the paid tests themselves, called with a scripted reviewer in place of
  the real one, gate on the floors in ``kstrl.calibration`` and record
  under the fixture ids.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from kstrl import calibration, calibration_baseline, git
from kstrl.agents.proc import TIMEOUT_MESSAGE_PREFIX
from kstrl.integration import integration_stories
from kstrl.review import ReviewResult
from tests import test_calibration
from tests.helpers.calibration_integration_fixture import (
    INTEGRATION_CLEAN_ROLE,
    INTEGRATION_DIR,
    INTEGRATION_ROLE,
    KIND_CLEAN,
    KIND_POSITIVE,
    REPO_DIR_SUFFIX,
    BoundedAgent,
    IntegrationFixture,
    agent_failure,
    detected,
    integration_clean_twins,
    integration_positives,
    load_integration_fixtures,
    materialize,
    opened_nothing,
    review_fixture,
    run_slot,
)
from tests.helpers.gitrepo import GIT_TIMEOUT_SECONDS

#: Design #480 section 8: F-D1, F-D2, F-D3, F-D3-clean, F-D4, F-D5 "and
#: three clean twins". The three are the twins of the other code stories
#: (IC2, IC1, IC4); IC3's is F-D3-clean, and IC5 has none because a
#: register finding never builds a fix component.
DESIGN_LABELS = {
    "F-D1",
    "F-D2",
    "F-D3",
    "F-D3-clean",
    "F-D4",
    "F-D5",
    "F-D1-clean",
    "F-D2-clean",
    "F-D5-clean",
}

FIXTURES = load_integration_fixtures()
BY_ID = {f.fixture_id: f for f in FIXTURES}
ALL = [pytest.param(f, id=f.fixture_id) for f in FIXTURES]
POSITIVES = [pytest.param(f, id=f.fixture_id) for f in integration_positives()]
CLEANS = [pytest.param(f, id=f.fixture_id) for f in integration_clean_twins()]
STORY_IDS = {s.story_id for s in integration_stories("0" * 40)}


def _root_commit(repo: Path) -> str:
    done = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return done.stdout.strip()


def _reply(repo: Path, fails: Mapping[str, str]) -> dict[str, Any]:
    """A reviewer reply for the fixture repository at ``repo``: every
    integration story passes except those in ``fails`` (story id to
    explanation); an id in ``fails`` that is no integration story is
    answered as an extra story, which ``integration_outcome`` reads as red.
    The diffstat is the one git measures, so the coverage check passes."""
    base = _root_commit(repo)
    stat = git.get_diff_stat(base, repo, resolved=True)
    stories = integration_stories(base)
    known = {s.story_id for s in stories}
    entries = [
        {
            "storyId": s.story_id,
            "storyTitle": s.title,
            "criteria": [
                {
                    "criterion": s.criterion,
                    "verdict": "fail" if s.story_id in fails else "pass",
                    "explanation": fails.get(s.story_id, "read and checked"),
                    "suggestion": "",
                }
            ],
        }
        for s in stories
    ]
    entries += [
        {
            "storyId": sid,
            "storyTitle": sid,
            "criteria": [
                {"criterion": sid, "verdict": "fail", "explanation": text, "suggestion": ""}
            ],
        }
        for sid, text in fails.items()
        if sid not in known
    ]
    return {
        "observedDiffstat": {
            "files": stat.files,
            "insertions": stat.insertions,
            "deletions": stat.deletions,
        },
        "stories": entries,
        "concerns": [],
        "exhaustively_searched": True,
        "overallNotes": "",
    }


class ScriptedReviewer:
    """Stands in for the reviewer CLI. Run N replies with plan N (the last
    plan repeats), and records the timeout each run was given."""

    name = "scripted-integration-reviewer"

    def __init__(self, *plans: Mapping[str, str]) -> None:
        self._plans = plans or ({},)
        self.final_message: str | None = None
        self.timeouts: list[float | None] = []

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        assert cwd is not None, "the integration review must run inside the repository"
        plan = self._plans[min(len(self.timeouts), len(self._plans) - 1)]
        self.timeouts.append(timeout)
        yield json.dumps(_reply(cwd, plan))


class CrashingReviewer:
    name = "crashing-integration-reviewer"
    final_message: str | None = None

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        raise OSError("reviewer CLI not found")
        yield ""  # pragma: no cover - makes this a generator, as every adapter is


class TimedOutReviewer:
    name = "timed-out-integration-reviewer"
    final_message: str | None = None

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        yield f"{TIMEOUT_MESSAGE_PREFIX} after {timeout}s"


def _cite_all(fixture: IntegrationFixture) -> str:
    """An explanation naming the first file of every component."""
    return "defect at " + " and ".join(f"{paths[0]}:1" for paths in fixture.components.values())


def _review(fixture: IntegrationFixture, tmp_path: Path, *plans: Mapping[str, str]):
    return review_fixture(fixture, ScriptedReviewer(*plans), run_slot(fixture, tmp_path))


# --------------------------------------------------------------- the set


def test_the_fixtures_are_the_nine_section_8_lists() -> None:
    assert {f.design_label for f in FIXTURES} == DESIGN_LABELS
    assert len(BY_ID) == len(FIXTURES), "two fixtures share a fixture_id"


@pytest.mark.parametrize("fixture", ALL)
def test_every_meta_names_what_its_kind_needs(fixture: IntegrationFixture) -> None:
    assert fixture.kind in (KIND_POSITIVE, KIND_CLEAN)
    expected_role = INTEGRATION_ROLE if fixture.kind == KIND_POSITIVE else INTEGRATION_CLEAN_ROLE
    assert fixture.role == expected_role
    assert fixture.story in STORY_IDS, fixture.story
    assert fixture.components and all(fixture.components.values()), fixture.components
    assert fixture.repo_dir.name == f"{fixture.meta_path.name.removesuffix('.meta.json')}_repo"
    assert (fixture.repo_dir / "base").is_dir() and (fixture.repo_dir / "feature").is_dir()
    if fixture.kind == KIND_POSITIVE:
        assert fixture.planted, "a positive states where its defect is planted"
        assert fixture.twin_of is None
    else:
        assert not fixture.planted
        twin = BY_ID[str(fixture.twin_of)]
        assert twin.kind == KIND_POSITIVE
        assert (twin.story, twin.components) == (fixture.story, fixture.components)


# --------------------------------------------------------------- the trees


@pytest.mark.parametrize("fixture", ALL)
def test_the_repository_is_one_feature_commit_on_its_base(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    """The factory resolves ``featureBaseSha`` through ``git.resolve_base_sha``
    and measures the change with ``git.get_diff_stat``; both must accept the
    materialised repository, and the change must not be empty."""
    repo = materialize(fixture, tmp_path / "repo")
    assert repo.base_sha != repo.head_sha
    assert git.resolve_base_sha(repo.base_sha, repo.path) == repo.base_sha
    assert git.get_diff_stat(repo.base_sha, repo.path, resolved=True).files > 0
    for paths in fixture.components.values():
        for path in paths:
            assert (repo.path / path).is_file(), f"component file {path} is not in the tree"


@pytest.mark.parametrize("fixture", POSITIVES)
def test_each_planted_defect_is_at_its_stated_location(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    repo = materialize(fixture, tmp_path / "repo")
    for path, text in fixture.planted:
        assert text in (repo.path / path).read_text(encoding="utf-8"), (path, text)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


@pytest.mark.parametrize("fixture", CLEANS)
def test_each_clean_twin_is_its_positive_without_the_defect(fixture: IntegrationFixture) -> None:
    """Same base, same files, and none of the positive's planted text: the
    delta between a positive and its twin is the defect."""
    positive = BY_ID[str(fixture.twin_of)]
    assert _tree(fixture.repo_dir / "base") == _tree(positive.repo_dir / "base")
    twin_feature = _tree(fixture.repo_dir / "feature")
    assert twin_feature.keys() == _tree(positive.repo_dir / "feature").keys()
    for path, text in positive.planted:
        head = fixture.repo_dir / "feature" / path
        if not head.is_file():
            head = fixture.repo_dir / "base" / path
        assert text not in head.read_text(encoding="utf-8"), (path, text)


def _run_in(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``python <args>`` inside a materialised fixture repository, with its
    ``src/`` on the path, as its own tests expect."""
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env.update(PYTHONPATH="src", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, *args],
        cwd=repo,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.mark.parametrize("fixture", ALL)
def test_each_fixture_s_own_tests_pass_at_its_head(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    """The defects escape the per-component tests, as #453's did; a fixture
    whose own tests fail hands the reviewer a second, louder defect."""
    repo = materialize(fixture, tmp_path / "repo")
    done = _run_in(repo.path, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests")
    assert done.returncode == 0, done.stdout + done.stderr
    assert " passed" in done.stdout, done.stdout


#: The two defects that are behaviour rather than text. Each probe runs in
#: the materialised repository and prints one line; the positive prints the
#: first value and its clean twin the second.
_BEHAVIOUR_PROBES = {
    "int-d1-stored-rows": (
        "from datetime import UTC, datetime\n"
        "from pastebin.snippets import SnippetError\n"
        "from pastebin.storage import SnippetStore\n"
        "store = SnippetStore(':memory:')\n"
        "created = datetime(2026, 1, 1, tzinfo=UTC)\n"
        "store._db.execute('INSERT INTO snippets VALUES (?, ?, ?, ?, ?)', ('old', 't', 'b', "
        "created.isoformat(), created.replace(month=2, day=1).isoformat()))\n"
        "try:\n"
        "    print(f'listed {len(store.list_all())}')\n"
        "except SnippetError:\n"
        "    print('refused')\n",
        "refused",
        "listed 1",
    ),
    "int-d5-predates-feature": (
        "from pastebin.client import ClientError, load_token\n"
        "try:\n"
        "    print(f\"loaded {load_token({'PASTEBIN_TOKEN': 'abc123'})}\")\n"
        "except ClientError:\n"
        "    print('refused')\n",
        "refused",
        "loaded abc123",
    ),
}


@pytest.mark.parametrize("positive_id", sorted(_BEHAVIOUR_PROBES))
def test_a_behaviour_defect_is_in_the_positive_and_not_in_its_twin(
    positive_id: str, tmp_path: Path
) -> None:
    """F-D1: a row stored under the older, wider expiry rule makes the whole
    list raise, and its twin lists it. F-D5: a valid bare token is refused,
    and its twin loads it. The text checks above cannot see a defect whose
    marker text survives an edit that removes it."""
    probe, in_positive, in_twin = _BEHAVIOUR_PROBES[positive_id]
    twin = next(f for f in FIXTURES if f.twin_of == positive_id)
    for fixture, expected in ((BY_ID[positive_id], in_positive), (twin, in_twin)):
        repo = materialize(fixture, tmp_path / fixture.fixture_id)
        done = _run_in(repo.path, "-c", probe)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == expected, (fixture.fixture_id, done.stdout, done.stderr)


def _integration_conftest_glob() -> list[str]:
    conftest_path = INTEGRATION_DIR / "conftest.py"
    spec = importlib.util.spec_from_file_location("_integration_conftest_probe", conftest_path)
    assert spec is not None and spec.loader is not None, f"cannot load {conftest_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.collect_ignore_glob)


def test_every_repo_directory_is_a_declared_fixture_and_is_not_collected() -> None:
    """Both sides of ``integration/conftest.py``'s ignore glob. The real
    collector over the whole fixture tree is
    ``test_calibration_repo_fixture.py::test_the_fixture_repositories_are_not_collected_by_our_own_suite``."""
    on_disk = {p for p in INTEGRATION_DIR.glob(f"*{REPO_DIR_SUFFIX}") if p.is_dir()}
    assert on_disk == {f.repo_dir for f in FIXTURES}
    assert _integration_conftest_glob() == [f"*{REPO_DIR_SUFFIX}"]


# --------------------------------------------------------------- the scoring


@pytest.mark.parametrize("fixture", POSITIVES)
def test_a_failed_story_citing_every_component_is_a_detection(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    review_round = _review(fixture, tmp_path, {fixture.story: _cite_all(fixture)})
    caught, detail = detected(fixture, review_round)
    assert caught, detail


@pytest.mark.parametrize("fixture", POSITIVES)
def test_a_review_that_passes_every_story_is_a_miss(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    caught, detail = detected(fixture, _review(fixture, tmp_path, {}))
    assert not caught
    assert f"{fixture.story} did not fail" in detail


@pytest.mark.parametrize("fixture", POSITIVES)
def test_a_finding_that_cites_one_component_is_not_a_detection(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    first = next(iter(fixture.components.values()))[0]
    review_round = _review(fixture, tmp_path, {fixture.story: f"defect at {first}:1"})
    caught, detail = detected(fixture, review_round)
    assert not caught
    assert "cited no file of" in detail, detail


@pytest.mark.parametrize("fixture", POSITIVES)
def test_the_wrong_story_failing_is_not_a_detection(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    other = sorted(STORY_IDS - {fixture.story})[0]
    caught, _detail = detected(fixture, _review(fixture, tmp_path, {other: _cite_all(fixture)}))
    assert not caught


@pytest.mark.parametrize("fixture", POSITIVES)
def test_a_red_round_is_not_a_detection(fixture: IntegrationFixture, tmp_path: Path) -> None:
    plan = {fixture.story: _cite_all(fixture), "IC9": "an extra story"}
    caught, detail = detected(fixture, _review(fixture, tmp_path, plan))
    assert not caught
    assert detail.startswith("red: "), detail


@pytest.mark.parametrize("fixture", CLEANS)
def test_a_review_that_opens_nothing_is_clean(fixture: IntegrationFixture, tmp_path: Path) -> None:
    clean, detail = opened_nothing(_review(fixture, tmp_path, {}))
    assert clean, detail


@pytest.mark.parametrize("fixture", CLEANS)
def test_a_finding_on_a_clean_twin_is_not_clean(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    clean, detail = opened_nothing(_review(fixture, tmp_path, {fixture.story: _cite_all(fixture)}))
    assert not clean
    assert detail.startswith("opened "), detail


@pytest.mark.parametrize("fixture", CLEANS)
def test_a_red_round_on_a_clean_twin_is_not_clean(
    fixture: IntegrationFixture, tmp_path: Path
) -> None:
    clean, detail = opened_nothing(_review(fixture, tmp_path, {"IC9": "an extra story"}))
    assert not clean
    assert detail.startswith("red: "), detail


# --------------------------------------------------------------- the agent


def test_the_hang_bound_reaches_the_agent(tmp_path: Path) -> None:
    """The factory passes the reviewer no timeout; the calibration harness
    must, or one wedged CLI hangs the capture."""
    fixture = FIXTURES[0]
    inner = ScriptedReviewer({})
    agent = BoundedAgent(inner, 123.0)
    review_round = review_fixture(fixture, agent, run_slot(fixture, tmp_path))
    assert inner.timeouts == [123.0]
    assert agent_failure(agent, review_round.result) == ""


@pytest.mark.parametrize(
    "reviewer", [CrashingReviewer(), TimedOutReviewer()], ids=["crash", "timeout"]
)
def test_an_agent_that_produced_no_review_is_reported_as_unavailable(
    reviewer: Any, tmp_path: Path
) -> None:
    fixture = FIXTURES[0]
    agent = BoundedAgent(reviewer, 5.0)
    review_round = review_fixture(fixture, agent, run_slot(fixture, tmp_path))
    assert review_round.outcome.errors, "run_review reads a dead agent as an infrastructure error"
    assert agent_failure(agent, review_round.result) != ""


def test_an_agent_that_raises_any_exception_is_reported_as_unavailable(tmp_path: Path) -> None:
    """run_review reads ANY exception from the agent as an infrastructure
    error, so the harness must record any exception, not only OSError, or a
    crashed run is scored as a miss instead of being excluded."""

    class RaisingReviewer:
        name = "raising-integration-reviewer"
        final_message: str | None = None

        def run(
            self, prompt: str, cwd: Path | None = None, timeout: float | None = None
        ) -> Iterator[str]:
            raise RuntimeError("reviewer exited 1")
            yield ""  # pragma: no cover - makes this a generator, as every adapter is

    fixture = FIXTURES[0]
    agent = BoundedAgent(RaisingReviewer(), 5.0)
    review_round = review_fixture(fixture, agent, run_slot(fixture, tmp_path))
    assert review_round.outcome.errors, "run_review reads a dead agent as an infrastructure error"
    assert agent_failure(agent, review_round.result) == (
        "agent raised RuntimeError: reviewer exited 1"
    )


def test_a_review_that_never_reached_the_agent_is_a_harness_fault() -> None:
    agent = BoundedAgent(ScriptedReviewer({}), 5.0)
    result = ReviewResult(passed=False, mode="hard", overall_notes="base did not resolve")
    with pytest.raises(AssertionError, match="never reached the agent"):
        agent_failure(agent, result)


# --------------------------------------------------------------- the paid tests


@pytest.fixture
def paid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """What a paid test needs, with the reviewer agent replaced: set
    ``state["reviewer"]`` before calling the paid test. The report writes
    under ``tmp_path``, never into the saved baselines."""
    state: dict[str, Any] = {"reviewer": ScriptedReviewer({})}
    monkeypatch.setattr(
        test_calibration, "_get_reviewer_calibration_agent", lambda: state["reviewer"]
    )
    monkeypatch.setattr(test_calibration, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(test_calibration, "CALIBRATION_RUNS", 3)
    state["report"] = test_calibration._DetectionReport()
    state["runs"] = tmp_path / "runs"
    yield state


def test_the_paid_tests_record_under_the_fixture_ids() -> None:
    assert [p.id for p in test_calibration.INTEGRATION_POSITIVE_PARAMS] == [
        f.fixture_id for f in integration_positives()
    ]
    assert [p.id for p in test_calibration.INTEGRATION_CLEAN_PARAMS] == [
        f.fixture_id for f in integration_clean_twins()
    ]
    assert (
        test_calibration.INTEGRATION_POSITIVE_PARAMS and test_calibration.INTEGRATION_CLEAN_PARAMS
    )


@pytest.mark.parametrize("fixture", POSITIVES)
def test_the_paid_positive_test_passes_on_a_reviewer_that_detects(
    fixture: IntegrationFixture, paid: dict[str, Any]
) -> None:
    paid["reviewer"] = ScriptedReviewer({fixture.story: _cite_all(fixture)})
    test_calibration.test_integration_review_detects_planted_defect(
        fixture, paid["runs"], paid["report"]
    )
    records = paid["report"].records
    assert [(r["role"], r["fixture_id"], r["category"], r["caught"]) for r in records] == [
        (INTEGRATION_ROLE, fixture.fixture_id, fixture.story, True)
    ] * 3
    assert paid["reviewer"].timeouts == [test_calibration.AGENT_RUN_TIMEOUT_S] * 3


def test_the_paid_positive_test_fails_on_a_reviewer_that_detects_once(
    paid: dict[str, Any],
) -> None:
    """One detection in three is 0.33, under the 0.65 floor."""
    fixture = integration_positives()[0]
    paid["reviewer"] = ScriptedReviewer({fixture.story: _cite_all(fixture)}, {})
    with pytest.raises(AssertionError, match="consistency 1/3"):
        test_calibration.test_integration_review_detects_planted_defect(
            fixture, paid["runs"], paid["report"]
        )


@pytest.mark.parametrize("fixture", CLEANS)
def test_the_paid_clean_test_passes_on_a_reviewer_that_opens_nothing(
    fixture: IntegrationFixture, paid: dict[str, Any]
) -> None:
    test_calibration.test_integration_review_opens_nothing_on_a_clean_twin(
        fixture, paid["runs"], paid["report"]
    )
    records = paid["report"].records
    assert [(r["role"], r["fixture_id"], r["category"], r["caught"]) for r in records] == [
        (INTEGRATION_CLEAN_ROLE, fixture.fixture_id, fixture.story, True)
    ] * 3


def test_one_finding_in_three_runs_fails_the_clean_twin(paid: dict[str, Any]) -> None:
    """Two clean runs of three is a majority, and still a failure: section 8
    allows a clean twin no finding in any run."""
    fixture = integration_clean_twins()[0]
    paid["reviewer"] = ScriptedReviewer({}, {fixture.story: _cite_all(fixture)}, {})
    with pytest.raises(AssertionError, match="consistency 2/3"):
        test_calibration.test_integration_review_opens_nothing_on_a_clean_twin(
            fixture, paid["runs"], paid["report"]
        )


def test_a_crashing_agent_is_excluded_not_scored(paid: dict[str, Any]) -> None:
    fixture = integration_positives()[0]
    paid["reviewer"] = CrashingReviewer()
    with pytest.raises(pytest.skip.Exception, match="agent unavailable for all 3 runs"):
        test_calibration.test_integration_review_detects_planted_defect(
            fixture, paid["runs"], paid["report"]
        )
    assert [r["error"] for r in paid["report"].records] == [True] * 3


# --------------------------------------------------------------- the floors


def test_the_floors_are_section_8s_acceptance() -> None:
    assert calibration.min_role_rate(INTEGRATION_ROLE) == 0.65
    assert calibration.min_role_rate(INTEGRATION_CLEAN_ROLE) == 1.0


def test_compare_fails_a_clean_twin_that_opened_a_finding_once(tmp_path: Path) -> None:
    """The saved-baseline comparison H2 runs applies the same floor: a new
    baseline whose clean twin was clean in two runs of three is a
    regression, though two of three is a majority."""
    fixture = integration_clean_twins()[0]

    def report(caught: list[bool], stamp: str) -> Path:
        records = [
            {
                "role": INTEGRATION_CLEAN_ROLE,
                "fixture_id": fixture.fixture_id,
                "category": fixture.story,
                "cwe": None,
                "caught": c,
                "error": False,
                "detail": "",
            }
            for c in caught
        ]
        built = calibration.build_report(records, model="m", timestamp=stamp, runs_per_fixture=3)
        return calibration.save_report(built, tmp_path)

    old = report([True, True, True], "20260101-000000")
    new = report([True, False, True], "20260102-000000")
    comparison = calibration.compare_baselines(
        calibration_baseline.load_baseline(old), calibration_baseline.load_baseline(new)
    )
    assert any(
        f.startswith(f"role {INTEGRATION_CLEAN_ROLE!r} detection rate 0.67 is below its floor 1.00")
        for f in comparison.failures
    ), comparison.failures
