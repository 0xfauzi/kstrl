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
   AST-walks kstrl/ for any module-level ``*_PROMPT`` constant and
   asserts it is enrolled in ``_PROMPTS``. Adding ``NEW_FANCY_PROMPT``
   without wiring up versioning fails the test.

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
   received different words. The two change-source functions are asserted
   to render exactly their enrolled body (#299).

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

import ast
import hashlib
import re
from pathlib import Path

import pytest

from kstrl import git, verify
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
from kstrl.review import REVIEWER_PROMPT, REVIEWER_PROMPT_VERSION
from kstrl.security import SECURITY_PROMPT, SECURITY_PROMPT_VERSION
from kstrl.verify import (
    VERIFY_COMMANDS_PROMPT,
    VERIFY_COMMANDS_PROMPT_VERSION,
    ResolvedVerifyCommands,
)


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

# Exemption set for the auto-discovery scan. These are user-facing
# scaffolding templates emitted by ``ks init`` (progress log files,
# codebase_map.md, the understand/feature understand instructions); they
# generate documentation outputs, not adversarial-role outputs, and are
# out of scope for H3 snapshot protection.
#
# If you add a NEW template that produces user-facing content rather
# than adversarial-role output, add its name here with a one-line
# rationale. (DEFAULT_PRD_PROMPT was previously enrolled here but was
# deleted along with the manual `kstrl prd create` path during the
# legacy-purge cleanup -- the factory is now the only PRD path.)
_ENROLLMENT_EXEMPT_NAMES = frozenset(
    {
        "DEFAULT_PROGRESS",
        "DEFAULT_CODEBASE_MAP",
        "DEFAULT_FEATURE_UNDERSTAND",
        "DEFAULT_UNDERSTAND_PROMPT",
        "DEFAULT_FEATURE_UNDERSTAND_PROMPT",
    }
)


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


# A snapshot pins the constant, not what the role is sent. Both blocks
# below are assembled at call time, so the constant could rot into an
# orphan -- still hashing clean while the function builds its own copy of
# the words, or wraps unhashed instructions around it. Each test asserts
# the render EQUALS the enrolled body (catching added or dropped words),
# then patches the constant and asserts the render followed (catching a
# copy that happens to be byte-identical today).
#
# Covered: the three prompts whose renderer takes no arguments this test
# cannot supply. Seven of the eight enrolled prompts are rendered through
# `.format` (DEFAULT_PROMPT alone ships verbatim); DECOMPOSE_PROMPT
# (decompose.py), REVIEWER_PROMPT (review.py), SECURITY_PROMPT
# (security.py) and DISTILL_PROMPT (knowledge.py) carry the same orphan
# exposure and have no guard. This is a guard on three renderers, not a
# general mechanism over the enrolled set.


def test_repo_change_source_renders_the_enrolled_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert repo_change_source("BASE_SHA") == REPO_CHANGE_SOURCE_PROMPT.format(
        base_ref="BASE_SHA"
    ), "repo_change_source renders something other than its enrolled body."
    monkeypatch.setattr(git, "REPO_CHANGE_SOURCE_PROMPT", "SENTINEL {base_ref}")
    assert repo_change_source("BASE_SHA") == "SENTINEL BASE_SHA", (
        "repo_change_source does not read REPO_CHANGE_SOURCE_PROMPT, so the "
        "enrolled constant is an orphan and the reviewer's words are not "
        "under H3 snapshot protection."
    )


def test_pasted_change_source_renders_the_enrolled_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block, token = pasted_change_source("DIFF BODY")
    assert block == PASTED_CHANGE_SOURCE_PROMPT.format(
        data_delimiter=token, diff_content="DIFF BODY"
    ), "pasted_change_source renders something other than its enrolled body."
    monkeypatch.setattr(
        git,
        "PASTED_CHANGE_SOURCE_PROMPT",
        "SENTINEL {data_delimiter} {diff_content}",
    )
    block, token = pasted_change_source("DIFF BODY")
    assert block == f"SENTINEL {token} DIFF BODY", (
        "pasted_change_source does not read PASTED_CHANGE_SOURCE_PROMPT, so "
        "the enrolled constant is an orphan and the reviewer's words are not "
        "under H3 snapshot protection."
    )


def test_verify_commands_render_the_enrolled_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VERIFY_COMMANDS_PROMPT reaches the engineer through
    ``ResolvedVerifyCommands.format_for_prompt`` (kstrl/verify.py), which
    carries the same orphan exposure as the change-source pair."""
    commands = ResolvedVerifyCommands(test="TEST CMD", typecheck="TYPECHECK CMD", lint="LINT CMD")
    assert commands.format_for_prompt() == VERIFY_COMMANDS_PROMPT.format(
        test="TEST CMD", typecheck="TYPECHECK CMD", lint="LINT CMD"
    ), "format_for_prompt renders something other than its enrolled body."
    monkeypatch.setattr(verify, "VERIFY_COMMANDS_PROMPT", "SENTINEL {test} {typecheck} {lint}")
    assert commands.format_for_prompt() == "SENTINEL TEST CMD TYPECHECK CMD LINT CMD", (
        "format_for_prompt does not read VERIFY_COMMANDS_PROMPT, so the "
        "enrolled constant is an orphan and the engineer's words are not "
        "under H3 snapshot protection."
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


def test_every_prompt_has_a_recorded_snapshot() -> None:
    for name in _PROMPTS:
        assert name in _EXPECTED_SNAPSHOTS, (
            f"{name} is missing a recorded snapshot in _EXPECTED_SNAPSHOTS. "
            "Every adversarial prompt must be snapshot-protected."
        )


# ---------------------------------------------------------------------------
# Auto-discovery: a new *_PROMPT in kstrl/ without enrollment is a bug
# ---------------------------------------------------------------------------


def _is_prompt_value(value: ast.expr | None) -> bool:
    """True when ``value`` is the AST of a string literal or f-string,
    i.e. plausibly a prompt body. ``None`` arises for annotated
    assignments without a right-hand side (``X: str``) and is treated
    as not-a-prompt."""
    if value is None:
        return False
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return True
    if isinstance(value, ast.JoinedStr):
        return True
    return False


def _module_level_prompt_constants(
    package_root: Path | None = None,
) -> dict[str, list[str]]:
    """Walk ``package_root`` (default: the real kstrl/) and find every
    assignment of a string literal or f-string to a ``NAME`` ending in
    ``_PROMPT``. Returns ``{module_filename: [const_name, ...]}``.

    Catches **all** forms a developer might use to declare a prompt:

    - Plain assignment: ``NAME = "..."``  (``ast.Assign``)
    - Typed assignment: ``NAME: str = "..."``  (``ast.AnnAssign``)
    - Nested inside functions / classes / conditionals (via
      ``ast.walk``, not just ``tree.body``)

    Used by ``test_no_unenrolled_prompt_constants`` to enforce that
    every prompt-shaped constant in ``kstrl/`` is enrolled in
    ``_PROMPTS``. The walker errs on the side of inclusion -- a const
    that ``ends in _PROMPT`` and has a string-shaped value is treated
    as a prompt regardless of nesting depth or annotation style.

    ``package_root`` exists so the regression-guard tests below can
    exercise THIS function against synthetic modules instead of
    re-implementing the walk inline (which would guard nothing).
    """
    found: dict[str, list[str]] = {}
    kstrl = package_root or (Path(__file__).resolve().parent.parent / "kstrl")
    for py_file in sorted(kstrl.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if not _is_prompt_value(node.value):
                    continue
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if not target.id.endswith("_PROMPT"):
                        continue
                    if target.id in _ENROLLMENT_EXEMPT_NAMES:
                        continue
                    names.append(target.id)
            elif isinstance(node, ast.AnnAssign):
                # Typed assignment: ``NAME: str = "..."``.
                if not _is_prompt_value(node.value):
                    continue
                target = node.target
                if not isinstance(target, ast.Name):
                    continue
                if not target.id.endswith("_PROMPT"):
                    continue
                if target.id in _ENROLLMENT_EXEMPT_NAMES:
                    continue
                names.append(target.id)
        if names:
            # Stable order: preserve first-seen ordering of AST walk.
            seen: set[str] = set()
            unique: list[str] = []
            for name in names:
                if name not in seen:
                    seen.add(name)
                    unique.append(name)
            found[str(py_file.relative_to(kstrl.parent))] = unique
    return found


def test_no_unenrolled_prompt_constants() -> None:
    """If someone adds ``NEW_PROMPT = \"...\"`` to a kstrl module
    without wiring it into ``_PROMPTS`` / ``_VERSIONS`` /
    ``_EXPECTED_SNAPSHOTS``, this test fails so the new prompt cannot
    silently slip past H3 protection."""
    discovered = _module_level_prompt_constants()
    enrolled = set(_PROMPTS.keys())
    leaked: list[str] = []
    for module_file, names in discovered.items():
        for name in names:
            if name not in enrolled:
                leaked.append(f"{module_file}::{name}")
    assert not leaked, (
        "Module-level *_PROMPT constants found in kstrl/ that are NOT "
        "enrolled in H3 snapshot protection:\n  " + "\n  ".join(leaked) + "\n\nFor each, either:\n"
        "  - Add a matching *_PROMPT_VERSION constant next to it and "
        "enroll in tests/test_prompt_versions.py::_PROMPTS, "
        "_VERSIONS, and _EXPECTED_SNAPSHOTS.\n"
        "  - OR add the constant name to _ENROLLMENT_EXEMPT_NAMES with a "
        "comment explaining why it is not an adversarial-role prompt."
    )


def _synthetic_module(tmp_path: Path, source: str) -> Path:
    """Write ``source`` as a module inside a synthetic package root and
    return the root, so the REAL walker can be pointed at it."""
    pkg = tmp_path / "synth_pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(source)
    return pkg


def test_ast_walker_catches_plain_assignment(tmp_path: Path) -> None:
    """Baseline regression guard for the real walker: the plain
    ``NAME = "..."`` form is discovered."""
    pkg = _synthetic_module(tmp_path, 'PLAIN_PROMPT = "you are a hostile reviewer"\n')
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["PLAIN_PROMPT"],
    }


def test_ast_walker_catches_typed_assignment(tmp_path: Path) -> None:
    """Regression guard: the REAL walker must catch ``NAME: str = "..."``
    in addition to ``NAME = "..."``. Without this, a developer can
    type-annotate the assignment and bypass H3 protection."""
    pkg = _synthetic_module(
        tmp_path,
        'TYPED_PROMPT: str = "you are a hostile reviewer"\n',
    )
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["TYPED_PROMPT"],
    }, "AST walker failed to catch typed-assignment prompt declaration."


def test_ast_walker_catches_nested_declaration(tmp_path: Path) -> None:
    """Regression guard: the REAL walker must catch ``NAME = "..."``
    declared inside a function or class body, not just at module level.
    Without this, wrapping a prompt declaration in
    ``def _build_default(): ...`` bypasses H3."""
    pkg = _synthetic_module(
        tmp_path,
        (
            "def _build_default():\n"
            '    NESTED_PROMPT = "you are a hostile reviewer"\n'
            "    return NESTED_PROMPT\n"
        ),
    )
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["NESTED_PROMPT"],
    }, "AST walker failed to catch nested prompt declaration."


def test_ast_walker_skips_enrollment_exempt_names(tmp_path: Path) -> None:
    """The REAL walker must honor _ENROLLMENT_EXEMPT_NAMES (exempt
    scaffolding templates are not flagged) while still catching a
    non-exempt prompt in the same module."""
    pkg = _synthetic_module(
        tmp_path,
        (
            'DEFAULT_UNDERSTAND_PROMPT = "scaffolding template"\n'
            'REAL_PROMPT = "you are a hostile reviewer"\n'
        ),
    )
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["REAL_PROMPT"],
    }


def test_enrollment_exempt_names_are_not_stale() -> None:
    """Every entry in ``_ENROLLMENT_EXEMPT_NAMES`` must reference a
    real module-level string assignment somewhere in kstrl/. If you
    delete an exempt constant (e.g. you remove DEFAULT_CODEBASE_MAP
    from init_cmd.py), the exempt entry would become dead code that
    silently masks a future name collision.

    The test fails fast and forces the developer to remove the stale
    entry instead of letting it rot.
    """
    discovered_anywhere: set[str] = set()
    kstrl = Path(__file__).resolve().parent.parent / "kstrl"
    for py_file in sorted(kstrl.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if not _is_prompt_value(node.value):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        discovered_anywhere.add(target.id)
            elif isinstance(node, ast.AnnAssign):
                if not _is_prompt_value(node.value):
                    continue
                if isinstance(node.target, ast.Name):
                    discovered_anywhere.add(node.target.id)
    stale = [name for name in _ENROLLMENT_EXEMPT_NAMES if name not in discovered_anywhere]
    assert not stale, (
        f"_ENROLLMENT_EXEMPT_NAMES has stale entries that no longer "
        f"correspond to a module-level string constant in kstrl/: "
        f"{stale}. Remove them, otherwise the exemption silently "
        "masks any future name collision."
    )


def test_ast_walker_ignores_typed_assignment_without_value(
    tmp_path: Path,
) -> None:
    """``NAME: str`` with no right-hand side is not a prompt
    declaration -- ``_is_prompt_value(None)`` returns False, so the
    REAL walker reports nothing for it."""
    pkg = _synthetic_module(tmp_path, "EMPTY_PROMPT: str\n")
    assert _module_level_prompt_constants(pkg) == {}
