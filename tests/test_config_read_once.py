"""A run resolves its config envelope once, and Phase 1 enforces that (#192).

``manifest.policy_hash`` is the audit record of what merge guardrails a
run was held to. Before this file it could disagree with what the run
actually enforced: the hash was taken once at run start while Phase 1
re-read ``[policy]``, ``[adequacy]`` and ``[autonomy]`` from
``kstrl.toml`` for every component. Measured at ``414d662`` on a
two-component run with the ladder off, which is the default: component B
was held to ``max_files_changed=500, deps_allow_new=true`` against a
manifest recording the hash of ``5`` and ``false``, the adequacy posture
flipped from enabled to disabled with nothing recording either posture,
and a malformed mid-run edit raised out of ``_phase_verify`` and aborted
the run.

The guard has two layers, both FLAGGING. Layer A pins the config surface
by discovering it, so a new config dataclass is an unexplained census
delta rather than a hole in a list. Layer B pins WHERE in
``kstrl/pipeline.py`` that surface may be called, and pins the number of
call sites it found beside it: ``assert offenders == []`` alone passes
both when the pipeline is clean and when the walk has been switched off,
which is the same shape as the defect.

Out of scope by construction, and stated rather than left implicit:
``kstrl/serve.py`` re-reads config per poll and per queue item. The
daemon admits and the ``ks factory`` CHILD enforces, and that child
records its own hash from its own resolution in its own process, so the
unit of "one config resolution" is the ``ks factory`` process rather
than the daemon's lifetime. ``resolve_merge_gate`` narrows a merge gate
milliseconds before the child re-resolves it; closing that window means
passing resolved config across a process boundary, which is #193.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kstrl.autonomy import AutonomyState
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, run_factory
from kstrl.manifest import Manifest
from kstrl.pipeline import ComponentPipeline
from kstrl.policy import PolicyConfig
from kstrl.runenvelope import RunEnvelope
from kstrl.ui.plain import PlainUI
from tests.helpers import astwalk, configwalk
from tests.helpers.astwalk import KSTRL_PACKAGE, package_sources
from tests.helpers.verify_phase import component, phase_verify_envelopes

# --- layer A: the config surface, discovered rather than listed ----------

#: Every class in ``kstrl/`` that reads config off disk, and the methods
#: of it that do. RE-DERIVED by running the walk below; never compared
#: against a literal at two revisions, because an identical pin is also
#: what a stale pin looks like (PR #341).
EXPECTED_SURFACE_CLASSES: dict[str, frozenset[str]] = {
    name: frozenset({"load"})
    for name in (
        "AdequacyConfig",
        "AutonomyConfig",
        "BreakerConfig",
        "ContractConfig",
        "DivergenceConfig",
        "EvolutionConfig",
        "FactoryConfig",
        "FeedforwardConfig",
        "FixturesConfig",
        "GitHubIntakeConfig",
        "InboxConfig",
        "KnowledgeConfig",
        "KstrlConfig",
        "LinearConfig",
        "NotifyConfig",
        "PolicyConfig",
        "QueueConfig",
        "SandboxConfig",
        "SecurityConfig",
        "ServeConfig",
        "TimeoutConfig",
        "VerifyConfig",
    )
}

#: The readers that own no class. ``load_toml_section`` is the primitive
#: one layer up from the file; ``run`` is the ``ks run`` command body.
#: ``resolve_or_report`` replaced ``load_or_report`` in this set when
#: #192 split the scope-owning wrapper off the resolution: the wrapper
#: no longer calls a primitive itself, so it is no longer surface.
EXPECTED_FREE_READERS = frozenset(
    {
        "_apply_toml_overrides",
        "_blamed_toml_value",
        "_masthead",
        "build_config_report",
        "collect_config_problems",
        "load_toml_section",
        "resolve_or_report",
        "run",
    }
)

#: ``kstrl/factory.py`` resolves run-level config in exactly two scopes.
#: ``_submit_args`` and every other per-component scope must not appear:
#: the scheduler calls them once per component attempt, which is the
#: same divergence the pipeline half is about. Nested scopes qualify
#: through their owner, so a read planted in ``_submit_args`` reads
#: ``_run_factory_locked._submit_args`` and is an offender.
EXPECTED_FACTORY_SCOPES = frozenset({"_run_factory_locked", "_open_health_breach_items"})
EXPECTED_FACTORY_SITES = 7

#: The sections ``RunEnvelope.resolve`` names, as the walk sees them.
#: Pinned by REFERENCE, because the envelope hands each loader to
#: ``config_preflight.resolve_or_report`` rather than calling it, and a
#: guard that looks for calls sees none of them -
#: ``tests/test_inbox_write_guards.py``'s config-load inventory is one
#: such guard and says so. Dropping a section from the envelope puts it
#: back on a per-component or per-constructor read, which is the whole
#: defect, so it fails here.
EXPECTED_ENVELOPE_SECTIONS = frozenset(
    {
        "AdequacyConfig.load",
        "AutonomyConfig.load",
        "DivergenceConfig.load",
        "FixturesConfig.load",
        "InboxConfig.load",
        "PolicyConfig.load",
        "SandboxConfig.load",
    }
)

#: ``kstrl/serve.py`` is outside the run envelope by construction (the
#: daemon admits, the ``ks factory`` child enforces and records its own
#: hash). Pinned by EQUALITY rather than ``>=``: the number is the
#: control for the walk, and a daemon read that disappeared is worth the
#: same failure as one that appeared.
EXPECTED_SERVE_SITES = 10

_NEW_CONFIG_CLASS = """
class WidgetConfig:
    @classmethod
    def load(cls, root_dir):
        section = load_toml_section(resolve_config_file(root_dir), "widget")
        return cls()
"""

_PER_PHASE_READ = """
class ComponentPipeline:
    def __init__(self, root_dir):
        self.sandbox_config = SandboxConfig.load(root_dir)

    def _phase_verify(self, comp):
        return PolicyConfig.load(self.root_dir)
"""


class TestConfigSurface:
    """Layer A. Enumerates no class name of its own."""

    def test_the_surface_is_the_pinned_one(self) -> None:
        found = configwalk.surface(package_sources())
        assert found.classes == EXPECTED_SURFACE_CLASSES, (
            "the set of config classes in kstrl/ moved. A new one is not "
            "wrong, but it is a new place a run can resolve config from, "
            "so decide whether it belongs in RunEnvelope before pinning "
            f"it here. Found {sorted(found.classes)}"
        )
        assert found.free == EXPECTED_FREE_READERS, (
            f"the free config readers in kstrl/ moved. Found {sorted(found.free)}"
        )

    def test_the_attribution_loses_no_primitive_call(self) -> None:
        """The census control for layer A.

        ``own_nodes`` stops at a nested function and at a lambda, so a
        primitive called from inside one would be invisible to the
        per-scope walk and would silently shrink the surface. Measured
        against the same count taken over the whole module: today they
        agree, and the day they stop agreeing this says so instead of
        reporting a smaller surface.
        """
        found = configwalk.surface(package_sources())
        assert found.attributed == found.raw, (
            "a call to a parse primitive was found in kstrl/ that the "
            "per-scope walk could not attribute to a scope, so the "
            "surface above is under-reported by "
            f"{found.raw - found.attributed}."
        )
        assert found.raw > 0, "the primitive net matched nothing in kstrl/"

    def test_the_walk_sees_a_config_class_it_was_never_told_about(self, tmp_path: Path) -> None:
        """Layer A's positive control, and its mutation.

        A guard you did not mutate is a guard you did not test, per
        LAYER. This plants a config class the pin has never heard of and
        asserts the walk discovers it, which is what makes the pinned
        surface an assertion about kstrl/ rather than about a list.
        """
        planted = tmp_path / "widget.py"
        planted.write_text(_NEW_CONFIG_CLASS, encoding="utf-8")
        found = configwalk.surface([planted])
        assert found.classes == {"WidgetConfig": frozenset({"load"})}

    def test_dropping_a_primitive_shrinks_the_surface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The primitive set is load-bearing, not decoration.

        With ``resolve_config_file`` dropped the walk must find strictly
        less. A primitive set that changes nothing is a primitive set
        that was never consulted.
        """
        full = configwalk.surface(package_sources())
        monkeypatch.setattr(
            configwalk,
            "PARSE_PRIMITIVES",
            frozenset({"load_toml_document"}),
        )
        narrowed = configwalk.surface(package_sources())
        assert narrowed.raw < full.raw
        assert set(narrowed.classes) < set(full.classes)

    def test_a_primitive_inside_a_lambda_is_attributed(self, tmp_path: Path) -> None:
        """The lambda half of layer A, planted.

        ``own_nodes`` stops at a lambda and ``astwalk.scopes`` does not
        enumerate one, so before ``configwalk.scopes_with_lambdas`` a
        primitive called inside one was counted by the raw census and
        attributed to no scope - which the control above would have
        caught - or, for a surface CLASS rather than a primitive,
        counted by neither and caught by nothing. Both halves now see
        it.
        """
        planted = tmp_path / "lambda_reader.py"
        planted.write_text(
            "from kstrl.config import load_toml_section, resolve_config_file\n\n\n"
            "def build(root):\n"
            '    return (lambda: load_toml_section(resolve_config_file(root), "x"))()\n',
            encoding="utf-8",
        )
        found = configwalk.surface([planted])
        assert found.raw == 2
        assert found.attributed == 2
        assert found.free == frozenset({"build.<lambda>"})

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_module_qualified_primitive_is_invisible(self, tmp_path: Path) -> None:
        """The disclosed limit in ``configwalk``'s docstring, with a test
        behind it rather than a paragraph on its own.

        Layer 2 resolves a surface reference through
        ``astwalk.bindings``; layer 1 still matches a PRIMITIVE by bare
        ``Name`` only, so ``c.load_toml_section`` after ``import
        kstrl.config as c`` enrols nothing and both the raw and the
        attributed census stay at zero together. Measured in ``kstrl/``
        today: 54 bare-name primitive calls, 0 in the attribute form, so
        this is latent. Under ``strict=True`` so that teaching layer 1
        the resolver XPASSes here and forces the disclosure to be edited
        in the same diff.
        """

        def probe(source: str) -> object:
            planted = tmp_path / "qualified.py"
            planted.write_text(source, encoding="utf-8")
            # The COUNT, not the Surface: a dataclass is truthy whatever
            # it holds, so returning one would XPASS this row on a walk
            # that saw nothing at all.
            return configwalk.surface([planted]).raw

        astwalk.blind_spot(
            probe,
            "import kstrl.config as c\n\n\ndef build(root):\n"
            '    return c.load_toml_section(c.resolve_config_file(root), "x")\n',
        )


class TestTheRunReadsConfigOnlyBeforeItStarts:
    """Layer B. Scoped to the three modules a run's config can be read
    from, which is why a per-poll read in ``kstrl/serve.py`` is outside
    the offender rule by construction rather than by an exemption
    somebody has to remember."""

    def test_the_pipeline_reads_no_config_at_all(self) -> None:
        found = configwalk.surface(package_sources())
        sites = configwalk.read_sites(KSTRL_PACKAGE / "pipeline.py", found)
        assert sites == [], (
            "kstrl/pipeline.py resolves config. Its phases run per "
            "component attempt, so a read there is a second answer the "
            "run's recorded policy hash does not cover, and a read in "
            "__init__ raises where nothing can report it (#192). The "
            f"envelope is injected instead. Offenders: {sites}"
        )

    def test_the_envelope_names_every_section_the_run_enforces(self) -> None:
        """The seven sections, pinned where the walk can see them.

        ``AutonomyState`` is not here: it reads ``.kstrl/autonomy.json``
        rather than ``kstrl.toml``, so no config walk covers it and
        ``TestRunEnvelope`` drives it instead.
        """
        found = configwalk.surface(package_sources())
        sites = configwalk.read_sites(KSTRL_PACKAGE / "runenvelope.py", found)
        named = {site.target for site in sites if site.scope == "RunEnvelope.resolve"}
        assert named == EXPECTED_ENVELOPE_SECTIONS, (
            "the run envelope's sections moved. A section that leaves it "
            "goes back to being resolved per component or inside a "
            f"constructor that cannot report a bad one (#192). Found {sorted(named)}"
        )

    def test_every_factory_config_read_is_run_level(self) -> None:
        """The half the pipeline scoping cannot see.

        ``_submit_args`` and ``_run_component`` run once per component
        attempt inside ``kstrl/factory.py``, and a ``PolicyConfig.load``
        planted at the top of ``_submit_args`` passed this whole file
        before layer B walked factory.py.
        """
        found = configwalk.surface(package_sources())
        sites = configwalk.read_sites(KSTRL_PACKAGE / "factory.py", found)
        offenders = [site for site in sites if site.scope not in EXPECTED_FACTORY_SCOPES]
        assert offenders == [], (
            "kstrl/factory.py resolves config outside its run-level "
            f"scopes {sorted(EXPECTED_FACTORY_SCOPES)}. Offenders: {offenders}"
        )

    @pytest.mark.parametrize(
        ("module", "expected"),
        [
            ("factory.py", EXPECTED_FACTORY_SITES),
            ("serve.py", EXPECTED_SERVE_SITES),
        ],
    )
    def test_the_site_count_is_pinned(self, module: str, expected: int) -> None:
        """The control for both assertions above.

        An empty offender list is what a clean module returns AND what a
        walk that stopped looking returns; ``pipeline.py`` returns zero
        SITES, so it cannot be its own control at all. These two can:
        both counts are non-zero, both are walked by the same call in
        the same run, and a walk that went blind takes them to zero.
        """
        found = configwalk.surface(package_sources())
        sites = configwalk.read_sites(KSTRL_PACKAGE / module, found)
        assert len(sites) == expected, (
            f"the number of config reads in kstrl/{module} moved. If it "
            "fell to 0 the walk stopped seeing them, which is the shape "
            f"the offender assertions cannot distinguish from clean. Found {sites}"
        )

    @pytest.mark.parametrize(
        ("shape", "source"),
        [
            (
                "spelled",
                "from kstrl.policy import PolicyConfig\n\n\nclass ComponentPipeline:\n"
                "    def _phase_verify(self, comp):\n"
                "        return PolicyConfig.load(self.root_dir)\n",
            ),
            (
                "module-qualified",
                "import kstrl.policy as mod\n\n\nclass ComponentPipeline:\n"
                "    def _phase_verify(self, comp):\n"
                "        return mod.PolicyConfig.load(self.root_dir)\n",
            ),
            (
                "aliased import",
                "from kstrl.policy import PolicyConfig as PC\n\n\nclass ComponentPipeline:\n"
                "    def _phase_verify(self, comp):\n"
                "        return PC.load(self.root_dir)\n",
            ),
            (
                "getattr",
                "from kstrl.policy import PolicyConfig\n\n\nclass ComponentPipeline:\n"
                "    def _phase_verify(self, comp):\n"
                '        return getattr(PolicyConfig, "load")(self.root_dir)\n',
            ),
            (
                "partial",
                "from functools import partial\nfrom kstrl.policy import PolicyConfig\n\n\n"
                "class ComponentPipeline:\n"
                "    def _phase_verify(self, comp):\n"
                "        return partial(PolicyConfig.load, self.root_dir)()\n",
            ),
            (
                "bound name",
                "from kstrl.policy import PolicyConfig\n\n\nclass ComponentPipeline:\n"
                "    def _phase_verify(self, comp):\n"
                "        reader = PolicyConfig.load\n"
                "        return reader(self.root_dir)\n",
            ),
            (
                "inside a lambda",
                "from kstrl.security import SecurityConfig\n\n\nclass ComponentPipeline:\n"
                "    def _phase_verify(self, comp):\n"
                "        return (lambda: SecurityConfig.load(self.root_dir))()\n",
            ),
            (
                "comprehension",
                "from kstrl.policy import PolicyConfig\n\n\nclass ComponentPipeline:\n"
                "    def _phase_verify(self, comp):\n"
                "        return [PolicyConfig.load(self.root_dir) for _ in range(1)]\n",
            ),
        ],
    )
    def test_the_walk_flags_a_per_phase_read_however_it_is_written(
        self, tmp_path: Path, shape: str, source: str
    ) -> None:
        """Layer B's mutation, once per call shape.

        The review measured five of these eight passing unnoticed. The
        lambda is the one that mattered most: it was invisible to this
        layer AND left layer A's ``attributed == raw`` census untouched,
        so both halves of the guard went quiet at the same time.
        """
        planted = tmp_path / "pipeline.py"
        planted.write_text(source, encoding="utf-8")
        found = configwalk.surface(package_sources())
        sites = configwalk.read_sites(planted, found)
        assert sites != [], f"the {shape} shape is invisible to layer B"
        assert all(site.scope.startswith("ComponentPipeline._phase_verify") for site in sites)


# --- the behaviour the guard is about ------------------------------------

_BEFORE = """\
[policy]
enabled = true
max_files_changed = 5
max_lines_changed = 100
deps_allow_new = false

[adequacy]
enabled = true
min_coverage_pct = 90

[autonomy]
enabled = {autonomy}
"""

_AFTER = """\
[policy]
enabled = true
max_files_changed = 500
max_lines_changed = 100000
deps_allow_new = true

[adequacy]
enabled = false
min_coverage_pct = 0

[autonomy]
enabled = {autonomy}
"""

_MALFORMED = """\
[policy]
enabled = true
max_files_changed = "two"

[autonomy]
enabled = false
"""


def _edit(root: Path, body: str) -> Callable[[], None]:
    """The operator editing kstrl.toml while the run is in flight."""

    def write() -> None:
        (root / "kstrl.toml").write_text(body, encoding="utf-8")

    return write


class TestAMidRunEditDoesNotChangeWhatIsEnforced:
    @pytest.mark.parametrize("autonomy", ["false", "true"])
    def test_the_policy_envelope_is_the_recorded_one(self, tmp_path: Path, autonomy: str) -> None:
        """Measured at 414d662 with ``enabled = false``, which is the
        default: two distinct envelopes enforced in one run, and the
        second was not the one the hash records."""
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy=autonomy))
        envelope = RunEnvelope.load(tmp_path)
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=_edit(tmp_path, _AFTER.format(autonomy=autonomy)),
            run_envelope=envelope,
        )

        assert [r.component for r in readings] == ["comp-a", "comp-b"]
        assert {r.policy.envelope_hash() for r in readings} == {envelope.policy_hash()}
        assert {r.policy.max_files_changed for r in readings} == {5}
        assert {r.policy.deps_allow_new for r in readings} == {False}

    @pytest.mark.parametrize("autonomy", ["false", "true"])
    def test_the_adequacy_posture_is_the_one_the_run_started_with(
        self, tmp_path: Path, autonomy: str
    ) -> None:
        """AdequacyConfig has no envelope hash and is in no artifact, so
        at 414d662 the posture a component was judged under flipped with
        nothing anywhere recording that it had."""
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy=autonomy))
        envelope = RunEnvelope.load(tmp_path)
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=_edit(tmp_path, _AFTER.format(autonomy=autonomy)),
            run_envelope=envelope,
        )

        assert {r.adequacy.enabled for r in readings} == {True}

    def test_a_malformed_mid_run_edit_does_not_abort_the_run(self, tmp_path: Path) -> None:
        """The failure mode is DELETED, not handled.

        At 414d662 an edit the entry preflight would have rejected
        reached a per-component load with nothing in front of it and
        raised ``ValueError`` out of ``_phase_verify``; its caller
        ``process_result`` runs outside the ``try`` that wraps
        ``future.result()``, so one bad edit aborted the whole run. There
        is no read left to raise from, so no handler is added.
        """
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=_edit(tmp_path, _MALFORMED),
        )

        assert [r.component for r in readings] == ["comp-a", "comp-b"]
        assert {r.policy.max_files_changed for r in readings} == {5}


class TestTheParseCountDoesNotGrowWithComponents:
    """Acceptance 4 as a measurement rather than a claim.

    TWO numbers, because they are two different claims and an earlier
    draft of this test conflated them. ``load_toml_document`` CALLS is
    how many times something asked for the document; ``tomllib.loads``
    is how many times the bytes were actually lexed, since
    ``load_toml_document`` serves a repeat inside a
    ``toml_parse_scope`` from the scope. Counting the wrapper and
    calling the answer a parse count over-reports by exactly the number
    of reads the scope already collapsed.

    Measured at 414d662 by the same two methods: 5 calls and 5 parses
    for 1 component, 9 and 9 for 2, 17 and 17 for 4, 33 and 33 for 8.
    They agreed at the base because nothing on that path was inside a
    scope; Phase 1 added 4 of each per component.
    """

    @staticmethod
    def _counts(tmp_path: Path, count: int, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
        from kstrl import config as config_module

        original = config_module.load_toml_document
        original_loads = config_module.tomllib.loads
        calls = 0
        parses = 0

        def counting(path: Path) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return original(path)

        def counting_loads(text: str, **kwargs: Any) -> dict[str, Any]:
            nonlocal parses
            parses += 1
            return original_loads(text, **kwargs)

        monkeypatch.setattr(config_module, "load_toml_document", counting)
        monkeypatch.setattr(config_module.tomllib, "loads", counting_loads)
        comps = [component(f"comp-{index}") for index in range(count)]
        phase_verify_envelopes(tmp_path, comps)
        return calls, parses

    def test_one_component_and_eight_cost_the_same(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        one = self._counts(tmp_path, 1, monkeypatch)
        eight = self._counts(tmp_path, 8, monkeypatch)
        assert one == eight, (
            f"1 component cost {one} (calls, parses) and 8 cost {eight}, "
            "so the config cost still grows with the component count."
        )
        # Stated rather than left as "equal". 7 calls, all seven inside
        # ``RunEnvelope.load``; 1 parse, because they share one
        # ``toml_parse_scope``, which is the whole point of the block.
        # It was (7, 2) while ``ComponentPipeline.__init__`` still
        # resolved four of them in a scope of its own.
        assert one == (7, 1)


# --- the factory half: the hash is taken off what the pipeline enforces ---


def _empty_run(tmp_path: Path, config: FactoryConfig) -> tuple[Manifest, ComponentPipeline]:
    """Drive ``run_factory`` over an empty manifest and hand back the
    pipeline it built.

    Empty because the subject is everything ``run_factory`` does BEFORE
    scheduling: resolve the envelope, clamp it with the autonomy ladder,
    hand it to the pipeline and record its hash.
    """
    scripts = tmp_path / "scripts" / "kstrl"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "prompt.md").write_text("p", encoding="utf-8")
    (scripts / "prd.json").write_text('{"branchName": "t", "userStories": []}', encoding="utf-8")
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[],
    )
    manifest.save(tmp_path / "manifest.json")
    built: list[ComponentPipeline] = []
    real = ComponentPipeline

    def capture(**kwargs: Any) -> ComponentPipeline:
        pipeline = real(**kwargs)
        built.append(pipeline)
        return pipeline

    import kstrl.factory as factory_module

    original = factory_module.ComponentPipeline
    factory_module.ComponentPipeline = capture  # type: ignore[misc]
    try:
        run_factory(
            manifest,
            config,
            KstrlConfig(
                prompt_file=scripts / "prompt.md",
                prd_file=scripts / "prd.json",
                sleep_seconds=0,
                agent_cmd="echo test",
            ),
            PlainUI(no_color=True),
            tmp_path,
        )
    finally:
        factory_module.ComponentPipeline = original  # type: ignore[misc]
    assert len(built) == 1
    return manifest, built[0]


class TestTheFactorySideParseCountIsPinned:
    """The half ``TestTheParseCountDoesNotGrowWithComponents`` cannot see.

    That class drives the pipeline harness, so it counts nothing
    ``_run_factory_locked`` does before the pipeline exists. The
    duplicate that lived there was ``[autonomy]``, read once bare at run
    start and once inside ``RunEnvelope.load`` nine lines later: two
    parses of one file, and a nested ``toml_parse_scope`` does NOT
    collapse them, because the inner scope replaces the outer cache
    rather than inheriting it (measured: 3 loads across nested scopes
    still cost 2 parses). The envelope carries the ``AutonomyConfig``
    instead, so the section is resolved once and read off it.
    """

    def test_a_run_resolves_each_section_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kstrl import config as config_module

        original_loads = config_module.tomllib.loads
        counts = {"calls": 0, "parses": 0}
        original_doc = config_module.load_toml_document

        def counting_doc(path: Path) -> dict[str, Any]:
            counts["calls"] += 1
            return original_doc(path)

        def counting_loads(text: str, **kwargs: Any) -> dict[str, Any]:
            counts["parses"] += 1
            return original_loads(text, **kwargs)

        monkeypatch.setattr(config_module, "load_toml_document", counting_doc)
        monkeypatch.setattr(config_module.tomllib, "loads", counting_loads)
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))

        _empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )

        assert (counts["calls"], counts["parses"]) == (13, 7), (
            "the cost of a run's config resolution moved. This is a "
            "census pin, not a performance budget: a number that grew "
            "means a section is being resolved twice, and the fix is to "
            "read it off something already resolved rather than to "
            "raise the pin. It fell from (15, 10) to (14, 9) when the "
            "second [autonomy] read went, and to (13, 7) when the four "
            "the pipeline used to resolve joined the envelope's single "
            "scope and the second [sandbox] read went with them (#192)."
        )


class TestTheFactoryHandsThePipelineWhatItRecords:
    def test_the_recorded_hash_is_the_pipeline_envelope(self, tmp_path: Path) -> None:
        """With ``[autonomy] enabled = false``, the default. At 414d662
        the assignment that gave the pipeline the resolved policy sat
        inside ``if autonomy_active:``, so on this path the pipeline was
        handed nothing and fell through to disk per component."""
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        manifest, pipeline = _empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )
        assert manifest.policy_hash == pipeline.run_envelope.policy_hash()
        assert pipeline.run_envelope.policy.max_files_changed == 5

    def test_the_ladder_clamp_reaches_the_pipeline_and_the_hash(self, tmp_path: Path) -> None:
        """The ladder withholds ``deps_allow_new`` below L3. The clamped
        envelope must be the one the pipeline holds AND the one the hash
        covers, which is what ``manifest.policy_hash`` promises."""
        (tmp_path / "kstrl.toml").write_text(
            "[policy]\nenabled = true\ndeps_allow_new = true\n"
            "[autonomy]\nenabled = true\nmax_level = 1\n"
        )
        AutonomyState(level=4).save(tmp_path)
        manifest, pipeline = _empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )
        assert pipeline.run_envelope.policy.deps_allow_new is False
        assert manifest.policy_hash == pipeline.run_envelope.policy_hash()
        assert manifest.policy_hash != PolicyConfig.load(tmp_path).envelope_hash()

    def test_phase_one_gets_the_clamped_autonomy_level(self, tmp_path: Path) -> None:
        """Finding (c), and it needs no file edit at all.

        ``resolve_runtime_level`` clamps by ``[autonomy] max_level``, by
        the policy envelope ceiling and by control-state location.
        Phase 1 and the set-point gate both read the RAW stored level.
        Measured at 414d662: stored L4, factory resolved L1, Phase 1
        used L4. No verdict changed at either level today, because both
        consumers test only ``>= 1``; it goes live the moment either
        threshold becomes level-graded.
        """
        (tmp_path / "kstrl.toml").write_text(
            "[policy]\nenabled = true\n[autonomy]\nenabled = true\nmax_level = 1\n"
        )
        AutonomyState(level=4).save(tmp_path)
        _, pipeline = _empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )
        assert AutonomyState.load(tmp_path).level == 4
        assert pipeline.run_envelope.autonomy_level == 1

    def test_the_setpoint_gate_gets_the_same_clamped_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the expression, measured separately.

        ``_phase_verify`` and ``_setpoint_blocking`` held two copies of
        the level expression, and mutating both at once is caught by
        the parse count through the ``_phase_verify`` copy alone. Split
        into two mutations, the set-point copy was measured STILL GREEN
        against ``test_setpoint_agreement.py`` and ``test_review.py``:
        nothing drove it with a stored level that differed from the
        run's. The verdict cannot tell them apart either, because
        ``setpoint_blocks`` tests only ``>= 1``, so this records the
        level the gate is HANDED rather than what it decided.
        """
        (tmp_path / "kstrl.toml").write_text(
            "[policy]\nenabled = true\n[autonomy]\nenabled = true\nmax_level = 1\n"
        )
        AutonomyState(level=4).save(tmp_path)
        _, pipeline = _empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )
        import kstrl.pipeline as pipeline_module

        seen: list[int] = []

        def recording(config: FactoryConfig, level: int) -> bool:
            seen.append(level)
            return False

        monkeypatch.setattr(pipeline_module, "setpoint_blocks", recording)
        pipeline._setpoint_blocking()

        assert seen == [1], (
            "the set-point gate was handed the raw stored level rather "
            f"than the clamped one the run operates at. Got {seen}, "
            "expected [1] with kstrl.toml clamping L4 to L1."
        )


class TestRunEnvelope:
    """The unit the two halves above share."""

    def test_it_reads_the_three_sections_the_hash_and_the_gates_use(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        envelope = RunEnvelope.load(tmp_path)
        assert envelope.policy.max_files_changed == 5
        assert envelope.adequacy.enabled is True
        assert envelope.autonomy.enabled is False
        assert envelope.autonomy_level == 0
        assert envelope.policy_hash() == envelope.policy.envelope_hash()

    def test_the_stored_level_is_read_only_when_the_ladder_is_on(self, tmp_path: Path) -> None:
        """0 when the ladder is off is not a default, it is the same
        expression ``_phase_verify`` used to evaluate per component."""
        AutonomyState(level=3).save(tmp_path)
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = false\n")
        assert RunEnvelope.load(tmp_path).autonomy_level == 0
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = true\nmax_level = 4\n")
        assert RunEnvelope.load(tmp_path).autonomy_level == 3

    def test_the_policy_override_wins_over_the_file(self, tmp_path: Path) -> None:
        """``FactoryConfig.policy_config`` is the injection seam callers
        already use; the envelope keeps it rather than adding a second."""
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        injected = PolicyConfig(enabled=True, max_files_changed=42)
        envelope = RunEnvelope.load(tmp_path, policy_override=injected)
        assert envelope.policy is injected
        assert envelope.adequacy.enabled is True

    def test_it_is_frozen(self, tmp_path: Path) -> None:
        """The factory clamps it with ``replace``; nothing edits one in
        place, so a holder cannot have it changed underneath."""
        import dataclasses

        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        envelope = RunEnvelope.load(tmp_path)
        with pytest.raises(dataclasses.FrozenInstanceError):
            envelope.autonomy_level = 4  # type: ignore[misc]
