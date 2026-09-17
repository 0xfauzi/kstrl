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
from kstrl.intake_github import GhResult, ProcessedLedger
from kstrl.operator_context import GUIDANCE_HEADING
from kstrl.serve import _NullObserver, serve_cycle
from kstrl.workqueue import ItemSource, MergeDisposition, Queue, QueueConfig
from tests.helpers.fakegh import GhRouter, marked, unmarked
from tests.helpers.runners import recording_runner
from tests.test_init_cmd import section_of
from tests.test_intake_actor_allowlist import BOT
from tests.test_intake_github import REPO
from tests.test_serve_seam import _enable_github_intake

#: `_comment`'s default author. Local rather than imported: no other
#: module needs a "the owner steered this" login, so there is nothing to
#: share (#231 D1 shares REPO and BOT, both already declared elsewhere;
#: OWNER is not).
OWNER = "0xfauzi"


def _steer_pr_url(number: int, repo: str = REPO) -> str:
    """A PR's URL. Named distinctly from `intake_github._pr_url`, whose
    argument order (`repo, number`) this deliberately does not match
    (#231 D5): two functions of the same name and opposite argument
    order is its own trap, not a naming collision worth keeping.
    """
    return f"https://github.com/{repo}/pull/{number}"


def _updated_at(comment_id: int) -> str:
    """A distinct, strictly increasing ISO-8601 timestamp per comment id.

    Distinct rather than one shared constant, so a case exercising C1's
    watermark (the cap-deferral and clock-reading plants) tests the
    REAL ordering GitHub's `updated_at` provides, not a fixture that
    happens to make every comment tie.
    """
    hours, minutes = divmod(comment_id, 60)
    return f"2026-01-01T{hours:02d}:{minutes:02d}:00Z"


def _comment(
    comment_id: int,
    body: str,
    *,
    login: str = OWNER,
    association: str = "OWNER",
    updated_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": login},
        "author_association": association,
        "updated_at": updated_at if updated_at is not None else _updated_at(comment_id),
    }


class _SteerGh(GhRouter):
    """The three routes `GhRouter` does not already answer.

    #231 D2: the shared skeleton (`self.calls`, the `__call__` shape,
    `repo view`, `pr list`, `issue list`, the empty-success fallback,
    `argv_for`) lives in `GhRouter`; this class is only the
    issue-comments endpoint (`api`), `pr comment`, and `comments_raw`'s
    override of the first.
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
        super().__init__(prs=prs, repo=repo)
        self.comments = comments or {}
        # `comments_raw`, when set, is returned for the comments endpoint
        # VERBATIM instead of `json.dumps(self.comments[...])`. Case 20
        # needs a payload `json.loads` cannot read and a payload whose
        # rows are not comment records, and neither can be expressed as a
        # `list[dict[str, Any]]`.
        self.comments_raw = comments_raw
        self.comment_result = comment_result or GhResult(ok=True)

    def _route(self, head: list[str], args: list[str]) -> GhResult | None:
        if head[:1] == ["api"]:
            if self.comments_raw is not None:
                return GhResult(ok=True, stdout=self.comments_raw)
            endpoint = args[1]
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            rows = self.comments.get(number, [])
            # `since` (#231 C1), REAL filtering and not just recorded:
            # only this makes a plant that advances the watermark from a
            # clock reading, or one that drops `since` from the request,
            # observable through the stub rather than merely through the
            # argv.
            if "since=" in endpoint:
                since = endpoint.split("since=", 1)[1]
                rows = [row for row in rows if str(row.get("updated_at", "")) >= since]
            return GhResult(ok=True, stdout=json.dumps(rows))
        if head == ["pr", "comment"]:
            return self.comment_result
        return None

    def posted(self) -> list[str]:
        """Every comment body this stub was asked to post."""
        return [c[c.index("--body") + 1] for c in self.argv_for("pr", "comment")]


def _gh(body: str, *, pr: int = 7, comment_id: int = 111, **comment_kwargs: Any) -> _SteerGh:
    """The single-marked-PR, single-comment `_SteerGh` most cases need.

    #231 D4: 17 of 21 `_SteerGh(...)` constructions before this differed
    only in the comment body (occasionally the login or association) -
    one call each, not a four-line literal repeating `marked(7)` and
    `111` each time.
    """
    return _SteerGh(prs=[marked(pr)], comments={pr: [_comment(comment_id, body, **comment_kwargs)]})


def _setup(
    root: Path,
    *,
    steer: str = "true",
    extra: str = "",
    memory: str = DEFAULT_MEMORY,
) -> None:
    """A root with [intake_github] and a scaffolded memory file.

    #231 D1: `[intake_github]`'s toml block itself is
    `_enable_github_intake` (`tests/test_serve_seam.py`), `enabled=False`
    since steering is what this module tests - intake stays off in every
    case - and `steer_enabled` and any other extra keys ride along in
    `extra`, which that helper already appends verbatim.

    #231 D5: returns nothing. It used to return the memory path, and 19
    of its 20 callers discarded it and called `_memory_path(root)`
    instead - one accessor, not two spellings of the same path.
    """
    _enable_github_intake(root, extra=f"steer_enabled = {steer}\n" + extra, enabled=False)
    path = root / "scripts" / "kstrl" / "memory.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(memory, encoding="utf-8")


def _cycle(root: Path, gh: _SteerGh) -> _NullObserver:
    """One real serve cycle against a stubbed gh. Returns the observer."""
    obs = _NullObserver()
    with patch("kstrl.intake_github.run_gh", gh):
        serve_cycle(root, runner=recording_runner([]), observer=obs)
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
    gh = _gh("/memory never touch migrations")
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    prefix = "- never touch migrations (from PR #7 by @0xfauzi, "
    assert prefix in body
    line = next(ln for ln in body.splitlines() if ln.startswith(prefix))
    assert section_of(body, line) == GUIDANCE_HEADING
    assert line.endswith(")")
    date_part = line[len(prefix) : -1]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part)
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert gh.posted() == [
        'Recorded to scripts/kstrl/memory.md: "never touch migrations". Commit it to keep it.'
    ]


def test_a_second_cycle_changes_nothing(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _gh("/memory never touch migrations")
    _cycle(tmp_path, gh)
    body1 = _memory_path(tmp_path).read_text(encoding="utf-8")
    _cycle(tmp_path, gh)
    body2 = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body2 == body1
    assert len(gh.posted()) == 1


def test_a_section_after_guidance_does_not_take_the_append(tmp_path: Path) -> None:
    memory = DEFAULT_MEMORY + "\n## Notes\n\n- my own scratch\n"
    _setup(tmp_path, memory=memory)
    gh = _gh("/memory never touch migrations")
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    prefix = "- never touch migrations (from PR #7 by @0xfauzi, "
    line = next(ln for ln in body.splitlines() if ln.startswith(prefix))
    assert section_of(body, line) == GUIDANCE_HEADING
    assert "- my own scratch" in body
    assert section_of(body, "- my own scratch") == "## Notes"


def test_a_memory_file_without_the_heading_gains_one(tmp_path: Path) -> None:
    memory = "# Memory\n\n## Notes\n\n- scratch\n"
    _setup(tmp_path, memory=memory)
    gh = _gh("/memory x")
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert GUIDANCE_HEADING in body
    prefix = "- x (from PR #7 by @0xfauzi, "
    line = next(ln for ln in body.splitlines() if ln.startswith(prefix))
    assert section_of(body, line) == GUIDANCE_HEADING
    assert "- scratch" in body
    assert section_of(body, "- scratch") == "## Notes"


def test_an_unauthorised_author_is_skipped_and_named(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _gh("/memory x", login=BOT, association="NONE")
    obs = _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.argv_for("pr", "comment") == []
    assert not ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert any(BOT in line and "NONE" in line for line in obs.lines)


def test_an_allowlisted_login_overrides_the_association(tmp_path: Path) -> None:
    _setup(tmp_path, extra='allowed_actors = ["alice"]\n')
    gh = _SteerGh(
        prs=[marked(7)],
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
    queue = _finished_item_recording(tmp_path, _steer_pr_url(7))
    gh = _gh("/iterate fix the off-by-one")
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
    queue = _finished_item_recording(tmp_path, _steer_pr_url(7, repo="someone/else"))
    gh = _gh("/iterate fix the off-by-one")
    _cycle(tmp_path, gh)
    assert len(queue.items()) == 1
    assert len(gh.posted()) == 1
    assert gh.posted()[0].endswith("Cannot iterate: no queue item recorded this PR")


def test_iterate_without_a_recorded_item_says_so_and_enqueues_nothing(tmp_path: Path) -> None:
    _setup(tmp_path)
    queue = Queue(tmp_path, QueueConfig())
    gh = _gh("/iterate fix it")
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert "- fix it (from PR #7 by @0xfauzi, " in body
    assert queue.items() == []
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert len(gh.posted()) == 1
    assert gh.posted()[0].endswith("Cannot iterate: no queue item recorded this PR")


def test_a_bare_iterate_skips_the_memory_step(tmp_path: Path) -> None:
    _setup(tmp_path)
    queue = _finished_item_recording(tmp_path, _steer_pr_url(7))
    gh = _gh("/iterate")
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert any(item.source_ref == f"{REPO}#5#iterate-111" for item in queue.items())


@pytest.mark.parametrize(
    ("text", "in_posted"),
    [
        pytest.param("/memory " + "x" * 501, "501", id="overlong"),
        pytest.param("/memory rule one\n# Heading\nrule two", "#", id="heading_line"),
        pytest.param("/memory", "Not recorded: ", id="bare"),
    ],
)
def test_a_malformed_memory_text_is_refused_and_recorded(
    tmp_path: Path,
    text: str,
    in_posted: str,
) -> None:
    """#231 D4: the three format refusals, parametrized. A refusal is
    TERMINAL - re-posting it every cycle would be worse than posting it
    once - so all three, not just two of them, must land in the ledger.
    """
    _setup(tmp_path)
    gh = _gh(text)
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    posted = gh.posted()
    assert posted[0].startswith("Not recorded: ")
    assert in_posted in posted[0]


def test_dry_run_writes_nothing_and_posts_nothing(tmp_path: Path) -> None:
    _setup(tmp_path, extra="dry_run = true\n")
    gh = _gh("/memory never touch migrations")
    obs = _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.argv_for("pr", "comment") == []
    assert not ProcessedLedger(tmp_path).load().contains(_ledger_key(7, 111))
    assert any("dry run" in line and "111" in line for line in obs.lines)


def test_steering_off_makes_no_comment_call(tmp_path: Path) -> None:
    _setup(tmp_path, steer="false")
    gh = _gh("/memory never touch migrations")
    _cycle(tmp_path, gh)
    assert gh.argv_for("api") == []
    assert gh.argv_for("repo", "view") == []
    assert [c[:2] for c in gh.calls] == [["pr", "list"]]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_a_failed_memory_write_is_retried_next_cycle(tmp_path: Path) -> None:
    _setup(tmp_path)
    memory_path = _memory_path(tmp_path)
    gh = _gh("/memory never touch migrations")
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
        prs=[marked(7)],
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
        prs=[marked(7), unmarked(8)],
        comments={
            7: [_comment(111, "/memory x")],
            8: [_comment(222, "/memory y")],
        },
    )
    _cycle(tmp_path, gh)
    assert [c[1] for c in gh.argv_for("api")] == [f"repos/{REPO}/issues/7/comments?per_page=100"]
    assert len(gh.argv_for("pr", "list")) == 2
    # #231 A4: the TOTAL call count, not just the `pr list` half - one
    # `gh repo view`, two `gh pr list` (the count `_run_intake` shares
    # with steering, and the open-PR bound's own at step 4 of the
    # cycle), one comments fetch for the one MARKED pr, and one `pr
    # comment` acknowledging the one command that ran. 2 + P gh calls
    # for the intake+steering half (P=1 marked PR here) plus one per
    # acted command, which is what the PR body's cost formula states.
    assert len(gh.calls) == 5


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
    gh = _SteerGh(prs=[marked(7)], comments_raw=comments_raw)
    obs = _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.argv_for("pr", "comment") == []
    assert ProcessedLedger(tmp_path).load().entries() == {}
    assert any(line.startswith("warn:") and "#7" in line for line in obs.lines)


def test_a_command_like_word_is_not_a_command(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _gh("/memorywipe everything")
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert body == DEFAULT_MEMORY
    assert gh.argv_for("pr", "comment") == []
    assert ProcessedLedger(tmp_path).load().entries() == {}


def test_a_failed_acknowledgement_does_not_cause_a_second_write(tmp_path: Path) -> None:
    _setup(tmp_path)
    gh = _SteerGh(
        prs=[marked(7)],
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


# ---------------------------------------------------------------------------
# C1: the per-pull-request watermark
# ---------------------------------------------------------------------------


def test_a_second_cycle_sends_since_the_first_cycles_watermark(tmp_path: Path) -> None:
    """Plant P5's control: dropping `since` from the request must turn this red."""
    _setup(tmp_path)
    gh = _gh("/memory never touch migrations")
    _cycle(tmp_path, gh)
    _cycle(tmp_path, gh)
    api_calls = gh.argv_for("api")
    assert len(api_calls) == 2
    assert "since=" not in api_calls[0][1]
    assert f"since={_updated_at(111)}" in api_calls[1][1]


def test_the_cap_deferred_comment_still_blocks_the_watermark(tmp_path: Path) -> None:
    """Plant P4's control, alongside `test_a_failed_memory_write_is_retried_next_cycle`.

    The cap defers comment 113 in cycle 1 (case 17's own scenario). A
    watermark advanced to a CLOCK reading rather than to the greatest
    RESOLVED `updated_at` would set `since` past comment 111 and 112 as
    well as 113 - past everything - and the stub's real `since` filter
    (unlike the argv-only check above) would then drop 113 from cycle
    2's fetch forever, which is exactly the bug C1's rule forbids.
    """
    _setup(tmp_path, extra="max_items_per_sync = 2\n")
    gh = _SteerGh(
        prs=[marked(7)],
        comments={
            7: [
                _comment(111, "/memory one"),
                _comment(112, "/memory two"),
                _comment(113, "/memory three"),
            ]
        },
    )
    _cycle(tmp_path, gh)
    _cycle(tmp_path, gh)
    body = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert "- three (" in body
    api_calls = gh.argv_for("api")
    assert len(api_calls) == 2
    # The watermark must not have advanced past comment 112: cycle 2
    # still asks for everything from 112 onward, which is what lets 113
    # (deferred, never resolved in cycle 1) be seen again.
    assert f"since={_updated_at(112)}" in api_calls[1][1]
