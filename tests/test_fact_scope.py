"""Facts reach later manifests, found by the paths their evidence cites (#517).

Part of #217, slice 3. Before this, ``build_knowledge_context`` read only
the directories named by the CURRENT manifest's component ids, so a fact
written under one manifest never reached a component of the next one:
#453 D5 measured 28 facts on disk and 0 reachable from slice 2.

Every test here drives the real retrieval entry point over a real
knowledge store written by the real writer, and reads the rendered prefix
back through ``_extract_prefix_claims``, the same parser the
fact-utilization metric uses, so a claim is asserted in the tier the
engineer is actually shown it under.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import factory as factory_mod
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.knowledge import (
    TIER_CORE,
    TIER_DEPENDENCY,
    TIER_SIBLING,
    Fact,
    KnowledgeConfig,
    _extract_prefix_claims,
    build_knowledge_context,
    write_facts,
)
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers.component_prd import write_component_prd

#: Read before any test patches ``_run_component``: a patched name has
#: the mock's ``(*args, **kwargs)`` signature, which binds nothing.
_RUN_COMPONENT = inspect.signature(factory_mod._run_component)


def _fact(fact_id: str, owner: str, claim: str, evidence: list[str]) -> Fact:
    return Fact(
        id=fact_id,
        component_id=owner,
        created_iter=1,
        created_run_id="factory-20260901-120000.000000-seed",
        scope="contract",
        evidence=evidence,
        confidence="review_passed",
        claim=claim,
    )


def _store(root: Path, owner: str, facts: list[Fact]) -> None:
    """Write through the real writer, under the layout every fact on disk has."""
    written = write_facts(facts, root, owner, "factory-20260901-120000.000000-seed")
    assert written == len(facts)


def _component(component_id: str, dependencies: list[str] | None = None) -> Component:
    return Component(
        id=component_id,
        title=component_id,
        description="",
        dependencies=dependencies or [],
        prd_path=f"scripts/kstrl/feature/{component_id}/prd.json",
        branch_name=f"kstrl/{component_id}",
    )


def _manifest(*components: Component) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=list(components),
    )


def _config(root: Path) -> KnowledgeConfig:
    return KnowledgeConfig(enabled=True, knowledge_root=root)


def _tiers(prefix: str) -> dict[str, list[str]]:
    """Claims by the tier heading they were rendered under."""
    out: dict[str, list[str]] = {TIER_CORE: [], TIER_DEPENDENCY: [], TIER_SIBLING: []}
    for claim, tier in _extract_prefix_claims(prefix):
        out.setdefault(tier, []).append(claim)
    return out


def _touch(worktree: Path, *rels: str) -> None:
    for rel in rels:
        path = worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")


def test_fact_from_an_earlier_manifest_reaches_a_new_component(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    _store(
        root,
        "server-api",
        [_fact("fact-001", "server-api", "Errors end with LL-417.", ["src/api.py:3"])],
    )
    first = _manifest(_component("server-api"))
    assert build_knowledge_context(first, first.components[0], root, _config(root))

    second = _manifest(_component("client-http"))
    prefix = build_knowledge_context(second, second.components[0], root, _config(root))

    assert _tiers(prefix)[TIER_SIBLING] == ["Errors end with LL-417."]


def test_fact_citing_a_path_in_scope_is_core(tmp_path: Path) -> None:
    root, worktree = tmp_path / "knowledge", tmp_path / "wt"
    _touch(worktree, "src/app/models.py", "src/other/y.py")
    _store(
        root,
        "old-app",
        [
            _fact("fact-001", "old-app", "Models are frozen.", ["src/app/models.py:3-9"]),
            _fact("fact-002", "old-app", "Other is unrelated.", ["src/other/y.py:1"]),
        ],
    )
    manifest = _manifest(_component("app"))

    prefix = build_knowledge_context(
        manifest,
        manifest.components[0],
        root,
        _config(root),
        allowed_paths=["src/app/"],
        dependency_paths={},
        worktree=worktree,
    )

    tiers = _tiers(prefix)
    assert tiers[TIER_CORE] == ["Models are frozen."]
    assert tiers[TIER_SIBLING] == ["Other is unrelated."]


def test_fact_citing_a_dependency_path_is_dependency(tmp_path: Path) -> None:
    root, worktree = tmp_path / "knowledge", tmp_path / "wt"
    _touch(worktree, "src/lib/z.py")
    _store(
        root, "old-lib", [_fact("fact-001", "old-lib", "Lib parses lazily.", ["src/lib/z.py:4"])]
    )
    manifest = _manifest(_component("lib"), _component("cli", ["lib"]))

    prefix = build_knowledge_context(
        manifest,
        manifest.components[1],
        root,
        _config(root),
        allowed_paths=["src/cli/"],
        dependency_paths={"lib": ["src/lib/"]},
        worktree=worktree,
    )

    assert _tiers(prefix)[TIER_DEPENDENCY] == ["Lib parses lazily."]


def test_fact_whose_paths_are_all_gone_is_dropped(tmp_path: Path) -> None:
    root, worktree = tmp_path / "knowledge", tmp_path / "wt"
    _touch(worktree, "src/kept.py")
    _store(
        root,
        "old-comp",
        [
            _fact(
                "fact-001", "old-comp", "Gone code did X.", ["src/gone.py:1", "src/also_gone.py:2"]
            ),
            _fact(
                "fact-002",
                "old-comp",
                "Half the evidence remains.",
                ["src/gone.py:1", "src/kept.py:2"],
            ),
        ],
    )
    manifest = _manifest(_component("new"))

    prefix = build_knowledge_context(
        manifest,
        manifest.components[0],
        root,
        _config(root),
        allowed_paths=None,
        dependency_paths={},
        worktree=worktree,
    )

    assert "Gone code did X." not in prefix
    assert _tiers(prefix)[TIER_SIBLING] == ["Half the evidence remains."]


def test_this_manifests_facts_are_kept_before_their_code_lands(tmp_path: Path) -> None:
    """Under ``--no-prs`` a dependent's worktree is cut from base without
    its dependency's files. Those facts are the only thing the dependent
    learns about the dependency, so the stale filter is for facts from
    OUTSIDE this manifest, never for this manifest's own components."""
    root, worktree = tmp_path / "knowledge", tmp_path / "wt"
    worktree.mkdir()
    _store(
        root, "api", [_fact("fact-001", "api", "The API pages by cursor.", ["src/api/server.py:1"])]
    )
    manifest = _manifest(_component("api"), _component("cli", ["api"]))

    prefix = build_knowledge_context(
        manifest,
        manifest.components[1],
        root,
        _config(root),
        allowed_paths=None,
        dependency_paths={},
        worktree=worktree,
    )

    assert _tiers(prefix)[TIER_DEPENDENCY] == ["The API pages by cursor."]


def test_path_match_is_by_part_not_by_prefix(tmp_path: Path) -> None:
    root, worktree = tmp_path / "knowledge", tmp_path / "wt"
    _touch(worktree, "src/application/x.py", "src/app/y.py", "src/other/z.py")
    _store(
        root,
        "old-comp",
        [
            _fact("fact-001", "old-comp", "Application is elsewhere.", ["src/application/x.py:1"]),
            _fact("fact-002", "old-comp", "App owns y.", ["src/app/y.py:1"]),
            # Starts with src/app but climbs out of it: never inside.
            _fact("fact-003", "old-comp", "Dots climb out.", ["src/app/../other/z.py:1"]),
        ],
    )
    manifest = _manifest(_component("app"))

    prefix = build_knowledge_context(
        manifest,
        manifest.components[0],
        root,
        _config(root),
        allowed_paths=["src/app"],
        dependency_paths={},
        worktree=worktree,
    )

    tiers = _tiers(prefix)
    assert tiers[TIER_CORE] == ["App owns y."]
    assert sorted(tiers[TIER_SIBLING]) == ["Application is elsewhere.", "Dots climb out."]


def test_missing_allowed_paths_matches_by_id_only(tmp_path: Path) -> None:
    root, worktree = tmp_path / "knowledge", tmp_path / "wt"
    _touch(worktree, "src/app/y.py", "src/own.py")
    _store(
        root, "old-comp", [_fact("fact-001", "old-comp", "Old comp wrote y.", ["src/app/y.py:1"])]
    )
    _store(root, "app", [_fact("fact-001", "app", "App keeps its own.", ["src/own.py:1"])])
    manifest = _manifest(_component("app"))

    prefix = build_knowledge_context(
        manifest,
        manifest.components[0],
        root,
        _config(root),
        allowed_paths=None,
        dependency_paths={},
        worktree=worktree,
    )

    tiers = _tiers(prefix)
    assert tiers[TIER_CORE] == ["App keeps its own."]
    assert tiers[TIER_SIBLING] == ["Old comp wrote y."]


def test_same_fact_id_in_two_components_are_two_facts(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    _store(root, "old-a", [_fact("fact-001", "old-a", "A says one thing.", ["src/a.py:1"])])
    _store(root, "old-b", [_fact("fact-001", "old-b", "B says another.", ["src/b.py:1"])])
    manifest = _manifest(_component("new"))

    prefix = build_knowledge_context(manifest, manifest.components[0], root, _config(root))

    assert sorted(_tiers(prefix)[TIER_SIBLING]) == ["A says one thing.", "B says another."]


def _run_factory_capturing(
    root: Path,
    components: list[Component],
    run_wide_allowed_paths: list[str] | None = None,
) -> dict[str, str]:
    """Drive the real ``run_factory`` and return each component's prefix.

    Only ``_run_component`` (the engineer), ``distill_facts`` and
    ``git.get_diff_content`` are patched, so no ``claude`` or ``codex``
    binary is needed. The prefix is read from the argument the scheduler
    hands the engineer, bound against the real signature.
    """
    (root / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "kstrl" / "prompt.md").write_text("test prompt", encoding="utf-8")
    write_component_prd(root, "scripts/kstrl/prd.json")
    seen: dict[str, str] = {}

    def capture(*args: Any, **kwargs: Any) -> ComponentResult:
        bound = _RUN_COMPONENT.bind_partial(*args)
        comp_id = bound.arguments["component_id"]
        seen[comp_id] = bound.arguments["knowledge_prefix"]
        return ComponentResult(comp_id, success=True, iterations=1)

    base = KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
        allowed_paths=list(run_wide_allowed_paths or []),
    )
    factory_config = FactoryConfig(
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
    with (
        patch("kstrl.factory._run_component", side_effect=capture),
        patch("kstrl.git.get_diff_content", return_value=""),
        patch("kstrl.factory.distill_facts", return_value=(0, "none", False)),
    ):
        run_factory(_manifest(*components), factory_config, base, PlainUI(no_color=True), root)
    missing = [comp.id for comp in components if comp.id not in seen]
    assert not missing, f"the scheduler never submitted {missing}"
    return seen


def test_factory_places_an_earlier_manifests_fact_by_the_components_prd_paths(
    tmp_path: Path,
) -> None:
    """End to end through ``run_factory``: the PRD's ``allowedPaths``
    and the worktree reach retrieval, and the prefix the engineer is
    handed carries the earlier manifest's fact in the core tier and not
    the fact whose code is gone."""
    root = tmp_path
    comp = _component("cli")
    write_component_prd(root, comp.prd_path, allowed_paths=["src/cli/"])
    _touch(root, "src/cli/main.py")
    _store(
        root / ".kstrl" / "knowledge",
        "old-cli",
        [
            _fact("fact-001", "old-cli", "The CLI exits 2 on bad input.", ["src/cli/main.py:1-4"]),
            _fact("fact-002", "old-cli", "A removed module did Y.", ["src/removed.py:1"]),
        ],
    )

    prefix = _run_factory_capturing(root, [comp])["cli"]

    assert _tiers(prefix)[TIER_CORE] == ["The CLI exits 2 on bad input."]
    assert "A removed module did Y." not in prefix


def test_factory_places_a_fact_by_a_dependencys_prd_paths(tmp_path: Path) -> None:
    """End to end: a dependency's PRD-authored ``allowedPaths`` reach
    retrieval, so an earlier manifest's fact citing the dependency's code
    is in the dependent's dependency tier."""
    root = tmp_path
    lib, cli = _component("lib"), _component("cli", ["lib"])
    write_component_prd(root, lib.prd_path, allowed_paths=["src/lib/"])
    write_component_prd(root, cli.prd_path, allowed_paths=["src/cli/"])
    _touch(root, "src/lib/z.py")
    _store(
        root / ".kstrl" / "knowledge",
        "old-lib",
        [_fact("fact-001", "old-lib", "Lib parses lazily.", ["src/lib/z.py:4"])],
    )

    prefixes = _run_factory_capturing(root, [lib, cli])

    assert _tiers(prefixes["cli"])[TIER_DEPENDENCY] == ["Lib parses lazily."]
    assert _tiers(prefixes["lib"])[TIER_CORE] == ["Lib parses lazily."]


def test_run_wide_allowed_paths_match_by_id_only(tmp_path: Path) -> None:
    """End to end: a run-wide ``--allowed-paths`` / ``[paths] allowed``
    list is the same for every component, so it says nothing about which
    component owns a path. With no PRD-authored list, an earlier
    manifest's fact is a sibling even when it cites a path inside the
    run-wide list."""
    root = tmp_path
    comp = _component("cli")
    write_component_prd(root, comp.prd_path)
    _touch(root, "src/cli/main.py")
    _store(
        root / ".kstrl" / "knowledge",
        "old-cli",
        [_fact("fact-001", "old-cli", "The CLI exits 2 on bad input.", ["src/cli/main.py:1-4"])],
    )

    prefix = _run_factory_capturing(root, [comp], run_wide_allowed_paths=["src/"])["cli"]

    tiers = _tiers(prefix)
    assert tiers[TIER_CORE] == []
    assert tiers[TIER_SIBLING] == ["The CLI exits 2 on bad input."]
