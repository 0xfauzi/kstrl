"""#217: every recurring pattern is routed to the inbox, the candidate
lessons or neither, and ``ks evolve`` prints each bucket.

``TestRoutingIsClosedOverTheTable`` is the routing guard, derived from
``_CATEGORY_BY_CHECK`` rather than hand-written.
``TestEvolveRoutesMechanicalFailuresAway``,
``TestEvolvePrintsReadinessNumbers`` and the six module-level #507 tests
at the end drive ``ks evolve`` end to end. #507 (Slice 1 of #217) deleted
the proposal generator, so nothing here expects a file to be written.

Two rules for every test here: never import a helper from another
``tests/test_*.py`` module (build journals with ``_write_journal``
below), and every ``CliRunner`` assertion carries ``result.exception``
in its message, because ``CliRunner.invoke`` swallows an exception into
that attribute and truncates stdout.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.evolution import (
    _CATEGORY_BY_CHECK,
    FINDINGS_SUPERSEDED_EVENT,
    INFRASTRUCTURE_CHECKS,
    EvolutionConfig,
    EvolutionJournal,
    FailurePattern,
    category_for_check,
    route_patterns,
)


def _write_journal(root: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (run_id, component_id, signature). One JSONL line each.

    The shape record_run writes.
    """
    entries = [
        {
            "schema_version": 2,
            "run_id": run_id,
            "component_id": component_id,
            "event_type": "component_result",
            "status": "failed",
            "error": "failed",
            "failure_signatures": [signature],
            "findings_summary": {"total": 0, "by_category": {}},
        }
        for run_id, component_id, signature in rows
    ]
    path = root / ".kstrl" / "evolution.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _invoke(root: Path) -> Result:
    """`ks evolve` on one project root."""
    return CliRunner().invoke(cli, ["evolve", "--root", str(root), "--ui", "plain", "--no-color"])


#: The categories a recurring failure can teach a lesson in, written out
#: here rather than imported from kstrl.evolution, so a change to the
#: router's own set is a red test and not a silent agreement (#507, P1.1).
_LEARNABLE_CATEGORIES = frozenset({"verification", "review", "security", "contract"})

_LESSONS_HEADING = "Candidate lessons (no writer until the playbook ships)"
_MECHANICAL_HEADING = "Routed to the inbox (mechanical, not a lesson)"
_UNROUTED_HEADING = "Not routed (not a learnable category)"


def _sections(output: str) -> dict[str, str]:
    """Plain-UI output split at its ``== heading ==`` lines."""
    sections: dict[str, list[str]] = {"": []}
    current = ""
    for line in output.splitlines():
        if line.startswith("== ") and line.endswith(" =="):
            current = line[3:-3]
            sections[current] = []
        else:
            sections[current].append(line)
    return {heading: "\n".join(lines) for heading, lines in sections.items()}


class TestRoutingIsClosedOverTheTable:
    """The routing guard, derived from the table rather than hand-written."""

    def test_every_enrolled_name_lands_in_exactly_one_bucket(self) -> None:
        patterns = [
            FailurePattern(
                description="d",
                frequency=2,
                total_components=2,
                affected_components=["comp-a"],
                check_name=name,
                error_signature="SIG",
                category="verification",
            )
            for name in _CATEGORY_BY_CHECK
        ]
        routing = route_patterns(patterns)
        assert len(routing.mechanical) + len(routing.lessons) + len(routing.unrouted) == len(
            _CATEGORY_BY_CHECK
        )
        mechanical_names = {p.check_name for p in routing.mechanical}
        lessons_names = {p.check_name for p in routing.lessons}
        unrouted_names = {p.check_name for p in routing.unrouted}
        assert mechanical_names.isdisjoint(lessons_names)
        assert mechanical_names.isdisjoint(unrouted_names)
        assert lessons_names.isdisjoint(unrouted_names)
        assert mechanical_names == set(INFRASTRUCTURE_CHECKS)
        assert unrouted_names == {
            name for name, category in _CATEGORY_BY_CHECK.items() if category == "iteration"
        }
        assert (len(mechanical_names), len(lessons_names), len(unrouted_names)) == (6, 21, 2)

    def test_the_router_recomputes_the_category_and_ignores_a_stamped_one(self) -> None:
        mismatched_infra = FailurePattern(
            description="d",
            frequency=2,
            total_components=2,
            affected_components=["comp-a"],
            check_name="pr",
            error_signature="SIG",
            category="review",
        )
        routing = route_patterns([mismatched_infra])
        assert routing.mechanical == (mismatched_infra,)
        assert routing.lessons == ()
        assert routing.unrouted == ()

        mismatched_lesson = FailurePattern(
            description="d",
            frequency=2,
            total_components=2,
            affected_components=["comp-a"],
            check_name="review",
            error_signature="SIG",
            category="infrastructure",
        )
        routing = route_patterns([mismatched_lesson])
        assert routing.lessons == (mismatched_lesson,)
        assert routing.mechanical == ()
        assert routing.unrouted == ()

    def test_an_unenrolled_name_is_unrouted_and_never_a_lesson(self) -> None:
        assert category_for_check("zzz-never-enrolled") == "unenrolled"
        pattern = FailurePattern(
            description="d",
            frequency=2,
            total_components=2,
            affected_components=["comp-a"],
            check_name="zzz-never-enrolled",
            error_signature="SIG",
            category="iteration",
        )
        routing = route_patterns([pattern])
        assert routing.unrouted == (pattern,)
        assert routing.lessons == ()
        assert routing.mechanical == ()


class TestEvolveRoutesMechanicalFailuresAway:
    def test_infrastructure_signatures_are_never_lessons(self, tmp_path: Path) -> None:
        rows = [
            ("r1", "comp-push", "pr:push-of-x-failed-to-https"),
            ("r1", "comp-review", "review:prd_criterion"),
            ("r1", "comp-patterns", "bad_patterns:hardcoded-secret"),
            ("r2", "comp-push", "pr:push-of-x-failed-to-https"),
            ("r2", "comp-review", "review:prd_criterion"),
            ("r2", "comp-patterns", "bad_patterns:hardcoded-secret"),
        ]
        _write_journal(tmp_path, rows)
        result = _invoke(tmp_path)

        assert result.exit_code == 0, (result.output, result.exception)
        sections = _sections(result.output)
        lessons = sections[_LESSONS_HEADING]
        assert "[review] prd_criterion" in lessons, result.output
        assert "[bad_patterns] hardcoded-secret" in lessons, result.output
        assert "push-of-x-failed-to-https" not in lessons, result.output
        mechanical = sections[_MECHANICAL_HEADING]
        assert "[pr] push-of-x-failed-to-https" in mechanical, result.output
        assert "ks inbox" in mechanical, result.output
        assert _UNROUTED_HEADING not in sections, result.output
        assert not (tmp_path / ".kstrl" / "proposals").exists()


class TestEvolvePrintsReadinessNumbers:
    def test_the_readiness_numbers_print_when_nothing_recurs(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, [("r1", "comp-a", "linter:S608")])
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert "recurring signatures" in result.output
        assert "fact utilization" in result.output
        assert "concern hit rate" in result.output


def _write_rows(root: Path, entries: list[dict[str, object]]) -> None:
    """Write whole journal rows, for the #496 tests that need a row
    ``_write_journal`` cannot express (a ``findings_superseded`` row)."""
    path = root / ".kstrl" / "evolution.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _result_row(run_id: str, component_id: str, signatures: list[str]) -> dict[str, object]:
    """A ``component_result`` row: the fields ``get_cross_run_patterns`` reads."""
    return {
        "schema_version": 3,
        "run_id": run_id,
        "component_id": component_id,
        "event_type": "component_result",
        "status": "failed" if signatures else "completed",
        "failure_signatures": signatures,
        "findings_summary": {"total": 0, "by_category": {}},
    }


def _superseded_row(
    run_id: str,
    component_id: str,
    attempt: int,
    signatures: list[str],
    carried_from_run: str | None = None,
) -> dict[str, object]:
    """The row ``Pipeline.journal_superseded_findings`` writes, plus the
    ``carried_from_run`` key ``EvolutionJournal.carry_superseded`` adds."""
    row: dict[str, object] = {
        "schema_version": 3,
        "run_id": run_id,
        "project": "p",
        "component_id": component_id,
        "event_type": FINDINGS_SUPERSEDED_EVENT,
        "attempt": attempt,
        "iteration_count": 1,
        "failure_signatures": signatures,
        "findings": [],
    }
    if carried_from_run is not None:
        row["carried_from_run"] = carried_from_run
    return row


def _pattern_rows(root: Path) -> list[tuple[str, str, int, int, list[str], bool]]:
    """What the router returns for ``root``, through the real config loader."""
    journal = EvolutionJournal(EvolutionConfig.load(root))
    return [
        (
            p.check_name,
            p.error_signature,
            p.frequency,
            p.total_components,
            p.affected_components,
            p.superseded_only,
        )
        for p in journal.get_cross_run_patterns(lookback_runs=10)
    ]


class TestTheRouterCountsSupersededAttempts:
    """#496: an attempt a retry superseded is journaled as a
    ``findings_superseded`` row, and the router read only
    ``component_result`` rows, so every retried attempt's signatures were
    missing from its input."""

    def test_superseded_attempt_signatures_are_counted(self, tmp_path: Path) -> None:
        _write_rows(
            tmp_path,
            [
                _superseded_row("r1", "comp-a", 1, ["review:error_handling"]),
                _result_row("r1", "comp-a", []),
                _superseded_row("r2", "comp-b", 1, ["review:error_handling"]),
                _result_row("r2", "comp-b", []),
            ],
        )
        assert _pattern_rows(tmp_path) == [
            ("review", "error_handling", 2, 2, ["comp-a", "comp-b"], True)
        ]
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert "'review:error_handling' appeared in 2/2 runs across 2 components" in result.output
        assert "recurring signatures (>= 2 runs): 1, of which 1 only on superseded attempts" in (
            result.output
        )

    def test_superseded_and_final_in_one_run_count_once(self, tmp_path: Path) -> None:
        _write_rows(
            tmp_path,
            [
                _superseded_row("r1", "comp-a", 1, ["review:prd_criterion"]),
                _superseded_row("r1", "comp-a", 2, ["review:prd_criterion"]),
                _result_row("r1", "comp-a", ["review:prd_criterion"]),
                _superseded_row("r2", "comp-b", 1, ["review:prd_criterion"]),
                _result_row("r2", "comp-b", []),
            ],
        )
        assert _pattern_rows(tmp_path) == [
            ("review", "prd_criterion", 2, 2, ["comp-a", "comp-b"], False)
        ]
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert "'review:prd_criterion' appeared in 2/2 runs across 2 components" in result.output
        assert "recurring signatures (>= 2 runs): 1, of which 0 only on superseded attempts" in (
            result.output
        )

    def test_a_carried_superseded_row_counts_in_the_run_it_ran_in(self, tmp_path: Path) -> None:
        """``carry_superseded`` (#463) writes run rA's superseded row again
        under rB with ``carried_from_run="rA"``. The attempt ran once, in
        rA, so it is one run, not two."""
        (tmp_path / "kstrl.toml").write_text(
            "[evolution]\nmin_pattern_frequency = 1\n", encoding="utf-8"
        )
        _write_rows(
            tmp_path,
            [
                _superseded_row("rA", "comp-a", 1, ["review:test_quality"]),
                _superseded_row("rB", "comp-a", 1, ["review:test_quality"], carried_from_run="rA"),
                _result_row("rB", "comp-a", []),
            ],
        )
        assert _pattern_rows(tmp_path) == [("review", "test_quality", 1, 2, ["comp-a"], True)]
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert "'review:test_quality' appeared in 1/2 runs across 1 components" in result.output
        assert "recurring signatures (>= 1 runs): 1, of which 1 only on superseded attempts" in (
            result.output
        )

    def test_a_carried_row_counts_when_its_original_is_outside_the_window(
        self, tmp_path: Path
    ) -> None:
        """rA's own row falls outside the ten-run lookback window, so the
        copy rB carried is the only row of that attempt the router reads.
        It still counts, attributed to rA."""
        (tmp_path / "kstrl.toml").write_text(
            "[evolution]\nmin_pattern_frequency = 1\n", encoding="utf-8"
        )
        _write_rows(
            tmp_path,
            [
                _superseded_row("rA", "comp-a", 1, ["review:test_quality"]),
                *[_result_row(f"r{n}", "comp-x", []) for n in range(1, 10)],
                _superseded_row("rB", "comp-a", 1, ["review:test_quality"], carried_from_run="rA"),
                _result_row("rB", "comp-a", []),
            ],
        )
        assert [(c, s, f, comps, only) for c, s, f, _, comps, only in _pattern_rows(tmp_path)] == [
            ("review", "test_quality", 1, ["comp-a"], True)
        ]
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert "'review:test_quality' appeared in 1/" in result.output

    def test_unenrolled_check_is_not_a_lesson(self, tmp_path: Path) -> None:
        assert category_for_check("zzz-never-enrolled") == "unenrolled"
        assert category_for_check("engineer") == "iteration"
        assert category_for_check("unknown") == "iteration"
        _write_rows(
            tmp_path,
            [
                _result_row("r1", "comp-z", ["zzz-never-enrolled:boom"]),
                _result_row("r2", "comp-z", ["zzz-never-enrolled:boom"]),
            ],
        )
        patterns = EvolutionJournal(EvolutionConfig.load(tmp_path)).get_cross_run_patterns()
        assert [(p.check_name, p.category) for p in patterns] == [
            ("zzz-never-enrolled", "unenrolled")
        ]
        routing = route_patterns(patterns)
        assert routing.unrouted == tuple(patterns)
        assert routing.lessons == ()
        assert routing.mechanical == ()
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert "[zzz-never-enrolled] boom (category unenrolled; not a lesson)" in result.output
        assert "'iteration'" not in result.output
        assert (
            "A check name not in evolution._CATEGORY_BY_CHECK lands here too, "
            "with category 'unenrolled'."
        ) in result.output
        assert not (tmp_path / ".kstrl" / "proposals").exists()


def test_lessons_bucket_is_every_learnable_category(tmp_path: Path) -> None:
    """#507: the lessons bucket is every check whose category is
    verification, review, security or contract, and nothing else."""
    patterns = [
        FailurePattern(
            description="d",
            frequency=2,
            total_components=2,
            affected_components=["comp-a"],
            check_name=name,
            error_signature="SIG",
            category="iteration",
        )
        for name in _CATEGORY_BY_CHECK
    ]
    routing = route_patterns(patterns)
    expected = {n for n, c in _CATEGORY_BY_CHECK.items() if c in _LEARNABLE_CATEGORIES}
    assert {p.check_name for p in routing.lessons} == expected
    assert {_CATEGORY_BY_CHECK[n] for n in expected} == _LEARNABLE_CATEGORIES

    signatures = [
        "linter:S608",
        "review:prd_criterion",
        "security:injection",
        "contract:api-mismatch",
        "pr:push-failed",
        "engineer:no-progress",
        "zzz-never-enrolled:boom",
    ]
    rows = [(run, f"comp-{n}", sig) for run in ("r1", "r2") for n, sig in enumerate(signatures)]
    _write_journal(tmp_path, rows)
    result = _invoke(tmp_path)
    assert result.exit_code == 0, (result.output, result.exception)
    sections = _sections(result.output)
    assert _LESSONS_HEADING in sections, result.output
    lessons = sections[_LESSONS_HEADING]
    for line in (
        "[linter] S608 (category verification)",
        "[review] prd_criterion (category review)",
        "[security] injection (category security)",
        "[contract] api-mismatch (category contract)",
    ):
        assert line in lessons, (line, result.output)
    for name in ("[pr]", "[engineer]", "[zzz-never-enrolled]"):
        assert name not in lessons, (name, result.output)
    assert "[pr] push-failed" in sections[_MECHANICAL_HEADING], result.output
    unrouted = sections[_UNROUTED_HEADING]
    assert "[engineer] no-progress (category iteration" in unrouted, result.output
    assert "[zzz-never-enrolled] boom (category unenrolled" in unrouted, result.output
    assert not (tmp_path / ".kstrl" / "proposals").exists()


_RETIRED_LINE = (
    "WARN: [evolution] {keys} in kstrl.toml: no effect since #217 "
    "(the proposal generator is deleted)"
)


def test_retired_evolution_keys_are_named(tmp_path: Path) -> None:
    """#507: a retired [evolution] key must not vanish silently."""
    _write_journal(tmp_path, [("r1", "comp-a", "linter:S608")])
    toml = tmp_path / "kstrl.toml"
    cases = [
        (
            "[evolution]\nauto_propose = false\nauto_apply_computational = true\n",
            ("auto_propose", "auto_apply_computational"),
        ),
        ("[evolution]\nauto_apply_computational = false\n", ("auto_apply_computational",)),
        ("[evolution]\nlookback_runs = 5\n", ()),
    ]
    for body, keys in cases:
        toml.write_text(body, encoding="utf-8")
        assert EvolutionConfig.load(tmp_path).retired_keys == keys
        expected = [_RETIRED_LINE.format(keys=", ".join(keys))] if keys else []
        # The default path and --status both name the keys.
        for extra in ([], ["--status"]):
            result = CliRunner().invoke(
                cli, ["evolve", *extra, "--root", str(tmp_path), "--ui", "plain", "--no-color"]
            )
            assert result.exit_code == 0, (extra, result.output, result.exception)
            named = [line for line in result.output.splitlines() if "no effect since #217" in line]
            assert named == expected, (body, extra, result.output)


def test_an_existing_proposals_directory_is_reported_and_left_alone(tmp_path: Path) -> None:
    """#507: files the deleted generator wrote are named, counted and
    never touched, and nothing new is written beside them (P1.3)."""
    root = tmp_path.resolve()
    proposals = root / ".kstrl" / "proposals"
    proposals.mkdir(parents=True)
    files = {
        "prop-001.md": "# PROP-001: a\n",
        "prop-002.md": "# PROP-002: b\n**Applied**: 2026-09-01T00:00:00Z\n",
        "notes.txt": "kept\n",
    }
    for name, body in files.items():
        (proposals / name).write_text(body, encoding="utf-8")
    rows = [(run, "comp-a", "review:prd_criterion") for run in ("r1", "r2")]

    notice = [
        f"{proposals}: 3 file(s) from the deleted proposal generator; "
        f"nothing reads this directory since #217"
    ]
    # Twice: once with a recurring pattern (main wrote prop-003.md here),
    # once with a single-row journal, so no pattern recurs and the notice
    # must not sit behind a "no patterns" early exit.
    for journal_rows in (rows, rows[:1]):
        _write_journal(root, journal_rows)
        result = _invoke(root)
        assert result.exit_code == 0, (result.output, result.exception)
        named = [line for line in result.output.splitlines() if str(proposals) in line]
        assert named == notice, (journal_rows, result.output)
        assert sorted(p.name for p in proposals.iterdir()) == sorted(files)
        for name, body in files.items():
            assert (proposals / name).read_text(encoding="utf-8") == body

    other = root / "other"
    _write_journal(other, rows)
    result = _invoke(other)
    assert result.exit_code == 0, (result.output, result.exception)
    assert "nothing reads this directory" not in result.output
    assert not (other / ".kstrl" / "proposals").exists()


def test_the_apply_option_is_gone(tmp_path: Path) -> None:
    """#507: ``ks evolve --apply`` is refused by click as an unknown
    option, and neither CLAUDE.md nor the proposal file is touched."""
    root = tmp_path.resolve()
    proposal = root / ".kstrl" / "proposals" / "prop-001.md"
    proposal.parent.mkdir(parents=True)
    proposal_text = (
        "# PROP-001: Record a convention\n\n"
        "**Type**: computational\n**Target**: claude_md\n\n"
        "## Suggested Change\n\nAlways run the linter.\n"
    )
    proposal.write_text(proposal_text, encoding="utf-8")
    claude_md = root / "CLAUDE.md"
    claude_md.write_text("# CLAUDE.md\n\n## Agent Learnings\n", encoding="utf-8")
    _write_journal(root, [("r1", "comp-a", "linter:S608")])

    for apply_id in ("PROP-001", "all"):
        result = CliRunner().invoke(
            cli,
            ["evolve", "--apply", apply_id, "--root", str(root), "--ui", "plain", "--no-color"],
            input="y\n",
        )
        assert result.exit_code == 2, (apply_id, result.output, result.exception)
        # click's wording, pinned loosely: it has changed between releases.
        assert "No such option" in result.output, result.output
        assert "--apply" in result.output, result.output
    assert claude_md.read_text(encoding="utf-8") == "# CLAUDE.md\n\n## Agent Learnings\n"
    assert proposal.read_text(encoding="utf-8") == proposal_text


def test_the_proposals_notice_counts_files_and_survives_an_unreadable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#507: the notice counts files, not subdirectories, and a proposals
    directory that cannot be listed is named with the reason rather than
    taking ``ks evolve`` down with a traceback."""
    root = tmp_path.resolve()
    proposals = root / ".kstrl" / "proposals"
    (proposals / "archive").mkdir(parents=True)
    (proposals / "prop-001.md").write_text("# PROP-001: a\n", encoding="utf-8")
    _write_journal(root, [("r1", "comp-a", "linter:S608")])

    result = _invoke(root)
    assert result.exit_code == 0, (result.output, result.exception)
    named = [line for line in result.output.splitlines() if str(proposals) in line]
    assert named == [
        f"{proposals}: 1 file(s) from the deleted proposal generator; "
        f"nothing reads this directory since #217"
    ], result.output

    real_iterdir = Path.iterdir

    def refusing_iterdir(self: Path) -> Iterator[Path]:
        if self == proposals:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", refusing_iterdir)
    result = _invoke(root)
    assert result.exit_code == 0, (result.output, result.exception)
    named = [line for line in result.output.splitlines() if str(proposals) in line]
    assert len(named) == 1, result.output
    assert named[0].startswith(f"{proposals}: files not counted ("), result.output
    assert "Permission denied" in named[0], result.output


def test_retired_keys_are_named_when_evolution_is_disabled(tmp_path: Path) -> None:
    """#507: the retired-key notice sits before the ``enabled`` exit, so a
    config that turns evolution off still hears that its keys are dead."""
    (tmp_path / "kstrl.toml").write_text(
        "[evolution]\nenabled = false\nauto_propose = true\n", encoding="utf-8"
    )
    result = _invoke(tmp_path)
    assert result.exit_code == 2, (result.output, result.exception)
    assert "Evolution is disabled in config" in result.output, result.output
    named = [line for line in result.output.splitlines() if "no effect since #217" in line]
    assert named == [_RETIRED_LINE.format(keys="auto_propose")], result.output
