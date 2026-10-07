"""Calibration tooling: baseline report format, comparison, and thresholds (R5.1/R5.5).

The calibration suite (``tests/test_calibration.py``) measures whether each
adversarial role catches its planted bugs. This module is the non-LLM half of
that loop:

- **Report format (v2)**: N runs per fixture with per-fixture consistency
  (fraction of runs that detected the planted issue), per-role and
  per-category detection rates, and the model id. ``build_report`` /
  ``save_report`` are the single source of the on-disk shape under
  ``tests/adversarial_fixtures/_results/``; the reader is
  ``kstrl.calibration_baseline.load_baseline``.
- **Comparison**: ``python -m kstrl.calibration compare <old.json> <new.json>``
  diffs two baseline files (v1 or v2) and applies the codified thresholds
  below. Exit code 0 = no regression, 1 = regression, 2 = usage/load error
  or a role id ``MIN_ROLE_DETECTION_RATE`` does not list.
  This is what H2's "compare against the baseline" concretely means.
  ``--root`` points it at a project so a regression also reaches the
  autonomy ladder; the consequences live in ``kstrl.calibration_ladder``
  and change no exit code except by adding 2 for an unloadable config.
- **Kind synonyms**: the architect matcher accepts documented paraphrases of
  the spec-issue taxonomy (see ``KIND_SYNONYM_GROUPS``) so a planted issue
  reported under a sibling label is a hit, not a miss.

Detection-rate semantics (what *consistency*, *detected* and
*detection_rate* mean, and ``FIXTURE_DETECTION_THRESHOLD``) live in
``kstrl.calibration_baseline``'s docstring, beside the arithmetic that
defines them.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_json
from kstrl.calibration_baseline import (
    REPORT_FORMAT_VERSION as REPORT_FORMAT_VERSION,
)
from kstrl.calibration_baseline import (
    Baseline,
    FixtureStats,
    baseline_document,
    document_timestamp,
    fixture_entry,
    load_baseline,
    mean,
    role_detection_rate,
)

# ---------------------------------------------------------------------------
# Codified thresholds (R5.1) - the single source of truth for what counts as
# a calibration regression. Documented in docs/adversarial-design.md.
#
# Sizing rationale (fixture counts as of R5.1: security 5, reviewer 3,
# architect 3, architect_allowed_paths 1; default 3 runs per fixture):
#
# - MAX_ROLE_DETECTION_DROP = 0.15: for a 3-fixture role at 3 runs, one
#   run flipping is a drop of 1/9 ~ 0.11 (allowed as run-to-run variance);
#   an entire fixture going dark is 1/3 ~ 0.33 (fails). For the 5-fixture
#   security role: run flip 0.07 allowed, fixture loss 0.20 fails.
# - MAX_CATEGORY_DETECTION_DROP = 0.40: categories often hold a single
#   fixture, so at 3 runs one run flip is 0.33 (allowed) and two flips are
#   0.67 (fails). Single-run (v1) baselines make any category flip a 1.0
#   drop - category gating is only meaningful at N >= 3, which is why
#   DEFAULT_CALIBRATION_RUNS is 3.
# - MIN_ROLE_DETECTION_RATE: absolute floors on the NEW baseline,
#   independent of the old one, so a slow multi-comparison slide cannot
#   ratchet a role to zero. security 0.80 = at most one of five fixtures
#   lost; reviewer/architect 0.65 = at least two of three caught.
# - integration 0.65 and integration_clean 1.0 (#482) are design #480
#   section 8's acceptance, adopted by its decision 6: each positive at or
#   above the reviewer floor, and no clean twin opening a finding in any
#   run. The paid tests gate each fixture on the same two numbers.
# - security_hard and architect_reuse 0.50 are the value the deleted
#   DEFAULT_MIN_ROLE_DETECTION_RATE gave them, written out (#633) so no
#   role is gated against a number nobody chose for it.
# - None (#633) means the role is recorded and not gated: compare applies
#   no floor to it, still applies MAX_ROLE_DETECTION_DROP against its
#   previous capture, and names it under "not gated" in its report. It is
#   for a role whose first capture has not been taken yet, so its floor
#   can be set from that capture. A role id this table does not list is
#   refused by compare (exit 2), so a new id has to be written here, with
#   a floor or with None, before any capture of it can be compared.
# - FP_RATE_MAX = 0.34 (R5.2, owned here since #633): a negative role's
#   false-positive rate in the NEW baseline must be at or below it, every
#   role included, so a reviewer that flags clean code fails compare the
#   way one that misses planted bugs does. The rate is the fraction of the
#   role's negative fixtures a majority of runs flagged: with four
#   negatives one false positive is 0.25 (allowed) and two are 0.50
#   (fails). The capture harness writes the block and reads this constant.
# - MIN_FP_NEGATIVES = 3 (#633, owner decision 6): with one or two
#   negatives a rate can only be 0, 0.5 or 1, so the ceiling would mean
#   "no false positive at all". A role measured over fewer negatives is
#   reported with its count and not gated, and the report says why.
# ---------------------------------------------------------------------------

DEFAULT_CALIBRATION_RUNS = 3
MAX_ROLE_DETECTION_DROP = 0.15
MAX_CATEGORY_DETECTION_DROP = 0.40
FP_RATE_MAX = 0.34
MIN_FP_NEGATIVES = 3
MIN_ROLE_DETECTION_RATE: dict[str, float | None] = {
    "security": 0.80,
    "security_hard": 0.50,
    "reviewer": 0.65,
    "architect": 0.65,
    "architect_allowed_paths": 0.50,
    "architect_reuse": 0.50,
    "integration": 0.65,
    "integration_clean": 1.0,
    # #633 slice 4: twins of the security and reviewer positives in a second
    # code language. The "_<language>" suffix of a role id is an attribute of
    # the fixture, which the calibration harness reads off the fixture's own
    # files (#696); nothing here names a language. The floors are the family
    # floors (#633 decision D4(a)), set from the first captures:
    # baseline-20261006-195010.json gave security_ts 1.00 and
    # baseline-20261006-190951.json gave reviewer_ts 1.00 (haiku, 3 runs,
    # `KSTRL_RUN_CALIBRATION=1 uv run pytest tests/test_calibration.py -k
    # "test_security_role or test_reviewer_role"`).
    "security_ts": 0.80,
    "reviewer_ts": 0.65,
    # #696 slice 8: the reviewer's test-weakening criterion, which replaced
    # the mechanical Layer 0 check, and its #633 twin. Recorded, not gated,
    # until the first capture lets the owner set a floor.
    "reviewer_test_weakening": None,
    "reviewer_test_weakening_ts": None,
    # #696 slice 9: the security reviewer lists every dependency a change
    # adds, which replaced the [policy] dependency and license gates.
    # Recorded, not gated, until their first capture sets a floor.
    "security_dependency": None,
    "security_dependency_ts": None,
    # #700 slice 7: the verification designer's checks, scored by running them
    # on tests/adversarial_fixtures/acceptance/. Recorded, not gated, until the
    # first capture sets the floors that let a model-written plan gate
    # (owner decision 10).
    "acceptance": None,
    "acceptance_clean": None,
}

# REPORT_FORMAT_VERSION lives in kstrl.calibration_baseline (#421 Group B3):
# it is both what this module writes and the threshold
# kstrl.calibration_baseline.load_baseline compares a document's
# format_version against, so it has one owner rather than two numbers that
# happen to agree. Imported above and re-exported here because
# tests/test_calibration_compare.py reads it as ``calibration.REPORT_FORMAT_VERSION``.

# ---------------------------------------------------------------------------
# Spec-issue kind synonyms (R5.1 matcher fix).
#
# DECOMPOSE_PROMPT's taxonomy separates ambiguity / missing_detail /
# contradiction / unstated_assumption / undefined_failure_mode /
# out_of_scope_creep / other, but the boundary inside the "the spec is
# silent about X" family is a judgment call the model makes differently
# run to run: "no audit log requirement" is simultaneously a missing
# detail, an unstated assumption that auditing is not needed, and an
# undefined failure mode. Every architect miss in the recorded baselines
# (baseline-20260527-161822 spec-01, baseline-20260527-191337 and
# baseline-20260527-195157 spec-02) is exactly this artifact: the planted
# issue WAS reported, under a sibling label (missing_detail instead of
# undefined_failure_mode / unstated_assumption).
#
# Groups are symmetric: a required kind is satisfied by any member of its
# group. Kinds outside any group (ambiguity, contradiction,
# out_of_scope_creep, other) only match themselves - ambiguity is about
# vague language that IS present, not absence, and stays distinct.
# Exact-label matching is still reported (``exact_kinds_present``) as a
# non-gating signal so taxonomy drift stays visible in run details.
# ---------------------------------------------------------------------------

KIND_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"missing_detail", "unstated_assumption", "undefined_failure_mode"}),
)


def acceptable_kinds(required_kind: str) -> frozenset[str]:
    """Return the set of emitted kinds that satisfy ``required_kind``."""
    for group in KIND_SYNONYM_GROUPS:
        if required_kind in group:
            return group
    return frozenset({required_kind})


def required_kinds_satisfied(
    required: Iterable[str],
    actual: Iterable[str],
) -> bool:
    """True when every required kind is present in ``actual`` up to synonyms."""
    actual_set = set(actual)
    return all(acceptable_kinds(kind) & actual_set for kind in required)


def exact_kinds_present(required: Iterable[str], actual: Iterable[str]) -> bool:
    """True when every required kind is present under its exact label."""
    return set(required).issubset(set(actual))


# ---------------------------------------------------------------------------
# Baseline report: build / save
# ---------------------------------------------------------------------------


def build_report(
    records: Sequence[Mapping[str, Any]],
    *,
    model: str,
    timestamp: str,
    runs_per_fixture: int,
    run_complete: bool = True,
    fixtures_attempted: Sequence[str] = (),
    fixtures_completed: Sequence[str] = (),
) -> dict[str, Any]:
    """Assemble the v2 report JSON from per-run records.

    Each record is one run of one fixture:
    ``{"role", "fixture_id", "category", "cwe", "caught", "error", "detail"}``
    where ``error=True`` marks an agent-infrastructure failure (excluded
    from the consistency denominator; a parse failure of model output is
    NOT an error - it is a completed miss). This is the RECORD contract,
    a separate thing from the on-disk document: this function computes
    every value from the records and hands them to
    ``kstrl.calibration_baseline.fixture_entry`` and ``baseline_document``,
    which are the only places a document KEY is spelled (#421 Group B).

    ``run_complete``, ``fixtures_attempted`` and ``fixtures_completed``
    (#398) are the progress bookkeeping a capture HARNESS keeps about its
    own begin/complete calls, which this function never sees - it only
    receives the finished per-run records. The harness knows the VALUES
    (whether teardown was reached, which fixture names it began and
    finished); ``baseline_document`` owns the KEYS, so the v2 format has
    one writer. The defaults describe a single-shot build that finished.
    ``kstrl.calibration_baseline.partial_capture_reason`` is the reader on
    the other side of this seam.
    """
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for record in records:
        key = (str(record["role"]), str(record["fixture_id"]))
        if key not in grouped:
            order.append(key)
        grouped.setdefault(key, []).append(record)

    fixtures_json: list[dict[str, Any]] = []
    stats: list[FixtureStats] = []
    for role, fixture_id in order:
        runs = grouped[(role, fixture_id)]
        errored = sum(1 for r in runs if bool(r.get("error")))
        detected = sum(1 for r in runs if bool(r.get("caught")) and not bool(r.get("error")))
        first = runs[0]
        fixture = FixtureStats(
            role=role,
            fixture_id=fixture_id,
            category=(str(first["category"]) if first.get("category") is not None else None),
            cwe=str(first["cwe"]) if first.get("cwe") is not None else None,
            runs_total=len(runs),
            runs_errored=errored,
            runs_detected=detected,
        )
        stats.append(fixture)
        fixtures_json.append(fixture_entry(fixture, runs))

    summary: dict[str, Any] = {}
    by_role: dict[str, list[FixtureStats]] = {}
    for fixture in stats:
        by_role.setdefault(fixture.role, []).append(fixture)
    for role, fixtures in by_role.items():
        role_summary: dict[str, Any] = {
            "fixtures_total": len(fixtures),
            "fixtures_detected": sum(1 for f in fixtures if f.detected),
            "detection_rate": role_detection_rate(fixtures),
        }
        by_category: dict[str, list[float]] = {}
        by_cwe: dict[str, list[float]] = {}
        for fixture in fixtures:
            if fixture.category is not None:
                by_category.setdefault(fixture.category, []).append(fixture.consistency)
            if fixture.cwe is not None:
                by_cwe.setdefault(fixture.cwe, []).append(fixture.consistency)
        if by_category:
            role_summary["by_category"] = {
                category: {
                    "fixtures_total": len(values),
                    "detection_rate": mean(values),
                }
                for category, values in by_category.items()
            }
        if by_cwe:
            role_summary["by_cwe"] = {
                cwe: {
                    "fixtures_total": len(values),
                    "detection_rate": mean(values),
                }
                for cwe, values in by_cwe.items()
            }
        summary[role] = role_summary

    return baseline_document(
        model=model,
        timestamp=timestamp,
        runs_per_fixture=runs_per_fixture,
        run_complete=run_complete,
        fixtures_attempted=fixtures_attempted,
        fixtures_completed=fixtures_completed,
        summary=summary,
        fixtures=fixtures_json,
    )


def save_report(report: Mapping[str, Any], results_dir: Path) -> Path:
    """Write a report to ``baseline-<timestamp>.json`` in ``results_dir``.

    Called many times during one run since #398, so the write is the
    atomic one and no reader sees half a document. Same bytes as before:
    ``atomic_write_json`` writes the same indent and trailing newline, and
    it needs the parent to exist, so the ``mkdir`` stays.
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"baseline-{document_timestamp(report)}.json"
    atomic_write_json(out, report)
    return out


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateDelta:
    """Old-vs-new detection rate for one role (category=None) or category."""

    role: str
    category: str | None
    old_rate: float | None
    new_rate: float | None

    @property
    def drop(self) -> float:
        if self.old_rate is None or self.new_rate is None:
            return 0.0
        return self.old_rate - self.new_rate


@dataclass(frozen=True)
class Comparison:
    """Result of comparing two baselines under the codified thresholds."""

    old: Baseline
    new: Baseline
    role_deltas: tuple[RateDelta, ...]
    category_deltas: tuple[RateDelta, ...]
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    newly_missed: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def min_role_rate(role: str) -> float | None:
    """``role``'s floor, or None when the role is recorded and not gated.

    Indexes the table with no default (#633): a role id it does not list
    is refused by :func:`main` before any comparison, so a ``KeyError``
    here is a caller that skipped that refusal.
    """
    return MIN_ROLE_DETECTION_RATE[role]


def unlisted_roles(*baselines: Baseline) -> list[str]:
    """Every role id the baselines carry that ``MIN_ROLE_DETECTION_RATE``
    does not list, sorted (#633)."""
    carried = {role for baseline in baselines for role in baseline.roles()}
    return sorted(carried - MIN_ROLE_DETECTION_RATE.keys())


def _floor_failure(role: str, new_rate: float) -> str | None:
    """The failure line when ``new_rate`` is under ``role``'s floor, else
    None. A role with no floor (None) has no floor failure."""
    floor = min_role_rate(role)
    if floor is None or new_rate >= floor:
        return None
    return f"role {role!r} detection rate {new_rate:.2f} is below its floor {floor:.2f}"


def _false_positive_failures(new: Baseline) -> list[str]:
    """One failure per negative role in ``new`` above ``FP_RATE_MAX``
    (#633). A role measured over fewer than ``MIN_FP_NEGATIVES`` negatives
    is not gated; :func:`_false_positive_lines` names it and says why. No
    block means no negative ran, which has nothing to gate."""
    if new.fp_rates is None:
        return []
    return [
        f"negative role {role!r} false-positive rate {negative.fp_rate:.2f} "
        f"is above the ceiling {FP_RATE_MAX:.2f}"
        for role, negative in sorted(new.fp_rates.items())
        if negative.negatives >= MIN_FP_NEGATIVES and negative.fp_rate > FP_RATE_MAX
    ]


def compare_baselines(old: Baseline, new: Baseline) -> Comparison:
    """Diff two baselines and apply the threshold block.

    Failures (regression, exit 1): a role's rate dropping more than
    ``MAX_ROLE_DETECTION_DROP``; a role's new rate below its
    ``MIN_ROLE_DETECTION_RATE`` floor (a role whose floor is None has
    none, and is still held to the drop); a category's rate dropping more
    than ``MAX_CATEGORY_DETECTION_DROP``; a negative role in the new
    baseline, measured over at least ``MIN_FP_NEGATIVES`` negatives, whose
    false-positive rate is above ``FP_RATE_MAX``.

    Warnings (reported, exit stays 0): roles/categories present in the
    old baseline but absent from the new one (partial runs are
    legitimate - e.g. an architect-only re-run after a DECOMPOSE_PROMPT
    edit), and comparisons across different models (H2-extended: those
    measure the model change, not the prompt change).
    """
    failures: list[str] = []
    warnings: list[str] = []

    if old.model != new.model:
        warnings.append(
            f"comparing across models ({old.model!r} -> {new.model!r}): "
            "deltas measure the model change, not a prompt change "
            "(H2-extended: re-calibrate on model change)"
        )

    old_rates = old.role_rates()
    new_rates = new.role_rates()

    role_deltas: list[RateDelta] = []
    for role in sorted(set(old_rates) | set(new_rates)):
        old_rate = old_rates.get(role)
        new_rate = new_rates.get(role)
        delta = RateDelta(role=role, category=None, old_rate=old_rate, new_rate=new_rate)
        role_deltas.append(delta)
        if new_rate is None:
            warnings.append(f"role {role!r} present in old baseline but not exercised in new")
            continue
        floor_failure = _floor_failure(role, new_rate)
        if floor_failure is not None:
            failures.append(floor_failure)
        if old_rate is not None and delta.drop > MAX_ROLE_DETECTION_DROP:
            failures.append(
                f"role {role!r} detection rate dropped "
                f"{old_rate:.2f} -> {new_rate:.2f} "
                f"(drop {delta.drop:.2f} > {MAX_ROLE_DETECTION_DROP:.2f})"
            )

    old_categories = old.category_rates()
    new_categories = new.category_rates()
    category_deltas: list[RateDelta] = []
    for role in sorted(set(old_categories) | set(new_categories)):
        old_by_cat = old_categories.get(role, {})
        new_by_cat = new_categories.get(role, {})
        if role not in new_rates:
            continue  # whole role missing: already warned above
        for category in sorted(set(old_by_cat) | set(new_by_cat)):
            old_rate = old_by_cat.get(category)
            new_rate = new_by_cat.get(category)
            delta = RateDelta(
                role=role,
                category=category,
                old_rate=old_rate,
                new_rate=new_rate,
            )
            category_deltas.append(delta)
            if new_rate is None:
                warnings.append(
                    f"category {role}/{category} present in old baseline but not exercised in new"
                )
                continue
            if old_rate is not None and delta.drop > MAX_CATEGORY_DETECTION_DROP:
                failures.append(
                    f"category {role}/{category} detection rate dropped "
                    f"{old_rate:.2f} -> {new_rate:.2f} "
                    f"(drop {delta.drop:.2f} > {MAX_CATEGORY_DETECTION_DROP:.2f})"
                )

    failures.extend(_false_positive_failures(new))

    old_fixtures = {(f.role, f.fixture_id): f for f in old.fixtures}
    new_fixtures = {(f.role, f.fixture_id): f for f in new.fixtures}
    newly_missed = tuple(
        f"{role}/{fixture_id}"
        for (role, fixture_id), old_fixture in sorted(old_fixtures.items())
        if old_fixture.detected
        and (role, fixture_id) in new_fixtures
        and not new_fixtures[(role, fixture_id)].detected
    )

    return Comparison(
        old=old,
        new=new,
        role_deltas=tuple(role_deltas),
        category_deltas=tuple(category_deltas),
        failures=tuple(failures),
        warnings=tuple(warnings),
        newly_missed=newly_missed,
    )


def _format_rate(rate: float | None) -> str:
    return "-" if rate is None else f"{rate:.2f}"


def _floor_text(role: str) -> str:
    floor = min_role_rate(role)
    return "no floor set" if floor is None else f"floor {floor:.2f}"


def _unjudged_blocks(comparison: Comparison) -> list[str]:
    """The report lines naming the roles the new baseline measures that
    nothing judged (#633): a first measurement has no old rate for the drop
    check, and a role whose floor is None has no floor check."""
    measured = [delta for delta in comparison.role_deltas if delta.new_rate is not None]
    first = [delta.role for delta in measured if delta.old_rate is None]
    ungated = [delta.role for delta in measured if min_role_rate(delta.role) is None]
    lines: list[str] = []
    if first:
        lines.append("")
        lines.append("first measurements (the old baseline has no rate for these roles):")
        lines.extend(f"  {role}" for role in first)
    if ungated:
        lines.append("")
        lines.append("not gated (MIN_ROLE_DETECTION_RATE sets no floor for these roles):")
        lines.extend(f"  {role}" for role in ungated)
    return lines


def _false_positive_lines(new: Baseline) -> list[str]:
    """The report lines for the new baseline's negative roles (#633): each
    role's false-positive rate and the count of negatives it was measured
    over, or one line saying no negative ran when the block is absent."""
    if new.fp_rates is None:
        return [
            "",
            "false-positive rate: not measured (the new baseline has no "
            "false-positive block, so no negative fixture ran)",
        ]
    lines = [
        "",
        f"false-positive rate per negative role (new baseline; ceiling {FP_RATE_MAX:.2f}, "
        f"gated from {MIN_FP_NEGATIVES} negatives):",
    ]
    for role, negative in sorted(new.fp_rates.items()):
        count = f"{negative.negatives} negatives"
        if negative.negatives < MIN_FP_NEGATIVES:
            count += f": fewer than {MIN_FP_NEGATIVES}, not gated"
        lines.append(f"  {role:<26} {negative.fp_rate:.2f}  ({count})")
    return lines


def format_comparison(comparison: Comparison) -> str:
    """Human-readable comparison report."""
    old, new = comparison.old, comparison.new
    lines: list[str] = [
        "calibration compare",
        f"  old: {old.path} (model={old.model}, runs={old.runs_per_fixture}, ts={old.timestamp})",
        f"  new: {new.path} (model={new.model}, runs={new.runs_per_fixture}, ts={new.timestamp})",
        "",
        "per-role detection rate (mean per-fixture consistency):",
    ]
    for delta in comparison.role_deltas:
        lines.append(
            f"  {delta.role:<26} {_format_rate(delta.old_rate)} -> "
            f"{_format_rate(delta.new_rate)}  ({_floor_text(delta.role)})"
        )
    lines.extend(_unjudged_blocks(comparison))
    lines.extend(_false_positive_lines(new))
    if comparison.category_deltas:
        lines.append("")
        lines.append("per-category detection rate:")
        for delta in comparison.category_deltas:
            lines.append(
                f"  {delta.role}/{delta.category or '?':<24} "
                f"{_format_rate(delta.old_rate)} -> {_format_rate(delta.new_rate)}"
            )
    if comparison.newly_missed:
        lines.append("")
        lines.append("fixtures newly missed (detected in old, not in new):")
        for name in comparison.newly_missed:
            lines.append(f"  {name}")
    if comparison.warnings:
        lines.append("")
        lines.append("warnings:")
        for warning in comparison.warnings:
            lines.append(f"  WARN: {warning}")
    lines.append("")
    if comparison.passed:
        lines.append("PASS: no calibration regression under the codified thresholds")
    else:
        lines.append("FAIL: calibration regression detected:")
        for failure in comparison.failures:
            lines.append(f"  FAIL: {failure}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reviewer-family override (R7.1)
# ---------------------------------------------------------------------------


def reviewer_override_from_env(
    environ: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """(agent_type, model) override for the reviewer and security
    calibration agents (R7.1), read from
    ``KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE`` /
    ``KSTRL_CALIBRATION_REVIEWER_MODEL``. Empty values mean unset.

    The override exists so the calibration suite can measure the
    same-family vs cross-family correlated-miss delta: one baseline with
    no override (same family end to end), one with the reviewer roles on
    the second family. The architect always keeps the base calibration
    agent - rotation applies to reviewers, not the spec red-team."""
    agent_type = (
        environ.get("KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE")
        or environ.get("KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE")
        or None
    )
    model = (
        environ.get("KSTRL_CALIBRATION_REVIEWER_MODEL")
        or environ.get("KSTRL_CALIBRATION_REVIEWER_MODEL")
        or None
    )
    return agent_type, model


def reviewer_override_label(
    base_model: str,
    reviewer_agent_type: str | None,
    reviewer_model: str | None,
) -> str:
    """Baseline ``model`` label for a run with a reviewer override.

    No override keeps the plain base model id. Override runs get
    ``<base>+reviewer:<type>/<model>`` so ``compare_baselines``'s
    cross-model warning fires exactly when the reviewer family differs
    between the two baselines - that comparison measures the family
    change, which is what R7.1 wants surfaced, not hidden."""
    if not reviewer_agent_type and not reviewer_model:
        return base_model
    reviewer = "/".join(part for part in (reviewer_agent_type, reviewer_model) if part)
    return f"{base_model}+reviewer:{reviewer}"


# ---------------------------------------------------------------------------
# CLI: python -m kstrl.calibration compare <old.json> <new.json>
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kstrl.calibration",
        description="Calibration baseline tooling (R5.1).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare_parser = subparsers.add_parser(
        "compare",
        help="Diff two baseline JSONs under the codified thresholds; exit 1 on regression.",
    )
    compare_parser.add_argument("old", type=Path, help="older baseline JSON")
    compare_parser.add_argument("new", type=Path, help="newer baseline JSON")
    compare_parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "project root holding kstrl.toml and the control directory, "
            "consulted for the autonomy ladder (default: cwd)"
        ),
    )
    args = parser.parse_args(argv)

    if args.command == "compare":
        try:
            old = load_baseline(args.old)
            new = load_baseline(args.new)
            if not any(f.detected for f in old.fixtures):
                # #421 Group A4: the fail-open the issue is about lives
                # here, not in the reader. An OLD baseline with no
                # detected fixture bounds nothing - compare_baselines
                # reports newly_missed only for a fixture the OLD
                # baseline detected, and a role/category drop only
                # against an old_rate that exists, so a baseline that
                # detected nothing passes having compared against
                # nothing. Raised inside this try, not in load_baseline
                # (newest_baseline_path/model_drift_message have no
                # comparison to bound) and not in compare_baselines (the
                # Comparison dataclass keeps its current meaning), so it
                # reaches the same "error: ..." / exit 2 path as a
                # malformed document.
                raise ValueError(
                    f"old baseline {old.path} has no detected fixture, so it bounds "
                    "nothing: a comparison against it reports newly_missed for no "
                    "fixture and a drop for no role. Re-capture the old baseline (#421)"
                )
            unlisted = unlisted_roles(old, new)
            if unlisted:
                # #633: before compare_baselines and report_to_ladder, so an
                # id nobody has decided a floor for, a misspelt one included,
                # reaches neither a verdict nor the inbox.
                raise ValueError(
                    f"role ids {unlisted} are not in kstrl.calibration.MIN_ROLE_DETECTION_RATE; "
                    "add each with its floor, or with None to record it without gating it (#633)"
                )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        comparison = compare_baselines(old, new)
        print(format_comparison(comparison))
        # Imported here rather than at module scope so this module, the
        # non-LLM measurement half, does not drag the control plane
        # (autonomy, inbox, UI) into every importer of a comparison.
        from kstrl.calibration_ladder import report_to_ladder

        override = report_to_ladder(comparison, (args.root or Path.cwd()).resolve())
        if override is not None:
            return override
        return 0 if comparison.passed else 1
    return 2  # pragma: no cover - argparse enforces the subcommand


if __name__ == "__main__":
    raise SystemExit(main())
