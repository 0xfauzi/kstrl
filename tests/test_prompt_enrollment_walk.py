"""H3: what the enrollment walk can and cannot see.

Split out of ``tests/test_prompt_versions.py`` when that file crossed the
repo's 800-line gate. The two jobs are genuinely separate: that file
records WHICH text is under snapshot and that it has not drifted, this
one is the discovery mechanism that decides what gets asked to enrol.
The seam is one import, the enrolled name set.

The walk keys on the target NAME plus a literal value: an assignment at
any nesting depth whose name ends in ``_PROMPT`` and whose right-hand
side is a string, an f-string, or (since #299) a concatenation or
``str.join`` of them. It is blind to instruction text that is never bound
to such a name at all, which is a residual H3 does not close; see the
H3-NOTE in ``tests/test_prompt_versions.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.test_prompt_versions import _PROMPTS

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


def _is_prompt_value(value: ast.expr | None) -> bool:
    """True when ``value`` is the AST of something that evaluates to a
    prompt body. ``None`` arises for annotated assignments without a
    right-hand side (``X: str``) and is treated as not-a-prompt.

    Accepts four shapes. A plain string literal and an f-string are the
    obvious two. The other two close a bypass #299 found: a name ending
    in ``_PROMPT`` escaped the walk entirely if its value was
    ``"a" + "b"`` (an ``ast.BinOp``) or ``"\n".join([...])`` (an
    ``ast.Call``), because both fail a literal-only test while passing
    the name test. H3a says naming a constant to dodge the walk is not an
    option; assembling its value out of pieces is the same dodge one
    level down, and unlike instruction text never bound to a ``*_PROMPT``
    name at all, this one IS mechanically detectable.

    Both additions are deliberately narrow, because this predicate also
    decides what is NOT a prompt. Concatenation counts only when a string
    literal appears somewhere in the tree, and a call counts only when it
    is ``<string literal>.join(...)``. So ``_ROOT_FROM_PROMPT =
    frozenset({...})`` in ``kstrl/cli.py``, a set of command names whose
    name happens to end in ``_PROMPT``, stays correctly invisible.
    """
    if value is None:
        return False
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return True
    if isinstance(value, ast.JoinedStr):
        return True
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return _is_prompt_value(value.left) or _is_prompt_value(value.right)
    if isinstance(value, ast.Call):
        func = value.func
        return (
            isinstance(func, ast.Attribute) and func.attr == "join" and _is_prompt_value(func.value)
        )
    return False


def _assigned_names(node: ast.AST) -> list[str]:
    """The ``Name`` targets of a prompt-shaped assignment, else ``[]``.

    Handles both binding forms in one place: ``NAME = "..."``
    (``ast.Assign``, which may bind several targets at once) and
    ``NAME: str = "..."`` (``ast.AnnAssign``, exactly one).
    """
    if isinstance(node, ast.Assign):
        targets: list[ast.expr] = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return []
    if not _is_prompt_value(node.value):
        return []
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _iter_prompt_names(tree: ast.AST) -> list[str]:
    """Every name in ``tree`` bound to a prompt-shaped value, any depth.

    ``ast.walk`` rather than ``tree.body``, so a declaration nested in a
    function, class or conditional is found too. First-seen order is
    preserved because callers assert on it.
    """
    names: list[str] = []
    for node in ast.walk(tree):
        names.extend(_assigned_names(node))
    return list(dict.fromkeys(names))


def _iter_modules(package_root: Path | None = None) -> list[tuple[Path, ast.AST, Path]]:
    """Parse every module under ``package_root`` (default: the real kstrl/).

    Returns ``(path, tree, root)`` triples, skipping anything that will
    not parse. One traversal shared by both consumers below: they used to
    rglob, read and parse all 123 files twice over.
    """
    root = package_root or (Path(__file__).resolve().parent.parent / "kstrl")
    parsed: list[tuple[Path, ast.AST, Path]] = []
    for py_file in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        parsed.append((py_file, tree, root))
    return parsed


def _module_level_prompt_constants(
    package_root: Path | None = None,
) -> dict[str, list[str]]:
    """Find every prompt-shaped assignment to a ``NAME`` ending in
    ``_PROMPT``. Returns ``{module_filename: [const_name, ...]}``.

    Catches every form a developer might use to declare a prompt: plain
    assignment, typed assignment, and declarations nested inside
    functions, classes or conditionals. Errs on the side of inclusion: a
    name that ends in ``_PROMPT`` with a prompt-shaped value is treated
    as a prompt regardless of nesting depth or annotation style.
    Exempt names are filtered here rather than in the shared helpers, so
    the staleness check below can still see them.

    ``package_root`` exists so the regression guards can exercise THIS
    function against synthetic modules instead of re-implementing the
    walk inline, which would guard nothing.
    """
    found: dict[str, list[str]] = {}
    for py_file, tree, root in _iter_modules(package_root):
        names = [
            name
            for name in _iter_prompt_names(tree)
            if name.endswith("_PROMPT") and name not in _ENROLLMENT_EXEMPT_NAMES
        ]
        if names:
            found[str(py_file.relative_to(root.parent))] = names
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


def test_ast_walker_catches_concatenated_body(tmp_path: Path) -> None:
    """Regression guard (#299): a name ending in ``_PROMPT`` whose value
    is built with ``+`` must still be discovered.

    Before this, ``X_PROMPT = _HEADER + "...instructions..."`` at module
    level passed the name test and failed the literal-only value test, so
    a prompt could ship with no version, no hash and no audit trail while
    ``test_no_unenrolled_prompt_constants`` stayed green. H3a calls naming
    a constant to dodge the walk not an option; this is the same dodge by
    value instead of by name, and it IS mechanically detectable."""
    pkg = _synthetic_module(
        tmp_path,
        'HEADER = "you are "\nCONCAT_PROMPT = HEADER + "a hostile reviewer"\n',
    )
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["CONCAT_PROMPT"],
    }, "AST walker failed to catch a concatenated prompt body."


def test_ast_walker_catches_joined_body(tmp_path: Path) -> None:
    """Regression guard (#299): the ``"\n".join([...])`` shape, the other
    way to assemble a body out of pieces at module level."""
    pkg = _synthetic_module(
        tmp_path,
        'JOINED_PROMPT = "\\n".join(["you are", "a hostile reviewer"])\n',
    )
    assert _module_level_prompt_constants(pkg) == {
        "synth_pkg/mod.py": ["JOINED_PROMPT"],
    }, "AST walker failed to catch a joined prompt body."


def test_ast_walker_ignores_non_string_calls(tmp_path: Path) -> None:
    """The widened value test must not sweep in every call.

    ``kstrl/cli.py`` really does hold ``_ROOT_FROM_PROMPT =
    frozenset({...})``, a set of command names whose name happens to end
    in ``_PROMPT``. It is not a prompt and must not be forced into
    enrollment, so a call counts only when it is
    ``<string literal>.join(...)``."""
    pkg = _synthetic_module(
        tmp_path,
        'SET_FROM_PROMPT = frozenset({"run", "understand"})\n',
    )
    assert _module_level_prompt_constants(pkg) == {}


def test_real_walk_does_not_flag_the_cli_command_set() -> None:
    """The same negative case, against the real kstrl/ tree rather than a
    synthetic one, so the exemption stays honest if cli.py moves."""
    discovered = _module_level_prompt_constants()
    flagged = [n for names in discovered.values() for n in names]
    assert "_ROOT_FROM_PROMPT" not in flagged, (
        "_ROOT_FROM_PROMPT is a frozenset of command names, not a prompt. "
        "The widened _is_prompt_value must not force it into enrollment."
    )


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
    """Every entry in ``_ENROLLMENT_EXEMPT_NAMES`` must reference a real
    string assignment somewhere in kstrl/.

    If you delete an exempt constant (say DEFAULT_CODEBASE_MAP goes from
    init_cmd.py), the exempt entry becomes dead code that silently masks
    a future name collision. Fail fast and make the developer remove the
    stale entry instead of letting it rot.

    Reuses ``_iter_modules`` / ``_iter_prompt_names`` rather than walking
    kstrl/ a second time, and deliberately does NOT filter on the
    ``_PROMPT`` suffix: most exempt names (DEFAULT_PROGRESS,
    DEFAULT_CODEBASE_MAP) do not carry it.
    """
    discovered: set[str] = set()
    for _py_file, tree, _root in _iter_modules():
        discovered.update(_iter_prompt_names(tree))
    stale = sorted(name for name in _ENROLLMENT_EXEMPT_NAMES if name not in discovered)
    assert not stale, (
        "_ENROLLMENT_EXEMPT_NAMES has stale entries that no longer "
        f"correspond to a string constant in kstrl/: {stale}. Remove "
        "them, otherwise the exemption silently masks any future name "
        "collision."
    )


def test_ast_walker_ignores_typed_assignment_without_value(
    tmp_path: Path,
) -> None:
    """``NAME: str`` with no right-hand side is not a prompt
    declaration -- ``_is_prompt_value(None)`` returns False, so the
    REAL walker reports nothing for it."""
    pkg = _synthetic_module(tmp_path, "EMPTY_PROMPT: str\n")
    assert _module_level_prompt_constants(pkg) == {}
