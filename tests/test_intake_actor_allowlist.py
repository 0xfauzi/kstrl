"""#188: the trigger label authorizes spend only when the actor is allowed.

End to end on purpose. The unit under test is a DECISION about money, and
the thing that decides whether money is spent is ``serve_cycle``: it reads
a real ``kstrl.toml`` through the real loader, runs the real adapter
against a stubbed ``gh`` transport, and calls the factory runner. So the
assertions here are "did the runner get called" and "what did the operator
surfaces print", never "what did the helper return".

Only the tests that must prove the DAEMON reaches the runner drive
``serve_cycle`` (via ``_cycle``). Everything else drives ``sync()``
directly (via ``_sync``): the wire from a refusal decision to the runner
is input-independent, the tests kept on ``serve_cycle`` cover it in both
directions, and ``sync()`` is 5x-20x cheaper per test (#188 simplify pass
item 7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.intake_github import (
    Authorization,
    GhResult,
    GitHubIntakeConfig,
    SyncResult,
    authorization_refusal,
    sync,
)
from kstrl.serve import _NullObserver, serve_cycle
from kstrl.workqueue import Queue, QueueConfig
from tests.test_intake_github import (
    REPO,
    _auth_payload,
    _GhStub,
    _issue,
    _issue_payload,
)
from tests.test_queue_cli import _invoke
from tests.test_serve_seam import _enable_github_intake, _recording_runner

#: Nothing here is about flow control; the fixture's docstring in
#: tests/conftest.py says why the R10.7 bound has to be held open.
pytestmark = pytest.mark.usefixtures("no_open_prs")

#: The actor the issue is actually about: any Action with ``issues: write``
#: can apply the trigger label, with no human involved at all.
BOT = "github-actions[bot]"


def _toml(root: Path, *, allowed: str | None = '["0xfauzi"]') -> None:
    """A real kstrl.toml, read by the real loader.

    ``allowed=None`` omits the key entirely, which is the pre-feature
    configuration.
    """
    _enable_github_intake(
        root,
        extra=f"allowed_actors = {allowed}\n" if allowed is not None else "",
    )


def _cycle(root: Path, gh: _GhStub) -> tuple[list[dict[str, Any]], Any]:
    """One real serve cycle against a stubbed gh. Returns (runner calls, result)."""
    calls: list[dict[str, Any]] = []
    with patch("kstrl.intake_github.run_gh", gh):
        result = serve_cycle(root, runner=_recording_runner(calls))
    return calls, result


def _sync(root: Path, gh: _GhStub) -> SyncResult:
    """The decision alone, without the daemon composition ``_cycle`` above
    already proves."""
    queue = Queue(root, QueueConfig())
    with patch("kstrl.intake_github.run_gh", gh):
        return sync(queue, GitHubIntakeConfig.load(root), root)


class TestWhoMayAuthorizeSpend:
    def test_an_allowlisted_actor_still_reaches_the_factory(self, tmp_path: Path) -> None:
        """The control must not be a blanket refusal."""
        _toml(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=_auth_payload(),
        )
        calls, result = _cycle(tmp_path, gh)
        assert len(calls) == 1, (
            f"an allowlisted actor was refused: synced={result.synced!r} "
            f"sync_errors={result.sync_errors!r}"
        )
        assert result.synced == (f"{REPO}#7",)

    def test_a_bot_applied_label_never_reaches_the_factory(self, tmp_path: Path) -> None:
        """The path the issue is about: a workflow labels, kstrl spends."""
        _toml(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=_auth_payload(actor=BOT),
        )
        calls, result = _cycle(tmp_path, gh)
        assert calls == [], "a label applied by a non-allowlisted actor spent money"
        assert result.synced == ()
        assert Queue(tmp_path, QueueConfig()).items() == []

    def test_the_login_comparison_ignores_case(self, tmp_path: Path) -> None:
        """GitHub logins are case-insensitive; a refusal on capitalization
        alone reads as a broken control and gets deleted."""
        _toml(tmp_path, allowed='["0xFAUZI"]')
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload())
        result = _sync(tmp_path, gh)
        assert result.enqueued == (f"{REPO}#7",)

    def test_a_state_label_applied_later_by_the_operator_does_not_admit(
        self,
        tmp_path: Path,
    ) -> None:
        """Self-authorization: the adapter writes kstrl:running back under
        the operator's own token, which IS on the allowlist. Only the
        trigger label's events may decide."""
        _toml(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=_auth_payload(
                nodes=[
                    {
                        "createdAt": "2026-07-30T10:00:00Z",
                        "label": {"name": "kstrl:queued"},
                        "actor": {"login": BOT},
                    },
                    {
                        "createdAt": "2026-07-30T11:00:00Z",
                        "label": {"name": "kstrl:running"},
                        "actor": {"login": "0xfauzi"},
                    },
                ]
            ),
        )
        result = _sync(tmp_path, gh)
        assert result.enqueued == (), "a state label written back by the adapter authorized a run"

    def test_an_empty_allowlist_admits_exactly_as_before(self, tmp_path: Path) -> None:
        """Opt-in: with no allowed_actors key, nothing about today changes."""
        _toml(tmp_path, allowed=None)
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        result = _sync(tmp_path, gh)
        assert result.enqueued == (f"{REPO}#7",)


class TestTheLatestTriggerEventWins:
    """``kstrl/intake_github.py:709`` keys authorization to the LATEST
    trigger-label event, not the first. Until now nothing pinned that
    choice: earliest-wins left the whole suite green (#188 simplify pass
    item 1)."""

    def test_a_bot_first_then_an_operator_reapplying_is_admitted(
        self,
        tmp_path: Path,
    ) -> None:
        """A bot's initial label followed by the operator re-applying it
        later must admit: the later act is the one that decides."""
        _toml(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=_auth_payload(
                nodes=[
                    {
                        "createdAt": "2026-07-30T10:00:00Z",
                        "label": {"name": "kstrl:queued"},
                        "actor": {"login": BOT},
                    },
                    {
                        "createdAt": "2026-07-30T11:00:00Z",
                        "label": {"name": "kstrl:queued"},
                        "actor": {"login": "0xfauzi"},
                    },
                ]
            ),
        )
        calls, result = _cycle(tmp_path, gh)
        assert len(calls) == 1
        assert result.synced == (f"{REPO}#7",)

    def test_an_operator_first_then_a_bot_reapplying_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        """The reverse must also hold: a bot re-applying the label later
        does not inherit an operator's earlier authorization, and the
        printed reason names the actor who actually holds it now."""
        _toml(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=_auth_payload(
                nodes=[
                    {
                        "createdAt": "2026-07-30T10:00:00Z",
                        "label": {"name": "kstrl:queued"},
                        "actor": {"login": "0xfauzi"},
                    },
                    {
                        "createdAt": "2026-07-30T11:00:00Z",
                        "label": {"name": "kstrl:queued"},
                        "actor": {"login": BOT},
                    },
                ]
            ),
        )
        calls, result = _cycle(tmp_path, gh)
        assert calls == [], "a later bot re-application inherited an earlier authorization"
        assert result.synced == ()
        with patch("kstrl.intake_github.run_gh", gh):
            cli_result = _invoke(["queue", "sync"], tmp_path)
        assert BOT in cli_result.output

    def test_two_events_in_the_same_second_are_decided_by_timeline_order(
        self,
        tmp_path: Path,
    ) -> None:
        """GitHub stamps ``createdAt`` to the second, so an operator label
        and a bot re-application can carry the identical timestamp. The
        timeline is chronological, so the later node wins the tie; ``>``
        in place of ``>=`` would hand the tie to the earlier actor and
        left the whole suite green (verifier plant MINE-3 on PR #382)."""
        _toml(tmp_path)
        same_second = "2026-07-30T10:00:00Z"
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=_auth_payload(
                nodes=[
                    {
                        "createdAt": same_second,
                        "label": {"name": "kstrl:queued"},
                        "actor": {"login": "0xfauzi"},
                    },
                    {
                        "createdAt": same_second,
                        "label": {"name": "kstrl:queued"},
                        "actor": {"login": BOT},
                    },
                ]
            ),
        )
        calls, result = _cycle(tmp_path, gh)
        assert calls == [], "a same-second bot re-application inherited the authorization"
        assert result.synced == ()


class TestItFailsClosed:
    def test_an_unreadable_timeline_is_refused(self, tmp_path: Path) -> None:
        """The test plant 3 moves."""
        _toml(tmp_path, allowed=None)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=GhResult(ok=False, error="HTTP 502"),
        )
        result = _sync(tmp_path, gh)
        assert result.enqueued == ()

    def test_a_refusal_with_no_reason_is_still_a_refusal(self) -> None:
        """``authorization_refusal`` returns "" only for an admission, so a
        refusal that arrives without a reason must not read as one. No
        production path builds ``Authorization(ok=False, reason="")``
        today; this pins the fallback so the day one does, the gate stays
        shut (verifier plant MINE-4 on PR #382)."""
        config = GitHubIntakeConfig(allowed_actors=["0xfauzi"])
        assert authorization_refusal(config, Authorization(ok=False, reason="")) != ""

    def test_a_labelling_event_with_no_actor_is_refused(self, tmp_path: Path) -> None:
        """GitHub returns a null actor for a deleted account. Unknown is
        not allowed."""
        _toml(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=_auth_payload(
                nodes=[
                    {
                        "createdAt": "2026-07-30T10:00:00Z",
                        "label": {"name": "kstrl:queued"},
                        "actor": None,
                    },
                ]
            ),
        )
        result = _sync(tmp_path, gh)
        assert result.enqueued == ()


class TestTheDaemonNarratesARefusal:
    """kstrl/serve.py::_run_intake (#188 simplify pass item 2): a refusal
    silently dropped by the daemon is a control nobody watching it run can
    see working."""

    def test_a_refusal_is_narrated(self, tmp_path: Path) -> None:
        _toml(tmp_path)
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        obs = _NullObserver()
        with patch("kstrl.intake_github.run_gh", gh):
            serve_cycle(tmp_path, runner=_recording_runner([]), observer=obs)
        warns = [line for line in obs.lines if "intake refused" in line]
        assert len(warns) == 1, obs.lines
        assert BOT in warns[0]
        assert "kstrl:queued" in warns[0]

    def test_an_empty_allowlist_narrates_nothing(self, tmp_path: Path) -> None:
        _toml(tmp_path, allowed=None)
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        obs = _NullObserver()
        with patch("kstrl.intake_github.run_gh", gh):
            serve_cycle(tmp_path, runner=_recording_runner([]), observer=obs)
        assert not any("intake refused" in line for line in obs.lines)


class TestTheOperatorCanFindOutWhy:
    """The issue's own acceptance: a label that did nothing must be
    explainable, with the actor named."""

    def test_queue_sync_names_the_actor_and_the_allowlist(self, tmp_path: Path) -> None:
        _toml(tmp_path)
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        with patch("kstrl.intake_github.run_gh", gh):
            result = _invoke(["queue", "sync"], tmp_path)
        assert result.exit_code == 0, result.output
        assert "refuse_unauthorized" in result.output
        assert BOT in result.output
        # The printed allowlist itself (kstrl/cli.py's own kv line:
        # "allowed_actors:0xfauzi"), not its incidental mention inside the
        # refusal reason ("allowed_actors (0xfauzi)") - the two render
        # differently, and only the kv line's own spelling proves it.
        assert "allowed_actors:0xfauzi" in result.output
        assert Queue(tmp_path, QueueConfig()).items() == []

    def test_serve_dry_run_names_the_actor(self, tmp_path: Path) -> None:
        _toml(tmp_path)
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        with patch("kstrl.intake_github.run_gh", gh):
            result = _invoke(["serve", "--dry-run"], tmp_path)
        assert f"skip {REPO}#7:" in result.output, result.output
        assert BOT in result.output
        assert "allowed_actors:0xfauzi" in result.output


class TestTheValueIsCheckedBeforeAnythingIsSpent:
    """A config fault is reported by the pre-spend entry check, before a
    poll is ever paid for."""

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            ('"0xfauzi"', "must be a list"),
            ("[7]", "allowed_actors[0]"),
            ('["0xfauzi", "   "]', "allowed_actors[1]"),
        ],
    )
    def test_a_malformed_allowed_actors_is_a_config_error(
        self,
        tmp_path: Path,
        value: str,
        fragment: str,
    ) -> None:
        _toml(tmp_path, allowed=value)
        result = _invoke(["queue", "sync"], tmp_path)
        assert result.exit_code == 1, result.output
        assert "configuration rejected before anything was started" in result.output
        assert "[intake_github]" in result.output
        assert fragment in result.output

    def test_a_good_value_reaches_the_loader(self, tmp_path: Path) -> None:
        """The pin for repro check 1: the key is read, not discarded.

        The only loader-level assertion in this file, and it is here
        because "the config carries the field" is exactly what the
        reproduction measured.
        """
        _toml(tmp_path, allowed='["0xfauzi", "someone-else"]')
        assert GitHubIntakeConfig.load(tmp_path).allowed_actors == [
            "0xfauzi",
            "someone-else",
        ]


class TestTheEnvDoor:
    """The env var must reach the GATE, not just the dataclass.

    Both tests run a real sync, because a loader that parses the variable
    into a field nothing consults is the same as not reading it.
    """

    def test_the_env_var_gates_admission_when_the_file_sets_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _toml(tmp_path, allowed="[]")
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_ALLOWED_ACTORS", "0xfauzi, someone-else")
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        result = _sync(tmp_path, gh)
        assert result.enqueued == (), "the env allowlist was parsed but never consulted"

    def test_a_set_but_empty_env_var_turns_the_allowlist_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The two doors disagree about "" the way every other pair in this
        project does, and the rule is written down where the field is: a
        SET but empty variable means "no allowlist" and overrides the file.
        """
        _toml(tmp_path)
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_ALLOWED_ACTORS", "")
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        result = _sync(tmp_path, gh)
        assert result.enqueued == (f"{REPO}#7",)
