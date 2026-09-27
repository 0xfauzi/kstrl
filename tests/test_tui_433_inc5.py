"""#433 increment 5: wrapped lines stay under their label, and the text says what happens.

Each test names the round-5 finding it pins (H1-H12 in the #433 round-5
audit). Every one drives the real app through Pilot over state written by
kstrl's own writers (``EventBus``, ``Manifest``, ``Inbox``, ``Queue``,
``write_launch_record``), except the one that drives the real pipeline's
checkpoint for the option labels it offers. A screen whose content is a
Rich ``Group`` is read with ``tests/helpers/rendered.py``: ``shown`` for
where a wrapped line starts, ``flat`` for a phrase.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import Button, DataTable, Static

from kstrl import events as ev
from kstrl.config_report import build_config_report
from kstrl.findings import Finding
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.interaction import CheckpointContext, PromptKind, PromptRequest
from kstrl.manifest import Component, ComponentStatus, Manifest, park_dedupe_key
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.checkpoint import CheckpointModal
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.config import ConfigScreen
from kstrl.tui.screens.inbox import InboxScreen
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.screens.retry import RetryScreen
from kstrl.tui.theme import short_run_id
from tests.helpers.fake_run import FakeRunSpec, write_fake_run
from tests.helpers.rendered import flat, shown
from tests.helpers.settle import mounted, settled
from tests.test_launch_session import FakeSession
from tests.test_tui_433_inc3 import RUN_ID, _dash, _failed_run
from tests.test_tui_433_inc4 import FAILED_RUN, LIMIT_ENV, _backdate, _failed_manifest

SIZES = [(120, 36), (80, 24)]
#: The retry scope's labels, in ``retry_scope``'s order.
SCOPE_LABELS = {
    "starts at",
    "resets",
    "stays out",
    "worktree",
    "branch",
    "keeps",
    "runs under",
    "replays",
    "not replayed",
    "based on",
}
DROPPED = "--verify-command is no longer an option. The command it named never ran."
OLD_MANIFEST = "/Users/someone/old-place/demo-project/scripts/kstrl/manifest.json"


def _home(root: Path) -> KstrlTuiApp:
    return KstrlTuiApp(root_dir=root, mode=Mode.HOME, poll_interval=0.05)


@pytest.fixture
def no_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIMIT_ENV:
        monkeypatch.delenv(name, raising=False)


def _assert_hanging(lines: list[str], first: str, labels: set[str]) -> None:
    """From the line that starts with ``first``: every line is a label then
    its value, or blank up to the value column. None starts flush left."""
    start = next(i for i, line in enumerate(lines) if line.lstrip().startswith(first))
    rows = lines[start:]
    indent = len(rows[0]) - len(rows[0].lstrip())
    column = len(rows[0]) - len(rows[0][indent + len(first) :].lstrip())
    for line in rows:
        if not line.strip():
            break
        head = line[:column].strip()
        assert head in labels or head == "", (head, lines)
        assert line[column : column + 1] != " " or not line[column:].strip(), lines


async def _open_retry(app: KstrlTuiApp, pilot: Any) -> Static:
    await mounted(pilot, lambda: app.screen, "#home-runs")
    app.push_screen(RetryScreen())
    return cast(Static, await mounted(pilot, lambda: app.screen, "#retry-detail"))


class TestRetryScope:
    @pytest.mark.parametrize("size", SIZES)
    async def test_the_scope_wraps_under_its_values_and_names_no_internal(
        self, tmp_path: Path, no_limit_env: None, size: tuple[int, int]
    ) -> None:
        """H1, H2, H3: the confirmation named preview_retry and #539, and at
        80x24 its lines and the pane's wrapped back to the left edge."""
        _failed_manifest(tmp_path, (("verify_command", "uv run pytest"),))
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            detail = await _open_retry(app, pilot)
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered once the carry is read",
            )
            await pilot.press("r")
            body = await mounted(pilot, lambda: app.screen, "#options-detail Static")
            modal = cast(OptionsModal, app.screen)
            await settled(pilot, lambda: body.content_region.width, what="the scope laid out")
            text = flat(body)
            assert "preview_retry" not in text and "#539" not in text, text
            assert re.search(r"not replayed\s+" + re.escape(DROPPED), text), text
            assert re.search(r"based on\s+the manifest as it is now", text), text
            _assert_hanging(shown(body), "starts at", SCOPE_LABELS)
            for button in modal.query(Button):
                assert 0 <= button.region.y < size[1], (button.label, button.region)
            await pilot.press("escape")
            await settled(pilot, lambda: "retry scope" in flat(detail), what="the scope pane")
            _assert_hanging(shown(detail), "starts at", SCOPE_LABELS)

    async def test_a_scope_taller_than_the_screen_scrolls_under_the_question(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """H3: the confirmation's detail is capped, so a long scope scrolls
        and neither the question nor the buttons leave an 80x24 screen."""
        _failed_manifest(tmp_path, (("verify_command", "uv run pytest"),))
        manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest = Manifest.load(manifest_file)
        for n in range(8):
            manifest.components.append(
                Component(
                    id=f"comp-x{n}",
                    title="X",
                    description="",
                    dependencies=[],
                    prd_path="p.json",
                    branch_name=f"kstrl/comp-x{n}",
                    status=ComponentStatus.FAILED.value,
                )
            )
        manifest.save(manifest_file)
        app = _home(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_retry(app, pilot)
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered once the carry is read",
            )
            await pilot.press("r")
            body = await mounted(pilot, lambda: app.screen, "#options-detail Static")
            await settled(pilot, lambda: body.content_region.width, what="the scope laid out")
            scroll = app.screen.query_one("#options-detail")
            assert scroll.virtual_size.height > scroll.region.height, (
                scroll.virtual_size,
                scroll.region,
            )
            question = app.screen.query_one("#options-question")
            assert 0 <= question.region.y < 24, question.region
            for button in app.screen.query(Button):
                assert 0 <= button.region.y < 24, (button.label, button.region)

    async def test_the_confirmation_is_the_scope_the_pane_shows(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """H3: one table for both, so the two cannot say different things."""
        _failed_manifest(tmp_path, ())
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            detail = await _open_retry(app, pilot)
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered once the carry is read",
            )
            await pilot.press("r")
            body = await mounted(pilot, lambda: app.screen, "#options-detail Static")
            confirmed = [line.strip() for line in flat(body).splitlines()]
            await pilot.press("escape")
            await settled(pilot, lambda: "retry scope" in flat(detail), what="the scope pane")
            pane = [line.strip() for line in flat(detail).splitlines()]
            start = pane.index(confirmed[0])
            assert pane[start : start + len(confirmed)] == confirmed, (confirmed, pane)


#: A recorded flag FactoryLaunch has a field for, its spelling, and its value.
CARRIED = [
    ("max_parallel", "--max-parallel 1", 1),
    ("review_mode", "--review-mode advisory", "advisory"),
]


class TestRecordedFlags:
    @pytest.mark.parametrize("size", SIZES)
    @pytest.mark.parametrize(("name", "spelled", "value"), CARRIED)
    async def test_a_recorded_flag_factory_launch_carries_reaches_the_relaunch(
        self,
        tmp_path: Path,
        no_limit_env: None,
        size: tuple[int, int],
        name: str,
        spelled: str,
        value: object,
    ) -> None:
        """H5: FactoryLaunch has a field for --max-parallel and --review-mode,
        and the screen refused the retry anyway."""
        _failed_manifest(tmp_path, ((name, value),))
        app = _home(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)  # type: ignore[method-assign]
        async with app.run_test(size=size) as pilot:
            await _open_retry(app, pilot)
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered for a run recorded with --max-parallel 1",
            )
            await pilot.press("r")
            body = await mounted(pilot, lambda: app.screen, "#options-detail Static")
            text = flat(body)
            if name == "max_parallel":
                # Stated once, under "runs under" (#433 K6).
                assert re.search(r"runs under\s+.*, 1 in parallel", text), text
                assert "replays" not in text, text
            else:
                assert re.search(r"replays\s+" + re.escape(spelled) + "\n", text + "\n"), text
            await pilot.press("1")
            await settled(pilot, lambda: specs, what="the confirmation to relaunch")
        assert getattr(specs[0], name) == value, specs
        manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
        assert specs[0].manifest_path == manifest_file
        comp = Manifest.load(manifest_file).get_component("comp-a")
        assert comp is not None and comp.status == ComponentStatus.PENDING.value

    async def test_a_recorded_flag_this_screen_cannot_pass_is_named(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """H5: the refusal said "the recorded run's flags" and named none."""
        _failed_manifest(tmp_path, (("max_parallel", 1), ("max_retries", 5)))
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            detail = await _open_retry(app, pilot)
            await settled(pilot, lambda: "ks retry comp-a" in flat(detail), what="the CLI route")
            text = flat(detail)
            run = short_run_id(FAILED_RUN)
            said = f"run {run} was launched with --max-retries 5, which this screen cannot pass on"
            assert said in text, text
            assert "--max-parallel" not in text, text
            assert app.screen.check_action("retry_selected", ()) is False


class TestMovedProject:
    @pytest.mark.parametrize("size", SIZES)
    async def test_the_cause_comes_first_and_each_path_is_whole_on_its_row(
        self, tmp_path: Path, no_limit_env: None, size: tuple[int, int]
    ) -> None:
        """H4: three absolute paths wrapped mid-word ahead of any cause."""
        _failed_manifest(tmp_path, ())
        manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
        record = tmp_path / ".kstrl" / "runs" / FAILED_RUN / "launch.json"
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["manifest"] = OLD_MANIFEST
        record.write_text(json.dumps(payload), encoding="utf-8")
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            detail = await _open_retry(app, pilot)
            await settled(pilot, lambda: "retry blocked" in flat(detail), what="the refusal")
            text = flat(detail)
            cause = "the launch record names a manifest at another path: this project moved"
            assert text.index(cause) < text.index(OLD_MANIFEST), text
            assert re.search(r"it names\s+" + re.escape(OLD_MANIFEST), text), text
            here = str(manifest_file.resolve())
            assert re.search(r"this project's\s+" + re.escape(here), text), text
            assert re.search(r"launch record\s+" + re.escape(str(record)), text), text
            # `ks retry` refuses the same record, so no command is offered.
            assert "ks retry comp-a" not in text, text
            table = cast(DataTable[Any], app.screen.query_one("#retry-table"))
            assert str(table.get_row_at(0)[-1]) == "retry blocked"
            assert app.screen.check_action("retry_selected", ()) is False
            lines = shown(detail)
            below = lines[next(i for i, ln in enumerate(lines) if ln == "retry blocked") + 1 :]
            assert below and all(line.startswith("  ") for line in below if line), lines


def _refused_run(root: Path) -> None:
    """A factory run that refused to start (the log block the factory writes
    for stale branches) and wrote no finish record two hours ago: unknown."""
    run_id = "factory-20260926-060000.000000-refu01"
    paths = ev.RunPaths.for_run(root, run_id)
    bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=run_id)
    bus.emit(ev.RunStarted(project="demo", components=1))
    bus.emit(ev.RunPlan(components=({"id": "api", "title": "API", "deps": []},)))
    bus.emit(ev.Log(severity="error", text="Refusing to run: stale component branches found"))
    bus.emit(
        ev.Log(
            severity="error",
            text="  branch 'kstrl/factory/client-http' (component 'client-http') already exists "
            "with commits not merged into 'main'; refusing to silently reuse it. Merge it or "
            "delete it (git branch -D kstrl/factory/client-http) and re-run.",
        )
    )
    bus.close()
    _backdate(paths.events_file, 7200)


class TestHistoryNote:
    @pytest.mark.parametrize("size", SIZES)
    async def test_a_cut_note_is_whole_below_and_the_title_says_so(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """H6: "Refusing to run: stale com…" with no way to read the rest; the
        line under the table held three lines and cut the remedy."""
        _refused_run(tmp_path)
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            runs = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#home-runs"))
            meta = cast(Static, app.screen.query_one("#home-preview-meta"))
            await settled(
                pilot,
                lambda: "and re-run." in flat(meta),
                what="the selected run's reason under the table",
            )
            title = flat(app.screen.query_one("#home-runs-title"))
            note = str(runs.get_row_at(0)[-1])
            # At 120 columns this one run's note has room; at 80 it is cut.
            assert note.endswith("…") or size[0] > 80, note
            cue = "a cut note is whole below when its row is selected"
            assert (cue in title) == note.endswith("…"), (title, note)
            await settled(
                pilot,
                lambda: meta.region.height == len(shown(meta)),
                what="the reason laid out at its own height",
            )
            assert shown(meta)[-1].rstrip().endswith(("components", "board")), shown(meta)


class TestInbox:
    @pytest.mark.parametrize("size", SIZES)
    async def test_a_park_is_said_in_plain_words_and_the_choices_wrap_under_themselves(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """H7: "pause_before_pr_merge is on" and backticks around a command,
        and each choice's sentence wrapped back to the left edge."""
        from kstrl.pipeline import PARK_DETAIL

        _failed_manifest(tmp_path, ())
        manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest = Manifest.load(manifest_file)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        comp.status = ComponentStatus.AWAITING_APPROVAL.value
        manifest.save(manifest_file)
        Inbox(tmp_path, InboxConfig()).add(
            ItemKind.MERGE_GATE,
            "comp-a awaiting merge approval",
            detail=PARK_DETAIL,
            component="comp-a",
            dedupe_key=park_dedupe_key("comp-a"),
            evidence={"head_sha": "87c3e2efbe2c"},
        )
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(InboxScreen())
            detail = cast(Static, await mounted(pilot, lambda: app.screen, "#inbox-detail"))
            await settled(pilot, lambda: "what each choice does" in flat(detail), what="choices")
            text = flat(detail)
            assert "`" not in text and "pause_before_pr_merge" not in text, text
            assert "Merge approval is required" in text, text
            lines = shown(detail)
            start = lines.index("what each choice does")
            assert all(line.startswith("  ") for line in lines[start + 1 :] if line), lines


def _checkpoint_request() -> PromptRequest:
    """What ``Pipeline._phase_checkpoint`` asks, with two findings long enough to wrap."""
    return PromptRequest(
        kind=PromptKind.CHECKPOINT,
        header="Approve PR creation and merge for comp-c?",
        options=(
            "Approve",
            "Reject (fail component, skip dependents)",
            "Retry (consume a retry, re-run component)",
        ),
        default=0,
        component_id="comp-c",
        checkpoint=CheckpointContext(
            component_id="comp-c",
            review_findings=(
                Finding(
                    phase="review",
                    category="error_handling",
                    severity="advisory",
                    location="src/snippetvault/cli.py:621-634",
                    explanation="A connection refused error prints a traceback instead of "
                    "the exit-2 message the PRD names.",
                ),
                Finding(
                    phase="review",
                    category="test_quality",
                    severity="advisory",
                    location="tests/test_cli_client.py:1923-1929",
                    explanation="The test asserts the exit code only; the stderr text the "
                    "criterion pins is never checked.",
                ),
            ),
            branch="kstrl/factory/comp-c",
        ),
    )


class TestCheckpoint:
    @pytest.mark.parametrize("size", SIZES)
    async def test_findings_wrap_under_their_text_and_each_choice_says_what_it_does(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """H8: a wrapped finding ran on under its severity tag. H9: Reject and
        Retry stated no consequence before the operator chose."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1, complete=False))
        app = _dash(tmp_path, run_dir)
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#component-table")
            app.push_screen(CheckpointModal(_checkpoint_request()))
            effects = cast(Static, await mounted(pilot, lambda: app.screen, "#checkpoint-effects"))
            findings = cast(Static, app.screen.query("#checkpoint-body Static").first())
            await settled(pilot, lambda: findings.content_region.width, what="findings laid out")
            lines = shown(findings)
            column = lines[1].index("src/")
            for line in lines[2:]:
                if line.strip() and "[advisory]" not in line:
                    assert line[:column].strip() == "" and line[column] != " ", lines
            text = flat(effects)
            assert re.search(r"Reject\s+comp-c fails and its dependents are skipped", text), text
            assert "nothing is pushed" in text, text
            assert re.search(r"Retry\s+the engineer runs comp-c again", text), text
            assert "Uses one retry; with none left, comp-c fails as on Reject." in text, text
            assert re.search(r"Approve\s+pushes kstrl/factory/comp-c", text), text
            assert "or unpushed without gh." in text, text
            for button in app.screen.query(Button):
                assert 0 <= button.region.y < size[1], (button.label, button.region)

    def test_every_answer_the_pipeline_offers_has_its_effect_stated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H9's guard: the effects are keyed by the pipeline's option labels,
        so a renamed option would drop its effect with nothing failing."""
        from kstrl.tui.screens.checkpoint import CHOICE_EFFECTS
        from tests.test_pipeline import _ChoiceUI, _factory_config, _make_pipeline, _success

        # tests/test_pipeline.py's autouse stubs: no git diff, no real agent.
        monkeypatch.setattr("kstrl.git.get_diff_content", lambda *a, **k: "diff --git a b\n")
        monkeypatch.setattr("kstrl.agents.get_agent", lambda *a, **k: object())
        asked: list[list[str]] = []

        class Recording(_ChoiceUI):
            def choose(self, header: str, options: list[str], default: int = 0) -> int:
                asked.append(list(options))
                return 1

        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(create_prs=True, pause_before_pr_merge=True),
            ui=Recording(choice=1),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        pipeline.process_result("comp-a", _success("comp-a"))
        assert [option.split(" (")[0] for option in asked[0]] == list(CHOICE_EFFECTS), asked


def _live_run_with_serve_item(
    root: Path, moving: bool, title: str = "slice three"
) -> tuple[Path, Any]:
    """A live factory run, whose comp-b is writing its transcript when
    ``moving``, and a ks serve item leased by this process, which holds
    the factory lock."""
    from kstrl.workqueue import Queue

    run_dir = write_fake_run(root, FakeRunSpec(components=2, complete=False))
    if moving:
        bus = ev.EventBus(ev.JsonlSink(run_dir / "events.jsonl"), run_id=run_dir.name)
        bus.emit(ev.ComponentStarted(component="comp-b"))
        bus.emit(ev.WorkerHeartbeat(component="comp-b", pid=os.getpid()))
    log = run_dir / "components" / "comp-b" / "engineer.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("working\n", encoding="utf-8")
    queue = Queue(root)
    queue.start(queue.lease(queue.add("spec", title=title), pid=os.getpid()))
    lock = (root / ".kstrl" / "factory.lock").open("a+", encoding="utf-8")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return run_dir, lock


class TestServeRow:
    @pytest.mark.parametrize("size", SIZES)
    @pytest.mark.parametrize(
        ("moving", "said"), [(True, r"output \d+s ago"), (False, "no output recorded")]
    )
    async def test_a_running_serve_item_shows_its_runs_output_age(
        self, tmp_path: Path, size: tuple[int, int], moving: bool, said: str
    ) -> None:
        """H10: the factory row said "output 10s ago"; the ks serve row that
        runs the same run said nothing about output. With no component
        moving there is no age to show, and the row says so."""
        from kstrl.serve import serve_lock

        run_dir, lock = _live_run_with_serve_item(tmp_path, moving)
        try:
            with serve_lock(tmp_path):
                app = _home(tmp_path)
                async with app.run_test(size=size) as pilot:
                    active = cast(
                        DataTable[Any], await mounted(pilot, lambda: app.screen, "#home-active")
                    )
                    await settled(
                        pilot,
                        lambda: (
                            active.row_count == 2 and "folding" not in str(active.get_row_at(0)[3])
                        ),
                        what="the factory row and the serve row",
                    )
                    serve = str(active.get_row_at(1)[3])
                    run = short_run_id(run_dir.name)
                    assert re.search(rf"run {run} · {said}", serve), serve
        finally:
            lock.close()


class TestConfig:
    @pytest.mark.parametrize("size", SIZES)
    async def test_a_cut_value_says_it_is_whole_below(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """H11: paths.progress ended mid-description with no cue that
        selecting the row shows the whole value."""
        app = KstrlTuiApp(
            root_dir=tmp_path,
            mode=Mode.HOME,
            poll_interval=0.05,
            config_report=build_config_report(tmp_path),
        )
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(ConfigScreen())
            table = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#config-table"))
            title = cast(Static, app.screen.query_one("#config-title"))
            await settled(pilot, lambda: table.row_count, what="the config rows")
            cut = sum("…" in str(table.get_row_at(i)[2]) for i in range(table.row_count))
            assert cut, "no value is long enough to be cut; the test needs one"
            said = flat(title)
            assert f"{cut} cut to fit, whole below when selected" in said, said
            assert title.region.width >= len(said.splitlines()[0]), (title.region, said)


class TestComponentDetail:
    @pytest.mark.parametrize("size", SIZES)
    async def test_a_failed_gate_says_where_the_retry_is(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """H12: the failed gate's detail had no route to the retry."""
        _failed_run(tmp_path)
        app = _dash(tmp_path, tmp_path / ".kstrl" / "runs" / RUN_ID)
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#component-table")
            app.push_screen(ComponentScreen("comp-a"))
            failure = cast(Static, await mounted(pilot, lambda: app.screen, "#failure-detail"))
            await settled(pilot, lambda: failure.display, what="the failed gate")
            lines = flat(failure).splitlines()
            route = "3 on home opens the failure queue; ks retry comp-a runs one from a shell"
            assert re.fullmatch(r"\s+retry\s+" + re.escape(route), lines[1]), lines
            # Under the failure line, so no gate output can push it out of the pane.
            assert failure.region.height > len(shown(failure)[:3]), failure.region
