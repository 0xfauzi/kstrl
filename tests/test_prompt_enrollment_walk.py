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
H3-NOTE in ``tests/test_prompt_versions.py`` and
``test_instruction_text_never_bound_to_a_prompt_name_is_invisible``
below, which pins it under ``xfail(strict=True)``.

TWO LAYERS, since #324.

LAYER 1, :data:`EXPECTED_PROMPT_NAME_SPELLINGS`, counts every node in
``kstrl/`` that writes a ``*_PROMPT`` identifier, per module. It
enumerates no node types and no field names, so it sees the shapes the
BINDING walk does not: a walrus, an import alias, the name as a string
inside a ``setattr``, a ``global`` declaration, a parameter. #324's
record is eleven guards each holed in the skip direction, and a matcher
that enumerates shapes is how every one of them got there.

LAYER 2 is the binding walk itself, which is what can say "add a version
constant next to it". Its two deliberate inversions are unchanged:
:func:`_is_prompt_value` is default-deny (is this value PROVABLY not a
string?) and the walk is depth-agnostic on purpose.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kstrl import cli
from tests.helpers import astwalk
from tests.test_prompt_versions import _PROMPTS

#: The suffix that puts a name under H3. One spelling, because both
#: layers below filter on it and the staleness check reads it too.
_PROMPT_SUFFIX = "_PROMPT"

# Exemption set for the auto-discovery scan. These are user-facing
# scaffolding templates emitted by ``ks init`` (progress log files,
# the understand and feature-understand instructions); they generate
# documentation outputs, not adversarial-role outputs, and are out of
# scope for H3 snapshot protection.
#
# Only names ending in ``_PROMPT`` can ever reach this set, because the
# walk filters on the suffix first. DEFAULT_PROGRESS, DEFAULT_CODEBASE_MAP
# and DEFAULT_FEATURE_UNDERSTAND were listed here until #299 round 2 and
# were inert: the suffix filter excluded them before the exemption was
# consulted, and the staleness check below dropped the suffix filter
# purely to keep them looking alive. Dead configuration that teaches the
# next reader a rule that does not exist.
#
# Whether these two belong here at all is an open question: both are full
# instruction bodies fed to an LLM through ``run_loop`` on ``ks
# understand`` and ``ks feature``, which H3a's wording arguably already
# covers. Recorded on #303; the exemption predates H3a and is left alone
# here.
#
# If you add a NEW template that produces user-facing content rather
# than adversarial-role output, add its name here with a one-line
# rationale. (DEFAULT_PRD_PROMPT was previously enrolled here but was
# deleted along with the manual `kstrl prd create` path during the
# legacy-purge cleanup -- the factory is now the only PRD path.)
_ENROLLMENT_EXEMPT_NAMES = frozenset(
    {
        "DEFAULT_UNDERSTAND_PROMPT",
        "DEFAULT_FEATURE_UNDERSTAND_PROMPT",
    }
)


#: Builtins whose call result cannot be a string. A ``*_PROMPT``-suffixed
#: name bound to one of these is not a prompt: ``kstrl/cli.py`` really
#: holds ``_ROOT_FROM_PROMPT = frozenset({"run", "understand", "feature"})``,
#: a set of command names. Kept small on purpose. Anything not named here
#: is treated as a possible prompt body, so the cost of an omission is a
#: spurious enrollment demand a human can answer, not a prompt that ships
#: unversioned.
_NOT_STRING_BUILTINS = frozenset(
    {
        "bool",
        "bytes",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "set",
        "tuple",
    }
)


def _is_prompt_value(value: ast.expr | None) -> bool:
    """True when ``value`` could evaluate to a string, so a ``*_PROMPT``
    name bound to it has to be enrolled or exempted.

    **This predicate is default-deny for prompts, not default-allow.**
    #299 round 1 tried the other way round, enumerating the shapes that
    ARE prompt bodies: string literal, f-string, ``"a" + "b"``,
    ``"lit".join(...)``. Round 2 measured that against the shapes a real
    multi-part prompt actually uses and found seven that slipped through
    with a ``*_PROMPT`` name at module level, among them
    ``textwrap.dedent(body)``, ``body.strip()``, ``A + B`` with both
    operands names, ``SEP.join([...])``, ``"..." % v``, a ternary, and a
    bare alias ``X_PROMPT = BASE``. An allowlist of shapes can only
    ever be as complete as the last person's imagination, and the whole
    point of H3 is that a prompt cannot ship unversioned by accident.

    So the question asked here is the negative one: is this value
    PROVABLY not a string? Only three answers count as proof, and each is
    visible in the syntax without resolving a name:

    - a non-string literal (``X_PROMPT = 3``),
    - a collection display (``{...}``, ``[...]``, ``(...)``, and the
      comprehension forms),
    - a call to a builtin that cannot return a string
      (``_NOT_STRING_BUILTINS``).

    Everything else, including every call, name, attribute, ternary and
    operator, is treated as a possible prompt body. ``None`` arises for
    an annotated assignment with no right-hand side (``X: str``) and is
    not a binding at all.

    The failure this trades for is a spurious demand to enrol something
    that is not a prompt. That is a conversation with a human, resolved
    by a line in ``_ENROLLMENT_EXEMPT_NAMES`` with a written reason. The
    failure it removes is a prompt reaching a role with no version, no
    hash and no calibration obligation, which nothing catches later.
    """
    if value is None:
        return False
    if isinstance(value, ast.Constant):
        return isinstance(value.value, str)
    if isinstance(
        value,
        ast.Set | ast.List | ast.Dict | ast.Tuple | ast.SetComp | ast.ListComp | ast.DictComp,
    ):
        return False
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _NOT_STRING_BUILTINS
    ):
        return False
    return True


def _assigned_names(node: ast.AST) -> list[str]:
    """The plain-name targets of a prompt-shaped binding, else ``[]``.

    ``astwalk.assignment_parts`` owns the three binding forms now:
    ``NAME = "..."`` (which may bind several targets at once),
    ``NAME: str = "..."`` and the WALRUS, which this file's own version
    did not read at all. An attribute target comes back dotted and is
    dropped here, because ``self.x`` is not a module-level constant
    anybody can enrol.
    """
    targets, value = astwalk.assignment_parts(node)
    if not _is_prompt_value(value):
        return []
    return [target for target in targets if target is not None and "." not in target]


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


def _package_root(package_root: Path | None) -> Path:
    """The tree to walk: the real ``kstrl/`` unless a fixture names one."""
    return package_root or astwalk.KSTRL_PACKAGE


def _iter_modules(package_root: Path | None = None) -> list[tuple[Path, ast.Module]]:
    """Every module under ``package_root`` (default: the real kstrl/).

    The parse comes from ``astwalk.parsed``, which is one cache for the
    whole session rather than this file's own. #299 round 1 claimed "one
    traversal shared by both consumers" while memoizing nothing, which
    made this file SLOWER than the two walks it replaced; the private
    ``functools.cache`` that fixed that is now nine other guards' cache
    too. Measured: 127 modules, 123 ms a pass, once per session.

    A file that will not parse now raises instead of being skipped.
    That direction is deliberate: a skipped module is a module this
    walk reports clean, which is the failure #324 is a record of.
    """
    root = _package_root(package_root)
    sources = astwalk.package_sources() if package_root is None else sorted(root.rglob("*.py"))
    return [(py_file, astwalk.parsed(py_file)) for py_file in sources]


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
    label_root = _package_root(package_root).parent
    for py_file, tree in _iter_modules(package_root):
        names = [
            name
            for name in _iter_prompt_names(tree)
            if name.endswith(_PROMPT_SUFFIX) and name not in _ENROLLMENT_EXEMPT_NAMES
        ]
        if names:
            found[astwalk.label(py_file, label_root)] = names
    return found


def _spells_a_prompt_name(node: ast.AST) -> bool:
    """Does this node write a ``*_PROMPT`` identifier anywhere it can?

    ``astwalk.spells`` asks exactly this for ONE token, by sweeping
    ``ast.iter_fields`` for strings. The subject here is a SUFFIX rather
    than a token, so the same sweep is done against ``str.endswith``;
    everything else, including the reason it is the strongest net in the
    file, is that function's docstring. It enumerates no node types and
    no field names, so it reaches ``Name.id``, ``Attribute.attr``,
    ``alias.asname``, ``arg.arg``, ``Global.names``, a bare string
    literal, and whatever identifier slot a future CPython adds.
    """
    for _field, value in ast.iter_fields(node):
        if isinstance(value, str):
            if value.endswith(_PROMPT_SUFFIX):
                return True
        elif isinstance(value, list) and any(
            isinstance(item, str) and item.endswith(_PROMPT_SUFFIX) for item in value
        ):
            return True
    return (astwalk.folded_str(node) or "").endswith(_PROMPT_SUFFIX)


#: Every node in ``kstrl/`` that writes a ``*_PROMPT`` identifier, per
#: module. Layer 1: a prompt cannot be declared, imported, passed or
#: read under such a name without one of these, whatever binding shape
#: it uses, so a name introduced by a shape ``_assigned_names`` does not
#: read - a ``for`` target, a ``with ... as``, a ``global``, a
#: ``setattr`` keyed by the string - still moves a row here.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds
#: one is where somebody says what the new name is and why it is or is
#: not a prompt body. ``git.py`` at four is two declarations and two
#: uses; ``init_cmd.py`` at twelve is ``DEFAULT_PROMPT`` plus the two
#: exempt scaffolding templates and their scaffold-ledger rows.
EXPECTED_PROMPT_NAME_SPELLINGS: dict[str, int] = {
    "cli.py": 2,  # _ROOT_FROM_PROMPT, which is a set of command names
    # #332 landed while this migration was in flight: the version
    # constant and the body of DECISIONS_CONTEXT_PROMPT, both enrolled.
    "decisions.py": 2,
    "decompose.py": 2,
    "git.py": 4,
    "init_cmd.py": 12,
    "knowledge.py": 2,
    "loop.py": 2,
    "review.py": 2,
    "security.py": 2,
    "verify.py": 2,
}


def test_every_module_that_spells_a_prompt_name_is_pinned() -> None:
    """Layer 1, the net: pin every mention of a ``*_PROMPT`` identifier.

    The binding walk below enumerates binding shapes, and #299 is the
    record of what enumerating shapes costs: round 1's predicate listed
    the shapes that ARE prompt bodies and seven ordinary ones walked
    past it. That predicate was inverted; the SHAPE list on the
    left-hand side never was. This layer has no shape list to be
    incomplete, so a ``*_PROMPT`` name bound by a ``for`` target, a
    ``with ... as`` or a ``global`` moves a count here even though
    ``_assigned_names`` cannot see it.

    ``assert_census`` refuses to pin an inventory whose predicate
    matched nothing in its own control, which is what stops this passing
    while switched off.
    """
    astwalk.assert_census(
        sources=astwalk.package_sources(),
        sees=_spells_a_prompt_name,
        expected=EXPECTED_PROMPT_NAME_SPELLINGS,
        control='NEW_PROMPT = "you are a hostile reviewer"\n',
        message=(
            "the set of places kstrl/ writes a *_PROMPT name changed. If this is a "
            "new prompt, enrol it in tests/test_prompt_versions.py; if it is not a "
            "prompt body, add the row with the reason."
        ),
    )


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
        "*_PROMPT constants found in kstrl/ that are NOT enrolled in H3 "
        "snapshot protection (the walk is depth-agnostic: a binding "
        "inside a function or class body counts too):\n  "
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
    return the root, so the REAL walker can be pointed at it.

    ``source`` is compiled first, and that check is load-bearing rather
    than tidiness. It says which of the two things went wrong: a fixture
    with an escaping slip fails HERE, naming the fixture, rather than
    inside the walk. Two of the fixtures below had exactly that slip
    while this was being written. (Until #324 ``_iter_modules`` SKIPPED a
    file it could not parse, so the same slip made an "ignores" test
    pass while asserting nothing; it raises now.)

    utf-8 on write to match the utf-8 read in ``_iter_modules``. The
    repo's encoding contract is two-sided (#291): a locale-default write
    under LC_ALL=C raises on the first curly quote, which is precisely
    the character LLM output puts in a prompt body.
    """
    pkg = tmp_path / "synth_pkg"
    pkg.mkdir()
    compile(source, "<synthetic fixture>", "exec")
    (pkg / "mod.py").write_text(source, encoding="utf-8")
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


#: Ways a real prompt body gets assembled. Every one of these was
#: INVISIBLE to the walk until #299 round 2 measured them: the predicate
#: enumerated the shapes that ARE prompts, so it only ever caught the
#: ones somebody had thought of. A body reaching a role through any of
#: these with no enrollment is a prompt with no version, no hash and no
#: calibration obligation.
_ASSEMBLED_BODIES: dict[str, str] = {
    "dedent": "import textwrap\nX_PROMPT = textwrap.dedent(BODY)\n",
    "strip": "X_PROMPT = BODY.strip()\n",
    "percent": 'X_PROMPT = "you are %s" % role\n',
    "sep_join": 'SEP = "\\n"\nX_PROMPT = SEP.join(PARTS)\n',
    "name_plus_name": "X_PROMPT = HEADER + BODY\n",
    "literal_plus_name": 'X_PROMPT = "you are " + ROLE\n',
    "literal_join": 'X_PROMPT = "\\n".join(PARTS)\n',
    "ternary": "X_PROMPT = HARD if strict else SOFT\n",
    "bare_alias": "X_PROMPT = _SHARED_BODY\n",
    "function_call": "X_PROMPT = build_body()\n",
    "attribute": "X_PROMPT = templates.reviewer\n",
    "fstring": 'X_PROMPT = f"you are {role}"\n',
}

#: Values that are PROVABLY not strings. These must stay invisible, or
#: the walk starts demanding enrollment for things that are not prompts.
#: The first is real: ``kstrl/cli.py`` holds ``_ROOT_FROM_PROMPT =
#: frozenset({...})``, a set of command names.
_NOT_BODIES: dict[str, str] = {
    "frozenset_call": 'X_PROMPT = frozenset({"run", "understand"})\n',
    "set_display": 'X_PROMPT = {"run", "understand"}\n',
    "list_display": 'X_PROMPT = ["run"]\n',
    "dict_display": 'X_PROMPT = {"a": 1}\n',
    "tuple_display": 'X_PROMPT = ("a", "b")\n',
    "int_literal": "X_PROMPT = 50_000\n",
    "none_literal": "X_PROMPT = None\n",
}


@pytest.mark.parametrize("shape", sorted(_ASSEMBLED_BODIES))
def test_ast_walker_catches_assembled_body(shape: str, tmp_path: Path) -> None:
    """Regression guard (#299): a ``*_PROMPT`` name must be discovered
    however its value was put together.

    The predicate asks whether a value is PROVABLY not a string, rather
    than whether it matches a known prompt shape, precisely so this table
    does not have to be complete to be safe. A shape nobody listed here
    is flagged anyway."""
    pkg = _synthetic_module(tmp_path, _ASSEMBLED_BODIES[shape])
    assert _module_level_prompt_constants(pkg) == {"synth_pkg/mod.py": ["X_PROMPT"]}, (
        f"the {shape} shape escaped the enrollment walk"
    )


@pytest.mark.parametrize("shape", sorted(_NOT_BODIES))
def test_ast_walker_ignores_non_string_values(shape: str, tmp_path: Path) -> None:
    """The widened predicate must not sweep in everything.

    Its default is "could be a prompt", so the only things that stay out
    are the ones the syntax proves are not strings. Get this wrong and
    the walk demands enrollment for a set of command names."""
    pkg = _synthetic_module(tmp_path, _NOT_BODIES[shape])
    assert _module_level_prompt_constants(pkg) == {}, (
        f"the {shape} shape was wrongly treated as a prompt body"
    )


def test_real_walk_does_not_flag_the_cli_command_set() -> None:
    """The same negative case against the real kstrl/ tree, so it stays
    honest if cli.py moves.

    The ``hasattr`` anchor is load-bearing: without it this passes
    vacuously the moment ``_ROOT_FROM_PROMPT`` is renamed or deleted,
    asserting nothing while still paying for a tree walk (#299 round 2)."""
    assert hasattr(cli, "_ROOT_FROM_PROMPT"), (
        "kstrl.cli._ROOT_FROM_PROMPT is gone, so this negative case no "
        "longer proves anything. Point it at whatever non-prompt "
        "*_PROMPT-suffixed name exists now, or delete it."
    )
    flagged = [name for names in _module_level_prompt_constants().values() for name in names]
    assert "_ROOT_FROM_PROMPT" not in flagged, (
        "_ROOT_FROM_PROMPT is a frozenset of command names, not a prompt. "
        "The predicate must not force it into enrollment."
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

    Filters on the ``_PROMPT`` suffix exactly as the walker does. Until
    #299 round 2 it deliberately did not, which kept three names alive in
    the exempt set that the walker's suffix filter could never consult.
    """
    discovered: set[str] = set()
    for _py_file, tree in _iter_modules():
        discovered.update(n for n in _iter_prompt_names(tree) if n.endswith(_PROMPT_SUFFIX))
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


def test_the_walk_reads_a_walrus_binding(tmp_path: Path) -> None:
    """The shape this file's own ``_assigned_names`` could not read.

    ``astwalk.assignment_parts`` covers ``Assign``, ``AnnAssign`` AND
    the walrus; the private version here read the first two. A walrus is
    what somebody writes to bind a body and use it in the same
    expression, and until #324 it was a ``*_PROMPT`` name with no
    version, no hash and no calibration obligation.
    """
    pkg = _synthetic_module(tmp_path, "def build():\n    return len(WALRUS_PROMPT := BODY)\n")
    assert _module_level_prompt_constants(pkg) == {"synth_pkg/mod.py": ["WALRUS_PROMPT"]}


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_instruction_text_never_bound_to_a_prompt_name_is_invisible() -> None:
    """The H3 residual, pinned rather than left as prose.

    Both layers key on the NAME. A body returned straight out of a
    function, or bound to a local called something else, is spelled
    nowhere either layer looks, and the fix is not a wider walk but the
    rule CLAUDE.md already states: hoist such text to an enrolled
    constant and interpolate the run-time values back in.

    Under ``xfail(strict=True)`` so that the day somebody does widen the
    walk, this XPASSes, that is a failure, and the H3-NOTE in
    ``tests/test_prompt_versions.py`` has to be edited in the same diff.
    """
    astwalk.blind_spot(
        lambda source: _iter_prompt_names(astwalk.parse(source)),
        'def reviewer_instructions() -> str:\n    return "You are a hostile reviewer."\n',
    )
