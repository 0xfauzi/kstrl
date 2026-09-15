"""H2's half of the tripwire: a digest of what each role is actually sent.

``tests/test_prompt_versions.py`` hashes the enrolled CONSTANTS. That is
H3, and it works: an edit to a prompt body moves a hash and the snapshot
fails until a version bump and a re-hash travel with it.

H2 is a different question. It asks whether a change to an adversarial
role's INPUT was re-measured, and detection rate is a property of the
text the role receives, which is the constant plus everything
interpolated into it. Nothing hashed that. Issue #325 measured the gap
on a real change: routing the architect's spec findings into each
component PRD grew the delivered security prompt by 60 percent, with
``test_prompt_versions.py`` green throughout, no ``*_PROMPT`` line in
the diff and no version bump owed. H3 was genuinely satisfied. H2 was
silently not.

This module closes the free half. Each role's DELIVERED text is rendered
through its real production builder against a fixed fixture below, and
its ``(sha256, length)`` is pinned in ``_EXPECTED_DELIVERED``. A change
to interpolation logic, to the order sections are assembled in, or to a
builder's own wording now moves a digest, and the length beside it puts
the byte delta in the diff where a reviewer reads it.

No runtime gate goes with it. Issue #325's option 3 was a per-prompt
size budget enforced at delivery; that is a new production failure path
that fires mid-run, needs a number nobody has measured, and catches the
same growth later than this does.

Three layers, all of which FLAG rather than clear:

1. ``test_delivered_prompt_digest_unchanged`` pins the digest.
2. ``test_every_enrolled_prompt_is_delivered_somewhere`` is the census:
   every prompt enrolled for H3 must be carried by some delivered text
   here, so a tenth enrolled prompt cannot get a constant hash and no
   delivered digest.
3. ``test_the_declared_constant_reaches_the_delivered_text`` proves each
   row of that census is true, by patching the constant to a marker and
   looking for the marker in the delivered text.

What the digests deliberately do NOT cover, stated rather than left
implicit:

- The per-run delimiter token. ``generate_data_delimiter`` returns 128
  fresh bits per build, so it is pinned to a fixed value here. A change
  to the token's SHAPE is therefore invisible to these digests;
  ``tests/test_prompt_injection_guard.py`` owns that.
- The engineer's absolute paths, normalised to ``<ROOT>``.
- Every truncation limit in the code. Every fixture input here is far
  smaller than every limit, so raising or lowering one moves no digest.
  Measured: changing the 50000 character diff cut in
  ``knowledge.distill_facts`` to 20 leaves all 25 tests here green, and
  ``tests/test_knowledge.py`` green too, because that test asserts only
  that the truncation marker is present. Covering a limit would need a
  fixture input larger than it, and for the distiller the cut is in
  ``distill_facts``, which is already named above as having no verbatim
  test.
- For the security row, that ``security.py`` still calls
  ``git.repo_change_source``: the block is passed in as an argument
  here, exactly as ``run_security_review`` passes it. That wiring is
  guarded end to end by
  ``test_prompt_versions.test_change_source_reaches_the_role``.
- Text a phase entry point wraps around its builder's output AFTER the
  builder returns. A digest of a builder cannot see that, so
  ``test_the_entry_point_delivers_the_builder_output_verbatim`` drives
  the two phases that have a builder and an entry point taking a repo
  (review, security) and requires the agent to receive the builder's
  string with nothing added. Measured: appending one line to ``prompt``
  in ``run_review`` after ``build_review_prompt`` returns leaves all 24
  other tests here and all of ``tests/test_prompt_versions.py`` green,
  and fails only that test. The same wrapping in ``decompose_spec``
  (kstrl/decompose.py:2264), ``distill_facts``
  (kstrl/knowledge.py:1239) and ``factory`` (kstrl/factory.py:4036) is
  NOT covered here: those three entry points need an agent, a manifest
  or a whole run to drive, and the cost was judged not worth it. That is
  a disclosed miss, not an oversight.
- The engineer needs no such test: its row already drives ``run_loop``
  and captures what the agent was handed, so wrapping is inside the
  digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kstrl import decisions, decompose, git, init_cmd, knowledge, review, security, verify
from kstrl.config import KstrlConfig
from kstrl.decisions import SpecDecision, build_decisions_context
from kstrl.loop import COMPLETION_MARKER, run_loop
from kstrl.manifest import Component
from kstrl.review import ReviewMode
from kstrl.security import SecurityConfig, SecurityMode
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.conftest import make_review_repo
from tests.test_prompt_versions import _ORPHAN_MARKER, _PROMPTS
from tests.test_review_payload import RecordingAgent

# ---------------------------------------------------------------------------
# The fixture. Small, committed, and free of anything that varies between
# runs or machines: no clock, no randomness, no path that is not
# normalised back out below.
# ---------------------------------------------------------------------------

#: ``generate_data_delimiter`` is 128 fresh bits per prompt build, which
#: is the point of it. Pinned here so the digests are about the words.
_FIXED_DELIMITER = "KSTRL-DATA-" + "0" * 32

_SPEC_TEXT = "# Spec\n\nBuild a document parser.\n"

_PRD_JSON = (
    json.dumps(
        {
            "branchName": "feat/parser",
            "userStories": [
                {
                    "id": "S1",
                    "title": "Parse a document",
                    "acceptanceCriteria": ["it parses a valid file", "it errors on a bad one"],
                    "priority": 1,
                    "passes": False,
                    "notes": "a note",
                }
            ],
        },
        indent=2,
    )
    + "\n"
)

_CLAUDE_MD = "# Project Rules\n\nWrite type hints.\n"

_DIFF_TEXT = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

_EXISTING_FACTS = "- C1 writes JSON with utf-8 pinned\n"

#: One dependency on purpose: an empty list renders "(none)", which is
#: also what a builder that stopped reading the field would render.
_COMPONENT = Component(
    id="C1",
    title="Document parser",
    description="Parses documents.",
    dependencies=["C0"],
    prd_path="prd.json",
    branch_name="feat/parser",
)

#: One check on purpose, for the same reason: an empty list renders an
#: empty summary block, which is also what a dropped loop renders.
_VERIFICATION = VerificationResult(
    passed=True,
    checks=[CheckResult(name="tests", passed=True, message="12 passed")],
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Every module that reads ``generate_data_delimiter`` at prompt-build
#: time. Each imported the NAME, so the binding to replace is the one in
#: the consuming module, not the one in ``kstrl.delimiters``.
_DELIMITER_CONSUMERS: tuple[ModuleType, ...] = (decompose, review, security, knowledge, git)


def _pin_delimiters(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in _DELIMITER_CONSUMERS:
        monkeypatch.setattr(module, "generate_data_delimiter", lambda: _FIXED_DELIMITER)


# ---------------------------------------------------------------------------
# The renderers. Each drives the real production builder.
# ---------------------------------------------------------------------------


def _architect(_tmp_path: Path) -> str:
    return decompose.build_decompose_prompt("PROJECT", _SPEC_TEXT)


def _reviewer(tmp_path: Path) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    prd_path = tmp_path / "prd.json"
    prd_path.write_text(_PRD_JSON, encoding="utf-8")
    return review.build_review_prompt(prd_path, "BASE_SHA", _VERIFICATION)


def _security(_tmp_path: Path) -> str:
    # Two arguments, exactly as ``run_security_review`` calls it
    # (kstrl/security.py:686). Passing the delimiter explicitly would
    # take the pasted-diff branch of the ``is None`` test instead of the
    # production one; the rendered text is identical either way because
    # ``security.generate_data_delimiter`` is pinned above.
    return security._build_security_prompt(_PRD_JSON, git.repo_change_source("BASE_SHA"))


def _distiller(_tmp_path: Path) -> str:
    return knowledge.build_distill_prompt(_COMPONENT, 5, _PRD_JSON, _EXISTING_FACTS, _DIFF_TEXT)


def _pasted_change_source(_tmp_path: Path) -> str:
    return git.pasted_change_source(_DIFF_TEXT)[0]


def _decisions_context(_tmp_path: Path) -> str:
    return build_decisions_context(
        [
            SpecDecision(issue="i1", question="q1", disposition="decided", resolution="r1"),
            SpecDecision(
                issue="i2",
                question="q2",
                disposition="decided",
                resolution="r2",
                component="C1",
            ),
        ],
        "C1",
    )


class _PromptCapturingAgent:
    """Records the prompt the engineer was handed, and completes."""

    name = "capture"
    final_message: str | None = None
    usage_records: list[Any] = []

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        self.prompts.append(prompt)
        yield COMPLETION_MARKER


def _engineer(tmp_path: Path) -> str:
    """The engineer has no builder: the assembly IS ``run_loop``.

    So this one is driven end to end through the real entry point, with
    only the agent replaced. No ``prompt.md`` is scaffolded, so the
    harness ``DEFAULT_PROMPT`` fallback is what gets substituted and
    digested, which is what an un-customised project runs.
    """
    root = tmp_path / "proj"
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text(_CLAUDE_MD, encoding="utf-8")
    (kstrl_dir / "prd.json").write_text(_PRD_JSON, encoding="utf-8")
    config = KstrlConfig(
        max_iterations=1,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        kstrl_branch="",
        kstrl_branch_explicit=True,
    )
    agent = _PromptCapturingAgent()
    run_loop(
        config,
        PlainUI(no_color=True),
        agent,  # type: ignore[arg-type]
        root,
        verify_config=VerifyConfig(test_command="T", typecheck_command="TC", lint_command="L"),
    )
    assert agent.prompts, "run_loop never called the agent, so this digest pins nothing."
    return agent.prompts[0].replace(str(root), "<ROOT>")


#: ``{role: the function that produces what that role is sent}``.
_DELIVERED: dict[str, Callable[[Path], str]] = {
    "architect": _architect,
    "decisions-context": _decisions_context,
    "distiller": _distiller,
    "engineer": _engineer,
    "pasted-change-source": _pasted_change_source,
    "reviewer": _reviewer,
    "security": _security,
}

#: ``{role: the enrolled constants its delivered text carries}``. The
#: census layer: the union must be every prompt enrolled for H3.
_COVERS: dict[str, frozenset[str]] = {
    "architect": frozenset({"DECOMPOSE_PROMPT"}),
    "decisions-context": frozenset({"DECISIONS_CONTEXT_PROMPT"}),
    "distiller": frozenset({"DISTILL_PROMPT"}),
    "engineer": frozenset({"DEFAULT_PROMPT", "VERIFY_COMMANDS_PROMPT"}),
    "pasted-change-source": frozenset({"PASTED_CHANGE_SOURCE_PROMPT"}),
    "reviewer": frozenset({"REVIEWER_PROMPT", "REPO_CHANGE_SOURCE_PROMPT"}),
    "security": frozenset({"SECURITY_PROMPT", "REPO_CHANGE_SOURCE_PROMPT"}),
}

#: ``{constant: the module whose binding a renderer reads}``. The patch
#: target for the containment layer. ``DEFAULT_PROMPT`` is imported
#: inside ``run_loop``'s body, so patching ``init_cmd`` reaches it.
_HOME_MODULE: dict[str, ModuleType] = {
    "DECISIONS_CONTEXT_PROMPT": decisions,
    "DECOMPOSE_PROMPT": decompose,
    "DEFAULT_PROMPT": init_cmd,
    "DISTILL_PROMPT": knowledge,
    "PASTED_CHANGE_SOURCE_PROMPT": git,
    "REPO_CHANGE_SOURCE_PROMPT": git,
    "REVIEWER_PROMPT": review,
    "SECURITY_PROMPT": security,
    "VERIFY_COMMANDS_PROMPT": verify,
}

#: ``{role: (sha256 of the delivered text, its length in characters)}``.
#: The length is redundant for failing and is recorded anyway: it is the
#: number a reviewer reads off the diff to see how much the role's input
#: grew.
_EXPECTED_DELIVERED: dict[str, tuple[str, int]] = {
    "architect": ("8bf3cb8b8fa5de5380dcacfd2fb9500cda4d882d6b83c436851ed9a085bac0c1", 12319),
    "decisions-context": ("3a5a37400d561b95b39b8a2b69896d6453dcd7611f59d16f3432bfd83146ca43", 623),
    "distiller": ("f7b64117a9d1281e44a064101e67c8eaa639bbb4263b14b33a90cd68f2ac546b", 4069),
    "engineer": ("0ce37b67c9717479aac69ab7428959cd736e703806d6107715946de28f721428", 5145),
    "pasted-change-source": (
        "e23bd60d3e43f6b8865ef93c73f24021dfd03bd023911a289ef9fdf15ebef9f4",
        865,
    ),
    "reviewer": ("ec189f894a02e357dfa2d3921e7777397b0c7f72b4c23a445e8f1dfb2b501858", 7578),
    "security": ("46ff0b2f459392307b0af7af7b85c61b34b46e6bb553c5a979873cee0a48842b", 8540),
}


def _drift_message(role: str, expected: tuple[str, int], actual: tuple[str, int]) -> str:
    exp_hash, exp_len = expected
    act_hash, act_len = actual
    delta = act_len - exp_len
    pct = (delta / exp_len * 100) if exp_len else 0.0
    return (
        f"The {role} role is being sent different text than it was.\n"
        f"  sha256:  expected={exp_hash}\n           actual  ={act_hash}\n"
        f"  length:  expected={exp_len}  actual={act_len}  "
        f"({delta:+d} chars, {pct:+.1f}%)\n"
        "\n"
        "Nothing here is a constant, so H3 may be entirely satisfied and\n"
        "this can still have moved 60 percent of what the role reads.\n"
        "To land this change:\n"
        "  1. Say in the PR body WHAT changed about the delivered text and\n"
        "     by how many characters. Both numbers are in this message.\n"
        "  2. Decide whether H2 is owed. It is owed when the change alters\n"
        "     what an adversarial role is asked to judge or how much of it\n"
        "     it gets: new interpolated content, a new or changed\n"
        "     truncation limit, a reordering of sections. It is not owed\n"
        "     for a change with no effect on any real input. Record the\n"
        "     decision in the PR body either way. If it is owed:\n"
        "       KSTRL_RUN_CALIBRATION=1 uv run pytest tests/test_calibration.py -v\n"
        f"  3. Update _EXPECTED_DELIVERED[{role!r}] in this file to "
        "(new hash, new length).\n"
        "\n"
        "If a *_PROMPT constant moved too, tests/test_prompt_versions.py\n"
        "will say so separately and its steps apply as well.\n"
    )


@pytest.mark.parametrize("role", sorted(_DELIVERED))
def test_delivered_prompt_digest_unchanged(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_delimiters(monkeypatch)
    delivered = _DELIVERED[role](tmp_path / role)
    assert delivered.strip(), f"the {role} renderer produced nothing, so its digest pins nothing."
    actual = (_sha256(delivered), len(delivered))
    assert actual == _EXPECTED_DELIVERED[role], _drift_message(
        role, _EXPECTED_DELIVERED[role], actual
    )


@pytest.mark.parametrize("role", sorted(_DELIVERED))
def test_the_delivered_text_is_deterministic(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two renders in one process must agree.

    Without this a renderer that picked up a clock, a uuid or a path
    would make the digest flaky, and the fix someone reaches for under
    time pressure is to normalise the varying part away, which is how a
    digest goes blind."""
    _pin_delimiters(monkeypatch)
    first = _DELIVERED[role](tmp_path / "first")
    second = _DELIVERED[role](tmp_path / "second")
    assert first == second, (
        f"the {role} delivered text differs between two renders of the "
        "same fixture, so something in it varies per build."
    )


def test_every_enrolled_prompt_is_delivered_somewhere() -> None:
    """The census. Every prompt H3 enrolls must reach some role's
    delivered text here, or its interpolation is unmeasured."""
    union: set[str] = set()
    for names in _COVERS.values():
        union |= names
    missing = sorted(set(_PROMPTS) - union)
    assert not missing, (
        f"enrolled prompts with no delivered digest: {missing}. Add each "
        "to the _COVERS row of the role that carries it, and add a "
        "renderer and an _EXPECTED_DELIVERED row if that role is new."
    )
    stale = sorted(union - set(_PROMPTS))
    assert not stale, f"_COVERS names prompts that are not enrolled for H3: {stale}."


def test_every_delivered_role_declares_coverage() -> None:
    """The three tables are keyed the same way, so a role added to one
    and forgotten in another fails here with a name rather than as a
    KeyError inside another test."""
    assert sorted(_COVERS) == sorted(_DELIVERED)
    assert sorted(_EXPECTED_DELIVERED) == sorted(_DELIVERED)


@pytest.mark.parametrize("role", sorted(_DELIVERED))
def test_the_declared_constant_reaches_the_delivered_text(role: str, tmp_path: Path) -> None:
    """Each _COVERS row is a claim; this is what makes it true.

    Patch the constant to the orphan marker and the marker must appear
    in the delivered text. A row claiming a constant the role never
    reads would otherwise satisfy the census while measuring nothing."""
    for const in sorted(_COVERS[role]):
        with pytest.MonkeyPatch.context() as mp:
            _pin_delimiters(mp)
            mp.setattr(_HOME_MODULE[const], const, _ORPHAN_MARKER)
            delivered = _DELIVERED[role](tmp_path / f"{role}-{const}")
        assert _ORPHAN_MARKER[:26] in delivered, (
            f"_COVERS says the {role} text carries {const}, but patching "
            f"{_HOME_MODULE[const].__name__}.{const} changed nothing in "
            "it. Either the row is wrong or the renderer stopped reading "
            "the constant."
        )


#: Not a prompt and deliberately not named like one: a string no builder
#: could produce, used to prove an entry point adds nothing to what its
#: builder returned.
_VERBATIM_MARKER = "KSTRL-DELIVERED-VERBATIM-" + "z" * 40


@pytest.mark.parametrize("which", ["review", "security"])
def test_the_entry_point_delivers_the_builder_output_verbatim(
    which: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest of a builder says nothing about what the CALLER adds.

    Replace the builder with one returning a marker, drive the real
    phase, and the agent must receive that marker and nothing else. Any
    line appended, prepended or interpolated around the builder's output
    in ``run_review`` / ``run_security_review`` fails here, and fails
    nowhere else in the suite."""
    repo = make_review_repo(tmp_path / f"repo-{which}")
    (repo.path / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}', encoding="utf-8"
    )
    agent = RecordingAgent("not valid json")
    ui = PlainUI(no_color=True)

    if which == "review":
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: _VERBATIM_MARKER)
        review.run_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            VerificationResult(passed=True, checks=[]),
            ReviewMode.ADVISORY,
            ui,
        )
    else:
        monkeypatch.setattr(security, "_build_security_prompt", lambda *a, **k: _VERBATIM_MARKER)
        security.run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            SecurityConfig(mode=SecurityMode.ADVISORY.value),
            ui,
        )

    assert agent.prompts, (
        f"the {which} phase never called its agent, so this proves nothing "
        "about what the role was sent."
    )
    assert agent.prompts[0] == _VERBATIM_MARKER, (
        f"{which} sends its agent something other than exactly what its "
        "builder returned, so the pinned digest above is not the whole "
        "delivered text.\n"
        f"  got {len(agent.prompts[0])} chars, expected "
        f"{len(_VERBATIM_MARKER)}\n"
        f"  head: {agent.prompts[0][:120]!r}\n"
        f"  tail: {agent.prompts[0][-120:]!r}"
    )
