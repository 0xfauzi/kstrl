"""The code reviewer runs the engineer's custom command when none is set (#498).

The defect: with ``[agent] command`` set in kstrl.toml and no
``--review-agent-cmd``, the security reviewer fell back to the engineer's
command and the code reviewer did not. The code reviewer resolved to an
auto-detected ``claude`` or ``codex`` CLI instead, so an operator running
a stub engineer paid for real review calls with no line saying so. The
code reviewer is also the integration reviewer, so the same selection
reached that phase too.

These tests drive the real CLI in a subprocess against a real git
repository and a real kstrl.toml. The manifest has no components, so no
agent is ever dispatched: the run resolves the reviewer, announces it,
records it in ``.kstrl/progress.jsonl``, and ends. A custom engineer
command has no known model family, so the R7.1 liveness probe never runs
either, and ``KSTRL_AGENT_PROBE=0`` is set as a second lock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.helpers import gitrepo

ENGINEER = "./stub-engineer.sh"
REVIEWER = "./stub-reviewer.sh"
REVIEW_WARNING = "the review reviewer is a custom command"
EXECUTION_HEADER = "== Factory: Execution =="


def _repo(tmp_path: Path) -> Path:
    """A git repo whose kstrl.toml names a custom engineer command and
    whose manifest has no components."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    (root / "kstrl.toml").write_text(f'[agent]\ncommand = "{ENGINEER}"\n', encoding="utf-8")
    manifest = root / "scripts" / "kstrl" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "version": "1",
                "specFile": "spec.md",
                "projectName": "p",
                "baseBranch": "main",
                "singlePr": False,
                "components": [],
            }
        ),
        encoding="utf-8",
    )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    return root


def _factory(root: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """`ks factory` in a subprocess, with every ambient kstrl knob removed
    so kstrl.toml is the only place the engineer command comes from."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL")
    }
    env["KSTRL_AGENT_PROBE"] = "0"
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "kstrl",
            "factory",
            "--manifest",
            str(root / "scripts" / "kstrl" / "manifest.json"),
            "--no-tui",
            "--no-prs",
            "--review-mode",
            "hard",
            "--contract-check",
            "skip",
            *flags,
            "--root",
            str(root),
            "--yes",
            "--ui",
            "plain",
            "--no-color",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def _review_selection(root: Path) -> dict[str, Any]:
    """The one ``adversarial_agent_selected`` event for the review phase."""
    lines = (root / ".kstrl" / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    review = [
        e["data"]
        for e in events
        if e["event"] == "adversarial_agent_selected" and e["data"]["phase"] == "review"
    ]
    assert len(review) == 1, review
    return review[0]


def test_toml_agent_command_is_the_review_command(tmp_path: Path) -> None:
    """RED before the fix: the recorded identity is "claude-code" (or
    "codex" on a machine without claude), and no review warning prints."""
    root = _repo(tmp_path)

    result = _factory(root)

    assert result.returncode == 0, result.stdout + result.stderr
    selection = _review_selection(root)
    assert selection["identity"] == f"custom ({ENGINEER})", selection
    assert selection["source"] == "same-family-fallback", selection
    # The operator sees it before any spend: the warning prints ahead of
    # the execution header, which is where components start.
    # PlainUI writes every line to stderr, so one stream holds both.
    assert REVIEW_WARNING in result.stderr, result.stderr
    assert result.stderr.index(REVIEW_WARNING) < result.stderr.index(EXECUTION_HEADER)


def test_review_agent_cmd_still_wins_over_the_engineer_command(tmp_path: Path) -> None:
    """Control, GREEN before and after the fix: an explicit reviewer
    command is never replaced by the engineer's."""
    root = _repo(tmp_path)

    result = _factory(root, "--review-agent-cmd", REVIEWER)

    assert result.returncode == 0, result.stdout + result.stderr
    selection = _review_selection(root)
    assert selection["identity"] == f"custom ({REVIEWER})", selection
    assert selection["source"] == "explicit", selection
