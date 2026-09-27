"""The architect's disposition register (#260), on disk and in the factory.

The register is written through ``write_decisions`` and read back with
``read_decisions``: the halt flag and every decision survive the round
trip, an empty register is still written, and a missing file reports
missing while a torn, non-utf8 or malformed-entry file reports UNREADABLE
rather than empty (F1 one layer down: a hand-edited capitalised
disposition is a named fault, never a silent zero).

A ``ks run`` manifest built by ``Manifest.from_prd`` has ``spec_file ==
""`` and binds nothing without refusing (round 3), taken from the real
constructor rather than assumed.

The delivery hop is driven through the real code: ``_run_component``
passes the decisions block into the engineer's context prefix, the
scheduler hands ``run_factory``'s worker the rendered block at the right
positional slot (a mutation guard: deleting the injection from
``_submit_args`` left every round-1 test passing), and a register that
belongs to another project stops the run through ``_report_preflight``
with exit code 2 before any worker starts (F3 end to end).
"""

from __future__ import annotations

import inspect
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.decisions import (
    REGISTER_MISSING,
    REGISTER_OK,
    REGISTER_UNREADABLE,
    SPEC_DECISIONS_REL_PATH,
    DecisionRegister,
    SpecDecision,
    bind_register,
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
            run_id="test-run",
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
    """

    def _register(self) -> DecisionRegister:
        return DecisionRegister(
            decisions=(_spec_decision(component="main"),),
            project="proj",
            spec_file="spec.md",
            halted=False,
            status=REGISTER_OK,
        )

    def test_the_real_from_prd_manifest_is_the_shape_this_protects(self, tmp_path: Path) -> None:
        """The premise, taken from the constructor rather than assumed."""
        from kstrl.manifest import Manifest

        prd = tmp_path / "prd.json"
        prd.write_text("{}", encoding="utf-8")
        manifest = Manifest.from_prd(prd, "kstrl/auth", base_branch="main")
        assert manifest.spec_file == ""
        assert bind_register(self._register(), manifest.project_name, manifest.spec_file) == ()


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
