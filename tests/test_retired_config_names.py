"""#395: a retired kstrl.toml name or environment variable is refused by
name instead of being silently ignored.

An unknown kstrl.toml key is ignored by design
(``tests/test_config_toml.py::test_from_toml_ignores_unknown_keys`` pins
that silence for names kstrl never used). A RENAMED key is a different
thing: the old spelling parses fine, does nothing, and a blocking gate
reverts to advisory with no message. So a retired name is REFUSED,
before the command body runs, and the message says what to rename it
to. No alias layer: two spellings do not both live.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from kstrl.config_keys import RETIRED_ENV_VARS as _PRODUCTION_RETIRED_ENV_VARS
from kstrl.config_preflight import collect_config_problems
from kstrl.factory import FactoryConfig
from kstrl.findings import CLAIM_DISAGREEMENT_CATEGORY, Finding


def test_a_retired_toml_section_is_refused_by_name(tmp_path: Path) -> None:
    (tmp_path / "kstrl.toml").write_text("[feedforward]\nenabled = false\n")

    problems = collect_config_problems(tmp_path, warn=lambda _m: None)

    assert len(problems) == 1
    assert "[feedforward]" in problems[0]
    assert "[codebase_scan]" in problems[0]
    assert "will not guess" in problems[0]


def test_a_retired_toml_key_is_refused_by_name(tmp_path: Path) -> None:
    (tmp_path / "kstrl.toml").write_text('[factory]\nsetpoint_agreement = "block"\n')

    problems = collect_config_problems(tmp_path, warn=lambda _m: None)

    assert len(problems) == 1
    assert "setpoint_agreement" in problems[0]
    assert "claim_agreement" in problems[0]
    assert "will not guess" in problems[0]
    # The retired key never reaches the field: it does not silently
    # become the new one either, it just never lands.
    assert FactoryConfig.load(tmp_path).claim_agreement == "advisory"


def test_a_retired_env_var_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KSTRL_FACTORY_SETPOINT_AGREEMENT", "block")

    problems = collect_config_problems(tmp_path, warn=lambda _m: None)

    assert len(problems) == 1
    assert "KSTRL_FACTORY_SETPOINT_AGREEMENT" in problems[0]
    assert "KSTRL_FACTORY_CLAIM_AGREEMENT" in problems[0]
    assert "will not guess" in problems[0]


# Parametrized off the production table itself (kstrl/config_keys.py),
# not a hand-typed copy of it: a hand-typed tuple stops growing the day
# someone forgets to update it alongside a new retired row, which is
# exactly the guard-goes-blind shape CLAUDE.md names. A new row in
# RETIRED_ENV_VARS gets a test for free; P-B3 (plant.md) is the proof: a
# row added there with no matching change here still fails, because this
# list is read from the same dict the row was added to.
@pytest.mark.parametrize("name", sorted(_PRODUCTION_RETIRED_ENV_VARS))
def test_every_retired_env_var_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "1")

    problems = collect_config_problems(tmp_path, warn=lambda _m: None)

    assert len(problems) == 1
    assert name in problems[0]
    assert "will not guess" in problems[0]


def test_a_retired_name_stops_the_command_before_its_body(tmp_path: Path) -> None:
    (tmp_path / "kstrl.toml").write_text("[feedforward]\nenabled = false\n")

    proc = subprocess.run(
        [sys.executable, "-m", "kstrl", "status", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "KSTRL_NO_TUI": "1"},
    )

    # The CLI configuration seam (_KstrlGroup.invoke) exits 2 for a
    # rejected configuration on every command (#452).
    assert proc.returncode == 2
    assert "[codebase_scan]" in proc.stderr
    # The command stopped BEFORE its body: without this assertion the
    # test would also pass on a run that printed the refusal and then
    # went on to run "status" anyway.
    assert "No manifest found" not in proc.stdout + proc.stderr


def test_an_old_finding_record_still_resolves() -> None:
    finding = Finding.from_dict(
        {
            "phase": "review",
            "category": "setpoint_disagreement",
            "severity": "advisory",
            "location": "",
            "explanation": "x",
        }
    )

    assert finding.category == CLAIM_DISAGREEMENT_CATEGORY
    assert CLAIM_DISAGREEMENT_CATEGORY == "claim_disagreement"


# --- T6: the retired vocabulary itself does not survive in the source. ----
#
# Six words #395 retires (case-insensitive substring; every hit is a
# failure): "sensor", "actuator", "setpoint", "dampener", "control loop",
# "control-loop". A seventh check, SENSE_PATTERN, covers the retired CLI
# command name and its many compounds ("ks sense", "sense baseline",
# "_sense", "SENSE_", ...) without also flagging ordinary English "sense"
# ("this makes sense"), which the plain word would.
#
# The module kstrl/feedforward.py keeps its path (#395 measured that three
# pre-commit ratchets refuse the git-mv), so "feedforward" is not one of
# the six retired words. It gets its own, narrower check: a line naming it
# must spell it as a module path or a test/prompt-fixture name, not as the
# bare noun the issue retires everywhere else.
SET_A = (
    "sensor",
    "actuator",
    "setpoint",
    "set-point",
    "set point",
    "dampener",
    "control loop",
    "control-loop",
)

SENSE_PATTERN = re.compile(
    r"""(?ix)
    ks\ sense | sense\ baseline | sense\ run | sense\ step | sense\ dampener
    | _sense | sense_ | sense- | SENSE_ | steps\.sense | ["'`]sense["'`]
    """,
    re.VERBOSE,
)

FEEDFORWARD_ALLOWED = (
    "kstrl.feedforward",
    # "kstrl/feedforward.py" is deliberately not a separate entry here: it
    # is a strict superset-match of "feedforward.py" below (any line
    # containing the former also contains the latter), so it was dead -
    # measured by removing it and finding zero lines it alone allowed.
    "feedforward.py",
    "feedforward_prompts",
    "test_feedforward",
)

REPO_ROOT = Path(__file__).resolve().parents[1]

WALK_EXTENSIONS = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".html", ".example"}

# A per-extension floor, not just a floor on the total. A single floor on
# `len(files)` cannot tell "an extension was dropped from WALK_EXTENSIONS"
# from "the repo grew elsewhere": dropping ".json" (40 files at the time
# this was measured) still clears a >=500 total floor on a repo this
# size, so the walk would go quietly blind to
# scripts/kstrl/baseline.json's renamed key. Re-derive by RUNNING the
# walk, never by editing a number here.
MIN_FILES_BY_EXTENSION: dict[str, int] = {
    ".example": 1,
    ".html": 1,
    ".json": 40,
    ".md": 47,
    ".py": 469,
    ".toml": 2,
    ".yaml": 1,
    ".yml": 6,
}

SKIP_DIRS = {".git", ".venv", ".pytest_cache", "__pycache__", "lessons"}

# Whole files this walk does not read at all, each for a reason recorded
# here rather than left to be rediscovered. This is deliberately down to
# two entries: a whole-file skip is a ledger of give-ups (CLAUDE.md), so
# every other retired-word survivor is a counted ALLOWED line instead.
#   - CHANGELOG.md and docs/lessons/ are the project's own pre-existing
#     historical record; docs/lessons/ is a directory (SKIP_DIRS above).
#   - This file is the guard's own implementation and test fixtures: T1-T5
#     above write literal old TOML/env spellings to prove they are refused,
#     and the constants below (SET_A, SENSE_PATTERN, ALLOWED, ...) name the
#     retired words in order to look for them. A guard's source is not a
#     leak of the thing it detects, the way a spam filter's own source
#     is not spam; skip it rather than allowlisting every comment line one
#     at a time, since T1-T5's assertions already prove they say the old
#     names.
# docs/loop-design.md (a dated design analysis whose SUBJECT is
# control-theory vocabulary) and docs/spec-harness-engineering.md (a dated
# draft spec quoting an external article's own terminology) used to be
# skipped whole here. Both are now walked like any other file, and the
# lines each still needs to keep are counted in ALLOWED below instead.
SKIP_FILES = {
    "CHANGELOG.md",
    "tests/test_retired_config_names.py",
}

# Every remaining hit, and why it is not a leak of the retired vocabulary.
# Keyed by (path relative to repo root, matched word), valued by the exact
# line count so a new, uncounted hit anywhere - including a second hit of
# an already-allowed word in an already-allowed file - fails the test
# rather than being silently absorbed. Re-derive this table by running the
# walk below and reading its report; never hand-adjust a count.
ALLOWED: dict[tuple[str, str], int] = {
    # The one place production code still spells a retired name: the table
    # RETIRED_SECTIONS/RETIRED_KEYS/RETIRED_ENV_VARS in kstrl/config_keys.py,
    # which must keep the old spelling as data or it cannot recognise it to
    # refuse it. (This file's own T1-T5 fixtures do the same; that file is
    # skipped above rather than allowlisted line by line.)
    ("kstrl/config_keys.py", "feedforward"): 7,
    ("kstrl/config_keys.py", "setpoint"): 2,
    # The one data value with a read-side alias: the old finding category,
    # named once so Finding.from_dict can recognise and translate it.
    ("kstrl/findings.py", "setpoint"): 1,
    # MARKDOWN_MARKER's value must not change (records already posted to
    # open PRs match on it); every other mention of it is a quote of that
    # same literal, in the module that defines it, the workflow example
    # that reproduces it, and the tests that assert against it.
    ("kstrl/baseline_report.py", "dampener"): 1,
    ("kstrl/baseline_report.py", "sense"): 1,
    ("docs/baseline.md", "dampener"): 1,
    ("docs/baseline.md", "sense"): 1,
    ("docs/examples/check-baseline.yml", "dampener"): 2,
    ("docs/examples/check-baseline.yml", "sense"): 2,
    ("tests/test_check_baseline.py", "dampener"): 1,
    ("tests/test_check_baseline.py", "sense"): 1,
    # RETIRED_STOPPED_MEASURING_CLAIM is a historical string this PR does
    # not reword (the plan names it explicitly); it happens to use the
    # word "sensor" as a metaphor for a check that stopped reporting.
    ("tests/test_check_baseline.py", "sensor"): 1,
    # A comment recording, for the reader's benefit, which JSON key a v1
    # document used before this PR's schema bump - the old name is the
    # fact being recorded, not a live spelling.
    ("kstrl/baseline.py", "sense"): 1,
    ("kstrl/cli.py", "dampener"): 2,
    # A historical description of a past `grep` command's own output.
    ("tests/test_check_committed_baseline.py", "sense"): 1,
    # "Set-point disagreement" is a live user-facing string inside
    # REVIEWER_PROMPT-adjacent code (claim_retry_context). The prompt BODY
    # is kept byte-identical under Decision 3, so rewording this string
    # would change what ships without moving the version constant or the
    # snapshot hash it is pinned by (H3): the string stays, and its two
    # test mirrors quote it verbatim.
    ("kstrl/review.py", "set-point"): 1,
    ("tests/test_builder_prompts.py", "set-point"): 1,
    ("tests/test_claim_agreement_pipeline.py", "set-point"): 1,
    # Bare references to the ``kstrl.feedforward`` module Decision 1 keeps
    # at its path: attribute access and monkeypatch targets that
    # FEEDFORWARD_ALLOWED's path-shaped substrings do not match.
    ("tests/helpers/feedforward_prompts.py", "feedforward"): 14,
    ("tests/test_feedforward.py", "feedforward"): 8,
    ("tests/test_feedforward_notices.py", "feedforward"): 3,
    # docs/loop-design.md is a dated design analysis whose SUBJECT is
    # control-theory vocabulary: it explains the metaphor kstrl retired
    # (as a rejected alternative, "sensor"/"dampener"/etc. were once
    # considered as kstrl's own names) and states, as a measured fact
    # about the repository at the time it was written, which
    # control-theory words had which occurrence counts. Rewriting its
    # prose would make those sentences false rather than retire a name.
    # Every literal `ks sense` in it was updated to `ks check` (the one
    # thing in the file that was a live command name, not prose about the
    # metaphor); the rest is counted here instead of skipped whole.
    ("docs/loop-design.md", "control loop"): 4,
    ("docs/loop-design.md", "control-loop"): 1,
    ("docs/loop-design.md", "actuator"): 24,
    ("docs/loop-design.md", "dampener"): 9,
    ("docs/loop-design.md", "feedforward"): 5,
    ("docs/loop-design.md", "sensor"): 46,
    ("docs/loop-design.md", "set point"): 13,
    ("docs/loop-design.md", "set-point"): 4,
    # docs/spec-harness-engineering.md is a dated draft spec that quotes
    # an external article's own terminology ("feedback controls",
    # "feedforward controls") as a citation. Rewriting the quote
    # misattributes it to kstrl rather than to the article.
    ("docs/spec-harness-engineering.md", "feedforward"): 21,
    ("docs/spec-harness-engineering.md", "sensor"): 8,
}


def _is_skipped_dir(rel: Path) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts)


def _source_files() -> list[Path]:
    """Every file this walk reads, as paths relative to REPO_ROOT.

    The corpus is what git TRACKS, not what is on disk: a filesystem
    walk counts gitignored files such as ``.complexipy_cache/README.md``
    (four of them on a developer machine, none in CI), and a floor
    derived from that count fails on the checkout that lacks them.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    found: list[Path] = []
    for entry in sorted(listing.decode("utf-8").split("\0")):
        if not entry:
            continue
        rel = Path(entry)
        if _is_skipped_dir(rel):
            continue
        if entry in SKIP_FILES or rel.suffix not in WALK_EXTENSIONS:
            continue
        if not (REPO_ROOT / rel).is_file():
            continue
        found.append(rel)
    return found


def _feedforward_hit(low_line: str) -> bool:
    if "feedforward" not in low_line:
        return False
    return not any(allowed in low_line for allowed in FEEDFORWARD_ALLOWED)


def _words_in_line(line: str) -> list[str]:
    """Every retired word this one line matches, by name."""
    low = line.lower()
    words = [w for w in SET_A if w in low]
    if SENSE_PATTERN.search(line):
        words.append("sense")
    if _feedforward_hit(low):
        words.append("feedforward")
    return words


def test_no_retired_name_survives_in_the_source() -> None:
    counts: dict[tuple[str, str], int] = {}
    unexpected: list[str] = []
    files = _source_files()

    for rel in files:
        relstr = rel.as_posix()
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for word in _words_in_line(line):
                key = (relstr, word)
                counts[key] = counts.get(key, 0) + 1
                if counts[key] <= ALLOWED.get(key, 0):
                    continue
                unexpected.append(f"{relstr}:{lineno}: {word!r}: {line.strip()[:160]}")

    assert not unexpected, "retired vocabulary leaked:\n" + "\n".join(unexpected)
    # A guard that never runs still reports zero unexpected hits. Pin both
    # sides of the census so a walk that quietly stopped walking, or an
    # ALLOWED row nobody re-derived, fails loudly instead of agreeing with
    # itself: files_walked is a floor (the repo only grows), the sum is
    # exact (every allowed line is accounted for, not just capped), and the
    # SKIP_FILES count is exact too (a fifth carve-out must be a deliberate
    # edit here, not a silent addition).
    assert len(files) >= 500, f"walked only {len(files)} files - the walk is not running"
    files_by_ext = Counter(f.suffix for f in files)
    for ext, floor in MIN_FILES_BY_EXTENSION.items():
        found = files_by_ext.get(ext, 0)
        assert found >= floor, (
            f"walked only {found} {ext!r} files, expected at least {floor} - "
            "an extension dropped from WALK_EXTENSIONS clears a total floor "
            "on a repo this size without this per-extension one"
        )
    assert sum(counts.get(k, 0) for k in ALLOWED) == sum(ALLOWED.values())
    assert len(SKIP_FILES) == 2
    assert sum(ALLOWED.values()) == 188
