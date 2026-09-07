"""#247: the record of which phases produced a reading, at the layer the
rule lives in.

``tests/test_context.py`` holds the one case the issue names. This file
holds the cases that decide whether the record is SAFE: that a stale
record retires nothing, that a crashed sensor still beats it, that it
survives the process boundary it is built to cross, and that a context
written before it existed reads back as the old rule rather than as
nonsense. It is a separate file because ``tests/test_context.py`` is at
687 lines against an 800-line ratchet.
"""

from __future__ import annotations

import json

import pytest

from kstrl.context import (
    PHASE_RANK,
    SKIPPABLE_PHASES,
    IterationContext,
    IterationRecord,
    PhaseReading,
)
from tests.test_context import (
    NOT_REMEASURED,
    RESOLVED,
    build_sweep_context,
    section,
    sweep_sequences,
)


class TestReadingsRetireOnlyWhatTheyMeasured:
    def test_a_reading_from_an_older_attempt_retires_nothing(self) -> None:
        """The record is attempt-scoped, which is what makes carrying it
        forward safe.

        ``_merge_phase_readings`` in the pipeline merges every reading it
        holds into the context at whichever gate fails, and the context
        then travels to the next attempt with all of them. A reading
        from attempt 1 must not retire anything when attempt 2 is the
        latest evidence: the reviewer's attempt-1 verdict is exactly the
        finding under discussion, not a re-measurement of it.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding("sql injection", attempt=2, phase="security")
        ctx.add_phase_reading("review", attempt=1)

        text = ctx.format_for_prompt()
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_a_reading_never_beats_the_crashed_sensor_rule(self) -> None:
        """Branch ORDER in ``_buckets``, pinned.

        Attempt 2's review entry is an infrastructure failure, so the
        ``rank == q`` branch refuses to retire attempt 1's finding. That
        branch sits above the skippable branch, so it decides first even
        with a reading recorded for review at attempt 2. Reordering the
        two would let a crashed reviewer retire a live finding by way of
        a record that should never have been written for it in the first
        place, and this is the assertion that fails if someone does.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding(
            "reviewer crashed",
            attempt=2,
            phase="review",
            infrastructure=True,
        )
        ctx.add_phase_reading("review", attempt=2)

        text = ctx.format_for_prompt()
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_a_reading_retires_a_finding_when_the_attempt_recorded_no_entry(
        self,
    ) -> None:
        """The merge-conflict restart, which records no entry.

        Attempt 2 reaches the ``pr`` phase, so review and security both
        ran and passed and both recorded a reading; the PR then hits a
        merge conflict, ``_retry_after_merge_conflict`` adds an
        ``IterationRecord`` and no ``FailureEntry``, and
        ``retry_or_fail`` merges the readings in. ``Q`` is then -1, so
        no inference retires anything - and until the readings branch
        moved above the rank comparison the records were there and were
        discarded, and attempt 3 was told to re-check a criterion
        attempt 2 had cleared.

        The engineer-loop failure is the same shape with the opposite
        answer, and the second half asserts it: no sensor ran, so there
        is no reading and the finding is still shown.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_iteration(
            IterationRecord(
                iteration=3,
                success=False,
                attempt=2,
                error="merge conflict with the base branch",
            )
        )
        ctx.add_phase_reading("review", attempt=2)
        ctx.add_phase_reading("security", attempt=2)

        text = ctx.format_for_prompt()
        assert "criterion X" not in text
        assert "from review passed or were re-measured in attempt 2" in section(text, RESOLVED)
        assert section(text, NOT_REMEASURED) == ""

        no_sensor_ran = IterationContext()
        no_sensor_ran.add_review_finding("criterion X", attempt=1, phase="review")
        no_sensor_ran.add_iteration(
            IterationRecord(
                iteration=3,
                success=False,
                attempt=2,
                error="engineer loop gave up",
            )
        )

        text = no_sensor_ran.format_for_prompt()
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_a_reading_for_an_unknown_phase_is_rejected(self) -> None:
        """Same vocabulary and the same refusal as ``_add``: the only
        strings the record holds are phase names from ``PHASE_RANK``."""
        ctx = IterationContext()
        with pytest.raises(ValueError) as exc:
            ctx.add_phase_reading("distill", attempt=1)
        assert "unknown phase 'distill'" in str(exc.value)

    def test_recording_the_same_reading_twice_is_one_reading(self) -> None:
        """The merge runs once per failing gate and the record is
        carried forward, so a repeated pair arrives by design."""
        ctx = IterationContext()
        ctx.add_phase_reading("review", attempt=2)
        ctx.add_phase_reading("review", attempt=2)
        assert ctx.readings == {PhaseReading(attempt=2, phase="review")}


class TestReadingsCrossTheProcessBoundary:
    def test_readings_survive_the_json_round_trip(self) -> None:
        """The real transport. ``component_contexts`` holds a STRING
        that crosses the ProcessPoolExecutor boundary and is re-parsed in
        the worker, and every writer of it serialises through
        ``to_json``. A record that does not serialise is a record that
        does not exist.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding("sql injection", attempt=2, phase="security")
        ctx.add_phase_reading("review", attempt=2)

        back = IterationContext.from_json(ctx.to_json())
        assert back.readings == {PhaseReading(attempt=2, phase="review")}
        assert back.format_for_prompt() == ctx.format_for_prompt()
        assert "criterion X" not in back.format_for_prompt()

    def test_a_reading_naming_an_unknown_phase_is_refused_on_read(self) -> None:
        """The read path checks the same vocabulary as the write path.

        ``_require_known_phase`` says it is the one definition of a
        phase name this object accepts, and an entry is retired when a
        reading names its phase, so a deserialiser that accepted a name
        ``add_phase_reading`` refuses would be the second, weaker
        definition of that join. The direction the leniency failed in
        was safe - an unknown phase can only fail to subtract from
        ``SKIPPABLE_PHASES``, so it under-retires - which is why this is
        about the invariant the helper states rather than about a live
        drop.
        """
        payload = json.dumps(
            {
                "records": [],
                "entries": [],
                "readings": [{"attempt": 1, "phase": "distill"}],
            }
        )

        with pytest.raises(ValueError) as exc:
            IterationContext.from_json(payload)
        assert "unknown phase 'distill'" in str(exc.value)

    def test_the_serialised_readings_are_in_a_stable_order(self) -> None:
        """``to_json`` sorts, and this is what fails if it stops.

        The source is a set, so its iteration order is a function of the
        hash seed and differs between parent processes. The string is
        what crosses the ProcessPoolExecutor boundary, so an unsorted
        list makes the same context serialise differently run to run,
        and any later comparison of two contexts by their bytes is then
        wrong for a reason nobody would look for.

        The residual, stated: with the sort deleted the emitted order is
        the set's, which agrees with this assertion only if the seed
        happens to produce it. Six readings makes that one arrangement
        in 720, and the mutation was planted to confirm it goes red.
        """
        ctx = IterationContext()
        for attempt in (3, 1, 2):
            for phase in ("security", "review"):
                ctx.add_phase_reading(phase, attempt=attempt)

        serialised = json.loads(ctx.to_json())["readings"]
        assert serialised == [
            {"attempt": 1, "phase": "review"},
            {"attempt": 1, "phase": "security"},
            {"attempt": 2, "phase": "review"},
            {"attempt": 2, "phase": "security"},
            {"attempt": 3, "phase": "review"},
            {"attempt": 3, "phase": "security"},
        ]

    def test_a_context_written_before_readings_existed_reads_back_clean(
        self,
    ) -> None:
        """An absent key is no readings, which is the pre-#247 rule.

        Both older shapes are covered: the current entries shape written
        by a parent process that predates this field, and the pre-R10.2
        three-list shape. Degrading to showing the finding is the safe
        direction; degrading to dropping it is the failure this default
        is chosen to avoid.
        """
        current_shape = json.dumps(
            {
                "records": [],
                "entries": [
                    {
                        "attempt": 1,
                        "phase": "review",
                        "text": "criterion X",
                        "infrastructure": False,
                    },
                    {
                        "attempt": 2,
                        "phase": "security",
                        "text": "sql injection",
                        "infrastructure": False,
                    },
                ],
            }
        )
        ctx = IterationContext.from_json(current_shape)
        assert ctx.readings == set()
        assert "criterion X" in section(ctx.format_for_prompt(), NOT_REMEASURED)

        legacy_shape = json.dumps(
            {
                "records": [],
                "review_findings": ["criterion X"],
                "verification_failures": [],
                "contract_failures": [],
            }
        )
        legacy = IterationContext.from_json(legacy_shape)
        assert legacy.readings == set()
        assert "criterion X" in section(legacy.format_for_prompt(), NOT_REMEASURED)


class TestBucketRuleSweepWithReadings:
    """The same 5600-case product as ``TestBucketRuleSweep``, run with a
    reading recorded for every skippable phase at attempt ``n``.

    ``TestBucketRuleSweep`` is the control: it records nothing, so it
    proves the rule is byte-identical when the record is empty. This is
    the other half, asserting the amended rule over the whole space
    rather than at the one case the issue names.
    """

    def build(self, sequence: tuple[str, ...], legacy: bool) -> IterationContext:
        """The control's corpus, plus a reading for every skippable
        phase at the latest attempt. Sharing the builder is what keeps
        the two sweeps over the same entries."""
        ctx = build_sweep_context(sequence, legacy)
        for phase in sorted(SKIPPABLE_PHASES):
            ctx.add_phase_reading(phase, attempt=len(sequence))
        return ctx

    def check_one_partition(self, sequence: tuple[str, ...], legacy: bool) -> None:
        ctx = self.build(sequence, legacy)
        b = ctx._buckets()
        total = len(b.current) + len(b.not_remeasured) + len(b.resolved)
        assert total == len(ctx.entries)

        n = len(sequence)
        q = PHASE_RANK[sequence[-1]]
        assert [e.attempt for e in b.current] == [n]
        for e in b.not_remeasured:
            # Every skippable phase has a reading at n, so no dated
            # skippable entry survives here at any rank: the readings
            # branch retires them by observation, including the ones
            # ranked ABOVE q that no inference could reach. The dated
            # survivors are the other phases ranked above q, which
            # nothing in attempt n ran past. The crashed-sensor rule
            # holds nothing back here: this sweep records no
            # infrastructure entries.
            assert e.attempt == 0 or (
                e.attempt < n and PHASE_RANK[e.phase] > q and e.phase not in SKIPPABLE_PHASES
            )
        for e in b.resolved:
            assert 0 < e.attempt < n
            # At or below q is the inference; above it is only reachable
            # for a skippable phase, and only because one was observed.
            assert PHASE_RANK[e.phase] <= q or e.phase in SKIPPABLE_PHASES

    def test_every_entry_lands_in_exactly_one_bucket(self) -> None:
        cases = 0
        for sequence in sweep_sequences():
            for legacy in (False, True):
                self.check_one_partition(sequence, legacy)
                cases += 1
        assert cases == 5600

    def check_one_difference(self, sequence: tuple[str, ...]) -> int:
        """How many entries the record moved into ``resolved`` for one
        sequence, having checked that each is one it may move."""
        with_readings = self.build(sequence, legacy=False)
        without = IterationContext(
            records=list(with_readings.records),
            entries=list(with_readings.entries),
        )
        q = PHASE_RANK[sequence[-1]]
        before = {e.text for e in without._buckets().resolved}
        after = {e.text for e in with_readings._buckets().resolved}
        assert before <= after
        for text in after - before:
            entry = next(e for e in with_readings.entries if e.text == text)
            assert entry.phase in SKIPPABLE_PHASES
            # Not AT q: an entry at the failing gate's own rank is
            # decided by the branch above the readings one, so a record
            # can never move it. That is the crashed-sensor rule, and
            # this inequality is what fails if the two branches are
            # reordered.
            assert PHASE_RANK[entry.phase] != q
            assert entry.attempt < len(sequence)
        return len(after - before)

    def test_a_reading_moves_exactly_the_entries_it_should(self) -> None:
        """Diff the two sweeps rather than restating either.

        The entries that change bucket between ``TestBucketRuleSweep``'s
        contexts and these are exactly the skippable-phase entries whose
        rank is not ``q``. Asserting the difference is what makes this a
        statement about the record's effect rather than a second copy of
        the rule.

        The total is PINNED rather than asserted positive. ``moved > 0``
        passes on one moved entry out of 5600 sequences, which is the
        same shape as a record that has almost stopped working; round 1
        of review measured it at 810 and it is 1944 now that a reading
        also retires a skippable entry ranked above ``q``. A change in
        either direction is a change in what the record retires, and it
        should be read before the number is edited.
        """
        moved = sum(self.check_one_difference(seq) for seq in sweep_sequences())
        assert moved == 1944
