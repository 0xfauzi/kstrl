"""#633 slice 2: a calibration fixture records under a role id derived from its own files.

Every paid arm recorded under a literal role id, and the loader globbed a
directory with no notion of language, so a TypeScript twin of sec-01 placed
in ``security/`` was averaged into the Python ``security`` rate. The id now
comes from the suffixes of the files a fixture changes or ships: ``security``
for Python, ``security_ts`` for TypeScript, ``security_rust`` for Rust. A
fixture whose language cannot be derived, and a detection id
``MIN_ROLE_DETECTION_RATE`` does not list, are refused while the suite is
collected, before any agent call.

End to end and unpaid: every paid test in ``tests/test_calibration.py`` is
called with ``kstrl.agents.get_agent`` replaced by a stub, over a fixture
tree in ``tmp_path`` read through the real loaders, with ``PATH`` holding
only ``git`` so no agent CLI can start (the ``tests/test_calibration_replies.py``
pattern). The assertion is on the baseline file the real report writes.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import kstrl.agents
from kstrl import calibration
from tests import test_calibration as tc
from tests.helpers import calibration_integration_fixture as cif
from tests.helpers import calibration_repo_fixture as crf

SAVED_FIXTURES = crf.FIXTURES_DIR
RESULTS = SAVED_FIXTURES / "_results"

#: The Python fixtures the mixed tree copies, each beside a TypeScript twin.
DIFF_FIXTURES = {
    "security": ("01_sql_injection", "06_multihop_authz"),
    "security_negative": ("01_parameterized_dynamic_sql",),
    "concerns": ("01_dead_code",),
    "concerns_negative": ("01_used_helper_refactor",),
}
INTEGRATION_FIXTURES = ("01_d1_stored_rows", "02_d1_stored_rows_clean")

#: The detection ids a TypeScript twin records under, listed as recorded
#: and not gated, as a first capture of them would be.
TS_DETECTION_IDS = ("security_ts", "security_hard_ts", "reviewer_ts")
TS_INTEGRATION_IDS = ("integration_ts", "integration_clean_ts")

#: The architect roles a saved baseline carries. They are scored on spec
#: fixtures, which carry no code file, so no role id is derived for them.
SPEC_ROLES = {"architect", "architect_allowed_paths", "architect_reuse"}


class _StubAgent:
    """Answers every call with text no matcher accepts, and counts the calls."""

    def __init__(self, made: list[str]) -> None:
        self.name = "stub"
        self.final_message: str | None = None
        self._made = made

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.final_message = f"stub reply {len(self._made) + 1}"
        self._made.append(self.final_message)
        yield self.final_message


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """A stub agent under the real getters, a PATH with only git, the report
    written under ``tmp_path`` and the loaders reading ``tmp_path/fixtures``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    os.symlink(git, bin_dir / "git")
    monkeypatch.setenv("PATH", str(bin_dir))
    made: list[str] = []
    monkeypatch.setattr(kstrl.agents, "get_agent", lambda **_kwargs: _StubAgent(made))
    monkeypatch.setattr(tc, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(tc, "CALIBRATION_RUNS", 2)
    monkeypatch.setattr(crf, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(cif, "INTEGRATION_DIR", tmp_path / "fixtures" / "integration")
    return made


def _to_ts(text: str) -> str:
    return text.replace(".py", ".ts")


def _ts_diff(diff: str) -> str:
    """The Python diff with every path it names moved to ``.ts``; the body is kept."""
    header = ("diff --git ", "--- a/", "+++ b/")
    return "".join(
        _to_ts(line) if line.startswith(header) else line for line in diff.splitlines(True)
    )


def _ts_id(fixture_id: str) -> str:
    """``sec-01-sql-injection`` -> ``sec-ts-01-sql-injection``: the infix is the oracle."""
    head, _, tail = fixture_id.partition("-")
    return f"{head}-ts-{tail}"


def _place_diff_fixtures(root: Path) -> None:
    for subdir, stems in DIFF_FIXTURES.items():
        target = root / subdir
        target.mkdir(parents=True)
        for stem in stems:
            diff = (SAVED_FIXTURES / subdir / f"{stem}.diff").read_text(encoding="utf-8")
            meta = json.loads(
                (SAVED_FIXTURES / subdir / f"{stem}.meta.json").read_text(encoding="utf-8")
            )
            (target / f"{stem}.diff").write_text(diff, encoding="utf-8")
            (target / f"{stem}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            twin = json.loads(_to_ts(json.dumps(meta)))
            twin["fixture_id"] = _ts_id(meta["fixture_id"])
            (target / f"{stem}_ts.diff").write_text(_ts_diff(diff), encoding="utf-8")
            (target / f"{stem}_ts.meta.json").write_text(json.dumps(twin), encoding="utf-8")


def _place_integration_fixtures(root: Path) -> None:
    target = root / "integration"
    target.mkdir(parents=True)
    for stem in INTEGRATION_FIXTURES:
        meta = json.loads(
            (SAVED_FIXTURES / "integration" / f"{stem}.meta.json").read_text(encoding="utf-8")
        )
        shutil.copytree(
            SAVED_FIXTURES / "integration" / meta["repo_dir"], target / meta["repo_dir"]
        )
        (target / f"{stem}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        twin = json.loads(_to_ts(json.dumps(meta)))
        twin["fixture_id"] = _ts_id(meta["fixture_id"])
        twin["repo_dir"] = f"{stem}_ts_repo"
        if meta.get("twin_of"):
            twin["twin_of"] = _ts_id(meta["twin_of"])
        for source in sorted((SAVED_FIXTURES / "integration" / meta["repo_dir"]).rglob("*")):
            if source.is_file():
                relative = source.relative_to(SAVED_FIXTURES / "integration" / meta["repo_dir"])
                dest = target / twin["repo_dir"] / _to_ts(relative.as_posix())
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, dest)
        (target / f"{stem}_ts.meta.json").write_text(json.dumps(twin), encoding="utf-8")


#: Every paid arm the languages reach, its argument lists, and whether a
#: Python fixture's miss fails its gate.
ARMS: dict[str, tuple[Callable[[], list[tuple[Any, ...]]], bool]] = {
    "test_security_role_catches_planted_bug": (tc._security_positive_easy_fixtures, True),
    "test_security_role_hard_positive": (tc._security_positive_hard_fixtures, False),
    "test_security_role_no_false_positive": (tc._security_negative_fixtures, False),
    "test_reviewer_role_catches_planted_concern": (tc._concern_fixtures, True),
    "test_reviewer_role_no_false_positive": (tc._concern_negative_fixtures, False),
    "test_integration_review_detects_planted_defect": (
        lambda: [(f,) for f in cif.integration_positives()],
        True,
    ),
    "test_integration_review_opens_nothing_on_a_clean_twin": (
        lambda: [(f,) for f in cif.integration_clean_twins()],
        True,
    ),
}


def _fixture_id(args: tuple[Any, ...]) -> str:
    head = args[0]
    return str(
        head.fixture_id if isinstance(head, cif.IntegrationFixture) else args[1]["fixture_id"]
    )


def test_a_typescript_twin_records_under_its_own_role_id(
    stubbed: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 observable 1. Every paid arm runs a Python fixture and its TypeScript
    twin. The saved baseline records each twin under ``<base>_ts`` and each
    Python fixture under the id it always had; the oracle is the ``-ts-``
    infix of the fixture id. A Python miss fails its gate as before, and a
    twin whose role is listed with None is recorded without failing."""
    for role in TS_DETECTION_IDS + TS_INTEGRATION_IDS:
        monkeypatch.setitem(calibration.MIN_ROLE_DETECTION_RATE, role, None)
    _place_diff_fixtures(tmp_path / "fixtures")
    _place_integration_fixtures(tmp_path / "fixtures")
    report = tc._DetectionReport()

    for name, (build_args, gated) in ARMS.items():
        cases = build_args()
        assert len(cases) == 2, (name, cases)
        for args in cases:
            twin = "-ts-" in _fixture_id(args)
            work = tmp_path / "work" / f"{name}-{_fixture_id(args)}"
            work.mkdir(parents=True)
            try:
                getattr(tc, name)(*args, work, report)
            except AssertionError as exc:
                assert gated and not twin and "missed planted issue" in str(exc), (name, exc)
            else:
                assert not (gated and not twin), f"{name} passed its gate on stub replies"

    saved = json.loads(report.save().read_text(encoding="utf-8"))
    recorded = {(f["role"], f["fixture_id"]) for f in saved["fixtures"]}
    for role, block in saved["false_positive_analysis"]["roles"].items():
        recorded |= {(role, f["fixture_id"]) for f in block["fixtures"]}
    assert len(recorded) == 14, recorded
    for role, fixture_id in recorded:
        assert role.endswith("_ts") == ("-ts-" in fixture_id), (role, fixture_id)
    assert sorted(saved["summary"]) == sorted(
        ["security", "security_hard", "reviewer", "integration", "integration_clean"]
        + list(TS_DETECTION_IDS + TS_INTEGRATION_IDS)
    )
    assert sorted(saved["false_positive_analysis"]["roles"]) == [
        "reviewer_negative",
        "reviewer_negative_ts",
        "security_negative",
        "security_negative_ts",
    ]


@pytest.mark.parametrize("saved", ["20260925-120951", "20260926-124722", "20260926-131430"])
def test_every_python_fixture_keeps_the_role_id_the_saved_baselines_carry(saved: str) -> None:
    """T1 observable 2. Over the real fixture tree, each role id a saved
    baseline carries holds the same fixtures the harness derives that id
    for today, so saved captures stay comparable with new ones."""
    document = json.loads((RESULTS / f"baseline-{saved}.json").read_text(encoding="utf-8"))
    carried: dict[str, set[str]] = {}
    for fixture in document["fixtures"]:
        carried.setdefault(fixture["role"], set()).add(fixture["fixture_id"])
    for role, block in document.get("false_positive_analysis", {}).get("roles", {}).items():
        carried.setdefault(role, set()).update(f["fixture_id"] for f in block["fixtures"])

    derived: dict[str, set[str]] = {}
    for base, fixtures in (
        ("security", tc._security_positive_easy_fixtures()),
        ("security_hard", tc._security_positive_hard_fixtures()),
        ("security_negative", tc._security_negative_fixtures()),
        ("reviewer", tc._concern_fixtures()),
        ("reviewer_negative", tc._concern_negative_fixtures()),
    ):
        for artifact, meta in fixtures:
            derived.setdefault(tc._diff_role(base, artifact), set()).add(meta["fixture_id"])
    for base, integration in (
        ("integration", cif.integration_positives()),
        ("integration_clean", cif.integration_clean_twins()),
    ):
        for fixture in integration:
            role = crf.language_role(base, cif.repo_language(fixture))
            derived.setdefault(role, set()).add(fixture.fixture_id)

    compared = sorted(set(carried) - SPEC_ROLES)
    assert compared, sorted(carried)
    for role in compared:
        assert derived.get(role) == carried[role], (role, sorted(derived))


# ---------------------------------------------------------------------------
# T2: refused while the suite is collected, before any agent call
# ---------------------------------------------------------------------------


def _sec01() -> tuple[str, dict[str, Any]]:
    diff = (SAVED_FIXTURES / "security" / "01_sql_injection.diff").read_text(encoding="utf-8")
    meta = json.loads(
        (SAVED_FIXTURES / "security" / "01_sql_injection.meta.json").read_text(encoding="utf-8")
    )
    return diff, meta


def _moved(diff: str, path: str) -> str:
    return diff.replace("src/users.py", path)


_SEC01_DIFF, _ = _sec01()

#: (diff file name, diff text, the path the refusal must name)
REFUSED_DIFFS = {
    "mixed": ("01_mixed.diff", _SEC01_DIFF + _moved(_SEC01_DIFF, "src/users.ts"), "src/users.ts"),
    "unknown_suffix": ("01_go.diff", _moved(_SEC01_DIFF, "src/users.go"), "src/users.go"),
    "no_code_file": ("01_manifest.diff", _moved(_SEC01_DIFF, "package.json"), "package.json"),
    "no_suffix": (
        "01_no_suffix.diff",
        _SEC01_DIFF + _moved(_SEC01_DIFF, "src/Makefile"),
        "src/Makefile",
    ),
    "two_hunks": (
        "01_two_hunks.diff",
        _SEC01_DIFF + "@@ -40,1 +40,2 @@\n x = 1\n+y = 2\n",
        "src/users.py",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSED_DIFFS))
def test_a_diff_no_role_id_can_be_derived_for_is_refused_at_load(
    case: str, stubbed: list[str], tmp_path: Path
) -> None:
    """T2. A mixed-language diff, a diff in a language no role knows, a diff
    naming no code file and a segment with two hunks are each refused by the
    loader every paid test is parametrized from, naming the fixture file and
    the path. No agent is called."""
    name, text, named = REFUSED_DIFFS[case]
    _diff, meta = _sec01()
    target = tmp_path / "fixtures" / "security"
    target.mkdir(parents=True)
    (target / name).write_text(text, encoding="utf-8")
    (target / name.replace(".diff", ".meta.json")).write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(ValueError) as refused:
        tc._security_fixtures()

    assert f"security/{name}" in str(refused.value)
    assert named in str(refused.value)
    assert stubbed == []


def test_an_integration_repository_in_an_unknown_language_is_refused_at_load(
    stubbed: list[str], tmp_path: Path
) -> None:
    """T2. A ``.go`` file under an integration fixture's repository is refused
    by the integration loader, naming the meta file and the path."""
    _place_integration_fixtures(tmp_path / "fixtures")
    repo = tmp_path / "fixtures" / "integration" / "01_d1_stored_rows_repo"
    (repo / "feature" / "src" / "extra.go").write_text("package main\n", encoding="utf-8")

    with pytest.raises(ValueError) as refused:
        cif.load_integration_fixtures()

    assert "01_d1_stored_rows.meta.json" in str(refused.value)
    assert "feature/src/extra.go" in str(refused.value)
    assert stubbed == []


def test_a_detection_role_the_floor_table_does_not_list_is_refused_at_collection(
    stubbed: list[str], tmp_path: Path
) -> None:
    """T2. A Rust security positive, with no ``security_rust`` row in
    ``MIN_ROLE_DETECTION_RATE``, is refused where the paid test's parameters
    are built, naming the fixture and the id. No agent is called."""
    diff, meta = _sec01()
    target = tmp_path / "fixtures" / "security"
    target.mkdir(parents=True)
    (target / "11_sql_injection_rust.diff").write_text(
        _moved(diff, "src/users.rs"), encoding="utf-8"
    )
    (target / "11_sql_injection_rust.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(ValueError) as refused:
        tc._security_positive_easy_fixtures()

    assert "11_sql_injection_rust.diff" in str(refused.value)
    assert "'security_rust'" in str(refused.value)
    assert stubbed == []


def test_an_integration_role_the_floor_table_does_not_list_is_refused_at_collection(
    stubbed: list[str], tmp_path: Path
) -> None:
    """T2. A TypeScript integration positive, with no ``integration_ts`` row in
    ``MIN_ROLE_DETECTION_RATE``, is refused where the paid test's parameters
    are built, naming the meta file and the id. No agent is called."""
    _place_integration_fixtures(tmp_path / "fixtures")

    with pytest.raises(ValueError) as refused:
        tc._integration_params(tc.INTEGRATION_ROLE, cif.integration_positives())

    assert "01_d1_stored_rows_ts.meta.json" in str(refused.value)
    assert "'integration_ts'" in str(refused.value)
    assert stubbed == []
