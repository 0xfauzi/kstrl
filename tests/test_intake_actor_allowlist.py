"""#188: the trigger label authorizes spend only when the actor is allowed.

End to end on purpose. The unit under test is a DECISION about money, and
the thing that decides whether money is spent is ``serve_cycle``: it reads
a real ``kstrl.toml`` through the real loader, runs the real adapter
against a stubbed ``gh`` transport, and calls the factory runner. So the
assertions here are "did the runner get called" and "what did the operator
surfaces print", never "what did the helper return".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.intake_github import GhResult, GitHubIntakeConfig
from kstrl.serve import RunOutcome, serve_cycle
from kstrl.workqueue import Queue, QueueConfig
from tests.test_intake_github import (
    REPO,
    _auth_payload,
    _GhStub,
    _issue,
    _issue_payload,
)

#: Held open for the same reason tests/test_intake_github.py holds it: none
#: of this is about flow control. The fixture monkeypatches
#: kstrl.serve.count_open_kstrl_prs for every test in this module, so no
#: test here patches it again. See the fixture's docstring in
#: tests/conftest.py.
pytestmark = pytest.mark.usefixtures("no_open_prs")

#: The actor the issue is actually about: any Action with ``issues: write``
#: can apply the trigger label, with no human involved at all.
BOT = "github-actions[bot]"
OWNER = "0xfauzi"

TRIGGER = "kstrl:queued"


def _toml(root: Path, *, allowed: str | None = '["0xfauzi"]') -> None:
    """A real kstrl.toml, read by the real loader.

    ``allowed=None`` omits the key entirely, which is the pre-feature
    configuration.
    """
    lines = [
        "[intake_github]",
        "enabled = true",
        f'repo = "{REPO}"',
        "comment_on_result = false",
    ]
    if allowed is not None:
        lines.append(f"allowed_actors = {allowed}")
    root.joinpath("kstrl.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _recording_runner(calls: list[dict[str, Any]]) -> Any:
    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: Any = None,
    ) -> RunOutcome:
        calls.append({"project_name": project_name, "spec_path": spec_path})
        return RunOutcome(0)

    return runner


def _cycle(root: Path, gh: _GhStub) -> tuple[list[dict[str, Any]], Any]:
    """One real serve cycle against a stubbed gh. Returns (runner calls, result)."""
    calls: list[dict[str, Any]] = []
    with patch("kstrl.intake_github.run_gh", gh):
        result = serve_cycle(root, runner=_recording_runner(calls))
    return calls, result


def _invoke(args: list[str], root: Path) -> Result:
    return CliRunner().invoke(cli, [*args, "--root", str(root), "--ui", "plain", "--no-color"])


class TestWhoMayAuthorizeSpend:
    def test_an_allowlisted_actor_still_reaches_the_factory(self, tmp_path: Path) -> None:
        """The control must not be a blanket refusal."""
        _toml(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=_auth_payload(actor=OWNER),
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
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=OWNER))
        calls, _ = _cycle(tmp_path, gh)
        assert len(calls) == 1

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
                        "label": {"name": TRIGGER},
                        "actor": {"login": BOT},
                    },
                    {
                        "createdAt": "2026-07-30T11:00:00Z",
                        "label": {"name": "kstrl:running"},
                        "actor": {"login": OWNER},
                    },
                ]
            ),
        )
        calls, result = _cycle(tmp_path, gh)
        assert calls == [], "a state label written back by the adapter authorized a run"
        assert result.synced == ()

    def test_an_empty_allowlist_admits_exactly_as_before(self, tmp_path: Path) -> None:
        """Opt-in: with no allowed_actors key, nothing about today changes."""
        _toml(tmp_path, allowed=None)
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        calls, _ = _cycle(tmp_path, gh)
        assert len(calls) == 1


class TestItFailsClosed:
    def test_an_unreadable_timeline_never_reaches_the_factory(self, tmp_path: Path) -> None:
        """ "We could not check" is not evidence a trusted actor labelled it."""
        _toml(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=GhResult(ok=False, error="HTTP 502"),
        )
        calls, _ = _cycle(tmp_path, gh)
        assert calls == []

    def test_an_unreadable_timeline_is_refused_with_no_allowlist_either(
        self,
        tmp_path: Path,
    ) -> None:
        """The #187 F1 refusal must survive the refactor that absorbs it.

        With the allowlist EMPTY there is no second reason to refuse, so
        this is the test that goes red if ``authorization_refusal`` stops
        honouring ``auth.ok``. Its sibling above cannot do that job: with
        the allowlist on, an admitted-but-actorless authorization is
        refused by the no-actor branch instead, and the test stays green.
        """
        _toml(tmp_path, allowed=None)
        gh = _GhStub(
            issues=_issue_payload(_issue(7)),
            auth=GhResult(ok=False, error="HTTP 502"),
        )
        calls, _ = _cycle(tmp_path, gh)
        assert calls == []

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
                        "label": {"name": TRIGGER},
                        "actor": None,
                    },
                ]
            ),
        )
        calls, _ = _cycle(tmp_path, gh)
        assert calls == [], "a labelling event with no actor login authorized a run"


class TestTheOperatorCanFindOutWhy:
    """The issue's own acceptance: a label that did nothing must be
    explainable, with the actor named."""

    def test_queue_sync_names_the_actor_and_the_allowlist(self, tmp_path: Path) -> None:
        _toml(tmp_path)
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        with patch("kstrl.intake_github.run_gh", gh):
            result = _invoke(["queue", "sync"], tmp_path)
        assert result.exit_code == 0, result.output
        # BOTH tokens below appear only inside the refusal reason. Do NOT
        # assert on the bare label instead: `ks queue sync` prints
        # `label: kstrl:queued` in its header regardless, so that assertion
        # passes with no refusal at all. Measured on this tree before the
        # change (see measurements.md section 7).
        assert "refuse_unauthorized" in result.output
        assert BOT in result.output
        assert "allowed_actors" in result.output
        assert Queue(tmp_path, QueueConfig()).items() == []

    def test_serve_dry_run_names_the_actor(self, tmp_path: Path) -> None:
        _toml(tmp_path)
        gh = _GhStub(issues=_issue_payload(_issue(7)), auth=_auth_payload(actor=BOT))
        with patch("kstrl.intake_github.run_gh", gh):
            result = _invoke(["serve", "--dry-run"], tmp_path)
        assert f"skip {REPO}#7:" in result.output, result.output
        assert BOT in result.output


class TestTheValueIsCheckedBeforeAnythingIsSpent:
    """A config fault is reported by the pre-spend entry check, through the
    same path every other [intake_github] fault takes, not at admission
    time when the poll has already been paid for.

    Driven through the real CLI rather than through ``preflight_config``,
    because the observable outcome an operator gets is the exit status and
    the line, and the phrase asserted below is the one only the pre-spend
    path prints. Measured on this tree with an existing bad key
    (``max_items_per_sync = 0``): ``ks queue sync`` exits 1 and prints
    ``error: configuration rejected before anything was started; fix it and
    run again:`` followed by
    ``[intake_github] intake_github.max_items_per_sync must be >= 1, got 0
    (kstrl.toml has [intake_github] max_items_per_sync = 0)``.
    """

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            ('"0xfauzi"', "must be a list"),
            ('["0xfauzi", ""]', "allowed_actors[1]"),
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

    Both tests run a real cycle, because a loader that parses the variable
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
        calls, _ = _cycle(tmp_path, gh)
        assert calls == [], "the env allowlist was parsed but never consulted"

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
        calls, _ = _cycle(tmp_path, gh)
        assert len(calls) == 1
