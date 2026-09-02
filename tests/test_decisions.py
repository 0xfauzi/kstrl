"""The architect's disposition register (#260).

Four ways to close a question, one of which halts, plus the delivery
path that puts the closed ones in front of the engineer.

Round 2 of this change added the four properties review found missing,
and every class below is named for the one it holds:

- a malformed entry is a NAMED FAULT, never a silent zero (F1);
- a decision that binds this component is never dropped (F2);
- the register on disk must prove it belongs to this manifest (F3);
- the engineer-facing English lives in an enrolled prompt (F4).
"""

from __future__ import annotations

import inspect
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import kstrl.decisions as decisions_mod
from kstrl.decisions import (
    DECISIONS_CONTEXT_PROMPT,
    MAX_OTHER_DECISION_TOKENS,
    REGISTER_MISSING,
    REGISTER_OK,
    REGISTER_UNREADABLE,
    SPEC_DECISIONS_REL_PATH,
    DecisionRegister,
    DecisionRegisterError,
    SpecDecision,
    _decision_counts,
    bind_register,
    build_decisions_context,
    decision_entry_errors,
    decisions_payload_errors,
    escalations,
    parse_decisions,
    read_decisions,
    write_decisions,
)


def _decision(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "issue": "reader-encoding-unspecified",
        "question": "what encoding does the reader name",
        "disposition": "decided",
        "resolution": "utf-8, named explicitly at every read site",
        "reason": "the locale default is not a contract",
        "alternative": "leave it to the locale",
        "component": "",
    }
    entry.update(overrides)
    return entry


def _spec_decision(**overrides: object) -> SpecDecision:
    fields: dict[str, Any] = {
        "issue": "i0",
        "question": "q",
        "disposition": "decided",
        "resolution": "r",
    }
    fields.update(overrides)
    return SpecDecision(**fields)


class TestParsing:
    def test_a_well_formed_entry_round_trips(self) -> None:
        parsed = parse_decisions({"decisions": [_decision()]})
        assert parsed == [
            SpecDecision(
                issue="reader-encoding-unspecified",
                question="what encoding does the reader name",
                disposition="decided",
                resolution="utf-8, named explicitly at every read site",
                reason="the locale default is not a contract",
                alternative="leave it to the locale",
                component="",
            )
        ]

    def test_whitespace_is_stripped(self) -> None:
        parsed = parse_decisions({"decisions": [_decision(question="  padded  ")]})
        assert parsed[0].question == "padded"


class TestAMalformedEntryIsAFaultNotAZero:
    """F1. Round 1 parsed first and counted afterwards, so an entry the
    parser could not read became an ABSENCE, and an absence agrees with
    any count. Measured on that code: a disposition of "Escalated"
    produced a payload that validated, wrote a PRD and wrote a manifest.
    """

    @pytest.mark.parametrize(
        ("label", "entry", "needle"),
        [
            ("capitalised", _decision(disposition="Escalated"), "case-exact"),
            ("upper", _decision(disposition="ESCALATED"), "case-exact"),
            ("misspelled", _decision(disposition="excalated"), "case-exact"),
            ("truncated", _decision(disposition="escalate"), "case-exact"),
            ("non-string disposition", _decision(disposition=7), "must be a string"),
            ("missing disposition", {"issue": "i", "question": "q", "resolution": "r"}, "string"),
            ("blank question", _decision(question="   "), "question: must not be empty"),
            ("missing question", _decision(question=None), "question: must be a string"),
            ("blank resolution", _decision(resolution=""), "resolution: must not be empty"),
            ("missing join key", _decision(issue=None), "issue: must be a string"),
            ("blank join key", _decision(issue="  "), "issue: must not be empty"),
            ("non-string component", _decision(component=3), "component: must be a string"),
        ],
    )
    def test_each_bad_entry_is_named_and_indexed(
        self, label: str, entry: object, needle: str
    ) -> None:
        errors = decision_entry_errors(0, entry)
        assert errors, f"{label} produced no error"
        assert all(e.startswith("decisions[0].") or e.startswith("decisions[0]:") for e in errors)
        assert any(needle in e for e in errors)

    def test_a_non_object_entry_is_rejected(self) -> None:
        assert decision_entry_errors(2, "escalated") == ["decisions[2]: must be an object, got str"]

    def test_the_array_is_required(self) -> None:
        assert decisions_payload_errors({"spec_issues": []}) == [
            "'decisions' is required (use [] when the spec raised no question)"
        ]

    def test_an_empty_array_is_legal(self) -> None:
        assert decisions_payload_errors({"decisions": []}) == []

    def test_a_non_list_array_is_rejected(self) -> None:
        assert decisions_payload_errors({"decisions": {}}) == [
            "'decisions' must be an array, got dict"
        ]

    def test_parse_skips_exactly_what_the_validator_rejects(self) -> None:
        """The two must agree, or a skip is again the difference between
        halting and not."""
        bad = _decision(disposition="Escalated")
        assert decision_entry_errors(0, bad)
        assert parse_decisions({"decisions": [bad]}) == []


class TestEscalation:
    def test_escalations_are_selected_by_exact_disposition(self) -> None:
        parsed = parse_decisions(
            {
                "decisions": [
                    _decision(issue="a", disposition="escalated"),
                    _decision(issue="b", disposition="decided"),
                ]
            }
        )
        assert [d.issue for d in escalations(parsed)] == ["a"]

    def test_counts_cover_every_disposition(self) -> None:
        counts = _decision_counts([_spec_decision(disposition="escalated")])
        assert counts == {"escalated": 1, "decided": 0, "assumed": 0, "spiked": 0}


class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = write_decisions(
            [_spec_decision(component="comp-a")],
            root_dir=tmp_path,
            project_name="proj",
            spec_file="spec.md",
            halted=False,
        )
        assert path == tmp_path / SPEC_DECISIONS_REL_PATH
        register = read_decisions(tmp_path)
        assert register.status == REGISTER_OK
        assert register.project == "proj"
        assert register.spec_file == "spec.md"
        assert register.halted is False
        assert register.decisions == (_spec_decision(component="comp-a"),)

    def test_the_halt_flag_survives_the_round_trip(self, tmp_path: Path) -> None:
        write_decisions(
            [_spec_decision(disposition="escalated")],
            root_dir=tmp_path,
            project_name="proj",
            spec_file="spec.md",
            halted=True,
        )
        assert read_decisions(tmp_path).halted is True

    def test_the_write_goes_through_atomicio(self, tmp_path: Path) -> None:
        """Mutation guard. Replacing ``atomic_write_json`` with a direct
        ``write_text`` left all six round-1 persistence tests passing,
        because every one of them only checked what came back out. A
        torn register is exactly the input F3 has to treat as fatal, so
        the atomicity is the property, not an implementation detail."""
        with patch.object(decisions_mod, "atomic_write_json") as writer:
            write_decisions(
                [_spec_decision()],
                root_dir=tmp_path,
                project_name="proj",
                spec_file="spec.md",
                halted=False,
            )
        writer.assert_called_once()
        payload = writer.call_args.args[1]
        assert payload["project"] == "proj"
        assert payload["decisions"][0]["issue"] == "i0"

    def test_an_empty_register_is_still_written(self, tmp_path: Path) -> None:
        write_decisions([], root_dir=tmp_path, project_name="p", spec_file="s.md", halted=False)
        register = read_decisions(tmp_path)
        assert register.status == REGISTER_OK
        assert register.decisions == ()

    def test_a_missing_file_reports_missing(self, tmp_path: Path) -> None:
        register = read_decisions(tmp_path)
        assert register.status == REGISTER_MISSING
        assert register.decisions == ()

    def test_a_non_utf8_file_reports_unreadable(self, tmp_path: Path) -> None:
        """``UnicodeDecodeError`` is a ``ValueError``, so a fail-closed
        ``except OSError`` would let it escape."""
        path = tmp_path / SPEC_DECISIONS_REL_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"decisions": [], "project": "\xff\xfe"}')
        register = read_decisions(tmp_path)
        assert register.status == REGISTER_UNREADABLE
        assert register.decisions == ()

    def test_a_torn_file_reports_unreadable(self, tmp_path: Path) -> None:
        path = tmp_path / SPEC_DECISIONS_REL_PATH
        path.parent.mkdir(parents=True)
        path.write_text('{"decisions": [{"issue": "i0",', encoding="utf-8")
        assert read_decisions(tmp_path).status == REGISTER_UNREADABLE

    def test_a_malformed_entry_on_disk_reports_unreadable(self, tmp_path: Path) -> None:
        """Not "reads as empty". A hand-edited register with a
        capitalised disposition is the same fail-open F1 closed, one
        layer down."""
        path = tmp_path / SPEC_DECISIONS_REL_PATH
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "project": "p",
                    "specFile": "s.md",
                    "decisions": [_decision(disposition="Escalated")],
                }
            ),
            encoding="utf-8",
        )
        register = read_decisions(tmp_path)
        assert register.status == REGISTER_UNREADABLE
        assert "case-exact" in register.detail


class TestTheRegisterMustBelongToThisManifest:
    """F3. The factory reads one fixed path, so without this a register
    left by another project sits exactly where this run looks.
    Reproduced on round 1: a run on project-b/b.md with project A's
    register beside it handed the engineer project A's instruction."""

    def _ok(self, **kw: object) -> DecisionRegister:
        fields: dict[str, Any] = {
            "decisions": (_spec_decision(),),
            "project": "proj",
            "spec_file": "spec.md",
            "status": REGISTER_OK,
        }
        fields.update(kw)
        return DecisionRegister(**fields)

    def test_a_matching_register_binds(self) -> None:
        assert bind_register(self._ok(), "proj", "spec.md") == (_spec_decision(),)

    def test_a_missing_register_binds_nothing_and_does_not_raise(self) -> None:
        assert bind_register(DecisionRegister(status=REGISTER_MISSING), "p", "s.md") == ()

    def test_a_foreign_project_is_refused(self) -> None:
        with pytest.raises(DecisionRegisterError, match="belongs to project"):
            bind_register(self._ok(), "other", "spec.md")

    def test_a_foreign_spec_is_refused(self) -> None:
        with pytest.raises(DecisionRegisterError, match="belongs to project"):
            bind_register(self._ok(), "proj", "other.md")

    def test_an_unreadable_register_is_refused(self) -> None:
        register = DecisionRegister(status=REGISTER_UNREADABLE, detail="torn")
        with pytest.raises(DecisionRegisterError, match="unreadable"):
            bind_register(register, "proj", "spec.md")

    def test_a_halted_register_is_refused(self) -> None:
        """A halted decompose saves no manifest, so its register would
        otherwise sit beside an OLDER manifest and read as that run's."""
        with pytest.raises(DecisionRegisterError, match="HALTED"):
            bind_register(self._ok(halted=True), "proj", "spec.md")


class TestNothingBindingThisComponentIsEverDropped:
    """F2. Round 1 ran one greedy budget over all three tiers, so the
    ordering protected nothing: measured, 100 own decisions with
    300-character questions rendered 22 and dropped 78, and one
    oversized own decision rendered an empty block."""

    def _own(self, count: int, chars: int) -> list[SpecDecision]:
        return [
            _spec_decision(issue=f"i{i}", question=f"q{i} " + "x" * chars, component="comp-a")
            for i in range(count)
        ]

    @pytest.mark.parametrize(("count", "chars"), [(100, 300), (1000, 300), (1000, 5)])
    def test_every_own_decision_is_rendered_whatever_the_size(self, count: int, chars: int) -> None:
        block = build_decisions_context(self._own(count, chars), "comp-a")
        own_section = block.split("### Decisions binding the whole run")[0]
        assert own_section.count("- **[") == count

    def test_one_oversized_own_decision_still_renders(self) -> None:
        block = build_decisions_context(self._own(1, 40000), "comp-a")
        assert block != ""
        assert "x" * 40000 in block

    def test_a_run_wide_decision_is_never_dropped_either(self) -> None:
        run_wide = [
            _spec_decision(issue=f"r{i}", question=f"rq{i} " + "y" * 300, component="")
            for i in range(200)
        ]
        block = build_decisions_context(run_wide, "comp-a")
        section = block.split("### Decisions binding the whole run")[1]
        section = section.split("### Decisions binding other components")[0]
        assert section.count("- **[") == 200

    def test_the_other_tier_is_the_only_one_with_a_cap(self) -> None:
        others = [
            _spec_decision(issue=f"o{i}", question=f"oq{i} " + "z" * 300, component="comp-b")
            for i in range(500)
        ]
        block = build_decisions_context(self._own(3, 10) + others, "comp-a")
        own_section = block.split("### Decisions binding the whole run")[0]
        other_section = block.split("### Decisions binding other components")[1]
        assert own_section.count("- **[") == 3
        shown = other_section.count("- **[")
        assert 0 < shown < 500
        body = other_section.split("\n\n", 1)[1]
        assert len(body) // 4 <= MAX_OTHER_DECISION_TOKENS

    def test_the_cap_is_a_parameter_and_the_parameter_is_the_cap(self) -> None:
        """The round-2 /simplify pass proposed deleting
        ``max_other_tokens`` because no test passed anything but the
        default, which is a fair hit on the tests rather than on the
        seam: an unexercised parameter is an unchecked one. Checked
        here instead of removed, because it is the only way to measure
        the cap without monkeypatching a module constant, and the F2
        measurements in the PR body were taken through it.
        """
        others = [
            _spec_decision(issue=f"o{i}", question=f"oq{i} " + "z" * 300, component="comp-b")
            for i in range(500)
        ]
        tight = build_decisions_context(others, "comp-a", max_other_tokens=100)
        loose = build_decisions_context(others, "comp-a", max_other_tokens=100000)
        assert tight.count("- **[") < loose.count("- **[")
        assert loose.count("- **[") == 500
        tight_body = tight.split("### Decisions binding other components")[1]
        assert len(tight_body.split("\n\n", 1)[1]) // 4 <= 100

    def test_a_cap_of_zero_shows_nothing_and_says_so(self) -> None:
        """The boundary, because a budget that admits one item at zero
        is a budget that rounds permissive, which is the arithmetic
        half of F2."""
        others = [_spec_decision(issue="o0", component="comp-b")]
        block = build_decisions_context(others, "comp-a", max_other_tokens=0)
        assert "0 of 1 shown" in block
        assert block.count("- **[") == 0

    def test_the_heading_reports_the_real_numbers(self) -> None:
        others = [
            _spec_decision(issue=f"o{i}", question=f"oq{i} " + "z" * 300, component="comp-b")
            for i in range(500)
        ]
        block = build_decisions_context(others, "comp-a")
        heading = block.split("### Decisions binding other components (")[1].split(")")[0]
        shown = int(heading.split(" of ")[0])
        assert f"{shown} of 500 shown" in heading
        other_section = block.split("### Decisions binding other components")[1]
        assert other_section.count("- **[") == shown

    def test_a_smaller_later_item_still_fits(self) -> None:
        items = [
            _spec_decision(issue="big", question="b" * 20000, component="comp-b"),
            _spec_decision(issue="small", question="small one", component="comp-b"),
        ]
        block = build_decisions_context(items, "comp-a")
        assert "small one" in block
        assert "b" * 20000 not in block

    def test_no_decisions_renders_nothing(self) -> None:
        assert build_decisions_context([], "comp-a") == ""

    def test_escalations_lead_their_tier(self) -> None:
        items = [
            _spec_decision(issue="b", question="second", component="comp-a"),
            _spec_decision(
                issue="a", question="first", disposition="escalated", component="comp-a"
            ),
        ]
        rendered = build_decisions_context(items, "comp-a")
        assert rendered.index("first") < rendered.index("second")


class TestTheEngineerFacingEnglishIsEnrolled:
    """F4. Round 1 built this block from inline f-strings, so the
    reviewer changed "binding" to "advisory" and all 61 prompt-version
    and enrollment tests stayed green."""

    def test_the_block_is_exactly_the_enrolled_template(self) -> None:
        own = _spec_decision(issue="i1", question="own q", component="comp-a")
        run_wide = _spec_decision(issue="i2", question="run q", component="")
        other = _spec_decision(issue="i3", question="other q", component="comp-b")
        block = build_decisions_context([own, run_wide, other], "comp-a")
        assert block == DECISIONS_CONTEXT_PROMPT.format(
            component_id="comp-a",
            own="- **[decided]** own q\n  - Resolution: r",
            run_wide="- **[decided]** run q\n  - Resolution: r",
            other_shown=1,
            other_total=1,
            other="- **[decided]** other q -> r",
        )

    def test_an_empty_tier_renders_as_nothing_at_all(self) -> None:
        """No "(none)" marker: a second piece of harness English would
        need its own enrolled constant, and the H3 render guard cannot
        hold a fragment nested inside another enrolled template."""
        block = build_decisions_context([_spec_decision(component="comp-a")], "comp-a")
        assert block == DECISIONS_CONTEXT_PROMPT.format(
            component_id="comp-a",
            own="- **[decided]** q\n  - Resolution: r",
            run_wide="",
            other_shown=0,
            other_total=0,
            other="",
        )

    def test_the_instruction_still_says_binding(self) -> None:
        assert "These are binding: implement what is written here." in DECISIONS_CONTEXT_PROMPT


class TestTheRegisterReachesTheEngineer:
    """The delivery hop. Without this the register is a file nobody
    reads, which is exactly what #260 found the audit already was."""

    def test_the_prefix_lands_in_the_engineer_context(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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


def _factory_project(tmp_path: Path, component_id: str) -> Path:
    """The minimal layout the factory needs, as test_knowledge builds it."""
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("test prompt", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}', encoding="utf-8"
    )
    feature_dir = kstrl_dir / "feature" / component_id
    feature_dir.mkdir(parents=True)
    (feature_dir / "prd.json").write_text(
        json.dumps(
            {
                "branchName": "test",
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Test",
                        "acceptanceCriteria": ["AC1"],
                        "priority": 1,
                        "passes": True,
                        "notes": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _one_component_manifest(project: str, spec_file: str) -> Any:
    from kstrl.manifest import Component, Manifest

    return Manifest(
        version="1",
        spec_file=spec_file,
        project_name=project,
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id="comp-a",
                title="Component comp-a",
                description="d",
                dependencies=[],
                prd_path="scripts/kstrl/feature/comp-a/prd.json",
                branch_name="kstrl/comp-a",
            )
        ],
    )


def _factory_inputs(root: Path) -> tuple[Any, Any, Any]:
    from kstrl.config import KstrlConfig
    from kstrl.factory import FactoryConfig
    from kstrl.ui.plain import PlainUI
    from kstrl.verify import VerifyConfig

    config = FactoryConfig(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=root / "scripts/kstrl/prompt.md",
        prd_file=root / "scripts/kstrl/prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    return config, base, PlainUI(no_color=True)


class TestAManifestWithNoSpecBindsNothing:
    """#260 round 3 (altitude 1). ``ks run`` was dead in any project
    that had ever run ``ks factory``.

    ``Manifest.from_prd`` sets ``spec_file=""``, so round 2's identity
    check refused the register on every ``ks run`` in a decomposed
    project, and told the operator to "re-run the decompose for this
    spec" when ``ks run`` has no spec to decompose. The only exits were
    deleting the register or not using the command.

    ``spec_file == ""`` meant two different things at that call site,
    "no spec" and "wrong spec", and the code read it as the second.
    """

    def _register(self) -> DecisionRegister:
        return DecisionRegister(
            decisions=(_spec_decision(component="main"),),
            project="proj",
            spec_file="spec.md",
            halted=False,
            status=REGISTER_OK,
        )

    def test_a_prd_derived_manifest_binds_nothing_and_does_not_refuse(self) -> None:
        assert bind_register(self._register(), "proj", "") == ()

    def test_the_real_from_prd_manifest_is_the_shape_this_protects(self, tmp_path: Path) -> None:
        """The premise, taken from the constructor rather than assumed."""
        from kstrl.manifest import Manifest

        prd = tmp_path / "prd.json"
        prd.write_text("{}", encoding="utf-8")
        manifest = Manifest.from_prd(prd, "kstrl/auth", base_branch="main")
        assert manifest.spec_file == ""
        assert bind_register(self._register(), manifest.project_name, manifest.spec_file) == ()

    def test_a_spec_derived_manifest_still_gets_the_identity_check(self) -> None:
        with pytest.raises(DecisionRegisterError, match="belongs to project"):
            bind_register(self._register(), "proj", "other.md")


class TestTheSchedulerActuallyInjectsIt:
    """Mutation guard (review round 2).

    Deleting the injection from ``_submit_args`` entirely left all 25
    round-1 decision tests passing. None of them ran the scheduler: the
    one delivery test called ``_run_component`` with the prefix as a
    KEYWORD, so it would also have passed with the 33-element positional
    tuple misaligned. This one drives ``run_factory`` and binds the
    captured positional tuple against the real signature.
    """

    def test_the_block_reaches_the_worker_at_the_right_position(self, tmp_path: Path) -> None:
        import kstrl.factory as factory_mod
        from kstrl.factory import ComponentResult, run_factory

        root = _factory_project(tmp_path, "comp-a")
        write_decisions(
            [
                _spec_decision(
                    issue="encoding",
                    question="what encoding does the reader name",
                    resolution="utf-8, named at every read site",
                    component="comp-a",
                )
            ],
            root_dir=root,
            project_name="proj",
            spec_file="spec.md",
            halted=False,
        )
        signature = inspect.signature(factory_mod._run_component)
        seen: dict[str, Any] = {}

        def capture(*args: Any, **kwargs: Any) -> ComponentResult:
            seen["bound"] = signature.bind(*args, **kwargs)
            return ComponentResult("comp-a", success=True, iterations=1)

        manifest = _one_component_manifest("proj", "spec.md")
        config, base, ui = _factory_inputs(root)
        with (
            patch("kstrl.factory._run_component", side_effect=capture),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(manifest, config, base, ui, root)

        prefix = seen["bound"].arguments["decisions_prefix"]
        assert "## Architect Decisions" in prefix
        assert "what encoding does the reader name" in prefix
        assert "utf-8, named at every read site" in prefix

    def test_a_foreign_register_stops_the_run_before_any_worker(self, tmp_path: Path) -> None:
        """F3 end to end. Project A's register, project B's manifest,
        both with a component called comp-a.

        Round 3: the refusal goes through ``_report_preflight`` and exit
        code 2, the same way the scope and stale-branch refusals do. The
        round-2 shape let ``DecisionRegisterError`` out of
        ``run_factory``, and measured, that reached the operator as a
        traceback and exit 1, so anything keying on 2 for "refused
        before spend" saw a crash instead.
        """
        from kstrl.factory import ComponentResult, run_factory

        root = _factory_project(tmp_path, "comp-a")
        write_decisions(
            [
                _spec_decision(
                    issue="fmt",
                    question="what does the formatter emit",
                    resolution="A-v1",
                    component="comp-a",
                )
            ],
            root_dir=root,
            project_name="project-a",
            spec_file="a.md",
            halted=False,
        )
        from kstrl.ui.plain import PlainUI

        manifest = _one_component_manifest("project-b", "b.md")
        config, base, _ = _factory_inputs(root)
        ui_output = io.StringIO()
        ui = PlainUI(no_color=True, file=ui_output)
        started: list[Any] = []

        def capture(*args: Any, **kwargs: Any) -> ComponentResult:
            started.append(args)
            return ComponentResult("comp-a", success=True, iterations=1)

        with (
            patch("kstrl.factory._run_component", side_effect=capture),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(manifest, config, base, ui, root)
        assert result.exit_code == 2
        assert started == []
        printed = ui_output.getvalue()
        assert "Refusing to run: the architect decision register cannot bind" in printed
        assert "belongs to project" in printed
