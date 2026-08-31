"""H3: Snapshot tests for the adversarial prompts.

These tests are the enforcement mechanism for the prompt-versioning policy
described in CLAUDE.md and docs/adversarial-design.md.

What this file protects against:

1. **Silent prompt drift.** Each prompt's SHA-256 is snapshotted alongside
   its semver version in ``_EXPECTED_SNAPSHOTS``. Editing the prompt body
   changes the hash and fails the snapshot test until both the version
   constant AND the recorded snapshot are updated. The two-write
   requirement is the audit trail that the change was reviewed.

2. **Version-without-hash drift.** If a developer bumps a ``*_PROMPT_VERSION``
   constant without updating the recorded snapshot, the snapshot test
   fails -- because the recorded version no longer matches the live one.

3. **Hash-without-version drift (silent version pin).** If a developer
   edits a prompt body (the live hash moves) while leaving the version
   constant pinned at its old value, the snapshot check fails on the
   hash mismatch alone -- the pinned version cannot mask the edit.
   ``test_no_silent_version_pin`` regression-guards exactly this
   failure mode by mutating a live prompt body and asserting the check
   raises on the hash while the version columns still agree. The
   residual bypass (updating the RECORDED snapshot hash while pinning
   both version stores) is out of in-process reach; see H3-NOTE below.

4. **New prompt without enrollment.** ``test_no_unenrolled_prompt_constants``
   AST-walks kstrl/ for any ``*_PROMPT`` constant and asserts it is
   enrolled in ``_PROMPTS``. Adding ``NEW_FANCY_PROMPT`` without wiring
   up versioning fails the test. That walk and its regression guards
   live in ``tests/test_prompt_enrollment_walk.py``; this file owns the
   enrolled set and the snapshots, that one owns discovery.

   What the walk keys on is the target NAME plus a literal value: an
   assignment at any nesting depth whose name ends in ``_PROMPT`` and
   whose right-hand side is a string or f-string. It is therefore blind
   to a body never bound to such a name, which is how two reviewer-facing
   blocks in ``kstrl/git.py`` shipped unversioned until #299. Hoisting
   such text to an enrolled constant is the rule; see H3-NOTE for what
   that leaves uncovered.

5. **Enrolled but unguarded.** The per-prompt hash check is parametrized
   over ``_PROMPTS`` rather than hand-written one test per prompt. The
   hand-written form checked only the prompts somebody remembered to
   write a test for: VERIFY_COMMANDS_PROMPT sat in all three dicts from
   #261 with no test calling ``_check_snapshot`` on it, so its body was
   editable with the suite staying green (#299).

6. **Enrolled but not the text that ships.** A constant a function has
   stopped interpolating would match its snapshot for ever while the role
   received different words. Every enrolled prompt that has a renderer is
   asserted to render exactly its enrolled body, parametrized over
   ``_RENDERERS``; a prompt in neither ``_RENDERERS`` nor
   ``_RENDER_EXEMPT`` fails ``test_every_prompt_has_a_renderer`` (#299).

7. **Enrolled body dropped from the prompt that carries it.** A leaf
   renderer can keep reading its constant while its CALLER stops calling
   it. ``test_change_source_reaches_the_role`` drives ``run_review`` and
   ``run_security_review`` themselves and asserts the change-source body
   reaches the prompt each role is sent. It goes through the phase entry
   points, not the builders: an earlier version passed that block in as
   an argument the test computed, so it could not see security.py hand
   the role a lookalike literal instead (#299).

8. **Enrolled template that cannot render.** Hashing a template and
   patching it away never executes it, so a body whose placeholders its
   renderer does not supply passed every H3 test and raised in
   production. ``test_the_real_enrolled_body_renders`` renders each real
   body through its production renderer (#299).

H3-NOTE on enforcement limits: a sufficiently determined developer can
edit a prompt and update both the snapshot hash AND the version constant
to keep the *previous* number (e.g. leave version at 1.0.0 while moving
the hash). This is unenforceable in code; it requires reviewer
discipline. The H3 policy makes that bypass require explicit deceit in
the snapshot file, which is the audit trail.

A second residual, found by #299 and NOT closed by it: nothing detects
instruction text that is never bound to a ``*_PROMPT`` name at all --
returned straight out of a function, or bound to a local called
something else. The walk can only flag a body somebody named. #299
hoisted the two known instances by hand and wrote the rule into H3;
keeping to it is reviewer discipline, not a check.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from kstrl import decompose, git, knowledge, review, security, verify
from kstrl.decompose import DECOMPOSE_PROMPT, DECOMPOSE_PROMPT_VERSION
from kstrl.git import (
    PASTED_CHANGE_SOURCE_PROMPT,
    PASTED_CHANGE_SOURCE_PROMPT_VERSION,
    REPO_CHANGE_SOURCE_PROMPT,
    REPO_CHANGE_SOURCE_PROMPT_VERSION,
    pasted_change_source,
    repo_change_source,
)
from kstrl.init_cmd import (
    DEFAULT_PROMPT,
    DEFAULT_PROMPT_VERSION,
)
from kstrl.knowledge import DISTILL_PROMPT, DISTILL_PROMPT_VERSION
from kstrl.manifest import Component
from kstrl.review import REVIEWER_PROMPT, REVIEWER_PROMPT_VERSION, ReviewMode
from kstrl.security import SECURITY_PROMPT, SECURITY_PROMPT_VERSION, SecurityConfig, SecurityMode
from kstrl.ui import PlainUI
from kstrl.verify import (
    VERIFY_COMMANDS_PROMPT,
    VERIFY_COMMANDS_PROMPT_VERSION,
    ResolvedVerifyCommands,
    VerificationResult,
)
from tests.conftest import make_review_repo
from tests.helpers.component_prd import write_component_prd
from tests.test_review_payload import RecordingAgent


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_PROMPTS: dict[str, str] = {
    "DECOMPOSE_PROMPT": DECOMPOSE_PROMPT,
    "REVIEWER_PROMPT": REVIEWER_PROMPT,
    "SECURITY_PROMPT": SECURITY_PROMPT,
    "DISTILL_PROMPT": DISTILL_PROMPT,
    "DEFAULT_PROMPT": DEFAULT_PROMPT,
    "VERIFY_COMMANDS_PROMPT": VERIFY_COMMANDS_PROMPT,
    "REPO_CHANGE_SOURCE_PROMPT": REPO_CHANGE_SOURCE_PROMPT,
    "PASTED_CHANGE_SOURCE_PROMPT": PASTED_CHANGE_SOURCE_PROMPT,
}

_VERSIONS: dict[str, str] = {
    "DECOMPOSE_PROMPT": DECOMPOSE_PROMPT_VERSION,
    "REVIEWER_PROMPT": REVIEWER_PROMPT_VERSION,
    "SECURITY_PROMPT": SECURITY_PROMPT_VERSION,
    "DISTILL_PROMPT": DISTILL_PROMPT_VERSION,
    "DEFAULT_PROMPT": DEFAULT_PROMPT_VERSION,
    "VERIFY_COMMANDS_PROMPT": VERIFY_COMMANDS_PROMPT_VERSION,
    "REPO_CHANGE_SOURCE_PROMPT": REPO_CHANGE_SOURCE_PROMPT_VERSION,
    "PASTED_CHANGE_SOURCE_PROMPT": PASTED_CHANGE_SOURCE_PROMPT_VERSION,
}

# Joint snapshot: (sha256_hash, semver_version). Both must move together
# when a prompt is edited; the test fails if either is stale.
_EXPECTED_SNAPSHOTS: dict[str, tuple[str, str]] = {
    "DECOMPOSE_PROMPT": (
        "8bce50b09f19220e58d941fe0b99a0f45d0c4e003d90a40c7570a4af542b1452",
        "1.4.2",
    ),
    # 2.0.0 (#266): MAJOR, because the change-acquisition contract and
    # the output schema both broke. Neither prompt carries a diff any
    # more - the reviewers run inside the worktree, so both bodies now
    # instruct them to run `git diff <base>...HEAD` themselves - and the
    # whole "Truncated and chunked diffs" section is gone with the
    # machinery it described. In its place both schemas gained a
    # MANDATORY "observedDiffstat" field, which is the replacement for
    # the guarantee chunking used to give: the harness runs the same
    # numstat and refuses, in hard mode, a review whose reported figure
    # is not git's. Both bodies also gained the instruction that the
    # engineer's own "## Self-Critique" block is a claim and not
    # evidence, which used to be enforced by deleting it from the
    # pasted diff (E2) and cannot be, now that nothing is pasted.
    "REVIEWER_PROMPT": (
        "d3ecc207c3628737bb2ca8f1452a4920de9da5073d1dd03d532b89dae50a47fa",
        "2.0.0",
    ),
    "SECURITY_PROMPT": (
        "f2f6b87779fc3e203de9689f4c74cb5b17ba361be7195223c10c1376eb2b6a84",
        "2.0.0",
    ),
    "DISTILL_PROMPT": (
        "8040021a09d97598434d08c766495a4185df70b632e3ff4e5e1086b2e56ab30c",
        "1.1.0",
    ),
    # 1.3.0 (#276): step 9 now defers to the VERIFY_COMMANDS_PROMPT block
    # rather than telling the engineer to derive its own typecheck and
    # test commands, and it names lint - a blocking Phase 1 gate the
    # previous body never mentioned. Step 14's duplicate done-rule, which
    # carried the same lint omission, now refers to step 9. 1.2.0 was an
    # unreleased draft of the same change, revised in review and never
    # pinned here; the version moved with the body rather than being
    # reused, so each round carries its own audit trail.
    #
    # Scope (H3-engineer): the per-project scripts/kstrl/prompt.md is
    # user-editable, but this harness-shipped template is the
    # adversarial-role definition for the engineer phase and is
    # snapshot-protected on the same terms as the role prompts.
    "DEFAULT_PROMPT": (
        "392eb698daf71d486a9d4573698df3bb2b3ca4be87c178657accc8a66c54f384",
        "1.3.0",
    ),
    # 1.0.0 (#261): harness-authored instruction text prepended to the
    # engineer prompt every iteration, naming the commands Phase 1 will
    # run. Enrolled because it steers the engineer exactly as
    # DEFAULT_PROMPT does. The TEMPLATE is what is hashed; the three
    # command values are the operator's and are interpolated at run time.
    "VERIFY_COMMANDS_PROMPT": (
        "2b78ef192783332e3693d197fc135460a46275df30411a500d55902d0a9c5e4b",
        "1.0.0",
    ),
    # 1.0.0 (#299): both bodies already reached a reviewer's prompt on
    # every run; #299 only hoisted them out of the functions that built
    # them, and the text did not change by a byte. So 1.0.0 opens a
    # series for a body whose earlier edits predate any version at all -
    # the footing VERIFY_COMMANDS_PROMPT was enrolled on in #261.
    # Back-dating to 1.1.0 would imply a 1.0.0 that never existed.
    "REPO_CHANGE_SOURCE_PROMPT": (
        "a631e04c744b55157f9e023d352b572eb8f5e6a5148c9f9114eaf26f6a359cb5",
        "1.0.0",
    ),
    "PASTED_CHANGE_SOURCE_PROMPT": (
        "a1e6082933043d31c9efc513c0e16466629ccf770f6e6e828ace39565736d0d5",
        "1.0.0",
    ),
}

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _drift_message(name: str, expected: tuple[str, str], actual: tuple[str, str]) -> str:
    exp_hash, exp_ver = expected
    act_hash, act_ver = actual
    parts = [f"{name} snapshot drift detected.\n"]
    if exp_hash != act_hash:
        parts.append(f"  Hash:    expected={exp_hash}\n           actual  ={act_hash}\n")
    if exp_ver != act_ver:
        parts.append(f"  Version: expected={exp_ver!r:>10}    actual={act_ver!r}\n")
    parts.append(
        "\nTo land this change:\n"
        "  1. Re-run calibration to verify detection rate did not regress:\n"
        "       KSTRL_RUN_CALIBRATION=1 KSTRL_CALIBRATION_MODEL=haiku "
        "uv run pytest tests/test_calibration.py -v\n"
        f"  2. Bump {name}_VERSION in kstrl/ to a new semver "
        "(MAJOR for breaking taxonomy changes, MINOR for wording, PATCH for typos).\n"
        f"  3. Update _EXPECTED_SNAPSHOTS[{name!r}] in this file to the new "
        "(hash, version) tuple.\n"
    )
    if name == "DEFAULT_PROMPT":
        parts.append(
            "  4. APPEND the new (hash, version) row to the prompt.md entry of "
            "SCAFFOLDED_TEMPLATES in kstrl/init_cmd.py, keeping every older "
            "row (#286). Without it the bump reaches no already-initialised "
            "project and nothing can tell an operator their prompt.md is "
            "behind.\n"
        )
    parts.append(
        "All of these writes are required. The PR diff with prompt + version + "
        "snapshot all moving is the audit trail.\n"
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Joint snapshot tests
# ---------------------------------------------------------------------------


def _check_snapshot(name: str) -> None:
    actual = (_sha256(_PROMPTS[name]), _VERSIONS[name])
    expected = _EXPECTED_SNAPSHOTS[name]
    assert actual == expected, _drift_message(name, expected, actual)


@pytest.mark.parametrize("name", sorted(_PROMPTS))
def test_prompt_snapshot_unchanged(name: str) -> None:
    """Every enrolled prompt's (hash, version) matches its snapshot.

    Parametrized over ``_PROMPTS`` so enrollment is the only thing anyone
    has to remember; see module docstring item 5 for why."""
    _check_snapshot(name)


# A snapshot pins the constant, not what the role is sent. Every enrolled
# body below is assembled at call time, so the constant can rot into an
# orphan: still hashing clean while the renderer builds its own copy of
# the words, wraps unhashed instructions around it, or stops reading it
# altogether.
#
# The guard patches the constant to a marker carrying no fields, then
# asserts the renderer returns THAT and nothing else. ``str.format``
# ignores keyword arguments a template does not use, so one fieldless
# marker works for every renderer whatever its field set, and the test
# never has to recompute what the production builder passes in. Wrapping,
# truncating or ignoring the constant each break the equality.
#
# Parametrized over _RENDERERS rather than hand-written per prompt: the
# hand-written form is what left VERIFY_COMMANDS_PROMPT unguarded from
# #261 to #299, and repeating it here would rebuild that defect one level
# down.

_MARKER_HEAD = "<<<H3-RENDER-GUARD-START>>>"
_MARKER_TAIL = "<<<H3-RENDER-GUARD-END>>>"

#: Deliberately longer than the largest cap in the codebase, and sized
#: FROM that constant so it tracks it. #299 round 2 measured the earlier
#: 52-character marker against a renderer that appended ``[:4000]`` and
#: found all nine guards green: the marker never reached the cap, so
#: truncation was invisible to a test whose comment claimed it caught
#: truncation. A renderer reintroducing a size cap ships a clipped role
#: prompt, and ``git.truncate_diff_for_prompt`` already exists and is
#: used at other paste sites, so this is a live shape rather than a
#: hypothetical one.
#:
#: The honest limit: this catches any cap at or below the marker length.
#: A renderer that capped ABOVE it would still pass, and no fixed marker
#: can close that.
_ORPHAN_MARKER = _MARKER_HEAD + "." * (git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT + 1) + _MARKER_TAIL


def _reviewer_render(tmp_path: Path) -> str:
    prd_path = write_component_prd(tmp_path, "prd.json")
    return review.build_review_prompt(
        prd_path, "BASE_SHA", VerificationResult(passed=True, checks=[])
    )


def _distill_render(_tmp_path: Path) -> str:
    component = Component(
        id="C1",
        title="T",
        description="D",
        dependencies=[],
        prd_path="prd.json",
        branch_name="b",
    )
    return knowledge.build_distill_prompt(component, 5, "PRD", "FACTS", "DIFF")


def _verify_render(_tmp_path: Path) -> str:
    commands = ResolvedVerifyCommands(test="T", typecheck="TC", lint="L")
    return commands.format_for_prompt()


#: ``{enrolled prompt: (module holding the constant, production renderer)}``.
#: The module is the patch target; the renderer is the path the role's
#: text actually travels.
_RENDERERS: dict[str, tuple[ModuleType, Callable[[Path], str]]] = {
    "DECOMPOSE_PROMPT": (
        decompose,
        lambda _p: decompose.build_decompose_prompt("PROJECT", "SPEC"),
    ),
    "REVIEWER_PROMPT": (review, _reviewer_render),
    "SECURITY_PROMPT": (
        security,
        lambda _p: security._build_security_prompt("PRD", "CHANGE SOURCE", "TOKEN"),
    ),
    "DISTILL_PROMPT": (knowledge, _distill_render),
    "VERIFY_COMMANDS_PROMPT": (verify, _verify_render),
    "REPO_CHANGE_SOURCE_PROMPT": (git, lambda _p: repo_change_source("BASE_SHA")),
    "PASTED_CHANGE_SOURCE_PROMPT": (git, lambda _p: pasted_change_source("DIFF")[0]),
}

#: Enrolled prompts with no renderer, and why. DEFAULT_PROMPT is written
#: to disk verbatim by ``ks init`` and read back by ``run_loop``; it is
#: never interpolated, so there is no render step to orphan. Its reach is
#: covered by H3b's scaffold ledger instead
#: (``test_engineer_prompt_bump_reaches_existing_projects``).
_RENDER_EXEMPT = frozenset({"DEFAULT_PROMPT"})


def test_every_prompt_has_a_renderer() -> None:
    """A ninth enrolled prompt gets a hash check for free; without this it
    would get a render guard only if its author remembered."""
    unclassified = sorted(set(_PROMPTS) - set(_RENDERERS) - _RENDER_EXEMPT)
    assert not unclassified, (
        f"Enrolled prompts with no orphan guard: {unclassified}. Add each "
        "to _RENDERERS with the production function that renders it, or to "
        "_RENDER_EXEMPT with a written reason why it has no render step to "
        "orphan."
    )


def test_no_stale_renderer_entries() -> None:
    """Neither table may name a prompt that is no longer enrolled, or the
    entry rots into dead configuration nothing exercises."""
    stale = sorted((set(_RENDERERS) | _RENDER_EXEMPT) - set(_PROMPTS))
    assert not stale, f"_RENDERERS/_RENDER_EXEMPT name unenrolled prompts: {stale}."


@pytest.mark.parametrize("name", sorted(_RENDERERS))
def test_the_real_enrolled_body_renders(name: str, tmp_path: Path) -> None:
    """The enrolled template must actually format with the fields its
    production renderer supplies.

    Every other H3 test either hashes the template or patches it away, so
    until #299 round 2 nothing in the suite ever rendered a real body.
    Measured: adding ``Extra note about {bogus_field}.`` to
    VERIFY_COMMANDS_PROMPT and updating hash and version, which is
    exactly the flow ``_drift_message`` prescribes, passed all 37 H3
    tests while production ``format_for_prompt()`` raised
    ``KeyError: 'bogus_field'``.

    This also carries the cost of #299's own change. Both change-source
    bodies were f-strings before it and are ``.format()`` templates
    after, which demotes an unbalanced or stray brace from a SyntaxError
    at ``import kstrl.git`` to a KeyError or ValueError at call time. A
    prompt body asking for JSON output is exactly where a stray brace
    appears. This test is what puts that error back in the suite."""
    _module, render = _RENDERERS[name]
    rendered = render(tmp_path)
    assert rendered.strip(), f"{name} rendered empty through its production renderer."


@pytest.mark.parametrize("name", sorted(_RENDERERS))
def test_renderer_renders_the_enrolled_body(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, render = _RENDERERS[name]
    monkeypatch.setattr(module, name, _ORPHAN_MARKER)
    rendered = render(tmp_path)
    if rendered != _ORPHAN_MARKER:
        pytest.fail(
            f"{module.__name__} does not render {name} verbatim, so the "
            "enrolled constant is not what the role receives. Either the "
            "renderer stopped reading it (an orphan constant that will "
            "match its snapshot for ever), it wraps text around it that "
            "no snapshot covers, or it truncates it.\n"
            f"  expected {len(_ORPHAN_MARKER)} chars, got {len(rendered)}\n"
            f"  head: {rendered[:80]!r}\n"
            f"  tail: {rendered[-80:]!r}"
        )


def _run_and_capture_prompt(which: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Drive the real phase entry point and return the prompt it sent.

    Both phases are exercised through ``run_review`` / ``run_security_review``
    rather than through the builders they call. #299 round 1 asserted on
    ``_build_security_prompt(prd, git.repo_change_source(sha))``, which
    passes the change-source block in as an argument the TEST computed,
    so it could never observe ``security.py`` handing the reviewer
    something else. Measured: replacing that call site with a lookalike
    f-string left all 480 selected tests green while the review half of
    the same mutation failed. The asymmetry was the tell.
    """
    repo = make_review_repo(tmp_path / f"repo-{which}")
    (repo.path / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}', encoding="utf-8"
    )
    agent = RecordingAgent("not valid json")
    ui = PlainUI(no_color=True)

    if which == "review":
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
        security.run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            SecurityConfig(mode=SecurityMode.ADVISORY.value),
            ui,
        )

    assert agent.prompts, (
        f"the {which} phase never called its agent, so this test proves "
        "nothing about what the role was sent."
    )
    return agent.prompts[0]


@pytest.mark.parametrize("which", ["review", "security"])
def test_change_source_reaches_the_role(
    which: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leaf guard proves ``repo_change_source`` reads its constant.
    It cannot prove the CALLER still calls it.

    Drop ``change_source=git.repo_change_source(...)`` from ``review.py``
    or ``security.py``, or replace it with a lookalike literal, and every
    other test here stays green while the role is told how to obtain the
    change by words under no snapshot. That is the exact H3 hole #299 was
    filed for, one call site further out."""
    monkeypatch.setattr(git, "REPO_CHANGE_SOURCE_PROMPT", _ORPHAN_MARKER)
    prompt = _run_and_capture_prompt(which, tmp_path, monkeypatch)
    assert _MARKER_HEAD in prompt and _MARKER_TAIL in prompt, (
        f"the {which} prompt no longer carries repo_change_source's "
        "enrolled body, so that role's change-acquisition instructions "
        "are outside H3 snapshot protection."
    )


def test_engineer_prompt_bump_reaches_existing_projects() -> None:
    """H3's reach (#286): a version bump that is not also recorded in the
    scaffold ledger is invisible to every already-initialised project.

    ``ks init`` never overwrites ``scripts/kstrl/prompt.md``, so the only
    thing that can tell an operator their copy is behind is the ledger of
    bodies the harness has shipped. This test fails the moment
    DEFAULT_PROMPT moves without a matching row, in the same file the
    person doing the bump is already editing. The deeper invariants live
    in tests/test_prompt_staleness.py."""
    from kstrl.init_cmd import SCAFFOLDED_TEMPLATES

    template = next(t for t in SCAFFOLDED_TEMPLATES if t.filename == "prompt.md")
    assert template.history[-1] == (_sha256(DEFAULT_PROMPT), DEFAULT_PROMPT_VERSION), (
        "SCAFFOLDED_TEMPLATES in kstrl/init_cmd.py does not end with the "
        "engineer prompt this harness ships. APPEND "
        f"({_sha256(DEFAULT_PROMPT)!r}, {DEFAULT_PROMPT_VERSION!r}) to its "
        "history and keep every older row."
    )


def test_no_silent_version_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """R4.3: a prompt-body edit with the version left pinned MUST fail.

    Simulates the exact drift the H3 policy exists to catch: the live
    prompt body changes (its hash moves) while the ``*_PROMPT_VERSION``
    constant stays at the recorded value. The snapshot check must raise
    on the hash mismatch alone -- the agreeing versions must not let
    the edit slip through -- and its message must direct the developer
    at the hash, not the version."""
    name = "REVIEWER_PROMPT"
    monkeypatch.setitem(_PROMPTS, name, _PROMPTS[name] + "\nsilent edit")
    with pytest.raises(AssertionError) as exc_info:
        _check_snapshot(name)
    message = str(exc_info.value)
    assert "Hash:" in message, "snapshot failure must name the hash drift"
    assert "Version:" not in message, "version columns still agree; only the hash moved"


# ---------------------------------------------------------------------------
# Structural integrity
# ---------------------------------------------------------------------------


def test_all_prompt_versions_are_semver() -> None:
    for name, value in _VERSIONS.items():
        assert _SEMVER_RE.match(value), (
            f"{name}_VERSION={value!r} must be semver (MAJOR.MINOR.PATCH)."
        )


def test_versions_and_snapshots_agree_on_version_string() -> None:
    """Catches the case where a developer updates ``_EXPECTED_SNAPSHOTS``
    but forgets to update the matching ``*_PROMPT_VERSION`` constant
    (or vice versa). Both stores of the version string must match."""
    for name in _PROMPTS:
        live_version = _VERSIONS[name]
        recorded_version = _EXPECTED_SNAPSHOTS[name][1]
        assert live_version == recorded_version, (
            f"Version drift for {name}: "
            f"live constant says {live_version!r}, "
            f"_EXPECTED_SNAPSHOTS says {recorded_version!r}. "
            "Either bump the constant to match the snapshot, or update "
            "the snapshot to match the constant. They must agree."
        )


def test_every_prompt_has_a_version() -> None:
    for prompt_name in _PROMPTS:
        assert prompt_name in _VERSIONS, (
            f"{prompt_name} is missing a {prompt_name}_VERSION constant. "
            "Every adversarial prompt must declare a semver version."
        )


def test_every_version_has_a_prompt() -> None:
    for prompt_name in _VERSIONS:
        assert prompt_name in _PROMPTS, (
            f"{prompt_name}_VERSION declared but no matching prompt body. "
            "Dead version constants drift; remove them."
        )


def test_every_snapshot_has_a_prompt() -> None:
    """The reverse of ``test_every_prompt_has_a_recorded_snapshot``.

    The hash check is now derived from ``_PROMPTS``, so a snapshot row
    whose prompt was deleted is parametrized over by nothing and silently
    stops meaning what it says. That is what happened to
    DEFAULT_PRD_PROMPT. Dead rows rot; remove them."""
    for name in _EXPECTED_SNAPSHOTS:
        assert name in _PROMPTS, (
            f"_EXPECTED_SNAPSHOTS has a row for {name!r}, which is not an "
            "enrolled prompt. If the prompt was deleted, delete its "
            "snapshot row too: nothing checks it any more."
        )


def test_every_prompt_has_a_recorded_snapshot() -> None:
    for name in _PROMPTS:
        assert name in _EXPECTED_SNAPSHOTS, (
            f"{name} is missing a recorded snapshot in _EXPECTED_SNAPSHOTS. "
            "Every adversarial prompt must be snapshot-protected."
        )
