"""R1.4 truncated-diff policy tests (H-16) + security strip parity.

Head-truncating a review diff at the prompt cap let a rogue engineer pad
the first 50KB with benign churn and land a malicious hunk after the
cut: both reviewers saw a truncation notice, but hard mode had no
policy, so the unreviewed tail merged. These tests prove the mechanical
policy:

- Oversized diffs split on file boundaries into <=cap chunks; splitting
  never drops content. R8: a single file over the cap splits further on
  hunk boundaries (with its file header repeated on every part) instead
  of failing the component; only a single hunk over the cap is still
  unsplittable, and that fails closed.
- Hard mode runs one review pass per chunk (each pass consumes the
  adversarial budget) and merges verdicts: any chunk failure fails.
- Budget that cannot cover the chunks is an infrastructure failure with
  ZERO passes run - never a partial review, never a silent skip.
- Advisory mode keeps the single truncated pass but the result is
  visibly PARTIAL (flag + injected finding + PR body annotation).
- The security reviewer no longer sees the engineer's Self-Critique
  block (E2 anti-anchoring parity), and the factory strips it once for
  both reviewers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.git import (
    DEFAULT_PROMPT_DIFF_CHAR_LIMIT,
    DiffUnsplittableError,
    split_diff_for_prompt,
)
from kstrl.manifest import Component, Manifest
from kstrl.review import (
    ReviewMode,
    ReviewResult,
    merge_review_results,
    run_chunked_review,
    run_review,
)
from kstrl.security import (
    SecurityConfig,
    SecurityMode,
    SecurityResult,
    merge_security_results,
    run_chunked_security_review,
    run_security_review,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig

UI = PlainUI(no_color=True)

_VERIFICATION = VerificationResult(
    passed=True, checks=[CheckResult("test_suite", True, "ok")],
)


class CountingAgent:
    """Agent that counts invocations, captures prompts, and replies with
    the output at the current call index (last output repeats)."""

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self.calls = 0
        self.prompts: list[str] = []
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "counting-agent"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        idx = min(self.calls, len(self._outputs) - 1)
        self.calls += 1
        self.prompts.append(prompt)
        yield from self._outputs[idx].splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


def _story(story_id: str, verdict: str) -> dict[str, object]:
    return {
        "storyId": story_id,
        "storyTitle": f"Story {story_id}",
        "criteria": [{
            "criterion": "AC1",
            "verdict": verdict,
            "explanation": "checked",
            "suggestion": "",
        }],
    }


def _review_json(verdict: str = "pass") -> str:
    return json.dumps({
        "stories": [_story("US-001", verdict)],
        "concerns": [],
        "exhaustively_searched": True,
        "overallNotes": "",
    })


_CLEAN_SECURITY_JSON = json.dumps({
    "findings": [],
    "exhaustively_searched": True,
    "overallNotes": "",
})


def _write_prd(path: Path, story_ids: list[str]) -> None:
    path.write_text(json.dumps({
        "branchName": "test",
        "userStories": [
            {
                "id": sid, "title": f"Story {sid}",
                "acceptanceCriteria": ["AC1"], "priority": 1,
                "passes": True, "notes": "",
            }
            for sid in story_ids
        ],
    }))


def _file_segment(name: str, payload_chars: int) -> str:
    """One per-file segment of a synthetic unified diff, ~payload_chars
    long."""
    header = (
        f"diff --git a/{name} b/{name}\n"
        f"--- a/{name}\n"
        f"+++ b/{name}\n"
        "@@ -0,0 +1 @@\n"
    )
    line = "+" + "x" * 98 + "\n"
    n_lines = max(1, (payload_chars - len(header)) // 100)
    return header + line * n_lines


def _synthetic_diff(n_files: int, payload_chars: int) -> str:
    return "".join(
        _file_segment(f"src/f{i}.py", payload_chars) for i in range(n_files)
    )


def _multi_hunk_segment(
    name: str, n_hunks: int, hunk_payload_chars: int,
) -> str:
    """One file's diff carrying ``n_hunks`` hunks.

    Models the shape that broke file-boundary chunking in the
    2026-07-27 factory run: a single test file whose diff exceeds the
    per-chunk budget on its own, but which has plenty of internal hunk
    boundaries to split on.
    """
    header = (
        f"diff --git a/{name} b/{name}\n"
        f"--- a/{name}\n"
        f"+++ b/{name}\n"
    )
    line = "+" + "x" * 98 + "\n"
    n_lines = max(1, hunk_payload_chars // 100)
    hunks = "".join(
        f"@@ -{i * 50 + 1},0 +{i * 50 + 1},{n_lines} @@ def test_{i}()\n"
        + line * n_lines
        for i in range(n_hunks)
    )
    return header + hunks


def _hunk_of_size(index: int, size: int) -> str:
    """One hunk of EXACTLY ``size`` chars, starting with a ``@@ `` header.

    Exact sizes are what make the part-marker reserve observable: the
    P3 cases below sit one char either side of the content budget, so an
    approximate fixture would not distinguish "fits" from "does not".
    """
    head = f"@@ -{index},0 +{index},1 @@ h{index}\n"
    rest = size - len(head)
    if rest < 0:
        raise ValueError(f"size {size} is under the {len(head)}-char header")
    line = "+" + "x" * 98 + "\n"
    full, tail = divmod(rest, 100)
    if tail == 1 and full:  # a 1-char tail cannot be a "+...\n" line
        full, tail = full - 1, tail + 100
    text = head + line * full + ("+" + "x" * (tail - 2) + "\n" if tail else "")
    assert len(text) == size, (len(text), size)
    return text


# Inverse of split_diff_for_prompt's injected provenance. The chunk
# header is one line; a within-file part adds a marker line, and a
# continuation part additionally repeats the file header (every line up
# to the part's first "@@ " hunk line).
_CHUNK_HEADER_RE = re.compile(r"^# \[kstrl R1\.4\] diff chunk [^\n]*\n")
_CONTINUED_PART_RE = re.compile(
    r"^# \[kstrl R1\.4\] file part \d+ of \d+: [^\n]*- continued;[^\n]*\n"
    r"(?:(?!@@ )[^\n]*\n)*",
    re.MULTILINE,
)
_FIRST_PART_RE = re.compile(
    r"^# \[kstrl R1\.4\] file part \d+ of \d+: [^\n]*\n", re.MULTILINE,
)


def _reassemble(chunks: list[str]) -> str:
    """Strip every line split_diff_for_prompt injected and concatenate.

    Must reproduce the input byte for byte: chunking is a repackaging,
    never a truncation (R1.4).
    """
    out = []
    for chunk in chunks:
        body = _CHUNK_HEADER_RE.sub("", chunk, count=1)
        body = _CONTINUED_PART_RE.sub("", body)
        out.append(_FIRST_PART_RE.sub("", body))
    return "".join(out)


def _hunk_headers(text: str) -> list[str]:
    """Every ``@@`` hunk header line, in order.

    Hunk headers are unique per file here, so comparing this list
    between the original diff and the raw concatenation of the chunks
    proves each hunk appears exactly once - none dropped, none
    duplicated across chunks.
    """
    return re.findall(r"(?m)^@@ [^\n]*$", text)


_SELF_CRITIQUE_DIFF = """\
diff --git a/scripts/kstrl/progress.txt b/scripts/kstrl/progress.txt
+## Iteration 1 - US-001
+- What I did: added the function
+## Self-Critique
+- Failure mode 1: empty input crashes the parser
+- Failure mode 2: concurrent writes race
+---
+
diff --git a/src/x.py b/src/x.py
+def add(a, b): return a + b
"""


# ---------------------------------------------------------------------------
# split_diff_for_prompt mechanics
# ---------------------------------------------------------------------------


class TestSplitDiffForPrompt:
    def test_small_diff_returned_unchanged(self) -> None:
        diff = _synthetic_diff(2, 300)
        assert split_diff_for_prompt(diff, limit=5000) == [diff]

    def test_oversized_diff_splits_on_file_boundaries(self) -> None:
        diff = _synthetic_diff(10, 300)
        chunks = split_diff_for_prompt(diff, limit=1000)
        assert len(chunks) >= 2
        for i, chunk in enumerate(chunks, 1):
            assert len(chunk) <= 1000
            assert chunk.startswith(
                f"# [kstrl R1.4] diff chunk {i} of {len(chunks)}"
            )
        # Reassembly invariant: dropping each chunk's header line
        # reproduces the input exactly - chunking never loses content.
        reassembled = "".join(c.split("\n", 1)[1] for c in chunks)
        assert reassembled == diff
        # Every file boundary survives exactly once.
        for i in range(10):
            assert reassembled.count(f"diff --git a/src/f{i}.py") == 1

    def test_preamble_before_first_boundary_is_kept(self) -> None:
        diff = "binary files differ notice\n" + _synthetic_diff(6, 300)
        chunks = split_diff_for_prompt(diff, limit=1000)
        reassembled = "".join(c.split("\n", 1)[1] for c in chunks)
        assert reassembled == diff

    def test_single_oversized_hunk_raises(self) -> None:
        """One hunk over the budget is the floor: a diff cannot be split
        below hunk granularity, and R1.4 forbids truncating a diff that
        is under review, so it must fail closed."""
        diff = _file_segment("src/big.py", 3000)  # one file, ONE hunk
        assert len(_hunk_headers(diff)) == 1
        with pytest.raises(DiffUnsplittableError, match="single hunk"):
            split_diff_for_prompt(diff, limit=1000)

    def test_oversized_file_without_hunks_raises(self) -> None:
        diff = (
            "diff --git a/x.bin b/x.bin\nBinary files differ\n" + "x" * 2000
        )
        with pytest.raises(DiffUnsplittableError, match="no '@@ ' hunk"):
            split_diff_for_prompt(diff, limit=1000)

    def test_no_file_boundaries_raises(self) -> None:
        with pytest.raises(DiffUnsplittableError, match="no 'diff --git'"):
            split_diff_for_prompt("x" * 2000, limit=1000)

    def test_limit_must_exceed_header_reserve(self) -> None:
        with pytest.raises(ValueError, match="header"):
            split_diff_for_prompt("x", limit=100)

    def test_multi_file_packing_is_unchanged_by_within_file_splitting(
        self,
    ) -> None:
        """R8 regression guard: diffs that already split cleanly on file
        boundaries must chunk EXACTLY as before - same chunk count, same
        pre-R8 header wording, whole files only, byte-exact reassembly.
        Within-file splitting must not perturb the common path."""
        diff = _synthetic_diff(10, 300)
        chunks = split_diff_for_prompt(diff, limit=1000)
        # Golden values captured from the pre-R8 implementation.
        assert len(chunks) == 4
        assert [len(c) for c in chunks] == [950, 950, 950, 388]
        for i, chunk in enumerate(chunks, 1):
            assert chunk.startswith(
                f"# [kstrl R1.4] diff chunk {i} of 4: oversized diff split "
                "on file boundaries; other files are in other chunks\n"
            )
            # Whole files only: no within-file part markers anywhere.
            assert "file part" not in chunk
            assert chunk.split("\n", 1)[1].startswith("diff --git ")
        assert "".join(c.split("\n", 1)[1] for c in chunks) == diff


class TestSplitWithinFile:
    """R8: a single file whose diff exceeds the per-chunk budget used to
    raise DiffUnsplittableError and fail the component, even though the
    file had many hunks to split on. In the 2026-07-27 factory run that
    cost a full engineer-loop pass ($3.99) to repackage a 55,710-char
    test file - a harness packaging limit charged to the engineer, not a
    defect in the code under review. Split within the file instead.
    """

    def _production_shape(self) -> str:
        """~55.7KB in one file, the size that halted the real run."""
        diff = _multi_hunk_segment("tests/test_purity.py", 12, 4600)
        assert len(diff) > DEFAULT_PROMPT_DIFF_CHAR_LIMIT
        return diff

    def test_single_oversized_file_now_splits_on_hunks(self) -> None:
        diff = self._production_shape()
        chunks = split_diff_for_prompt(diff)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= DEFAULT_PROMPT_DIFF_CHAR_LIMIT

    def test_within_file_chunks_reassemble_byte_exactly(self) -> None:
        diff = self._production_shape()
        assert _reassemble(split_diff_for_prompt(diff)) == diff

    def test_every_hunk_appears_exactly_once(self) -> None:
        """Round-trip property: the chunks cover every hunk of the
        original diff, in order, with nothing dropped or duplicated."""
        diff = self._production_shape()
        chunks = split_diff_for_prompt(diff)
        original = _hunk_headers(diff)
        assert len(original) == 12
        assert _hunk_headers("".join(chunks)) == original

    def test_every_part_repeats_the_file_header(self) -> None:
        """A reviewer holding part 2 must still know which file it is
        looking at: the diff --git / --- / +++ header is repeated on
        every part."""
        diff = self._production_shape()
        chunks = split_diff_for_prompt(diff)
        for chunk in chunks:
            assert (
                "diff --git a/tests/test_purity.py b/tests/test_purity.py"
                in chunk
            )
            assert "--- a/tests/test_purity.py" in chunk
            assert "+++ b/tests/test_purity.py" in chunk

    def test_every_part_is_labelled_with_file_and_part_number(self) -> None:
        """A part must not read as the file's whole change."""
        diff = self._production_shape()
        chunks = split_diff_for_prompt(diff)
        n = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            assert (
                f"# [kstrl R1.4] file part {i} of {n}: tests/test_purity.py"
                in chunk
            )
            # The chunk header states the true split granularity.
            assert chunk.startswith(
                f"# [kstrl R1.4] diff chunk {i} of {n}: oversized diff "
                "split on file/hunk boundaries; the rest is in other "
                "chunks\n"
            )
        # Continuation parts flag their repeated header as a repeat, so
        # it is not reviewed as a second change to the same file.
        assert "not a second change" not in chunks[0]
        for chunk in chunks[1:]:
            assert "not a second change" in chunk

    def test_mixed_diff_splits_only_the_oversized_file(self) -> None:
        """Small files stay whole; only the over-budget one is parted."""
        diff = (
            _file_segment("src/small_a.py", 300)
            + _multi_hunk_segment("src/big.py", 8, 400)
            + _file_segment("src/small_b.py", 300)
        )
        chunks = split_diff_for_prompt(diff, limit=1500)
        joined = "".join(chunks)
        assert joined.count("file part 1 of") == 1
        assert "src/small_a.py -" not in joined  # never parted
        assert "src/small_b.py -" not in joined
        assert _reassemble(chunks) == diff
        assert _hunk_headers(joined) == _hunk_headers(diff)
        for chunk in chunks:
            assert len(chunk) <= 1500

    def test_hundred_hunk_file_splits_and_reassembles(self) -> None:
        """Many parts: part numbering stays within the header reserve
        and every chunk still fits the limit."""
        diff = _multi_hunk_segment("src/huge.py", 100, 2000)
        chunks = split_diff_for_prompt(diff, limit=5000)
        assert len(chunks) >= 40
        for chunk in chunks:
            assert len(chunk) <= 5000
        assert _reassemble(chunks) == diff
        assert _hunk_headers("".join(chunks)) == _hunk_headers(diff)

    def test_chunks_are_still_capped_at_the_default_limit(self) -> None:
        """The fix must not paper over the cap by raising the budget."""
        assert DEFAULT_PROMPT_DIFF_CHAR_LIMIT == 50_000
        chunks = split_diff_for_prompt(self._production_shape())
        assert max(len(c) for c in chunks) <= DEFAULT_PROMPT_DIFF_CHAR_LIMIT


class TestPartMarkerWidthFromPartCount:
    """[P3] The ``file part i of n`` marker's digit width must be sized
    from the ACTUAL part count, not from the hunk count.

    The hunk count is only an UPPER BOUND on the part count, so reserving
    its width shrinks the per-part content budget by more than the marker
    will ever need. A hunk that fits an exact rendering then gets
    rejected and the harness forces the engineer retry this PR exists to
    eliminate.
    """

    PATH = "xx.py"
    HEADER = (
        f"diff --git a/{PATH} b/{PATH}\n--- a/{PATH}\n+++ b/{PATH}\n"
    )

    def _reviewer_case(self) -> str:
        """The reviewer's reproduction: ONE 49,706-char hunk followed by
        999 tiny hunks, at the default 50,000-char limit.

        49,706 is the knife edge: it is exactly the content budget left
        for a 1-digit part count, and 6 chars over the budget left once
        the marker is (wrongly) sized for 1,000 parts.
        """
        diff = (
            self.HEADER
            + _hunk_of_size(1, 49_706)
            + "".join(_hunk_of_size(i + 2, 30) for i in range(999))
        )
        assert len(_hunk_headers(diff)) == 1000
        return diff

    def test_reviewer_case_splits_instead_of_raising(self) -> None:
        """Before the fix this raised DiffUnsplittableError and bounced
        the component back to the engineer; a compliant hunk-boundary
        partition existed the whole time."""
        diff = self._reviewer_case()
        chunks = split_diff_for_prompt(diff)
        assert len(chunks) == 2
        for chunk in chunks:
            assert len(chunk) <= DEFAULT_PROMPT_DIFF_CHAR_LIMIT
        # The big hunk is alone in part 1; the 999 tiny ones follow.
        assert _hunk_headers(chunks[0]) == _hunk_headers(diff)[:1]
        assert _hunk_headers(chunks[1]) == _hunk_headers(diff)[1:]

    def test_reviewer_case_round_trips(self) -> None:
        """The round-trip property must hold for the newly-splittable
        case too: every hunk exactly once, in order, byte-exact."""
        diff = self._reviewer_case()
        chunks = split_diff_for_prompt(diff)
        assert _reassemble(chunks) == diff
        assert _hunk_headers("".join(chunks)) == _hunk_headers(diff)

    def test_reviewer_case_parts_are_labelled_and_self_describing(
        self,
    ) -> None:
        diff = self._reviewer_case()
        chunks = split_diff_for_prompt(diff)
        for i, chunk in enumerate(chunks, 1):
            assert f"file part {i} of 2: {self.PATH}" in chunk
            assert self.HEADER in chunk

    def test_marker_width_comes_from_parts_not_hunks(self) -> None:
        """Same failure mode without the knife edge: 100 hunks that pack
        into 50 parts. Sizing the marker from the 3-digit hunk count
        costs 2 chars per part - just enough to stop two hunks sharing a
        part, doubling the reviewer passes (100 chunks instead of 50).
        """
        diff = self.HEADER + "".join(
            _hunk_of_size(i + 1, 2352) for i in range(100)
        )
        chunks = split_diff_for_prompt(diff, limit=5000)
        assert len(chunks) == 50
        for chunk in chunks:
            assert len(chunk) <= 5000
        assert _reassemble(chunks) == diff
        assert _hunk_headers("".join(chunks)) == _hunk_headers(diff)

    def test_non_convergence_fails_closed_instead_of_looping(self) -> None:
        """The part-count fixed point provably settles (at most one round
        per digit width), but the loop is bounded anyway. If that bound
        is ever reached the diff must fail closed like any other
        unsplittable diff - never spin, never emit parts whose markers
        disagree with the rendered part count."""
        diff = self.HEADER + "".join(
            _hunk_of_size(i + 1, 2352) for i in range(100)
        )
        # This shape needs two rounds (assume 1 part -> pack 50 -> 50),
        # so a one-round budget cannot settle it.
        with patch("kstrl.git._PART_COUNT_FIXED_POINT_ROUNDS", 1):
            with pytest.raises(DiffUnsplittableError, match="did not settle"):
                split_diff_for_prompt(diff, limit=5000)

    def test_single_oversized_hunk_among_many_still_raises(self) -> None:
        """The residual floor is unchanged: a hunk over the budget that
        even a 1-part rendering could not hold still fails closed. The
        fix must not paper over it by shrinking the marker away."""
        diff = self.HEADER + _hunk_of_size(1, 60_000) + "".join(
            _hunk_of_size(i + 2, 30) for i in range(999)
        )
        with pytest.raises(DiffUnsplittableError, match="single hunk"):
            split_diff_for_prompt(diff)


# ---------------------------------------------------------------------------
# merge semantics
# ---------------------------------------------------------------------------


class TestMergeResults:
    def _result(self, passed: bool, **kwargs: object) -> ReviewResult:
        return ReviewResult(passed=passed, mode="hard", **kwargs)  # type: ignore[arg-type]

    def test_all_chunks_pass_merges_to_pass(self) -> None:
        merged = merge_review_results(
            [self._result(True), self._result(True)], "hard",
        )
        assert merged.passed is True
        assert merged.infrastructure_error is False
        assert "Chunked review: 2 passes" in merged.overall_notes

    def test_any_chunk_failure_fails(self) -> None:
        merged = merge_review_results(
            [self._result(True), self._result(False)], "hard",
        )
        assert merged.passed is False

    def test_findings_concatenate(self) -> None:
        from kstrl.review import CriterionReview, ReviewConcern
        a = self._result(True)
        a.criteria.append(CriterionReview("AC1", "pass", "ok"))
        b = self._result(False)
        b.concerns.append(ReviewConcern(
            "dead_code", "fail", "x.py:1", "unused",
        ))
        merged = merge_review_results([a, b], "hard")
        assert len(merged.criteria) == 1
        assert len(merged.concerns) == 1

    def test_chunk_infra_error_marks_merged_infra(self) -> None:
        merged = merge_review_results(
            [
                self._result(True),
                self._result(False, infrastructure_error=True),
            ],
            "hard",
        )
        assert merged.infrastructure_error is True
        assert merged.passed is False

    def test_exhaustive_hint_requires_all_chunks(self) -> None:
        merged = merge_review_results(
            [
                self._result(True, exhaustively_searched=True),
                self._result(True, exhaustively_searched=False),
            ],
            "hard",
        )
        assert merged.exhaustively_searched is False

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError):
            merge_review_results([], "hard")
        with pytest.raises(ValueError):
            merge_security_results([], "hard")

    def test_security_merge_mirrors_policy(self) -> None:
        from kstrl.security import SecurityFinding
        a = SecurityResult(passed=True, mode="hard")
        a.findings.append(SecurityFinding(
            "injection", "low", "x.py:1", "meh",
        ))
        b = SecurityResult(
            passed=False, mode="hard", infrastructure_error=True,
        )
        merged = merge_security_results([a, b], "hard")
        assert merged.passed is False
        assert merged.infrastructure_error is True
        assert len(merged.findings) == 1


# ---------------------------------------------------------------------------
# chunked runners: one pass per chunk, budget rules
# ---------------------------------------------------------------------------


class TestRunChunkedReview:
    def _prd(self, tmp_path: Path) -> Path:
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        return prd

    def test_one_pass_per_chunk_and_merge(self, tmp_path: Path) -> None:
        agent = CountingAgent([_review_json("pass")])
        consumed = {"n": 0}

        def consume() -> None:
            consumed["n"] += 1

        chunks = ["chunk-a", "chunk-b", "chunk-c"]
        result = run_chunked_review(
            agent, self._prd(tmp_path), tmp_path, "main",
            _VERIFICATION, ReviewMode.HARD, UI,
            diff_chunks=chunks,
            budget_remaining=3,
            consume_budget=consume,
        )
        assert agent.calls == 3
        assert consumed["n"] == 3
        assert result.passed is True
        assert len(result.criteria) == 3  # US-001 verdict per chunk
        # Each pass saw exactly its own chunk.
        for chunk, prompt in zip(chunks, agent.prompts, strict=True):
            assert chunk in prompt

    def test_failing_chunk_fails_merged_verdict(self, tmp_path: Path) -> None:
        agent = CountingAgent([
            _review_json("pass"), _review_json("fail"), _review_json("pass"),
        ])
        result = run_chunked_review(
            agent, self._prd(tmp_path), tmp_path, "main",
            _VERIFICATION, ReviewMode.HARD, UI,
            diff_chunks=["a", "b", "c"],
        )
        assert agent.calls == 3
        assert result.passed is False
        assert result.infrastructure_error is False

    def test_insufficient_budget_is_infra_fail_with_zero_passes(
        self, tmp_path: Path,
    ) -> None:
        agent = CountingAgent([_review_json("pass")])
        result = run_chunked_review(
            agent, self._prd(tmp_path), tmp_path, "main",
            _VERIFICATION, ReviewMode.HARD, UI,
            diff_chunks=["a", "b", "c"],
            budget_remaining=2,
        )
        assert agent.calls == 0
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "refusing" in result.overall_notes

    def test_security_chunked_runner_mirrors(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("spec")
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        config = SecurityConfig(mode=SecurityMode.HARD.value)
        result = run_chunked_security_review(
            agent, prd, tmp_path, "main", config, UI,
            diff_chunks=["a", "b"],
            budget_remaining=2,
        )
        assert agent.calls == 2
        assert result.passed is True

        starved = CountingAgent([_CLEAN_SECURITY_JSON])
        result = run_chunked_security_review(
            starved, prd, tmp_path, "main", config, UI,
            diff_chunks=["a", "b"],
            budget_remaining=1,
        )
        assert starved.calls == 0
        assert result.passed is False
        assert result.infrastructure_error is True


# ---------------------------------------------------------------------------
# advisory mode: single truncated pass, visibly PARTIAL
# ---------------------------------------------------------------------------


class TestAdvisoryPartial:
    def _oversized(self) -> str:
        return _synthetic_diff(3, DEFAULT_PROMPT_DIFF_CHAR_LIMIT // 2)

    def test_advisory_review_is_marked_partial(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        agent = CountingAgent([_review_json("pass")])
        result = run_review(
            agent, prd, tmp_path, "main", _VERIFICATION,
            ReviewMode.ADVISORY, UI,
            diff_content=self._oversized(),
        )
        assert result.passed is True
        assert result.partial is True
        partial_concerns = [
            c for c in result.concerns if "Partial review" in c.explanation
        ]
        assert len(partial_concerns) == 1
        assert partial_concerns[0].severity == "advisory"
        assert "PARTIAL REVIEW" in result.as_pr_body_section()

    def test_fitting_diff_is_not_partial(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        agent = CountingAgent([_review_json("pass")])
        result = run_review(
            agent, prd, tmp_path, "main", _VERIFICATION,
            ReviewMode.ADVISORY, UI,
            diff_content="+small diff\n",
        )
        assert result.partial is False
        assert "PARTIAL" not in result.as_pr_body_section()

    def test_hard_review_backstop_fails_closed(self, tmp_path: Path) -> None:
        """Direct hard-mode call with an unchunked oversized diff must
        never pass (the factory chunks; this guards other callers)."""
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        agent = CountingAgent([_review_json("pass")])
        result = run_review(
            agent, prd, tmp_path, "main", _VERIFICATION,
            ReviewMode.HARD, UI,
            diff_content=self._oversized(),
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "without chunking" in result.overall_notes

    def test_advisory_security_is_marked_partial(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("spec")
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        result = run_security_review(
            agent, prd, tmp_path, "main", config, UI,
            diff_content=self._oversized(),
        )
        assert result.passed is True
        assert result.partial is True
        markers = [
            f for f in result.findings
            if "Partial security review" in f.explanation
        ]
        assert len(markers) == 1
        assert markers[0].severity == "low"
        assert "PARTIAL SECURITY REVIEW" in result.as_pr_body_section()

    def test_hard_security_backstop_fails_closed(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("spec")
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        config = SecurityConfig(mode=SecurityMode.HARD.value)
        result = run_security_review(
            agent, prd, tmp_path, "main", config, UI,
            diff_content=self._oversized(),
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "without chunking" in result.overall_notes


# ---------------------------------------------------------------------------
# security parity: the Self-Critique block never reaches either prompt
# ---------------------------------------------------------------------------


class TestSelfCritiqueStripParity:
    def test_security_prompt_contains_no_self_critique(
        self, tmp_path: Path,
    ) -> None:
        """String-level proof on the BUILT prompt via the fetch-fallback
        path (diff_content=None)."""
        prd = tmp_path / "prd.json"
        prd.write_text("spec")
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        with patch(
            "kstrl.git.get_diff_content",
            return_value=_SELF_CRITIQUE_DIFF,
        ):
            run_security_review(agent, prd, tmp_path, "main", config, UI)
        assert len(agent.prompts) == 1
        assert "Self-Critique" not in agent.prompts[0]
        assert "Failure mode 1" not in agent.prompts[0]
        # The actual code change still reaches the reviewer.
        assert "def add(a, b)" in agent.prompts[0]

    def test_review_prompt_fallback_still_strips(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        agent = CountingAgent([_review_json("pass")])
        with patch(
            "kstrl.git.get_diff_content",
            return_value=_SELF_CRITIQUE_DIFF,
        ):
            run_review(
                agent, prd, tmp_path, "main", _VERIFICATION,
                ReviewMode.ADVISORY, UI,
            )
        assert len(agent.prompts) == 1
        assert "Self-Critique" not in agent.prompts[0]
        assert "def add(a, b)" in agent.prompts[0]


# ---------------------------------------------------------------------------
# factory wiring: chunk orchestration, budget accounting, strip-once
# ---------------------------------------------------------------------------


def _scaffold(tmp_path: Path, comp_ids: list[str]) -> Path:
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
    (tmp_path / "scripts" / "kstrl" / "prompt.md").write_text("p")
    (tmp_path / "scripts" / "kstrl" / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    for comp_id in comp_ids:
        feature_dir = tmp_path / "scripts" / "kstrl" / "feature" / comp_id
        feature_dir.mkdir(parents=True)
        _write_prd(feature_dir / "prd.json", ["US-001"])
    return tmp_path


def _make_manifest(ids: list[str]) -> Manifest:
    return Manifest(
        version="1", spec_file="s", project_name="t",
        base_branch="main", single_pr=False,
        components=[
            Component(
                id=i, title=i, description="", dependencies=[],
                prd_path=f"scripts/kstrl/feature/{i}/prd.json",
                branch_name=f"kstrl/{i}",
            )
            for i in ids
        ],
    )


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts/kstrl/prompt.md",
        prd_file=root / "scripts/kstrl/prd.json",
        sleep_seconds=0, agent_cmd="echo test",
        kstrl_branch="", kstrl_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


def _factory_config(**overrides: object) -> FactoryConfig:
    defaults: dict[str, object] = dict(
        use_worktrees=False, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=5.0,
        ),
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)  # type: ignore[arg-type]


def _read_events(log_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# Three ~40KB files: over the 50KB cap, and each file fits a chunk, so
# greedy packing yields exactly 3 chunks of one file each.
def _oversized_component_diff() -> str:
    return _synthetic_diff(3, 40_000)


class TestFactoryChunkedReview:
    def test_hard_mode_oversized_diff_runs_one_pass_per_chunk(
        self, tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        config = _factory_config(
            review_mode="hard", max_adversarial_calls=3,
            progress_log_path=log_path,
        )
        agent = CountingAgent([_review_json("pass")])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content",
            return_value=_oversized_component_diff(),
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.completed
        assert agent.calls == 3
        events = _read_events(log_path)
        chunk_events = [e for e in events if e["event"] == "diff_chunked"]
        assert len(chunk_events) == 1
        assert chunk_events[0]["data"]["chunks"] == 3  # type: ignore[index]

    def test_insufficient_budget_fails_component_without_retry(
        self, tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        # 3 chunks needed, budget 2; retries available but must NOT be
        # used - the budget can only shrink, so retrying is pure waste.
        config = _factory_config(
            review_mode="hard", max_adversarial_calls=2, max_retries=2,
            progress_log_path=log_path,
        )
        agent = CountingAgent([_review_json("pass")])
        run_component_calls = {"n": 0}

        def fake_run_component(
            comp_id: str, *a: object, **k: object,
        ) -> ComponentResult:
            run_component_calls["n"] += 1
            return ComponentResult(comp_id, success=True, iterations=1)

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content",
            return_value=_oversized_component_diff(),
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.failed
        assert agent.calls == 0
        assert run_component_calls["n"] == 1
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.error is not None
        assert "Review infrastructure error" in comp.error
        infra = [
            f for f in comp.findings
            if f.is_infrastructure_error and f.phase == "review"
        ]
        assert len(infra) == 1
        assert "refusing" in infra[0].explanation
        events = _read_events(log_path)
        assert any(
            e["event"] == "chunk_budget_insufficient" for e in events
        )

    def test_security_hard_mode_chunks_too(self, tmp_path: Path) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config(
            review_mode="skip", max_adversarial_calls=3,
            security_config=SecurityConfig(mode=SecurityMode.HARD.value),
        )
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content",
            return_value=_oversized_component_diff(),
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.completed
        assert agent.calls == 3

    def test_oversized_single_file_is_reviewed_not_bounced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R8 end-to-end: the production shape (one 55KB test file with
        many hunks) used to fail the component and buy a full engineer
        retry ($3.99 measured). It must now chunk and review instead."""
        # Knowledge distillation off: it would spend another agent call
        # and put a second copy of the diff in agent.prompts.
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        config = _factory_config(
            review_mode="hard", max_adversarial_calls=5,
            progress_log_path=log_path,
        )
        one_big_file = _multi_hunk_segment("tests/test_purity.py", 12, 4600)
        assert len(one_big_file) > DEFAULT_PROMPT_DIFF_CHAR_LIMIT
        agent = CountingAgent([_review_json("pass")])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content", return_value=one_big_file,
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.completed
        events = _read_events(log_path)
        assert not any(e["event"] == "diff_unsplittable" for e in events)
        chunk_events = [e for e in events if e["event"] == "diff_chunked"]
        assert len(chunk_events) == 1
        n_chunks = chunk_events[0]["data"]["chunks"]  # type: ignore[index]
        assert n_chunks >= 2
        # One review pass per chunk, every hunk seen by a reviewer.
        assert agent.calls == n_chunks
        seen = _hunk_headers("".join(agent.prompts))
        assert seen == _hunk_headers(one_big_file)

    def test_unsplittable_diff_fails_closed(self, tmp_path: Path) -> None:
        """Residual floor: one hunk over the cap still fails closed via
        the retry path (R1.4 - an unreviewable diff must not merge)."""
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        config = _factory_config(
            review_mode="hard", progress_log_path=log_path,
        )
        # One file, ONE hunk, bigger than the cap: nothing to split on.
        big_single_file = _file_segment(
            "src/huge.py", DEFAULT_PROMPT_DIFF_CHAR_LIMIT + 10_000,
        )
        assert len(_hunk_headers(big_single_file)) == 1
        agent = CountingAgent([_review_json("pass")])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content", return_value=big_single_file,
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.failed
        assert agent.calls == 0
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.error is not None
        assert "unsplittable" in comp.error
        events = _read_events(log_path)
        assert any(e["event"] == "diff_unsplittable" for e in events)


class TestSinglePassSecurityBudget:
    def test_single_pass_security_consumes_exactly_one_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: the R1.4 refactor briefly double-consumed the
        budget for non-chunked security passes, which would silently
        halve the effective budget. Two components + budget 2 must both
        get their security pass."""
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = _scaffold(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest(["comp-a", "comp-b"])
        config = _factory_config(
            review_mode="skip", max_adversarial_calls=2,
            security_config=SecurityConfig(
                mode=SecurityMode.ADVISORY.value,
            ),
        )
        agent = CountingAgent([_CLEAN_SECURITY_JSON])

        def fake_run_component(
            comp_id: str, *a: object, **k: object,
        ) -> ComponentResult:
            return ComponentResult(comp_id, success=True, iterations=1)

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content", return_value="+small\n",
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert set(result.completed) == {"comp-a", "comp-b"}
        assert agent.calls == 2
        for comp_id in ("comp-a", "comp-b"):
            comp = manifest.get_component(comp_id)
            assert comp is not None
            assert not any(
                f.is_phase_skip and f.phase == "security"
                for f in comp.findings
            )


class TestFactoryStripOnce:
    def test_both_reviewers_receive_stripped_diff(
        self, tmp_path: Path,
    ) -> None:
        """R1.4 requirement 3: the factory strips the Self-Critique
        block once and shares the result with Phase 2 AND Phase 2.5."""
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config(
            review_mode="advisory", max_adversarial_calls=2,
            security_config=SecurityConfig(
                mode=SecurityMode.ADVISORY.value,
            ),
        )
        captured: dict[str, str] = {}

        def fake_run_review(
            *args: object, **kwargs: object,
        ) -> ReviewResult:
            captured["review"] = str(kwargs.get("diff_content"))
            return ReviewResult(passed=True, mode="advisory")

        def fake_run_security(
            *args: object, **kwargs: object,
        ) -> SecurityResult:
            captured["security"] = str(kwargs.get("diff_content"))
            return SecurityResult(passed=True, mode="advisory")

        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.factory.run_review", side_effect=fake_run_review,
        ), patch(
            "kstrl.factory.run_security_review",
            side_effect=fake_run_security,
        ), patch(
            "kstrl.git.get_diff_content",
            return_value=_SELF_CRITIQUE_DIFF,
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.completed
        assert "Self-Critique" not in captured["review"]
        assert "Self-Critique" not in captured["security"]
        # The real change survives the strip for both reviewers.
        assert "def add(a, b)" in captured["review"]
        assert "def add(a, b)" in captured["security"]
