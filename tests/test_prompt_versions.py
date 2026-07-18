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
   AST-walks ralph_py/ for any module-level ``*_PROMPT`` constant and
   asserts it is enrolled in ``_PROMPTS``. Adding ``NEW_FANCY_PROMPT``
   without wiring up versioning fails the test.

H3-NOTE on enforcement limits: a sufficiently determined developer can
edit a prompt and update both the snapshot hash AND the version constant
to keep the *previous* number (e.g. leave version at 1.0.0 while moving
the hash). This is unenforceable in code; it requires reviewer
discipline. The H3 policy makes that bypass require explicit deceit in
the snapshot file, which is the audit trail.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest

from ralph_py.decompose import DECOMPOSE_PROMPT, DECOMPOSE_PROMPT_VERSION
from ralph_py.init_cmd import (
    DEFAULT_PROMPT,
    DEFAULT_PROMPT_VERSION,
)
from ralph_py.knowledge import DISTILL_PROMPT, DISTILL_PROMPT_VERSION
from ralph_py.review import REVIEWER_PROMPT, REVIEWER_PROMPT_VERSION
from ralph_py.security import SECURITY_PROMPT, SECURITY_PROMPT_VERSION


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_PROMPTS: dict[str, str] = {
    "DECOMPOSE_PROMPT": DECOMPOSE_PROMPT,
    "REVIEWER_PROMPT": REVIEWER_PROMPT,
    "SECURITY_PROMPT": SECURITY_PROMPT,
    "DISTILL_PROMPT": DISTILL_PROMPT,
    "DEFAULT_PROMPT": DEFAULT_PROMPT,
}

_VERSIONS: dict[str, str] = {
    "DECOMPOSE_PROMPT": DECOMPOSE_PROMPT_VERSION,
    "REVIEWER_PROMPT": REVIEWER_PROMPT_VERSION,
    "SECURITY_PROMPT": SECURITY_PROMPT_VERSION,
    "DISTILL_PROMPT": DISTILL_PROMPT_VERSION,
    "DEFAULT_PROMPT": DEFAULT_PROMPT_VERSION,
}

# Joint snapshot: (sha256_hash, semver_version). Both must move together
# when a prompt is edited; the test fails if either is stale.
_EXPECTED_SNAPSHOTS: dict[str, tuple[str, str]] = {
    "DECOMPOSE_PROMPT": (
        "9fddb3faf8509503f4a00b2e84ddf9be90e398c007a77a85720b371097a3627f",
        "1.3.0",
    ),
    "REVIEWER_PROMPT": (
        "4ba50b9ab16a8597459b0b5e15b70cb0b57f8c04d1b238d4340b5293388674a9",
        "1.1.0",
    ),
    "SECURITY_PROMPT": (
        "ad8fa39294e1adf3304c90af891286839c5dbe95b41ad29e222b2a45191c7be9",
        "1.1.0",
    ),
    "DISTILL_PROMPT": (
        "8040021a09d97598434d08c766495a4185df70b632e3ff4e5e1086b2e56ab30c",
        "1.1.0",
    ),
    "DEFAULT_PROMPT": (
        "aa7fa6acb045dc6105d1a4c4ce8b687e1e04289c7b751eb0373b7c59dca3f7ae",
        "1.1.0",
    ),
}

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Exemption set for the auto-discovery scan. These are user-facing
# scaffolding templates emitted by ``ralph init`` (progress log files,
# codebase_map.md, the understand/feature understand instructions); they
# generate documentation outputs, not adversarial-role outputs, and are
# out of scope for H3 snapshot protection.
#
# If you add a NEW template that produces user-facing content rather
# than adversarial-role output, add its name here with a one-line
# rationale. (DEFAULT_PRD_PROMPT was previously enrolled here but was
# deleted along with the manual `ralph prd create` path during the
# legacy-purge cleanup -- the factory is now the only PRD path.)
_ENROLLMENT_EXEMPT_NAMES = frozenset({
    "DEFAULT_PROGRESS",
    "DEFAULT_CODEBASE_MAP",
    "DEFAULT_FEATURE_UNDERSTAND",
    "DEFAULT_UNDERSTAND_PROMPT",
    "DEFAULT_FEATURE_UNDERSTAND_PROMPT",
})


def _drift_message(name: str, expected: tuple[str, str], actual: tuple[str, str]) -> str:
    exp_hash, exp_ver = expected
    act_hash, act_ver = actual
    parts = [f"{name} snapshot drift detected.\n"]
    if exp_hash != act_hash:
        parts.append(
            f"  Hash:    expected={exp_hash}\n           actual  ={act_hash}\n"
        )
    if exp_ver != act_ver:
        parts.append(
            f"  Version: expected={exp_ver!r:>10}    actual={act_ver!r}\n"
        )
    parts.append(
        "\nTo land this change:\n"
        "  1. Re-run calibration to verify detection rate did not regress:\n"
        "       RALPH_RUN_CALIBRATION=1 RALPH_CALIBRATION_MODEL=haiku "
        "uv run pytest tests/test_calibration.py -v\n"
        f"  2. Bump {name}_VERSION in ralph_py/ to a new semver "
        "(MAJOR for breaking taxonomy changes, MINOR for wording, PATCH for typos).\n"
        f"  3. Update _EXPECTED_SNAPSHOTS[{name!r}] in this file to the new "
        "(hash, version) tuple.\n"
        "Both writes are required. The PR diff with prompt + version + "
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


def test_decompose_prompt_snapshot_unchanged() -> None:
    _check_snapshot("DECOMPOSE_PROMPT")


def test_reviewer_prompt_snapshot_unchanged() -> None:
    _check_snapshot("REVIEWER_PROMPT")


def test_security_prompt_snapshot_unchanged() -> None:
    _check_snapshot("SECURITY_PROMPT")


def test_distill_prompt_snapshot_unchanged() -> None:
    _check_snapshot("DISTILL_PROMPT")


def test_default_engineer_prompt_snapshot_unchanged() -> None:
    """H3-engineer: the per-project ``scripts/ralph/prompt.md`` is
    user-editable, but the harness-shipped DEFAULT_PROMPT template at
    ``ralph_py/init_cmd.py`` is the adversarial-role definition for the
    engineer phase. Snapshot-protected on the same terms as the other
    role prompts."""
    _check_snapshot("DEFAULT_PROMPT")


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
    assert "Hash:" in message, (
        "snapshot failure must name the hash drift"
    )
    assert "Version:" not in message, (
        "version columns still agree; only the hash moved"
    )


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
# Auto-discovery: a new *_PROMPT in ralph_py/ without enrollment is a bug
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
    """Walk ``package_root`` (default: the real ralph_py/) and find every
    assignment of a string literal or f-string to a ``NAME`` ending in
    ``_PROMPT``. Returns ``{module_filename: [const_name, ...]}``.

    Catches **all** forms a developer might use to declare a prompt:

    - Plain assignment: ``NAME = "..."``  (``ast.Assign``)
    - Typed assignment: ``NAME: str = "..."``  (``ast.AnnAssign``)
    - Nested inside functions / classes / conditionals (via
      ``ast.walk``, not just ``tree.body``)

    Used by ``test_no_unenrolled_prompt_constants`` to enforce that
    every prompt-shaped constant in ``ralph_py/`` is enrolled in
    ``_PROMPTS``. The walker errs on the side of inclusion -- a const
    that ``ends in _PROMPT`` and has a string-shaped value is treated
    as a prompt regardless of nesting depth or annotation style.

    ``package_root`` exists so the regression-guard tests below can
    exercise THIS function against synthetic modules instead of
    re-implementing the walk inline (which would guard nothing).
    """
    found: dict[str, list[str]] = {}
    ralph_py = package_root or (
        Path(__file__).resolve().parent.parent / "ralph_py"
    )
    for py_file in sorted(ralph_py.rglob("*.py")):
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
            found[str(py_file.relative_to(ralph_py.parent))] = unique
    return found


def test_no_unenrolled_prompt_constants() -> None:
    """If someone adds ``NEW_PROMPT = \"...\"`` to a ralph_py module
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
        "Module-level *_PROMPT constants found in ralph_py/ that are NOT "
        "enrolled in H3 snapshot protection:\n  "
        + "\n  ".join(leaked)
        + "\n\nFor each, either:\n"
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
        tmp_path, 'TYPED_PROMPT: str = "you are a hostile reviewer"\n',
    )
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["TYPED_PROMPT"],
    }, "AST walker failed to catch typed-assignment prompt declaration."


def test_ast_walker_catches_nested_declaration(tmp_path: Path) -> None:
    """Regression guard: the REAL walker must catch ``NAME = "..."``
    declared inside a function or class body, not just at module level.
    Without this, wrapping a prompt declaration in
    ``def _build_default(): ...`` bypasses H3."""
    pkg = _synthetic_module(tmp_path, (
        "def _build_default():\n"
        '    NESTED_PROMPT = "you are a hostile reviewer"\n'
        "    return NESTED_PROMPT\n"
    ))
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["NESTED_PROMPT"],
    }, "AST walker failed to catch nested prompt declaration."


def test_ast_walker_skips_enrollment_exempt_names(tmp_path: Path) -> None:
    """The REAL walker must honor _ENROLLMENT_EXEMPT_NAMES (exempt
    scaffolding templates are not flagged) while still catching a
    non-exempt prompt in the same module."""
    pkg = _synthetic_module(tmp_path, (
        'DEFAULT_UNDERSTAND_PROMPT = "scaffolding template"\n'
        'REAL_PROMPT = "you are a hostile reviewer"\n'
    ))
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["REAL_PROMPT"],
    }


def test_enrollment_exempt_names_are_not_stale() -> None:
    """Every entry in ``_ENROLLMENT_EXEMPT_NAMES`` must reference a
    real module-level string assignment somewhere in ralph_py/. If you
    delete an exempt constant (e.g. you remove DEFAULT_CODEBASE_MAP
    from init_cmd.py), the exempt entry would become dead code that
    silently masks a future name collision.

    The test fails fast and forces the developer to remove the stale
    entry instead of letting it rot.
    """
    discovered_anywhere: set[str] = set()
    ralph_py = Path(__file__).resolve().parent.parent / "ralph_py"
    for py_file in sorted(ralph_py.rglob("*.py")):
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
    stale = [
        name for name in _ENROLLMENT_EXEMPT_NAMES
        if name not in discovered_anywhere
    ]
    assert not stale, (
        f"_ENROLLMENT_EXEMPT_NAMES has stale entries that no longer "
        f"correspond to a module-level string constant in ralph_py/: "
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
