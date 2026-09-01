"""The architect's disposition register (#260).

Four ways to close a question, one of which halts, plus the delivery
path that puts the closed ones in front of the engineer.
"""

from __future__ import annotations

import json
from pathlib import Path

from kstrl.decisions import (
    MAX_DECISION_TOKENS,
    SPEC_DECISIONS_REL_PATH,
    SpecDecision,
    _decision_counts,
    build_decisions_context,
    escalations,
    parse_decisions,
    read_decisions,
    write_decisions,
)


def _decision(**overrides: str) -> dict[str, str]:
    entry = {
        "question": "what encoding does the reader name",
        "disposition": "decided",
        "resolution": "utf-8, named explicitly at every read site",
        "reason": "the locale default is not a contract",
        "alternative": "leave it to the locale",
        "component": "",
    }
    entry.update(overrides)
    return entry


class TestParsing:
    def test_a_well_formed_entry_round_trips(self) -> None:
        parsed = parse_decisions({"decisions": [_decision()]})
        assert parsed == [
            SpecDecision(
                question="what encoding does the reader name",
                disposition="decided",
                resolution="utf-8, named explicitly at every read site",
                reason="the locale default is not a contract",
                alternative="leave it to the locale",
                component="",
            )
        ]

    def test_an_unknown_disposition_is_dropped(self) -> None:
        assert parse_decisions({"decisions": [_decision(disposition="deferred")]}) == []

    def test_a_missing_resolution_is_dropped(self) -> None:
        """A decision with no answer in it is not a decision. Dropping it
        is safe only because the validator counts escalations before this
        runs, so a dropped escalation is a retryable error upstream."""
        assert parse_decisions({"decisions": [_decision(resolution="  ")]}) == []

    def test_a_missing_question_is_dropped(self) -> None:
        assert parse_decisions({"decisions": [_decision(question="")]}) == []

    def test_a_non_object_entry_is_dropped(self) -> None:
        assert parse_decisions({"decisions": ["escalated"]}) == []

    def test_a_non_list_decisions_field_is_empty(self) -> None:
        assert parse_decisions({"decisions": {"a": 1}}) == []

    def test_a_payload_that_is_not_an_object_is_empty(self) -> None:
        assert parse_decisions(["decisions"]) == []

    def test_optional_fields_default_to_empty(self) -> None:
        parsed = parse_decisions(
            {"decisions": [{"question": "q", "disposition": "spiked", "resolution": "r"}]}
        )
        assert parsed == [SpecDecision(question="q", disposition="spiked", resolution="r")]

    def test_escalations_are_the_halting_subset(self) -> None:
        parsed = parse_decisions(
            {
                "decisions": [
                    _decision(),
                    _decision(disposition="escalated", question="what ships first"),
                    _decision(disposition="assumed", question="what the default is"),
                ]
            }
        )
        assert [d.question for d in escalations(parsed)] == ["what ships first"]

    def test_counts_cover_every_disposition(self) -> None:
        parsed = parse_decisions({"decisions": [_decision(), _decision(disposition="spiked")]})
        assert _decision_counts(parsed) == {
            "escalated": 0,
            "decided": 1,
            "assumed": 0,
            "spiked": 1,
        }


class TestPersistence:
    def test_the_register_round_trips_through_disk(self, tmp_path: Path) -> None:
        decisions = parse_decisions({"decisions": [_decision(), _decision(disposition="spiked")]})
        path = write_decisions(decisions, tmp_path, "proj", "spec.md")
        assert path == tmp_path / SPEC_DECISIONS_REL_PATH
        assert read_decisions(tmp_path) == decisions

    def test_a_closed_nothing_run_still_writes_a_record(self, tmp_path: Path) -> None:
        """An empty register is the record that the architect had no open
        question. That is a different fact from "no record"."""
        path = write_decisions([], tmp_path, "proj", "spec.md")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["decisions"] == []
        assert payload["counts"]["escalated"] == 0

    def test_the_file_names_its_project_and_spec(self, tmp_path: Path) -> None:
        path = write_decisions([], tmp_path, "writers-room", "spec-slice-1.md")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["project"] == "writers-room"
        assert payload["specFile"] == "spec-slice-1.md"

    def test_a_missing_file_reads_as_no_decisions(self, tmp_path: Path) -> None:
        assert read_decisions(tmp_path) == []

    def test_a_truncated_file_reads_as_no_decisions(self, tmp_path: Path) -> None:
        path = tmp_path / SPEC_DECISIONS_REL_PATH
        path.parent.mkdir(parents=True)
        path.write_text('{"decisions": [', encoding="utf-8")
        assert read_decisions(tmp_path) == []

    def test_a_non_utf8_file_reads_as_no_decisions(self, tmp_path: Path) -> None:
        """UnicodeDecodeError is a ValueError, not an OSError, so a
        fail-closed ``except OSError`` would let it escape and take the
        whole factory run down."""
        path = tmp_path / SPEC_DECISIONS_REL_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"decisions": [], "project": "\xff\xfe"}')
        assert read_decisions(tmp_path) == []


class TestEngineerContext:
    def test_no_decisions_renders_nothing(self) -> None:
        assert build_decisions_context([], "comp-a") == ""

    def test_own_decisions_render_in_full(self) -> None:
        decisions = parse_decisions({"decisions": [_decision(component="comp-a")]})
        rendered = build_decisions_context(decisions, "comp-a")
        assert "Decisions binding this component (comp-a)" in rendered
        assert "utf-8, named explicitly at every read site" in rendered
        assert "the locale default is not a contract" in rendered
        assert "leave it to the locale" in rendered

    def test_a_run_wide_decision_also_renders_in_full(self) -> None:
        """An empty component means "binds the whole run", so it binds
        this engineer too and keeps its reason and its alternative."""
        decisions = parse_decisions({"decisions": [_decision(component="")]})
        rendered = build_decisions_context(decisions, "comp-a")
        assert "Decisions binding the whole run" in rendered
        assert "the locale default is not a contract" in rendered
        assert "leave it to the locale" in rendered

    def test_other_components_decisions_are_summarised_not_dropped(self) -> None:
        decisions = parse_decisions({"decisions": [_decision(component="comp-b")]})
        rendered = build_decisions_context(decisions, "comp-a")
        assert "Decisions binding other components" in rendered
        assert "utf-8, named explicitly at every read site" in rendered
        # The summary tier carries the answer but not the argument.
        assert "the locale default is not a contract" not in rendered

    def test_the_binding_tiers_pack_before_the_stranger_tier(self) -> None:
        """The budget must never cut something that binds this
        component, so own and run-wide pack first and only the stranger
        tier can be dropped."""
        decisions = parse_decisions(
            {
                "decisions": [
                    _decision(component="comp-b", question=f"stranger {n} " + "x" * 300)
                    for n in range(40)
                ]
                + [
                    _decision(component="comp-a", question="mine"),
                    _decision(component="", question="everyone's"),
                ]
            }
        )
        rendered = build_decisions_context(decisions, "comp-a")
        assert "mine" in rendered
        assert "everyone's" in rendered
        assert "did not fit the context budget" in rendered
        assert "None of them bind this component" in rendered

    def test_an_escalation_leads_its_tier(self) -> None:
        decisions = parse_decisions(
            {
                "decisions": [
                    _decision(component="comp-a", question="second"),
                    _decision(
                        component="comp-a",
                        disposition="escalated",
                        question="first",
                    ),
                ]
            }
        )
        rendered = build_decisions_context(decisions, "comp-a")
        assert rendered.index("first") < rendered.index("second")

    def test_the_budget_bites_out_loud(self) -> None:
        """A register of the size five real runs produced does not fit,
        so the drop has to be stated rather than silent."""
        decisions = parse_decisions(
            {
                "decisions": [
                    _decision(component="comp-a", question=f"question {n} " + "x" * 400)
                    for n in range(60)
                ]
            }
        )
        rendered = build_decisions_context(decisions, "comp-a")
        assert "did not fit the context budget" in rendered
        assert len(rendered) // 4 <= MAX_DECISION_TOKENS

    def test_everything_dropped_renders_nothing(self) -> None:
        decisions = parse_decisions({"decisions": [_decision(question="q " + "x" * 100)]})
        assert build_decisions_context(decisions, "comp-a", max_tokens=1) == ""


class TestTheRegisterReachesTheEngineer:
    """The delivery hop. Without this the register is a file nobody
    reads, which is exactly what #260 found the audit already was."""

    def test_the_prefix_lands_in_the_engineer_context(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        from types import SimpleNamespace

        import kstrl.agents as agents_mod
        import kstrl.factory as factory_mod
        import kstrl.loop as loop_mod
        from kstrl.loop import LoopResult

        seen: dict[str, object] = {}

        def fake_run_loop(*args: object, **kwargs: object) -> LoopResult:
            seen.update(kwargs)
            return LoopResult(completed=True, iterations=1, exit_code=0)

        monkeypatch.setattr(loop_mod, "run_loop", fake_run_loop)
        monkeypatch.setattr(
            agents_mod,
            "get_agent",
            lambda *a, **k: SimpleNamespace(name="fake", usage_records=[]),
        )
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "scripts" / "kstrl" / "prd.json").write_text("{}", encoding="utf-8")
        (tmp_path / "scripts" / "kstrl" / "prompt.md").write_text("go", encoding="utf-8")

        factory_mod._run_component(
            "comp-a",
            "scripts/kstrl/prd.json",
            str(tmp_path),
            str(tmp_path),
            "scripts/kstrl/prompt.md",
            None,
            None,
            None,
            "claude",
            0.0,
            decisions_prefix="## Architect Decisions\n\n- **[decided]** utf-8 it is\n",
        )
        prefix = seen["context_prefix"]
        assert isinstance(prefix, str)
        assert "## Architect Decisions" in prefix
        assert "utf-8 it is" in prefix
