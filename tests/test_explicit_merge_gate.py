"""#195: the ladder may raise the merge gate, and may not lower one the operator set.

The asymmetry, and why it needs two directions rather than one rule:

- PR #174 correction 1 closed the direction where a hand-edited flag
  GRANTS autonomy the ladder never awarded. An explicit
  ``pause_before_pr_merge = false`` at L1 still pauses, and that stays
  true here.
- This file is the other direction. At L3 and L4 the bundle's ``False``
  was assigned straight onto the config, which removed a human gate the
  operator had asked for in writing and recorded it as an override
  ignored. Measured at 414d662 across 17 real ``run_factory`` calls: an
  explicit ``true`` from kstrl.toml OR from the env var resolved to
  ``False`` at both levels, and the run's only record was the words
  "bundle wins".

The distinction the fix rests on is PROVENANCE, not value: a
configured-but-defaulted ``True`` is still dropped at L3 (that is
``tests/test_autonomy_ladder.py::TestFactoryWiring``'s renamed case),
while a ``True`` the operator wrote survives. So every test here that
claims a source drives the decision has to reach the decision THROUGH
that source: ``FactoryConfig.load`` with a real kstrl.toml, a real env
var, or the real click command. A hand-built ``FactoryConfig`` carries no
provenance and would prove nothing about where the value came from.

``tests/test_factory_config_provenance.py`` is the static half: it
inventories every place ``kstrl/`` spells the flag, so a fifth source
cannot be added without this file's matrix being asked to grow.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.autonomy import AutonomyLevel, AutonomyState, flag_bundle_for, pause_gate_for
from kstrl.config import KstrlConfig
from kstrl.events import AutonomyLevelApplied, CallbackSink, Event, EventBus
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.review import ReviewResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.helpers.component_prd import write_component_prd
from tests.helpers.factorycli import capture_run_factory, invoke_factory

# ---------------------------------------------------------------------------
# The rule, on its own
# ---------------------------------------------------------------------------

#: (level, configured, explicit) -> the gate the run must use. Written out
#: rather than computed: an expectation derived from the same expression
#: the code uses would pass with the code deleted.
_GATE_TABLE: list[tuple[AutonomyLevel, bool, bool, bool]] = [
    # L1 and L2 hold the gate up whatever the config says. The ladder is
    # WITHHOLDING auto-merge, which it may always do.
    (AutonomyLevel.L1_SUPERVISED, False, False, True),
    (AutonomyLevel.L1_SUPERVISED, False, True, True),
    (AutonomyLevel.L1_SUPERVISED, True, False, True),
    (AutonomyLevel.L1_SUPERVISED, True, True, True),
    (AutonomyLevel.L2_GATED_MERGE, False, False, True),
    (AutonomyLevel.L2_GATED_MERGE, False, True, True),
    (AutonomyLevel.L2_GATED_MERGE, True, False, True),
    (AutonomyLevel.L2_GATED_MERGE, True, True, True),
    # L3 and L4 permit auto-merge. Only an explicit True refuses it; a
    # True nobody wrote is still the ladder's to drop.
    (AutonomyLevel.L3_ENVELOPED_AUTO, False, False, False),
    (AutonomyLevel.L3_ENVELOPED_AUTO, False, True, False),
    (AutonomyLevel.L3_ENVELOPED_AUTO, True, False, False),
    (AutonomyLevel.L3_ENVELOPED_AUTO, True, True, True),
    (AutonomyLevel.L4_DEPLOY, False, False, False),
    (AutonomyLevel.L4_DEPLOY, False, True, False),
    (AutonomyLevel.L4_DEPLOY, True, False, False),
    (AutonomyLevel.L4_DEPLOY, True, True, True),
]


class TestPauseGateFor:
    @pytest.mark.parametrize(("level", "configured", "explicit", "expected"), _GATE_TABLE)
    def test_table(
        self,
        level: AutonomyLevel,
        configured: bool,
        explicit: bool,
        expected: bool,
    ) -> None:
        assert (
            pause_gate_for(flag_bundle_for(level), configured=configured, explicit=explicit)
            is expected
        )

    def test_explicit_false_never_lowers_a_bundle_gate(self) -> None:
        """The #174 direction, stated as its own claim.

        Four of the table's rows say this, but they say it as data. This
        one names it, because it is the invariant a future "make the
        rule symmetric" edit would break first.
        """
        for level in (AutonomyLevel.L1_SUPERVISED, AutonomyLevel.L2_GATED_MERGE):
            bundle = flag_bundle_for(level)
            assert bundle.pause_before_pr_merge is True
            assert pause_gate_for(bundle, configured=False, explicit=True) is True

    def test_the_table_covers_every_level(self) -> None:
        """A level added to the ladder is a row missing here."""
        assert {row[0] for row in _GATE_TABLE} == set(AutonomyLevel)


# ---------------------------------------------------------------------------
# End to end: each source, at each level
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    run("init")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "tester")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-m", "base")


def _prepare(root: Path, level: AutonomyLevel, toml_pause: str | None) -> None:
    """A repo with the ladder on at ``level`` and the toml key present or not."""
    _init_git_repo(root)
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt", encoding="utf-8")
    write_component_prd(root, "scripts/kstrl/feature/comp-a/prd.json")
    text = "[autonomy]\nenabled = true\nmax_level = 4\n[policy]\nenabled = true\n[factory]\n"
    if toml_pause is not None:
        text += f"pause_before_pr_merge = {toml_pause}\n"
    (root / "kstrl.toml").write_text(text, encoding="utf-8")
    AutonomyState(level=int(level)).save(root)


def _for_the_run(config: FactoryConfig) -> FactoryConfig:
    """Strip the run down to the ladder: no worktrees, no PRs, no agent."""
    config.use_worktrees = False
    config.create_prs = False
    config.max_parallel = 1
    config.max_retries = 0
    config.retry_delay = 0
    config.verify_config = VerifyConfig(
        test_command="true",
        typecheck_command="true",
        lint_command="true",
        check_bad_patterns=False,
        subprocess_timeout=5.0,
    )
    return config


def _manifest() -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a",
                "Component A",
                "Desc",
                [],
                "scripts/kstrl/feature/comp-a/prd.json",
                "kstrl/factory/comp-a",
            )
        ],
    )


def _run(root: Path, config: FactoryConfig) -> tuple[list[Event], str]:
    """Run the real factory with the engineer and the gates stubbed.

    Returns the events and the UI text. The UI writes to a StringIO
    rather than to the real stream: PlainUI defaults to ``sys.stderr``,
    and a test asserting on ``capsys.readouterr().out`` for it passes
    vacuously against an empty string, which is how the first draft of
    this file went green while asserting nothing.
    """
    base = KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    sink = io.StringIO()
    events: list[Event] = []

    def _bus(**kwargs: Any) -> EventBus:
        bus = EventBus(**kwargs)
        bus.add_sink(CallbackSink(events.append))
        return bus

    verification = VerificationResult(passed=True, checks=[CheckResult("diff_scope", True, "ok")])
    with (
        patch("kstrl.factory.EventBus", _bus),
        patch(
            "kstrl.factory._run_component",
            return_value=ComponentResult("comp-a", success=True, iterations=1),
        ),
        patch("kstrl.factory.run_mechanical_verification", return_value=verification),
        patch("kstrl.factory.run_review", return_value=ReviewResult(passed=True, mode="hard")),
    ):
        run_factory(_manifest(), config, base, PlainUI(no_color=True, file=sink), root)
    return events, sink.getvalue()


#: source name -> (toml value or None, env value or None, pass the CLI flag)
_SOURCES: dict[str, tuple[str | None, str | None, bool]] = {
    "default": (None, None, False),
    "toml_true": ("true", None, False),
    "toml_false": ("false", None, False),
    "env_true": (None, "1", False),
    "flag_true": (None, None, True),
}

#: The gate each source must produce, per level. L1/L2 pause whatever
#: happens; at L3/L4 only the three explicit-true sources survive.
_EXPECTED: dict[str, dict[AutonomyLevel, bool]] = {
    name: {
        AutonomyLevel.L1_SUPERVISED: True,
        AutonomyLevel.L2_GATED_MERGE: True,
        AutonomyLevel.L3_ENVELOPED_AUTO: name in {"toml_true", "env_true", "flag_true"},
        AutonomyLevel.L4_DEPLOY: name in {"toml_true", "env_true", "flag_true"},
    }
    for name in _SOURCES
}


def _config_from_source(
    root: Path,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> FactoryConfig:
    """Build the config the way that source really builds it.

    ``flag_true`` goes through the click command with ``run_factory``
    captured, so the object handed to the ladder below is the one the
    CLI produced rather than a copy of what the CLI is believed to do.
    """
    toml_pause, env_pause, use_flag = _SOURCES[source]
    if env_pause is not None:
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", env_pause)
    if use_flag:
        box = capture_run_factory(monkeypatch)
        result = invoke_factory(root, "--pause-before-pr-merge")
        assert result.exit_code == 0, result.output
        config: FactoryConfig = box["factory_config"]
        return config
    return FactoryConfig.load(root)


class TestEverySourceAtEveryLevel:
    """20 real ``run_factory`` calls: five provenance shapes x four levels.

    The cross product is measured rather than reasoned about, because the
    defect was exactly a level silently overruling a source.
    """

    @pytest.mark.parametrize("source", sorted(_SOURCES))
    @pytest.mark.parametrize("level", list(AutonomyLevel))
    def test_resolved_gate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        level: AutonomyLevel,
        source: str,
    ) -> None:
        _prepare(tmp_path, level, _SOURCES[source][0])
        config = _for_the_run(_config_from_source(tmp_path, source, monkeypatch))
        _run(tmp_path, config)
        assert config.pause_before_pr_merge is _EXPECTED[source][level]

    @pytest.mark.parametrize("source", ["toml_true", "env_true", "flag_true"])
    def test_explicit_sources_are_recorded_as_explicit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        source: str,
    ) -> None:
        """The provenance itself, before any ladder runs.

        Separate from the resolved value: a source could set the value
        and lose the provenance, and the L1/L2 rows above would still
        pass because their bundle pauses anyway.
        """
        _prepare(tmp_path, AutonomyLevel.L1_SUPERVISED, _SOURCES[source][0])
        config = _config_from_source(tmp_path, source, monkeypatch)
        assert "pause_before_pr_merge" in config.explicit_fields

    def test_a_defaulted_gate_carries_no_provenance(self, tmp_path: Path) -> None:
        _prepare(tmp_path, AutonomyLevel.L1_SUPERVISED, None)
        assert FactoryConfig.load(tmp_path).explicit_fields == frozenset()


# ---------------------------------------------------------------------------
# The audit trail: the run must say WHY it paused
# ---------------------------------------------------------------------------


def _applied(events: list[Event]) -> AutonomyLevelApplied:
    found = [e for e in events if isinstance(e, AutonomyLevelApplied)]
    assert len(found) == 1, found
    return found[0]


@pytest.fixture
def _l3_toml_run(tmp_path: Path) -> Iterator[tuple[list[Event], str]]:
    """One L3 run with an explicit toml true, shared by the record tests."""
    _prepare(tmp_path, AutonomyLevel.L3_ENVELOPED_AUTO, "true")
    config = _for_the_run(FactoryConfig.load(tmp_path))
    events, output = _run(tmp_path, config)
    assert config.pause_before_pr_merge is True
    yield events, output


class TestTheRecord:
    def test_the_run_says_the_gate_was_retained(
        self, _l3_toml_run: tuple[list[Event], str]
    ) -> None:
        _, output = _l3_toml_run
        assert "gate retained by explicit request" in output
        assert "Manual override ignored" not in output

    def test_the_event_records_the_retention(self, _l3_toml_run: tuple[list[Event], str]) -> None:
        events, _ = _l3_toml_run
        assert any(
            "gate retained by explicit request" in note for note in _applied(events).overrides
        )

    def test_the_event_flags_describe_the_resolved_bundle(
        self, _l3_toml_run: tuple[list[Event], str]
    ) -> None:
        """``flags`` is what the run USED, not what the level awarded.

        Otherwise the one event that exists to make a run's permissions
        auditable records "merge gate: off" for a run that pauses at
        every component.
        """
        events, _ = _l3_toml_run
        flags = _applied(events).flags
        assert any("merge gate: ON" in flag for flag in flags), flags
        assert not any("merge gate: off" in flag for flag in flags), flags

    def test_a_withheld_override_still_says_bundle_wins(self, tmp_path: Path) -> None:
        """L1 with an explicit false: the ladder withholds, and says so."""
        _prepare(tmp_path, AutonomyLevel.L1_SUPERVISED, "false")
        config = _for_the_run(FactoryConfig.load(tmp_path))
        events, output = _run(tmp_path, config)
        assert config.pause_before_pr_merge is True
        assert "Manual override ignored" in output
        assert "bundle wins" in output
        assert "gate retained" not in output
        assert any("bundle wins" in note for note in _applied(events).overrides)


# ---------------------------------------------------------------------------
# The toml value is read strictly
# ---------------------------------------------------------------------------


class TestStrictBoolean:
    """``= "false"`` is a refusal, not True.

    ``bool("false")`` is True, so before #195 a quoted value armed the
    gate. That was a documented hole while the ladder could overrule the
    flag; it stopped being one when the flag started outranking the
    ladder, because a coerced string manufactures an EXPLICIT request
    nobody wrote and then keeps the gate up at every level.
    """

    @pytest.mark.parametrize("value", ['"false"', '"true"', "0", "1", '""'])
    def test_a_non_boolean_is_refused(self, tmp_path: Path, value: str) -> None:
        from kstrl.config import ConfigError

        (tmp_path / "kstrl.toml").write_text(
            f"[factory]\npause_before_pr_merge = {value}\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="must be a boolean"):
            FactoryConfig.load(tmp_path)

    @pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
    def test_a_real_boolean_loads(self, tmp_path: Path, value: str, expected: bool) -> None:
        (tmp_path / "kstrl.toml").write_text(
            f"[factory]\npause_before_pr_merge = {value}\n", encoding="utf-8"
        )
        config = FactoryConfig.load(tmp_path)
        assert config.pause_before_pr_merge is expected
        assert "pause_before_pr_merge" in config.explicit_fields


# ---------------------------------------------------------------------------
# Provenance is key presence, never a value comparison
# ---------------------------------------------------------------------------


class TestProvenanceIsKeyPresence:
    """The rule that separates "the operator wrote this" from "this is the default".

    Measured at 414d662: ``config_report.build_config_report`` tags the
    env source BEHAVIOURALLY (``resolved != noenv``), so
    ``KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE=0`` reports ``default`` even
    though the var is set. That is a display defect there and would be a
    governance defect here, because the same comparison would read an
    explicit ``false`` as absent, and #195 turns absence into "the ladder
    decides".
    """

    def test_an_env_var_set_to_the_default_value_is_still_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "0")
        config = FactoryConfig.load(tmp_path)
        assert config.pause_before_pr_merge is False  # equals the default
        assert "pause_before_pr_merge" in config.explicit_fields

    def test_a_toml_key_set_to_the_default_value_is_still_explicit(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\npause_before_pr_merge = false\n", encoding="utf-8"
        )
        config = FactoryConfig.load(tmp_path)
        assert config.pause_before_pr_merge is False
        assert "pause_before_pr_merge" in config.explicit_fields

    def test_env_overrides_the_toml_value_and_both_are_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Precedence is untouched; provenance is a union, not a winner."""
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\npause_before_pr_merge = true\n", encoding="utf-8"
        )
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "0")
        config = FactoryConfig.load(tmp_path)
        assert config.pause_before_pr_merge is False
        assert config.explicit_fields == frozenset({"pause_before_pr_merge"})

    def test_from_env_reports_the_var_presence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert FactoryConfig.from_env().explicit_fields == frozenset()
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "0")
        assert FactoryConfig.from_env().explicit_fields == frozenset({"pause_before_pr_merge"})

    def test_a_bare_config_claims_nothing(self) -> None:
        """Not-explicit is the safe default: the ladder decides."""
        assert FactoryConfig().explicit_fields == frozenset()

    def test_the_negated_cli_flag_is_also_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-pause-before-pr-merge`` is a request too.

        It cannot lower an L1 bundle, but it is provenance, and treating
        only the True side as explicit would make the field mean
        "someone asked for a gate" rather than "someone set this key".
        """
        box = capture_run_factory(monkeypatch)
        result = invoke_factory(tmp_path, "--no-pause-before-pr-merge")
        assert result.exit_code == 0, result.output
        config: FactoryConfig = box["factory_config"]
        assert config.pause_before_pr_merge is False
        assert "pause_before_pr_merge" in config.explicit_fields

    def test_provenance_is_not_a_toml_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ks factory`` sweeps dataclass fields to report toml-driven
        values. ``explicit_fields`` is provenance, not a value, so it must
        not be announced as "[factory] explicit_fields = ... from
        kstrl.toml"."""
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\npause_before_pr_merge = true\n", encoding="utf-8"
        )
        capture_run_factory(monkeypatch)
        result = invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert "explicit_fields" not in result.output
        assert "pause_before_pr_merge" in result.output
