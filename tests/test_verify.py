"""Tests for verify module."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from kstrl.verify import (
    MechanicalVerification,
    check_prd_stories,
    check_self_critique,
    run_mechanical_verification,
)
from kstrl.verify_diff import check_bad_patterns
from kstrl.verify_model import VerifyConfig
from tests.conftest import make_review_repo
from tests.helpers.stack_confirmation import in_process_stack


class TestCheckPrdStories:
    def test_all_passing(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        result = check_prd_stories(prd)
        assert result.passed is True

    def test_story_not_passing(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": False,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        result = check_prd_stories(prd)
        assert result.passed is False
        assert "US-001" in result.details[0]

    def test_invalid_prd(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("not json")
        result = check_prd_stories(prd)
        assert result.passed is False
        assert "Failed to load" in result.message

    def test_empty_stories(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [],
                }
            )
        )
        result = check_prd_stories(prd)
        assert result.passed is True


class TestCheckBadPatterns:
    """Built on ``make_review_repo`` (#399 simplify pass on #405, C1) rather
    than a local ``_repo``/``_commit`` pair: that helper already builds a
    base-then-branch repository under a real identity and is imported by
    eight other modules, so this class adds no row of its own to
    ``tests/test_git_identity.py``'s per-file census.
    """

    def test_clean_files(self, tmp_path: Path) -> None:
        repo = make_review_repo(tmp_path, files={"clean.py": "x = 1\n"})

        result = check_bad_patterns(repo.path, repo.base_branch)
        assert result.passed is True
        assert result.message == "secrets: scanned the lines added to 1 changed files, no issues"

    def test_secret_detected(self, tmp_path: Path) -> None:
        repo = make_review_repo(
            tmp_path,
            files={"leak.py": 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz"\n'},
        )

        result = check_bad_patterns(repo.path, repo.base_branch)
        assert result.passed is False
        assert any("secret" in d.lower() for d in result.details)


class TestRunMechanicalVerification:
    def test_all_pass(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        config = VerifyConfig(
            project_stack=in_process_stack({"tests": "true", "typecheck": "true", "lint": "true"}),
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )
        result = run_mechanical_verification(
            tmp_path,
            prd,
            "main",
            None,
            config,
        )
        assert result.passed is True
        assert len(result.checks) == 4  # prd + test + typecheck + lint

    def test_partial_failure(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        config = VerifyConfig(
            project_stack=in_process_stack(
                {"tests": "false", "typecheck": "true", "lint": "true"}
            ),  # Tests fail
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )
        result = run_mechanical_verification(
            tmp_path,
            prd,
            "main",
            None,
            config,
        )
        assert result.passed is False
        # All checks should have run (no short-circuit)
        assert len(result.checks) == 4
        assert result.checks[0].passed is True  # PRD stories
        assert result.checks[1].passed is False  # Test suite
        assert result.checks[2].passed is True  # Typecheck


class TestCheckSelfCritique:
    """Tests for the engineer-prompt self-critique mechanical check."""

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        result = check_self_critique(tmp_path / "missing.txt")
        assert result.passed is False
        assert "Could not read" in result.message

    def test_no_block_fails(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n- did stuff\n- ran tests\n",
        )
        result = check_self_critique(progress)
        assert result.passed is False
        assert "No '## Self-Critique'" in result.message

    def test_block_with_three_bullets_passes(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n"
            "- did stuff\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: invalid input crashes parser\n"
            "  - Failure mode 2: concurrent writes race\n"
            "  - Failure mode 3: timeout swallowed silently\n"
        )
        result = check_self_critique(progress)
        assert result.passed is True
        assert "3 failure modes" in result.message

    def test_fewer_than_min_fails(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: x\n"
            "  - Failure mode 2: y\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False
        assert "2 bullets" in result.message

    def test_tbd_bullets_dont_count(self, tmp_path: Path) -> None:
        """The check should reject placeholder content like TBD/TODO/N/A."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "- **Self-Critique:**\n"
            "  - TBD\n"
            "  - TODO write later\n"
            "  - N/A\n"
            "  - Failure mode: empty input crashes the parser\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False  # only 1 substantive bullet

    def test_latest_iteration_block_used(self, tmp_path: Path) -> None:
        """Multiple Self-Critique blocks in one file - the LAST one is
        evaluated so previous iterations don't carry the current one."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "- **Self-Critique:**\n"
            "  - mode1: x\n"
            "  - mode2: y\n"
            "  - mode3: z\n"
            "\n## Iteration 2\n"
            "- **Self-Critique:**\n"
            "  - only one this time\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False  # latest iteration has only 1

    def test_h2_style_heading_recognized(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "Some narrative.\n"
            "## Self-Critique\n"
            "- failure A: detailed reason\n"
            "- failure B: detailed reason\n"
            "- failure C: detailed reason\n"
        )
        result = check_self_critique(progress)
        assert result.passed is True

    def test_does_not_match_self_critique_in_prose(self, tmp_path: Path) -> None:
        """A reference to 'self-critique' in body text must not be
        treated as the heading. Only proper headings count."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "I wrote a self-critique that lists three failure modes:\n"
            "- mode A\n"
            "- mode B\n"
            "- mode C\n"
            "\nDone.\n"
        )
        result = check_self_critique(progress)
        assert result.passed is False
        assert "No '## Self-Critique'" in result.message

    def test_fuzz_corpus_of_accepted_headings(self, tmp_path: Path) -> None:
        """Forms the engineer prompt's loose phrasing might produce."""
        accepted = [
            "## Self-Critique",
            "## self-critique",  # case-insensitive
            "### Self-Critique",  # H3 also OK
            "- **Self-Critique:**",
            "- **Self-Critique**",
            "* **Self-Critique:**",
            "## Self Critique",  # space instead of hyphen
        ]
        for heading in accepted:
            progress = tmp_path / "progress.txt"
            progress.write_text(
                f"## Iteration 1\nbody\n{heading}\n"
                "- failure 1: realistic description with details\n"
                "- failure 2: realistic description with details\n"
                "- failure 3: realistic description with details\n"
            )
            result = check_self_critique(progress)
            assert result.passed is True, f"heading {heading!r} should be accepted"

    def test_fuzz_corpus_of_rejected_lines(self, tmp_path: Path) -> None:
        """Lines that mention self-critique but aren't a heading."""
        rejected = [
            "the self-critique below lists failure modes",
            "self-critique: yes",  # no leading marker
            "**self-critique:**",  # bare bold, no list marker
            "selfcritique",  # no separator
            "see Self-Critique above",
        ]
        for line in rejected:
            progress = tmp_path / "progress.txt"
            progress.write_text(
                f"## Iteration 1\n{line}\n"
                "- failure: realistic\n"
                "- failure: realistic\n"
                "- failure: realistic\n"
            )
            result = check_self_critique(progress)
            assert result.passed is False, f"line {line!r} should not be treated as heading"

    def test_missing_block_in_latest_iteration_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """R5.4 regression: an earlier iteration's Self-Critique block
        must not satisfy the check when the LATEST entry omits it."""
        earlier_entries = [
            # Documented '## [YYYY-MM-DD] - [Story ID]' form
            "## [2026-07-17] - US-001\n",
            # Unbracketed date form
            "## 2026-07-17 - US-001\n",
            # Loose 'Iteration N' form
            "## Iteration 1\n",
        ]
        for earlier in earlier_entries:
            progress = tmp_path / "progress.txt"
            progress.write_text(
                f"{earlier}"
                "- implemented the parser\n"
                "- **Self-Critique:**\n"
                "  - Failure mode 1: realistic detail\n"
                "  - Failure mode 2: realistic detail\n"
                "  - Failure mode 3: realistic detail\n"
                "---\n"
                "## [2026-07-18] - US-002\n"
                "- implemented the serializer\n"
                "- ran the tests\n"
                "---\n"
            )
            result = check_self_critique(progress, min_bullets=3)
            assert result.passed is False, (
                f"earlier entry {earlier!r} must not mask the latest entry's missing block"
            )
            assert "latest iteration entry" in result.message

    def test_next_section_bullets_do_not_inflate_count(
        self,
        tmp_path: Path,
    ) -> None:
        """R5.4 regression: bullets belonging to a FOLLOWING bold-label
        section (e.g. Interpretations in the engineer prompt's format)
        must not count toward min_bullets."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## [2026-07-18] - US-002\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: only one substantive failure mode\n"
            "- **Interpretations** (PRD was ambiguous): assumed idempotency\n"
            "- **Learnings:**\n"
            "  - discovered a pattern\n"
            "  - hit a gotcha\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False
        assert "1 bullets" in result.message

    def test_default_prompt_entry_format_counts_exact_bullets(
        self,
        tmp_path: Path,
    ) -> None:
        """Positive control: a full entry in the engineer prompt's
        documented Progress Format passes with EXACTLY the critique
        bullets counted - surrounding sections contribute nothing."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "# kstrl Progress Log\n\n"
            "## Codebase Patterns\n"
            "- (add reusable patterns here)\n\n"
            "## Iteration Notes\n"
            "- (append entries below using the format in prompt.md)\n\n"
            "---\n"
            "## [2026-07-18] - US-002\n"
            "- What was implemented: the serializer\n"
            "- Files changed: serializer.py\n"
            "- Verification run: uv run pytest -q\n"
            "- **Learnings:**\n"
            "  - Patterns discovered: registry pattern\n"
            "  - Gotchas encountered: import cycle\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: malformed input crashes the encoder\n"
            "  - Failure mode 2: concurrent flushes interleave\n"
            "  - Failure mode 3: timeout error swallowed silently\n"
            "- **Interpretations** (only if PRD was ambiguous): assumed utf-8\n"
            "---\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is True
        assert "3 failure modes" in result.message

    def test_entry_separator_terminates_bullet_count(
        self,
        tmp_path: Path,
    ) -> None:
        """Bullets after the closing `---` belong to no entry and must
        not count."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## [2026-07-18] - US-002\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: realistic detail\n"
            "---\n"
            "- stray bullet after the separator\n"
            "- another stray bullet\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False
        assert "1 bullets" in result.message

    def test_no_iteration_heading_falls_back_to_whole_file(
        self,
        tmp_path: Path,
    ) -> None:
        """Free-form progress files without a recognized iteration
        heading are treated as a single entry (documented fallback)."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "Free-form notes, no iteration headings anywhere.\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: realistic detail\n"
            "  - Failure mode 2: realistic detail\n"
            "  - Failure mode 3: realistic detail\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is True


class TestVerificationSignatureIsKeywordOnly:
    """#316: nothing after ``config`` can be passed positionally.

    Three of the arguments in that block - ``allowed_paths_error``,
    ``harness_paths`` and, beside them, ``allowed_paths`` - are
    near-identical types carrying opposite meanings. See
    ``verify.MechanicalVerification`` for why the type system was not
    catching a transposition between them.

    Two tests, not three: an "everything after index 4 is keyword-only"
    assertion is derivable from the exact positional list below, since
    the function has no ``*args`` or ``**kwargs`` for the two to
    disagree about.
    """

    def test_only_the_first_five_parameters_are_positional(self) -> None:
        params = list(inspect.signature(run_mechanical_verification).parameters.values())
        positional = [p.name for p in params if p.kind is p.POSITIONAL_OR_KEYWORD]
        assert positional == [
            "worktree_path",
            "prd_path",
            "base_branch",
            "allowed_paths",
            "config",
        ]

    def test_a_sixth_positional_argument_is_refused(self) -> None:
        """The rule as behaviour, not only as a signature assertion.

        ``allowed_paths_error`` sat in slot six. ``Signature.bind``
        rather than a real call: ``pytest.raises`` does not stop the body
        running, so if the ``*`` this test guards were ever removed, a
        real call here would shell out to the default test, typecheck and
        lint commands at 300s timeouts apiece to prove a signature point.
        ``bind`` raises the same ``TypeError`` without entering the
        function.
        """
        with pytest.raises(TypeError, match="positional argument"):
            inspect.signature(run_mechanical_verification).bind(
                Path("."),
                None,
                "main",
                None,
                VerifyConfig(),
                "scope unreadable",
            )


def test_the_protocol_says_exactly_what_the_function_says() -> None:
    """#316: ``MechanicalVerification`` has not drifted from its function.

    One assertion rather than four, because ``Signature.__eq__``
    compares name, kind, annotation, default VALUE and return
    annotation in one go - it is strictly stronger than the four
    field-by-field checks it replaces, which let a Protocol saying
    ``autonomy_level: int = 1`` through against a function saying 0.

    Most drift is caught before this runs: ``kstrl/factory.py`` binds
    the real function to the Protocol-typed field, so a Protocol with a
    wrong annotation, a missing default or a wrong return type is a
    ``mypy --strict`` error in CI. Measured on this branch, widening
    ``allowed_paths_error`` in the Protocol produced
    ``factory.py: error: Argument "run_mechanical_verification" to
    "PipelineHooks" has incompatible type ... expected
    "MechanicalVerification"``. What mypy CANNOT see is drift that
    leaves the function MORE permissive than the Protocol - a parameter
    the function grows and the Protocol lacks, or a kind that relaxes -
    because the function still satisfies the Protocol. Those reach the
    hook's callers as arguments they cannot pass, and this is what
    catches them.

    ``MechanicalVerification.__call__`` therefore spells its defaults as
    real values (``= None``, ``= 0``, ``= False``) rather than the
    conventional ``= ...``. Do not "tidy" that back: ``...`` is a
    distinct default value, so it makes this comparison fail on every
    optional parameter and forces the four weaker checks back.
    """
    proto = inspect.signature(MechanicalVerification.__call__)
    params = list(proto.parameters.values())
    assert params[0].name == "self"
    without_self = proto.replace(parameters=params[1:])
    assert without_self == inspect.signature(run_mechanical_verification)


class TestRunMechanicalVerificationWithoutPrd:
    """R10.1: ``prd_path=None`` skips exactly the PRD-dependent checks."""

    @staticmethod
    def _config() -> VerifyConfig:
        return VerifyConfig(
            project_stack=in_process_stack({"tests": "true", "typecheck": "true", "lint": "true"}),
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )

    def test_run_mechanical_verification_without_prd(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        with_prd = run_mechanical_verification(tmp_path, prd, "main", None, self._config())
        without_prd = run_mechanical_verification(tmp_path, None, "main", None, self._config())

        # The Path call keeps its full list, PRD-dependent checks included.
        assert [c.name for c in with_prd.checks] == [
            "prd_stories",
            "stack:tests",
            "stack:typecheck",
            "stack:lint",
        ]
        # None drops exactly prd_stories; nothing else moves.
        assert [c.name for c in without_prd.checks] == [
            "stack:tests",
            "stack:typecheck",
            "stack:lint",
        ]
        assert without_prd.passed is True

    def test_without_prd_self_critique_needs_an_explicit_progress_path(
        self,
        tmp_path: Path,
    ) -> None:
        """No PRD means no sibling log to derive: the check is skipped,
        unless [verify] progress_file_path names the log explicitly."""
        config = self._config()
        config.require_self_critique = True

        result = run_mechanical_verification(tmp_path, None, "main", None, config)
        assert not any(c.name == "self_critique" for c in result.checks)

        config.progress_file_path = "progress.txt"
        result = run_mechanical_verification(tmp_path, None, "main", None, config)
        critique = [c for c in result.checks if c.name == "self_critique"]
        assert len(critique) == 1
        # The configured file is absent, so the check fails closed
        # against tmp_path/progress.txt rather than being skipped.
        assert critique[0].passed is False
        assert "Could not read progress file" in critique[0].message
