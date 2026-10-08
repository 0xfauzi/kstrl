"""#654 slice 7: a GitHub issue with the human `bug` label.

A bug report takes the same `ks factory` path as other work (owner
decision Q2 of 2026-10-08). The label changes two things only: the run
`ks serve` launches gets `--design-acceptance`, and the spec built from
the issue carries BUG_REPORT_PROMPT, which asks for at least one
acceptance check that fails on the base.

End to end: the real `serve_cycle` polls a stubbed `gh`, writes the queue
item to disk, claims it and launches the real `subprocess_factory_runner`
through `_default_runner`. Only `sys.executable` is replaced, by the
recorder of `tests/test_serve_seam.py`, which keeps the argv and the spec
the child was given. The argv is then parsed by the real `ks factory`
command. That the factory gives the spec to the verification designer
under `--design-acceptance` is `tests/test_acceptance_design_e2e.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import click
import pytest

from kstrl.cli import cli
from kstrl.intake_github import BUG_REPORT_PROMPT
from kstrl.serve import ServeConfig, serve_cycle
from tests.helpers.stack_confirmation import confirm_stack, write_stack
from tests.test_intake_github import _GhStub, _issue, _issue_payload
from tests.test_serve_seam import _enable_github_intake, _install_stub_interpreter

#: Nothing here is about flow control; the fixture's docstring in
#: tests/conftest.py says why the R10.7 bound has to be held open.
pytestmark = pytest.mark.usefixtures("no_open_prs")

BODY = "Running `widget ''` exits 1 with a traceback."


@dataclass(frozen=True)
class Launched:
    """What the launched child recorded, read at the moment it ran."""

    argv: list[str]
    spec_text: str


def _launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labels: list[str]) -> Launched:
    """One serve cycle over one issue carrying ``labels`` beside the trigger."""
    _enable_github_intake(tmp_path)
    write_stack(tmp_path)
    confirm_stack(tmp_path)
    record = _install_stub_interpreter(tmp_path, monkeypatch)
    issue = _issue(9, title="Crash on empty input", body=BODY)
    issue["labels"] = [{"name": "kstrl:queued"}, *({"name": name} for name in labels)]
    with patch("kstrl.intake_github.run_gh", _GhStub(issues=_issue_payload(issue))):
        serve_cycle(tmp_path, config=ServeConfig(caffeinate=False, factory_timeout_seconds=60.0))
    assert record.exists(), "the cycle launched no factory"
    raw: dict[str, object] = json.loads(record.read_text(encoding="utf-8"))
    argv = raw["argv"]
    spec_text = raw["spec_text"]
    assert isinstance(argv, list) and isinstance(spec_text, str)
    assert BODY in spec_text, f"the child was not given the issue's spec: {spec_text!r}"
    return Launched([str(arg) for arg in argv], spec_text)


def _parsed_design_acceptance(tmp_path: Path, launched: Launched) -> object:
    """The ``design_acceptance`` the real `ks factory` reads from the argv.

    The recorded ``--spec`` path is gone once the cycle moves the item, and
    the option needs an existing file, so the spec the child read is
    written back and the argv points at it.
    """
    argv = list(launched.argv)
    seen = tmp_path / "seen_spec.md"
    seen.write_text(launched.spec_text, encoding="utf-8")
    argv[argv.index("--spec") + 1] = str(seen)
    assert argv[:3] == ["-m", "kstrl", "factory"], argv
    command = cli.commands["factory"]
    ctx = click.Context(command, info_name="factory")
    command.parse_args(ctx, argv[3:])
    return ctx.params["design_acceptance"]


@pytest.mark.parametrize("label", ["bug", "Bug"])
def test_a_bug_report_launches_the_factory_with_designed_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """GitHub compares label names without regard to case, so `Bug` is the
    same label as `bug`."""
    launched = _launch(tmp_path, monkeypatch, [label])

    assert "--design-acceptance" in launched.argv, launched.argv
    assert _parsed_design_acceptance(tmp_path, launched) is True
    spec = launched.spec_text
    assert BUG_REPORT_PROMPT in spec, spec
    assert spec.index(BUG_REPORT_PROMPT) < spec.index(BODY), (
        "the instruction must come before the body, where truncation cannot remove it"
    )


@pytest.mark.parametrize("labels", [[], ["enhancement"], ["bugfix", "not-a-bug"]])
def test_an_issue_without_the_bug_label_runs_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labels: list[str]
) -> None:
    """The control: only the label `bug` itself turns the path on, not a
    label that contains the word."""
    launched = _launch(tmp_path, monkeypatch, labels)

    assert "--design-acceptance" not in launched.argv, launched.argv
    assert _parsed_design_acceptance(tmp_path, launched) is False
    assert "onBase" not in launched.spec_text, launched.spec_text
