"""Where Layer 0 ran, the pull request says nothing measured it (#696 decision 7).

#696 slice 8 removed Layer 0, the mechanical check that read one
language's test files for a change that weakened the suite. From autonomy
level 1 it used to block. Decision 7 keeps the gap visible rather than
silent: at level 1 and above Phase 1 records ``test_adequacy`` as not
measured, and the pull request carries it as a skipped phase. Below level
1 Layer 0 never ran, so nothing is recorded.

Each test is a real ``run_factory`` over a real git repository with a bare
origin and a real engineer subprocess that commits one file. Two things are
stubbed: ``gh``, an executable on PATH that keeps the body of the pull
request it is asked to open, and the code reviewer, because the ladder
forces hard review and no LLM may run.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from kstrl.autonomy import AutonomyLevel, AutonomyState
from kstrl.factory import FactoryConfig, run_factory
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.interaction import PromptRequest, PromptResponse
from kstrl.manifest import Manifest
from kstrl.review import CriterionReview, ReviewResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers.executables import write_executable
from tests.helpers.plan_approval import approve_plan
from tests.helpers.stack_confirmation import in_process_stack
from tests.spine_utils import (
    base_config,
    component,
    factory_config,
    git,
    init_kstrl_repo,
    make_manifest,
)

#: The words the pull request uses for the gap; ``verify.LAYER0_NOT_MEASURED``.
GAP = "Layer 0 not measured"

#: `pr create` keeps the value after `--body` in $GH_BODY; `pr view` says the
#: pull request merged.
FAKE_GH = """#!/bin/sh
if [ "$1" = "auth" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  prev=""
  for a in "$@"; do
    if [ "$prev" = "--body" ]; then printf '%s' "$a" > "$GH_BODY"; fi
    prev="$a"
  done
  echo "https://github.com/o/r/pull/41"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  printf '{"state": "MERGED", "mergeCommit": null}\\n'
  exit 0
fi
echo "[]"
exit 0
"""

ENGINEER = (
    "echo work > work.txt && git add -A && git commit -q -m work && "
    "echo '<promise>COMPLETE</promise>'"
)

PASS = ReviewResult(
    passed=True,
    mode="hard",
    criteria=[
        CriterionReview(
            criterion="AC1", verdict="pass", explanation="ok", suggestion="", story_id="US-001"
        )
    ],
)


class _NoOne:
    """Nobody at the merge gate, so a run at L1 and up parks before it pushes."""

    def can_prompt(self) -> bool:
        return False

    def request(self, req: PromptRequest) -> PromptResponse:
        return PromptResponse(request_id=req.request_id, choice=None)


@pytest.fixture(autouse=True)
def _stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_executable(bindir / "gh", FAKE_GH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_BODY", str(tmp_path / "pr-body.md"))
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    monkeypatch.delenv("KSTRL_AUTONOMY_ENABLED", raising=False)
    monkeypatch.setattr("kstrl.factory.run_review", lambda *a, **k: PASS)


def _factory(
    root: Path, manifest: Manifest, out: io.StringIO, config: FactoryConfig | None = None
) -> None:
    run_factory(
        manifest,
        config or factory_config(create_prs=True),
        base_config(root, agent_cmd=ENGINEER),
        PlainUI(no_color=True, file=out),
        root,
        interaction=_NoOne(),
    )


def _repo(tmp_path: Path, level: AutonomyLevel | None) -> tuple[Path, Manifest]:
    """A repository with a bare origin, at ``level`` (None: the ladder off),
    its plan approved when the ladder needs that."""
    root = tmp_path / "repo"
    init_kstrl_repo(root, ("web",), with_origin=True)
    toml = "[inbox]\nenabled = true\n"
    if level is not None:
        toml += "[autonomy]\nenabled = true\n"
    (root / "kstrl.toml").write_text(toml, encoding="utf-8")
    git("add", "kstrl.toml", cwd=root)
    git("commit", "-q", "-m", "config", cwd=root)
    git("push", "-q", "origin", "main", cwd=root)
    manifest = make_manifest([component("web")])
    if level is not None:
        assert AutonomyState(level=int(level)).save(root) is None
        approve_plan(root, manifest)
    return root, manifest


def _journal(root: Path, event: str) -> list[dict[str, object]]:
    """The payload of every ``event`` in every run's journal: an approved
    merge resumes in a run of its own, which does not verify again."""
    return [
        record["data"]
        for events in sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
        for record in (json.loads(line) for line in events.read_text(encoding="utf-8").splitlines())
        if record.get("event") == event
    ]


def _run(tmp_path: Path, level: AutonomyLevel | None) -> tuple[str, list[dict[str, object]]]:
    """A factory run at ``level``, through the merge gate when the ladder
    parks it; the PR body and every verification payload the runs journalled."""
    root, manifest = _repo(tmp_path, level)
    out = io.StringIO()
    _factory(root, manifest, out)
    if level is not None:
        # The ladder parks the merge before anything is pushed; an approval
        # through the inbox lets the next run open the pull request.
        box = Inbox(root, InboxConfig.load(root))
        (item,) = [i for i in box.open_items() if i.kind is ItemKind.MERGE_GATE]
        box.approve(item.id, actor="operator")
        _factory(root, Manifest.load(root / "scripts" / "kstrl" / "manifest.json"), out)
    body_path = tmp_path / "pr-body.md"
    assert body_path.is_file(), out.getvalue()
    return body_path.read_text(encoding="utf-8"), _journal(root, "verification_result")


@pytest.mark.parametrize("level", [AutonomyLevel.L1_SUPERVISED, AutonomyLevel.L3_ENVELOPED_AUTO])
def test_from_level_1_the_pull_request_says_layer_0_was_not_measured(
    tmp_path: Path, level: AutonomyLevel
) -> None:
    body, verdicts = _run(tmp_path, level)

    assert f"**PHASE SKIPPED**: {GAP}" in body, body
    assert "test-weakening criterion" in body, body
    assert verdicts, "no verification_result event"
    assert all("test_adequacy:retired" in v["not_measured"] for v in verdicts), verdicts


def test_with_the_ladder_off_nothing_is_recorded(tmp_path: Path) -> None:
    """The control: Layer 0 never ran below level 1, so there is no gap to name."""
    body, verdicts = _run(tmp_path, None)

    assert GAP not in body, body
    assert verdicts, "no verification_result event"
    assert all(v["not_measured"] == [] for v in verdicts), verdicts


def test_a_failed_verification_names_the_gap_and_files_no_finding(tmp_path: Path) -> None:
    """At level 1 a failing Phase 1 still names the gap in its event, and
    files no phase-skip finding: a finding would turn a failure with no
    evidence into one serve reports as judged on its merits
    (``serve._merits_outcome``)."""
    root, manifest = _repo(tmp_path, AutonomyLevel.L1_SUPERVISED)
    # Green on the base, red once the engineer commits work.txt.
    red = in_process_stack({"tests": "test ! -f work.txt"})
    config = factory_config(
        create_prs=True,
        project_stack=red,
        verify_config=VerifyConfig(
            project_stack=red,
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=10.0,
        ),
    )
    out = io.StringIO()
    _factory(root, manifest, out, config)

    verdicts = _journal(root, "verification_result")
    assert verdicts, out.getvalue()
    assert not any(v["passed"] for v in verdicts), verdicts
    assert all("test_adequacy:retired" in v["not_measured"] for v in verdicts), verdicts
    skips = _journal(root, "phase_skipped")
    assert [s for s in skips if s["phase"] == "adequacy"] == [], skips
    assert not (tmp_path / "pr-body.md").exists()
