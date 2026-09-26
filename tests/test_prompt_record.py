"""#532: every agent call a run makes leaves the exact prompt it was given.

The agent in these tests is a real subprocess run through the real adapter:
a shell command that saves every stdin it receives to its own file. So the
set of prompts the agent CLI actually received is known exactly, and each
test compares it with what kstrl recorded, rather than with what kstrl
meant to send. Fake ``claude`` and ``codex`` binaries go first on PATH in
every test that drives a whole command, so no adapter can reach a real,
paid CLI; each fake writes its name to a sentinel file, and the tests
assert the sentinel was never written.
"""

from __future__ import annotations

import errno
import io
import json
import os
import shlex
from collections import Counter
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.agents import prompt_record
from kstrl.agents.base import (
    ARCHITECT_COMPONENT,
    ARCHITECT_ROLE,
    INTEGRATION_COMPONENT,
    INTEGRATION_ROLE,
)
from kstrl.agents.claude_code import ClaudeCodeAgent
from kstrl.agents.claude_sdk import ClaudeSdkAgent
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.agents.prompt_record import (
    AgentCall,
    PromptRecord,
    PromptRecordError,
    read_prompt_records,
    record_prompt,
    recording_prompts,
)
from kstrl.atomicio import atomic_write_json
from kstrl.cli import cli
from kstrl.init_cmd import gitignore_block, run_init
from kstrl.ui.plain import PlainUI
from kstrl.version import kstrl_version
from tests.helpers import gitrepo
from tests.helpers import integration_harness as h
from tests.helpers.executables import write_executable
from tests.spine_utils import git

COMPLETE = "<promise>COMPLETE</promise>"

PRD = {
    "branchName": "kstrl/p532",
    "userStories": [
        {
            "id": "US-001",
            "title": "Do the thing",
            "acceptanceCriteria": ["it is done"],
            "priority": 1,
            "passes": False,
            "notes": "",
        }
    ],
}


def _capturing_agent(capdir: Path, reply: str = COMPLETE, *, commit: bool = True) -> str:
    """A shell agent that saves each stdin to its own file, then prints ``reply``.

    With ``commit`` it commits one file the first time it runs, so the
    review phases have a diff to read. That is the only side effect.
    """
    capdir.mkdir(parents=True, exist_ok=True)
    work = (
        "[ -f work.txt ] || { echo x > work.txt; git add work.txt; "
        "git commit -qm work >/dev/null 2>&1; }; "
        if commit
        else ""
    )
    return (
        f'f=$(mktemp {shlex.quote(str(capdir))}/call.XXXXXX); cat > "$f"; '
        f"{work}printf '%s\\n' {shlex.quote(reply)}"
    )


def _fails_its_first_call(capdir: Path, marker: Path) -> str:
    """Like :func:`_capturing_agent`, but the FIRST call answers without the
    completion token, so a one-iteration engineer attempt fails and the
    factory retries the component."""
    capdir.mkdir(parents=True, exist_ok=True)
    return (
        f'f=$(mktemp {shlex.quote(str(capdir))}/call.XXXXXX); cat > "$f"; '
        f"if [ ! -f {shlex.quote(str(marker))} ]; then "
        f"touch {shlex.quote(str(marker))}; echo not-done; exit 0; fi; "
        "[ -f work.txt ] || { echo x > work.txt; git add work.txt; "
        "git commit -qm work >/dev/null 2>&1; }; "
        f"printf '%s\\n' {shlex.quote(COMPLETE)}"
    )


def _no_space(role: str | None = None) -> object:
    """An ``atomic_write_json`` stand-in that fails the way a full disk does,
    for every record or only for ``role``'s."""

    def write(path: Path, payload: dict[str, object]) -> None:
        if role is None or payload["role"] == role:
            raise OSError(errno.ENOSPC, "No space left on device (planted)")
        atomic_write_json(path, payload)

    return write


def _delivered(capdir: Path) -> Counter[str]:
    return Counter(p.read_text(encoding="utf-8") for p in capdir.iterdir())


def _no_paid_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake ``claude`` and ``codex`` first on PATH; returns their sentinel."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    sentinel = tmp_path / "paid-cli-called"
    for name in ("claude", "codex"):
        write_executable(bindir / name, f"#!/bin/sh\necho {name} >> '{sentinel}'\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("KSTRL_AGENT_PROBE", "0")
    return sentinel


def _only_run(root: Path) -> Path:
    runs = [d for d in (root / ".kstrl" / "runs").iterdir() if d.is_dir()]
    assert len(runs) == 1, runs
    return runs[0]


def _all_records(run_root: Path) -> list[PromptRecord]:
    """Every record in the run, read through the production reader."""
    components = sorted(d.name for d in (run_root / "prompts").iterdir())
    return [
        record
        for component in components
        for record in read_prompt_records(run_root, run_id=run_root.name, component=component)
    ]


def _initialised_project(tmp_path: Path, *, review: bool = True) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "README.md", cwd=root)
    git("commit", "-q", "-m", "seed", cwd=root)
    assert run_init(root, PlainUI(no_color=True, file=io.StringIO())) == 0
    (root / "scripts" / "kstrl" / "prd.json").write_text(json.dumps(PRD), encoding="utf-8")
    if review:
        toml = root / "kstrl.toml"
        text = toml.read_text(encoding="utf-8")
        assert text.count("[security]\n") == 1 and "# review_mode =" in text
        text = text.replace("# review_mode =", 'review_mode = "advisory"\n# review_mode =', 1)
        text = text.replace("[security]\n", '[security]\nmode = "advisory"\n', 1)
        toml.write_text(text, encoding="utf-8")
    return root


def _ks_run(root: Path, agent: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        cli, ["run", "1", "--sleep", "0", "--no-verify", "--branch", "", "--agent-cmd", agent]
    )
    return result.output


class TestAFactoryRunRecordsEveryPrompt:
    def test_each_role_s_record_is_the_exact_prompt_the_cli_received(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _no_paid_cli(tmp_path, monkeypatch)
        root = _initialised_project(tmp_path)
        capdir = tmp_path / "captured"

        output = _ks_run(root, _capturing_agent(capdir), monkeypatch)

        run_root = _only_run(root)
        records = _all_records(run_root)
        assert not sentinel.exists(), sentinel.read_text(encoding="utf-8")
        assert Counter(r.prompt for r in records) == _delivered(capdir), output
        assert sorted(r.role for r in records) == ["distill", "engineer", "review", "security"]
        assert {(r.run_id, r.attempt, r.call, r.agent_cli) for r in records} == {
            (run_root.name, 1, 1, "custom")
        }

    def test_records_survive_the_progress_log_opt_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record is evidence a scorer reads, like the usage accounting,
        so turning the progress log off does not turn it off."""
        _no_paid_cli(tmp_path, monkeypatch)
        monkeypatch.setenv("KSTRL_FACTORY_PROGRESS_LOG_ENABLED", "0")
        root = _initialised_project(tmp_path, review=False)
        capdir = tmp_path / "captured"

        output = _ks_run(root, _capturing_agent(capdir), monkeypatch)

        run_root = _only_run(root)
        assert not (run_root / "events.jsonl").exists()
        records = _all_records(run_root)
        assert Counter(r.prompt for r in records) == _delivered(capdir), output
        assert "engineer" in {r.role for r in records}

    def test_a_retried_component_records_each_attempt_under_its_own_number(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attempt 1's engineer does not finish; attempt 2 does and is
        reviewed. The records say which attempt each prompt belonged to."""
        sentinel = _no_paid_cli(tmp_path, monkeypatch)
        root = _initialised_project(tmp_path)
        capdir = tmp_path / "captured"

        output = _ks_run(root, _fails_its_first_call(capdir, tmp_path / "marker"), monkeypatch)

        run_root = _only_run(root)
        records = _all_records(run_root)
        assert not sentinel.exists()
        assert Counter(r.prompt for r in records) == _delivered(capdir), output
        assert sorted((r.role, r.attempt, r.call) for r in records) == [
            ("distill", 2, 1),
            ("engineer", 1, 1),
            ("engineer", 2, 1),
            ("review", 2, 1),
            ("security", 2, 1),
        ], output

    def test_an_engineer_whose_record_cannot_be_written_fails_before_the_cli_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed record write fails the call: the run reports the
        error, the component fails, and the agent CLI never ran."""
        sentinel = _no_paid_cli(tmp_path, monkeypatch)
        monkeypatch.setenv("FACTORY_MAX_RETRIES", "0")
        monkeypatch.setattr(prompt_record, "atomic_write_json", _no_space())
        root = _initialised_project(tmp_path)
        capdir = tmp_path / "captured"
        monkeypatch.chdir(root)

        result = CliRunner().invoke(
            cli,
            [
                "run",
                "1",
                "--sleep",
                "0",
                "--no-verify",
                "--branch",
                "",
                "--agent-cmd",
                _capturing_agent(capdir),
            ],
        )

        assert not sentinel.exists()
        assert list(capdir.iterdir()) == [], result.output
        assert result.exit_code != 0, result.output
        assert "No space left on device (planted)" in result.output

    def test_a_reviewer_whose_record_cannot_be_written_is_reported_and_never_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the review record fails: the review phase reports the
        reviewer failed, and the review prompt never reached the CLI."""
        _no_paid_cli(tmp_path, monkeypatch)
        monkeypatch.setenv("FACTORY_MAX_RETRIES", "0")
        monkeypatch.setattr(prompt_record, "atomic_write_json", _no_space("review"))
        root = _initialised_project(tmp_path)
        capdir = tmp_path / "captured"

        output = _ks_run(root, _capturing_agent(capdir), monkeypatch)

        records = _all_records(_only_run(root))
        assert "No space left on device (planted)" in output
        assert "review" not in {r.role for r in records}
        assert Counter(r.prompt for r in records) == _delivered(capdir), output
        assert not any("hostile senior reviewer" in p for p in _delivered(capdir))


class TestCommandRunsRecordTheirPrompts:
    def test_the_architect_records_one_prompt_per_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ks decompose` with an architect that never answers in JSON: three
        attempts, three records, the retry prompts carrying the error."""
        sentinel = _no_paid_cli(tmp_path, monkeypatch)
        root = tmp_path / "project"
        root.mkdir()
        git("init", "-q", "-b", "main", cwd=root)
        gitrepo.set_identity(root)
        (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
        (root / ".gitignore").write_text(gitignore_block("Python"), encoding="utf-8")
        (root / "spec.md").write_text("# Spec\n\nBuild a thing.\n", encoding="utf-8")
        git("add", "-A", cwd=root)
        git("commit", "-q", "-m", "seed", cwd=root)
        capdir = tmp_path / "captured"

        result = CliRunner().invoke(
            cli,
            [
                "decompose",
                "--spec",
                str(root / "spec.md"),
                "--project-name",
                "demo",
                "--root",
                str(root),
                "--agent-cmd",
                _capturing_agent(capdir, reply="not json"),
                "--ui",
                "plain",
                "--no-color",
            ],
        )

        run_root = _only_run(root)
        records = read_prompt_records(run_root, run_id=run_root.name, component=ARCHITECT_COMPONENT)
        assert not sentinel.exists()
        assert Counter(r.prompt for r in records) == _delivered(capdir), result.output
        assert [(r.role, r.attempt, r.call) for r in records] == [
            (ARCHITECT_ROLE, n, 1) for n in (1, 2, 3)
        ]
        assert "PREVIOUS ATTEMPT FAILED" not in records[0].prompt
        assert "PREVIOUS ATTEMPT FAILED" in records[2].prompt

    def test_understand_records_its_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _no_paid_cli(tmp_path, monkeypatch)
        root = _initialised_project(tmp_path, review=False)
        capdir = tmp_path / "captured"

        result = CliRunner().invoke(
            cli,
            ["understand", "--root", str(root), "--agent-cmd", _capturing_agent(capdir)],
        )

        run_root = _only_run(root)
        records = read_prompt_records(run_root, run_id=run_root.name, component="understand")
        assert not sentinel.exists()
        assert Counter(r.prompt for r in records) == _delivered(capdir), result.output
        assert {(r.role, r.attempt) for r in records} == {("understand", 1)}

    def test_feature_records_each_phase_s_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ks feature` end to end: the understand loop, then the implement
        loop, each a real run of the agent command."""
        sentinel = _no_paid_cli(tmp_path, monkeypatch)
        root = _initialised_project(tmp_path, review=False)
        noop = 'test_command = "exit 0"\ntypecheck_command = "exit 0"\nlint_command = "exit 0"\n'
        toml = root / "kstrl.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8").replace("[verify]\n", "[verify]\n" + noop, 1),
            encoding="utf-8",
        )
        feature_dir = root / "scripts" / "kstrl" / "feature" / "demo"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps(PRD), encoding="utf-8")
        (root / "scripts" / "kstrl" / "codebase_map.md").write_text("# map\n", encoding="utf-8")
        capdir = tmp_path / "captured"

        result = CliRunner().invoke(
            cli,
            [
                "feature",
                "--root",
                str(root),
                "--prd",
                "scripts/kstrl/feature/demo/prd.json",
                "--understand-iterations",
                "1",
                "--branch",
                "",
                "--implementation-auto-run",
                "--agent-cmd",
                _capturing_agent(capdir, commit=False),
            ],
        )

        run_root = _only_run(root)
        records = read_prompt_records(run_root, run_id=run_root.name, component="demo")
        assert not sentinel.exists()
        assert Counter(r.prompt for r in records) == _delivered(capdir), result.output
        assert [(r.role, r.attempt) for r in records] == [("implement", 1), ("understand", 1)], (
            result.output
        )


class TestTheIntegrationReviewRecordsItsPrompt:
    def test_the_integration_reviewer_s_prompt_is_recorded(self, tmp_path: Path) -> None:
        """The real integration round over a merged feature, with the
        reviewer a real subprocess adapter instead of the harness fake."""
        root = tmp_path / "repo"
        base, _head = h.merged_feature(root)
        reply = tmp_path / "reply.json"
        reply.write_text(json.dumps(h.review_payload(root, base)), encoding="utf-8")
        capdir = tmp_path / "captured"
        capdir.mkdir()
        reviewer = CustomAgent(
            f'f=$(mktemp {shlex.quote(str(capdir))}/call.XXXXXX); cat > "$f"; '
            f"cat {shlex.quote(str(reply))}"
        )

        h.run_factory_over(root, reviewer)  # type: ignore[arg-type]

        run_root = _only_run(root)
        records = read_prompt_records(
            run_root, run_id=run_root.name, component=INTEGRATION_COMPONENT
        )
        assert Counter(r.prompt for r in records) == _delivered(capdir)
        assert [(r.role, r.attempt, r.agent_cli) for r in records] == [
            (INTEGRATION_ROLE, 1, "custom")
        ]


def _call(tmp_path: Path, **overrides: object) -> AgentCall:
    fields: dict[str, object] = {
        "run_root": tmp_path / ".kstrl" / "runs" / "factory-x",
        "run_id": "factory-x",
        "component": "comp-a",
        "role": "engineer",
        "attempt": 2,
    }
    fields.update(overrides)
    return AgentCall(**fields)  # type: ignore[arg-type]


def _adapter(kind: str, capture: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
    """Each adapter with a fake CLI that saves its stdin to ``capture``."""
    bindir = tmp_path / "adapterbin"
    bindir.mkdir(exist_ok=True)
    # `codex exec --help` is the adapter's capability probe, not a prompt.
    save = f"#!/bin/sh\ncase \"$*\" in *--help*) exit 0 ;; esac\ncat > '{capture}'\n"
    write_executable(bindir / "claude", save + 'echo \'{"type":"result","result":"ok"}\'\n')
    write_executable(bindir / "codex", save + "echo ok\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    if kind == "claude-code":
        return ClaudeCodeAgent()
    if kind == "codex":
        return CodexAgent()
    if kind == "claude-sdk":
        return ClaudeSdkAgent()
    return CustomAgent(f"cat > '{capture}'; echo ok")


ADAPTERS = ("claude-code", "claude-sdk", "codex", "custom")


class TestTheSeam:
    @pytest.mark.parametrize("kind", ADAPTERS)
    def test_every_adapter_records_under_its_own_name(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = tmp_path / "stdin.txt"
        agent = _adapter(kind, capture, monkeypatch, tmp_path)
        call = _call(tmp_path)

        with recording_prompts(call):
            list(agent.run("the prompt\nline two\n", cwd=tmp_path, timeout=30))  # type: ignore[attr-defined]

        records = read_prompt_records(call.run_root, run_id="factory-x", component="comp-a")
        assert [(r.role, r.attempt, r.call, r.agent_cli) for r in records] == [
            ("engineer", 2, 1, kind)
        ]
        assert records[0].prompt == "the prompt\nline two\n"
        assert records[0].kstrl_version == kstrl_version()
        if kind != "claude-sdk":  # the SDK runner reads a JSON config, not the prompt
            assert capture.read_text(encoding="utf-8") == records[0].prompt

    @pytest.mark.parametrize("kind", ADAPTERS)
    def test_a_record_that_cannot_be_written_fails_the_call_before_the_cli_runs(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = tmp_path / "stdin.txt"
        agent = _adapter(kind, capture, monkeypatch, tmp_path)
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("", encoding="utf-8")

        with recording_prompts(_call(tmp_path, run_root=blocker / "run")):
            with pytest.raises(OSError):
                list(agent.run("the prompt", cwd=tmp_path, timeout=30))  # type: ignore[attr-defined]

        assert not capture.exists()

    @pytest.mark.parametrize("kind", ADAPTERS)
    def test_a_record_write_failing_with_file_not_found_is_not_a_missing_cli(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claude adapter reports a FileNotFoundError from its spawn as
        a missing CLI and returns normally. A record write that fails with
        the same type must raise out of the call instead."""
        capture = tmp_path / "stdin.txt"
        agent = _adapter(kind, capture, monkeypatch, tmp_path)

        def gone(path: Path, payload: dict[str, object]) -> None:
            raise FileNotFoundError(errno.ENOENT, "run directory removed (planted)")

        monkeypatch.setattr(prompt_record, "atomic_write_json", gone)

        with recording_prompts(_call(tmp_path)):
            with pytest.raises(FileNotFoundError, match="planted"):
                list(agent.run("the prompt", cwd=tmp_path, timeout=30))  # type: ignore[attr-defined]

        assert not capture.exists()

    def test_calls_in_one_scope_are_numbered_in_order(self, tmp_path: Path) -> None:
        call = _call(tmp_path)
        agent = CustomAgent("cat > /dev/null; echo ok")

        with recording_prompts(call):
            for text in ("first", "second", "third"):
                list(agent.run(text, cwd=tmp_path, timeout=30))

        records = read_prompt_records(call.run_root, run_id="factory-x", component="comp-a")
        assert [(r.call, r.prompt) for r in records] == [
            (1, "first"),
            (2, "second"),
            (3, "third"),
        ]

    def test_records_are_returned_in_call_order_past_nine_calls(self, tmp_path: Path) -> None:
        """An engineer attempt makes one call per iteration, and file names sort
        c1, c10, c11, ..., c2, so the reader must order by the number."""
        call = _call(tmp_path)
        with recording_prompts(call):
            for n in range(1, 12):
                record_prompt(f"prompt {n}", agent_cli="custom")

        records = read_prompt_records(call.run_root, run_id="factory-x", component="comp-a")

        assert [(r.call, r.prompt) for r in records] == [(n, f"prompt {n}") for n in range(1, 12)]

    def test_outside_a_scope_nothing_is_written(self, tmp_path: Path) -> None:
        with recording_prompts(None):
            list(CustomAgent("cat > /dev/null; echo ok").run("p", cwd=tmp_path, timeout=30))
        list(CustomAgent("cat > /dev/null; echo ok").run("p", cwd=tmp_path, timeout=30))

        assert not list(tmp_path.rglob("*.json"))

    def test_a_call_after_the_scope_closes_is_not_recorded(self, tmp_path: Path) -> None:
        call = _call(tmp_path)
        with recording_prompts(call):
            assert record_prompt("inside", agent_cli="custom") is not None

        assert record_prompt("after", agent_cli="custom") is None
        records = read_prompt_records(call.run_root, run_id="factory-x", component="comp-a")
        assert [r.prompt for r in records] == ["inside"]

    def test_an_unknown_agent_cli_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown agent_cli"):
            record_prompt("p", agent_cli="claude")


class TestTheReaderRefusesWhatIsNotItsOwn:
    def _written(self, tmp_path: Path) -> Path:
        with recording_prompts(_call(tmp_path)):
            path = record_prompt("the prompt", agent_cli="custom")
        assert path is not None
        return path

    def test_another_run_s_record_is_refused(self, tmp_path: Path) -> None:
        path = self._written(tmp_path)
        run_root = path.parents[2]
        with pytest.raises(PromptRecordError, match="not run 'factory-y'"):
            read_prompt_records(run_root, run_id="factory-y", component="comp-a")

    def test_a_record_filed_under_another_component_is_refused(self, tmp_path: Path) -> None:
        path = self._written(tmp_path)
        run_root = path.parents[2]
        moved = run_root / "prompts" / "comp-b" / path.name
        moved.parent.mkdir(parents=True)
        path.rename(moved)
        with pytest.raises(PromptRecordError, match="component 'comp-a'"):
            read_prompt_records(run_root, run_id="factory-x", component="comp-b")

    @pytest.mark.parametrize(
        "mutation",
        [
            ("drop", "prompt", None),
            ("set", "attempt", True),
            ("set", "attempt", "2"),
            ("set", "schema", 2),
            ("set", "agent_cli", "gpt"),
            ("set", "call", 7),
        ],
        ids=["no-prompt", "bool-attempt", "string-attempt", "schema-2", "unknown-cli", "renamed"],
    )
    def test_a_malformed_record_is_refused(
        self, tmp_path: Path, mutation: tuple[str, str, object]
    ) -> None:
        path = self._written(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        action, field, value = mutation
        if action == "drop":
            del payload[field]
        else:
            payload[field] = value
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(PromptRecordError):
            read_prompt_records(path.parents[2], run_id="factory-x", component="comp-a")

    def test_a_file_that_is_not_json_is_refused(self, tmp_path: Path) -> None:
        path = self._written(tmp_path)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(PromptRecordError, match="not a JSON document"):
            read_prompt_records(path.parents[2], run_id="factory-x", component="comp-a")

    def test_a_component_with_no_calls_has_no_records(self, tmp_path: Path) -> None:
        assert read_prompt_records(tmp_path, run_id="factory-x", component="comp-a") == []
