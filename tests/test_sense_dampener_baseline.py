"""R10.6 (#227): the baseline DOCUMENT - what is written, read and refused.

Split out of ``tests/test_sense_dampener.py`` in review round 1, when that file
crossed the 800-line ratchet. The division is the one the module already makes:
that file is the comparison arithmetic and how it renders, this one is the
artifact on disk, the identity it carries, and every way reading it fails.

Nothing here is read leniently, and that is the subject rather than a detail. A
document this cannot understand has to be a refusal: read as ``{}``, every
current signature is new (or every baseline one vanishes), and the mechanism is
gone with nothing failing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kstrl import dampener, dampener_report
from tests.test_sense_dampener import DIGEST, PROJECT, _baseline

# --- the document on disk -----------------------------------------------


def test_write_baseline_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "sense-baseline.json"
    baseline = _baseline({"linter:E501": 2})

    dampener.write_baseline(path, baseline, force=False)

    assert dampener.read_baseline(path) == baseline
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == dampener.BASELINE_SCHEMA_VERSION == 1
    assert document["base_ref"] == "0123456789abcdef"
    assert document["sense_schema_version"] == 2


def test_baseline_keys_are_sorted_in_the_file_bytes(tmp_path: Path) -> None:
    """Assert on the FILE TEXT, not on a re-parse: ``json.loads`` into a dict
    would hide an unsorted write, and unsorted is a whole-file git diff on
    every regeneration."""
    path = tmp_path / "b.json"
    reversed_order = {"typecheck:arg-type": 1, "linter:F401": 1, "linter:E501": 1}

    dampener.write_baseline(
        path,
        _baseline(reversed_order, measured=("typecheck", "linter"), unmeasured=("z", "a")),
        force=False,
    )

    text = path.read_text(encoding="utf-8")
    assert text.index("linter:E501") < text.index("linter:F401") < text.index("typecheck:arg-type")
    assert text.index('"a"') < text.index('"z"')
    # Top-level key ORDER is part of the format: reordering it would produce a
    # whole-file diff on a run that changed nothing.
    assert list(json.loads(text)) == [
        "schema_version",
        "generated_at",
        "base_ref",
        "project",
        "passed",
        "sense_schema_version",
        "verify_digest",
        "measured_checks",
        "unmeasured_checks",
        "unmeasured_reasons",
        "signatures",
    ]


def test_write_refuses_an_existing_file_without_force(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    dampener.write_baseline(path, _baseline({"linter:E501": 1}), force=False)

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.write_baseline(path, _baseline({}), force=False)
    assert str(path) in str(excinfo.value)
    assert "--force" in str(excinfo.value)

    dampener.write_baseline(path, _baseline({}), force=True)
    assert dampener.read_baseline(path).signatures == {}


def test_missing_baseline_names_the_remedy(tmp_path: Path) -> None:
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(tmp_path / "absent.json")

    assert str(excinfo.value) == (
        f"no baseline at {tmp_path / 'absent.json'}; run ks sense --write-baseline first"
    )


def _valid_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-09-06T00:00:00Z",
        "base_ref": "abc",
        "project": PROJECT,
        "passed": False,
        "sense_schema_version": 2,
        "verify_digest": DIGEST,
        "measured_checks": ["linter"],
        "unmeasured_checks": [],
        "unmeasured_reasons": {},
        "signatures": {"linter:E501": 1},
    }


@pytest.mark.parametrize(
    ("mutate", "expected_in_message"),
    [
        (lambda d: [1, 2, 3], "JSON object"),
        (lambda d: d.pop("schema_version") and d, "schema_version"),
        (lambda d: {**d, "schema_version": 2}, "expected 1"),
        (lambda d: {**d, "schema_version": "1"}, "schema_version"),
        (lambda d: {**d, "passed": "false"}, "passed"),
        (lambda d: {**d, "base_ref": 7}, "base_ref"),
        (lambda d: {**d, "sense_schema_version": True}, "sense_schema_version"),
        (lambda d: {**d, "measured_checks": "linter"}, "measured_checks"),
        (lambda d: {**d, "measured_checks": ["linter", 3]}, "measured_checks'[1]"),
        (lambda d: {**d, "measured_checks": [""]}, "measured_checks'[0]"),
        (lambda d: {**d, "signatures": ["linter:E501"]}, "signatures"),
        (lambda d: {**d, "signatures": {"linter:E501": "1"}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"linter:E501": -1}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"linter:E501": True}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"": 1}}, "non-empty string"),
    ],
)
def test_a_malformed_baseline_is_refused_and_names_what_is_wrong(
    mutate: Any,
    expected_in_message: str,
) -> None:
    """Never read leniently. A document read as ``{}`` makes every current
    signature new (or every baseline one vanish) with nothing failing, which
    is the mechanism silently gone."""
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document(mutate(_valid_document()))

    assert expected_in_message in str(excinfo.value)


def test_a_baseline_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(path)
    assert "not JSON" in str(excinfo.value)


def test_a_baseline_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    """``UnicodeDecodeError`` is a ``ValueError`` and escapes a bare
    ``except OSError``, so it is caught explicitly beside it."""
    path = tmp_path / "b.json"
    path.write_bytes(b'{"schema_version": 1, "x": "\xff\xfe"}')

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(path)
    assert "cannot read the baseline" in str(excinfo.value)


def test_a_wrong_schema_version_names_the_remedy() -> None:
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document({**_valid_document(), "schema_version": 99})

    assert "--write-baseline --force" in str(excinfo.value)


@pytest.mark.parametrize("key", ["measured_checks", "unmeasured_checks", "signatures"])
def test_a_missing_collection_is_refused_not_read_as_empty(key: str) -> None:
    """The lenient half of fail-closed, stated separately because it is the
    one that reads as legal. A missing ``measured_checks`` read as ``[]``
    would put every baseline signature in ``unmeasured`` forever, which looks
    exactly like a repository whose sensors are all off."""
    document = _valid_document()
    del document[key]

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document(document)
    assert key in str(excinfo.value)


# --- the baseline's identity --------------------------------------------


def _commands(lint: str = "ruff check .") -> Any:
    from kstrl.verify import ResolvedVerifyCommands

    return ResolvedVerifyCommands(test="pytest", typecheck="mypy .", lint=lint)


def test_the_digest_moves_with_the_commands_and_with_the_timeout() -> None:
    same = dampener.verify_digest(_commands(), 1800.0)

    assert dampener.verify_digest(_commands(), 1800.0) == same
    assert dampener.verify_digest(_commands("ruff check --fix ."), 1800.0) != same
    assert dampener.verify_digest(_commands(), 300.0) != same


def test_a_baseline_measured_differently_is_refused_naming_both_digests() -> None:
    """``bind_register``'s rule, applied to the baseline.

    ``docs/dampener.md`` already said a baseline and a comparison measured at
    different timeouts are not a comparison; before this the only mechanism
    behind that sentence was a literal 1800 typed into a workflow file.
    """
    baseline = _baseline({}, digest="aaaaaaaaaaaaaaaa")

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.refuse_foreign_baseline(baseline, "bbbbbbbbbbbbbbbb")

    message = str(excinfo.value)
    assert "aaaaaaaaaaaaaaaa" in message
    assert "bbbbbbbbbbbbbbbb" in message
    assert "--write-baseline --force" in message


def test_a_baseline_measured_the_same_way_is_accepted() -> None:
    """The control. Without it the refusal above passes with the comparison
    inverted, which would refuse every legitimate run."""
    dampener.refuse_foreign_baseline(_baseline({}, digest=DIGEST), DIGEST)


def test_a_different_project_is_a_note_and_not_a_refusal() -> None:
    """A baseline copied between two checkouts of the same project is
    legitimate; between two different projects it is #260's mistake. Only a
    person can tell those apart, so this reports rather than refuses."""
    comparison = dampener.compare(
        _baseline({}, project="writers-room"),
        _baseline({}, project="kstrl"),
    )

    assert comparison.project_changed == ("writers-room", "kstrl")
    assert comparison.regressed is False
    human = dampener_report.render_human(comparison, _baseline({}), Path("b.json"))
    assert any("'writers-room'" in line and "'kstrl'" in line for line in human)


def test_a_hole_in_a_baseline_carries_the_reason_it_is_there() -> None:
    document = _valid_document()
    document["unmeasured_checks"] = ["dead_code"]

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document(document)

    assert "unmeasured_reasons" in str(excinfo.value)
    assert "dead_code" in str(excinfo.value)


def test_a_signature_count_of_zero_is_refused() -> None:
    """``to_document`` writes a Counter of occurrences, so zero is a shape it
    cannot produce. Accepting one put "was 0" in a report's fixed table."""
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document({**_valid_document(), "signatures": {"linter:E501": 0}})

    assert "positive integer" in str(excinfo.value)


# --- reading a baseline the parser cannot parse -------------------------


def test_a_deeply_nested_baseline_is_refused_and_not_a_traceback(tmp_path: Path) -> None:
    """``RecursionError`` is a ``RuntimeError``, not a ``ValueError``.

    Round 1 of review on #357 measured 200000 nested arrays escaping
    ``except ValueError`` around ``json.loads``, so a document this function
    promises to refuse with exit 2 killed the command with a traceback and
    exit 1. The parser's error taxonomy belongs to the parser (#318).
    """
    path = tmp_path / "deep.json"
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(path)

    assert "RecursionError" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_a_directory_where_a_baseline_should_be_is_an_os_error(tmp_path: Path) -> None:
    """The other half of rule 3: the I/O is outside the guard, so widening the
    parse guard to ``Exception`` cannot swallow a disk failure and report it
    as malformed JSON."""
    directory = tmp_path / "b.json"
    directory.mkdir()

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(directory)

    assert "cannot read the baseline" in str(excinfo.value)
    assert "is not JSON" not in str(excinfo.value)


# --- where a baseline path resolves --------------------------------------


def test_a_relative_explicit_path_resolves_under_root() -> None:
    """One rule for one flag. Passing the exact path ``--help`` advertises as
    the default, together with ``--root``, used to read a different file and
    report "no baseline at ..." for a file that exists."""
    root = Path("/elsewhere")

    assert dampener._baseline_path("scripts/kstrl/sense-baseline.json", root) == (
        root / "scripts/kstrl/sense-baseline.json"
    )
    assert dampener._baseline_path(dampener.OPTIONAL_VALUE_SENTINEL, root) == (
        root / dampener.DEFAULT_BASELINE_PATH
    )
    assert dampener._baseline_path("/tmp/b.json", root) == Path("/tmp/b.json")


# --- which project a baseline is OF -------------------------------------


def test_the_project_identity_survives_a_worktree(tmp_path: Path) -> None:
    """``owner/repo`` from ``origin``, in both URL shapes and through a
    worktree, because the directory name is exactly what a worktree changes.

    Measured rather than argued: every kstrl lane runs inside a git worktree
    named after an issue number, so a baseline written in one records ``227``
    as its directory name and every later comparison in an ordinary checkout
    reports a mismatch that means nothing.
    """
    from kstrl.git import get_origin_slug
    from tests.spine_utils import git as run_git

    repo = tmp_path / "some-issue-number"
    repo.mkdir()
    run_git("init", "-q", "-b", "main", cwd=repo)
    run_git("remote", "add", "origin", "https://github.com/0xfauzi/kstrl.git", cwd=repo)

    assert get_origin_slug(repo) == "0xfauzi/kstrl"

    run_git("remote", "set-url", "origin", "git@github.com:0xfauzi/kstrl.git", cwd=repo)
    assert get_origin_slug(repo) == "0xfauzi/kstrl"


def test_a_repository_with_no_remote_has_no_slug(tmp_path: Path) -> None:
    """The fallback's precondition. Without this the CLI's
    ``get_origin_slug(path) or path.name`` looks like belt over braces."""
    from kstrl.git import get_origin_slug
    from tests.spine_utils import git as run_git

    repo = tmp_path / "local-only"
    repo.mkdir()
    run_git("init", "-q", "-b", "main", cwd=repo)

    # A directory that is not a repository at all: git exits nonzero and this
    # returns None too. So does a path that does not EXIST, since `subprocess`
    # raises FileNotFoundError for a missing cwd and this function now fails
    # closed on OSError; `ks sense` refuses a path that is not a directory
    # before any of it is reached, so that case is unreachable from the CLI.
    plain = tmp_path / "plain-directory"
    plain.mkdir()

    assert get_origin_slug(repo) is None
    assert get_origin_slug(plain) is None
    assert get_origin_slug(tmp_path / "not-there") is None


def test_the_dampener_s_identity_reads_fall_back_when_git_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine with no git gets the documented fallback, not a traceback.

    Both functions are called on ONE line of `_sense_dampener_report`, after
    the whole sensor run: `project=get_origin_slug(path) or path.name` and
    `base_ref=get_head_sha(path)`. Round 2 of review on #357 emptied PATH and
    measured both raising FileNotFoundError, so an operator paid for the
    measurement and got exit 1 with a stack trace where the command documents
    a fallback and exit 2.

    PATH is emptied rather than `shutil.which` patched: what is under test is
    what these functions do when the binary is not there, and the environment
    is how a machine says so.

    The third row is the reason this matters before the report is reached at
    all: the strict diff read is what `ks sense` turns into exit 2, and it let
    the same error out.
    """
    from kstrl.git import GitDiffError, get_diff_names, get_head_sha, get_origin_slug

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert get_origin_slug(tmp_path) is None
    assert get_head_sha(tmp_path) is None
    with pytest.raises(GitDiffError, match="could not run"):
        get_diff_names("main", tmp_path, strict=True)


def test_the_identity_reads_still_answer_when_git_is_present(tmp_path: Path) -> None:
    """The control for the test above.

    Without it, `except (TimeoutExpired, OSError): return None` widened to
    swallow everything would pass it, and a fallback that fires on every
    machine records a project identity of `path.name` for every baseline - the
    worktree-number defect the identity exists to prevent.
    """
    from kstrl.git import get_head_sha, get_origin_slug
    from tests.spine_utils import git as run_git

    repo = tmp_path / "present"
    repo.mkdir()
    run_git("init", "-q", "-b", "main", cwd=repo)
    run_git("remote", "add", "origin", "https://github.com/0xfauzi/kstrl.git", cwd=repo)
    run_git("commit", "-q", "--allow-empty", "-m", "base", cwd=repo)

    assert get_origin_slug(repo) == "0xfauzi/kstrl"
    assert get_head_sha(repo) is not None
