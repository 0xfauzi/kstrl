"""R10.4: safe mode, one predicate over four existing degraded states.

Every condition here is built with the real writer that creates it in
production - ``Queue.pause``, ``AutonomyState.save``, ``JsonlSink``, a
real ``kstrl.toml``, a real ``XDG_STATE_HOME``. Nothing the predicate
reads is mocked, because a predicate whose inputs are faked proves only
that the fake was shaped correctly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kstrl import events as ev
from kstrl.autonomy import AutonomyLevel, AutonomyState
from kstrl.safemode import RECOVERY, safe_mode_reasons
from kstrl.workqueue import Queue, QueueConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_ID = "factory-20260827-120000-abcd"


# ---------------------------------------------------------------------------
# Condition builders - one per degrade path, shared by the specific tests
# below and by the coverage test that proves all four are reachable.
# ---------------------------------------------------------------------------


def make_control_dir_untrusted(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point XDG_STATE_HOME inside the repo, the documented untrusted case."""
    from kstrl.statedir import clear_xdg_state_home_cache

    nested = root / "nested-xdg"
    nested.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(nested))
    clear_xdg_state_home_cache()


def make_autonomy_degraded(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enable the ladder over an autonomy.json that will not parse."""
    (root / "kstrl.toml").write_text(
        "[autonomy]\nenabled = true\n", encoding="utf-8",
    )
    path = AutonomyState.path_for(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")


def make_queue_paused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Queue(root, QueueConfig()).pause(
        reason="daily budget exhausted", actor="test",
    )


def make_review_skipped(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a real event stream in which review never ran."""
    sink = ev.JsonlSink(root / ".kstrl" / "runs" / RUN_ID / "events.jsonl")
    try:
        for component in ("comp-a", "comp-b"):
            sink.emit(ev.PhaseSkipped(
                component=component,
                phase="review",
                reason="adversarial LLM budget exhausted",
            ))
    finally:
        sink.close()


SCENARIOS = {
    "control_dir": make_control_dir_untrusted,
    "autonomy": make_autonomy_degraded,
    "queue": make_queue_paused,
    "adversarial_skipped": make_review_skipped,
}


def sources(root: Path) -> list[str]:
    return [reason.source for reason in safe_mode_reasons(root)]


# ---------------------------------------------------------------------------
# Acceptance: all four paths reachable, and the four are the documented four
# ---------------------------------------------------------------------------


class TestEveryDegradePathIsReachable:
    @pytest.mark.parametrize("source", sorted(SCENARIOS))
    def test_source_is_reported(
        self, source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        SCENARIOS[source](tmp_path, monkeypatch)
        with pytest.warns(RuntimeWarning) if source == "autonomy" else _noop():
            assert source in sources(tmp_path)

    def test_the_scenarios_cover_exactly_the_documented_sources(self) -> None:
        """The acceptance criterion as a mechanism rather than a promise.

        Adding a fifth source to RECOVERY without a scenario that
        produces it fails here, which is the only thing stopping the
        count from drifting away from four silently.
        """
        assert set(SCENARIOS) == set(RECOVERY)
        assert len(SCENARIOS) == 4


class _noop:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


# ---------------------------------------------------------------------------
# Nominal
# ---------------------------------------------------------------------------


class TestNominal:
    def test_nominal_when_nothing_is_wrong(self, tmp_path: Path) -> None:
        assert safe_mode_reasons(tmp_path) == []

    def test_a_disabled_ladder_is_not_a_degraded_ladder(
        self, tmp_path: Path,
    ) -> None:
        """[autonomy] is off by default, and a file nothing reads is not
        a degraded state. Reporting one would put every repo that never
        opted in into safe mode."""
        path = AutonomyState.path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        assert safe_mode_reasons(tmp_path) == []


# ---------------------------------------------------------------------------
# Per-source detail
# ---------------------------------------------------------------------------


class TestControlDir:
    def test_untrusted_control_dir_is_one_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        make_control_dir_untrusted(tmp_path, monkeypatch)

        reasons = safe_mode_reasons(tmp_path)

        assert len(reasons) == 1
        assert reasons[0].source == "control_dir"
        assert "XDG_STATE_HOME" in reasons[0].detail


class TestAutonomy:
    def test_fallback_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        make_autonomy_degraded(tmp_path, monkeypatch)

        with pytest.warns(RuntimeWarning, match="failing closed to L1"):
            reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["autonomy"]
        assert "failing closed to L1" in reasons[0].detail

    def test_degraded_reason_is_never_written_to_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The field is transient by construction: ``save`` builds its
        payload key by key, not from ``asdict``. This pins that."""
        make_autonomy_degraded(tmp_path, monkeypatch)

        with pytest.warns(RuntimeWarning):
            state = AutonomyState.load(tmp_path)
        assert state.degraded_reason is not None
        state.save(tmp_path)

        written = json.loads(
            AutonomyState.path_for(tmp_path).read_text(encoding="utf-8"),
        )
        assert "degraded_reason" not in written
        # And a reload of what we just wrote is clean, not degraded.
        assert AutonomyState.load(tmp_path).degraded_reason is None

    def test_first_run_is_not_a_fallback(self, tmp_path: Path) -> None:
        """No autonomy.json at all is first run, not a damaged record."""
        assert AutonomyState.load(tmp_path).degraded_reason is None

    def test_clamp_names_both_levels(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """L3 is auto-merge INSIDE the policy envelope. With [policy]
        enabled=false there is no envelope, so L2 is the ceiling."""
        (tmp_path / "kstrl.toml").write_text(
            "[autonomy]\nenabled = true\n\n[policy]\nenabled = false\n",
            encoding="utf-8",
        )
        AutonomyState(
            level=int(AutonomyLevel.L3_ENVELOPED_AUTO),
        ).save(tmp_path)

        reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["autonomy"]
        assert "L3" in reasons[0].detail
        assert "L2" in reasons[0].detail

    def test_level_at_its_ceiling_is_not_a_clamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An enabled ladder sitting at a level nothing lowers is nominal."""
        (tmp_path / "kstrl.toml").write_text(
            "[autonomy]\nenabled = true\n", encoding="utf-8",
        )
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)

        assert safe_mode_reasons(tmp_path) == []


class TestQueue:
    def test_paused_queue_carries_the_pause_reason(
        self, tmp_path: Path,
    ) -> None:
        Queue(tmp_path, QueueConfig()).pause(
            reason="poison breaker tripped", actor="test",
        )

        reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["queue"]
        assert reasons[0].detail == "poison breaker tripped"

    def test_unreadable_pause_marker_is_a_reason(
        self, tmp_path: Path,
    ) -> None:
        """Fail-closed: a corrupt marker means paused, never running."""
        from kstrl.statedir import CONTROL_PAUSE, control_file

        path = control_file(tmp_path, CONTROL_PAUSE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["queue"]
        assert "unreadable" in reasons[0].detail

    def test_a_lapsed_pause_is_not_a_reason(self, tmp_path: Path) -> None:
        """resume_after in the past means the queue admits work again,
        which is what the daemon's own is_paused asks."""
        Queue(tmp_path, QueueConfig()).pause(
            reason="daily budget", actor="test",
            resume_after="2020-01-01T00:00:00+00:00",
        )

        assert safe_mode_reasons(tmp_path) == []


class TestAdversarialSkipped:
    def test_skipped_review_counts_components_and_names_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        make_review_skipped(tmp_path, monkeypatch)

        reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["adversarial_skipped"]
        assert reasons[0].detail == (
            f"review did not run for 2 component(s) in run {RUN_ID}"
        )

    def test_review_and_security_are_separate_reasons(
        self, tmp_path: Path,
    ) -> None:
        sink = ev.JsonlSink(root_events(tmp_path))
        try:
            sink.emit(ev.PhaseSkipped(
                component="comp-a", phase="review", reason="mode=skip",
            ))
            sink.emit(ev.PhaseSkipped(
                component="comp-a", phase="security", reason="mode=skip",
            ))
        finally:
            sink.close()

        details = [r.detail for r in safe_mode_reasons(tmp_path)]

        assert len(details) == 2
        assert details[0].startswith("review did not run")
        assert details[1].startswith("security did not run")

    def test_a_skipped_verify_is_not_a_safe_mode_reason(
        self, tmp_path: Path,
    ) -> None:
        """Mechanical verification is reported per component by `ks
        status` already, and unlike review it cannot be dropped by budget
        exhaustion. Only the adversarial gates land here."""
        sink = ev.JsonlSink(root_events(tmp_path))
        try:
            sink.emit(ev.PhaseSkipped(
                component="comp-a", phase="verify", reason="--no-verify",
            ))
        finally:
            sink.close()

        assert safe_mode_reasons(tmp_path) == []

    def test_no_runs_is_no_reason(self, tmp_path: Path) -> None:
        assert safe_mode_reasons(tmp_path) == []


def root_events(root: Path) -> Path:
    return root / ".kstrl" / "runs" / RUN_ID / "events.jsonl"


# ---------------------------------------------------------------------------
# Composition: several reasons, and the duplicate rule
# ---------------------------------------------------------------------------


class TestComposition:
    def test_multiple_reasons_are_reported_in_evaluation_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two independent readers, both unhappy. The pair is autonomy
        plus queue rather than control_dir plus queue, because
        Queue.pause_state consults control_untrusted_reason itself and
        the second pair collapses to one reason by design."""
        make_autonomy_degraded(tmp_path, monkeypatch)
        make_queue_paused(tmp_path, monkeypatch)

        with pytest.warns(RuntimeWarning):
            reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["autonomy", "queue"]

    def test_the_control_dir_verdict_is_not_repeated_by_the_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Queue.pause_state returns the untrusted reason verbatim, so a
        naive predicate would print the same sentence under two labels
        and tell the operator there are two problems when there is one."""
        make_queue_paused(tmp_path, monkeypatch)
        make_control_dir_untrusted(tmp_path, monkeypatch)

        reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["control_dir"]

    def test_every_reason_carries_its_own_recovery_anchor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        make_autonomy_degraded(tmp_path, monkeypatch)
        make_queue_paused(tmp_path, monkeypatch)
        make_review_skipped(tmp_path, monkeypatch)

        with pytest.warns(RuntimeWarning):
            reasons = safe_mode_reasons(tmp_path)

        assert len(reasons) >= 3
        for reason in reasons:
            assert reason.recovery == RECOVERY[reason.source]


# ---------------------------------------------------------------------------
# The never-raises contract
# ---------------------------------------------------------------------------


class TestNeverRaises:
    def test_a_runs_path_that_is_a_file_becomes_a_reason(
        self, tmp_path: Path,
    ) -> None:
        """_v2_run_dirs swallows this into "no runs", which would read as
        "nothing was skipped" - a fail-open on a question about whether a
        gate ran."""
        runs = tmp_path / ".kstrl" / "runs"
        runs.parent.mkdir(parents=True, exist_ok=True)
        runs.write_text("i am a file", encoding="utf-8")

        reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["adversarial_skipped"]
        assert reasons[0].detail.startswith("could not read")

    def test_a_reader_that_raises_becomes_a_reason(
        self, tmp_path: Path,
    ) -> None:
        """max_level = 99 makes AutonomyConfig.__post_init__ raise. The
        predicate must report that it could not read the signal rather
        than propagate, and must still answer for the other three."""
        (tmp_path / "kstrl.toml").write_text(
            "[autonomy]\nenabled = true\nmax_level = 99\n", encoding="utf-8",
        )
        Queue(tmp_path, QueueConfig()).pause(reason="paused", actor="test")

        reasons = safe_mode_reasons(tmp_path)

        assert [r.source for r in reasons] == ["autonomy", "queue"]
        assert reasons[0].detail.startswith("could not read")
        # One reader failing did not hide the next one's answer.
        assert reasons[1].detail == "paused"

    def test_a_malformed_kstrl_toml_does_not_propagate(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[[[not toml", encoding="utf-8")

        reasons = safe_mode_reasons(tmp_path)

        assert all(r.detail.startswith("could not read") for r in reasons)


# ---------------------------------------------------------------------------
# The code and the runbook cannot drift apart
# ---------------------------------------------------------------------------


def _github_anchor(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s+", "-", slug)


class TestRecoveryAnchors:
    def test_every_anchor_resolves_to_a_runbook_heading(self) -> None:
        """Without this, ``recovery`` is a convention: a renamed heading
        would send an operator in safe mode to a section that no longer
        exists, and nothing would notice."""
        runbook = REPO_ROOT / "docs" / "runbook.md"
        anchors = {
            _github_anchor(line.lstrip("#").strip())
            for line in runbook.read_text(encoding="utf-8").splitlines()
            if line.startswith("#")
        }

        for source, recovery in sorted(RECOVERY.items()):
            path, _, anchor = recovery.partition("#")
            assert path == "docs/runbook.md", source
            assert anchor in anchors, (
                f"{source} points at #{anchor}, which is not a heading in "
                f"docs/runbook.md"
            )

    def test_the_runbook_has_the_safe_mode_section(self) -> None:
        runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(
            encoding="utf-8",
        )
        assert "\n## Safe mode\n" in runbook
