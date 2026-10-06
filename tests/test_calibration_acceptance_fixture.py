"""The verification designer's calibration fixtures score by execution (#700 slice 7).

The paid tests in ``tests/test_calibration.py`` ask a real designer; these
ask a scripted one, through the same call and the same scorer, so a
broken fixture or scorer is found without spending. Each fixture's
reference plan, an operator's checks, must hold on the base, pass the
correct head and fail every planted head, or the fixture cannot tell a
designer that catches the plant from one that does not. A vacuous plan
must score as a miss on every head, never as a catch.

End to end: real git repositories, the real ``acceptance.replay_base``
and ``acceptance.judge_head`` inside the proven rung (on a platform with no
prover, on the host under the fallback label).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.helpers.calibration_acceptance_fixture import (
    CORRECT,
    AcceptanceFixture,
    base_checkout,
    caught,
    clean,
    design,
    load_acceptance_fixtures,
    materialize,
    score,
)
from tests.helpers.recording_agent import RecordingAgent
from tests.test_isolation_rung import runs_a_stack

FIXTURES = load_acceptance_fixtures()


def test_there_are_fixtures_in_two_stacks_with_three_planted_heads_each() -> None:
    """At least two stacks, and each fixture plants a wrong, a special-cased
    and a partial head beside its correct one, each named with what it gets
    wrong."""
    assert len({f.meta["stack"]["instructions"] for f in FIXTURES}) >= 2, FIXTURES
    for fixture in FIXTURES:
        assert fixture.planted == ("partial", "special_cased", "wrong"), fixture.fixture_id
        on_disk = sorted(p.name for p in (fixture.directory / "heads").iterdir())
        assert on_disk == sorted(fixture.heads), fixture.fixture_id


@runs_a_stack
@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.fixture_id for f in FIXTURES])
def test_the_reference_plan_asked_of_a_scripted_designer_catches_every_planted_head(
    fixture: AcceptanceFixture, tmp_path: Path
) -> None:
    """The scripted designer answers the reference plan. It is asked once,
    in a repository whose only commit is the base (no head is reachable
    from it), with the specification and every criterion; the plan holds on
    the base, passes the correct head and fails every planted head."""
    where = base_checkout(fixture, tmp_path / "designer")
    agent = RecordingAgent(json.dumps(fixture.reference))

    entry, asks, errors = design(fixture, agent, where, timeout=None)

    assert (asks, errors) == (1, []), errors
    assert agent.cwds == [where]
    reachable = subprocess.run(
        ["git", "rev-list", "--all", "--count"], cwd=where, capture_output=True, text=True
    )
    assert reachable.stdout.strip() == "1", reachable
    repo = materialize(fixture, tmp_path / "repo")
    assert fixture.spec.strip() in agent.prompts[0]
    assert all(line in agent.prompts[0] for line in fixture.criteria)
    scored = score(fixture, repo, entry, tmp_path / "plan")
    assert clean(scored)[0], clean(scored)[1]
    for head in fixture.planted:
        assert caught(scored, head)[0], caught(scored, head)[1]


@runs_a_stack
def test_a_vacuous_plan_catches_nothing(tmp_path: Path) -> None:
    """A check that passes everywhere is refused on the base, so it scores
    as a miss on every planted head and as not clean on the correct one."""
    fixture = FIXTURES[0]
    repo = materialize(fixture, tmp_path / "repo")
    vacuous = {
        "createsApp": False,
        "checks": [
            {
                "id": "vacuous",
                "criterion": "c",
                "argv": ["true"],
                "onBase": "fails",
                "heldOut": False,
            }
        ],
    }

    scored = score(fixture, repo, vacuous, tmp_path / "plan")

    assert "passes on the base" in " ".join(scored.refused), scored
    assert not clean(scored)[0]
    assert not any(caught(scored, head)[0] for head in fixture.planted)
    assert CORRECT not in scored.verdicts


@runs_a_stack
def test_a_check_that_cannot_run_catches_nothing(tmp_path: Path) -> None:
    """A check whose command is not found exits 127 everywhere. The plan
    marks the component as creating the app, so the base lets it through,
    but a check that did not run is never a failure: it catches no planted
    head, and the correct head is not clean."""
    fixture = FIXTURES[0]
    repo = materialize(fixture, tmp_path / "repo")
    missing = {
        "createsApp": True,
        "checks": [
            {
                "id": "missing",
                "criterion": "c",
                "argv": ["kstrl-no-such-check-7c1d"],
                "onBase": "fails",
                "heldOut": False,
            }
        ],
    }

    scored = score(fixture, repo, missing, tmp_path / "plan")

    assert scored.refused == [], scored
    assert set(scored.verdicts[CORRECT].values()) == {"not_run"}, scored
    assert not clean(scored)[0]
    assert not any(caught(scored, head)[0] for head in fixture.planted)
