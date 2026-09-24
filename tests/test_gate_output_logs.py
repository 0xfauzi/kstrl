"""A failed gate's output is written where the operator can read it (#462).

Every test here drives the REAL ``ComponentPipeline._phase_verify`` over the
REAL ``run_mechanical_verification``, which runs each gate as a real
subprocess in a fixture directory, with a real ``JsonlSink`` writing the
run's ``events.jsonl``. The assertions are on what an operator can open: the
event line on disk, and the file it names.

Before #462 the event carried ``"Tests failed (exit code 1)"`` and nothing
under ``.kstrl/`` held the output: ``check_test_suite`` parsed it into the
retry context and dropped it.
"""

from __future__ import annotations

import io
import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from kstrl.events import JsonlSink
from kstrl.factory import ComponentResult
from kstrl.gateparse import GATE_LINT, GATE_TEST, GATE_TYPECHECK
from kstrl.manifest import Component
from kstrl.pipeline import ComponentPipeline, VerifyPhaseResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import (
    GATE_OUTPUT_MAX_CHARS,
    VerificationResult,
    VerifyConfig,
    run_mechanical_verification,
)
from tests.helpers.verify_phase import _pipeline, component

FAILING_TEST_NAME = "test_kstrl462_token_roundtrip_is_broken"
PYTHON = shlex.quote(sys.executable)


def _python(code: str) -> str:
    """A shell command running ``code`` under this interpreter."""
    return f"{PYTHON} -c {shlex.quote(code)}"


def _config(**commands: str) -> VerifyConfig:
    """Only the three gates, each ``true`` unless overridden."""
    return VerifyConfig(
        test_command=commands.get("test", "true"),
        typecheck_command=commands.get("typecheck", "true"),
        lint_command=commands.get("lint", "true"),
        check_bad_patterns=False,
        subprocess_timeout=120.0,
    )


def _project(tmp_path: Path) -> Path:
    """A fixture project whose one test fails, named by ``FAILING_TEST_NAME``."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_fixture.py").write_text(
        f"def {FAILING_TEST_NAME}():\n    assert 1 == 2, 'kstrl462 marker'\n",
        encoding="utf-8",
    )
    return project


class _Run:
    """One pipeline over ``project`` whose Phase 1 is the real verifier."""

    def __init__(self, project: Path, config: VerifyConfig, comp: Component) -> None:
        def real_phase_1(*args: Any, **kwargs: Any) -> VerificationResult:
            worktree, prd, base, allowed, _ignored = args
            return run_mechanical_verification(worktree, prd, base, allowed, config, **kwargs)

        self.project = project
        self.comp = comp
        self.narration = io.StringIO()
        self.pipeline: ComponentPipeline = _pipeline(
            project,
            comp,
            VerificationResult(passed=True),
            ui=PlainUI(no_color=True, file=self.narration),
            verify_hook=real_phase_1,
        )
        self.events_file = project / ".kstrl" / "runs" / "run-test" / "events.jsonl"
        self.pipeline.bus.add_sink(JsonlSink(self.events_file))

    def phase_1(self) -> VerifyPhaseResult:
        return self.pipeline._phase_verify(
            self.comp,
            ComponentResult(self.comp.id, success=True, iterations=1, duration_seconds=1.0),
            self.project,
        )

    def verification_events(self) -> list[dict[str, Any]]:
        """The ``data`` of every ``verification_result`` line on disk."""
        rows = [
            json.loads(line) for line in self.events_file.read_text(encoding="utf-8").splitlines()
        ]
        return [row["data"] for row in rows if row["event"] == "verification_result"]


def _debug_dir(project: Path, comp: Component) -> Path:
    return project / ".kstrl" / "debug" / "run-test" / comp.id


def test_a_failing_test_gate_leaves_a_log_naming_the_failing_test(tmp_path: Path) -> None:
    """The issue's acceptance: the log holds the failing test's name, and
    the event names the log. The failure then goes through the pipeline's
    own routing, which stamps the directory holding the log onto the
    component: the path the failure summary, ``ks status`` and the TUI
    retry screen print as the component's raw outputs."""
    project = _project(tmp_path)
    comp = component()
    run = _Run(
        project,
        _config(test=f"{PYTHON} -m pytest -q -p no:cacheprovider test_fixture.py"),
        comp,
    )

    failure = run.phase_1().failure
    assert failure is not None
    run.pipeline._route_failure(comp, failure)
    assert comp.evidence_debug_dir == str(_debug_dir(project, comp))

    (data,) = run.verification_events()
    expected = _debug_dir(project, comp) / "attempt-1" / f"{GATE_TEST}.log"
    assert data["gate_logs"] == [str(expected)]
    log = expected.read_text(encoding="utf-8")
    assert FAILING_TEST_NAME in log
    assert "kstrl462 marker" in log
    # The event stays small: the output is in the file, not the line.
    assert FAILING_TEST_NAME not in json.dumps(data)


@pytest.mark.parametrize("gate", [GATE_TYPECHECK, GATE_LINT])
def test_a_failing_typecheck_or_lint_gate_leaves_a_log(tmp_path: Path, gate: str) -> None:
    """The same record for the other two gates. Stdout and stderr both
    reach the file, stdout first, as the gate joins them."""
    project = _project(tmp_path)
    comp = component()
    failing = _python(
        "import sys; print('kstrl462 stdout from the gate'); "
        "print('kstrl462 stderr from the gate', file=sys.stderr); sys.exit(1)"
    )
    key = "typecheck" if gate == GATE_TYPECHECK else "lint"
    run = _Run(project, _config(**{key: failing}), comp)

    run.phase_1()

    (data,) = run.verification_events()
    expected = _debug_dir(project, comp) / "attempt-1" / f"{gate}.log"
    assert data["gate_logs"] == [str(expected)]
    log = expected.read_text(encoding="utf-8")
    assert log.index("kstrl462 stdout from the gate") < log.index("kstrl462 stderr from the gate")


def test_a_retry_does_not_overwrite_the_earlier_attempt_s_log(tmp_path: Path) -> None:
    """The issue's question was about attempt 2 of a component that took
    three. Each attempt keeps its own file."""
    project = _project(tmp_path)
    comp = component()
    run = _Run(
        project,
        _config(test=f"{PYTHON} -m pytest -q -p no:cacheprovider test_fixture.py"),
        comp,
    )

    run.phase_1()
    comp.retries = 1
    run.phase_1()

    first, second = run.verification_events()
    attempt_1 = _debug_dir(project, comp) / "attempt-1" / f"{GATE_TEST}.log"
    attempt_2 = _debug_dir(project, comp) / "attempt-2" / f"{GATE_TEST}.log"
    assert first["gate_logs"] == [str(attempt_1)]
    assert second["gate_logs"] == [str(attempt_2)]
    assert FAILING_TEST_NAME in attempt_1.read_text(encoding="utf-8")
    assert FAILING_TEST_NAME in attempt_2.read_text(encoding="utf-8")


def test_output_over_the_bound_keeps_both_ends_and_says_what_it_dropped(
    tmp_path: Path,
) -> None:
    """Truncated at a stated bound, and the truncation is marked."""
    project = _project(tmp_path)
    comp = component()
    total = GATE_OUTPUT_MAX_CHARS + 100_000
    body = total - len("HEAD") - len("TAIL")
    failing = _python(f"import sys; sys.stdout.write('HEAD' + 'x' * {body} + 'TAIL'); sys.exit(1)")
    run = _Run(project, _config(lint=failing), comp)

    run.phase_1()

    (data,) = run.verification_events()
    (path,) = data["gate_logs"]
    log = Path(path).read_text(encoding="utf-8")
    assert log.startswith("HEAD")
    assert log.endswith("TAIL")
    dropped = total - GATE_OUTPUT_MAX_CHARS
    assert f"output truncated, {dropped} of {total} characters dropped here" in log
    assert len(log) < GATE_OUTPUT_MAX_CHARS + 200


def test_output_at_the_bound_is_kept_whole(tmp_path: Path) -> None:
    """The boundary: exactly the bound is not truncated."""
    project = _project(tmp_path)
    comp = component()
    failing = _python(f"import sys; sys.stdout.write('y' * {GATE_OUTPUT_MAX_CHARS}); sys.exit(1)")
    run = _Run(project, _config(lint=failing), comp)

    run.phase_1()

    (data,) = run.verification_events()
    (path,) = data["gate_logs"]
    log = Path(path).read_text(encoding="utf-8")
    assert log == "y" * GATE_OUTPUT_MAX_CHARS


def test_passing_gates_write_nothing(tmp_path: Path) -> None:
    """No failure, no file and no path: the quiet default."""
    project = _project(tmp_path)
    comp = component()
    run = _Run(project, _config(), comp)

    run.phase_1()

    (data,) = run.verification_events()
    assert data["gate_logs"] == []
    assert not _debug_dir(project, comp).exists()


def test_a_failed_write_is_said_and_the_event_names_no_file(tmp_path: Path) -> None:
    """A log kstrl could not write is reported on the terminal, the event
    does not name a file that is not there, and Phase 1 still routes the
    gate failure."""
    project = _project(tmp_path)
    comp = component()
    # A FILE where the debug directory would go, so creating it fails.
    (project / ".kstrl").mkdir()
    (project / ".kstrl" / "debug").write_text("not a directory", encoding="utf-8")
    run = _Run(
        project,
        _config(test=f"{PYTHON} -m pytest -q -p no:cacheprovider test_fixture.py"),
        comp,
    )

    result = run.phase_1()

    (data,) = run.verification_events()
    assert data["gate_logs"] == []
    assert data["passed"] is False
    assert result.failure is not None
    assert f"could not write the {GATE_TEST} output" in run.narration.getvalue()
