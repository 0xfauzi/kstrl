"""R10.7: the open-PR bound, the daemon's flow control, driven through
``serve_cycle``, ``serve`` and ``ks serve --dry-run``.

The property these tests exist to hold is narrow and easy to lose: the
daemon stops admitting work while `max_open_prs` kstrl-authored pull
requests are open, and it reaches GitHub to find that out ONLY when
every cheaper local gate has already admitted. The second half is not
decoration. The gates tuple in `serve_cycle` is built eagerly, so a
member of it runs even when an earlier member refuses; that is why this
gate is a standalone check after the factory lock rather than a fifth
tuple entry, and `TestGateOrderIsCost` is what keeps it there - one
arrangement per boundary the ordering claims, and a positive control
first, because `assert not marker.exists()` also passes when the fake
`gh` was never reachable.

Every test here runs a real cycle against a fake ``gh`` on PATH: one
open marked PR holds the item in the queue until it merges; a gate that
refuses with an empty reason still stops the cycle; three consecutive
count failures inside one ``serve`` loop file exactly one inbox item and
a good count in between resets the streak; ``--dry-run`` lists the gate
last and names it when it blocks; and ``ServeConfig.load`` reads the
bound from ``kstrl.toml``, lets the environment win, and refuses a
negative bound.

What a payload MEANS, and what counts as a kstrl-authored pull request,
is `tests/test_open_pr_counter.py`. This file is what the daemon does
with the answer.
"""

from __future__ import annotations

import fcntl
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.serve import (
    Admission,
    CycleResult,
    RunOutcome,
    ServeConfig,
    ServeError,
    SpendLedger,
    serve,
    serve_cycle,
    state_dir,
)
from kstrl.workqueue import ItemState
from tests.helpers.fakegh import FAKE_GH_THIRD_CALL_WORKS as _FAKE_GH_THIRD_CALL_WORKS
from tests.helpers.fakegh import install_fake_gh as _install_fake_gh
from tests.helpers.fakegh import install_marker_gh as _install_marker_gh
from tests.helpers.fakegh import marked as _marked
from tests.helpers.fakegh import put_gh_on_path as _put_gh_on_path
from tests.helpers.stack_confirmation import confirm_stack, write_stack
from tests.test_serve import _add, _no_spend, _queue, _stub_runner  # noqa: F401


@pytest.fixture(autouse=True)
def _confirmed_stack(tmp_path: Path) -> None:
    """Every ``serve_cycle``/``serve`` call here reads ``tmp_path``'s
    ``[stack]`` before it claims anything (#696 flag day): with none it
    refuses before any of the gates this file exists to test. A test that
    overwrites kstrl.toml for its own config (`[serve]`, `[inbox]`) does so
    after this fixture, which is why those do not need one of their own."""
    write_stack(tmp_path)
    confirm_stack(tmp_path)


# ---------------------------------------------------------------------------
# The gate inside the cycle
# ---------------------------------------------------------------------------


class TestServeCycleGate:
    def test_open_pr_holds_the_item_in_the_queue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One marked PR open, bound 1: nothing is leased and nothing runs."""
        payload = _install_fake_gh(tmp_path, monkeypatch, [_marked(1)])
        queue = _queue(tmp_path)
        _add(queue)
        calls: list[dict[str, object]] = []

        result = serve_cycle(
            tmp_path,
            config=ServeConfig(max_open_prs=1),
            runner=_stub_runner(RunOutcome(0), calls),
        )

        assert result.ran_item == ""
        assert "bound 1" in result.skipped
        assert result.paused == ""
        assert calls == []
        assert _queue(tmp_path).items()[0].state is ItemState.QUEUED

        # The same repo with the PR merged admits the same item.
        payload.write_text("[]", encoding="utf-8")
        result = serve_cycle(
            tmp_path,
            config=ServeConfig(max_open_prs=1),
            runner=_stub_runner(RunOutcome(0), calls),
        )

        assert result.ran_item != ""
        assert result.skipped == ""
        assert len(calls) == 1


class TestGateOrderIsCost:
    """Every boundary the ordering claims, and the control that proves the fake works.

    `assert not marker.exists()` on its own is not a control: it passes
    when the gate is correctly ordered AND when the fake `gh` was never
    reachable at all, which is the same shape as the defect. So the
    positive control comes first, in the same class, with the same fake
    on the same PATH. Two measured mutations made the old single
    assertion vacuous: writing the fake where PATH never looks, and
    making the fake stop touching its marker; both left the suite green.

    Three boundaries, because the docstring claims three. The budget
    gate is a member of the eagerly-built `gates` tuple; the inbox cap
    and the factory lock are the two checks inside
    `_wait_gate_refusal` that run before the bound. Moving the bound
    first inside that function left all 308 tests green before this.
    """

    def _cycle(self, tmp_path: Path, config: ServeConfig | None = None) -> CycleResult:
        _add(_queue(tmp_path))
        return serve_cycle(
            tmp_path,
            config=config or ServeConfig(max_open_prs=1),
            runner=_stub_runner(RunOutcome(0)),
        )

    def test_the_fake_gh_is_reached_when_every_earlier_gate_admits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POSITIVE CONTROL for the three negative assertions below.

        Nothing refuses before the bound here, so the fake must run. If
        this goes red the fake is unreachable and every `not
        marker.exists()` in this class is measuring nothing.
        """
        marker = _install_marker_gh(tmp_path, monkeypatch)

        result = self._cycle(tmp_path)

        assert marker.exists(), "the fake gh is not on PATH; the negative tests are vacuous"
        assert "cannot count" in result.skipped

    def test_no_gh_call_when_the_budget_gate_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The eager `gates` tuple: membership of it would call gh anyway."""
        marker = _install_marker_gh(tmp_path, monkeypatch)
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\ndaily_budget_usd = 5.0\nallow_uncovered_cost = true\n",
            encoding="utf-8",
        )
        SpendLedger(tmp_path).charge(10.0, covered_calls=1, total_calls=1)

        result = self._cycle(
            tmp_path,
            ServeConfig(max_open_prs=1, daily_budget_usd=5.0, allow_uncovered_cost=True),
        )

        assert "daily budget reached" in result.skipped
        assert not marker.exists(), "the open-PR gate reached gh behind a refusing budget gate"

    def test_no_gh_call_when_the_inbox_cap_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First of the two checks inside `_wait_gate_refusal`."""
        marker = _install_marker_gh(tmp_path, monkeypatch)
        (tmp_path / "kstrl.toml").write_text(
            "[inbox]\nopen_item_cap = 1\n",
            encoding="utf-8",
        )
        Inbox(tmp_path, InboxConfig.load(tmp_path)).add(ItemKind.HALTED_RUN, "already full")

        result = self._cycle(tmp_path)

        assert "open-item cap" in result.skipped
        assert not marker.exists(), "the open-PR gate reached gh behind a full inbox"

    def test_no_gh_call_when_the_factory_lock_is_held(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second of the two, and a real flock rather than a patch."""
        marker = _install_marker_gh(tmp_path, monkeypatch)
        lock_path = state_dir(tmp_path) / "factory.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self._cycle(tmp_path)

        assert "already holds" in result.skipped
        assert not marker.exists(), "the open-PR gate reached gh behind a held factory lock"


class TestEmptyRefusalIsStillARefusal:
    """`_wait_gate_refusal` returns `str | None`, and this is the control.

    None of the three wait gates can produce an empty reason today, so
    the change from `""` to None is a representation fix with nothing
    observable behind it - which means a mutation back to `if waiting:`
    stays green, and the sentinel has no mechanism. Measured: it does.

    So one gate is made to refuse with an empty reason. The cycle must
    still skip. Under the `""` sentinel it would lease the item and
    spend, with nothing anywhere saying why.
    """

    def test_a_gate_refusing_with_no_reason_still_stops_the_cycle(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = _install_marker_gh(tmp_path, monkeypatch)
        _add(_queue(tmp_path))
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            "kstrl.serve.check_inbox_cap",
            lambda root: Admission(allowed=False, reason=""),
        )

        result = serve_cycle(
            tmp_path,
            config=ServeConfig(max_open_prs=1),
            runner=_stub_runner(RunOutcome(0), calls),
        )

        assert calls == [], "an empty refusal was read as an admission and spent"
        assert result.ran_item == ""
        assert _queue(tmp_path).items()[0].state is ItemState.QUEUED
        assert not marker.exists(), "the refusal did not stop the cycle before the bound"


# ---------------------------------------------------------------------------
# A count that never works
# ---------------------------------------------------------------------------


class TestPersistentCountFailure:
    """A wait is right for a condition that clears itself, and wrong here.

    An expired `gh` token and a `gh` missing from launchd's PATH are the
    two failures an unattended daemon actually meets, and neither ever
    clears. Before this the daemon waited on them forever while every
    surface an operator checks read healthy: the queue unpaused, `ks
    inbox` empty, `needs_human` False, the exit code 0, and one WARN
    line per poll in `serve.err.log` as the only evidence anywhere.
    """

    def _run(self, tmp_path: Path, cycles: int) -> list[CycleResult]:
        return serve(
            tmp_path,
            config=ServeConfig(max_open_prs=1),
            runner=_stub_runner(RunOutcome(0)),
            max_cycles=cycles,
            sleeper=lambda _: None,
        )

    def _open_items(self, tmp_path: Path) -> list[object]:
        return Inbox(tmp_path, InboxConfig.load(tmp_path)).open_items()

    def test_two_failures_file_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Under the threshold is still a wait; a rate limit clears itself."""
        _install_marker_gh(tmp_path, monkeypatch)
        _add(_queue(tmp_path))

        results = self._run(tmp_path, 2)

        assert all("cannot count" in r.skipped for r in results)
        assert self._open_items(tmp_path) == []
        assert not any(r.needs_human for r in results)

    def test_three_failures_file_exactly_one_item(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_marker_gh(tmp_path, monkeypatch)
        _add(_queue(tmp_path))

        results = self._run(tmp_path, 6)

        items = self._open_items(tmp_path)
        assert len(items) == 1, "one item per streak, not one per poll"
        assert items[0].kind is ItemKind.HALTED_RUN  # type: ignore[attr-defined]
        assert items[0].occurrences == 1  # type: ignore[attr-defined]
        assert "cannot count" in items[0].detail  # type: ignore[attr-defined]
        assert [r.needs_human for r in results] == [False, False, True, False, False, False]

    def test_a_successful_count_resets_the_streak(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail, fail, SUCCEED, fail, fail inside ONE loop: nothing filed.

        The reset is what keeps an intermittent `gh` from filing on the
        strength of failures spread over an afternoon.

        All five polls have to be one `serve` call. `serve` builds its
        own streak per call, so three separate calls would pass with
        `record_conclusive` deleted: the streak would reset because it
        was thrown away, not because a count succeeded. That is the
        vacuity this class exists to avoid, so the fake `gh` counts its
        own invocations and succeeds on the third.
        """
        counter_file = tmp_path / "gh_calls"
        monkeypatch.setenv("FAKE_GH_COUNT", str(counter_file))
        payload = tmp_path / "ok.json"
        payload.write_text("[]", encoding="utf-8")
        monkeypatch.setenv("FAKE_GH_JSON", str(payload))
        _put_gh_on_path(tmp_path, monkeypatch, _FAKE_GH_THIRD_CALL_WORKS)
        _add(_queue(tmp_path))

        results = self._run(tmp_path, 5)

        assert counter_file.read_text(encoding="utf-8").strip() == "5"
        reasons = [r.skipped for r in results]
        assert [("cannot count" in reason) for reason in reasons] == [
            True,
            True,
            False,
            True,
            True,
        ], reasons
        assert self._open_items(tmp_path) == [], "the streak did not reset on a good count"

    def test_five_failures_in_one_loop_do_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same five polls with no good count in the middle DO file.

        Without this, the reset test above passes whenever nothing files
        for any reason at all.
        """
        _install_marker_gh(tmp_path, monkeypatch)
        _add(_queue(tmp_path))

        self._run(tmp_path, 5)

        assert len(self._open_items(tmp_path)) == 1


# ---------------------------------------------------------------------------
# The dry-run listing
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_lists_the_gate_last(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--dry-run` lists gates in evaluation order; this one is last."""
        _install_fake_gh(tmp_path, monkeypatch, [])

        result = CliRunner().invoke(
            cli,
            ["serve", "--dry-run", "--root", str(tmp_path), "--no-color"],
        )

        assert result.exit_code == 0
        lines = result.output.splitlines()
        inbox = next(i for i, line in enumerate(lines) if "gate inbox cap" in line)
        bound = next(i for i, line in enumerate(lines) if "gate open-PR bound" in line)
        assert bound > inbox
        assert "gate open-PR bound: ok" in result.output

    def test_dry_run_reports_the_gate_blocking(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_gh(tmp_path, monkeypatch, [_marked(1)])

        result = CliRunner().invoke(
            cli,
            ["serve", "--dry-run", "--root", str(tmp_path), "--no-color"],
        )

        assert result.exit_code == 0
        assert "gate open-PR bound: BLOCKS - 1 kstrl PR(s) open (bound 1)" in result.output


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_env_beats_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[serve]\nmax_open_prs = 3\n", encoding="utf-8")
        assert ServeConfig.load(tmp_path).max_open_prs == 3

        monkeypatch.setenv("KSTRL_SERVE_MAX_OPEN_PRS", "0")
        assert ServeConfig.load(tmp_path).max_open_prs == 0
        assert ServeConfig.from_env().max_open_prs == 0

    def test_a_negative_bound_in_toml_is_a_config_error(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[serve]\nmax_open_prs = -1\n", encoding="utf-8")
        with pytest.raises(ServeError, match="max_open_prs must be >= 0"):
            ServeConfig.load(tmp_path)
