"""How a dampener comparison is rendered: terminal, pull-request comment, JSON.

Split out of :mod:`kstrl.dampener` in review round 1 of #227, when the module
crossed the repository's 800-line ratchet. The split is where the two jobs
already were: that module decides WHAT a branch changed about a measurement,
this one decides how a person or a workflow reads the answer. It imports one
way only, so the arithmetic can be tested without a renderer in scope.

:data:`MARKDOWN_MARKER` and the two bucket-title notes live here rather than
beside the buckets, because a renderer is the only thing that can spell them
and the workflow test compares the YAML against this constant.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kstrl.dampener import Baseline, Comparison

#: First line of the markdown report. A workflow finds its own earlier comment
#: by this string, so it is a constant here and never retyped in the YAML: the
#: workflow test compares the YAML against THIS.
MARKDOWN_MARKER = "<!-- kstrl-sense-dampener -->"

#: Why the fourth bucket is not the third. Both renderers title that bucket
#: with it, so a reworded explanation cannot land in one report and not the
#: other.
UNMEASURED_NOTE = "a check that did not run cannot prove a fix"

#: The fifth bucket's title, for the same reason.
STOPPED_MEASURING_NOTE = "these sensors measured on the baseline and not here"

#: The footer, in the two states the flag puts the job in. Both spellings live
#: here because the renderer is the only thing that says them and the workflow
#: test reads THESE rather than retyping either.
ADVISORY_FOOTER = "This report is advisory: it never fails the job."
BLOCKING_FOOTER = "This job fails on a regression."


def short_ref(base_ref: str | None) -> str:
    """The baseline's provenance sha, abbreviated, or ``unknown``."""
    return base_ref[:7] if base_ref else "unknown"


def _notes(comparison: Comparison, baseline: Baseline) -> list[str]:
    notes: list[str] = []
    if comparison.sense_schema_changed is not None:
        was, now = comparison.sense_schema_changed
        notes.append(
            f"note: the sense schema moved from {was} to {now} since this baseline "
            "was written; refresh it with ks sense --write-baseline --force"
        )
    if comparison.project_changed is not None:
        was_name, now_name = comparison.project_changed
        notes.append(
            f"note: this baseline was written for {was_name!r} and this run measured "
            f"{now_name!r}; check it is the same project"
        )
    if baseline.base_ref is None:
        notes.append("note: the baseline records no commit; it was written outside a repository")
    return notes


def _verdict(comparison: Comparison) -> str:
    if not comparison.regressed:
        return "no regression"
    return (
        f"regression: {len(comparison.new)} new, "
        f"{len(comparison.increased)} increased, "
        f"{len(comparison.stopped_measuring)} stopped measuring"
    )


def _human_bucket(title: str, rows: Iterable[str]) -> list[str]:
    lines = [f"{title}:"]
    body = [f"  {row}" for row in rows]
    return [*lines, *(body or ["  (none)"])]


def render_human(comparison: Comparison, baseline: Baseline, path: Path) -> list[str]:
    """The terminal report. Printed INSTEAD of the check table."""
    lines = [f"sense regression report vs {path} ({short_ref(baseline.base_ref)})"]
    lines.extend(_notes(comparison, baseline))
    lines.append("")
    lines.extend(_human_bucket("new", (f"{s}  {n}" for s, n in comparison.new.items())))
    lines.extend(
        _human_bucket(
            "increased",
            (f"{s}  {was} -> {now}" for s, (was, now) in comparison.increased.items()),
        )
    )
    lines.extend(
        _human_bucket(
            f"stopped measuring ({STOPPED_MEASURING_NOTE})",
            (f"{c}  {why}" for c, why in comparison.stopped_measuring.items()),
        )
    )
    lines.extend(_human_bucket("fixed", (f"{s}  {n}" for s, n in comparison.fixed.items())))
    lines.extend(
        _human_bucket(
            f"unmeasured ({UNMEASURED_NOTE})",
            (f"{s}  {n}" for s, n in comparison.unmeasured.items()),
        )
    )
    lines.append("")
    lines.append(_verdict(comparison))
    return lines


def cell(text: str) -> str:
    """One free-text value, safe to put between two pipes.

    Every other value in these tables is a signature or an integer. A REASON is
    neither: it is a check message or a `reason: detail` pair, and both carry
    subprocess and git stderr verbatim. Measured in round 2 of review on #357:
    a reason of "command_failed: git said\nfatal: bad | ref" ended the row
    mid-cell and shifted every later column of the posted comment.

    Newlines collapse rather than escape, because a markdown table row IS a
    line and there is no escape that keeps one; the pipe has one and it is
    used. Nothing else in GitHub-flavoured markdown can break a row.
    """
    return " ".join(text.split()).replace("|", "\\|")


def _markdown_table(title: str, header: str, rows: list[str]) -> list[str]:
    if not rows:
        return []
    columns = header.count("|") - 1
    return [
        "",
        f"**{title}**",
        "",
        header,
        "|" + "|".join([" --- "] * columns) + "|",
        *rows,
        "",
    ]


def render_markdown(
    comparison: Comparison,
    baseline: Baseline,
    path: Path,
    *,
    fail_on_regression: bool = False,
) -> str:
    """The pull-request comment. First line is :data:`MARKDOWN_MARKER`, exactly.

    A workflow finds its own earlier comment by that line and edits it in place,
    so nothing may precede it: not a blank line, not a heading.

    ``fail_on_regression`` is the mode actually in force, and the footer is
    rendered from it. It used to say "advisory: it never fails the job"
    unconditionally, while ``docs/dampener.md`` says adding the flag is the
    whole of graduating to blocking - so the comment sitting on a pull request
    that had just been failed by this report denied that it could.
    """
    lines = [
        MARKDOWN_MARKER,
        "## sense dampener",
        "",
        f"Baseline: `{path}` at `{short_ref(baseline.base_ref)}`.",
        "",
        f"**{_verdict(comparison)}**",
    ]
    for note in _notes(comparison, baseline):
        lines.extend(["", note])
    lines.extend(
        _markdown_table(
            "New signatures",
            "| signature | count |",
            [f"| `{s}` | {n} |" for s, n in comparison.new.items()],
        )
    )
    lines.extend(
        _markdown_table(
            "Increased",
            "| signature | baseline | now |",
            [f"| `{s}` | {was} | {now} |" for s, (was, now) in comparison.increased.items()],
        )
    )
    lines.extend(
        _markdown_table(
            f"Stopped measuring ({STOPPED_MEASURING_NOTE})",
            "| check | why |",
            [f"| `{c}` | {cell(why)} |" for c, why in comparison.stopped_measuring.items()],
        )
    )
    lines.extend(
        _markdown_table(
            "Fixed",
            "| signature | was |",
            [f"| `{s}` | {n} |" for s, n in comparison.fixed.items()],
        )
    )
    lines.extend(
        _markdown_table(
            f"Unmeasured ({UNMEASURED_NOTE})",
            "| signature | was |",
            [f"| `{s}` | {n} |" for s, n in comparison.unmeasured.items()],
        )
    )
    lines.append("")
    lines.append(BLOCKING_FOOTER if fail_on_regression else ADVISORY_FOOTER)
    return "\n".join(lines)


def comparison_document(
    comparison: Comparison,
    baseline: Baseline,
    current: Baseline,
    path: Path,
) -> dict[str, Any]:
    """The ``dampener`` block of ``ks sense --json``.

    One nested key rather than the six flat ones the issue sketched: a flat
    ``new`` or ``current`` at the top of the sense document could collide with a
    future check name, and this has room for ``unmeasured`` and the schema note
    without another round of top-level keys.
    """
    schema_changed: dict[str, int] | None = None
    if comparison.sense_schema_changed is not None:
        was, now = comparison.sense_schema_changed
        schema_changed = {"baseline": was, "current": now}
    project_changed: dict[str, str] | None = None
    if comparison.project_changed is not None:
        was_name, now_name = comparison.project_changed
        project_changed = {"baseline": was_name, "current": now_name}
    return {
        "baseline_path": str(path),
        "baseline": baseline.to_document(),
        "current": {
            "measured_checks": sorted(current.measured_checks),
            "unmeasured_checks": sorted(current.unmeasured_checks),
            "unmeasured_reasons": dict(sorted(current.unmeasured_reasons.items())),
            "signatures": dict(sorted(current.signatures.items())),
        },
        "new": dict(comparison.new),
        "increased": {
            signature: {"baseline": was, "current": now}
            for signature, (was, now) in comparison.increased.items()
        },
        "fixed": dict(comparison.fixed),
        "unmeasured": dict(comparison.unmeasured),
        "stopped_measuring": dict(comparison.stopped_measuring),
        "regressed": comparison.regressed,
        "sense_schema_changed": schema_changed,
        "project_changed": project_changed,
    }
