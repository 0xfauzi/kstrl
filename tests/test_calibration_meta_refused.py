"""A fixture meta no matcher can read is refused, never scored (#564).

The calibration matchers used to read a fixture's matcher block with a
default: ``_SEVERITY_ORDER.get(threshold, 0)`` ranked a mistyped
``severity_at_least`` 0, so every finding met it and the planted-bug
fixture scored as caught whatever the reviewer reported. A misspelt
``evidence_path_contains`` switched the path gate off the same way, and a
misspelt ``categories`` made a negative fixture score as clean. Now every
field a matcher reads is required, checked against the vocabulary the
matcher compares it with, and a block may carry no field a matcher does
not read. The loader and the prompt optimizer refuse such a meta before
any agent call, naming the fixture and the field.

Two layers guard the next matcher field read with a default:

- BEHAVIOUR, closed over the saved data. Every field of every ``must_*``
  block in every saved meta is deleted, nulled, given a sibling no matcher
  reads, and given the wrong JSON type, and each must be refused by the
  reader AND by the matcher that grades the block. A field read with a
  default in place of a refusal fails here the moment any saved meta
  carries it.
- STATIC, a census of every read with a fallback (``.get``,
  ``.setdefault``, ``.pop``, three-argument ``getattr``, a key-membership
  test, an ``except`` that catches a missing key) in every scope of
  ``kstrl/calibration_score.py`` and in every function annotated
  ``-> tuple[bool, str]`` (a matcher's verdict) in the test-side modules
  that hold matchers. This sees a new field no saved meta carries yet.
  Its blind spots are recorded below as strict xfails.
"""

from __future__ import annotations

import ast
import copy
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kstrl import gepa_adapter
from kstrl.calibration_score import (
    FixtureMetaError,
    fixture_meta_errors,
    reviewer_caught,
    reviewer_false_positive,
    security_caught,
    security_false_positive,
)
from kstrl.gepa_adapter import RoleFixture, run_optimization
from kstrl.review import ReviewResult
from kstrl.security import SECURITY_PROMPT, SecurityMode, SecurityResult, parse_security_output
from tests.helpers import calibration_repo_fixture
from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    REPO_ROOT,
    all_nodes,
    assert_census,
    blind_spot,
    label,
    parse,
    parsed,
)
from tests.helpers.astwalk.scope import own_nodes, scope_of, scopes
from tests.helpers.calibration_repo_fixture import (
    FIXTURES_DIR,
    RepoSpecFixture,
    load_fixtures,
    names_existing_module,
)
from tests.test_calibration import architect_allowed_paths_caught, architect_caught

# ---------------------------------------------------------------------------
# The saved metas
# ---------------------------------------------------------------------------

#: Graded by kstrl.integration's own reader through
#: tests/helpers/calibration_integration_fixture.py, not by these matchers,
#: and checked by tests/test_calibration_integration_fixture.py.
NOT_READ_HERE = frozenset({"integration", "integration_clean"})

SAVED = sorted(
    path
    for path in FIXTURES_DIR.glob("*/*.meta.json")
    if json.loads(path.read_text(encoding="utf-8"))["role"] not in NOT_READ_HERE
)


def _meta(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _rel(path: Path) -> str:
    return path.relative_to(FIXTURES_DIR).as_posix()


_NO_FINDINGS = SecurityResult(passed=True, mode=SecurityMode.HARD.value, findings=[])
_NO_CONCERNS = ReviewResult(passed=True, mode="hard", criteria=[], concerns=[])

#: The matcher that grades each (role, block). A saved meta whose pair is
#: missing here fails ``test_every_saved_block_has_its_matcher`` rather
#: than being left out of the mutation cases.
MATCHERS: dict[tuple[str, str], Callable[[Any], tuple[bool, str]]] = {
    ("security", "must_detect"): lambda block: security_caught(_NO_FINDINGS, block),
    ("security", "must_not_flag"): lambda block: security_false_positive(_NO_FINDINGS, block),
    ("reviewer", "must_detect"): lambda block: reviewer_caught(_NO_CONCERNS, block),
    ("reviewer", "must_not_flag"): lambda block: reviewer_false_positive(_NO_CONCERNS, block),
    ("architect", "must_detect"): lambda block: architect_caught([], block),
    ("architect", "must_emit_allowed_paths"): lambda block: architect_allowed_paths_caught(
        {"components": []}, block
    ),
    ("architect_reuse", "must_reuse"): lambda block: names_existing_module({}, block),
}


def _blocks(meta: dict[str, Any]) -> list[str]:
    """The matcher blocks of a meta: every top-level key starting ``must_``."""
    return sorted(key for key in meta if key.startswith("must_"))


# ---------------------------------------------------------------------------
# The issue's own scenario, end to end
# ---------------------------------------------------------------------------


def _one_finding(category: str, severity: str, location: str) -> SecurityResult:
    """A security reply with one finding, through the real parser."""
    reply = json.dumps(
        {
            "findings": [
                {
                    "category": category,
                    "severity": severity,
                    "location": location,
                    "explanation": "user input reaches the query",
                }
            ],
            "exhaustively_searched": True,
        }
    )
    return parse_security_output(reply, SecurityMode.ADVISORY.value)


@pytest.mark.parametrize("typo", ["hgih", "High", "HIGH", ""])
def test_a_mistyped_severity_floor_is_refused_not_caught(typo: str) -> None:
    """sec-01 with its floor mistyped, and a LOW injection in the right
    file: the saved floor scores it as missed, the mistyped one used to
    rank 0 and score it as caught."""
    meta = _meta(FIXTURES_DIR / "security" / "01_sql_injection.meta.json")
    low = _one_finding("injection", "low", "src/users.py:11")
    assert security_caught(low, meta["must_detect"]) == (False, "")

    meta["must_detect"]["severity_at_least"] = typo

    with pytest.raises(FixtureMetaError, match=r"must_detect\.severity_at_least: "):
        security_caught(low, meta["must_detect"])


def test_a_misspelt_forbidden_category_is_refused_not_clean() -> None:
    """sp-01 with ``categories`` misspelt, and a high injection finding:
    the saved block flags it, the misspelt one used to score it clean."""
    meta = _meta(FIXTURES_DIR / "security_negative" / "01_parameterized_dynamic_sql.meta.json")
    high = _one_finding("injection", "high", "src/products.py:20")
    assert security_false_positive(high, meta["must_not_flag"])[0] is True

    block = meta["must_not_flag"]
    block["category"] = block.pop("categories")

    with pytest.raises(FixtureMetaError, match=r"must_not_flag\.categories: missing"):
        security_false_positive(high, block)


@pytest.fixture
def fixtures_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty fixture library the real loader reads instead of the saved one."""
    monkeypatch.setattr(calibration_repo_fixture, "FIXTURES_DIR", tmp_path)
    return tmp_path


def _place(root: Path, saved_meta: Path, meta: dict[str, Any]) -> None:
    """Write ``meta`` and a copy of the saved meta's artifact under ``root``."""
    target = root / saved_meta.parent.name
    target.mkdir(parents=True, exist_ok=True)
    stem = saved_meta.name.removesuffix(".meta.json")
    for artifact in saved_meta.parent.glob(f"{stem}.*"):
        if not artifact.name.endswith(".meta.json"):
            shutil.copy(artifact, target / artifact.name)
    (target / saved_meta.name).write_text(json.dumps(meta), encoding="utf-8")


def test_the_loader_refuses_a_meta_naming_its_file_and_field(fixtures_root: Path) -> None:
    """The loader every paid calibration test is parametrized from refuses
    the fixture while the suite is collected, before any agent call."""
    saved = FIXTURES_DIR / "security" / "01_sql_injection.meta.json"
    meta = _meta(saved)
    meta["must_detect"]["severity_at_least"] = "hgih"
    _place(fixtures_root, saved, meta)

    with pytest.raises(FixtureMetaError) as refused:
        load_fixtures("security", ".diff")

    assert str(refused.value).startswith("security/01_sql_injection.meta.json: ")
    assert "must_detect.severity_at_least: 'hgih' is not one of" in str(refused.value)


def test_the_loader_reads_every_saved_meta(fixtures_root: Path) -> None:
    """Control: every saved meta, copied as it is, loads."""
    for saved in SAVED:
        _place(fixtures_root, saved, _meta(saved))
    loaded = [
        meta
        for subdir in sorted({p.parent.name for p in SAVED})
        for suffix in (".diff", ".md")
        for _artifact, meta in load_fixtures(subdir, suffix)
    ]
    assert len(loaded) == len(SAVED) > 0


class _CountingRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str, fixture: RoleFixture) -> str:
        self.calls += 1
        return json.dumps({"findings": [], "exhaustively_searched": True})


def _role_fixtures(*subdirs: str) -> list[RoleFixture]:
    return [
        RoleFixture(meta["fixture_id"], meta, artifact.read_text(encoding="utf-8"))
        for subdir in subdirs
        for artifact, meta in load_fixtures(subdir, ".diff")
    ]


@pytest.mark.parametrize(
    "fixture_id,block,field,value",
    [
        ("sec-01-sql-injection", "must_detect", "severity_at_least", "High"),
        ("sec-neg-01-parameterized-dynamic-sql", "must_not_flag", "categories", ["injecton"]),
    ],
    ids=["positive", "negative"],
)
def test_the_optimizer_refuses_before_any_role_call(
    tmp_path: Path, fixture_id: str, block: str, field: str, value: Any
) -> None:
    """A security optimization run over the saved fixtures, one of them
    unreadable, stops before the first role call and before it creates its
    run directory. A negative is broken as well as a positive: every
    negative sits in both splits, so a check that skipped negatives would
    pay for a role call before the matcher refused."""
    fixtures = _role_fixtures("security", "security_negative")
    broken = next(f for f in fixtures if f.fixture_id == fixture_id)
    meta = copy.deepcopy(broken.meta)
    meta[block][field] = value
    fixtures[fixtures.index(broken)] = RoleFixture(broken.fixture_id, meta, broken.diff)
    runner = _CountingRunner()

    with pytest.raises(FixtureMetaError) as refused:
        run_optimization(
            "security",
            SECURITY_PROMPT,
            fixtures,
            runner=runner,
            reflection_lm=lambda prompt: "",
            max_metric_calls=100,
            run_dir=tmp_path / "run",
        )

    assert str(refused.value).startswith(f"{fixture_id}: ")
    assert f"{block}.{field}" in str(refused.value)
    assert runner.calls == 0
    assert not (tmp_path / "run").exists()


def test_the_optimizer_splits_the_saved_fixtures() -> None:
    """Control: the saved fixtures pass the same check."""
    for role, subdirs in (
        ("security", ("security", "security_negative")),
        ("reviewer", ("concerns", "concerns_negative")),
    ):
        train, validation = gepa_adapter.split_fixtures(role, _role_fixtures(*subdirs))
        assert train and validation


def test_a_quoted_arm_flag_is_refused() -> None:
    """``bool("false")`` is True, so a quoted flag graded the repo_absent
    arm as if the module were there to be read."""
    saved = FIXTURES_DIR / "specs" / "05_reuse_existing.meta.json"
    meta = _meta(saved)
    meta["arms"][1]["expect_module_named"] = "false"
    fixture = RepoSpecFixture(spec_path=saved.with_name("05_reuse_existing.md"), meta=meta)

    with pytest.raises(FixtureMetaError, match=r"arms\[1\]\.expect_module_named: must be true"):
        _ = fixture.arms


# ---------------------------------------------------------------------------
# Vocabulary: each value checked against the constant its matcher reads
# ---------------------------------------------------------------------------

VOCABULARY_CASES = [
    ("security/01_sql_injection", "must_detect", "severity_at_least", "hgih"),
    ("security/01_sql_injection", "must_detect", "category", "Injection"),
    ("security/06_multihop_authz", "must_detect", "category_any_of", ["authz_bypas"]),
    ("security/06_multihop_authz", "must_detect", "category_any_of", []),
    (
        "security_negative/01_parameterized_dynamic_sql",
        "must_not_flag",
        "severity_at_least",
        "Medium",
    ),
    ("security_negative/01_parameterized_dynamic_sql", "must_not_flag", "categories", ["injecton"]),
    ("security_negative/01_parameterized_dynamic_sql", "must_not_flag", "categories", []),
    ("concerns/02_tautological_test", "must_detect", "severity_at_least", "Fail"),
    ("concerns/02_tautological_test", "must_detect", "severity_at_least", "high"),
    ("concerns/02_tautological_test", "must_detect", "category", "test_qualty"),
    ("concerns_negative/02_thorough_tests", "must_not_flag", "severity_at_least", "blocking"),
    ("concerns_negative/02_thorough_tests", "must_not_flag", "categories", ["tests"]),
    ("specs/01_no_error_handling", "must_detect", "must_include_kind", ["missng_detail"]),
    ("specs/01_no_error_handling", "must_detect", "spec_issues_min", 0),
    ("specs/01_no_error_handling", "must_detect", "spec_issues_min", True),
    ("specs/04_clear_layout", "must_emit_allowed_paths", "includes_test_root_prefix", ""),
    ("specs/04_clear_layout", "must_emit_allowed_paths", "excludes_harness_internals", [""]),
    ("specs/05_reuse_existing", "must_reuse", "module_markers", []),
]


@pytest.mark.parametrize(
    "stem,block,field,value",
    VOCABULARY_CASES,
    ids=[f"{c[0]}:{c[2]}={c[3]!r}" for c in VOCABULARY_CASES],
)
def test_a_value_outside_its_vocabulary_is_refused(
    stem: str, block: str, field: str, value: Any
) -> None:
    meta = _meta(FIXTURES_DIR / f"{stem}.meta.json")
    meta[block][field] = value

    errors = fixture_meta_errors(meta)
    with pytest.raises(FixtureMetaError) as refused:
        MATCHERS[(meta["role"], block)](meta[block])

    assert any(f"{block}.{field}" in error for error in errors), errors
    assert f"{block}.{field}" in str(refused.value)


def test_both_category_fields_are_refused() -> None:
    meta = _meta(FIXTURES_DIR / "security" / "01_sql_injection.meta.json")
    meta["must_detect"]["category_any_of"] = ["xss"]

    with pytest.raises(FixtureMetaError, match="must carry exactly one of the two"):
        security_caught(_NO_FINDINGS, meta["must_detect"])


@pytest.mark.parametrize(
    "change,fragment",
    [
        (lambda meta: meta.update(role="Security"), "meta.role: 'Security' is not one of"),
        (lambda meta: meta.pop("role"), "meta.role: must be a string"),
        (
            lambda meta: meta.update(must_not_flag=meta["must_detect"]),
            "carries exactly one of ['must_detect', 'must_not_flag']",
        ),
        (lambda meta: meta.pop("must_detect"), "carries exactly one of"),
    ],
    ids=["unknown role", "no role", "two blocks", "no block"],
)
def test_a_meta_that_names_no_single_block_is_refused(
    change: Callable[[dict[str, Any]], object], fragment: str
) -> None:
    meta = _meta(FIXTURES_DIR / "security" / "01_sql_injection.meta.json")
    change(meta)
    assert any(fragment in error for error in fixture_meta_errors(meta))


# ---------------------------------------------------------------------------
# Behaviour layer: every field of every saved block, four ways
# ---------------------------------------------------------------------------


def _wrong_type(value: Any) -> Any:
    """The value in a JSON type its field does not take, chosen by type
    alone so no field list is needed. A bool or a number becomes its text
    (which Python's truthiness would have read), a list gains a null, and
    a string becomes a one-item list."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return [*value, None]
    return [value]


MUTATIONS: dict[str, Callable[[dict[str, Any], str], str]] = {}


def _mutation(name: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    def register(fn: Callable[[dict[str, Any], str], str]) -> Callable[[dict[str, Any], str], str]:
        MUTATIONS[name] = fn
        return fn

    return register


@_mutation("deleted")
def _delete(block: dict[str, Any], field: str) -> str:
    del block[field]
    return field


@_mutation("null")
def _null(block: dict[str, Any], field: str) -> str:
    block[field] = None
    return field


@_mutation("unread sibling")
def _sibling(block: dict[str, Any], field: str) -> str:
    block[f"{field}_unread"] = block[field]
    return f"{field}_unread"


@_mutation("wrong type")
def _retype(block: dict[str, Any], field: str) -> str:
    block[field] = _wrong_type(block[field])
    return field


CASES = [
    (saved, block, field, mutation)
    for saved in SAVED
    for block in _blocks(_meta(saved))
    for field in sorted(_meta(saved)[block])
    for mutation in MUTATIONS
]


def test_the_mutation_cases_cover_every_saved_block() -> None:
    """Anti-vacuity: the cases below are built from the saved data, so an
    empty glob would make every one of them pass by not existing."""
    assert len(SAVED) >= 26
    assert all(_blocks(_meta(saved)) for saved in SAVED)
    assert len({(c[0], c[1]) for c in CASES}) == len(SAVED)


def test_every_saved_block_has_its_matcher() -> None:
    pairs = {(_meta(saved)["role"], block) for saved in SAVED for block in _blocks(_meta(saved))}
    assert pairs == set(MATCHERS)


@pytest.mark.parametrize("saved", SAVED, ids=_rel)
def test_every_saved_block_is_read_and_scored(saved: Path) -> None:
    """Control for the mutations: the saved meta as it is reads cleanly
    and its matcher scores an empty reply without refusing it."""
    meta = _meta(saved)
    assert fixture_meta_errors(meta) == []
    for block in _blocks(meta):
        caught, _detail = MATCHERS[(meta["role"], block)](meta[block])
        assert isinstance(caught, bool)


@pytest.mark.parametrize(
    "saved,block,field,mutation",
    CASES,
    ids=[f"{_rel(c[0])}:{c[1]}.{c[2]}:{c[3]}" for c in CASES],
)
def test_every_field_a_matcher_reads_is_refused_when_unreadable(
    saved: Path, block: str, field: str, mutation: str
) -> None:
    meta = _meta(saved)
    named = MUTATIONS[mutation](meta[block], field)

    errors = fixture_meta_errors(meta)
    with pytest.raises(FixtureMetaError) as refused:
        MATCHERS[(meta["role"], block)](meta[block])

    assert any(f"{block}.{named}" in error for error in errors), errors
    assert f"{block}.{named}" in str(refused.value)


# ---------------------------------------------------------------------------
# Static layer: every read with a fallback where a matcher could hide one
# ---------------------------------------------------------------------------

SCORER = KSTRL_PACKAGE / "calibration_score.py"

#: Modules that hold a matcher outside kstrl/. Only their functions
#: annotated with the verdict type are walked: the rest of
#: tests/test_calibration.py reads environment variables, report records
#: and prompt context with defaults, none of which grades a reply.
TEST_SIDE = (
    REPO_ROOT / "tests" / "test_calibration.py",
    REPO_ROOT / "tests" / "helpers" / "calibration_repo_fixture.py",
    REPO_ROOT / "tests" / "helpers" / "calibration_integration_fixture.py",
)
VERDICT = "tuple[bool, str]"

_FALLBACK_METHODS = frozenset({"get", "setdefault", "pop"})
_MISSING_KEY = frozenset({"KeyError", "LookupError", "IndexError", "Exception", "BaseException"})


def _handler_names(node: ast.expr | None) -> set[str]:
    if node is None:
        return {"BaseException"}
    if isinstance(node, ast.Tuple):
        return {name for elt in node.elts for name in _handler_names(elt)}
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _falls_back(node: ast.AST) -> bool:
    """Is this node a read that can answer when a key is missing?"""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _FALLBACK_METHODS:
            return True
        return isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) == 3
    if isinstance(node, ast.ExceptHandler):
        return bool(_handler_names(node.type) & _MISSING_KEY)
    if isinstance(node, ast.Compare):
        keyed = isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
        membership = (ast.In, ast.NotIn)  # codespell:ignore
        return keyed and any(isinstance(op, membership) for op in node.ops)
    return False


def _verdict_nodes(tree: ast.Module) -> set[int]:
    """Ids of every node inside a function annotated ``-> tuple[bool, str]``."""
    return {
        id(child)
        for node, _name in scopes(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.returns is not None
        and ast.unparse(node.returns) == VERDICT
        for child in own_nodes(node)
    }


def _unwalked() -> frozenset[int]:
    """Ids of the test-side nodes the census does not walk."""
    skipped: set[int] = set()
    for path in TEST_SIDE:
        tree = parsed(path)
        skipped |= {id(node) for node in all_nodes(tree)} - _verdict_nodes(tree)
    return frozenset(skipped)


_SCOPES: dict[Path, dict[int, str]] = {}


def _row(path: Path, node: ast.AST) -> str:
    if path not in _SCOPES:
        _SCOPES[path] = scope_of(parsed(path))
    return f"{label(path)}::{_SCOPES[path].get(id(node), '<lambda>')}"


#: Every read with a fallback the census walks, by scope. Re-derive by
#: running the census, never by editing this literal. None of them reads a
#: matcher field with a default:
#: - ``_detect_errors`` and ``_acceptable_categories`` test which of the two
#:   category fields a block carries, after the reader refused a block
#:   carrying both or neither; ``fixture_meta_errors`` reads ``role`` to
#:   refuse it by name.
#: - ``_meets_severity`` ranks the ROLE'S finding, which the parser has
#:   already filtered; ``architect_allowed_paths_caught`` reads the
#:   ARCHITECT'S components.
#: - ``render_verification`` reads prompt context, not a matcher field.
#: - each paid architect ``run_once`` turns an agent crash into
#:   ``_AgentUnavailable`` with ``except Exception``.
EXPECTED_FALLBACKS = {
    "calibration_score.py::_acceptable_categories": 1,
    "calibration_score.py::_detect_errors": 3,
    "calibration_score.py::_meets_severity": 1,
    "calibration_score.py::fixture_meta_errors": 1,
    "calibration_score.py::render_verification": 4,
    "tests/test_calibration.py::architect_allowed_paths_caught": 11,
    "tests/test_calibration.py::test_architect_emits_sensible_allowed_paths.run_once": 1,
    "tests/test_calibration.py::test_architect_reuses_what_the_repository_already_has.run_once": 1,
    "tests/test_calibration.py::test_architect_role_flags_vague_spec.run_once": 1,
}

#: One source per disjunct of ``_falls_back``; each must be counted.
CONTROLS = (
    'r.get("k", 1)',
    'r.setdefault("k", 1)',
    'r.pop("k", 1)',
    'getattr(r, "k", 1)',
    'try:\n    v = r["k"]\nexcept KeyError:\n    v = 1\n',
    'v = r["k"] if "k" in r else 1\n',
)


def test_every_fallback_read_in_a_matcher_is_accounted_for() -> None:
    unwalked = _unwalked()
    assert_census(
        sources=[SCORER, *TEST_SIDE],
        sees=lambda node: id(node) not in unwalked and _falls_back(node),
        expected=EXPECTED_FALLBACKS,
        control=CONTROLS,
        key=_row,
        message=(
            "A read with a fallback appeared in, or left, the calibration scorer or a "
            "matcher. A field a matcher reads from a fixture meta is required and read "
            "through a reader in kstrl/calibration_score.py that refuses it; it is "
            "never defaulted (#564)."
        ),
    )


def _walked(source: str, *, whole: bool) -> int:
    """How many fallback reads the census counts in ``source``, walked as
    the scorer module (``whole``) or as a test-side module."""
    tree = parse(source)
    walked = {id(node) for node in all_nodes(tree)} if whole else _verdict_nodes(tree)
    return sum(1 for node in all_nodes(tree) if id(node) in walked and _falls_back(node))


@pytest.mark.parametrize("source", CONTROLS)
def test_the_census_sees_each_shape_inside_a_test_side_matcher(source: str) -> None:
    body = "\n".join(f"    {line}" for line in source.strip().splitlines())
    matcher = f"def m(r) -> tuple[bool, str]:\n{body}\n    return True, ''\n"
    assert _walked(matcher, whole=False) == 1


def test_the_census_does_not_walk_a_test_side_function_that_is_not_a_matcher() -> None:
    """The narrowing is deliberate, and this pins that it is the only one."""
    source = 'def env(r):\n    return r.get("k", 1)\n'
    assert _walked(source, whole=False) == 0
    assert _walked(source, whole=True) == 1


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="blind spot: defaults merged into a dict display are a subscript, not a fallback call",
)
def test_blind_spot_defaults_merged_under_the_block() -> None:
    blind_spot(lambda src: _walked(src, whole=True), 'v = {**{"k": 1}, **r}["k"]\n')


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="blind spot: a ChainMap over a defaults dict is a subscript, not a fallback call",
)
def test_blind_spot_a_chainmap_of_defaults() -> None:
    blind_spot(lambda src: _walked(src, whole=True), 'v = ChainMap(r, {"k": 1})["k"]\n')


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="blind spot: contextlib.suppress(KeyError) is a with-statement, not an except clause",
)
def test_blind_spot_a_suppressed_missing_key() -> None:
    blind_spot(
        lambda src: _walked(src, whole=True),
        'v = 1\nwith suppress(KeyError):\n    v = r["k"]\n',
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="blind spot: a test-side matcher without the verdict annotation is not walked",
)
def test_blind_spot_an_unannotated_test_side_matcher() -> None:
    blind_spot(
        lambda src: _walked(src, whole=False),
        'def m(r):\n    return r.get("k", 1) > 0, ""\n',
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="blind spot: a helper a test-side matcher delegates to is not walked",
)
def test_blind_spot_a_test_side_helper_a_matcher_calls() -> None:
    blind_spot(
        lambda src: _walked(src, whole=False),
        'def h(r):\n    return r.get("k", 1)\n\n'
        'def m(r) -> tuple[bool, str]:\n    return h(r) > 0, ""\n',
    )
