"""A component parked at the merge gate waits for a human, and approval merges it (#465).

The defect: the gate that `ks serve` forces on every remote item had no
interactive UI to ask, so it parked the component, and the pipeline
recorded the park as ``fail(..., check="merge_gate")``. The manifest said
FAILED, the dependent was cascade-SKIPPED, `serve` read the component's
findings as a spec-level failure and poisoned the item, and the only
approval path (`ks inbox retry`) reset the component to PENDING, after
which the next run refused on the reviewed branch as a stale one.

The fix gives the park its own status, AWAITING_APPROVAL: dependents wait,
`serve` leaves the item awaiting approval and says so on the issue, and
`ks inbox approve` re-enters `ks factory` on the same manifest, where the
approved branch is pushed, opened as a PR and merged, and the run
continues with the dependents. The engineer is not run again.

Every test drives the real `ks` CLI in a subprocess against a real git
repository with a real bare origin. The engineer is a shell command that
commits one file (``AGENT_CMD``), every verify command is ``true``, review
and security are skipped, and ``gh`` is a stub on PATH that records its
arguments and what the pushed branch pointed at when the PR was created.
The serve test runs the real ``serve_cycle`` with a runner that launches
the real `ks factory` through the real ``run_supervised``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from kstrl.findings import Finding
from kstrl.inbox import Inbox, InboxConfig, InboxItem
from kstrl.manifest import Component, Manifest
from kstrl.serve import (
    Outcome,
    RunOutcome,
    ServeConfig,
    SpendLedger,
    classify_run,
    run_supervised,
    serve_cycle,
)
from kstrl.workqueue import ItemSource, Queue, QueueConfig
from tests.helpers import gitrepo
from tests.helpers.executables import write_executable

pytestmark = pytest.mark.usefixtures("no_open_prs")

HTTP = "http"
CMDS = "cmds"
HTTP_BRANCH = f"kstrl/factory/{HTTP}"
REPO = "o/r"
ISSUE = 7

#: Records every call in $GH_LOG. `pr create` also records what origin's
#: copy of the head branch points at, in $GH_PUSHED, because a merged
#: PR's remote branch is deleted afterwards and cannot be read later.
FAKE_GH = """#!/bin/sh
printf '%s\\n' "gh $*" >> "$GH_LOG"
if [ "$1" = "auth" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  for a in "$@"; do case "$a" in --head=*) head="${a#--head=}";; esac; done
  git ls-remote origin "refs/heads/$head" >> "$GH_PUSHED"
  printf '%s' "$head" > "$GH_HEAD"
  echo "https://github.com/o/r/pull/41"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  # A real merge: origin's main moves to the PR head, so a dependent cut
  # from origin/main after this holds the merged work.
  git push -q origin "refs/heads/$(cat "$GH_HEAD"):refs/heads/main" || exit 1
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  printf '{"state": "MERGED", "mergeCommit": null}\\n'
  exit 0
fi
if [ "$1" = "issue" ] && [ "$2" = "comment" ]; then
  shift 2
  while [ $# -gt 0 ]; do
    if [ "$1" = "--body" ]; then printf '%s\\n' "$2" >> "$GH_COMMENTS"; fi
    shift
  done
  exit 0
fi
echo "[]"
exit 0
"""

#: The flags every factory run in this file is launched with. Every
#: verify command is `true`, so nothing but the merge gate can stop a
#: component.
FACTORY_FLAGS = (
    "--yes",
    "--ui",
    "plain",
    "--no-color",
    "--no-tui",
    "--max-parallel",
    "1",
    "--max-retries",
    "0",
    "--review-mode",
    "skip",
    "--contract-check",
    "skip",
    "--test-command",
    "true",
    "--typecheck-command",
    "true",
    "--lint-command",
    "true",
)

#: The engineer: records the worktree it ran in, commits one file named
#: after its component, and completes in one iteration.
ENGINEER = (
    'pwd >> "$ENGINEER_LOG" && echo work > "work-$(basename "$(pwd)").txt" && '
    "git add -A && git commit -q -m work && echo '<promise>COMPLETE</promise>'"
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8", timeout=30
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _repo(tmp_path: Path, toml: str = "") -> Path:
    """`http`, then `cmds` depending on it, with a bare origin and a saved manifest."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    components = []
    for cid, deps in ((HTTP, []), (CMDS, [HTTP])):
        prd = root / "scripts" / "kstrl" / "feature" / cid / "prd.json"
        prd.parent.mkdir(parents=True)
        story = {
            "id": "US-001",
            "title": "t",
            "acceptanceCriteria": ["AC1"],
            "priority": 1,
            "passes": True,
            "notes": "",
        }
        prd.write_text(
            json.dumps({"branchName": f"kstrl/factory/{cid}", "userStories": [story]}),
            encoding="utf-8",
        )
        components.append(
            {
                "id": cid,
                "title": cid,
                "description": "",
                "dependencies": deps,
                "prdPath": f"scripts/kstrl/feature/{cid}/prd.json",
                "branchName": f"kstrl/factory/{cid}",
            }
        )
    (root / "kstrl.toml").write_text("[inbox]\nenabled = true\n" + toml, encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    origin = tmp_path / "origin.git"
    gitrepo.git_in(tmp_path, "init", "-q", "--bare", str(origin))
    gitrepo.git_in(root, "remote", "add", "origin", str(origin))
    gitrepo.git_in(root, "push", "-q", "-u", "origin", "main")
    manifest = {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "p",
        "baseBranch": "main",
        "singlePr": False,
        "components": components,
    }
    template = tmp_path / "manifest.template.json"
    template.write_text(json.dumps(manifest), encoding="utf-8")
    shutil.copyfile(template, _manifest_path(root))
    return root


def _manifest_path(root: Path) -> Path:
    return root / "scripts" / "kstrl" / "manifest.json"


def _env(tmp_path: Path) -> dict[str, str]:
    """The caller's environment minus every kstrl knob, plus the stub gh and engineer."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL", "FACTORY_MAX_PARALLEL")
    }
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_executable(bindir / "gh", FAKE_GH)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["GH_LOG"] = str(tmp_path / "gh.log")
    env["GH_PUSHED"] = str(tmp_path / "gh.pushed")
    env["GH_HEAD"] = str(tmp_path / "gh.head")
    env["GH_COMMENTS"] = str(tmp_path / "gh.comments")
    env["ENGINEER_LOG"] = str(tmp_path / "engineer.log")
    env["AGENT_CMD"] = ENGINEER
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    return env


def _ks(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "kstrl", *args, "--root", str(root)],
        cwd=root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=300,
    )


def _factory(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _ks(
        root,
        env,
        "factory",
        "--manifest",
        str(_manifest_path(root)),
        *FACTORY_FLAGS,
        "--pause-before-pr-merge",
    )


def _status(root: Path, cid: str) -> str:
    comp = Manifest.load(_manifest_path(root)).get_component(cid)
    assert comp is not None
    return str(comp.status)


def _engineer_ran(tmp_path: Path) -> list[str]:
    log = tmp_path / "engineer.log"
    if not log.exists():
        return []
    return [Path(line).name for line in log.read_text(encoding="utf-8").split()]


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _park_item(root: Path) -> InboxItem:
    box = Inbox(root, InboxConfig.load(root))
    items = [item for item in box.open_items() if str(item.kind) == "merge_gate"]
    assert len(items) == 1, [(str(i.kind), i.title) for i in box.open_items()]
    return items[0]


def _parked_run(tmp_path: Path, toml: str = "") -> tuple[Path, dict[str, str], str]:
    """One factory run that parks `http`; returns the root, the env and the branch tip."""
    root = _repo(tmp_path, toml)
    env = _env(tmp_path)
    first = _factory(root, env)
    out = first.stdout + first.stderr
    assert first.returncode == 1, out
    return root, env, _git(root, "rev-parse", f"refs/heads/{HTTP_BRANCH}")


class TestTheParkIsItsOwnOutcome:
    def test_the_parked_component_waits_and_its_dependent_is_not_skipped(
        self, tmp_path: Path
    ) -> None:
        root, _env_, reviewed = _parked_run(tmp_path)
        assert _status(root, HTTP) == "awaiting_approval"
        assert _status(root, CMDS) == "pending"
        comp = Manifest.load(_manifest_path(root)).get_component(HTTP)
        assert comp is not None and comp.failed_check == ""
        item = _park_item(root)
        assert item.component == HTTP
        assert item.evidence.get("head_sha") == reviewed
        assert _engineer_ran(tmp_path) == [HTTP]
        # The gate withheld the push and the PR; nothing reached GitHub.
        assert not any("pr create" in line for line in _lines(tmp_path / "gh.log"))

    def test_a_park_nothing_can_approve_fails_at_the_gate_and_ks_retry_rebuilds_it(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        env = _env(tmp_path)
        # No inbox: no merge_gate item, so `ks inbox approve` has nothing to act on.
        env["KSTRL_INBOX_ENABLED"] = "0"
        first = _factory(root, env)
        out = first.stdout + first.stderr

        assert first.returncode == 1, out
        comp = Manifest.load(_manifest_path(root)).get_component(HTTP)
        assert comp is not None
        assert (comp.status, comp.failed_check) == ("failed", "merge_gate"), out
        assert "ks retry" in comp.error, comp.error
        assert _status(root, CMDS) == "skipped", out
        # serve is not wedged behind it, and does not call it a spec failure.
        # Imported here so the module still collects on a tree without it (red first).
        from kstrl.serve import check_parked_merges

        assert check_parked_merges(root).allowed
        verdict = classify_run(
            root, run=RunOutcome(returncode=1), manifest_path=_manifest_path(root)
        )
        assert str(verdict.verdict) != "spec_failure", verdict.reason

        _ks(root, env, "retry", HTTP, "--yes", "--ui", "plain", "--no-color")
        assert _engineer_ran(tmp_path) == [HTTP, HTTP]

    def test_a_parked_run_is_not_clean_so_the_release_gate_withholds(self, tmp_path: Path) -> None:
        """A park is incomplete, not failed, but it is still not clean: the
        R8.7 release gate (`factory.run_is_clean`) must withhold a release
        while `http` sits at AWAITING_APPROVAL, exactly as it withholds one
        while a component sits at MERGE_PENDING
        (`tests.test_spine_pr_failures.TestSpineReleaseRef.test_a_merge_pending_run_is_not_clean`).
        """
        root, _env_, _tip = _parked_run(
            tmp_path, '[release]\nenabled = true\nenvironment = "staging"\n'
        )
        runs = sorted((root / ".kstrl" / "runs").iterdir())
        rows = [
            json.loads(line)
            for line in (runs[-1] / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        completed = [r for r in rows if r["event"] == "factory_completed"]
        assert len(completed) == 1, completed
        assert completed[0]["data"]["release_withheld"] == "run_not_clean"


class TestApprovalMergesTheReviewedBranch:
    def test_approve_pushes_opens_and_merges_the_reviewed_branch_then_runs_the_dependent(
        self, tmp_path: Path
    ) -> None:
        root, env, reviewed = _parked_run(tmp_path)
        item = _park_item(root)

        approved = _ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")
        out = approved.stdout + approved.stderr

        # One PR was opened, from the branch the gate parked, and origin held
        # exactly the commit it parked when the PR was created.
        pushed = _lines(tmp_path / "gh.pushed")
        assert pushed == [f"{reviewed}	refs/heads/{HTTP_BRANCH}"], out
        assert _status(root, HTTP) == "completed", out
        # The engineer ran for the dependent, never again for http.
        assert _engineer_ran(tmp_path) == [HTTP, CMDS], out
        # Approval merges: one `gh pr merge`, and it happened before the
        # dependent was cut, so the dependent's branch holds http's work.
        merges = [line for line in _lines(tmp_path / "gh.log") if "pr merge" in line]
        assert len(merges) == 1, merges
        _git(root, "cat-file", "-e", f"kstrl/factory/{CMDS}:work-{HTTP}.txt")
        # The dependent met the same gate with no one to ask, so it parks too.
        assert _status(root, CMDS) == "awaiting_approval", out

    def test_an_approval_run_a_preflight_refuses_pushes_nothing(self, tmp_path: Path) -> None:
        root, env, _reviewed = _parked_run(tmp_path)
        item = _park_item(root)
        # An unmerged branch kstrl did not make, where the dependent's
        # branch goes: the stale-branch preflight refuses the run.
        tree = _git(root, "rev-parse", "main^{tree}")
        foreign = _git(root, "commit-tree", tree, "-p", "main", "-m", "foreign")
        _git(root, "update-ref", f"refs/heads/kstrl/factory/{CMDS}", foreign)

        approved = _ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")
        out = approved.stdout + approved.stderr

        assert approved.returncode == 2, out
        assert "stale component branches" in out, out
        # A refused run reached nothing: no push, no PR, still parked.
        assert not any("pr create" in line for line in _lines(tmp_path / "gh.log")), out
        assert _status(root, HTTP) == "awaiting_approval", out

    def test_inbox_retry_on_a_park_is_refused_and_names_approve(self, tmp_path: Path) -> None:
        root, env, _reviewed = _parked_run(tmp_path)
        item = _park_item(root)

        retried = _ks(root, env, "inbox", "retry", item.id, "--ui", "plain", "--no-color")
        out = retried.stdout + retried.stderr

        assert retried.returncode == 2, out
        assert "ks inbox approve" in out, out
        # Nothing moved: the reviewed work is still parked and still asks.
        assert _status(root, HTTP) == "awaiting_approval", out
        assert _park_item(root).id == item.id
        assert _engineer_ran(tmp_path) == [HTTP], out

    def test_a_branch_that_moved_after_the_park_is_not_merged(self, tmp_path: Path) -> None:
        root, env, _reviewed = _parked_run(tmp_path)
        item = _park_item(root)
        # A commit nobody reviewed lands on the parked branch. Plumbing,
        # because the park run removed its worktree when it ended.
        tree = _git(root, "rev-parse", f"{HTTP_BRANCH}^{{tree}}")
        moved = _git(root, "commit-tree", tree, "-p", HTTP_BRANCH, "-m", "unreviewed")
        _git(root, "update-ref", f"refs/heads/{HTTP_BRANCH}", moved)

        approved = _ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")
        out = approved.stdout + approved.stderr

        assert not any("pr create" in line for line in _lines(tmp_path / "gh.log")), out
        comp = Manifest.load(_manifest_path(root)).get_component(HTTP)
        assert comp is not None
        assert comp.status == "failed", out
        assert comp.failed_check == "merge_gate", out
        # The refusal names the commit it would not merge.
        assert moved in comp.error, comp.error
        assert _status(root, CMDS) == "skipped", out

    def test_reject_fails_the_parked_component_and_skips_its_dependent(
        self, tmp_path: Path
    ) -> None:
        root, env, _reviewed = _parked_run(tmp_path)
        item = _park_item(root)

        rejected = _ks(
            root, env, "inbox", "reject", item.id, "--comment", "no", "--ui", "plain", "--no-color"
        )
        out = rejected.stdout + rejected.stderr

        comp = Manifest.load(_manifest_path(root)).get_component(HTTP)
        assert comp is not None
        assert comp.status == "failed", out
        assert comp.failed_check == "hitl_reject", out
        assert _status(root, CMDS) == "skipped", out
        assert not any("pr create" in line for line in _lines(tmp_path / "gh.log")), out
        assert _engineer_ran(tmp_path) == [HTTP], out

    def test_an_approved_merge_github_has_not_confirmed_is_merge_pending(
        self, tmp_path: Path
    ) -> None:
        root, env, _reviewed = _parked_run(tmp_path)
        item = _park_item(root)
        # GitHub accepts the merge but never reports it MERGED.
        write_executable(
            tmp_path / "bin" / "gh", FAKE_GH.replace('"state": "MERGED"', '"state": "OPEN"')
        )
        env["FACTORY_MERGE_TIMEOUT"] = "2"
        approved = _ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")
        out = approved.stdout + approved.stderr
        assert _status(root, HTTP) == "merge_pending", out
        assert _status(root, CMDS) == "pending", out
        assert _engineer_ran(tmp_path) == [HTTP], out


def _real_factory_runner(
    template: Path, env: dict[str, str], calls: list[str]
) -> Callable[..., RunOutcome]:
    """What `serve` launches, with `--manifest` in place of `--spec`.

    `--spec` would run the architect, which is an LLM. Everything after the
    manifest is the real thing: the real `ks factory` in its own process
    group under the real `run_supervised`, with the gate flag `serve`
    resolved passed through the same way `subprocess_factory_runner` does.
    """

    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: Callable[[int], None] | None = None,
    ) -> RunOutcome:
        manifest = _manifest_path(root_dir)
        shutil.copyfile(template, manifest)
        calls.append(project_name)
        command = [
            sys.executable,
            "-m",
            "kstrl",
            "factory",
            "--manifest",
            str(manifest),
            "--root",
            str(root_dir),
            *FACTORY_FLAGS,
            "--pause-before-pr-merge" if pause_before_pr_merge else "--no-pause-before-pr-merge",
        ]
        return run_supervised(
            command, cwd=root_dir, env=env, timeout_seconds=300.0, on_spawn=on_spawn
        )

    return runner


class TestServeLeavesAParkedItemAwaitingApproval:
    def test_the_item_awaits_approval_and_the_next_item_waits_for_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path, f'[intake_github]\nenabled = true\nrepo = "{REPO}"\n')
        env = _env(tmp_path)
        monkeypatch.setenv("PATH", env["PATH"])
        monkeypatch.setenv("GH_LOG", env["GH_LOG"])
        monkeypatch.setenv("GH_COMMENTS", env["GH_COMMENTS"])
        monkeypatch.setenv("GH_PUSHED", env["GH_PUSHED"])
        monkeypatch.setenv("GH_HEAD", env["GH_HEAD"])
        queue = Queue(root, QueueConfig())
        remote = queue.add(
            "# spec\n",
            title="remote",
            source=ItemSource.GITHUB,
            source_ref=f"{REPO}#{ISSUE}",
            target_repo=REPO,
        )
        calls: list[str] = []
        runner = _real_factory_runner(tmp_path / "manifest.template.json", env, calls)

        first = serve_cycle(root, config=ServeConfig(caffeinate=False), runner=runner)

        current = queue.get(remote.item_id)
        assert current is not None
        assert str(current.state) == "awaiting_approval", first.reason
        assert str(first.verdict) == "awaiting_approval", first.reason
        assert HTTP in first.reason
        assert SpendLedger(root).read_state().consecutive_poison == 0
        edits = [line for line in _lines(tmp_path / "gh.log") if "issue edit" in line]
        assert any("--add-label kstrl:awaiting_approval" in line for line in edits), edits
        assert not any("--add-label kstrl:poison" in line for line in edits), edits
        comments = (tmp_path / "gh.comments").read_text(encoding="utf-8")
        assert "ks inbox approve" in comments
        assert "poison" not in comments.lower()

        queue.add("# second spec\n", title="second")
        second = serve_cycle(root, config=ServeConfig(caffeinate=False), runner=runner)

        assert len(calls) == 1, "the second item must not overwrite the parked run's manifest"
        assert second.ran_item == ""
        assert "merge gate" in second.skipped, second.skipped
        assert _status(root, HTTP) == "awaiting_approval"


class TestTheClassifierNeverBlamesAPark:
    """`serve` reads the manifest the run wrote; a park is not a verdict on the spec."""

    @staticmethod
    def _classify(tmp_path: Path, components: list[Component]) -> Outcome:
        path = tmp_path / "manifest.json"
        Manifest(
            version="1",
            spec_file="s.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=components,
            run_id="factory-x",
        ).save(path)
        return classify_run(tmp_path, run=RunOutcome(returncode=1), manifest_path=path)

    @staticmethod
    def _parked() -> Component:
        comp = Component(HTTP, HTTP, "", [], "a.json", HTTP_BRANCH, status="awaiting_approval")
        comp.findings = [
            Finding.from_review_concern("test_quality", "advisory", "a.py:1", "could be tighter")
        ]
        return comp

    def test_a_park_with_an_advisory_finding_awaits_approval(self, tmp_path: Path) -> None:
        dependent = Component(CMDS, CMDS, "", [HTTP], "b.json", "kstrl/factory/cmds")
        outcome = self._classify(tmp_path, [self._parked(), dependent])
        assert str(outcome.verdict) == "awaiting_approval", outcome.reason
        assert HTTP in outcome.reason
        assert not outcome.verdict.may_retry

    def test_a_failed_sibling_is_judged_and_the_park_is_not(self, tmp_path: Path) -> None:
        other = Component("other", "other", "", [], "c.json", "kstrl/factory/other")
        other.status = "failed"
        other.findings = [Finding.from_review_concern("correctness", "fail", "c.py:1", "wrong")]
        outcome = self._classify(tmp_path, [self._parked(), other])
        assert str(outcome.verdict) == "spec_failure", outcome.reason
        assert outcome.evidence["judged_failures"] == ["other"]


class TestTheDecisionRunSettlesTheQueueItem:
    """#464: the run `ks inbox approve` or `reject` starts moves serve's item on.

    Before #464 the item stayed in ``awaiting_approval/`` after the
    approval run with no PR URLs, and the GitHub issue kept the
    ``kstrl:awaiting_approval`` label. The join is the run id serve
    records when it parks the item.
    """

    @staticmethod
    def _served_park(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, dict[str, str], Queue, str]:
        root = _repo(tmp_path, f'[intake_github]\nenabled = true\nrepo = "{REPO}"\n')
        env = _env(tmp_path)
        for key in ("PATH", "GH_LOG", "GH_COMMENTS", "GH_PUSHED", "GH_HEAD"):
            monkeypatch.setenv(key, env[key])
        queue = Queue(root, QueueConfig())
        remote = queue.add(
            "# spec\n",
            title="remote",
            source=ItemSource.GITHUB,
            source_ref=f"{REPO}#{ISSUE}",
            target_repo=REPO,
        )
        runner = _real_factory_runner(tmp_path / "manifest.template.json", env, [])
        serve_cycle(root, config=ServeConfig(caffeinate=False), runner=runner)
        parked = queue.get(remote.item_id)
        assert parked is not None and str(parked.state) == "awaiting_approval"
        assert parked.last_run_id == Manifest.load(_manifest_path(root)).run_id
        return root, env, queue, remote.item_id

    def test_approval_moves_the_item_to_done_with_its_pr_urls_and_tells_the_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, env, queue, item_id = self._served_park(tmp_path, monkeypatch)

        first = _ks(
            root, env, "inbox", "approve", _park_item(root).id, "--ui", "plain", "--no-color"
        )
        out = first.stdout + first.stderr
        # http merged, and the dependent parked under the approval run's own id.
        assert _status(root, HTTP) == "completed", out
        assert _status(root, CMDS) == "awaiting_approval", out
        still = queue.get(item_id)
        assert still is not None and str(still.state) == "awaiting_approval", out
        assert still.last_run_id == Manifest.load(_manifest_path(root)).run_id, out

        second = _ks(
            root, env, "inbox", "approve", _park_item(root).id, "--ui", "plain", "--no-color"
        )
        out = second.stdout + second.stderr
        assert second.returncode == 0, out
        assert _status(root, CMDS) == "completed", out

        done = queue.get(item_id)
        assert done is not None
        assert str(done.state) == "done", out
        assert done.pr_urls == ("https://github.com/o/r/pull/41",), out
        edits = [line for line in _lines(tmp_path / "gh.log") if "issue edit" in line]
        assert "--add-label kstrl:done" in edits[-1], edits
        comments = (tmp_path / "gh.comments").read_text(encoding="utf-8")
        assert "**kstrl: done**" in comments
        assert "https://github.com/o/r/pull/41" in comments

    def test_rejection_poisons_the_item_and_tells_the_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, env, queue, item_id = self._served_park(tmp_path, monkeypatch)

        rejected = _ks(
            root,
            env,
            "inbox",
            "reject",
            _park_item(root).id,
            "--comment",
            "no",
            "--ui",
            "plain",
            "--no-color",
        )
        out = rejected.stdout + rejected.stderr

        poisoned = queue.get(item_id)
        assert poisoned is not None
        assert str(poisoned.state) == "poison", out
        assert poisoned.poison_reason, out
        edits = [line for line in _lines(tmp_path / "gh.log") if "issue edit" in line]
        assert "--add-label kstrl:poison" in edits[-1], edits

    def test_a_refused_approval_run_leaves_the_item_awaiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, env, queue, item_id = self._served_park(tmp_path, monkeypatch)
        # The stale-branch preflight refuses the run with exit 2, which the
        # classifier reads before the manifest.
        tree = _git(root, "rev-parse", "main^{tree}")
        foreign = _git(root, "commit-tree", tree, "-p", "main", "-m", "foreign")
        _git(root, "update-ref", f"refs/heads/kstrl/factory/{CMDS}", foreign)

        approved = _ks(
            root, env, "inbox", "approve", _park_item(root).id, "--ui", "plain", "--no-color"
        )
        out = approved.stdout + approved.stderr

        assert approved.returncode == 2, out
        waiting = queue.get(item_id)
        assert waiting is not None and str(waiting.state) == "awaiting_approval", out
        edits = [line for line in _lines(tmp_path / "gh.log") if "issue edit" in line]
        assert not any("--add-label kstrl:poison" in line for line in edits), edits

    def test_only_the_item_parked_on_this_run_is_settled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, env, queue, item_id = self._served_park(tmp_path, monkeypatch)
        # A second awaiting item, parked on another run. Its higher priority
        # sorts it first, so neither "the only awaiting item" nor "the first
        # awaiting item" names the one this decision belongs to.
        other = queue.add("# other\n", title="other", priority=10)
        other = queue.start(queue.lease(other), run_id="factory-other")
        queue.await_approval(other, reason="parked elsewhere", run_id="factory-other")

        rejected = _ks(
            root,
            env,
            "inbox",
            "reject",
            _park_item(root).id,
            "--comment",
            "no",
            "--ui",
            "plain",
            "--no-color",
        )
        out = rejected.stdout + rejected.stderr

        settled = queue.get(item_id)
        assert settled is not None and str(settled.state) == "poison", out
        untouched = queue.get(other.item_id)
        assert untouched is not None and str(untouched.state) == "awaiting_approval", out
        assert untouched.last_run_id == "factory-other", out
