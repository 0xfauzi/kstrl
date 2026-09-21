"""Reading a calibration baseline back off disk (R5.1/R5.5).

Split out of ``kstrl/calibration.py`` when that file would have crossed
the 800-line growth ratchet (#406): the split itself is the ratchet, the
way ``tests/helpers/demotion.py``, ``kstrl/fixtures_snapshot.py`` and
``kstrl/operator_guidance.py`` each say of themselves. ``kstrl.calibration``
imports this module; the dependency runs one way and must stay that way -
nothing here imports ``kstrl.calibration``, so there is no cycle.

This module holds:

- **Consistency math**: ``consistency``, ``FixtureStats``, ``mean``,
  ``role_detection_rate`` - the per-fixture and per-role detection-rate
  arithmetic that both the report builder and the baseline reader need.
- **Baseline report: write**: ``fixture_entry`` and ``baseline_document``
  are the ONLY places the v2 document's keys are written (#421 Group B).
  ``kstrl.calibration.build_report`` computes every value and calls these
  two to spell them, so a document key has one owner in both directions
  instead of being spelled independently in the module that writes and
  the module that reads.
- **Baseline report: load**: ``Baseline``, ``partial_capture_reason``,
  ``fixture_from_entry`` and ``load_baseline`` parse a baseline file (v1
  or v2) back into ``FixtureStats``, refusing a partial capture (#398) by
  name and refusing a v2 entry whose run counts are absent, non-integer or
  negative, a v1 entry whose ``caught`` is absent or non-boolean, and a
  ``role``/``fixture_id`` that is absent, empty or not a string (#421).
- **Model drift (R5.5, H2-extended)**: ``newest_baseline_path`` and
  ``model_drift_message`` find the newest complete baseline and compare its
  recorded model against the one configured now, so the always-run
  structural test can warn that a re-calibration is due.

Detection-rate semantics: a fixture's *consistency* is
``runs_detected / runs_completed`` (agent-infrastructure errors are excluded
from the denominator; unparseable model output counts as a completed miss).
A fixture is *detected* when its consistency reaches
``FIXTURE_DETECTION_THRESHOLD`` (majority of completed runs). A role's
*detection_rate* is the mean consistency across its fixtures - the expected
single-run detection probability, which is what the thresholds gate on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: What ``load_baseline`` puts in ``Baseline.timestamp`` when the file
#: carries no ``timestamp`` key. A FILL-IN, not an identity: every such
#: baseline shares it, so anything that keys on a baseline has to
#: recognise it and key on something else. Named here rather than
#: spelled twice, so the reader and the recogniser cannot disagree.
UNKNOWN_TIMESTAMP = "unknown"

# #421 Group B: sixteen report-key constants used to live here, spelled
# independently again in kstrl/calibration.py, so a rename in one half
# left the other reading a key that is no longer written - #421 showed
# that reads as zero rather than failing. Rather than police the two
# spellings with a census, the document now has one owner BY
# CONSTRUCTION: every key below is a bare literal, and it is spelled in
# exactly one place - inside ``fixture_entry``, ``baseline_document`` or
# ``load_baseline`` - so there is nowhere left for a second spelling to
# live. ``kstrl.calibration.build_report`` never spells a document key;
# it only reads the per-run RECORD fields (``role``, ``fixture_id``,
# ``category``, ``cwe``, ``caught``, ``error``) documented at its own
# docstring, five of which happen to share a spelling with a document key
# without being one - ``tests/test_calibration_report_format.py`` pins
# that set separately from the document.
#
# The one number both halves still depend on independently is the format
# VERSION: ``REPORT_FORMAT_VERSION`` below is what ``build_report`` writes
# and what ``load_baseline`` compares a document's ``format_version``
# against to take the v2 branch. One int, one owner, here - a census
# cannot help here anyway, because ``folded_str`` only ever sees strings.
REPORT_FORMAT_VERSION = 2

# FIXTURE_DETECTION_THRESHOLD = 0.5: a fixture counts as detected when a
# majority of its completed runs caught the planted issue (2 of 3 at the
# default run count). One flaky miss does not fail the suite; a fixture
# that misses most runs does.
FIXTURE_DETECTION_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# Consistency math
# ---------------------------------------------------------------------------


def consistency(runs_detected: int, runs_completed: int) -> float:
    """Fraction of completed runs that detected the planted issue.

    Zero completed runs (every run hit an agent-infrastructure error)
    yields 0.0 - the caller is expected to have skipped such fixtures
    before they reach a gate.
    """
    if runs_completed <= 0:
        return 0.0
    return runs_detected / runs_completed


@dataclass(frozen=True)
class FixtureStats:
    """Aggregated result of running one fixture N times."""

    role: str
    fixture_id: str
    category: str | None
    cwe: str | None
    runs_total: int
    runs_errored: int
    runs_detected: int

    @property
    def runs_completed(self) -> int:
        return self.runs_total - self.runs_errored

    @property
    def consistency(self) -> float:
        return consistency(self.runs_detected, self.runs_completed)

    @property
    def detected(self) -> bool:
        return self.runs_completed > 0 and self.consistency >= FIXTURE_DETECTION_THRESHOLD


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean, or 0.0 for an empty sequence (``statistics.mean`` raises)."""
    return sum(values) / len(values) if values else 0.0


def role_detection_rate(fixtures: Sequence[FixtureStats]) -> float:
    """Mean per-fixture consistency: expected single-run detection rate."""
    return mean([f.consistency for f in fixtures])


# ---------------------------------------------------------------------------
# Baseline report: load
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    """A parsed baseline file, normalized across format versions."""

    path: Path | None
    model: str
    timestamp: str
    format_version: int
    runs_per_fixture: int
    fixtures: tuple[FixtureStats, ...]

    def roles(self) -> dict[str, list[FixtureStats]]:
        grouped: dict[str, list[FixtureStats]] = {}
        for fixture in self.fixtures:
            grouped.setdefault(fixture.role, []).append(fixture)
        return grouped

    def role_rates(self) -> dict[str, float]:
        return {role: role_detection_rate(fixtures) for role, fixtures in self.roles().items()}

    def category_rates(self) -> dict[str, dict[str, float]]:
        """Per-role, per-category mean consistency. Unknown (None)
        categories - v1 baselines predate category recording - are skipped."""
        rates: dict[str, dict[str, float]] = {}
        for role, fixtures in self.roles().items():
            by_category: dict[str, list[float]] = {}
            for fixture in fixtures:
                if fixture.category is None:
                    continue
                by_category.setdefault(fixture.category, []).append(fixture.consistency)
            if by_category:
                rates[role] = {category: mean(values) for category, values in by_category.items()}
        return rates


def partial_capture_reason(data: Mapping[str, Any]) -> str | None:
    """Why a report is a PARTIAL capture (#398), or None when it is not.

    ``run_complete`` ABSENT means complete: every baseline written before
    #398 was written once, at the end of a run. Anything present that is
    not exactly ``True`` is partial, so junk fails closed. One definition,
    because ``load_baseline`` refuses on it and ``newest_baseline_path``
    skips on it.
    """
    if data.get("run_complete", True) is True:
        return None
    attempted = data.get("fixtures_attempted")
    done = set(data.get("fixtures_completed") or [])
    names = attempted if isinstance(attempted, list) else []
    missing = ", ".join(str(x) for x in names if x not in done) or "none recorded"
    return f"the run did not finish; fixtures attempted but not completed: {missing}"


def _read_document(path: Path) -> dict[str, Any]:
    """Read one baseline file and return its top-level JSON object.

    One reader for ``load_baseline`` and ``newest_baseline_path``, so
    there is one SOURCE copy of the read/parse/is-a-dict prologue instead
    of two. The newest baseline is still read and parsed TWICE on every
    drift check: ``newest_baseline_path`` finds it, then
    ``model_drift_message`` calls ``load_baseline`` on that same path.
    That second read costs 0.04 ms and its only caller is one ungated
    test per session, and removing it would widen
    ``newest_baseline_path``'s return type, so it stays. Raises
    ``ValueError`` on anything that is not a readable JSON object, which
    is the type both callers already handle.
    """
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read baseline {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # Its own message, because this one is printed at the CLI and
        # the remedy differs: "cannot read" is a path or a permission
        # problem, and this file opened fine. kstrl writes baselines as
        # ASCII JSON, so a byte that will not decode came from elsewhere.
        raise ValueError(f"baseline {path} is not valid UTF-8; re-save it as UTF-8: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"baseline {path} is not a JSON object")
    return data


def _required_int(path: Path, where: str, data: Mapping[str, Any], key: str, default: int) -> int:
    """Every ``int`` this module reads off an untrusted document: absent
    falls back to ``default``, present must be a genuine, non-negative
    int (#421 Group A1).

    ``bool`` is refused although it is an ``int`` subclass: JSON ``true``
    in a count is a writer that lost the count, not a count of one. A
    bare ``int(...)`` on a JSON ``null`` used to raise ``TypeError``,
    which ``kstrl.calibration.main``'s ``except ValueError`` does not
    catch, so a malformed ``format_version`` or ``runs_per_fixture``
    reached the terminal as a traceback (exit 1, the CLI's REGRESSION
    code) instead of exit 2. A negative value is the same "writer that
    lost the count" shape an absent key is - refused here rather than
    producing a negative detection rate.
    """
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"baseline {path}: {where} has an invalid {key!r}: {value!r}")
    return value


def _required_count(path: Path, fixture_id: str, entry: Mapping[str, Any], key: str) -> int:
    """One v2 run count, refusing absence, refusing a non-integer, and
    refusing a negative value.

    An absent count used to read as zero (#421), and zero is the
    fail-open direction: ``compare_baselines`` reports ``newly_missed``
    only for a fixture the OLD baseline detected, so an old baseline that
    detected nothing passes the comparison having compared against
    nothing. Zero is a legal VALUE; an absent key is not a value, which
    is the one thing genuinely specific to a run count rather than to
    every int this module reads - :func:`_required_int` owns the rest.
    """
    if key not in entry:
        raise ValueError(f"baseline {path}: fixture {fixture_id!r} has no {key!r}")
    return _required_int(path, f"fixture {fixture_id!r}", entry, key, default=0)


def _required_str(path: Path, index: int, entry: Mapping[str, Any], key: str) -> str:
    """One required, non-empty string field, indexed like every other
    indexed validator in the repo (``kstrl.decisions.required_field_error``
    is the sibling shape for a RETURNED message rather than a raise).

    ``str(...)`` used to coerce first, so a JSON ``null`` role or
    fixture_id loaded as the truthy string ``"None"`` instead of being
    refused (#421 Group A3).
    """
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"baseline {path}: fixture[{index}] has no usable {key!r}: {value!r}")
    return value


def _required_bool(path: Path, fixture_id: str, entry: Mapping[str, Any], key: str) -> bool:
    """A v1 fixture's ``caught`` flag: refuses absence and refuses a
    non-boolean, the same shape :func:`_required_count` gives a v2 run
    count.

    v1 has no run counts; ``caught`` is its one count-bearing field, and
    it used to be read with a falsy default (``bool(entry.get(...))``),
    so an entry that lost the field read as "not detected" - the exact
    #421 fail-open, one format version over.
    """
    if key not in entry:
        raise ValueError(f"baseline {path}: fixture {fixture_id!r} has no {key!r}")
    value = entry[key]
    if not isinstance(value, bool):
        raise ValueError(
            f"baseline {path}: fixture {fixture_id!r} has a non-boolean {key!r}: {value!r}"
        )
    return value


def fixture_entry(fixture: FixtureStats, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The on-disk v2 form of one fixture, including its ``runs[]``
    sub-entries. One of the two places document keys are written (#421
    Group B1): ``kstrl.calibration.build_report`` computes ``fixture``
    and calls this with the per-run RECORDS it grouped to build it.

    ``runs`` is that record sequence, not the document's own ``runs[]``
    shape - three of its fields (``caught``, ``error``, ``detail``)
    happen to share a spelling with what gets written here, which is the
    record/document coincidence ``build_report``'s docstring discloses.
    """
    return {
        "role": fixture.role,
        "fixture_id": fixture.fixture_id,
        "category": fixture.category,
        "cwe": fixture.cwe,
        "runs_total": fixture.runs_total,
        "runs_errored": fixture.runs_errored,
        "runs_detected": fixture.runs_detected,
        "consistency": fixture.consistency,
        "detected": fixture.detected,
        "runs": [
            {
                "caught": bool(r.get("caught")),
                "error": bool(r.get("error")),
                "detail": str(r.get("detail", "")),
            }
            for r in runs
        ],
    }


def baseline_document(
    *,
    model: str,
    timestamp: str,
    runs_per_fixture: int,
    run_complete: bool,
    fixtures_attempted: Sequence[str],
    fixtures_completed: Sequence[str],
    summary: Mapping[str, Any],
    fixtures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The on-disk v2 document. The other of the two places document keys
    are written (#421 Group B1); ``fixtures`` is a sequence of
    :func:`fixture_entry` results."""
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "model": model,
        "timestamp": timestamp,
        "runs_per_fixture": runs_per_fixture,
        "run_complete": run_complete,
        "fixtures_attempted": list(fixtures_attempted),
        "fixtures_completed": list(fixtures_completed),
        "summary": dict(summary),
        "fixtures": list(fixtures),
    }


def document_timestamp(document: Mapping[str, Any]) -> str:
    """The one document key ``kstrl.calibration.save_report`` needs back
    immediately after building it, to name the output file. Owned here,
    not spelled again as a literal in the writer, which would otherwise
    reintroduce exactly the two-spelling shape #421 Group B removes."""
    return str(document["timestamp"])


def fixture_from_entry(
    path: Path, role: str, fixture_id: str, entry: Mapping[str, Any]
) -> FixtureStats:
    """The v2 half of one fixture read, refusing a run count that is
    absent, non-integer or negative (#421 Group A1)."""
    return FixtureStats(
        role=role,
        fixture_id=fixture_id,
        category=(str(entry["category"]) if entry.get("category") is not None else None),
        cwe=str(entry["cwe"]) if entry.get("cwe") is not None else None,
        runs_total=_required_count(path, fixture_id, entry, "runs_total"),
        runs_errored=_required_count(path, fixture_id, entry, "runs_errored"),
        runs_detected=_required_count(path, fixture_id, entry, "runs_detected"),
    )


def load_baseline(path: Path) -> Baseline:
    """Load and normalize a baseline file (v1 or v2).

    v1 files (no ``format_version``) recorded a single boolean run per
    fixture and no category metadata: they normalize to
    ``runs_total=1`` with consistency 1.0/0.0 and ``category=None``.

    Raises ``ValueError`` on malformed content.
    """
    data = _read_document(path)

    reason = partial_capture_reason(data)
    if reason is not None:
        raise ValueError(f"baseline {path} is a partial capture: {reason}")

    format_version = _required_int(path, "document", data, "format_version", default=1)
    raw_fixtures = data.get("fixtures")
    if not isinstance(raw_fixtures, list):
        raise ValueError(f"baseline {path} has no 'fixtures' list")

    fixtures: list[FixtureStats] = []
    for index, entry in enumerate(raw_fixtures):
        if not isinstance(entry, dict):
            raise ValueError(f"baseline {path}: fixture entry is not an object")
        role = _required_str(path, index, entry, "role")
        fixture_id = _required_str(path, index, entry, "fixture_id")
        if format_version >= REPORT_FORMAT_VERSION:
            fixtures.append(fixture_from_entry(path, role, fixture_id, entry))
        else:
            fixtures.append(
                FixtureStats(
                    role=role,
                    fixture_id=fixture_id,
                    category=None,
                    cwe=None,
                    runs_total=1,
                    runs_errored=0,
                    runs_detected=1 if _required_bool(path, fixture_id, entry, "caught") else 0,
                )
            )

    return Baseline(
        path=path,
        model=str(data.get("model", "unknown")),
        timestamp=str(data.get("timestamp", UNKNOWN_TIMESTAMP)),
        format_version=format_version,
        runs_per_fixture=_required_int(path, "document", data, "runs_per_fixture", default=1),
        fixtures=tuple(fixtures),
    )


# ---------------------------------------------------------------------------
# Model drift (R5.5, H2-extended)
# ---------------------------------------------------------------------------


class CalibrationModelDriftWarning(UserWarning):
    """The configured calibration model differs from the newest baseline's."""


def newest_baseline_path(results_dir: Path) -> Path | None:
    """Newest COMPLETE ``baseline-*.json`` by filename
    (baseline-YYYYMMDD-HHMMSS.json sorts lexicographically = chronologically).

    A partial capture (#398) is skipped: returning it would take the drift
    warning SILENT, because ``load_baseline`` refuses it and
    ``model_drift_message`` swallows that refusal. A candidate this cannot
    READ is returned unchanged, so only a file that positively says it is
    partial is skipped. The clause is plain ``ValueError`` because
    ``_read_document`` raises it for an unreadable file, a byte that will
    not decode (``UnicodeDecodeError`` is a ``ValueError``) and a
    non-object document alike.
    """
    if not results_dir.is_dir():
        return None
    for candidate in sorted(results_dir.glob("baseline-*.json"), reverse=True):
        try:
            data = _read_document(candidate)
        except ValueError:
            return candidate
        if partial_capture_reason(data) is None:
            return candidate
    return None


def model_drift_message(results_dir: Path, configured_model: str) -> str | None:
    """Return a warning message when ``configured_model`` differs from the
    newest baseline's recorded model, else None.

    Tolerates a missing results dir, no baselines, or a malformed newest
    baseline (all return None): this feeds an always-run structural test
    that must warn, never fail (R5.5).
    """
    path = newest_baseline_path(results_dir)
    if path is None:
        return None
    try:
        baseline = load_baseline(path)
    except ValueError:
        return None
    if baseline.model == configured_model:
        return None
    return (
        f"configured calibration model {configured_model!r} differs from the "
        f"newest baseline's model {baseline.model!r} ({path.name}). "
        "H2-extended: calibration must be re-run on model change, not just "
        "prompt change - capture a fresh baseline with "
        "KSTRL_RUN_CALIBRATION=1 before trusting detection rates."
    )
