"""#265: the across-attempt divergence predicate, in isolation.

The predicate is the whole load-bearing part of the detector, so it is
tested without the pipeline: readings in, verdict out. The cases mirror
the reasoning in ``kstrl/divergence.py`` one for one, including the four
fail-open paths (too few readings, a gap in the attempts, a step that did
not grow, a step whose findings were a proper subset).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.divergence import (
    AttemptReading,
    DivergenceConfig,
    detect_divergence,
    review_finding_keys,
)
from kstrl.review import CriterionReview, ReviewConcern, ReviewResult


def _reading(
    attempt: int,
    lines: int,
    keys: tuple[str, ...],
    files: int = 3,
    blocking: int | None = None,
) -> AttemptReading:
    return AttemptReading(
        attempt=attempt,
        lines_changed=lines,
        files_changed=files,
        finding_keys=frozenset(keys),
        blocking_count=len(keys) if blocking is None else blocking,
    )


#: The shape the detector is for: the change gets larger at every step
#: and not one blocking finding is ever retired.
_DIVERGING = [
    _reading(1, 612, ("a", "b")),
    _reading(2, 1408, ("a", "b", "c")),
    _reading(3, 2907, ("a", "b", "c", "d")),
]


class TestPredicate:
    def test_diverging_component_trips(self) -> None:
        verdict = detect_divergence(_DIVERGING, DivergenceConfig())
        assert verdict is not None
        assert [r.attempt for r in verdict.readings] == [1, 2, 3]

    def test_converging_component_does_not_trip(self) -> None:
        """Growth is not the signal on its own. This component grows just
        as fast, but every attempt retires findings, so the growth bought
        something."""
        converging = [
            _reading(1, 612, ("a", "b", "c")),
            _reading(2, 1408, ("a", "b")),
            _reading(3, 2907, ("a",)),
        ]
        assert detect_divergence(converging, DivergenceConfig()) is None

    def test_retire_one_raise_one_is_convergence(self) -> None:
        """The trajectory a proper-subset reset would have condemned, and
        the reason the reset is a plain set difference instead.

        Retire A, keep B, draw C; retire B, keep C, draw D. One objection
        answered every attempt, the count never rises, and the change
        grows because answering a finding means writing code. A subset
        test never fires here and the component would be failed with
        retries left."""
        readings = [
            _reading(1, 612, ("a", "b")),
            _reading(2, 1408, ("b", "c")),
            _reading(3, 2907, ("c", "d")),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None

    def test_one_retirement_anywhere_in_the_window_resets(self) -> None:
        """A single retirement at the LAST step alone holds the detector
        off, even with the diverging shape before it."""
        readings = [
            _reading(1, 612, ("a", "b")),
            _reading(2, 1408, ("a", "b", "c")),
            _reading(3, 2907, ("b", "c", "d", "e")),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None

    def test_identical_findings_while_growing_trips(self) -> None:
        """Nothing retired and the change larger every step: the worst
        case the predicate has."""
        readings = [
            _reading(1, 612, ("a", "b")),
            _reading(2, 1408, ("a", "b")),
            _reading(3, 2907, ("a", "b")),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is not None

    def test_key_instability_can_only_produce_misses(self) -> None:
        """The saving property of the weak reset, asserted rather than
        claimed: a key that changes for cosmetic reasons makes an old key
        vanish, which reads as a retirement and resets the streak. There
        is no arrangement of unstable keys that manufactures a trip."""
        readings = [
            _reading(1, 612, ("a", "b")),
            _reading(2, 1408, ("a", "b-reworded")),
            _reading(3, 2907, ("a", "b-reworded-again")),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None

    def test_first_attempt_has_nothing_to_compare(self) -> None:
        assert detect_divergence([], DivergenceConfig()) is None
        assert detect_divergence(_DIVERGING[:1], DivergenceConfig()) is None

    def test_two_readings_are_one_step_short_of_the_default_window(self) -> None:
        assert detect_divergence(_DIVERGING[:2], DivergenceConfig()) is None

    def test_change_that_shrank_does_not_trip(self) -> None:
        readings = [
            _reading(1, 612, ("a",)),
            _reading(2, 2907, ("b",)),
            _reading(3, 1408, ("c",)),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None

    def test_change_that_stayed_the_same_size_does_not_trip(self) -> None:
        """Strictly greater, so a plateau is not growth."""
        readings = [
            _reading(1, 612, ("a",)),
            _reading(2, 1408, ("b",)),
            _reading(3, 1408, ("c",)),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None

    def test_gap_in_the_attempt_numbers_does_not_trip(self) -> None:
        """Attempt 3 left no reading (its reviewer crashed, or its size
        could not be measured), so 2 and 4 are not consecutive and the
        streak is broken."""
        readings = [
            _reading(1, 612, ("a",)),
            _reading(2, 1408, ("b",)),
            _reading(4, 2907, ("c",)),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None

    def test_only_the_window_tail_is_judged(self) -> None:
        """An early retirement does not immunise a component that starts
        diverging later."""
        readings = [
            _reading(1, 100, ("a", "b")),
            _reading(2, 300, ("a",)),
            _reading(3, 612, ("x",)),
            _reading(4, 1408, ("x", "y")),
            _reading(5, 2907, ("x", "y", "z")),
        ]
        verdict = detect_divergence(readings, DivergenceConfig())
        assert verdict is not None
        assert [r.attempt for r in verdict.readings] == [3, 4, 5]

    def test_unkeyable_verdict_does_not_trip(self) -> None:
        """A failed review whose blocking findings could not be keyed is
        not a verdict this predicate can compare."""
        readings = [
            _reading(1, 612, ("a",)),
            _reading(2, 1408, ()),
            _reading(3, 2907, ("c",)),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None

    def test_one_growth_step_trips_a_step_earlier(self) -> None:
        config = DivergenceConfig(growth_steps=1)
        verdict = detect_divergence(_DIVERGING[:2], config)
        assert verdict is not None
        assert [r.attempt for r in verdict.readings] == [1, 2]

    def test_three_growth_steps_needs_a_fourth_attempt(self) -> None:
        config = DivergenceConfig(growth_steps=3)
        assert detect_divergence(_DIVERGING, config) is None
        extended = [*_DIVERGING, _reading(4, 4001, ("a", "b", "c", "d", "e"))]
        assert detect_divergence(extended, config) is not None


class TestTheMotivatingRun:
    """The claim the module docstring, `docs/env-vars.md` and the runbook
    all make about #265, enforced here so it cannot rot into a boast."""

    def test_a_step_that_retires_anything_can_never_contribute(self) -> None:
        """The structural reason, checked exhaustively by construction
        rather than by sampling trajectories: a streak step requires
        ``previous <= current``, and a larger set is never a subset of a
        smaller one. So ANY step where the blocking-finding count drops
        resets the streak, whatever the identities are."""
        for previous_size in range(1, 8):
            for current_size in range(1, previous_size):
                previous = frozenset(f"k{i}" for i in range(previous_size))
                current = frozenset(f"k{i}" for i in range(current_size))
                assert not previous <= current

    def test_the_265_trajectory_does_not_trip(self) -> None:
        """6 blocking findings, then 1, then 10. Attempt 2 retired at
        least five of attempt 1's six however they were keyed, so the
        streak resets there and one bad step afterwards is not two.

        The detector is deliberately narrower than the run that
        motivated it, and that is the price of not condemning
        ``test_retire_one_raise_one_is_convergence`` above. It is also
        the single strongest reason the gate ships advisory."""
        readings = [
            _reading(1, 914, tuple(f"k{i}" for i in range(6))),
            _reading(2, 1600, ("k0",)),
            _reading(3, 2221, tuple(f"k{i}" for i in range(10))),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None
        # Not even at the most aggressive setting, for the 6 -> 1 step.
        assert detect_divergence(readings[:2], DivergenceConfig(growth_steps=1)) is None


class TestMessage:
    def test_message_names_every_number_it_saw(self) -> None:
        verdict = detect_divergence(_DIVERGING, DivergenceConfig())
        assert verdict is not None
        message = verdict.message
        for number in ("612", "1408", "2907"):
            assert number in message
        assert "attempts 1, 2, 3" in message
        assert "added plus removed" in message
        # And what to do about it.
        assert "Split this component" in message

    def test_message_reports_the_reviewer_count_not_the_key_count(self) -> None:
        """Keys deduplicate and the reviewer's fail_count does not. The
        operator reads this directly under "Phase 2 FAILED: N failures",
        and a dashboard joins it against ReviewResultEvent.fail_count."""
        readings = [
            _reading(1, 612, ("a", "b"), blocking=6),
            _reading(2, 1408, ("a", "b", "c"), blocking=1),
            _reading(3, 2907, ("a", "b", "c", "d"), blocking=10),
        ]
        verdict = detect_divergence(readings, DivergenceConfig())
        assert verdict is not None
        assert "6 -> 1 -> 10 blocking findings" in verdict.message


class TestFindingKeys:
    def test_two_findings_in_one_file_keep_distinct_keys(self) -> None:
        """Retiring two findings in one file must read as a retirement.
        See ``review_finding_keys`` for why the explanation is in the
        key: without it both collapse onto one, and the sets come out
        EQUAL rather than smaller."""
        before = ReviewResult(
            passed=False,
            mode="hard",
            concerns=[
                ReviewConcern("test_quality", "fail", "tests/test_document.py:10", "no oracle"),
                ReviewConcern("test_quality", "fail", "tests/test_document.py:50", "tautology"),
            ],
        )
        after = ReviewResult(
            passed=False,
            mode="hard",
            concerns=[
                ReviewConcern("test_quality", "fail", "tests/test_document.py:800", "swallowed"),
            ],
        )
        previous, current = review_finding_keys(before), review_finding_keys(after)
        assert len(previous) == 2
        assert previous - current, "both findings were retired and must read as retired"
        assert (
            detect_divergence(
                [
                    _reading(1, 612, tuple(previous), blocking=2),
                    _reading(2, 1408, tuple(current), blocking=1),
                ],
                DivergenceConfig(growth_steps=1),
            )
            is None
        )

    def test_only_blocking_findings_are_keyed(self) -> None:
        """An advisory did not fail the component, so it cannot decide
        whether the component got better."""
        result = ReviewResult(
            passed=False,
            mode="hard",
            criteria=[
                CriterionReview("crit-1", "fail", "no", story_id="US-1"),
                CriterionReview("crit-2", "advisory", "meh", story_id="US-1"),
                CriterionReview("crit-3", "pass", "fine", story_id="US-1"),
            ],
            concerns=[
                ReviewConcern("test_quality", "fail", "tests/a.py:12", "weak"),
                ReviewConcern("dead_code", "advisory", "src/b.py:3", "unused"),
            ],
        )
        assert review_finding_keys(result) == frozenset(
            {"criterion:us-1:crit-1", "concern:test_quality:tests/a.py:weak"}
        )

    def test_a_moved_line_is_the_same_finding(self) -> None:
        """The whole point of stripping the line number: a change that
        grows shifts every location, and keeping them would make every
        finding look new on every attempt."""
        before = ReviewResult(
            passed=False,
            mode="hard",
            concerns=[ReviewConcern("test_quality", "fail", "tests/a.py:120", "weak")],
        )
        after = ReviewResult(
            passed=False,
            mode="hard",
            concerns=[ReviewConcern("test_quality", "fail", "tests/a.py:1840", "weak")],
        )
        assert review_finding_keys(before) == review_finding_keys(after)

    def test_a_reworded_explanation_is_the_same_finding(self) -> None:
        before = ReviewResult(
            passed=False,
            mode="hard",
            criteria=[
                CriterionReview("Handles empty input", "fail", "it does not", story_id="US-1")
            ],
        )
        after = ReviewResult(
            passed=False,
            mode="hard",
            criteria=[
                CriterionReview(
                    "  handles   empty  input ",
                    "fail",
                    "still not handled, see line 40",
                    story_id="us-1",
                )
            ],
        )
        assert review_finding_keys(before) == review_finding_keys(after)

    def test_different_files_are_different_findings(self) -> None:
        result = ReviewResult(
            passed=False,
            mode="hard",
            concerns=[
                ReviewConcern("test_quality", "fail", "tests/a.py:1", "weak"),
                ReviewConcern("test_quality", "fail", "tests/b.py:1", "weak"),
            ],
        )
        assert len(review_finding_keys(result)) == 2

    def test_a_passing_review_keys_nothing(self) -> None:
        assert review_finding_keys(ReviewResult(passed=True, mode="hard")) == frozenset()


class TestConfig:
    def test_defaults_are_advisory(self) -> None:
        config = DivergenceConfig()
        assert config.mode == "advisory"
        assert config.growth_steps == 2
        assert config.measures is True
        assert config.blocks is False

    @pytest.mark.parametrize(
        ("mode", "measures", "blocks"),
        [("skip", False, False), ("advisory", True, False), ("block", True, True)],
    )
    def test_mode_drives_both_switches(self, mode: str, measures: bool, blocks: bool) -> None:
        config = DivergenceConfig(mode=mode)
        assert config.measures is measures
        assert config.blocks is blocks

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_DIVERGENCE_MODE", "block")
        monkeypatch.setenv("KSTRL_DIVERGENCE_GROWTH_STEPS", "4")
        config = DivergenceConfig.from_env()
        assert config.mode == "block"
        assert config.growth_steps == 4

    def test_load_reads_the_toml_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[divergence]\nmode = "block"\ngrowth_steps = 5\n',
            encoding="utf-8",
        )
        config = DivergenceConfig.load(tmp_path)
        assert config.mode == "block"
        assert config.growth_steps == 5

    def test_env_beats_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[divergence]\nmode = "block"\ngrowth_steps = 5\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("KSTRL_DIVERGENCE_MODE", "skip")
        monkeypatch.setenv("KSTRL_DIVERGENCE_GROWTH_STEPS", "3")
        config = DivergenceConfig.load(tmp_path)
        assert config.mode == "skip"
        assert config.growth_steps == 3

    def test_load_falls_back_to_defaults_without_a_toml(self, tmp_path: Path) -> None:
        assert DivergenceConfig.load(tmp_path) == DivergenceConfig()

    @pytest.mark.parametrize("steps", [0, -1])
    def test_non_positive_growth_steps_is_rejected_not_silently_off(self, steps: int) -> None:
        """A reader could reasonably set 0 meaning "most aggressive" and
        get a dead gate. mode = "skip" is the way to turn it off."""
        with pytest.raises(ValueError, match="growth_steps"):
            DivergenceConfig(growth_steps=steps)

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            DivergenceConfig(mode="blocking")
