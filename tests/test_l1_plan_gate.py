"""At L1 a person approves the plan before anything runs (#602).

The defect: the autonomy ladder records "plans: human-approved" for every
L1 run, in ``ks autonomy status`` and in the run's own
``autonomy_level_applied`` event, and nothing asked anyone.
``FlagBundle.auto_accept_plan`` is the only field on which L1 and L2
differ, and it had no reader, so an L1 run and an L2 run of the same
manifest behaved identically: the engineer ran, no ``checkpoint_requested``
was emitted and no inbox item was filed.

The fix: ``run_plan_gate`` reads the run's clamped bundle after every
pre-spend refusal and before anything is pushed, merged or scheduled. At L1
it asks through the interaction channel; only an answered approval runs the
plan, and anything else parks it as a ``plan_gate`` inbox item bound to a
digest of the plan. ``ks inbox approve`` and ``ks inbox reject`` decide the
park and re-enter ``ks factory``, as they do for a merge-gate park.

Two harnesses, both end to end. The ``ks`` CLI in a subprocess (the
harness of ``tests/test_merge_gate_park.py``: real git, a bare origin, a
shell engineer that commits one file, a stub ``gh``) drives every path on
which nobody can answer a prompt. ``run_factory`` in process drives the
paths where a person answers, through a scripted ``InteractionChannel``;
the code reviewer is patched to a pass there because the ladder forces a
hard review at every level and no LLM may run in the suite.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.autonomy import AutonomyState
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, run_factory
from kstrl.inbox import Inbox, InboxConfig, InboxItem
from kstrl.interaction import PromptKind, PromptRequest, PromptResponse
from kstrl.manifest import Manifest
from kstrl.review import ReviewResult
from kstrl.security import SecurityConfig
from kstrl.serve import RunOutcome, check_parked_merges, classify_run
from kstrl.ui.plain import PlainUI
from kstrl.verify_model import VerifyConfig
from tests.helpers.stack_confirmation import in_process_stack
from tests.test_merge_gate_park import (
    CMDS,
    ENGINEER,
    FACTORY_FLAGS,
    HTTP,
    _engineer_ran,
    _env,
    _git,
    _ks,
    _lines,
    _manifest_path,
    _repo,
)

pytestmark = pytest.mark.usefixtures("no_open_prs")

#: The ladder on, at the stored level (L1 unless a test saves another).
AUTONOMY = "[autonomy]\nenabled = true\n"

#: The ladder forces a hard review at every level. `true` prints nothing,
#: so a component that reaches review fails there as an infrastructure
#: error: these subprocess tests read whether the engineer RAN, never
#: whether a component completed. Without it the reviewer falls back to
#: the engineer's command (#498), which would write to the engineer log.
REVIEWER = ("--review-agent-cmd", "true")


# --- reading what a run left ---------------------------------------------------


def _factory(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _ks(
        root, env, "factory", "--manifest", str(_manifest_path(root)), *FACTORY_FLAGS, *REVIEWER
    )


def _runs(root: Path) -> list[list[dict[str, Any]]]:
    """Every run's events, oldest run first."""
    runs = root / ".kstrl" / "runs"
    return [
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        for path in sorted(runs.glob("*/events.jsonl"))
    ]


def _plan_asks(rows: list[dict[str, Any]]) -> list[str]:
    return [
        r["data"]["question"]
        for r in rows
        if r["event"] == "checkpoint_requested" and r["data"]["kind"] == "plan"
    ]


def _plan_decisions(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (r["data"]["decision"], r["data"]["decided_by"])
        for r in rows
        if r["event"] == "checkpoint_resolved" and r["data"]["kind"] == "plan"
    ]


def _plan_items(root: Path) -> list[InboxItem]:
    return [i for i in Inbox(root, InboxConfig.load(root)).items() if str(i.kind) == "plan_gate"]


def _awaiting(root: Path) -> str:
    """The manifest's planAwaitingApproval, read from the file itself."""
    data = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
    return str(data.get("planAwaitingApproval", ""))


def _parked(tmp_path: Path, toml: str = AUTONOMY) -> tuple[Path, dict[str, str], InboxItem]:
    """One `ks factory` run at L1 with nobody to ask; returns the open plan item."""
    root = _repo(tmp_path, toml)
    env = _env(tmp_path)
    first = _factory(root, env)
    out = first.stdout + first.stderr
    assert first.returncode == 1, out
    items = _plan_items(root)
    assert [str(i.status) for i in items] == ["open"], out
    return root, env, items[0]


# --- nobody to ask: the real CLI in a subprocess ------------------------------


class TestNobodyToAsk:
    def test_l1_without_a_prompt_parks_the_plan_and_runs_nothing(self, tmp_path: Path) -> None:
        """RED before the fix: the engineer ran, nothing was asked, no item was filed."""
        # `--yes` is in the flags: it answers the launch confirm, never the plan.
        assert "--yes" in FACTORY_FLAGS
        root, _env_, item = _parked(tmp_path)

        assert _engineer_ran(tmp_path) == []
        digest = str(item.evidence.get("plan_digest"))
        assert len(digest) == 64, item.evidence
        assert _awaiting(root) == digest
        # Nothing was stamped either: the feature base is recorded when the
        # approved plan starts, not when it parks (#481).
        manifest = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
        assert manifest.get("featureBaseSha", "") == "", manifest
        (rows,) = _runs(root)
        assert len(_plan_asks(rows)) == 1, rows
        assert _plan_decisions(rows) == [("parked", "inbox")]
        # The ask came before anything was scheduled.
        assert not any(r["event"] == "component_started" for r in rows)
        assert not any("pr create" in line for line in _lines(tmp_path / "gh.log"))
        # serve reads the park as waiting on a person and admits no new work.
        verdict = classify_run(
            root, run=RunOutcome(returncode=1), manifest_path=_manifest_path(root)
        )
        assert str(verdict.verdict) == "awaiting_approval", verdict.reason
        assert not check_parked_merges(root).allowed

    def test_l1_with_no_inbox_and_no_prompt_refuses(self, tmp_path: Path) -> None:
        root = _repo(tmp_path, AUTONOMY)
        env = _env(tmp_path)
        env["KSTRL_INBOX_ENABLED"] = "0"

        result = _factory(root, env)
        out = result.stdout + result.stderr

        assert result.returncode == 2, out
        # #696 flag day: the [stack] checkpoint is ahead of the L1 plan gate's
        # own escalation now, and it hits the identical "nobody to ask"
        # condition first - the stack this suite's _repo confirms lives in
        # the inbox, which [inbox]=0 makes unreadable, and nothing answered
        # the prompt either. The behaviour under test here (refuses, no
        # engineer paid, nothing parked) is unchanged; only which gate says so.
        assert "is not confirmed, and [inbox] is disabled" in out, out
        assert _engineer_ran(tmp_path) == []
        assert _awaiting(root) == ""

    def test_a_stored_l2_clamped_to_l1_by_max_level_asks(self, tmp_path: Path) -> None:
        root = _repo(tmp_path, AUTONOMY + "max_level = 1\n")
        AutonomyState(level=2).save(root)
        env = _env(tmp_path)

        result = _factory(root, env)
        out = result.stdout + result.stderr

        assert result.returncode == 1, out
        assert _engineer_ran(tmp_path) == []
        assert [str(i.status) for i in _plan_items(root)] == ["open"], out

    @pytest.mark.parametrize("toml", ["l2", "ladder_off"])
    def test_l2_and_a_disabled_ladder_run_the_plan_without_asking(
        self, tmp_path: Path, toml: str
    ) -> None:
        """The L1/L2 pair: the same manifest, and only L1 asks."""
        root = _repo(tmp_path, AUTONOMY if toml == "l2" else "")
        if toml == "l2":
            AutonomyState(level=2).save(root)
        env = _env(tmp_path)

        result = _factory(root, env)
        out = result.stdout + result.stderr

        assert _engineer_ran(tmp_path)[:1] == [HTTP], out
        assert _plan_items(root) == []
        assert all(_plan_asks(rows) == [] for rows in _runs(root))


# --- the inbox decides a parked plan -------------------------------------------


class TestTheInboxDecides:
    def test_ks_inbox_approve_runs_the_parked_plan(self, tmp_path: Path) -> None:
        root, env, item = _parked(tmp_path)

        approved = _ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")
        out = approved.stdout + approved.stderr

        assert _engineer_ran(tmp_path) == [HTTP], out
        # The park ended its run; the approval run does not take it over as
        # an interrupted one (#463), which would count its spend twice.
        assert "Carried from interrupted run" not in out, out
        assert [str(i.status) for i in _plan_items(root)] == ["approved"], out
        assert _awaiting(root) == ""
        manifest = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
        assert len(manifest.get("featureBaseSha", "")) == 40, manifest
        _park_run, approval_run = _runs(root)
        assert _plan_asks(approval_run) == []
        assert _plan_decisions(approval_run) == [("approved", "inbox")]

    def test_ks_inbox_reject_refuses_the_parked_plan(self, tmp_path: Path) -> None:
        root, env, item = _parked(tmp_path)

        rejected = _ks(
            root, env, "inbox", "reject", item.id, "--comment", "wrong plan", "--ui", "plain"
        )
        out = rejected.stdout + rejected.stderr

        assert rejected.returncode == 2, out
        assert _engineer_ran(tmp_path) == []
        assert [str(i.status) for i in _plan_items(root)] == ["rejected"], out
        # The park is over, so serve is not wedged behind it.
        assert _awaiting(root) == ""
        assert check_parked_merges(root).allowed
        _park_run, reject_run = _runs(root)
        assert _plan_decisions(reject_run) == [("rejected", "inbox")]

    def test_approving_a_plan_the_manifest_no_longer_waits_on_records_nothing(
        self, tmp_path: Path
    ) -> None:
        root, env, item = _parked(tmp_path)
        # A new spec's decompose writes a manifest with no park in it.
        manifest = Manifest.load(_manifest_path(root))
        manifest.plan_awaiting_approval = ""
        manifest.save(_manifest_path(root))

        approved = _ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")
        out = approved.stdout + approved.stderr

        assert approved.returncode == 2, out
        assert "no parked plan to approve" in out, out
        assert [str(i.status) for i in _plan_items(root)] == ["open"], out
        assert _engineer_ran(tmp_path) == []

    @pytest.mark.parametrize("change", ["criterion", "allowed_paths"])
    def test_an_approval_does_not_carry_over_to_a_changed_plan(
        self, tmp_path: Path, change: str
    ) -> None:
        root, env, item = _parked(tmp_path)
        Inbox(root, InboxConfig.load(root)).approve(item.id, actor="test")
        prd = root / "scripts" / "kstrl" / "feature" / HTTP / "prd.json"
        data = json.loads(prd.read_text(encoding="utf-8"))
        if change == "criterion":
            data["userStories"][0]["acceptanceCriteria"] = ["AC1, and also something new"]
        else:
            data["allowedPaths"] = ["src/"]
        prd.write_text(json.dumps(data), encoding="utf-8")

        again = _factory(root, env)
        out = again.stdout + again.stderr

        assert again.returncode == 1, out
        assert _engineer_ran(tmp_path) == []
        statuses = sorted(str(i.status) for i in _plan_items(root))
        assert statuses == ["approved", "open"], out
        assert _awaiting(root) not in ("", item.evidence["plan_digest"])


# --- a person answers: run_factory in process ------------------------------------


class _Channel:
    """Answers the FIRST request as scripted and leaves every later one
    unanswered. The plan checkpoint is the first request of an L1 run.
    ``choice=None`` answers with the request's own default, which is what
    a person pressing Enter sends."""

    def __init__(self, *, answered: bool, choice: int | None) -> None:
        self._answer = (answered, choice)
        self.asked: list[PromptRequest] = []

    def can_prompt(self) -> bool:
        return True

    def request(self, req: PromptRequest) -> PromptResponse:
        self.asked.append(req)
        answered, choice = self._answer if len(self.asked) == 1 else (False, None)
        # Enter takes the default, which is an answer; nobody answering is None (#647).
        picked = (req.default if choice is None else choice) if answered else None
        return PromptResponse(request_id=req.request_id, choice=picked)


def _in_process(root: Path, channel: _Channel, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """One real run_factory over the saved manifest, with the flags the
    subprocess tests pass, a real engineer, and the reviewer patched to a pass."""
    monkeypatch.setenv("ENGINEER_LOG", str(root.parent / "engineer.log"))
    # As `_env` does for the subprocess: the distiller would run the
    # engineer's command a second time per component.
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    scripts = root / "scripts" / "kstrl"
    (scripts / "prompt.md").write_text("p", encoding="utf-8")
    config = FactoryConfig(
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        create_prs=False,
        review_mode="skip",
        security_config=SecurityConfig(mode="skip"),
        project_stack=in_process_stack({"tests": "true", "typecheck": "true", "lint": "true"}),
        verify_config=VerifyConfig(
            project_stack=in_process_stack({"tests": "true", "typecheck": "true", "lint": "true"}),
            check_bad_patterns=False,
            subprocess_timeout=30.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=scripts / "prompt.md",
        prd_file=scripts / "prd.json",
        sleep_seconds=0,
        agent_cmd=ENGINEER,
        ui_mode="plain",
        no_color=True,
    )
    out = io.StringIO()
    with patch("kstrl.factory.run_review", return_value=ReviewResult(passed=True, mode="hard")):
        result = run_factory(
            Manifest.load(_manifest_path(root)),
            config,
            base,
            PlainUI(no_color=True, file=out),
            root,
            manifest_path=_manifest_path(root),
            interaction=channel,
        )
    return result.exit_code, out.getvalue()


def _answerable_repo(tmp_path: Path) -> Path:
    """``_repo`` at L1 with no PRD stories. A story the engineer claims done
    needs the reviewer to confirm it at L1 (R10.3's claim gate), and the
    patched reviewer confirms nothing."""
    root = _repo(tmp_path, AUTONOMY)
    for prd in (root / "scripts" / "kstrl" / "feature").glob("*/prd.json"):
        data = json.loads(prd.read_text(encoding="utf-8"))
        prd.write_text(json.dumps({**data, "userStories": []}), encoding="utf-8")
    _git(root, "commit", "-qam", "no stories")
    _git(root, "push", "-q", "origin", "main")
    return root


class TestAPersonAnswers:
    def test_an_answered_approval_runs_the_plan_and_records_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _answerable_repo(tmp_path)
        channel = _Channel(answered=True, choice=0)

        _code, out = _in_process(root, channel, monkeypatch)

        assert channel.asked[0].kind is PromptKind.CHECKPOINT
        assert channel.asked[0].header.startswith("Approve the plan for p:"), channel.asked[0]
        assert _engineer_ran(tmp_path) == [HTTP, CMDS], out
        (rows,) = _runs(root)
        names = [r["event"] for r in rows]
        assert names.index("checkpoint_requested") < names.index("component_started")
        assert _plan_decisions(rows) == [("approved", "operator")]
        (item,) = _plan_items(root)
        assert (str(item.status), item.decided_by) == ("approved", "operator")

    @pytest.mark.parametrize(
        ("answered", "choice"),
        [(False, 0), (True, 7), (True, 2), (True, None)],
        ids=["unanswered", "out_of_range", "decide_later", "enter_takes_the_default"],
    )
    def test_anything_but_an_answered_approval_parks_the_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answered: bool, choice: int | None
    ) -> None:
        root = _answerable_repo(tmp_path)

        code, out = _in_process(root, _Channel(answered=answered, choice=choice), monkeypatch)

        assert code == 1, out
        assert _engineer_ran(tmp_path) == [], out
        assert [str(i.status) for i in _plan_items(root)] == ["open"], out
        (rows,) = _runs(root)
        assert _plan_decisions(rows) == [("parked", "inbox")]

    def test_an_answered_rejection_refuses_with_exit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _answerable_repo(tmp_path)

        code, out = _in_process(root, _Channel(answered=True, choice=1), monkeypatch)

        assert code == 2, out
        assert _engineer_ran(tmp_path) == [], out
        assert [str(i.status) for i in _plan_items(root)] == ["rejected"], out
        assert _awaiting(root) == ""

    def test_a_resumed_run_of_an_approved_plan_does_not_ask_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _answerable_repo(tmp_path)
        _in_process(root, _Channel(answered=True, choice=0), monkeypatch)
        # The first run approved the plan and completed both components, so
        # every status the plan's digest must ignore has changed.
        statuses = [c.status for c in Manifest.load(_manifest_path(root)).components]
        assert statuses == ["completed", "completed"], statuses
        second = _Channel(answered=True, choice=1)

        _in_process(root, second, monkeypatch)

        assert second.asked == []
        _first, resumed = _runs(root)
        assert _plan_asks(resumed) == []
        assert _plan_decisions(resumed) == [("approved", "inbox")]
