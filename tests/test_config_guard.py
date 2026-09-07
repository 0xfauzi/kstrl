"""Who may read ``kstrl.toml``, and from which scope (#192).

The guard half of #192. ``tests/test_config_read_once.py`` is the
behaviour half, and the split is by size rather than by subject: the two
together crossed the repo's 800-line file limit.

Two layers, both FLAGGING. Layer A pins the config surface by
DISCOVERING it, so a new config dataclass is an unexplained census delta
rather than a hole in a list. Layer B pins WHERE that surface may be
called, over the three modules a run's config can be resolved from, and
pins the number of call sites it found beside it: ``assert offenders ==
[]`` alone passes both when the module is clean and when the walk has
been switched off, which is the same shape as the defect.

``kstrl/pipeline.py`` is the module whose count is zero, so it cannot be
its own control; ``kstrl/factory.py`` and ``kstrl/serve.py`` are walked
in the same call and their counts are not zero.

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

from pathlib import Path

import pytest

from kstrl import runenvelope
from kstrl.config_preflight import _section_for
from tests.helpers import astwalk, configwalk
from tests.helpers.astwalk import KSTRL_PACKAGE, package_sources

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

#: The classes the type closure adds after round 0: the ones that HOLD a
#: resolved section rather than reading one. Every method counts,
#: because obtaining the holder is obtaining the config it holds, and
#: ``RunEnvelope.resolve`` returns an ``EnvelopeResolution`` rather than
#: a ``RunEnvelope``, so a rule keyed on the return type would not have
#: caught the plant that made this necessary. ``EnvelopeResolution`` and
#: ``FeatureParams`` define no methods, so they add no site; they are
#: here because the census must show every holder, not only the useful
#: ones.
EXPECTED_SURFACE_CLASSES |= {
    "EnvelopeResolution": frozenset(),
    "FeatureParams": frozenset(),
    "FactoryConfig": frozenset(
        {"__post_init__", "engineer_verify_config", "from_env", "load", "resolved_verify_config"}
    ),
    "RunEnvelope": frozenset({"load", "policy_hash", "resolve"}),
}

#: How many rounds the type closure runs after round 0. The second
#: control on the closure, beside ``RunEnvelope`` being in the pin above:
#: a walk whose loop stopped running reports 0 here, which is exactly
#: what the round-2 walk did and what let the pipeline plant through.
EXPECTED_SURFACE_ROUNDS = 2

#: The readers that own no class. ``load_toml_section`` is the primitive
#: one layer up from the file; ``run`` is the ``ks run`` command body.
#: ``resolve_or_report`` replaced ``load_or_report`` in this set when
#: #192 split the scope-owning wrapper off the resolution: the wrapper
#: no longer calls a primitive itself, so it is no longer surface.
#:
#: That swap NARROWED the walk as a side effect, which is worth naming
#: because a production refactor quietly shrinking a static walk is the
#: class CLAUDE.md logs eleven instances of. Measured by running this
#: walk against the code before the split: exactly one site left,
#: ``tui/widgets/config_problem.py::ConfigProblemBanner.load``, because
#: it calls ``load_or_report`` and that name is no longer surface. It is
#: latent for layer B, whose three modules call ``load_or_report``
#: nowhere; the TUI's own config guard is
#: ``tests/test_tui_config_guard.py``.
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
#: 7 until the type closure enrolled ``RunEnvelope``; the eighth is
#: ``RunEnvelope.resolve`` at the top of ``_run_factory_locked``, which
#: is the run's one envelope resolution and the site the whole change is
#: about being visible to the walk at last.
EXPECTED_FACTORY_SITES = 8

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

#: The one fallback ``kstrl/pipeline.py``'s constructor forbids in its
#: own words, spelled exactly as the comment there spells it. Round 2 of
#: #192 shipped a walk that could not see it: layer A enrolled a class
#: only when one of its own methods called a parse primitive by bare
#: name, ``RunEnvelope.resolve`` reaches the file through
#: ``resolve_or_report`` and seven loaders instead, so the envelope was
#: not surface and this plant passed the whole suite.
_REQUIRED_ENVELOPE = "        run_envelope: RunEnvelope,\n"
_OPTIONAL_ENVELOPE = "        run_envelope: RunEnvelope | None = None,\n"
_FALLBACK_ANCHOR = "        self.sandbox_config = run_envelope.sandbox\n"
_FALLBACK = "        run_envelope = run_envelope or RunEnvelope.load(root_dir)\n"


def _pipeline_with_the_forbidden_fallback(tmp_path: Path) -> Path:
    """A copy of the real ``kstrl/pipeline.py`` with the fallback back in.

    The positive control for ``sites == []``. An empty offender list is
    what a clean module returns AND what a walk that stopped looking
    returns, and ``pipeline.py`` is the module whose count is zero, so
    it cannot be its own control the way ``factory.py`` and ``serve.py``
    are each other's. This makes it one: the same walk over the same
    module with the one forbidden line added must report a site.

    Both anchors are asserted to occur exactly once before the edit, so
    a rename that makes the plant apply to nothing is a failure rather
    than a control that silently stops planting (#344).
    """
    source = (KSTRL_PACKAGE / "pipeline.py").read_text(encoding="utf-8")
    for anchor in (_REQUIRED_ENVELOPE, _FALLBACK_ANCHOR):
        assert source.count(anchor) == 1, (
            f"the plant's anchor {anchor!r} occurs {source.count(anchor)} times "
            "in kstrl/pipeline.py, so this control is not planting what it "
            "says it plants. Retarget it; do not delete it."
        )
    planted = tmp_path / "pipeline.py"
    planted.write_text(
        source.replace(_REQUIRED_ENVELOPE, _OPTIONAL_ENVELOPE).replace(
            _FALLBACK_ANCHOR, _FALLBACK + _FALLBACK_ANCHOR
        ),
        encoding="utf-8",
    )
    return planted


@pytest.fixture(scope="module")
def package_surface() -> configwalk.Surface:
    """The whole config surface, walked once for this file.

    Nine tests need it and each recomputed it: measured at a median
    0.285 s a call, about 2.6 s of this file's 9.8 s. Module scope
    rather than a cache on ``configwalk.surface`` itself, because
    ``test_dropping_a_primitive_shrinks_the_surface`` monkeypatches
    ``PARSE_PRIMITIVES`` and needs a fresh walk; a cached function would
    hand it the unpatched answer and it would pass while measuring
    nothing. The two tests that must walk for themselves call
    ``configwalk.surface`` directly and say why.
    """
    return configwalk.surface(package_sources())


class TestConfigSurface:
    """Layer A. Enumerates no class name of its own."""

    def test_the_surface_is_the_pinned_one(self, package_surface: configwalk.Surface) -> None:
        found = package_surface
        assert found.classes == EXPECTED_SURFACE_CLASSES, (
            "the set of config classes in kstrl/ moved. A new one is not "
            "wrong, but it is a new place a run can resolve config from, "
            "so decide whether it belongs in RunEnvelope before pinning "
            f"it here. Found {sorted(found.classes)}"
        )
        assert found.free == EXPECTED_FREE_READERS, (
            f"the free config readers in kstrl/ moved. Found {sorted(found.free)}"
        )
        assert found.rounds == EXPECTED_SURFACE_ROUNDS, (
            "the type closure ran a different number of rounds. 0 means "
            "it did not run at all, which is the round-2 walk that could "
            "not see RunEnvelope; a higher number means kstrl/ grew a "
            f"class that holds a holder. Found {found.rounds}"
        )

    def test_the_closure_enrols_a_holder_it_was_never_told_about(self, tmp_path: Path) -> None:
        """Layer A's fixed point, planted rather than pinned.

        The pin above names ``RunEnvelope`` and would go red if the
        closure stopped running, which is the control. This is the
        mutation for the RULE: a class the walk has never heard of, that
        calls no primitive and only HOLDS one, must be discovered with
        every method of it, in one round.
        """
        planted = tmp_path / "holder.py"
        planted.write_text(
            _NEW_CONFIG_CLASS + "\n\n@dataclass\nclass WidgetEnvelope:\n"
            "    widget: WidgetConfig\n\n"
            "    @classmethod\n"
            "    def build(cls, root_dir):\n"
            "        return cls(widget=WidgetConfig.load(root_dir))\n",
            encoding="utf-8",
        )
        found = configwalk.surface([planted])
        assert found.rounds == 1
        assert found.classes == {
            "WidgetConfig": frozenset({"load"}),
            "WidgetEnvelope": frozenset({"build"}),
        }

    def test_the_attribution_loses_no_primitive_call(
        self, package_surface: configwalk.Surface
    ) -> None:
        """The census control for layer A.

        ``own_nodes`` stops at a nested function and at a lambda, so a
        primitive called from inside one would be invisible to the
        per-scope walk and would silently shrink the surface. Measured
        against the same count taken over the whole module: today they
        agree, and the day they stop agreeing this says so instead of
        reporting a smaller surface.
        """
        found = package_surface
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

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_primitive_named_as_a_string_is_invisible(self, tmp_path: Path) -> None:
        """The second disclosed shape: the name is data, not a name.

        ``getattr(c, "load_toml_document")(root)`` spells the primitive
        in a STRING, so there is no ``ast.Name`` for layer 1 to match and
        no binding for a resolver to follow. Measured in ``kstrl/``
        today: 0 occurrences, so latent. Separate row from the
        module-qualified one because they fail for different reasons and
        closing one does not close the other.
        """

        def probe(source: str) -> object:
            planted = tmp_path / "by_string.py"
            planted.write_text(source, encoding="utf-8")
            return configwalk.surface([planted]).raw

        astwalk.blind_spot(
            probe,
            "import kstrl.config as c\n\n\ndef build(root):\n"
            '    return getattr(c, "load_toml_document")(root)\n',
        )


class TestTheRunReadsConfigOnlyBeforeItStarts:
    """Layer B. Scoped to the three modules a run's config can be read
    from, which is why a per-poll read in ``kstrl/serve.py`` is outside
    the offender rule by construction rather than by an exemption
    somebody has to remember."""

    def test_the_pipeline_reads_no_config_at_all(
        self, package_surface: configwalk.Surface, tmp_path: Path
    ) -> None:
        found = package_surface
        control = configwalk.read_sites(_pipeline_with_the_forbidden_fallback(tmp_path), found)
        assert control != [], (
            "the walk reports nothing on a copy of kstrl/pipeline.py with "
            "`run_envelope = run_envelope or RunEnvelope.load(root_dir)` "
            "planted in the constructor, which is the one fallback that "
            "file forbids by name. The assertion below therefore proves "
            "nothing: a walk that cannot see the offender returns the "
            "same empty list a clean module returns."
        )
        sites = configwalk.read_sites(KSTRL_PACKAGE / "pipeline.py", found)
        assert sites == [], (
            "kstrl/pipeline.py resolves config. Its phases run per "
            "component attempt, so a read there is a second answer the "
            "run's recorded policy hash does not cover, and a read in "
            "__init__ raises where nothing can report it (#192). The "
            f"envelope is injected instead. Offenders: {sites}"
        )

    def test_the_envelope_names_every_section_the_run_enforces(
        self, package_surface: configwalk.Surface
    ) -> None:
        """The seven sections, pinned where the walk can see them.

        ``AutonomyState`` is not here: it reads ``.kstrl/autonomy.json``
        rather than ``kstrl.toml``, so no config walk covers it and
        ``TestRunEnvelope`` drives it instead.
        """
        found = package_surface
        sites = configwalk.read_sites(KSTRL_PACKAGE / "runenvelope.py", found)
        named = {site.target for site in sites if site.scope == "RunEnvelope.resolve"}
        assert named == EXPECTED_ENVELOPE_SECTIONS, (
            "the run envelope's sections moved. A section that leaves it "
            "goes back to being resolved per component or inside a "
            f"constructor that cannot report a bad one (#192). Found {sorted(named)}"
        )
        # Every one of them must ALSO be a loader `config_sections()`
        # names, because that is what gives a rejected section its
        # label. `_section_for` raises LookupError for a loader the
        # registry does not hold, and LookupError is outside
        # `SURFACE_REJECTIONS`, so it escapes `resolve_or_report`,
        # escapes `RunEnvelope.resolve` and reproduces blocker 1 - a
        # traceback out of run_factory - for the next section added to
        # the envelope and not to the registry. Latent today, and this
        # is what keeps it latent.
        for target in sorted(EXPECTED_ENVELOPE_SECTIONS):
            class_name, method = target.split(".")
            loader = getattr(getattr(runenvelope, class_name), method)
            assert _section_for(loader).sections, (
                f"{target} resolves to a registry entry naming no toml "
                "section, so a rejection of it would have no label."
            )

    def test_every_factory_config_read_is_run_level(
        self, package_surface: configwalk.Surface
    ) -> None:
        """The half the pipeline scoping cannot see.

        ``_submit_args`` and ``_run_component`` run once per component
        attempt inside ``kstrl/factory.py``, and a ``PolicyConfig.load``
        planted at the top of ``_submit_args`` passed this whole file
        before layer B walked factory.py.
        """
        found = package_surface
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
    def test_the_site_count_is_pinned(
        self, package_surface: configwalk.Surface, module: str, expected: int
    ) -> None:
        """The control for both assertions above.

        An empty offender list is what a clean module returns AND what a
        walk that stopped looking returns; ``pipeline.py`` returns zero
        SITES, so it cannot be its own control at all. These two can:
        both counts are non-zero, both are walked by the same call in
        the same run, and a walk that went blind takes them to zero.
        """
        found = package_surface
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
        self, package_surface: configwalk.Surface, tmp_path: Path, shape: str, source: str
    ) -> None:
        """Layer B's mutation, once per call shape.

        The review measured five of these eight passing unnoticed. The
        lambda is the one that mattered most: it was invisible to this
        layer AND left layer A's ``attributed == raw`` census untouched,
        so both halves of the guard went quiet at the same time.
        """
        planted = tmp_path / "pipeline.py"
        planted.write_text(source, encoding="utf-8")
        sites = configwalk.read_sites(planted, package_surface)
        assert sites != [], f"the {shape} shape is invisible to layer B"
        assert all(site.scope.startswith("ComponentPipeline._phase_verify") for site in sites)

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_receiver_built_at_run_time_is_invisible(
        self, package_surface: configwalk.Surface, tmp_path: Path
    ) -> None:
        """The third disclosed shape, and layer B's own.

        ``importlib.import_module("kstrl.policy").PolicyConfig.load(root)``
        names the module in a STRING and builds the receiver while the
        program runs, so there is no binding for ``astwalk.bindings`` to
        follow and the eight shapes above all resolve where this one
        cannot. Measured in ``kstrl/`` today: 0 occurrences, so latent.
        The same class of miss covers a helper in ANOTHER module that
        calls a surface class, because nothing here is transitive over
        calls; ``configwalk``'s DISCLOSED LIMIT paragraph says both.
        """

        def probe(source: str) -> object:
            planted = tmp_path / "pipeline.py"
            planted.write_text(source, encoding="utf-8")
            return len(configwalk.read_sites(planted, package_surface))

        astwalk.blind_spot(
            probe,
            "import importlib\n\n\nclass ComponentPipeline:\n"
            "    def _phase_verify(self, comp):\n"
            '        mod = importlib.import_module("kstrl.policy")\n'
            "        return mod.PolicyConfig.load(self.root_dir)\n",
        )
