"""R10.10 (#231): /memory and /iterate comments on open kstrl PRs.

End to end on purpose. What this feature IS is a daemon cycle that reads
GitHub and writes the operator's checkout, so every case but the last
drives the real `serve_cycle` against a stubbed `run_gh` transport and
asserts on bytes on disk, the queue, the ledger, or the argv the stub
recorded. `no_open_prs` is deliberately NOT used: it stubs out the very
function steering reads the marked PRs from.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.init_cmd import DEFAULT_MEMORY
from kstrl.intake_github import GUIDANCE_HEADING, GhResult, ProcessedLedger
from kstrl.pr import PR_FOOTER_MARKER
from kstrl.serve import _NullObserver, serve_cycle
from kstrl.workqueue import ItemSource, MergeDisposition, Queue, QueueConfig
from tests.test_init_cmd import section_of
from tests.test_serve_seam import _recording_runner

REPO = "0xfauzi/claude-skills"
OWNER = "0xfauzi"
BOT = "github-actions[bot]"


def _pr_url(number: int, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def _comment(
    comment_id: int,
    body: str,
    *,
    login: str = OWNER,
    association: str = "OWNER",
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": login},
        "author_association": association,
    }


class _SteerGh:
    """Routes canned results by `gh` subcommand and records every argv.

    Same routing shape as `tests/test_intake_github.py::_GhStub`, which
    cannot be reused as it stands: it answers `issue list` and the
    authorization GraphQL, and knows nothing about `pr list`, the
    issue-comments REST endpoint, or `pr comment`.
    """

    def __init__(
        self,
        *,
        prs: list[dict[str, object]] | None = None,
        comments: dict[int, list[dict[str, Any]]] | None = None,
        comments_raw: str | None = None,
        repo: str = REPO,
        comment_result: GhResult | None = None,
    ) -> None:
        self.prs = prs if prs is not None else []
        self.comments = comments or {}
        # `comments_raw`, when set, is returned for the comments endpoint
        # VERBATIM instead of `json.dumps(self.comments[...])`. Case 20
        # needs a payload `json.loads` cannot read and a payload whose
        # rows are not comment records, and neither can be expressed as a
        # `list[dict[str, Any]]`.
        self.comments_raw = comments_raw
        self.repo = repo
        self.comment_result = comment_result or GhResult(ok=True)
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        *,
        timeout: float,
        cwd: Path | None = None,
    ) -> GhResult:
        self.calls.append(list(args))
        head = args[:2]
        if head == ["repo", "view"]:
            return GhResult(ok=True, stdout=json.dumps({"nameWithOwner": self.repo}))
        if head == ["pr", "list"]:
            return GhResult(ok=True, stdout=json.dumps(self.prs))
        if head == ["issue", "list"]:
            return GhResult(ok=True, stdout="[]")
        if head[:1] == ["api"]:
            if self.comments_raw is not None:
                return GhResult(ok=True, stdout=self.comments_raw)
            number = int(args[1].split("/issues/")[1].split("/")[0])
            return GhResult(ok=True, stdout=json.dumps(self.comments.get(number, [])))
        if head == ["pr", "comment"]:
            return self.comment_result
        return GhResult(ok=True, stdout="")

    def argv_for(self, *head: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(head)] == list(head)]

    def posted(self) -> list[str]:
        """Every comment body this stub was asked to post."""
        return [c[c.index("--body") + 1] for c in self.argv_for("pr", "comment")]


def _marked_pr(number: int) -> dict[str, object]:
    return {"number": number, "body": f"Body\n\n---\n{PR_FOOTER_MARKER}"}


def _unmarked_pr(number: int) -> dict[str, object]:
    return {"number": number, "body": "A hand-written PR body"}


def _setup(
    root: Path,
    *,
    steer: str = "true",
    extra: str = "",
    memory: str = DEFAULT_MEMORY,
) -> Path:
    """A root with [intake_github] and a scaffolded memory file."""
    (root / "kstrl.toml").write_text(
        f'[intake_github]\nenabled = false\nsteer_enabled = {steer}\nrepo = "{REPO}"\n' + extra,
        encoding="utf-8",
    )
    path = root / "scripts" / "kstrl" / "memory.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(memory, encoding="utf-8")
    return path


def _cycle(root: Path, gh: _SteerGh) -> _NullObserver:
    """One real serve cycle against a stubbed gh. Returns the observer."""
    obs = _NullObserver()
    with patch("kstrl.intake_github.run_gh", gh):
        serve_cycle(root, runner=_recording_runner([]), observer=obs)
    return obs


def _ledger_key(number: int, comment_id: int, repo: str = REPO) -> str:
    return f"pr-comment:{repo}#{number}:{comment_id}"


def _finished_item_recording(root: Path, url: str) -> Queue:
    """A queue holding one DONE item whose `pr_urls` carries `url`.

    `Queue.finish_ok` is a transition to DONE and `_LEGAL_TRANSITIONS`
    (`kstrl/workqueue.py:170`) allows DONE only from RUNNING, so
    `finish_ok` on a freshly added item raises
    `QueueError: illegal queue transition queued -> done`. The
    lease/start/finish chain is the idiom the rest of the suite uses
    (`tests/test_workqueue.py:199`, `tests/test_queue_cli.py:182`).
    """
    queue = Queue(root, QueueConfig())
    item = queue.add(
        "Build the widget.",
        title="widget",
        priority=3,
        source=ItemSource.GITHUB,
        source_ref=f"{REPO}#5",
    )
    queue.finish_ok(queue.start(queue.lease(item)), pr_urls=(url,))
    return queue


def _memory_path(root: Path) -> Path:
    return root / "scripts" / "kstrl" / "memory.md"


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_a_memory_comment_lands_under_guidance_and_is_recorded(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory never touch migrations")]},
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    prefix = "- never touch migrations (from PR #7 by @0xfauzi, "
    assert prefix in body
    line = next(ln for ln in body.splitlines() if ln.startswith(prefix))
    assert section_of(body, line) == "## Guidance"
    assert line.endswith(")")
    date_part = line[len(prefix) : -1]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part)
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert gh.posted() == [
        'Recorded to scripts/kstrl/memory.md: "never touch migrations". Commit it to keep it.'
    ]


def test_a_second_cycle_changes_nothing(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory never touch migrations")]},
    )
    _cycle(tmp_path, gh)
    body1 = _memory_path(tmp_path).read_text(encoding="utf-8")
    _cycle(tmp_path, gh)
    body2 = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body2 == body1
    assert len(gh.posted()) == 1


def test_a_section_after_guidance_does_not_take_the_append(tmp_path: Path) -> None:
    memory = DEFAULT_MEMORY + "\n## Notes\n\n- my own scratch\n"
    _setup(tmp_path, memory=memory)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory never touch migrations")]},
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    prefix = "- never touch migrations (from PR #7 by @0xfauzi, "
    line = next(ln for ln in body.splitlines() if ln.startswith(prefix))
    assert section_of(body, line) == "## Guidance"
    assert "- my own scratch" in body
    assert section_of(body, "- my own scratch") == "## Notes"


def test_a_memory_file_without_the_heading_gains_one(tmp_path: Path) -> None:
    memory = "# Memory\n\n## Notes\n\n- scratch\n"
    _setup(tmp_path, memory=memory)
    gh = _SteerGh(prs=[_marked_pr(7)], comments={7: [_comment(111, "/memory x")]})
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert GUIDANCE_HEADING in body
    prefix = "- x (from PR #7 by @0xfauzi, "
    line = next(ln for ln in body.splitlines() if ln.startswith(prefix))
    assert section_of(body, line) == "## Guidance"
    assert "- scratch" in body
    assert section_of(body, "- scratch") == "## Notes"


def test_an_unauthorised_author_is_skipped_and_named(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory x", login=BOT, association="NONE")]},
    )
    obs = _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.argv_for("pr", "comment") == []
    assert not ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert any(BOT in line and "NONE" in line for line in obs.lines)


def test_an_allowlisted_login_overrides_the_association(tmp_path: Path) -> None:
    _setup(tmp_path, extra='allowed_actors = ["alice"]\n')
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={
            7: [
                _comment(111, "/memory from bob", login="bob", association="OWNER"),
                _comment(112, "/memory from alice", login="alice", association="NONE"),
            ]
        },
    )
    obs = _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert "from alice" in body
    assert "from bob" not in body
    assert any("bob" in line for line in obs.lines)


# ONE COMMENT, NOT TWO. `/iterate` builds one acknowledgement body out of
# the lines it has to say and posts it once, so `gh.posted()` has ONE
# entry whose body may contain a newline. Cases below assert on that one
# entry, with `endswith` / `startswith` / `in`, never on a second element.


def test_iterate_requeues_the_item_that_recorded_this_pr(tmp_path: Path) -> None:
    _setup(tmp_path)
    queue = _finished_item_recording(tmp_path, _pr_url(7))
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/iterate fix the off-by-one")]},
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert "- fix the off-by-one (from PR #7 by @0xfauzi, " in body
    new_items = [item for item in queue.items() if item.source_ref == f"{REPO}#5#iterate-111"]
    assert len(new_items) == 1
    new = new_items[0]
    assert new.priority == 3
    assert new.merge_disposition is MergeDisposition.STOP_AT_PR
    assert new.source is ItemSource.LOCAL
    assert queue.read_spec(new) == "Build the widget."
    assert len(gh.posted()) == 1
    posted = gh.posted()[0]
    assert posted.startswith('Recorded to scripts/kstrl/memory.md: "fix the off-by-one".')
    assert posted.endswith(
        f"Queued a re-run as {new.item_id}; it starts once this PR is merged "
        "or closed (open-PR bound)."
    )


def test_iterate_does_not_match_an_item_by_pr_number_alone(tmp_path: Path) -> None:
    _setup(tmp_path)
    queue = _finished_item_recording(tmp_path, _pr_url(7, repo="someone/else"))
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/iterate fix the off-by-one")]},
    )
    _cycle(tmp_path, gh)
    assert len(queue.items()) == 1
    assert len(gh.posted()) == 1
    assert gh.posted()[0].endswith("Cannot iterate: no queue item recorded this PR")


def test_iterate_without_a_recorded_item_says_so_and_enqueues_nothing(tmp_path: Path) -> None:
    _setup(tmp_path)
    queue = Queue(tmp_path, QueueConfig())
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/iterate fix it")]},
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert "- fix it (from PR #7 by @0xfauzi, " in body
    assert queue.items() == []
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert len(gh.posted()) == 1
    assert gh.posted()[0].endswith("Cannot iterate: no queue item recorded this PR")


def test_a_bare_iterate_skips_the_memory_step(tmp_path: Path) -> None:
    _setup(tmp_path)
    queue = _finished_item_recording(tmp_path, _pr_url(7))
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/iterate")]},
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert any(item.source_ref == f"{REPO}#5#iterate-111" for item in queue.items())


def test_an_overlong_text_is_refused_without_writing(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory " + "x" * 501)]},
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    posted = gh.posted()
    assert posted[0].startswith("Not recorded: ")
    assert "501" in posted[0]


def test_a_text_with_a_heading_line_is_refused_without_writing(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory rule one\n# Heading\nrule two")]},
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert "#" in gh.posted()[0]


def test_a_bare_memory_is_refused(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(prs=[_marked_pr(7)], comments={7: [_comment(111, "/memory")]})
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.posted()[0].startswith("Not recorded: ")


def test_dry_run_writes_nothing_and_posts_nothing(tmp_path: Path) -> None:
    _setup(tmp_path, extra="dry_run = true\n")
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory never touch migrations")]},
    )
    obs = _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.argv_for("pr", "comment") == []
    assert not ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert any("dry run" in line and "111" in line for line in obs.lines)


def test_steering_off_makes_no_comment_call(tmp_path: Path) -> None:
    _setup(tmp_path, steer="false")
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory never touch migrations")]},
    )
    _cycle(tmp_path, gh)
    assert gh.argv_for("api") == []
    assert gh.argv_for("repo", "view") == []
    assert [c[:2] for c in gh.calls] == [["pr", "list"]]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_a_failed_memory_write_is_retried_next_cycle(tmp_path: Path) -> None:
    memory_path = _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory never touch migrations")]},
    )
    os.chmod(memory_path.parent, 0o500)
    try:
        _cycle(tmp_path, gh)
        body = memory_path.read_text(encoding="utf-8")
        assert body == DEFAULT_MEMORY
        assert not ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    finally:
        os.chmod(memory_path.parent, 0o700)
    _cycle(tmp_path, gh)
    body = memory_path.read_text(encoding="utf-8")
    assert "- never touch migrations (from PR #7 by @0xfauzi, " in body
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))


def test_the_cap_holds_the_rest_for_the_next_cycle(tmp_path: Path) -> None:
    _setup(tmp_path, extra="max_items_per_sync = 2\n")
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={
            7: [
                _comment(111, "/memory one"),
                _comment(112, "/memory two"),
                _comment(113, "/memory three"),
            ]
        },
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert "- one (" in body
    assert "- two (" in body
    assert "- three (" not in body
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert "- three (" in body
    i1 = body.index("- one (")
    i2 = body.index("- two (")
    i3 = body.index("- three (")
    assert i1 < i2 < i3


def test_only_marked_prs_are_scanned(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7), _unmarked_pr(8)],
        comments={
            7: [_comment(111, "/memory x")],
            8: [_comment(222, "/memory y")],
        },
    )
    _cycle(tmp_path, gh)
    assert [c[1] for c in gh.argv_for("api")] == [f"repos/{REPO}/issues/7/comments?per_page=100"]
    assert len(gh.argv_for("pr", "list")) == 2


def test_the_guidance_heading_is_the_one_the_scaffold_ships() -> None:
    assert DEFAULT_MEMORY.rstrip("\n").endswith(GUIDANCE_HEADING)


@pytest.mark.parametrize(
    "comments_raw",
    ["{not json", json.dumps([{"id": "111", "body": "/memory x"}])],
    ids=["unparseable", "bad_row_shape"],
)
def test_an_unreadable_comments_payload_is_an_error_not_an_empty_list(
    tmp_path: Path,
    comments_raw: str,
) -> None:
    _setup(tmp_path)
    gh = _SteerGh(prs=[_marked_pr(7)], comments_raw=comments_raw)
    obs = _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.argv_for("pr", "comment") == []
    assert ProcessedLedger(tmp_path).load().entries() == {}
    assert any(line.startswith("warn:") and "#7" in line for line in obs.lines)


def test_a_command_like_word_is_not_a_command(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memorywipe everything")]},
    )
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.argv_for("pr", "comment") == []
    assert ProcessedLedger(tmp_path).load().entries() == {}


def test_a_failed_acknowledgement_does_not_cause_a_second_write(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[_marked_pr(7)],
        comments={7: [_comment(111, "/memory never touch migrations")]},
        comment_result=GhResult(ok=False, error="rate limited"),
    )
    obs = _cycle(tmp_path, gh)
    body1 = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert "- never touch migrations (from PR #7 by @0xfauzi, " in body1
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert any("rate limited" in line for line in obs.lines)
    _cycle(tmp_path, gh)
    body2 = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body2 == body1
