"""Tables for the integration review's prompts in ``kstrl/integration.py``:
``INTEGRATION_CRITERIA_PROMPT`` (#482), ``INTEGRATION_CARRIED_PROMPT`` (#483),
``INTEGRATION_REQUIREMENT_PROMPT`` and ``INTEGRATION_NON_GOAL_PROMPT`` (#639
slice 3).

They live here rather than in ``tests/test_prompt_versions.py`` for the reason
``tests/helpers/builder_prompts.py`` gives: that file is close to the repo's
800-line ratchet. #639 slice 3 moved the first two rows here unchanged, with
their version history. ``tests/test_prompt_versions.py`` merges each table
into ``_PROMPTS``, ``_VERSIONS``, ``_EXPECTED_SNAPSHOTS`` and ``_RENDERERS``,
so every check there applies to these prompts as before. Each prompt keeps
its own version constant.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from kstrl import integration
from kstrl.requirements import SpecRequirement

INTEGRATION_PROMPTS: dict[str, str] = {
    "INTEGRATION_CRITERIA_PROMPT": integration.INTEGRATION_CRITERIA_PROMPT,
    "INTEGRATION_CARRIED_PROMPT": integration.INTEGRATION_CARRIED_PROMPT,
    "INTEGRATION_REQUIREMENT_PROMPT": integration.INTEGRATION_REQUIREMENT_PROMPT,
    "INTEGRATION_NON_GOAL_PROMPT": integration.INTEGRATION_NON_GOAL_PROMPT,
}

INTEGRATION_VERSIONS: dict[str, str] = {
    "INTEGRATION_CRITERIA_PROMPT": integration.INTEGRATION_CRITERIA_PROMPT_VERSION,
    "INTEGRATION_CARRIED_PROMPT": integration.INTEGRATION_CARRIED_PROMPT_VERSION,
    "INTEGRATION_REQUIREMENT_PROMPT": integration.INTEGRATION_REQUIREMENT_PROMPT_VERSION,
    "INTEGRATION_NON_GOAL_PROMPT": integration.INTEGRATION_NON_GOAL_PROMPT_VERSION,
}

#: The pin. Nothing here computes a hash.
INTEGRATION_SNAPSHOTS: dict[str, tuple[str, str]] = {
    # 1.0.0 (#482): new. H3 discharged here. Its H2 roles, "integration"
    # and "integration_clean", are scored on the #480 section 8 Layer B
    # fixtures; no saved baseline carries their ids yet.
    # 1.1.0 (#500): IC5 names the component's prd.json in words instead of the `<component>`
    # placeholder a reviewer echoing it could rewrite. H2 pending: the owner's paid calibration run.
    # 1.2.0 (#480): every story says it gets its own verdict, pass included, and that the
    # specification and the prd.json files are evidence, not stories; IC2 fails a read path that
    # re-applies new-input checks with no rule tightened yet; IC1 is one condition. H2: the before
    # and after integration captures saved beside this change.
    # 1.3.0 (#696 slice 10): MINOR. IC1 and IC4 judge a call against the
    # callee's documented contract (doc comment, docstring or equivalent)
    # rather than its docstring. H2: the integration and integration_clean
    # captures.
    "INTEGRATION_CRITERIA_PROMPT": (
        "e434808afe8697231d025a15ebc0d6d525846b34717e405d953bd4b189a6a7d7",
        "1.3.0",
    ),
    # 1.0.0 (#483): new. The criterion of a carried finding's story, sent to
    # the integration reviewer. H3 only, for INTEGRATION_CRITERIA_PROMPT's
    # reason: its calibration role "integration" has no fixture yet.
    "INTEGRATION_CARRIED_PROMPT": (
        "71c4e1edade09d984f65f6dac1356f26b4ea9baea01ee5f9b9b321fa91975d7c",
        "1.0.0",
    ),
    # 1.0.0 (#639 slice 3): new. The criterion of the story that judges one
    # requirement of the bound register, sent to the integration reviewer.
    # H3 only: no integration fixture carries a requirement (#639 M6), so H2
    # cannot be discharged, and the verdict is recorded and opens nothing.
    "INTEGRATION_REQUIREMENT_PROMPT": (
        "725408daf55c4b2fab75c2d67967adbc3a2d573b563971e863d2461082bf54ba",
        "1.0.0",
    ),
    # 1.0.0 (#639 slice 3): new. The same for one non-goal, for the same reason.
    "INTEGRATION_NON_GOAL_PROMPT": (
        "417c77a089dd3e4d25371c0c7d58ba7eb6f9a03fc0ac0177c4d367ace0e377fb",
        "1.0.0",
    ),
}

#: One requirement and one non-goal, as ``read_decisions`` returns them (#639
#: slice 3). ``tests/test_delivered_prompts.py`` renders the same two.
ONE_REQUIREMENT = SpecRequirement("R-1", "requirement", "STATEMENT", ("US-001",))
ONE_NON_GOAL = SpecRequirement("R-2", "non_goal", "STATEMENT", ())

#: ``{enrolled prompt: (module holding the constant, production renderer)}``,
#: read as ``tests/test_prompt_versions.py::_RENDERERS`` is read.
INTEGRATION_RENDERERS: dict[str, tuple[ModuleType, Callable[[Path], str]]] = {
    "INTEGRATION_CRITERIA_PROMPT": (
        integration,
        lambda _p: integration.render_integration_criteria("BASE_SHA"),
    ),
    "INTEGRATION_CARRIED_PROMPT": (
        integration,
        lambda _p: integration.carried_story("IF-1", "TEXT", ["src/a.py"]).criterion,
    ),
    "INTEGRATION_REQUIREMENT_PROMPT": (
        integration,
        lambda _p: integration.requirement_stories([ONE_REQUIREMENT])[0].criterion,
    ),
    "INTEGRATION_NON_GOAL_PROMPT": (
        integration,
        lambda _p: integration.requirement_stories([ONE_NON_GOAL])[0].criterion,
    ),
}

assert (
    set(INTEGRATION_PROMPTS)
    == set(INTEGRATION_VERSIONS)
    == set(INTEGRATION_SNAPSHOTS)
    == set(INTEGRATION_RENDERERS)
), "the four tables name different prompts: each prompt needs a row in all four."
