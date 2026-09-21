"""Reading a calibration baseline back off disk (R5.1/R5.5).

Split out of ``kstrl/calibration.py`` when that file crossed the 800-line
growth ratchet (#406): the split itself is the ratchet, the way
``tests/helpers/demotion.py``, ``kstrl/fixtures_snapshot.py`` and
``kstrl/operator_guidance.py`` each say of themselves. ``kstrl.calibration``
imports this module for ``Baseline``, ``FixtureStats``, ``load_baseline``,
``mean`` and ``role_detection_rate``; the dependency runs one way and must
stay that way - nothing here imports ``kstrl.calibration``, so there is no
cycle.

Two seams were measured and refused before this one. ``scripts/precommit/
cyclomatic_ratchet.py`` keys its census on ``(relative path, name)``, so a
function MOVED to a new file is a new function to it: ``build_report`` is
cyclomatic 11 against a limit of 10, and moving it would have required
splitting it too, which is a separate change #406 does not make.
``compare_baselines`` is cyclomatic 11 for the same reason. So this module
holds the reader and the value types - ``Baseline``, ``FixtureStats``, the
consistency math, ``load_baseline``, and the model-drift check that reads
the newest one - while ``kstrl/calibration.py`` keeps the writer, the
comparison and the CLI.

This module holds:

- **Consistency math**: ``consistency``, ``FixtureStats``, ``mean``,
  ``role_detection_rate`` - the per-fixture and per-role detection-rate
  arithmetic that both the report builder and the baseline reader need.
- **Baseline report: load**: ``Baseline``, ``partial_capture_reason`` and
  ``load_baseline`` parse a baseline file (v1 or v2) back into
  ``FixtureStats``, refusing a partial capture (#398) by name.
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

    One reader for ``load_baseline`` and ``newest_baseline_path``: the
    newest baseline used to be opened and parsed twice on every drift
    check. Raises ``ValueError`` on anything that is not a readable JSON
    object, which is the type both callers already handle.
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

    format_version = int(data.get("format_version", 1))
    raw_fixtures = data.get("fixtures")
    if not isinstance(raw_fixtures, list):
        raise ValueError(f"baseline {path} has no 'fixtures' list")

    fixtures: list[FixtureStats] = []
    for entry in raw_fixtures:
        if not isinstance(entry, dict):
            raise ValueError(f"baseline {path}: fixture entry is not an object")
        role = str(entry.get("role", ""))
        fixture_id = str(entry.get("fixture_id", ""))
        if not role or not fixture_id:
            raise ValueError(f"baseline {path}: fixture entry missing role/fixture_id")
        if format_version >= 2:
            fixtures.append(
                FixtureStats(
                    role=role,
                    fixture_id=fixture_id,
                    category=(
                        str(entry["category"]) if entry.get("category") is not None else None
                    ),
                    cwe=str(entry["cwe"]) if entry.get("cwe") is not None else None,
                    runs_total=int(entry.get("runs_total", 0)),
                    runs_errored=int(entry.get("runs_errored", 0)),
                    runs_detected=int(entry.get("runs_detected", 0)),
                )
            )
        else:
            fixtures.append(
                FixtureStats(
                    role=role,
                    fixture_id=fixture_id,
                    category=None,
                    cwe=None,
                    runs_total=1,
                    runs_errored=0,
                    runs_detected=1 if bool(entry.get("caught")) else 0,
                )
            )

    return Baseline(
        path=path,
        model=str(data.get("model", "unknown")),
        timestamp=str(data.get("timestamp", UNKNOWN_TIMESTAMP)),
        format_version=format_version,
        runs_per_fixture=int(data.get("runs_per_fixture", 1)),
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
