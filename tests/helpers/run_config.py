"""One run's configuration, driven end to end from a test (#192).

Shared by ``tests/test_config_read_once.py``, which drives Phase 1
directly, and ``tests/test_run_envelope.py``, which drives the whole
``run_factory``. One home rather than two copies: the ``kstrl.toml``
bodies are the fixture both halves compare against, and two copies of a
fixture drift into two different measurements.
"""

from __future__ import annotations

import io
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl import config as _config_module
from kstrl import config_toml as _toml_module
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, FactoryResult, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.pipeline import ComponentPipeline
from kstrl.ui.plain import PlainUI
from tests.helpers.component_prd import PASSING_STORY, write_component_prd


@dataclass
class TomlParseCounts:
    """How many times one run opened and parsed ``kstrl.toml``."""

    calls: int = 0
    parses: int = 0


def count_toml_parses(monkeypatch: pytest.MonkeyPatch) -> TomlParseCounts:
    """Count document loads and ``tomllib`` parses for the rest of the test.

    ``load_toml_document`` is bound in two namespaces since #366 split the
    reader into ``kstrl/config_toml.py``: ``load_toml_section`` resolves it
    there, and ``kstrl.config._apply_toml_overrides`` resolves it in
    ``kstrl.config``. Both are patched so a count that reaches either path
    is a count, not a silent under-count. ``tomllib.loads`` is looked up at
    call time on the module object, so patching the module is enough.
    """
    counts = TomlParseCounts()
    original_doc = _toml_module.load_toml_document
    original_loads = tomllib.loads

    def counting_doc(path: Path) -> dict[str, Any]:
        counts.calls += 1
        return original_doc(path)

    def counting_loads(text: str, **kwargs: Any) -> dict[str, Any]:
        counts.parses += 1
        return original_loads(text, **kwargs)

    monkeypatch.setattr(_config_module, "load_toml_document", counting_doc)
    monkeypatch.setattr(_toml_module, "load_toml_document", counting_doc)
    monkeypatch.setattr(tomllib, "loads", counting_loads)
    return counts


BEFORE = """\
[policy]
enabled = true
max_files_changed = 5
max_lines_changed = 100
deps_allow_new = false

[adequacy]
enabled = true
min_coverage_pct = 90

[autonomy]
enabled = {autonomy}
"""

AFTER = """\
[policy]
enabled = true
max_files_changed = 500
max_lines_changed = 100000
deps_allow_new = true

[adequacy]
enabled = false
min_coverage_pct = 0

[autonomy]
enabled = {autonomy}
"""

MALFORMED = """\
[policy]
enabled = true
max_files_changed = "two"

[autonomy]
enabled = false
"""


def edit(root: Path, body: str) -> Callable[[], None]:
    """The operator editing kstrl.toml while the run is in flight."""

    def write() -> None:
        (root / "kstrl.toml").write_text(body, encoding="utf-8")

    return write


def init_git_repo(root: Path) -> None:
    """A repo the diff phase can read."""

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

    run("init")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "tester")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-m", "base")


@dataclass(frozen=True)
class RunOutcome:
    """Everything one ``run_factory`` left behind that these tests read."""

    manifest: Manifest
    result: FactoryResult
    pipelines: list[ComponentPipeline]
    narration: str


def drive_run(
    tmp_path: Path,
    config: FactoryConfig,
    *,
    components: Sequence[Component] = (),
    on_pipeline: Callable[[], None] | None = None,
) -> RunOutcome:
    """Drive the REAL ``run_factory`` and hand back what it produced.

    With no components the subject is everything ``run_factory`` does
    BEFORE scheduling: resolve the envelope, refuse a section it cannot
    read, clamp with the autonomy ladder, hand the result to the
    pipeline and record its hash.

    With components the scheduler, ``process_result`` and the ``try``
    around ``future.result()`` are all entered, which is what makes a
    mid-run failure a failure of the RUN rather than of one call. The
    engineer is stubbed at ``kstrl.factory._run_component`` because the
    subject is the phase chain around it, not the loop.
    """
    scripts = tmp_path / "scripts" / "kstrl"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "prompt.md").write_text("p", encoding="utf-8")
    (scripts / "prd.json").write_text('{"branchName": "t", "userStories": []}', encoding="utf-8")
    for comp in components:
        write_component_prd(tmp_path, comp.prd_path, stories=[PASSING_STORY])
    if components:
        # Without a real repo the diff phase fails as infrastructure and
        # no component reaches a terminal verdict, so the run's exit code
        # would say nothing about the config edit under test.
        init_git_repo(tmp_path)
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=list(components),
    )
    manifest.save(tmp_path / "manifest.json")
    built: list[ComponentPipeline] = []
    real = ComponentPipeline

    def capture(**kwargs: Any) -> ComponentPipeline:
        # ``on_pipeline`` runs at CONSTRUCTION, which is the boundary
        # between what a run resolves for itself and what its epilogue
        # re-reads afterwards. A caller counting run-start work needs
        # that line and cannot get it from the return value.
        if on_pipeline is not None:
            on_pipeline()
        pipeline = real(**kwargs)
        built.append(pipeline)
        return pipeline

    narration = io.StringIO()
    with patch("kstrl.factory.ComponentPipeline", side_effect=capture):
        result = run_factory(
            manifest,
            config,
            KstrlConfig(
                prompt_file=scripts / "prompt.md",
                prd_file=scripts / "prd.json",
                sleep_seconds=0,
                agent_cmd="echo test",
            ),
            PlainUI(no_color=True, file=narration),
            tmp_path,
        )
    return RunOutcome(manifest, result, built, narration.getvalue())


def empty_run(
    tmp_path: Path,
    config: FactoryConfig,
    *,
    on_pipeline: Callable[[], None] | None = None,
) -> tuple[Manifest, ComponentPipeline]:
    """:func:`_drive_run` for the tests that only want the pipeline."""
    outcome = drive_run(tmp_path, config, on_pipeline=on_pipeline)
    assert len(outcome.pipelines) == 1
    return outcome.manifest, outcome.pipelines[0]
