"""#639 slice 2: the architect returns the product spec as ``requirements``.

DECOMPOSE_PROMPT 5.0.0 asks the architect for ``requirements`` beside its
components: each entry is a requirement or a non_goal with an "R-<n>" id,
and a requirement names the user stories that build it. The field is
required whenever components are returned, and every story must be named
by a requirement (owner decisions 4a and 5). The raw payload is validated
entry by entry with an indexed message inside the decompose retry loop,
so a malformed payload is a retry and, when the retries run out, no plan.
The requirements are recorded in the decision register, and a register
whose requirements do not validate is refused before any spend
(tests/test_spec_identity.py).

End to end: ``python -m kstrl decompose`` as a subprocess on a temp git
repository, with the stub architect of tests/test_stack_proposal_e2e.py.

Slice 5: the requirements of the register a run bound reach the engineer
(every requirement and non-goal, in the architect-decisions block of its
prompt) and the pull request (the id and statement of each requirement the
PR's stories build). ``ks decompose`` then ``ks factory``, as subprocesses,
with the stub gh of tests/test_merge_gate_park.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.executables import write_executable
from tests.helpers.stack_confirmation import write_stack
from tests.test_escalation_inbox import CLOSED
from tests.test_merge_gate_park import FACTORY_FLAGS, _env, _ks, _manifest_path
from tests.test_spec_identity import _project
from tests.test_stack_proposal_e2e import HALTED, STACK, _architect, _decompose, greenfield

REGISTER = Path("scripts") / "kstrl" / "decisions.json"
MANIFEST = Path("scripts") / "kstrl" / "manifest.json"


def _story(story_id: str) -> dict[str, Any]:
    return {
        "id": story_id,
        "title": f"Story {story_id}",
        "acceptanceCriteria": [f"WHEN {story_id} runs THE SYSTEM SHALL succeed"],
        "priority": int(story_id[-1]),
        "passes": False,
        "notes": "",
    }


#: A requirement each for US-001 and US-002, and one non-goal.
GOOD: list[dict[str, Any]] = [
    {"id": "R-1", "kind": "requirement", "statement": "Add numbers", "stories": ["US-001"]},
    {"id": "R-2", "kind": "requirement", "statement": "Subtract", "stories": ["US-002"]},
    {"id": "R-3", "kind": "non_goal", "statement": "No division", "stories": []},
]


def _payload(requirements: object = GOOD) -> dict[str, Any]:
    """One component with two stories; ``requirements`` is left out when it is ``...``."""
    payload: dict[str, Any] = {
        "stack": STACK,
        "spec_issues": [],
        "decisions": [],
        "components": [
            {
                "id": "comp-a",
                "title": "Calculator",
                "description": "The calculator",
                "dependencies": [],
                "allowedPaths": ["src/", "scripts/kstrl/feature/comp-a/"],
                "userStories": [_story("US-001"), _story("US-002")],
            }
        ],
    }
    if requirements is not ...:
        payload["requirements"] = requirements
    return payload


def test_the_architects_requirements_are_recorded_in_the_register(tmp_path: Path) -> None:
    """A payload whose requirements name every story is accepted on the
    first attempt, and the register holds the requirements as written."""
    root = greenfield(tmp_path)
    agent, prompts = _architect(tmp_path, _payload())

    proc = _decompose(root, agent)

    assert proc.returncode == 0, proc.stdout
    assert len(list(prompts.iterdir())) == 1, proc.stdout
    register = json.loads((root / REGISTER).read_text(encoding="utf-8"))
    assert register["requirements"] == GOOD, register
    assert register["halted"] is False, register


def _with(index: int, **fields: object) -> list[dict[str, Any]]:
    """GOOD with the fields of entry ``index`` replaced."""
    return [{**entry, **fields} if i == index else entry for i, entry in enumerate(GOOD)]


#: (requirements, the indexed message the decompose must give for it).
MALFORMED: dict[str, tuple[object, str]] = {
    "omitted": (..., "'requirements' is required when 'components' is not empty"),
    "capitalised-kind": (
        _with(0, kind="Requirement"),
        "requirements[0].kind: 'Requirement' is not one of ['non_goal', 'requirement'] "
        "(match is case-exact)",
    ),
    "untraced-story": (
        GOOD[:1] + GOOD[2:],
        "requirements: story 'US-002' is named by no requirement",
    ),
    "unknown-story": (
        # Two named, two stories: a join by count would pass it.
        _with(1, stories=["US-404"]),
        "requirements[1].stories: unknown story id 'US-404'",
    ),
    "duplicate-id": (_with(1, id="R-1"), "requirements[1].id: duplicate id 'R-1'"),
    "non-goal-with-a-story": (
        _with(2, stories=["US-001"]),
        "requirements[2].stories: a non_goal is built by no story, so it must be []",
    ),
    "non-object-entry": ([*GOOD, "R-4"], "requirements[3]: must be an object, got str"),
    "requirement-with-no-story": (
        [*GOOD, {"id": "R-4", "kind": "requirement", "statement": "Log", "stories": []}],
        "requirements[3].stories: a requirement must name the stories that build it",
    ),
    "story-id-as-requirement-id": (
        _with(0, id="US-001"),
        "requirements[0].id: 'US-001' is not of the form 'R-<n>'",
    ),
    "id-with-a-suffix": (_with(0, id="R-1a"), "requirements[0].id: 'R-1a' is not of the form"),
}


@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_a_malformed_requirements_payload_is_refused_with_an_indexed_message(
    tmp_path: Path, shape: str
) -> None:
    """Each malformed shape is a retry whose prompt names the fault by its
    index; when every attempt gives it, the decompose fails and writes no
    manifest and no register."""
    requirements, message = MALFORMED[shape]
    root = greenfield(tmp_path)
    agent, prompts = _architect(tmp_path, _payload(requirements))

    proc = _decompose(root, agent)

    assert proc.returncode == 1, proc.stdout
    retry = (prompts / "2.txt").read_text(encoding="utf-8")
    assert message in retry, retry[-800:]
    assert message in proc.stdout, proc.stdout
    assert not (root / MANIFEST).exists(), proc.stdout
    assert not (root / REGISTER).exists(), proc.stdout


def test_a_halt_needs_no_requirements_but_a_malformed_one_is_still_a_retry(
    tmp_path: Path,
) -> None:
    """A halt returns no components, so it names no story and may omit
    requirements. One it does return is still checked entry by entry: the
    first answer's capitalised kind is a retry, and the second answer,
    with none, halts with exit 2 and a halted register."""
    root = greenfield(tmp_path)
    write_stack(root)
    malformed = {**HALTED, "requirements": _with(0, kind="Requirement", stories=[])}
    agent, prompts = _architect(tmp_path, malformed, HALTED)

    proc = _decompose(root, agent)

    assert proc.returncode == 2, proc.stdout
    retry = (prompts / "2.txt").read_text(encoding="utf-8")
    assert "requirements[0].kind: 'Requirement' is not one of" in retry, retry[-800:]
    register = json.loads((root / REGISTER).read_text(encoding="utf-8"))
    assert register["halted"] is True, register
    assert register["requirements"] == [], register
    assert not (root / MANIFEST).exists(), proc.stdout


# --- slice 5: the requirements reach the engineer and the pull request -------

#: CLOSED's one component and requirement, plus a non-goal no story builds.
BUILT = CLOSED["requirements"][0]
NON_GOAL = {"id": "R-2", "kind": "non_goal", "statement": "No single sign-on.", "stories": []}
TRACED = {**CLOSED, "requirements": [BUILT, NON_GOAL]}

#: The engineer's side: mark every story of its PRD done.
MARK_DONE = """import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    prd = json.load(f)
for story in prd["userStories"]:
    story["passes"] = True
with open(path, "w", encoding="utf-8") as f:
    f.write(json.dumps(prd))
"""


def _built(tmp_path: Path, *, single_pr: bool = False) -> tuple[str, str]:
    """``ks decompose`` of TRACED (with ``--single-pr`` when asked), then
    ``ks factory`` with an engineer that records its prompt, marks its story
    done and commits under src/. Returns the engineer's prompt and the stub
    gh's log from its ``pr create`` on."""
    root = _project(tmp_path)
    env = _env(tmp_path)
    architect = tmp_path / "architect.json"
    architect.write_text(json.dumps(TRACED), encoding="utf-8")
    mark = tmp_path / "mark_done.py"
    mark.write_text(MARK_DONE, encoding="utf-8")
    prompt = tmp_path / "engineer.prompt"
    engineer = f"""#!/bin/sh
cat > '{prompt}'
'{sys.executable}' '{mark}' scripts/kstrl/feature/login/prd.json
mkdir -p src && echo work > src/work.txt
git add -A && git commit -q -m work
echo '<promise>COMPLETE</promise>'
"""
    env["AGENT_CMD"] = str(write_executable(tmp_path / "engineer.sh", engineer))
    planned = _ks(
        root,
        env,
        "decompose",
        *("--spec", str(root / "spec.md"), "--project-name", "p"),
        *("--agent-cmd", f"cat > /dev/null; cat '{architect}'", "--ui", "plain", "--no-color"),
        *(("--single-pr",) if single_pr else ()),
    )
    assert planned.returncode == 0, planned.stdout + planned.stderr

    proc = _ks(root, env, "factory", "--manifest", str(_manifest_path(root)), *FACTORY_FLAGS)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    gh = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert gh.count("gh pr create") == 1, gh
    created = gh[gh.index("gh pr create") :]
    # The single PR is the one ``create_single_pr`` titles; the other is the component's.
    assert ("[p] Factory: all components" in created) is single_pr, created
    return prompt.read_text(encoding="utf-8"), created


def test_the_engineer_is_given_every_requirement_and_non_goal(tmp_path: Path) -> None:
    """The architect-decisions block of the engineer's prompt holds each
    requirement with the stories that build it, and each non-goal."""
    prompt, _gh = _built(tmp_path)

    assert f"- **R-1** [requirement] (stories US-001): {BUILT['statement']}" in prompt, prompt
    assert f"- **R-2** [non_goal]: {NON_GOAL['statement']}" in prompt, prompt


@pytest.mark.parametrize("single_pr", [False, True], ids=["per-component", "single-pr"])
def test_the_pull_request_cites_the_requirements_its_stories_build(
    tmp_path: Path, single_pr: bool
) -> None:
    """The body names R-1, which the PR's story builds, and not the
    non-goal R-2, which no story builds."""
    _prompt, gh = _built(tmp_path, single_pr=single_pr)

    assert f"## Requirements\n\n- R-1: {BUILT['statement']}\n" in gh, gh
    assert "R-2" not in gh, gh
