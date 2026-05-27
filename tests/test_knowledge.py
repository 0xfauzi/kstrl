"""Tests for the per-component knowledge layer."""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from ralph_py.config import RalphConfig
from ralph_py.factory import ComponentResult, FactoryConfig, run_factory
from ralph_py.knowledge import (
    DISTILL_PROMPT,
    Fact,
    KnowledgeConfig,
    _coerce_facts,
    _first_sentence,
    _pack_facts_full,
    _pack_facts_summary,
    _parse_distill_output,
    _parse_fact_md,
    _render_fact_md,
    _transitive_dependencies,
    build_knowledge_context,
    current_run_id,
    distill_facts,
    read_facts,
    write_facts,
)
from ralph_py.manifest import Component, Manifest
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import VerifyConfig


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_fact(
    fact_id: str = "fact-001",
    component_id: str = "comp-a",
    scope: str = "handler",
    confidence: str = "verified",
    claim: str = "The handler validates the request body before invoking the service.",
    evidence: list[str] | None = None,
    tags: list[str] | None = None,
    created_run_id: str = "factory-20260101-120000",
    created_iter: int = 1,
) -> Fact:
    return Fact(
        id=fact_id,
        component_id=component_id,
        created_iter=created_iter,
        created_run_id=created_run_id,
        scope=scope,
        evidence=evidence or ["src/handler.py:10-25"],
        confidence=confidence,
        claim=claim,
        tags=tags or [],
    )


class _FakeAgent:
    """Minimal Agent stand-in. Yields canned lines from .run()."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    @property
    def name(self) -> str:
        return "fake"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        for line in self._lines:
            yield line

    @property
    def final_message(self) -> str | None:
        return None


def _make_manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=components,
    )


def _make_component(
    component_id: str, dependencies: list[str] | None = None,
) -> Component:
    return Component(
        id=component_id,
        title=f"Component {component_id}",
        description=f"Description of {component_id}",
        dependencies=dependencies or [],
        prd_path=f"feature/{component_id}/prd.json",
        branch_name=f"ralph/{component_id}",
    )


# ---------------------------------------------------------------------------
# KnowledgeConfig
# ---------------------------------------------------------------------------


class TestKnowledgeConfig:
    def test_defaults(self) -> None:
        config = KnowledgeConfig()
        assert config.enabled is True
        assert config.max_core_tokens == 2000
        assert config.max_dependency_tokens == 1000
        assert config.max_sibling_tokens == 500
        assert config.distill_timeout_seconds == 300.0
        assert config.max_facts_per_distill == 7

    def test_load_no_toml_uses_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "RALPH_KNOWLEDGE_ENABLED",
            "RALPH_KNOWLEDGE_MAX_CORE_TOKENS",
        ):
            monkeypatch.delenv(var, raising=False)
        config = KnowledgeConfig.load(tmp_path)
        assert config.enabled is True
        assert config.max_core_tokens == 2000
        assert config.knowledge_root == tmp_path / ".ralph" / "knowledge"

    def test_load_reads_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "RALPH_KNOWLEDGE_ENABLED",
            "RALPH_KNOWLEDGE_MAX_CORE_TOKENS",
            "RALPH_KNOWLEDGE_MAX_DEPENDENCY_TOKENS",
        ):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / "ralph.toml").write_text(
            """
[knowledge]
enabled = false
max_core_tokens = 9999
max_dependency_tokens = 50
""",
        )
        config = KnowledgeConfig.load(tmp_path)
        assert config.enabled is False
        assert config.max_core_tokens == 9999
        assert config.max_dependency_tokens == 50

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        """KnowledgeConfig.load must raise ValueError on bad TOML, matching
        RalphConfig.load. Silently falling back to defaults would hide
        user typos in the [knowledge] section."""
        (tmp_path / "ralph.toml").write_text(
            "this is not = valid = toml = [\n",
        )
        with pytest.raises(ValueError, match="Invalid TOML"):
            KnowledgeConfig.load(tmp_path)

    def test_env_overrides_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "ralph.toml").write_text(
            """
[knowledge]
max_core_tokens = 9999
""",
        )
        monkeypatch.setenv("RALPH_KNOWLEDGE_MAX_CORE_TOKENS", "111")
        config = KnowledgeConfig.load(tmp_path)
        assert config.max_core_tokens == 111


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------


class TestFactSerialization:
    def test_render_then_parse_roundtrip(self) -> None:
        original = _make_fact(
            claim="Line one of the claim.\nLine two with more detail.",
            evidence=["a.py:1-5", "b.py:42"],
            tags=["a", "b"],
        )
        content = _render_fact_md(original)
        parsed = _parse_fact_md(content)
        assert parsed == original

    def test_parse_missing_opening_delimiter(self) -> None:
        with pytest.raises(ValueError, match="opening frontmatter"):
            _parse_fact_md("not a frontmatter file")

    def test_parse_missing_closing_delimiter(self) -> None:
        content = "---\n{\"id\": \"x\"}\nno closing"
        with pytest.raises(ValueError, match="closing frontmatter"):
            _parse_fact_md(content)

    def test_parse_invalid_json(self) -> None:
        content = "---\nthis is not json\n---\nbody"
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_fact_md(content)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


class TestReadWriteFacts:
    def test_write_then_read(self, tmp_path: Path) -> None:
        fact = _make_fact()
        written = write_facts([fact], tmp_path, "comp-a", "factory-20260101-120000")
        assert written == 1
        facts = read_facts(tmp_path, "comp-a")
        assert facts == [fact]

    def test_read_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        assert read_facts(tmp_path, "nonexistent") == []

    def test_read_prefers_latest_run_dir(self, tmp_path: Path) -> None:
        old = _make_fact(
            fact_id="fact-001", claim="old fact",
            created_run_id="factory-20260101-120000",
        )
        new = _make_fact(
            fact_id="fact-001", claim="new fact",
            created_run_id="factory-20260201-120000",
        )
        write_facts([old], tmp_path, "comp-a", "factory-20260101-120000")
        write_facts([new], tmp_path, "comp-a", "factory-20260201-120000")
        facts = read_facts(tmp_path, "comp-a")
        assert len(facts) == 1
        assert facts[0].claim == "new fact"

    def test_write_atomic_no_partial_files(self, tmp_path: Path) -> None:
        write_facts(
            [_make_fact()], tmp_path, "comp-a", "factory-20260101-120000",
        )
        run_dir = tmp_path / "comp-a" / "factory-20260101-120000"
        leftovers = [p for p in run_dir.iterdir() if p.name.startswith(".")]
        assert leftovers == [], f"atomic write left tmp files: {leftovers}"

    def test_write_skips_corrupt_files_on_read(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "comp-a" / "factory-20260101-120000"
        run_dir.mkdir(parents=True)
        (run_dir / "broken.md").write_text("not a valid fact file")
        good = _make_fact()
        write_facts([good], tmp_path, "comp-a", "factory-20260101-120000")
        facts = read_facts(tmp_path, "comp-a")
        assert facts == [good]

    def test_write_to_unwritable_dir_is_nonfatal(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("Running as root: cannot test permission denial")
        # Make parent read-only
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(stat.S_IREAD | stat.S_IEXEC)
        try:
            written = write_facts(
                [_make_fact()], readonly, "comp-a", "factory-20260101-120000",
            )
            assert written == 0
        finally:
            readonly.chmod(stat.S_IRWXU)

    def test_write_empty_returns_zero(self, tmp_path: Path) -> None:
        assert write_facts([], tmp_path, "comp-a", "run-id") == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_first_sentence_period(self) -> None:
        assert _first_sentence("Hello world. More text.") == "Hello world."

    def test_first_sentence_no_terminator(self) -> None:
        assert _first_sentence("just one phrase") == "just one phrase"

    def test_first_sentence_question_mark(self) -> None:
        assert _first_sentence("What does it do? It works.") == "What does it do?"

    def test_first_sentence_empty(self) -> None:
        assert _first_sentence("") == ""

    def test_first_sentence_preserves_eg_abbreviation(self) -> None:
        assert (
            _first_sentence(
                "Handler returns 200, e.g. for GET /health. Returns 500 otherwise.",
            )
            == "Handler returns 200, e.g. for GET /health."
        )

    def test_first_sentence_preserves_ie_abbreviation(self) -> None:
        assert (
            _first_sentence("Maps i.e. simply. Done.")
            == "Maps i.e. simply."
        )

    def test_first_sentence_single_sentence_ending_period(self) -> None:
        # No continuation after the terminal period - $ branch fires.
        assert _first_sentence("A single sentence.") == "A single sentence."

    def test_transitive_dependencies_chain(self) -> None:
        manifest = _make_manifest([
            _make_component("a"),
            _make_component("b", dependencies=["a"]),
            _make_component("c", dependencies=["b"]),
        ])
        deps = _transitive_dependencies(manifest, "c")
        assert deps == {"a", "b"}

    def test_transitive_dependencies_no_self_reference(self) -> None:
        manifest = _make_manifest([_make_component("a")])
        assert _transitive_dependencies(manifest, "a") == set()

    def test_transitive_dependencies_diamond(self) -> None:
        manifest = _make_manifest([
            _make_component("a"),
            _make_component("b", dependencies=["a"]),
            _make_component("c", dependencies=["a"]),
            _make_component("d", dependencies=["b", "c"]),
        ])
        assert _transitive_dependencies(manifest, "d") == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Budget capping
# ---------------------------------------------------------------------------


class TestPackFacts:
    def test_pack_empty(self) -> None:
        kept, over = _pack_facts_full([], 1000)
        assert kept == []
        assert over is False

    def test_pack_fits_within_budget(self) -> None:
        facts = [_make_fact(fact_id=f"fact-{i:03d}") for i in range(1, 4)]
        kept, over = _pack_facts_full(facts, 10000)
        assert len(kept) == 3
        assert over is False

    def test_pack_drops_asserted_before_verified(self) -> None:
        # Make claims sized so only one fact fits per call after the verified one
        big_claim = "x" * 800  # ~200 tokens
        verified = _make_fact(
            fact_id="fact-001", confidence="verified", claim=big_claim,
        )
        asserted_recent = _make_fact(
            fact_id="fact-002", confidence="asserted", claim=big_claim,
            created_run_id="factory-20260301-120000",
        )
        asserted_old = _make_fact(
            fact_id="fact-003", confidence="asserted", claim=big_claim,
            created_run_id="factory-20260101-120000",
        )
        kept, over = _pack_facts_full(
            [asserted_old, asserted_recent, verified], 250,
        )
        # Verified takes the budget; both asserted are dropped
        kept_ids = [f.id for f in kept]
        assert "fact-001" in kept_ids
        assert over is True

    def test_pack_summary_uses_first_sentence(self) -> None:
        fact = _make_fact(
            claim="First sentence. Second sentence that should be dropped.",
        )
        kept, _over = _pack_facts_summary([fact], 10000)
        assert len(kept) == 1
        assert kept[0].claim == "First sentence."

    def test_pack_full_does_not_drop_smaller_facts_after_overflow(self) -> None:
        """A single oversized fact must not abort the whole pack: smaller
        subsequent facts that still fit in the remaining budget should be
        kept."""
        huge = _make_fact(
            fact_id="fact-001",
            confidence="verified",
            claim="x" * 1000,  # render cost ~300 tokens
        )
        small_a = _make_fact(
            fact_id="fact-002",
            confidence="verified",
            claim="short A",
        )
        # Budget chosen so huge cannot fit AND the truncation branch
        # can't fire (remaining < 30 tokens after subtracting meta size).
        # Without the greedy-break fix, the loop aborts on huge and the
        # subsequent small fact never gets considered.
        kept, over = _pack_facts_full([huge, small_a], 70)
        kept_ids = [f.id for f in kept]
        assert "fact-001" not in kept_ids
        assert "fact-002" in kept_ids
        assert over is True

    def test_pack_summary_does_not_drop_smaller_facts_after_overflow(
        self,
    ) -> None:
        """Same fix in _pack_facts_summary: an oversized first sentence
        must not block smaller subsequent sentences."""
        # _first_sentence is greedy; use claims with no terminator so the
        # entire claim is the "first sentence".
        huge = _make_fact(
            fact_id="fact-001",
            confidence="verified",
            claim="A" * 4000,
        )
        small_a = _make_fact(
            fact_id="fact-002",
            confidence="verified",
            claim="short",
        )
        kept, over = _pack_facts_summary([huge, small_a], 60)
        kept_ids = [f.id for f in kept]
        assert "fact-002" in kept_ids
        assert "fact-001" not in kept_ids
        assert over is True

    def test_pack_full_truncates_only_first_overflowing_fact(self) -> None:
        """The truncation branch should fire at most once per pack call,
        not repeatedly squeezing fact bodies down to nothing."""
        # All three are too large at full size; only first should be
        # truncated to fit, the rest dropped.
        f1 = _make_fact(
            fact_id="fact-001", confidence="verified", claim="A" * 800,
        )
        f2 = _make_fact(
            fact_id="fact-002", confidence="verified", claim="B" * 800,
        )
        f3 = _make_fact(
            fact_id="fact-003", confidence="verified", claim="C" * 800,
        )
        kept, over = _pack_facts_full([f1, f2, f3], 200)
        truncated = [f for f in kept if f.claim.endswith("...")]
        assert len(truncated) <= 1
        assert over is True


# ---------------------------------------------------------------------------
# build_knowledge_context
# ---------------------------------------------------------------------------


class TestBuildKnowledgeContext:
    def test_empty_when_no_facts(self, tmp_path: Path) -> None:
        manifest = _make_manifest([_make_component("a"), _make_component("b")])
        comp = manifest.components[0]
        config = KnowledgeConfig(
            knowledge_root=tmp_path / "knowledge", enabled=True,
        )
        # Even with no knowledge dir at all
        result = build_knowledge_context(
            manifest, comp, config.knowledge_root, config,
        )
        assert result == ""

    def test_empty_when_disabled(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir()
        write_facts([_make_fact()], knowledge_root, "comp-a", "run-1")
        manifest = _make_manifest([_make_component("comp-a")])
        config = KnowledgeConfig(knowledge_root=knowledge_root, enabled=False)
        result = build_knowledge_context(
            manifest, manifest.components[0], knowledge_root, config,
        )
        assert result == ""

    def test_three_tiers(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        manifest = _make_manifest([
            _make_component("dep-1"),
            _make_component("comp-current", dependencies=["dep-1"]),
            _make_component("unrelated"),
        ])
        write_facts(
            [_make_fact(
                fact_id="fact-001", component_id="comp-current",
                claim="Current component fact.",
            )],
            knowledge_root, "comp-current", "factory-20260101-120000",
        )
        write_facts(
            [_make_fact(
                fact_id="fact-002", component_id="dep-1",
                claim="Dependency fact full body. Second sentence.",
            )],
            knowledge_root, "dep-1", "factory-20260101-120000",
        )
        write_facts(
            [_make_fact(
                fact_id="fact-003", component_id="unrelated",
                claim="Unrelated sibling fact. Second sentence trimmed.",
            )],
            knowledge_root, "unrelated", "factory-20260101-120000",
        )

        config = KnowledgeConfig(knowledge_root=knowledge_root)
        result = build_knowledge_context(
            manifest, manifest.components[1], knowledge_root, config,
        )
        assert "## Component Knowledge" in result
        assert "Current component fact." in result
        assert "Dependency fact full body. Second sentence." in result
        # Sibling shows only first sentence
        assert "Unrelated sibling fact." in result
        assert "Second sentence trimmed." not in result


# ---------------------------------------------------------------------------
# _coerce_facts / _parse_distill_output
# ---------------------------------------------------------------------------


class TestCoerceFacts:
    def test_valid_fact(self) -> None:
        raw = [{
            "id": "fact-001",
            "scope": "handler",
            "confidence": "verified",
            "evidence": ["src/a.py:1-10"],
            "claim": "a claim",
            "tags": ["x"],
        }]
        facts = _coerce_facts(raw, "comp-a", 1, "run-1", 7)
        assert len(facts) == 1
        assert facts[0].id == "fact-001"
        assert facts[0].scope == "handler"

    def test_invalid_id_skipped(self) -> None:
        raw = [{
            "id": "not-a-fact-id",
            "scope": "handler",
            "confidence": "verified",
            "evidence": ["a.py:1"],
            "claim": "x",
        }]
        assert _coerce_facts(raw, "c", 1, "r", 7) == []

    def test_unknown_scope_skipped(self) -> None:
        raw = [{
            "id": "fact-001",
            "scope": "weird-scope",
            "confidence": "verified",
            "evidence": ["a.py:1"],
            "claim": "x",
        }]
        assert _coerce_facts(raw, "c", 1, "r", 7) == []

    def test_missing_evidence_skipped(self) -> None:
        raw = [{
            "id": "fact-001",
            "scope": "handler",
            "confidence": "verified",
            "evidence": [],
            "claim": "x",
        }]
        assert _coerce_facts(raw, "c", 1, "r", 7) == []

    def test_empty_claim_skipped(self) -> None:
        raw = [{
            "id": "fact-001",
            "scope": "handler",
            "confidence": "verified",
            "evidence": ["a:1"],
            "claim": "",
        }]
        assert _coerce_facts(raw, "c", 1, "r", 7) == []

    def test_duplicate_id_skipped(self) -> None:
        raw = [
            {"id": "fact-001", "scope": "handler", "confidence": "verified",
             "evidence": ["a:1"], "claim": "first"},
            {"id": "fact-001", "scope": "handler", "confidence": "verified",
             "evidence": ["a:2"], "claim": "second"},
        ]
        facts = _coerce_facts(raw, "c", 1, "r", 7)
        assert len(facts) == 1
        assert facts[0].claim == "first"

    def test_max_facts_enforced(self) -> None:
        raw = [
            {"id": f"fact-{i:03d}", "scope": "handler", "confidence": "verified",
             "evidence": ["a:1"], "claim": "x"}
            for i in range(1, 11)
        ]
        assert len(_coerce_facts(raw, "c", 1, "r", 3)) == 3


class TestParseDistillOutput:
    def test_valid_json(self) -> None:
        output = json.dumps({"facts": [{"id": "fact-001"}]})
        assert _parse_distill_output(output) == [{"id": "fact-001"}]

    def test_fenced_json(self) -> None:
        output = '```json\n{"facts": [{"id": "fact-001"}]}\n```'
        assert _parse_distill_output(output) == [{"id": "fact-001"}]

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_distill_output("garbage") == []

    def test_missing_facts_key_returns_empty(self) -> None:
        assert _parse_distill_output('{"other": 1}') == []

    def test_facts_not_a_list_returns_empty(self) -> None:
        assert _parse_distill_output('{"facts": "not a list"}') == []


# ---------------------------------------------------------------------------
# distill_facts
# ---------------------------------------------------------------------------


class TestDistillFacts:
    def _setup_prd(self, tmp_path: Path, component_id: str) -> Path:
        prd_path = tmp_path / "feature" / component_id / "prd.json"
        prd_path.parent.mkdir(parents=True)
        prd_path.write_text(
            json.dumps({
                "branchName": "test",
                "userStories": [],
            }),
        )
        return prd_path

    def test_writes_facts_on_valid_output(self, tmp_path: Path) -> None:
        component = _make_component("comp-a")
        prd_path = self._setup_prd(tmp_path, "comp-a")
        knowledge_root = tmp_path / "knowledge"
        config = KnowledgeConfig(knowledge_root=knowledge_root)
        agent = _FakeAgent([
            json.dumps({
                "facts": [{
                    "id": "fact-001",
                    "scope": "handler",
                    "confidence": "verified",
                    "evidence": ["src/handler.py:10-25"],
                    "claim": "Handler validates input.",
                    "tags": [],
                }],
            }),
        ])

        written, status = distill_facts(
            agent, component, "diff text", prd_path, 1,
            "factory-20260101-120000", knowledge_root, config, tmp_path,
            review_passed=True,
        )

        assert written == 1
        assert "wrote 1" in status
        facts = read_facts(knowledge_root, "comp-a")
        assert len(facts) == 1
        assert facts[0].confidence == "verified"

    def test_invalid_json_nonfatal(self, tmp_path: Path) -> None:
        component = _make_component("comp-a")
        prd_path = self._setup_prd(tmp_path, "comp-a")
        knowledge_root = tmp_path / "knowledge"
        config = KnowledgeConfig(knowledge_root=knowledge_root)
        agent = _FakeAgent(["garbage that is not json"])

        written, status = distill_facts(
            agent, component, "diff", prd_path, 1, "run-1",
            knowledge_root, config, tmp_path, review_passed=True,
        )
        assert written == 0
        assert "no_facts" in status

    def test_empty_facts_returns_no_files(self, tmp_path: Path) -> None:
        component = _make_component("comp-a")
        prd_path = self._setup_prd(tmp_path, "comp-a")
        knowledge_root = tmp_path / "knowledge"
        config = KnowledgeConfig(knowledge_root=knowledge_root)
        agent = _FakeAgent([json.dumps({"facts": []})])

        written, status = distill_facts(
            agent, component, "diff", prd_path, 1, "run-1",
            knowledge_root, config, tmp_path, review_passed=True,
        )
        assert written == 0
        assert "no_facts" in status
        # No fact files were written (a debug dump may exist alongside;
        # that's a diagnostic, not a knowledge artifact).
        assert read_facts(knowledge_root, "comp-a") == []
        fact_files = list(knowledge_root.glob("comp-a/*/fact-*.md"))
        assert fact_files == []

    def test_disabled_short_circuits(self, tmp_path: Path) -> None:
        component = _make_component("comp-a")
        prd_path = self._setup_prd(tmp_path, "comp-a")
        knowledge_root = tmp_path / "knowledge"
        config = KnowledgeConfig(knowledge_root=knowledge_root, enabled=False)
        agent = _FakeAgent([json.dumps({"facts": [{"id": "fact-001"}]})])

        written, status = distill_facts(
            agent, component, "diff", prd_path, 1, "run-1",
            knowledge_root, config, tmp_path, review_passed=True,
        )
        assert written == 0
        assert status == "knowledge.disabled"

    def test_review_skip_caps_confidence_at_asserted(
        self, tmp_path: Path,
    ) -> None:
        component = _make_component("comp-a")
        prd_path = self._setup_prd(tmp_path, "comp-a")
        knowledge_root = tmp_path / "knowledge"
        config = KnowledgeConfig(knowledge_root=knowledge_root)
        agent = _FakeAgent([
            json.dumps({
                "facts": [{
                    "id": "fact-001",
                    "scope": "handler",
                    "confidence": "verified",  # agent claims verified
                    "evidence": ["a.py:1"],
                    "claim": "x",
                }],
            }),
        ])

        written, _status = distill_facts(
            agent, component, "diff", prd_path, 1, "run-1",
            knowledge_root, config, tmp_path,
            review_passed=None,  # skip mode
        )
        assert written == 1
        facts = read_facts(knowledge_root, "comp-a")
        # downgraded because Phase 2 was skipped
        assert facts[0].confidence == "asserted"

    def test_final_message_preferred_over_streamed_output(
        self, tmp_path: Path,
    ) -> None:
        """When the agent echoes the prompt back (as codex does), the streamed
        output contains the JSON schema example. _extract_json's first-brace
        heuristic would latch onto the schema's example fact (with the literal
        scope string "handler|adapter|...") and reject it during coercion.
        distill_facts must prefer agent.final_message when set."""
        component = _make_component("comp-a")
        prd_path = self._setup_prd(tmp_path, "comp-a")
        knowledge_root = tmp_path / "knowledge"
        config = KnowledgeConfig(knowledge_root=knowledge_root)

        schema_example_dump = """
prompt echoed back: schema is
{
  "facts": [
    {
      "id": "fact-001",
      "scope": "handler|adapter|schema|contract|invariant|gotcha",
      "confidence": "verified|asserted",
      "evidence": ["path/to/file.py:42-58"],
      "tags": ["x"],
      "claim": "example"
    }
  ]
}
... and so on ...
"""
        real_response = json.dumps({
            "facts": [{
                "id": "fact-001",
                "scope": "handler",
                "confidence": "verified",
                "evidence": ["src/x.py:1-2"],
                "claim": "real fact from final_message",
                "tags": [],
            }],
        })

        class _EchoingAgent:
            @property
            def name(self) -> str:
                return "fake-codex"

            def run(
                self, prompt: str, cwd: Path | None = None,
                timeout: float | None = None,
            ) -> Iterator[str]:
                # Mimic codex: echo prompt back, then output JSON
                for line in schema_example_dump.split("\n"):
                    yield line
                yield real_response

            @property
            def final_message(self) -> str | None:
                return real_response

        written, _status = distill_facts(
            _EchoingAgent(), component, "diff", prd_path, 1, "run-1",
            knowledge_root, config, tmp_path, review_passed=True,
        )

        assert written == 1
        facts = read_facts(knowledge_root, "comp-a")
        assert len(facts) == 1
        assert facts[0].claim == "real fact from final_message"
        # If we'd parsed the streamed echo, scope would have been the
        # pipe-separated schema string and the fact would have been rejected.
        assert facts[0].scope == "handler"

    def test_falls_back_to_streamed_when_final_message_unparseable(
        self, tmp_path: Path,
    ) -> None:
        """If agent.final_message is set but doesn't contain valid JSON
        (e.g. CustomAgent that emits multi-line JSON and records only the
        last line as final_message), distill_facts must fall back to the
        streamed output rather than silently dropping the response."""
        component = _make_component("comp-a")
        prd_path = self._setup_prd(tmp_path, "comp-a")
        knowledge_root = tmp_path / "knowledge"
        config = KnowledgeConfig(knowledge_root=knowledge_root)

        multi_line_json = json.dumps({
            "facts": [{
                "id": "fact-001",
                "scope": "handler",
                "confidence": "verified",
                "evidence": ["src/x.py:1"],
                "claim": "valid claim from streamed output",
                "tags": [],
            }],
        })

        class _PartialFinalAgent:
            """Mimics CustomAgent: final_message is just the last
            non-empty line of streamed output, not the full response."""

            @property
            def name(self) -> str:
                return "fake-custom"

            def run(
                self, prompt: str, cwd: Path | None = None,
                timeout: float | None = None,
            ) -> Iterator[str]:
                # Stream the JSON across multiple lines
                for line in multi_line_json.split(","):
                    yield line + ("," if line != multi_line_json.split(",")[-1] else "")

            @property
            def final_message(self) -> str | None:
                # Only the last line, like CustomAgent records
                return multi_line_json.split(",")[-1]

        written, status = distill_facts(
            _PartialFinalAgent(), component, "diff", prd_path, 1, "run-1",
            knowledge_root, config, tmp_path, review_passed=True,
        )

        assert written == 1, f"expected fallback to streamed; got status={status}"
        facts = read_facts(knowledge_root, "comp-a")
        assert facts[0].claim == "valid claim from streamed output"

    def test_diff_truncated_at_50kb(self, tmp_path: Path) -> None:
        component = _make_component("comp-a")
        prd_path = self._setup_prd(tmp_path, "comp-a")
        knowledge_root = tmp_path / "knowledge"
        config = KnowledgeConfig(knowledge_root=knowledge_root)
        captured: dict[str, str] = {}

        class _CapturingAgent:
            @property
            def name(self) -> str:
                return "fake"

            def run(
                self, prompt: str, cwd: Path | None = None,
                timeout: float | None = None,
            ) -> Iterator[str]:
                captured["prompt"] = prompt
                yield json.dumps({"facts": []})

            @property
            def final_message(self) -> str | None:
                return None

        huge_diff = "X" * 60000
        distill_facts(
            _CapturingAgent(), component, huge_diff, prd_path, 1, "r",
            knowledge_root, config, tmp_path, review_passed=True,
        )
        assert "(diff truncated at 50KB)" in captured["prompt"]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_current_run_id_matches_factory_format() -> None:
    rid = current_run_id()
    assert rid.startswith("factory-")
    # Same shape factory.py uses
    assert len(rid) == len("factory-20260101-120000")


def test_distill_prompt_includes_all_placeholders() -> None:
    rendered = DISTILL_PROMPT.format(
        max_facts=7,
        component_id="comp-a",
        component_title="title",
        component_description="desc",
        dependencies="(none)",
        prd_content="prd",
        existing_facts="(none)",
        diff_content="diff",
    )
    assert "comp-a" in rendered
    assert "prd" in rendered
    assert "diff" in rendered


# ---------------------------------------------------------------------------
# Factory integration: distillation called only on green gate
# ---------------------------------------------------------------------------


def _setup_factory_project(tmp_path: Path, component_id: str) -> Path:
    """Create a minimal project layout that satisfies the factory."""
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    feature_dir = tmp_path / "scripts" / "ralph" / "feature" / component_id
    feature_dir.mkdir(parents=True)
    (feature_dir / "prd.json").write_text(
        json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }),
    )
    return tmp_path


def _factory_base_config(root: Path) -> RalphConfig:
    return RalphConfig(
        prompt_file=root / "scripts/ralph/prompt.md",
        prd_file=root / "scripts/ralph/prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        ralph_branch="",
        ralph_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


class TestFactoryDistillIntegration:
    def test_distill_called_on_success(self, tmp_path: Path) -> None:
        root = _setup_factory_project(tmp_path, "comp-a")
        manifest = _make_manifest([_make_component("comp-a", dependencies=[])])
        # ensure component has the prd_path matching what factory expects
        manifest.components[0].prd_path = (
            "scripts/ralph/feature/comp-a/prd.json"
        )
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        base = _factory_base_config(root)
        ui = PlainUI(no_color=True)
        success = ComponentResult("comp-a", success=True, iterations=2)

        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ), patch(
            "ralph_py.factory.distill_facts", return_value=(2, "ok"),
        ) as mock_distill:
            result = run_factory(manifest, config, base, ui, root)

        assert "comp-a" in result.completed
        mock_distill.assert_called_once()

    def test_distill_not_called_on_verification_failure(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_factory_project(tmp_path, "comp-a")
        manifest = _make_manifest([_make_component("comp-a", dependencies=[])])
        manifest.components[0].prd_path = (
            "scripts/ralph/feature/comp-a/prd.json"
        )
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            max_retries=0, retry_delay=0, review_mode="skip",
        )
        base = _factory_base_config(root)
        ui = PlainUI(no_color=True)
        fail = ComponentResult("comp-a", success=False, error="test failure")

        with patch(
            "ralph_py.factory._run_component", return_value=fail,
        ), patch(
            "ralph_py.factory.distill_facts", return_value=(0, "skipped"),
        ) as mock_distill:
            result = run_factory(manifest, config, base, ui, root)

        assert "comp-a" in result.failed
        mock_distill.assert_not_called()

    def test_distill_disabled_via_env_skips_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "false")
        root = _setup_factory_project(tmp_path, "comp-a")
        manifest = _make_manifest([_make_component("comp-a", dependencies=[])])
        manifest.components[0].prd_path = (
            "scripts/ralph/feature/comp-a/prd.json"
        )
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        base = _factory_base_config(root)
        ui = PlainUI(no_color=True)
        success = ComponentResult("comp-a", success=True, iterations=2)

        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ), patch(
            "ralph_py.factory.distill_facts", return_value=(0, "disabled"),
        ) as mock_distill:
            run_factory(manifest, config, base, ui, root)

        mock_distill.assert_not_called()


# ---------------------------------------------------------------------------
# Latest-run-dir-wins handles contract-breaker rollback scenario
# ---------------------------------------------------------------------------


def test_latest_run_dir_orphans_old_facts(tmp_path: Path) -> None:
    """If a component re-runs (e.g. contract-breaker retry), the new run
    dir wins and old facts are orphaned without explicit supersede logic."""
    knowledge_root = tmp_path
    old = _make_fact(
        fact_id="fact-001", claim="OLD - should be ignored",
        created_run_id="factory-20260101-120000",
    )
    new = _make_fact(
        fact_id="fact-001", claim="NEW",
        created_run_id="factory-20260201-120000",
    )
    write_facts([old], knowledge_root, "comp-a", "factory-20260101-120000")
    # Simulate breaker retry creating a new run dir
    time.sleep(0.01)
    write_facts([new], knowledge_root, "comp-a", "factory-20260201-120000")

    facts = read_facts(knowledge_root, "comp-a")
    assert len(facts) == 1
    assert facts[0].claim == "NEW"
