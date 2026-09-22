"""The [release] section and its gate (R8.7 slice 1, #154).

Pure, no I/O beyond parsing this module's own source for the two
containment guards (T11, T12). Nothing here starts a process or reads
an environment variable, which is the whole containment claim of slice
1 - see ``kstrl/release.py``'s module docstring.
"""

from __future__ import annotations

import ast
import itertools
from dataclasses import replace

from kstrl import release
from kstrl.manifest import Component
from tests.helpers.astwalk import REPO_ROOT
from tests.helpers.proclifecycle import process_primitive_spellings
from tests.test_journal_one_writer import parsed


def _component(comp_id: str, merge_sha: str, completed_at: str) -> Component:
    return Component(
        id=comp_id,
        title=comp_id.upper(),
        description="",
        dependencies=[],
        prd_path=f"scripts/kstrl/feature/{comp_id}/prd.json",
        branch_name=f"kstrl/factory/{comp_id}",
        merge_sha=merge_sha,
        completed_at=completed_at,
    )


_FULLY_PERMITTED = release.ReleaseInputs(
    release_enabled=True,
    environment="prod",
    run_clean=True,
    release_ref="c" * 40,
    policy_enabled=True,
    policy_deploy=True,
    ladder_deploy_permitted=None,
)


def _inputs(**overrides: object) -> release.ReleaseInputs:
    return replace(_FULLY_PERMITTED, **overrides)  # type: ignore[arg-type]


class TestReleaseWithheldVocabulary:
    def test_every_input_combination_yields_a_reason_from_the_vocabulary(self) -> None:
        for (
            release_enabled,
            policy_enabled,
            policy_deploy,
            run_clean,
            environment,
            ladder_deploy_permitted,
            release_ref,
        ) in itertools.product(
            (True, False),
            (True, False),
            (True, False),
            (True, False),
            ("", "prod"),
            (None, True, False),
            ("", "c" * 40),
        ):
            inputs = release.ReleaseInputs(
                release_enabled=release_enabled,
                environment=environment,
                run_clean=run_clean,
                release_ref=release_ref,
                policy_enabled=policy_enabled,
                policy_deploy=policy_deploy,
                ladder_deploy_permitted=ladder_deploy_permitted,
            )
            reason = release.release_withheld(inputs)
            assert reason != ""
            assert reason in release.RELEASE_WITHHELD_REASONS

    def test_every_reason_in_the_vocabulary_is_produced_by_some_input(self) -> None:
        produced: set[str] = set()
        for (
            release_enabled,
            policy_enabled,
            policy_deploy,
            run_clean,
            environment,
            ladder_deploy_permitted,
            release_ref,
        ) in itertools.product(
            (True, False),
            (True, False),
            (True, False),
            (True, False),
            ("", "prod"),
            (None, True, False),
            ("", "c" * 40),
        ):
            inputs = release.ReleaseInputs(
                release_enabled=release_enabled,
                environment=environment,
                run_clean=run_clean,
                release_ref=release_ref,
                policy_enabled=policy_enabled,
                policy_deploy=policy_deploy,
                ladder_deploy_permitted=ladder_deploy_permitted,
            )
            produced.add(release.release_withheld(inputs))
        assert produced == set(release.RELEASE_WITHHELD_REASONS)


class TestReleaseWithheldDirections:
    def test_policy_disabled_withholds_even_when_deploy_is_true(self) -> None:
        assert release.release_withheld(_inputs(policy_enabled=False)) == "policy_disabled"

    def test_an_absent_environment_withholds(self) -> None:
        assert release.release_withheld(_inputs(environment="")) == "environment_unset"

    def test_a_fully_permitted_run_still_withholds_for_no_driver(self) -> None:
        assert release.release_withheld(_inputs()) == "no_driver"


class TestReleaseRefFrom:
    def test_the_release_ref_is_the_last_merge_and_a_tie_goes_to_the_later_component(
        self,
    ) -> None:
        components = [
            _component("no-merge", merge_sha="", completed_at="2026-01-03T00:00:00Z"),
            _component("first", merge_sha="a" * 40, completed_at="2026-01-01T00:00:00Z"),
            _component("tie-earlier", merge_sha="b" * 40, completed_at="2026-01-02T00:00:00Z"),
            _component("tie-later", merge_sha="c" * 40, completed_at="2026-01-02T00:00:00Z"),
        ]
        assert release.release_ref_from(components) == "c" * 40
        assert release.release_ref_from([]) == ""
        assert release.release_ref_from([components[0]]) == ""


class TestReleaseModuleContainment:
    def test_the_release_module_spells_no_process_primitive(self) -> None:
        """T11. RUN the guard rather than reading a pin: the driver
        slice adds a row here, this slice must add none."""
        source_file = REPO_ROOT / "kstrl" / "release.py"
        assert source_file.is_file()
        spellings = process_primitive_spellings(parsed(source_file))
        assert spellings == frozenset(), (
            f"kstrl/release.py spells a process primitive: {sorted(spellings)}. "
            "Slice 1 has no driver; the driver slice must add its own "
            "EXPECTED_PROCESS_MODULES row rather than widen this guard."
        )

    def test_the_release_module_reads_no_environment_variable(self) -> None:
        """T12. Slice 1's containment claim is that no environment
        variable can switch the release stage on.

        Checks for the OS-access spellings ``os.environ`` / ``os.getenv``
        rather than the bare substrings "environ"/"getenv": this module's
        own ``[release] environment`` field (the deploy environment,
        e.g. "staging") legitimately contains "environ" as a substring
        of "environment", so a bare substring check would flag the field
        name it is required to have. ``import os`` is refused outright
        by the walk above, which is what actually closes the door -
        this text check is the second, narrower layer over the exact
        access shape Plant 11 adds.
        """
        source_file = REPO_ROOT / "kstrl" / "release.py"
        tree = parsed(source_file)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "os" for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module != "os"
        source_text = source_file.read_text(encoding="utf-8")
        assert "os.environ" not in source_text
        assert "os.getenv" not in source_text
