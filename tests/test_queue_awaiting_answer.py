"""#644: a queued spec the architect escalates waits for the owner's answer.

Before #644 `ks serve` read the factory's architect halt (exit 2 with
"Spec issues written to:") as a spec failure: it poisoned the item,
counted it toward the consecutive-poison breaker, and filed a second
inbox row beside the architect's own escalation. Three escalations in a
row paused the queue, and the answer had no route back to the queued
copy of the spec.

Every test here drives a real entry point: `ks serve --once` spawning a
real `ks factory --spec` child, `ks queue answer`, `ks queue retry`,
`ks queue ls` and `ks factory --spec`. The architect is a fake ``claude``
on PATH, the harness of tests/test_serve_architect_spend.py, that saves
each call's stdin to its own file and answers from a numbered script.
No paid CLI runs: ``codex`` is a stub that exits 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from kstrl import events as ev
from kstrl.cli import cli
from kstrl.inbox import Inbox, InboxItem, ItemKind, ItemStatus
from kstrl.intake_github import GitHubIntakeConfig
from kstrl.runid import mint_run_id, run_kind
from kstrl.serve import SPAWNED_RUN_KIND, RunOutcome, ServeConfig, SpendLedger, Verdict, serve_cycle
from kstrl.workqueue import ItemSource, ItemState, Queue, QueueConfig, QueueItem
from tests.helpers.executables import write_executable
from tests.helpers.stack_confirmation import confirm_stack, write_stack
from tests.test_isolation_rung import needs_nono
from tests.test_prompt_record import ONE_COMPONENT
from tests.test_prompt_record import _spec_project as _bare_spec_project
from tests.test_serve_architect_spend import BLOCKER

pytestmark = pytest.mark.usefixtures("no_open_prs")


def _spec_project(tmp_path: Path, *, initialised: bool = False) -> Path:
    """#696: every entry point here checks for a confirmed [stack] before
    it will claim or run anything, even the refusal path that supplies
    its own runner, so every project this file builds needs one."""
    root = _bare_spec_project(tmp_path, initialised=initialised)
    write_stack(root)
    confirm_stack(root)
    return root


#: The line the owner adds to the queued spec. The second architect call
#: must read it.
ANSWER_LINE = "Answer: ship the command-line client first."

SPEC_TEXT = "# Spec\n\nBuild a thing.\n"


def _result_event(payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "type": "result",
            "result": json.dumps(payload),
            "total_cost_usd": 1.5,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )


def _scripted_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[dict[str, object]],
    *,
    foreign_run: tuple[Path, Path] | None = None,
) -> Path:
    """A fake ``claude`` first on PATH; returns the directory of its calls.

    Call N saves its stdin to ``<calls>/N.txt`` and answers ``answers[N-1]``;
    a call past the end answers the last entry. ``foreign_run`` is a
    ``(template, destination)`` pair: the first call copies the template run
    directory to the destination, so a factory-kind run that is not the
    launch's lands inside the launch's window, as the one at
    tests/test_serve_architect_spend.py:277 does.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    scripts = tmp_path / "answers"
    scripts.mkdir(exist_ok=True)
    for number, payload in enumerate(answers, start=1):
        (scripts / f"{number}.jsonl").write_text(_result_event(payload) + "\n", encoding="utf-8")
    (scripts / "last.jsonl").write_text(_result_event(answers[-1]) + "\n", encoding="utf-8")
    calls = tmp_path / "claude-calls"
    calls.mkdir(exist_ok=True)
    plant = ""
    if foreign_run is not None:
        template, destination = foreign_run
        plant = f"if [ ! -d '{destination}' ]; then cp -R '{template}' '{destination}'; fi\n"
    write_executable(
        bindir / "claude",
        "#!/bin/sh\n"
        'case "$*" in *--help*|*--version*) exit 0 ;; esac\n'
        f"n=$(( $(ls '{calls}' | wc -l) + 1 ))\n"
        f"cat > '{calls}/'\"$n\".txt\n"
        f"{plant}"
        f"if [ -f '{scripts}/'\"$n\".jsonl ]; then cat '{scripts}/'\"$n\".jsonl; "
        f"else cat '{scripts}/last.jsonl'; fi\n",
    )
    write_executable(bindir / "codex", "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("KSTRL_AGENT_PROBE", "0")
    monkeypatch.setenv("KSTRL_AGENT_TYPE", "claude-code")
    monkeypatch.setenv("KSTRL_SERVE_CAFFEINATE", "0")
    monkeypatch.setenv("KSTRL_SERVE_FACTORY_TIMEOUT", "120")
    return calls


def _call_stdin(calls: Path, number: int) -> str:
    return (calls / f"{number}.txt").read_text(encoding="utf-8")


def _foreign_factory_template(tmp_path: Path) -> Path:
    """A factory run directory from another process: it names no architect."""
    template = tmp_path / "foreign-template"
    template.mkdir()
    bus = ev.EventBus(ev.JsonlSink(template / "events.jsonl"), run_id="foreign")
    bus.emit(ev.RunStarted(project="other", components=1, pid=os.getpid()))
    bus.close()
    return template


def _ks(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, [*args, "--root", str(root), "--ui", "plain", "--no-color"])


def _queue(root: Path) -> Queue:
    return Queue(root, QueueConfig.load(root))


def _add(root: Path, title: str, *, max_attempts: int | None = None) -> QueueItem:
    return _queue(root).add(SPEC_TEXT, title=title, project_name="demo", max_attempts=max_attempts)


def _item(root: Path, item_id: str) -> QueueItem:
    found = _queue(root).get(item_id)
    assert found is not None, item_id
    return found


def _rows(root: Path) -> list[InboxItem]:
    return Inbox(root).items()


def _open_rows(root: Path) -> list[InboxItem]:
    return [row for row in _rows(root) if row.status is ItemStatus.OPEN]


def _streak(root: Path) -> int:
    return SpendLedger(root).read_state().consecutive_poison


def _journal(root: Path, item_id: str, to_state: str) -> list[dict[str, object]]:
    return [e for e in _queue(root).journal_entries(item_id) if e.get("to") == to_state]


def _awaiting(root: Path, *, max_attempts: int | None = None) -> QueueItem:
    """An item in awaiting_answer, moved there through the queue's own API.

    For the command tests, whose subject is `ks queue answer`, not how the
    item got there (T1 drives that end to end).
    """
    queue = _queue(root)
    item = _add(root, "waits", max_attempts=max_attempts)
    item = queue.start(queue.lease(item, actor="test"), actor="test")
    return queue.await_answer(
        item, reason="the architect escalated", run_id="decompose-x", actor="test"
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestAnEscalationWaitsForTheOwner:
    def test_one_escalation_waits_with_one_row_and_no_poison(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T1. The launch window also holds a factory run from another
        process, which sorts after the decompose run: the item must name
        the launch's architect run, never the last owned run."""
        root = _spec_project(tmp_path)
        foreign = mint_run_id(SPAWNED_RUN_KIND)
        destination = root / ".kstrl" / "runs" / foreign
        _scripted_claude(
            tmp_path,
            monkeypatch,
            [BLOCKER],
            foreign_run=(_foreign_factory_template(tmp_path), destination),
        )
        item = _add(root, "escalates")
        SpendLedger(root).record_terminal(poisoned=True)

        served = _ks(root, "serve", "--once")

        assert served.exit_code == 1, served.output
        assert destination.is_dir(), "the foreign factory run must land in the window"
        waiting = _item(root, item.item_id)
        assert waiting.state is ItemState.AWAITING_ANSWER, served.output
        assert (root / ".kstrl" / "queue" / "awaiting_answer" / item.item_id).is_dir()
        listed = _ks(root, "queue", "ls")
        assert listed.exit_code == 0, listed.output
        assert "awaiting_answer" in listed.output, listed.output

        opened = _open_rows(root)
        assert len(opened) == 1, [(r.kind, r.title) for r in opened]
        (row,) = opened
        assert row.kind is ItemKind.SPEC_ESCALATION
        assert row.evidence.get("queue_item") == item.item_id, row.evidence
        assert f"ks queue answer {item.item_id}" in row.detail, row.detail
        assert not [r for r in _rows(root) if r.dedupe_key.startswith("queue-poison:")]
        assert _streak(root) == 1, "an escalation neither poisons nor resets the streak"

        (architect_run,) = [
            p.name for p in (root / ".kstrl" / "runs").iterdir() if run_kind(p.name) == "decompose"
        ]
        (entry,) = _journal(root, item.item_id, "awaiting_answer")
        detail = entry["detail"]
        assert isinstance(detail, dict)
        assert detail["run_id"] == architect_run, (detail, foreign)
        assert waiting.last_run_id == architect_run

    def test_three_escalations_do_not_pause_the_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T2. Three correct halts in a row are not a systemic failure:
        the fourth item is still claimed."""
        root = _spec_project(tmp_path)
        _scripted_claude(tmp_path, monkeypatch, [BLOCKER, BLOCKER, BLOCKER, BLOCKER])
        items = [_add(root, f"spec-{n}") for n in range(4)]

        for _cycle in range(4):
            result = serve_cycle(root, config=ServeConfig.load(root))
            assert not result.paused, result.skipped
            assert not _queue(root).is_paused(), result.skipped

        states = [_item(root, item.item_id).state for item in items]
        assert states == [ItemState.AWAITING_ANSWER] * 4, states
        assert _streak(root) == 0


class TestTheAnswerReachesTheArchitect:
    @needs_nono
    def test_the_answered_spec_is_what_the_next_cycle_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T3."""
        root = _spec_project(tmp_path)
        calls = _scripted_claude(tmp_path, monkeypatch, [BLOCKER, ONE_COMPONENT])
        item = _add(root, "escalates")
        assert _ks(root, "serve", "--once").exit_code == 1
        answered = tmp_path / "answered.md"
        answered.write_text(SPEC_TEXT + "\n" + ANSWER_LINE + "\n", encoding="utf-8")

        result = _ks(root, "queue", "answer", item.item_id, str(answered))

        assert result.exit_code == 0, result.output
        before, after = _sha(SPEC_TEXT.encode()), _sha(answered.read_bytes())
        assert before in result.output and after in result.output, result.output
        (entry,) = _journal(root, item.item_id, "queued")[-1:]
        detail = entry["detail"]
        assert isinstance(detail, dict)
        assert detail["spec_sha256_after"] == after, detail
        assert detail["unchanged"] is False, detail
        assert _item(root, item.item_id).state is ItemState.QUEUED

        serve_cycle(root, config=ServeConfig.load(root))

        assert ANSWER_LINE in _call_stdin(calls, 2), "the architect never read the answer"
        (row,) = [r for r in _rows(root) if r.kind is ItemKind.SPEC_ESCALATION]
        assert row.status is ItemStatus.RESOLVED, (row.status, row.detail)

    def test_the_unchanged_copy_is_accepted_and_journalled(self, tmp_path: Path) -> None:
        """T5. Identical bytes requeue, so a crash between the write and the
        rename can be finished by running the command again."""
        root = _spec_project(tmp_path)
        item = _awaiting(root)
        same = tmp_path / "same.md"
        same.write_bytes(_queue(root).spec_path(item).read_bytes())

        result = _ks(root, "queue", "answer", item.item_id, str(same))

        assert result.exit_code == 0, result.output
        (entry,) = _journal(root, item.item_id, "queued")[-1:]
        detail = entry["detail"]
        assert isinstance(detail, dict)
        assert detail["unchanged"] is True, detail
        assert "unchanged" in result.output, result.output


class TestRefusalsChangeNothing:
    """T4. Each refusal exits 2, names its reason, and leaves meta.json and
    the spec byte-identical."""

    @staticmethod
    def _snapshot(root: Path, item_id: str) -> tuple[bytes, bytes]:
        queue = _queue(root)
        item = _item(root, item_id)
        return (queue.item_dir(item) / "meta.json").read_bytes(), queue.spec_path(item).read_bytes()

    def _refused(self, root: Path, item_id: str, args: list[str], reason: str) -> None:
        before = self._snapshot(root, item_id)
        result = _ks(root, *args)
        assert result.exit_code == 2, result.output
        assert reason in " ".join(result.output.split()), result.output
        assert self._snapshot(root, item_id) == before

    def _answer_file(self, tmp_path: Path, text: str = SPEC_TEXT + ANSWER_LINE + "\n") -> str:
        path = tmp_path / "answered.md"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_poisoned_item_is_refused(self, tmp_path: Path) -> None:
        root = _spec_project(tmp_path)
        queue = _queue(root)
        item = _add(root, "poisoned")
        queue.poison(item, reason="engineer failed", actor="test")
        self._refused(
            root,
            item.item_id,
            ["queue", "answer", item.item_id, self._answer_file(tmp_path)],
            "no undecided spec_escalation row in the inbox names it",
        )

    def test_a_queued_item_is_refused(self, tmp_path: Path) -> None:
        root = _spec_project(tmp_path)
        item = _add(root, "queued")
        self._refused(
            root,
            item.item_id,
            ["queue", "answer", item.item_id, self._answer_file(tmp_path)],
            "only an item awaiting an answer can be answered",
        )

    def test_an_empty_spec_is_refused(self, tmp_path: Path) -> None:
        root = _spec_project(tmp_path)
        item = _awaiting(root)
        self._refused(
            root,
            item.item_id,
            ["queue", "answer", item.item_id, self._answer_file(tmp_path, " \n")],
            "is empty",
        )

    def test_an_undecodable_spec_is_refused(self, tmp_path: Path) -> None:
        root = _spec_project(tmp_path)
        item = _awaiting(root)
        undecodable = tmp_path / "answered.md"
        undecodable.write_bytes(b"# Spec\n\xff\xfe not utf-8\n")
        self._refused(
            root,
            item.item_id,
            ["queue", "answer", item.item_id, str(undecodable)],
            "Could not read",
        )

    def test_exhausted_attempts_need_reset_attempts(self, tmp_path: Path) -> None:
        root = _spec_project(tmp_path)
        item = _awaiting(root, max_attempts=1)
        answer = self._answer_file(tmp_path)
        self._refused(
            root,
            item.item_id,
            ["queue", "answer", item.item_id, answer],
            "pass --reset-attempts",
        )

        result = _ks(root, "queue", "answer", item.item_id, answer, "--reset-attempts")

        assert result.exit_code == 0, result.output
        requeued = _item(root, item.item_id)
        assert (requeued.state, requeued.attempts) == (ItemState.QUEUED, 0)

    def test_retry_names_answer_for_an_awaiting_item(self, tmp_path: Path) -> None:
        root = _spec_project(tmp_path)
        item = _awaiting(root)
        self._refused(
            root,
            item.item_id,
            ["queue", "retry", item.item_id],
            f"ks queue answer {item.item_id}",
        )


class TestControls:
    """T6. What must not change."""

    def test_exit_two_without_a_marker_still_poisons(self, tmp_path: Path) -> None:
        root = _spec_project(tmp_path)
        item = _add(root, "refused")

        def refused(**_kwargs: object) -> RunOutcome:
            return RunOutcome(returncode=2, output_tail="refused for a reason nobody printed")

        result = serve_cycle(root, config=ServeConfig(caffeinate=False), runner=refused)

        assert result.verdict is Verdict.UNCLASSIFIABLE, result.reason
        assert _item(root, item.item_id).state is ItemState.POISON
        assert _streak(root) == 1

    def test_a_factory_outside_the_queue_keeps_today_s_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _spec_project(tmp_path)
        _scripted_claude(tmp_path, monkeypatch, [BLOCKER])

        result = CliRunner().invoke(
            cli,
            [
                "factory",
                "--spec",
                str(root / "spec.md"),
                "--project-name",
                "demo",
                "--root",
                str(root),
                "--yes",
                "--no-tui",
                "--ui",
                "plain",
                "--no-color",
            ],
        )

        assert result.exit_code == 2, result.output
        (row,) = _open_rows(root)
        assert "queue_item" not in row.evidence, row.evidence
        assert "ks queue answer" not in row.detail, row.detail
        assert "ks inbox approve <id> --comment ANSWER" in row.detail, row.detail


#: A `gh` that keeps the repository's labels and the issue's labels in
#: $GH_STATE. It models gh 2.73.0 as read in `editable_http.go` `UpdateIssue`
#: and `queries_repo.go` `LabelsToIDs`: `issue edit` adds and removes in two
#: separate steps, and a step fails whole with `'<name>' not found` when one
#: of its names is not a label of the repository. `issue view` and `issue
#: edit` must name issue 7 of `o/r` with `--repo`, or they fail as gh does for
#: an issue it cannot resolve. Every call is logged in
#: $GH_LOG and each comment body in $GH_COMMENTS; any other call answers `[]`.
FAKE_GH = r"""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    log.write("gh " + " ".join(args) + "\n")
path = os.environ["GH_STATE"]
with open(path, encoding="utf-8") as f:
    state = json.load(f)
if args[:2] == ["issue", "comment"]:
    with open(os.environ["GH_COMMENTS"], "a", encoding="utf-8") as out:
        out.write(args[args.index("--body") + 1] + "\n")
    sys.exit(0)
if args[:2] in (["issue", "view"], ["issue", "edit"]) and (
    args[2:5] != [state["issue"], "--repo", state["repo"]]
):
    print(f"could not resolve to an Issue with the number of {args[2]}", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["issue", "view"]:
    print(json.dumps({"labels": [{"name": n} for n in state["issue_labels"]]}))
    sys.exit(0)
if args[:2] != ["issue", "edit"]:
    print("[]")
    sys.exit(0)
known = {n.casefold() for n in state["repo_labels"]}
labels = list(state["issue_labels"])
failed = []
for flag in ("--add-label", "--remove-label"):
    names = [args[i + 1] for i, a in enumerate(args) if a == flag]
    missing = [n for n in names if n.casefold() not in known]
    if missing:
        failed.append(f"'{missing[0]}' not found")
        continue
    if flag == "--add-label":
        labels += [n for n in names if n not in labels]
    else:
        labels = [n for n in labels if n.casefold() not in {m.casefold() for m in names}]
state["issue_labels"] = labels
with open(path, "w", encoding="utf-8") as f:
    json.dump(state, f)
if failed:
    print("\n".join(failed), file=sys.stderr)
    sys.exit(1)
"""

#: Every label the adapter writes, which `docs/continuous-intake.md` tells
#: the operator to create.
ALL_LABELS = GitHubIntakeConfig().managed_labels


def _fake_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_labels: tuple[str, ...]
) -> tuple[Path, Path, Path]:
    """Put FAKE_GH first on PATH for an issue labelled `kstrl:queued`.

    Returns the call log, the comment log and the state file. Call it after
    `_scripted_claude`, which puts the directory on PATH.
    """
    write_executable(tmp_path / "fakebin" / "gh", f"#!{sys.executable}\n{FAKE_GH}")
    state = tmp_path / "gh.state.json"
    state.write_text(
        json.dumps(
            {
                "repo": "o/r",
                "issue": "7",
                "repo_labels": list(repo_labels),
                "issue_labels": ["kstrl:queued"],
            }
        ),
        encoding="utf-8",
    )
    gh_log, comments = tmp_path / "gh.log", tmp_path / "gh.comments"
    monkeypatch.setenv("GH_LOG", str(gh_log))
    monkeypatch.setenv("GH_COMMENTS", str(comments))
    monkeypatch.setenv("GH_STATE", str(state))
    return gh_log, comments, state


def _issue_labels(state: Path) -> list[str]:
    labels: list[str] = json.loads(state.read_text(encoding="utf-8"))["issue_labels"]
    return labels


def _edits(gh_log: Path) -> list[tuple[str, list[str]]]:
    """Each `gh issue edit` call as (the label it adds, the labels it removes)."""
    edits = []
    for call in gh_log.read_text(encoding="utf-8").splitlines():
        if not call.startswith("gh issue edit"):
            continue
        words = call.split()
        added = [words[i + 1] for i, w in enumerate(words) if w == "--add-label"]
        removed = [words[i + 1] for i, w in enumerate(words) if w == "--remove-label"]
        assert len(added) == 1, call
        edits.append((added[0], removed))
    return edits


class TestRemoteWriteback:
    def test_a_github_item_is_labelled_awaiting_answer_until_it_runs_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T7, #644 slice 3. The escalation moves the source issue from
        `kstrl:running` to `kstrl:awaiting_answer` and comments with the
        local command and the item's full id. After `ks queue answer`, the
        claim that re-runs the item moves the issue back to `kstrl:running`
        and removes `kstrl:awaiting_answer`."""
        root = _spec_project(tmp_path)
        (root / "kstrl.toml").write_text(
            '[intake_github]\nenabled = true\nrepo = "o/r"\n', encoding="utf-8"
        )
        write_stack(root)
        confirm_stack(root)
        _scripted_claude(tmp_path, monkeypatch, [BLOCKER])
        gh_log, comments, state = _fake_gh(tmp_path, monkeypatch, ALL_LABELS)
        item = _queue(root).add(
            SPEC_TEXT,
            title="remote",
            project_name="demo",
            source=ItemSource.GITHUB,
            source_ref="o/r#7",
        )

        first = serve_cycle(root, config=ServeConfig.load(root))

        assert _item(root, item.item_id).state is ItemState.AWAITING_ANSWER, first.reason
        edits = _edits(gh_log)
        assert [added for added, _ in edits] == ["kstrl:running", "kstrl:awaiting_answer"], edits
        assert "kstrl:running" in edits[1][1], edits
        assert _issue_labels(state) == ["kstrl:awaiting_answer"], edits
        body = comments.read_text(encoding="utf-8")
        assert "**kstrl: awaiting_answer**" in body, body
        assert "the architect escalated a question only the owner can answer" in body, body
        assert f"ks queue answer {item.item_id} <answered spec file>" in body, body

        answered = tmp_path / "answered.md"
        answered.write_text(SPEC_TEXT + "\n" + ANSWER_LINE + "\n", encoding="utf-8")
        assert _ks(root, "queue", "answer", item.item_id, str(answered)).exit_code == 0
        serve_cycle(root, config=ServeConfig.load(root))

        rerun = _edits(gh_log)[2]
        assert rerun[0] == "kstrl:running", rerun
        assert "kstrl:awaiting_answer" in rerun[1], rerun

    def test_a_repo_without_one_kstrl_label_still_moves_the_issue_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#738. The repository has every kstrl label except
        `kstrl:awaiting_approval`, as a repository set up before #465 does.
        The claim and the escalation still leave the issue with exactly one
        kstrl label, because each writeback names only the labels the issue
        carries. Before #738 every edit also named the missing label, gh
        refused each removal, and the issue kept `kstrl:queued` and
        `kstrl:running` beside the new label."""
        root = _spec_project(tmp_path)
        (root / "kstrl.toml").write_text(
            '[intake_github]\nenabled = true\nrepo = "o/r"\n', encoding="utf-8"
        )
        _scripted_claude(tmp_path, monkeypatch, [BLOCKER])
        present = tuple(name for name in ALL_LABELS if name != "kstrl:awaiting_approval")
        _, _, state = _fake_gh(tmp_path, monkeypatch, present)
        item = _queue(root).add(
            SPEC_TEXT,
            title="remote",
            project_name="demo",
            source=ItemSource.GITHUB,
            source_ref="o/r#7",
        )

        first = serve_cycle(root, config=ServeConfig.load(root))

        assert _item(root, item.item_id).state is ItemState.AWAITING_ANSWER, first.reason
        assert _issue_labels(state) == ["kstrl:awaiting_answer"]
