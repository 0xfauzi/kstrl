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
) -> AttemptReading:
    return AttemptReading(
        attempt=attempt,
        lines_changed=lines,
        files_changed=files,
        finding_keys=frozenset(keys),
    )


#: The measured #265 run: three failed reviews, a change that grew at
#: every step, and a reviewer that never once retired findings without
#: raising new ones.
_DIVERGING = [
    _reading(1, 612, ("a", "b", "c", "d", "e", "f")),
    _reading(2, 1408, ("g",)),
    _reading(3, 2907, ("g", "h", "i", "j")),
]


class TestPredicate:
    def test_diverging_component_trips(self) -> None:
        verdict = detect_divergence(_DIVERGING, DivergenceConfig())
        assert verdict is not None
        assert [r.attempt for r in verdict.readings] == [1, 2, 3]

    def test_converging_component_does_not_trip(self) -> None:
        """Growth is not the signal on its own. This component grows just
        as fast, but every attempt retires findings and raises none, so
        the growth bought something."""
        converging = [
            _reading(1, 612, ("a", "b", "c")),
            _reading(2, 1408, ("a", "b")),
            _reading(3, 2907, ("a",)),
        ]
        assert detect_divergence(converging, DivergenceConfig()) is None

    def test_one_clean_retirement_anywhere_in_the_window_resets(self) -> None:
        """A proper subset at the LAST step alone is enough to hold the
        detector off, even with the diverging shape before it."""
        readings = [
            _reading(1, 612, ("a", "b", "c")),
            _reading(2, 1408, ("d", "e")),
            _reading(3, 2907, ("d",)),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is None

    def test_identical_findings_while_growing_trips(self) -> None:
        """Equal sets are not a PROPER subset: the change grew and not one
        finding was retired, which is the worst case the predicate has."""
        readings = [
            _reading(1, 612, ("a", "b")),
            _reading(2, 1408, ("a", "b")),
            _reading(3, 2907, ("a", "b")),
        ]
        assert detect_divergence(readings, DivergenceConfig()) is not None

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
        """An early clean retirement does not immunise a component that
        starts diverging later."""
        readings = [
            _reading(1, 100, ("a", "b")),
            _reading(2, 300, ("a",)),
            _reading(3, 612, ("a", "b", "c")),
            _reading(4, 1408, ("d",)),
            _reading(5, 2907, ("d", "e")),
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

    @pytest.mark.parametrize("steps", [0, -1])
    def test_non_positive_growth_steps_never_trips(self, steps: int) -> None:
        assert detect_divergence(_DIVERGING, DivergenceConfig(growth_steps=steps)) is None

    def test_one_growth_step_trips_a_step_earlier(self) -> None:
        config = DivergenceConfig(growth_steps=1)
        verdict = detect_divergence(_DIVERGING[:2], config)
        assert verdict is not None
        assert [r.attempt for r in verdict.readings] == [1, 2]

    def test_three_growth_steps_needs_a_fourth_attempt(self) -> None:
        config = DivergenceConfig(growth_steps=3)
        assert detect_divergence(_DIVERGING, config) is None
        extended = [*_DIVERGING, _reading(4, 4001, ("g", "h", "i", "j", "k"))]
        assert detect_divergence(extended, config) is not None


class TestMessage:
    def test_message_names_every_number_it_saw(self) -> None:
        verdict = detect_divergence(_DIVERGING, DivergenceConfig())
        assert verdict is not None
        message = verdict.message
        for number in ("612", "1408", "2907"):
            assert number in message
        # Blocking-finding counts, in order.
        assert "6 -> 1 -> 4" in message
        assert "attempts 1, 2, 3" in message
        # And what to do about it.
        assert "Split this component" in message


class TestFindingKeys:
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
            {"criterion:us-1:crit-1", "concern:test_quality:tests/a.py"}
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
    def test_defaults(self) -> None:
        config = DivergenceConfig()
        assert config.enabled is True
        assert config.growth_steps == 2

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_DIVERGENCE_ENABLED", "0")
        monkeypatch.setenv("KSTRL_DIVERGENCE_GROWTH_STEPS", "4")
        config = DivergenceConfig.from_env()
        assert config.enabled is False
        assert config.growth_steps == 4

    def test_load_reads_the_toml_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[divergence]\nenabled = false\ngrowth_steps = 5\n",
            encoding="utf-8",
        )
        config = DivergenceConfig.load(tmp_path)
        assert config.enabled is False
        assert config.growth_steps == 5

    def test_env_beats_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[divergence]\nenabled = false\ngrowth_steps = 5\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("KSTRL_DIVERGENCE_ENABLED", "1")
        monkeypatch.setenv("KSTRL_DIVERGENCE_GROWTH_STEPS", "3")
        config = DivergenceConfig.load(tmp_path)
        assert config.enabled is True
        assert config.growth_steps == 3

    def test_load_falls_back_to_defaults_without_a_toml(self, tmp_path: Path) -> None:
        assert DivergenceConfig.load(tmp_path) == DivergenceConfig()
