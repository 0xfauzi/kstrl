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
its ``(sha256, length)`` is pinned in ``_ROLES``. A change to
interpolation logic, to the order sections are assembled in, or to a
builder's own wording now moves a digest, and the length beside it puts
the byte delta in the diff where a reviewer reads it.

Three layers, all of which FLAG rather than clear:

1. ``test_delivered_prompt_digest_unchanged`` pins the digest, and also
   renders twice against two different fixture roots so a flaky render
   cannot be "fixed" by normalising the varying part away.
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
  Covering a limit would need a fixture input larger than it.
- For the security row, that ``security.py`` still calls
  ``git.repo_change_source``: the block is passed in as an argument
  here, exactly as ``run_security_review`` passes it. That wiring is
  guarded end to end by
  ``test_prompt_versions.test_change_source_reaches_the_role``.
- Text a phase entry point wraps around its builder's output AFTER the
  builder returns. A digest of a builder cannot see that, so
  ``test_the_entry_point_delivers_the_builder_output_verbatim`` drives
  four entry points end to end with the builder replaced by a marker
  and requires the agent to receive that marker with nothing added. The
  same wrapping in the factory's decisions context is NOT covered here:
  that entry point needs a whole run to drive, and the cost was judged
  not worth it. That is a disclosed miss, not an oversight.
- The engineer needs no such test: its row already drives ``run_loop``
  and captures what the agent was handed, so wrapping is inside the
  digest.

H3-NOTE, in the shape of ``test_prompt_versions``' own: this enforces up
to the fixtures below. A digest that moves asks for the H2 decision to
be written in the PR body; nothing here checks that the decision was
actually written.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from kstrl import decompose, git, init_cmd, integration, knowledge, review, security
from kstrl.decisions import SpecDecision, build_decisions_context
from kstrl.loop import COMPLETION_MARKER
from kstrl.manifest import Component
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.helpers.builder_prompts import BUILDER_RENDER_EXEMPT
from tests.helpers.component_prd import write_component_prd
from tests.helpers.feedforward_prompts import NOTICE_PROMPTS
from tests.test_prompt_versions import (
    _MARKER_HEAD,
    _MARKER_TAIL,
    _ORPHAN_MARKER,
    _PROMPTS,
    _RENDERERS,
    _run_and_capture_prompt,
    _sha256,
)
from tests.test_review_payload import RecordingAgent
from tests.test_verify_command_contract import _engineer_prompt

# ---------------------------------------------------------------------------
# The fixture. Small, committed, and free of anything that varies between
# runs or machines: no clock, no randomness, no path that is not
# normalised back out below.
# ---------------------------------------------------------------------------

#: ``generate_data_delimiter`` is 128 fresh bits per prompt build, which
#: is the point of it. Pinned here so the digests are about the words.
_FIXED_DELIMITER = "KSTRL-DATA-" + "0" * 32

_SPEC_TEXT = "# Spec\n\nBuild a document parser.\n"

#: Defined once so the PRD built from it (below) and any test that wants
#: the same story data stay in sync by construction.
_STORY: dict[str, object] = {
    "id": "S1",
    "title": "Parse a document",
    "acceptanceCriteria": ["it parses a valid file", "it errors on a bad one"],
    "priority": 1,
    "passes": False,
    "notes": "a note",
}

_PRD_JSON = json.dumps({"branchName": "feat/parser", "userStories": [_STORY]}, indent=2) + "\n"

_CLAUDE_MD = "# Project Rules\n\nWrite type hints.\n"

_DIFF_TEXT = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

_EXISTING_FACTS = "- C1 writes JSON with utf-8 pinned\n"

#: One dependency on purpose: an empty list renders the same text as a
#: builder that stopped reading the field.
_COMPONENT = Component(
    id="C1",
    title="Document parser",
    description="Parses documents.",
    dependencies=["C0"],
    prd_path="prd.json",
    branch_name="feat/parser",
)

#: One passing and one failing check on purpose: an all-pass (or empty)
#: list renders the same PASS-only shape as a builder that stopped
#: reading the field, and would never exercise the FAIL line in
#: ``build_review_prompt``.
_VERIFICATION = VerificationResult(
    passed=False,
    checks=[
        CheckResult(name="tests", passed=True, message="12 passed"),
        CheckResult(name="lint", passed=False, message="1 error"),
    ],
)

#: Copied from ``tests.test_prompt_versions._decisions_context_render``
#: rather than imported, because that fixture is not a module-level
#: name. Three decisions, two components, all three tiers of
#: ``build_decisions_context`` present: i2 is "own" (comp-a, the target),
#: i1 is "run_wide" (no component), i3 is "other" (comp-b), which is what
#: makes ``_render_summary`` render at all.
_DECISIONS = [
    SpecDecision(issue="i1", question="q1", disposition="decided", resolution="r1"),
    SpecDecision(
        issue="i2", question="q2", disposition="decided", resolution="r2", component="comp-a"
    ),
    SpecDecision(
        issue="i3", question="q3", disposition="decided", resolution="r3", component="comp-b"
    ),
]


#: Every module that reads ``generate_data_delimiter`` at prompt-build
#: time. Each imported the NAME, so the binding to replace is the one in
#: the consuming module, not the one in ``kstrl.delimiters``.
_DELIMITER_CONSUMERS: tuple[ModuleType, ...] = (decompose, review, security, knowledge, git)


def _pin_delimiters(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in _DELIMITER_CONSUMERS:
        monkeypatch.setattr(module, "generate_data_delimiter", lambda: _FIXED_DELIMITER)


# ---------------------------------------------------------------------------
# The renderers that need more than one line. Every renderer that ignores
# its path argument is inlined as a lambda in ``_ROLES`` below instead.
# ---------------------------------------------------------------------------


def _reviewer(tmp_path: Path) -> str:
    prd_path = write_component_prd(tmp_path, "prd.json", branch="feat/parser", stories=[_STORY])
    return review.build_review_prompt(prd_path, "BASE_SHA", _VERIFICATION)


def _integration_reviewer(tmp_path: Path) -> str:
    """What the integration review sends (#482): the reviewer prompt over the
    PRD the harness writes from the enrolled criteria."""
    prd_path = tmp_path / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    integration.write_integration_prd(prd_path, integration.integration_stories("BASE_SHA"))
    return review.build_review_prompt(prd_path, "BASE_SHA", VerificationResult(passed=True))


def _engineer(tmp_path: Path) -> str:
    """The engineer has no builder: the assembly IS ``run_loop``.

    Reuses the sibling driver rather than re-assembling ``run_loop`` by
    hand: ``scaffold_prompt=False`` leaves no prompt.md, so the harness
    ``DEFAULT_PROMPT`` fallback is what gets substituted and digested,
    which is what an un-customised project runs.
    """
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text(_CLAUDE_MD, encoding="utf-8")
    prompt = _engineer_prompt(
        root,
        VerifyConfig(test_command="T", typecheck_command="TC", lint_command="L"),
        scaffold_prompt=False,
    )
    return prompt.replace(str(root), "<ROOT>")


@dataclass(frozen=True)
class _Role:
    render: Callable[[Path], str]
    #: The enrolled H3 constants this role's delivered text carries.
    covers: frozenset[str]
    #: sha256 of the delivered text, pinned at the fixtures above.
    digest: str
    #: Its length in characters. Redundant for failing; it is the number
    #: a reviewer reads off the diff to see how much a role's input grew.
    length: int


_ROLES: dict[str, _Role] = {
    "architect": _Role(
        lambda _p: decompose.build_decompose_prompt("PROJECT", _SPEC_TEXT),
        frozenset({"DECOMPOSE_PROMPT", "ARCHITECT_NO_REPO_SOURCE_PROMPT"}),
        "91a61a3dd236dbfd52bd8aeedf8180325bc46674b864c5e8bd3978f9ee34b45c",
        12803,
    ),
    "architect-with-repo": _Role(
        lambda _p: decompose.build_decompose_prompt(
            "PROJECT",
            _SPEC_TEXT,
            codebase_map_path="scripts/kstrl/codebase_map.md",
        ),
        frozenset({"DECOMPOSE_PROMPT", "ARCHITECT_REPO_SOURCE_PROMPT"}),
        "0b30cf903de5fe12df2d50fc8dda96915c21438a565a8ffcfc3da1b435eccf12",
        14244,
    ),
    "decisions-context": _Role(
        lambda _p: build_decisions_context(_DECISIONS, "comp-a"),
        frozenset({"DECISIONS_CONTEXT_PROMPT"}),
        "bb052fd147821f06f2dfcfd6d6b1132c34fa04d79424f2149aec5be24901f1f4",
        651,
    ),
    "distiller": _Role(
        lambda _p: knowledge.build_distill_prompt(
            _COMPONENT, 5, _PRD_JSON, _EXISTING_FACTS, _DIFF_TEXT
        ),
        frozenset({"DISTILL_PROMPT"}),
        "f7b64117a9d1281e44a064101e67c8eaa639bbb4263b14b33a90cd68f2ac546b",
        4069,
    ),
    "engineer": _Role(
        _engineer,
        frozenset({"DEFAULT_PROMPT", "VERIFY_COMMANDS_PROMPT"}),
        "0ce37b67c9717479aac69ab7428959cd736e703806d6107715946de28f721428",
        5145,
    ),
    "integration-criteria": _Role(
        lambda _p: integration.render_integration_criteria("BASE_SHA"),
        frozenset({"INTEGRATION_CRITERIA_PROMPT"}),
        "2e940a4995f0df24b52782364f8ab66bab8b8c857413932bbb55705cfd8ba5b3",
        1404,
    ),
    "integration-reviewer": _Role(
        _integration_reviewer,
        frozenset({"REVIEWER_PROMPT", "REPO_CHANGE_SOURCE_PROMPT"}),
        "9ea1bc6167cac1f7a75faa1127b7386f405d3e632e05870537bbedf8ce18c460",
        8901,
    ),
    "pasted-change-source": _Role(
        lambda _p: git.pasted_change_source(_DIFF_TEXT)[0],
        frozenset({"PASTED_CHANGE_SOURCE_PROMPT"}),
        "e23bd60d3e43f6b8865ef93c73f24021dfd03bd023911a289ef9fdf15ebef9f4",
        865,
    ),
    "reviewer": _Role(
        _reviewer,
        frozenset({"REVIEWER_PROMPT", "REPO_CHANGE_SOURCE_PROMPT"}),
        "d6b1300640142c90826bab67073af0e0182cc06235e6cd26f77a3d5265b80e11",
        7601,
    ),
    "security": _Role(
        # Two arguments, exactly as ``run_security_review`` calls it
        # (kstrl/security.py:686). Passing the delimiter explicitly would
        # take the pasted-diff branch of the ``is None`` test instead of
        # the production one; the rendered text is identical either way
        # because ``security.generate_data_delimiter`` is pinned above.
        lambda _p: security._build_security_prompt(_PRD_JSON, git.repo_change_source("BASE_SHA")),
        frozenset({"SECURITY_PROMPT", "REPO_CHANGE_SOURCE_PROMPT"}),
        "46ff0b2f459392307b0af7af7b85c61b34b46e6bb553c5a979873cee0a48842b",
        8540,
    ),
}

#: ``{constant: the module whose binding a renderer reads}``. Derived
#: from the sibling's own patch-target table rather than listed again by
#: hand: ``DEFAULT_PROMPT`` is the one enrolled prompt with no renderer
#: there (``_RENDER_EXEMPT``), so it is added explicitly. A delivered
#: renderer that reads a constant through a DIFFERENT binding than the
#: sibling's production renderer does needs an explicit override row
#: here, not silent reliance on this derivation.
_HOME_MODULE: dict[str, ModuleType] = {name: mod for name, (mod, _render) in _RENDERERS.items()} | {
    "DEFAULT_PROMPT": init_cmd
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
        f"  3. Update _ROLES[{role!r}] in this file to the new "
        "(digest, length).\n"
        "\n"
        "If a *_PROMPT constant moved too, tests/test_prompt_versions.py\n"
        "will say so separately and its steps apply as well.\n"
    )


@pytest.mark.parametrize("role", sorted(_ROLES))
def test_delivered_prompt_digest_unchanged(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the digest, and is also the determinism control.

    Rendering twice against two different fixture roots and requiring
    equality first is what stops a flaky digest being "fixed" by
    normalising the varying part away: the fix someone reaches for under
    time pressure is to regex out whatever varies, which is how a digest
    goes blind."""
    _pin_delimiters(monkeypatch)
    spec = _ROLES[role]
    first = spec.render(tmp_path / "first")
    second = spec.render(tmp_path / "second")
    assert first.strip(), f"the {role} renderer produced nothing, so its digest pins nothing."
    assert first == second, (
        f"the {role} delivered text differs between two renders of the "
        "same fixture, so something in it varies per build."
    )
    actual = (_sha256(first), len(first))
    assert actual == (spec.digest, spec.length), _drift_message(
        role, (spec.digest, spec.length), actual
    )


def test_every_enrolled_prompt_is_delivered_somewhere() -> None:
    """The census. Every prompt H3 enrolls must reach some role's
    delivered text here, or its interpolation is unmeasured."""
    union: set[str] = set()
    for spec in _ROLES.values():
        union |= spec.covers
    # The 53 #303 builder fragments are single branches of a multi-branch
    # builder, so no one role's delivered text carries all of them. Their
    # delivered-output digests and orphan guards live in
    # tests/test_builder_prompts.py, not here.
    # The six #428 codebase scan notices are each returned VERBATIM by one
    # production path, and tests/test_prompt_versions.py proves it by
    # exact equality against a marker. Their delivered text therefore IS
    # the enrolled constant plus the integers and path named in the
    # template, so the constant's own hash is the delivered digest and a
    # second one here would pin the same bytes twice.
    missing = sorted(set(_PROMPTS) - union - BUILDER_RENDER_EXEMPT - set(NOTICE_PROMPTS))
    assert not missing, (
        f"enrolled prompts with no delivered digest: {missing}. Add each "
        "to the covers set of the role that carries it, and add a row to "
        "_ROLES if that role is new."
    )
    stale = sorted(union - set(_PROMPTS))
    assert not stale, f"a role's covers name prompts that are not enrolled for H3: {stale}."


@pytest.mark.parametrize("role", sorted(_ROLES))
def test_the_declared_constant_reaches_the_delivered_text(role: str, tmp_path: Path) -> None:
    """Each role's ``covers`` is a claim; this is what makes it true.

    Patch the constant to the orphan marker and the marker must appear
    in the delivered text. A row claiming a constant the role never
    reads would otherwise satisfy the census while measuring nothing."""
    spec = _ROLES[role]
    for const in sorted(spec.covers):
        with pytest.MonkeyPatch.context() as mp:
            _pin_delimiters(mp)
            mp.setattr(_HOME_MODULE[const], const, _ORPHAN_MARKER)
            delivered = spec.render(tmp_path / f"{role}-{const}")
        assert _MARKER_HEAD in delivered and _MARKER_TAIL in delivered, (
            f"the {role} role's covers names {const}, but patching "
            f"{_HOME_MODULE[const].__name__}.{const} changed nothing in "
            "its delivered text. Either the row is wrong or the renderer "
            "stopped reading the constant. A head present with no tail means "
            "the renderer reintroduced a size cap and is clipping the constant."
        )


# ---------------------------------------------------------------------------
# The end-to-end half. A digest of a builder cannot see what its CALLER
# adds after the builder returns, so these drive the real entry point
# with the builder replaced by ``_ORPHAN_MARKER`` and require the agent
# to receive it with nothing wrapped around it.
# ---------------------------------------------------------------------------


def _run_distill_and_capture_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(knowledge, "build_distill_prompt", lambda *a, **k: _ORPHAN_MARKER)
    prd_path = tmp_path / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(_PRD_JSON, encoding="utf-8")
    knowledge_root = tmp_path / "knowledge"
    agent = RecordingAgent(COMPLETION_MARKER)
    knowledge.distill_facts(
        agent,
        _COMPONENT,
        _DIFF_TEXT,
        prd_path,
        1,
        "run-1",
        knowledge_root,
        knowledge.KnowledgeConfig(knowledge_root=knowledge_root),
        tmp_path,
        review_passed=True,
    )
    assert agent.prompts, "distill_facts never called its agent, so this proves nothing."
    return agent.prompts[0]


def _run_decompose_and_capture_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(decompose, "build_decompose_prompt", lambda *a, **k: _ORPHAN_MARKER)
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(_SPEC_TEXT, encoding="utf-8")
    agent = RecordingAgent("not valid json")
    with pytest.raises(ValueError):
        decompose.decompose_spec(
            spec_path=spec_path,
            project_name="PROJECT",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
            max_retries=1,
        )
    assert agent.prompts, "decompose_spec never called its agent, so this proves nothing."
    return agent.prompts[0]


@pytest.mark.parametrize("which", ["review", "security", "distill", "decompose"])
def test_the_entry_point_delivers_the_builder_output_verbatim(
    which: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest of a builder says nothing about what the CALLER adds.

    Replace the builder with one returning ``_ORPHAN_MARKER``, drive the
    real phase, and the agent must receive both the marker's head and its
    tail: a builder digest alone would not catch a caller that appends,
    prepends, or truncates around the builder's output."""
    root = tmp_path / which
    root.mkdir(parents=True, exist_ok=True)
    if which == "review":
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: _ORPHAN_MARKER)
        delivered = _run_and_capture_prompt(which, root, monkeypatch)
    elif which == "security":
        monkeypatch.setattr(security, "_build_security_prompt", lambda *a, **k: _ORPHAN_MARKER)
        delivered = _run_and_capture_prompt(which, root, monkeypatch)
    elif which == "distill":
        delivered = _run_distill_and_capture_prompt(root, monkeypatch)
    else:
        delivered = _run_decompose_and_capture_prompt(root, monkeypatch)

    # ``startswith``/``endswith`` rather than a bare ``in`` check on each
    # half: a caller that APPENDS after the builder's output still
    # contains ``_MARKER_TAIL`` as a substring, so plain containment
    # would not catch it. Measured: with two ``in`` checks, appending
    # ``"\n\nIMPORTANT: be brief.\n"`` after ``build_review_prompt``
    # returns (the original plan's plant 6) left this test green -
    # `uv run pytest tests/test_delivered_prompts.py -k
    # test_the_entry_point_delivers_the_builder_output_verbatim` reported
    # `4 passed`. ``startswith``/``endswith`` catches that append, the
    # matching prepend, and a truncating mutation, because all three move
    # one end of the string away from the marker.
    assert delivered.startswith(_MARKER_HEAD) and delivered.endswith(_MARKER_TAIL), (
        f"{which} sends its agent something other than exactly what its "
        "builder returned, so the pinned digest above is not the whole "
        "delivered text.\n"
        f"  head: {delivered[:120]!r}\n"
        f"  tail: {delivered[-120:]!r}"
    )
