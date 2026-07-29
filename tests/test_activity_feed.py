"""TUI surface A2: activity-feed lines for the decompose vocabulary."""

from __future__ import annotations

from kstrl import events as ev
from kstrl.tui import theme
from kstrl.tui.widgets.activity import humanize


class TestSpecIssueLines:
    def test_blocker_reads_as_error(self) -> None:
        line = humanize(ev.SpecIssueRecorded(
            severity="blocker", kind="ambiguity",
            summary="Spec contradicts itself", location="spec.md:12",
        ))
        assert line is not None
        text = line.plain
        assert "spec issue" in text
        assert "[blocker]" in text
        assert "Spec contradicts itself" in text
        assert "at spec.md:12" in text
        styles = " ".join(str(span.style) for span in line.spans)
        assert theme.ERROR in styles

    def test_minor_reads_as_warning_not_error(self) -> None:
        line = humanize(ev.SpecIssueRecorded(severity="minor", summary="nit"))
        assert line is not None
        assert "[minor]" in line.plain
        styles = " ".join(str(span.style) for span in line.spans)
        assert theme.WARNING in styles
        assert theme.ERROR not in styles

    def test_long_summary_truncated(self) -> None:
        line = humanize(ev.SpecIssueRecorded(severity="major",
                                             summary="x" * 200))
        assert line is not None
        assert "x" * 200 not in line.plain

    def test_unknown_severity_is_named(self) -> None:
        line = humanize(ev.SpecIssueRecorded(severity="future", summary="s"))
        assert line is not None
        assert "[unknown]" in line.plain


class TestArtifactLines:
    def test_label_and_path(self) -> None:
        line = humanize(ev.ArtifactWritten(
            label="manifest", path="scripts/kstrl/manifest.json",
        ))
        assert line is not None
        assert "manifest written" in line.plain
        assert "scripts/kstrl/manifest.json" in line.plain

    def test_pathless_artifact_still_renders(self) -> None:
        line = humanize(ev.ArtifactWritten(label="spec_issues"))
        assert line is not None
        assert "spec_issues written" in line.plain

    def test_unlabelled_artifact_has_readable_fallback(self) -> None:
        line = humanize(ev.ArtifactWritten())
        assert line is not None
        assert "artifact written" in line.plain


class TestCurationControl:
    def test_heartbeats_stay_out_of_the_feed(self) -> None:
        assert humanize(ev.WorkerHeartbeat(pid=1, elapsed_seconds=1.0)) is None


class TestBudgetCoverageLine:
    """R8 review finding 1: the feed dropped the one event that says a
    ceiling stopped covering the run."""

    def _event(self) -> ev.BudgetCoverage:
        return ev.BudgetCoverage(
            ceiling="max_cost_usd", axis="cost", calls=13, covered_calls=8,
            uncovered_calls=5, uncovered_tokens=193_633,
            uncovered_roles=("review",),
            detail="cost coverage is PARTIAL: 8 of 13 metered call(s) "
                   "reported a cost",
        )

    def test_the_gap_is_narrated(self) -> None:
        line = humanize(self._event())
        assert line is not None
        assert "max_cost_usd" in line.plain
        assert "8 of 13" in line.plain
        assert "review" in line.plain

    def test_the_uncovered_magnitude_stays_in_tokens(self) -> None:
        """Standing constraint: no price is ever inferred for uncovered
        calls, on any surface."""
        line = humanize(self._event())
        assert line is not None
        assert "193,633" in line.plain
        assert "$" not in line.plain

    def test_a_ceilingless_gap_still_names_its_axis(self) -> None:
        line = humanize(ev.BudgetCoverage(
            axis="token", calls=2, covered_calls=0, uncovered_calls=2,
        ))
        assert line is not None
        assert "token" in line.plain
