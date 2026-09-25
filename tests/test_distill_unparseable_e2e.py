"""#495: a distiller reply that does not parse is reported as unparseable,
from the distiller's subprocess to ``ks evolve``.

A real ``run_factory`` whose distiller is a real ``CustomAgent`` running
``cat`` on a reply file, then a real ``python -m kstrl evolve`` subprocess.
Only the engineer (``kstrl.factory._run_component``) and
``kstrl.git.get_diff_content`` are stubbed, the same two stubs
``tests/test_event_stream.py`` uses.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers.distill_replies import BROKEN_REPLY, EMPTY_REPLY, VALID_REPLY

PRD = '{"branchName": "t", "userStories": []}'


def _run_factory_with_reply(root: Path, reply: str) -> str:
    """One component, knowledge on, distiller = ``cat`` of ``reply``.

    Returns what the factory printed.
    """
    kstrl_dir = root / "scripts" / "kstrl"
    (kstrl_dir / "feature" / "comp-a").mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("p", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text(PRD, encoding="utf-8")
    (kstrl_dir / "feature" / "comp-a" / "prd.json").write_text(PRD, encoding="utf-8")
    (root / "kstrl.toml").write_text("[knowledge]\nenabled = true\n", encoding="utf-8")
    reply_file = root / "reply.json"
    reply_file.write_text(reply + "\n", encoding="utf-8")
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a",
                "A",
                "d",
                [],
                "scripts/kstrl/feature/comp-a/prd.json",
                "kstrl/factory/comp-a",
            )
        ],
    )
    config = FactoryConfig(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
        progress_log_path=root / "progress.jsonl",
    )
    base = KstrlConfig(
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        agent_cmd=f"cat {reply_file}",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    out = io.StringIO()

    def engineer(comp_id: str, *args: Any, **kwargs: Any) -> ComponentResult:
        return ComponentResult(comp_id, success=True, iterations=1, duration_seconds=1.0)

    with (
        patch("kstrl.factory._run_component", side_effect=engineer),
        patch("kstrl.git.get_diff_content", return_value="diff --git a/x b/x\n+x = 1\n"),
    ):
        run_factory(manifest, config, base, PlainUI(no_color=True, file=out), root)
    return out.getvalue()


def _distill_rows(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((root / ".kstrl" / "runs").glob("*/events.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            if obj.get("event") == "distill_result":
                rows.append(obj["data"])
    return rows


def _dump_status(root: Path) -> str:
    paths = list(root.glob(".kstrl/knowledge/comp-a/_debug/*/_distill_status.txt"))
    assert len(paths) == 1, paths
    return paths[0].read_text(encoding="utf-8")


def _evolve(root: Path) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kstrl",
            "evolve",
            "--root",
            str(root),
            "--ui",
            "plain",
            "--no-color",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    return proc.stdout


def test_the_broken_reply_is_the_valid_reply_with_one_extra_brace() -> None:
    """The fixture control: without this, a BROKEN_REPLY broken in some
    other way would make the tests below pass for the wrong reason."""
    assert BROKEN_REPLY.replace('"}},', '"},', 1) == VALID_REPLY
    assert len(json.loads(VALID_REPLY)["facts"]) == 2
    with pytest.raises(json.JSONDecodeError, match="Expecting ',' delimiter"):
        json.loads(BROKEN_REPLY)


def test_unparseable_reply_reaches_the_event_and_ks_evolve(tmp_path: Path) -> None:
    printed = _run_factory_with_reply(tmp_path, BROKEN_REPLY)

    assert _dump_status(tmp_path) == "unparseable"
    assert "Knowledge: the distiller's reply did not parse: " in printed
    rows = _distill_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["facts_written"] == 0
    assert rows[0]["parse_failed"] is True
    assert "distill replies that did not parse: 1 of 1 distill(s) in the last 1 run(s)" in _evolve(
        tmp_path
    )


def test_clean_empty_reply_is_not_counted_as_a_parse_failure(tmp_path: Path) -> None:
    printed = _run_factory_with_reply(tmp_path, EMPTY_REPLY)

    assert _dump_status(tmp_path) == "no_facts"
    assert "Knowledge: the distiller returned no facts" in printed
    rows = _distill_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["parse_failed"] is False
    assert "distill replies that did not parse: 0 of 1 distill(s) in the last 1 run(s)" in _evolve(
        tmp_path
    )
