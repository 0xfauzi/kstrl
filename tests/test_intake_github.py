"""R8.6 PR 3: GitHub Issues intake adapter tests.

Every test here stubs `gh`. The live round-trip against a real repo is a
separate, deliberate exercise (recorded in the PR), because a suite that
hit the API would be slow, rate-limited, and would post public comments
on every run.

The properties that matter most are the ones that protect the queue from
the front-end rather than the other way round: a GitHub outage must not
stall intake, a re-seen issue must not re-enqueue, and no label or config
may grant a remote item auto-merge.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.intake_github import (
    MAX_SPEC_CHARS,
    GhResult,
    GitHubIntakeConfig,
    IntakeError,
    ProcessedLedger,
    RemoteIssue,
    apply_state_label,
    issue_number_from_ref,
    parse_issue_list,
    poll_queued,
    post_comment,
    repo_from_ref,
    report_outcome,
    resolve_repo,
    run_gh,
    spec_from_issue,
    sync,
)
from kstrl.workqueue import (
    ItemSource,
    ItemState,
    MergeDisposition,
    Queue,
    QueueConfig,
)

REPO = "0xfauzi/claude-skills"


def _queue(root: Path, **kwargs: object) -> Queue:
    return Queue(root, QueueConfig(**kwargs))  # type: ignore[arg-type]


def _config(**kwargs: object) -> GitHubIntakeConfig:
    base: dict[str, object] = {"enabled": True, "repo": REPO}
    base.update(kwargs)
    return GitHubIntakeConfig(**base)  # type: ignore[arg-type]


def _issue_payload(*issues: dict[str, object]) -> str:
    return json.dumps(list(issues))


def _issue(
    number: int, title: str = "Add a thing", body: str = "Build the thing.",
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/{REPO}/issues/{number}",
        "labels": [{"name": "kstrl:queued"}],
    }


class _GhStub:
    """Records `gh` argv and replays canned results in order."""

    def __init__(self, *results: GhResult) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(
        self, args: list[str], *, timeout: float, cwd: Path | None = None,
    ) -> GhResult:
        self.calls.append(list(args))
        if self.results:
            return self.results.pop(0)
        return GhResult(ok=True, stdout="")

    def argv_for(self, subcommand: str) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == subcommand]


# --------------------------------------------------------------------------
# run_gh: every failure becomes a value
# --------------------------------------------------------------------------


class TestRunGhNeverRaises:
    """The adapter is additive by contract, so nothing may escape."""

    def test_a_missing_binary_is_reported(self) -> None:
        with patch("shutil.which", return_value=None):
            result = run_gh(["issue", "list"], timeout=1.0)
        assert not result.ok
        assert "not installed" in result.error

    def test_a_timeout_is_reported(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "kstrl.intake_github.subprocess.run",
                side_effect=subprocess.TimeoutExpired("gh", 1.0),
            ):
                result = run_gh(["issue", "list"], timeout=1.0)
        assert not result.ok
        assert "timed out" in result.error

    def test_an_os_error_is_reported(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "kstrl.intake_github.subprocess.run",
                side_effect=OSError("exec format error"),
            ):
                result = run_gh(["issue", "list"], timeout=1.0)
        assert not result.ok
        assert "could not run" in result.error

    def test_a_nonzero_exit_is_reported_with_stderr(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="HTTP 403 rate limited",
        )
        with patch("shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "kstrl.intake_github.subprocess.run", return_value=completed,
            ):
                result = run_gh(["issue", "list"], timeout=1.0)
        assert not result.ok
        assert "rate limited" in result.error

    def test_success_carries_stdout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout="[]", stderr="",
        )
        with patch("shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "kstrl.intake_github.subprocess.run", return_value=completed,
            ):
                result = run_gh(["issue", "list"], timeout=1.0)
        assert result.ok
        assert result.stdout == "[]"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class TestParseIssueList:
    def test_parses_a_normal_payload(self) -> None:
        issues = parse_issue_list(_issue_payload(_issue(7), _issue(3)))
        assert [i.number for i in issues] == [3, 7], "oldest first"

    def test_malformed_json_yields_nothing(self) -> None:
        assert parse_issue_list("{not json") == []

    def test_a_non_list_payload_yields_nothing(self) -> None:
        assert parse_issue_list('{"number": 1}') == []

    def test_one_bad_entry_does_not_discard_the_rest(self) -> None:
        """A single unparseable issue must not stall the whole queue."""
        payload = json.dumps([{"title": "no number"}, _issue(5)])
        issues = parse_issue_list(payload)
        assert [i.number for i in issues] == [5]

    def test_labels_are_extracted(self) -> None:
        issues = parse_issue_list(_issue_payload(_issue(1)))
        assert issues[0].labels == ("kstrl:queued",)

    def test_a_missing_title_falls_back(self) -> None:
        payload = json.dumps([{"number": 9}])
        assert parse_issue_list(payload)[0].title == "issue #9"

    def test_a_boolean_number_is_rejected(self) -> None:
        payload = json.dumps([{"number": True, "title": "x"}])
        assert parse_issue_list(payload) == []


# --------------------------------------------------------------------------
# Spec construction
# --------------------------------------------------------------------------


class TestSpecFromIssue:
    def test_carries_provenance(self) -> None:
        """A spec that cannot be traced back to a request is a liability."""
        spec = spec_from_issue(RemoteIssue(12, "Add X", "Body text", "u"), REPO)
        assert "# Add X" in spec
        assert f"{REPO}#12" in spec
        assert "Body text" in spec

    def test_truncates_a_pathological_body_and_says_so(self) -> None:
        spec = spec_from_issue(
            RemoteIssue(1, "T", "x" * (MAX_SPEC_CHARS * 2), "u"), REPO,
        )
        assert len(spec) <= MAX_SPEC_CHARS + 200
        assert "truncated by kstrl" in spec, "truncation must never be silent"

    def test_a_short_body_is_untouched(self) -> None:
        spec = spec_from_issue(RemoteIssue(1, "T", "short", "u"), REPO)
        assert "truncated" not in spec


# --------------------------------------------------------------------------
# The processed-ids ledger
# --------------------------------------------------------------------------


class TestProcessedLedger:
    def test_round_trips(self, tmp_path: Path) -> None:
        ledger = ProcessedLedger(tmp_path).load()
        ledger.record(f"{REPO}#1", item_id="q-1", when="2026-07-30T00:00:00Z")
        assert ProcessedLedger(tmp_path).load().contains(f"{REPO}#1")

    def test_an_absent_ledger_is_empty(self, tmp_path: Path) -> None:
        assert not ProcessedLedger(tmp_path).load().contains(f"{REPO}#1")

    def test_a_corrupt_ledger_reads_as_empty(self, tmp_path: Path) -> None:
        """Fails OPEN, deliberately: stalling intake is worse than a re-poll.

        The blast radius is bounded by find_by_source_ref, the per-sync
        cap, and the daily budget - unlike the SPEND ledger, where failing
        open disabled the only queue-wide cap.
        """
        ledger = ProcessedLedger(tmp_path).load()
        ledger.record(f"{REPO}#1", item_id="q-1", when="t")
        ledger.path.write_text("{corrupt")
        assert not ProcessedLedger(tmp_path).load().contains(f"{REPO}#1")

    def test_forget_allows_readmission(self, tmp_path: Path) -> None:
        ledger = ProcessedLedger(tmp_path).load()
        ledger.record(f"{REPO}#1", item_id="q-1", when="t")
        assert ledger.forget(f"{REPO}#1")
        assert not ledger.contains(f"{REPO}#1")

    def test_forget_of_an_unknown_ref_is_false(self, tmp_path: Path) -> None:
        assert not ProcessedLedger(tmp_path).load().forget("nope#1")


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------


class TestSync:
    def test_enqueues_a_labelled_issue(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(GhResult(ok=True, stdout=_issue_payload(_issue(4))))
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert result.enqueued == (f"{REPO}#4",)
        items = queue.items()
        assert len(items) == 1
        assert items[0].source is ItemSource.GITHUB
        assert items[0].source_ref == f"{REPO}#4"
        assert items[0].target_repo == REPO

    def test_a_remote_item_can_never_auto_merge(self, tmp_path: Path) -> None:
        """No label and no config setting may delete the human merge gate."""
        queue = _queue(tmp_path)
        labelled = _issue(4)
        labelled["labels"] = [
            {"name": "kstrl:queued"}, {"name": "auto-merge"},
            {"name": "kstrl:auto-merge"},
        ]
        stub = _GhStub(GhResult(ok=True, stdout=_issue_payload(labelled)))
        with patch("kstrl.intake_github.run_gh", stub):
            sync(queue, _config(), tmp_path)
        assert queue.items()[0].merge_disposition is MergeDisposition.STOP_AT_PR

    def test_a_disabled_adapter_does_nothing(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(enabled=False), tmp_path)
        assert result.enqueued == ()
        assert stub.calls == [], "a disabled adapter must not call gh at all"
        assert queue.items() == []

    def test_a_poll_failure_leaves_the_queue_untouched(
        self, tmp_path: Path,
    ) -> None:
        """A GitHub outage must never stall or corrupt the local queue."""
        queue = _queue(tmp_path)
        queue.add("# local\n", title="local work")
        stub = _GhStub(GhResult(ok=False, error="HTTP 503"))
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert not result.ok
        assert "503" in result.errors[0]
        assert len(queue.items()) == 1, "the local item is undisturbed"

    def test_an_already_processed_issue_is_not_re_enqueued(
        self, tmp_path: Path,
    ) -> None:
        """The half find_by_source_ref cannot cover: the item is GONE."""
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(4))
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(GhResult(ok=True, stdout=payload)),
        ):
            sync(queue, _config(), tmp_path)
        # The item completes and leaves the queue entirely.
        item = queue.items()[0]
        queue.remove(queue.finish_ok(queue.start(queue.lease(item))))
        assert queue.items() == []

        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(GhResult(ok=True, stdout=payload)),
        ):
            second = sync(queue, _config(), tmp_path)
        assert second.enqueued == ()
        assert second.skipped[f"{REPO}#4"] == "already processed"
        assert queue.items() == []

    def test_an_issue_already_in_the_queue_is_skipped(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(4))
        for _ in range(2):
            with patch(
                "kstrl.intake_github.run_gh",
                _GhStub(GhResult(ok=True, stdout=payload)),
            ):
                result = sync(queue, _config(), tmp_path)
        assert len(queue.items()) == 1
        assert f"{REPO}#4" in result.skipped

    def test_a_lost_ledger_does_not_duplicate_a_queued_item(
        self, tmp_path: Path,
    ) -> None:
        """The claim the ledger's fail-open rests on.

        ProcessedLedger reads a corrupt file as EMPTY, and the stated
        justification is that find_by_source_ref still catches an item
        that is in the queue. That is the ONLY scenario where the
        in-queue check does work the ledger cannot, so it is the only
        scenario that can pin it: with both guards intact and the ledger
        gone, a re-sync must still not duplicate.
        """
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(4))
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(GhResult(ok=True, stdout=payload)),
        ):
            sync(queue, _config(), tmp_path)
        assert len(queue.items()) == 1

        # Lose the ledger entirely; the item is still queued.
        ProcessedLedger(tmp_path).path.unlink()

        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(GhResult(ok=True, stdout=payload)),
        ):
            second = sync(queue, _config(), tmp_path)
        assert second.enqueued == ()
        assert second.skipped[f"{REPO}#4"] == "already in the queue"
        assert len(queue.items()) == 1, "a lost ledger must not duplicate work"

    def test_an_empty_body_is_skipped_without_spending(
        self, tmp_path: Path,
    ) -> None:
        """Paying an architect call to learn the issue says nothing is waste."""
        queue = _queue(tmp_path)
        stub = _GhStub(
            GhResult(ok=True, stdout=_issue_payload(_issue(4, body="   "))),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert result.enqueued == ()
        assert "empty" in result.skipped[f"{REPO}#4"]
        assert queue.items() == []

    def test_the_per_sync_cap_bounds_intake(self, tmp_path: Path) -> None:
        """A label applied to fifty issues must not queue fifty runs."""
        queue = _queue(tmp_path)
        payload = _issue_payload(*[_issue(n) for n in range(1, 9)])
        stub = _GhStub(GhResult(ok=True, stdout=payload))
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(max_items_per_sync=3), tmp_path)
        assert len(result.enqueued) == 3
        assert len(queue.items()) == 3
        assert any("cap of 3" in r for r in result.skipped.values())

    def test_oldest_issues_are_admitted_first(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(9), _issue(2), _issue(5))
        stub = _GhStub(GhResult(ok=True, stdout=payload))
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(max_items_per_sync=2), tmp_path)
        assert result.enqueued == (f"{REPO}#2", f"{REPO}#5")

    def test_the_state_label_is_swapped_on_admission(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(
            GhResult(ok=True, stdout=_issue_payload(_issue(4))),
            GhResult(ok=True),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            sync(queue, _config(), tmp_path)
        edits = stub.argv_for("issue")
        edit = [c for c in edits if "edit" in c]
        assert edit, "the issue must be relabelled on admission"
        assert "--add-label" in edit[0]
        assert "kstrl:running" in edit[0]
        assert "kstrl:queued" in edit[0], "the trigger label is removed"

    def test_a_label_writeback_failure_keeps_the_item_queued(
        self, tmp_path: Path,
    ) -> None:
        """The item IS queued; only the remote view is stale."""
        queue = _queue(tmp_path)
        stub = _GhStub(
            GhResult(ok=True, stdout=_issue_payload(_issue(4))),
            GhResult(ok=False, error="HTTP 403"),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert result.enqueued == (f"{REPO}#4",)
        assert len(queue.items()) == 1
        assert any("label writeback failed" in e for e in result.errors)

    def test_dry_run_sends_no_writes(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(GhResult(ok=True, stdout=_issue_payload(_issue(4))))
        with patch("kstrl.intake_github.run_gh", stub):
            sync(queue, _config(dry_run=True), tmp_path)
        assert stub.argv_for("issue") == [
            c for c in stub.calls if c[:2] == ["issue", "list"]
        ], "dry_run must poll but never edit"

    def test_the_polled_count_is_reported(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(1), _issue(2))
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(GhResult(ok=True, stdout=payload)),
        ):
            result = sync(queue, _config(max_items_per_sync=1), tmp_path)
        assert result.polled == 2
        assert len(result.enqueued) == 1


# --------------------------------------------------------------------------
# Repo resolution and refs
# --------------------------------------------------------------------------


class TestRepoResolution:
    def test_an_explicit_repo_needs_no_gh_call(self, tmp_path: Path) -> None:
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            repo, error = resolve_repo(_config(), tmp_path)
        assert (repo, error) == (REPO, "")
        assert stub.calls == []

    def test_resolution_falls_back_to_the_checkout(self, tmp_path: Path) -> None:
        stub = _GhStub(
            GhResult(ok=True, stdout=json.dumps({"nameWithOwner": REPO})),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            repo, error = resolve_repo(_config(repo=""), tmp_path)
        assert (repo, error) == (REPO, "")

    def test_a_resolution_failure_is_reported_not_raised(
        self, tmp_path: Path,
    ) -> None:
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(GhResult(ok=False, error="not a repo")),
        ):
            repo, error = resolve_repo(_config(repo=""), tmp_path)
        assert repo == ""
        assert "could not resolve" in error

    def test_unparseable_resolution_output_is_reported(
        self, tmp_path: Path,
    ) -> None:
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(GhResult(ok=True, stdout="{bad")),
        ):
            _repo, error = resolve_repo(_config(repo=""), tmp_path)
        assert "could not parse" in error

    @pytest.mark.parametrize(
        ("ref", "number", "repo"),
        [
            (f"{REPO}#12", 12, REPO),
            ("owner/name#1", 1, "owner/name"),
            ("no-hash", 0, ""),
            ("owner/name#abc", 0, "owner/name"),
        ],
    )
    def test_ref_parsing(self, ref: str, number: int, repo: str) -> None:
        assert issue_number_from_ref(ref) == number
        assert repo_from_ref(ref) == repo


# --------------------------------------------------------------------------
# Writeback
# --------------------------------------------------------------------------


class TestWriteback:
    def _github_item(self, tmp_path: Path) -> object:
        queue = _queue(tmp_path)
        return queue.add(
            "# spec\n", title="t", source=ItemSource.GITHUB,
            source_ref=f"{REPO}#4", target_repo=REPO,
        )

    def test_reports_a_poison_with_a_recovery_hint(
        self, tmp_path: Path,
    ) -> None:
        item = self._github_item(tmp_path)
        stub = _GhStub(GhResult(ok=True), GhResult(ok=True))
        with patch("kstrl.intake_github.run_gh", stub):
            error = report_outcome(
                item, state="poison", detail="tests failed",  # type: ignore[arg-type]
                config=_config(), root_dir=tmp_path,
            )
        assert error == ""
        comment = [c for c in stub.calls if "comment" in c]
        assert comment
        body = comment[0][comment[0].index("--body") + 1]
        assert "poison" in body
        assert "tests failed" in body
        assert "reset-attempts" in body, "say what the human can do"

    def test_a_done_comment_states_the_merge_gate(
        self, tmp_path: Path,
    ) -> None:
        item = self._github_item(tmp_path)
        stub = _GhStub(GhResult(ok=True), GhResult(ok=True))
        with patch("kstrl.intake_github.run_gh", stub):
            report_outcome(
                item, state="done", detail="completed",  # type: ignore[arg-type]
                config=_config(), root_dir=tmp_path,
            )
        comment = [c for c in stub.calls if "comment" in c][0]
        body = comment[comment.index("--body") + 1]
        assert "stop at the PR" in body

    def test_a_local_item_is_never_reported(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.add("# spec\n", title="local")
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            error = report_outcome(
                item, state="done", detail="", config=_config(),
                root_dir=tmp_path,
            )
        assert error == ""
        assert stub.calls == []

    def test_a_disabled_adapter_reports_nothing(self, tmp_path: Path) -> None:
        item = self._github_item(tmp_path)
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            report_outcome(
                item, state="done", detail="",  # type: ignore[arg-type]
                config=_config(enabled=False), root_dir=tmp_path,
            )
        assert stub.calls == []

    def test_a_writeback_failure_is_returned_not_raised(
        self, tmp_path: Path,
    ) -> None:
        item = self._github_item(tmp_path)
        stub = _GhStub(
            GhResult(ok=False, error="HTTP 403"),
            GhResult(ok=False, error="HTTP 500"),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            error = report_outcome(
                item, state="poison", detail="x",  # type: ignore[arg-type]
                config=_config(), root_dir=tmp_path,
            )
        assert "403" in error and "500" in error

    def test_an_unmappable_ref_is_reported(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.add(
            "# spec\n", title="t", source=ItemSource.GITHUB,
            source_ref="garbage",
        )
        error = report_outcome(
            item, state="done", detail="", config=_config(repo=""),
            root_dir=tmp_path,
        )
        assert "cannot map" in error

    def test_the_state_label_replaces_every_managed_label(
        self, tmp_path: Path,
    ) -> None:
        """An issue must never carry two contradictory kstrl states."""
        stub = _GhStub(GhResult(ok=True))
        with patch("kstrl.intake_github.run_gh", stub):
            apply_state_label(_config(), REPO, 4, "done", tmp_path)
        argv = stub.calls[0]
        assert argv[argv.index("--add-label") + 1] == "kstrl:done"
        removed = [
            argv[i + 1] for i, a in enumerate(argv) if a == "--remove-label"
        ]
        assert set(removed) == {
            "kstrl:queued", "kstrl:running", "kstrl:failed", "kstrl:poison",
        }

    def test_comments_can_be_switched_off(self, tmp_path: Path) -> None:
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            error = post_comment(
                _config(comment_on_result=False), REPO, 4, "body", tmp_path,
            )
        assert error == ""
        assert stub.calls == []


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class TestConfig:
    def test_off_by_default(self) -> None:
        config = GitHubIntakeConfig()
        assert not config.enabled
        assert config.queued_label == "kstrl:queued"
        assert config.max_items_per_sync == 5

    @pytest.mark.parametrize("kwargs", [
        {"queued_label": "  "},
        {"max_items_per_sync": 0},
        {"timeout_seconds": 0},
        {"repo": "not-a-repo"},
        {"repo": "a/b/c"},
    ])
    def test_invalid_values_are_rejected(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(IntakeError):
            GitHubIntakeConfig(**kwargs)  # type: ignore[arg-type]

    def test_managed_labels_cover_every_state(self) -> None:
        labels = GitHubIntakeConfig().managed_labels
        assert labels == (
            "kstrl:queued", "kstrl:running", "kstrl:done",
            "kstrl:failed", "kstrl:poison",
        )

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_ENABLED", "1")
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_REPO", REPO)
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_MAX_ITEMS", "2")
        config = GitHubIntakeConfig.from_env()
        assert config.enabled
        assert config.repo == REPO
        assert config.max_items_per_sync == 2

    def test_load_reads_the_toml_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[intake_github]\nenabled = true\nrepo = "
            f'"{REPO}"\nmax_items_per_sync = 7\n'
        )
        config = GitHubIntakeConfig.load(tmp_path)
        assert config.enabled
        assert config.repo == REPO
        assert config.max_items_per_sync == 7

    def test_env_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[intake_github]\nmax_items_per_sync = 7\n"
        )
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_MAX_ITEMS", "1")
        assert GitHubIntakeConfig.load(tmp_path).max_items_per_sync == 1

    def test_defaults_without_a_config_file(self, tmp_path: Path) -> None:
        assert not GitHubIntakeConfig.load(tmp_path).enabled


# --------------------------------------------------------------------------
# Polling argv
# --------------------------------------------------------------------------


class TestPollArgv:
    def test_polls_open_issues_with_the_trigger_label(
        self, tmp_path: Path,
    ) -> None:
        stub = _GhStub(GhResult(ok=True, stdout="[]"))
        with patch("kstrl.intake_github.run_gh", stub):
            poll_queued(_config(), REPO, tmp_path)
        argv = stub.calls[0]
        assert argv[:2] == ["issue", "list"]
        assert argv[argv.index("--label") + 1] == "kstrl:queued"
        assert argv[argv.index("--state") + 1] == "open"
        assert argv[argv.index("--repo") + 1] == REPO

    def test_a_custom_label_is_honored(self, tmp_path: Path) -> None:
        stub = _GhStub(GhResult(ok=True, stdout="[]"))
        with patch("kstrl.intake_github.run_gh", stub):
            poll_queued(_config(queued_label="factory:go"), REPO, tmp_path)
        argv = stub.calls[0]
        assert argv[argv.index("--label") + 1] == "factory:go"

    def test_a_poll_error_is_returned(self, tmp_path: Path) -> None:
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(GhResult(ok=False, error="boom")),
        ):
            issues, error = poll_queued(_config(), REPO, tmp_path)
        assert issues == []
        assert error == "boom"


# --------------------------------------------------------------------------
# serve integration
# --------------------------------------------------------------------------


class TestServeWriteback:
    """serve must report outcomes without letting the front-end break it."""

    def test_a_writeback_exception_cannot_break_the_cycle(
        self, tmp_path: Path,
    ) -> None:
        from kstrl.serve import RunOutcome, RunSpend, serve_cycle

        queue = _queue(tmp_path)
        queue.add(
            "# spec\n", title="remote", source=ItemSource.GITHUB,
            source_ref=f"{REPO}#4", target_repo=REPO,
        )

        def runner(**kwargs: object) -> RunOutcome:
            run_dir = tmp_path / ".kstrl" / "runs" / "factory-x"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").touch()
            return RunOutcome(returncode=0)

        with patch(
            "kstrl.serve.read_run_spend", lambda root, run_id: RunSpend(),
        ):
            with patch(
                "kstrl.intake_github.GitHubIntakeConfig.load",
                side_effect=RuntimeError("adapter exploded"),
            ):
                result = serve_cycle(tmp_path, runner=runner)  # type: ignore[arg-type]

        assert result.verdict is not None
        assert queue.items()[0].state is ItemState.DONE, (
            "a broken front-end must not change the local outcome"
        )
