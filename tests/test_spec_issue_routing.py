"""#260: the architect's non-blocker findings reach the engineer's PRD.

Before this, ``spec-issues.json`` was written on every decompose and
nothing in ``kstrl/`` opened it. The majors and minors - 91 of them
across the five recorded writers-room runs - were printed once and
discarded.

The issue bodies below marked REAL are verbatim architect output from
those runs, so the attachment rule is tested against what the model
actually emits rather than against a shape convenient for it.
"""

from __future__ import annotations

import io
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from kstrl.decompose import (
    SpecBlockerError,
    SpecIssue,
    _routed_prd_issues,
    decompose_spec,
    route_spec_issues,
)
from kstrl.prd import (
    PRD,
    SPEC_ISSUE_APPLIES_COMPONENT,
    SPEC_ISSUE_APPLIES_SPEC,
)
from kstrl.ui.plain import PlainUI
from tests.helpers.component_prd import write_component_prd
from tests.test_decompose import MockDecomposeAgent, _run_decompose
from tests.test_prd_allowed_paths import _make_prd_payload

# Component names in the shape the architect really produces. The first
# is verbatim from the one decomposed component on disk in the
# writers-room repo (scripts/kstrl/manifest.json); the second follows
# that spec's own "## Component 2: the agent adapter" heading.
DOCUMENT_FORMAT = {
    "id": "document-format",
    "title": "The writers-room document format: parse, serialize, allocate, read and write",
    "description": "Component 1 of slice 1. Reads and writes the on-disk Markdown document.",
    "dependencies": [],
    "allowedPaths": ["src/", "tests/"],
    "userStories": [
        {
            "id": "US-001",
            "title": "Parse a document into front matter and ordered blocks",
            "acceptanceCriteria": ["Round trip holds"],
            "priority": 1,
            "passes": False,
            "notes": "",
        }
    ],
}

AGENT_ADAPTER = {
    "id": "agent-adapter",
    "title": "The agent adapter: dispatch claude and codex as subprocesses",
    "description": "Component 2 of slice 1. Invokes the claude and codex CLIs.",
    "dependencies": [],
    "allowedPaths": ["src/", "tests/"],
    "userStories": [
        {
            "id": "US-002",
            "title": "Dispatch a prompt to a CLI and return a typed outcome",
            "acceptanceCriteria": ["A missing binary is an outcome, not an exception"],
            "priority": 1,
            "passes": False,
            "notes": "",
        }
    ],
}

# REAL, run 3 issue 08. Names the document format's surface and nothing
# of the adapter's.
SERIALIZE_ISSUE = {
    "severity": "major",
    "kind": "undefined_failure_mode",
    "summary": (
        "Serialize performs no validation of block text, so a Document whose block text "
        "contains a column-0 id comment or an unclosed fence serializes to a file that "
        "parses back differently."
    ),
    "location": (
        'Serializing: "parse(serialize(d)) == d for every valid d" with no definition of '
        "valid for a Document not produced by parse."
    ),
    "suggestion": (
        "Either define valid Document as a construction-time invariant that rejects block "
        "text containing a boundary line or an unclosed fence, or state the typed error "
        "serialize raises for such text."
    ),
}

# REAL, run 4 issue 06. The adapter's surface.
WORKING_DIRECTORY_ISSUE = {
    "severity": "major",
    "kind": "missing_detail",
    "summary": (
        "The working directory is a request field but the claude command line never "
        "consumes it, and the spec never says whether it becomes the subprocess working "
        "directory for either adapter."
    ),
    "location": (
        'Component 2: request carries "the working directory"; claude runs `claude -p '
        "--output-format json --model <model> --permission-mode <mode>` with no directory "
        "flag, while codex passes `-C <dir>`."
    ),
    "suggestion": (
        "State for each adapter whether the child process is started with cwd set to the "
        "request's working directory."
    ),
}

# REAL, run 4 issue 16. Its location says "Component 1, parsing" in
# prose the rule cannot resolve to a component id, so it broadcasts.
UNCLOSED_FENCE_ISSUE = {
    "severity": "minor",
    "kind": "undefined_failure_mode",
    "summary": (
        "Behavior for a fenced code block that is never closed before end of file is "
        "unspecified, so a stray fence can silently swallow every later block id comment."
    ),
    "location": (
        'Component 1, parsing: "a line whose first non-space characters are three or more '
        "backticks or three or more tildes opens a fence, and the next fence line of the "
        'same character closes it."'
    ),
    "suggestion": (
        "State whether an unclosed fence at EOF is a parse error naming the opening line "
        "number, or is accepted with the remainder treated as block text."
    ),
}

# ADAPTED from run 4 issue 09, which names both surfaces. The original
# location says "Component 1 ... Component 2", an ordinal the rule
# cannot resolve to an id; the wording here names them the way a
# decomposed spec does. Marked adapted rather than REAL on purpose.
TYPED_ERRORS_ISSUE = {
    "severity": "major",
    "kind": "missing_detail",
    "summary": (
        "Only one exception type is named across both components, so tests cannot assert "
        'on the types of the many other required "typed errors".'
    ),
    "location": (
        "Component 1 names `BlockIdExhausted` but says a document parse or serialize "
        "failure raises a typed error; the agent adapter says the same for a missing run "
        "directory when it is asked to dispatch claude."
    ),
    "suggestion": "Name every exception class and its module.",
}


def _payload(*issues: dict[str, str], components: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(
        {
            "components": components
            if components is not None
            else [DOCUMENT_FORMAT, AGENT_ADAPTER],
            "spec_issues": list(issues),
        }
    )


def _prd_path(tmp_path: Path, comp_id: str) -> Path:
    return tmp_path / "scripts" / "kstrl" / "feature" / comp_id / "prd.json"


def _summaries(tmp_path: Path, comp_id: str) -> list[tuple[str, str]]:
    """(summary, appliesTo) for every finding in one component's PRD."""
    data = json.loads(_prd_path(tmp_path, comp_id).read_text(encoding="utf-8"))
    return [(e["summary"], e["appliesTo"]) for e in data.get("specIssues", [])]


class TestFindingsReachThePrd:
    """End to end: the file the engineer is told to read carries them."""

    def test_a_finding_that_names_a_component_lands_only_in_that_prd(
        self,
        tmp_path: Path,
    ) -> None:
        _run_decompose(tmp_path, _payload(SERIALIZE_ISSUE))
        assert _summaries(tmp_path, "document-format") == [
            (SERIALIZE_ISSUE["summary"], SPEC_ISSUE_APPLIES_COMPONENT)
        ]
        # The adapter's engineer is not handed the parser's ambiguity.
        assert _summaries(tmp_path, "agent-adapter") == []

    def test_each_component_gets_its_own_finding(self, tmp_path: Path) -> None:
        _run_decompose(tmp_path, _payload(SERIALIZE_ISSUE, WORKING_DIRECTORY_ISSUE))
        assert _summaries(tmp_path, "document-format") == [
            (SERIALIZE_ISSUE["summary"], SPEC_ISSUE_APPLIES_COMPONENT)
        ]
        assert _summaries(tmp_path, "agent-adapter") == [
            (WORKING_DIRECTORY_ISSUE["summary"], SPEC_ISSUE_APPLIES_COMPONENT)
        ]

    def test_a_finding_the_rule_cannot_place_reaches_every_prd(
        self,
        tmp_path: Path,
    ) -> None:
        """Not attributable is not the same as not worth reading."""
        _run_decompose(tmp_path, _payload(UNCLOSED_FENCE_ISSUE))
        expected = [(UNCLOSED_FENCE_ISSUE["summary"], SPEC_ISSUE_APPLIES_SPEC)]
        assert _summaries(tmp_path, "document-format") == expected
        assert _summaries(tmp_path, "agent-adapter") == expected

    def test_a_finding_that_names_two_components_lands_in_both(
        self,
        tmp_path: Path,
    ) -> None:
        _run_decompose(tmp_path, _payload(TYPED_ERRORS_ISSUE))
        expected = [(TYPED_ERRORS_ISSUE["summary"], SPEC_ISSUE_APPLIES_COMPONENT)]
        assert _summaries(tmp_path, "document-format") == expected
        assert _summaries(tmp_path, "agent-adapter") == expected

    def test_a_components_own_findings_are_listed_before_the_shared_ones(
        self,
        tmp_path: Path,
    ) -> None:
        _run_decompose(tmp_path, _payload(UNCLOSED_FENCE_ISSUE, SERIALIZE_ISSUE))
        assert _summaries(tmp_path, "document-format") == [
            (SERIALIZE_ISSUE["summary"], SPEC_ISSUE_APPLIES_COMPONENT),
            (UNCLOSED_FENCE_ISSUE["summary"], SPEC_ISSUE_APPLIES_SPEC),
        ]

    def test_the_written_prd_still_loads_through_the_schema(self, tmp_path: Path) -> None:
        _run_decompose(tmp_path, _payload(SERIALIZE_ISSUE, UNCLOSED_FENCE_ISSUE))
        prd = PRD.load(_prd_path(tmp_path, "document-format"))
        assert prd.spec_issues is not None
        assert [e["kind"] for e in prd.spec_issues] == [
            SERIALIZE_ISSUE["kind"],
            UNCLOSED_FENCE_ISSUE["kind"],
        ]
        # The engineer needs the fix, not just the complaint.
        assert prd.spec_issues[0]["suggestion"] == SERIALIZE_ISSUE["suggestion"]

    def test_a_clean_audit_leaves_the_prd_as_it_was(self, tmp_path: Path) -> None:
        """No findings means no key, not an empty array."""
        _run_decompose(tmp_path, _payload())
        data = json.loads(_prd_path(tmp_path, "document-format").read_text(encoding="utf-8"))
        assert "specIssues" not in data

    def test_the_findings_survive_a_prd_save(self, tmp_path: Path) -> None:
        """The harness rewrites this file (R10.3); dropping the block on
        that write would take the findings straight back off the desk."""
        _run_decompose(tmp_path, _payload(SERIALIZE_ISSUE))
        path = _prd_path(tmp_path, "document-format")
        prd = PRD.load(path)
        prd.save(path)
        assert PRD.load(path).spec_issues == prd.spec_issues

    def test_the_operator_is_told_what_was_routed_where(self, tmp_path: Path) -> None:
        """ "It reached the engineer" has to be visible without opening
        the PRD, or nobody knows whether the audit went anywhere."""
        output = _run_decompose(tmp_path, _payload(SERIALIZE_ISSUE, UNCLOSED_FENCE_ISSUE))
        assert "document-format: 1 stories, 2 spec findings (1 on its own surface)" in output
        assert "agent-adapter: 1 stories, 1 spec findings (0 on its own surface)" in output

    def test_a_clean_audit_says_nothing_extra(self, tmp_path: Path) -> None:
        output = _run_decompose(tmp_path, _payload())
        assert "document-format: 1 stories\n" in output
        assert "spec findings" not in output


class TestHaltingIsUnchanged:
    def test_a_blocker_still_halts_and_writes_no_prd(self, tmp_path: Path) -> None:
        blocker = {
            "severity": "blocker",
            "kind": "ambiguity",
            "summary": "The document format is not specified at all",
            "location": "the whole spec",
            "suggestion": "Write it down",
        }
        payload = json.dumps({"components": [], "spec_issues": [blocker]})
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
        # Not _run_decompose: it swallows the halt, and the halt is the
        # assertion.
        with pytest.raises(SpecBlockerError):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(payload),
                ui=PlainUI(no_color=True, file=io.StringIO()),
                root_dir=tmp_path,
            )
        assert list(tmp_path.rglob("prd.json")) == []

    def test_a_blocker_is_not_routed_alongside_the_others(self) -> None:
        """Blockers halt before a PRD exists. Routing them would be dead
        code, and what a blocker means inside a PRD is the halt-moving
        change's question, not this one's."""
        data = json.loads(
            _payload(
                SERIALIZE_ISSUE,
                {
                    "severity": "blocker",
                    "kind": "contradiction",
                    "summary": "Serialize and parse cannot both hold as written",
                    "location": "Serializing and parsing a Document",
                    "suggestion": "Pick one",
                },
            )
        )
        routed = _routed_prd_issues(data)
        assert [e["summary"] for e in routed["document-format"]] == [SERIALIZE_ISSUE["summary"]]


class TestTheAttachmentRule:
    """The rule itself, on constructed inputs that isolate one knob."""

    @staticmethod
    def _route(location: str, summary: str = "x") -> dict[str, list[str]]:
        issue = SpecIssue(severity="major", kind="ambiguity", summary=summary, location=location)
        routed = route_spec_issues([issue], [DOCUMENT_FORMAT, AGENT_ADAPTER])
        return {comp_id: [e["appliesTo"] for e in entries] for comp_id, entries in routed.items()}

    def test_one_shared_name_word_is_not_enough(self) -> None:
        assert self._route("the document is unspecified") == {
            "document-format": [SPEC_ISSUE_APPLIES_SPEC],
            "agent-adapter": [SPEC_ISSUE_APPLIES_SPEC],
        }

    def test_two_shared_name_words_attach(self) -> None:
        assert self._route("the document format is unspecified") == {
            "document-format": [SPEC_ISSUE_APPLIES_COMPONENT],
            "agent-adapter": [],
        }

    def test_a_word_both_components_use_attaches_to_neither(self) -> None:
        """Both titles say "the", both descriptions could say anything.
        A word two components share cannot tell them apart, so it is
        dropped from both rather than attaching the finding twice."""
        shared = [
            {**DOCUMENT_FORMAT, "title": "Parse and serialize the shared document format"},
            {**AGENT_ADAPTER, "title": "Dispatch against the shared document format"},
        ]
        issue = SpecIssue(
            severity="major",
            kind="ambiguity",
            summary="x",
            location="the shared document format is unspecified",
        )
        routed = route_spec_issues([issue], shared)
        assert [e["appliesTo"] for e in routed["document-format"]] == [SPEC_ISSUE_APPLIES_SPEC]
        assert [e["appliesTo"] for e in routed["agent-adapter"]] == [SPEC_ISSUE_APPLIES_SPEC]

    def test_short_words_carry_no_signal(self) -> None:
        """Both words sit in the document-format title. ``read`` is four
        characters and drops out on length; ``write`` survives but one
        word is not enough, so this broadcasts rather than attaching."""
        assert self._route("read and write are unspecified") == {
            "document-format": [SPEC_ISSUE_APPLIES_SPEC],
            "agent-adapter": [SPEC_ISSUE_APPLIES_SPEC],
        }

    def test_the_summary_counts_as_well_as_the_location(self) -> None:
        """Half the real locations quote the spec without naming the
        component; the summary is the other half of the evidence."""
        assert self._route("somewhere in the spec", "claude and codex dispatch is undefined") == {
            "document-format": [],
            "agent-adapter": [SPEC_ISSUE_APPLIES_COMPONENT],
        }

    def test_no_findings_still_names_every_component(self) -> None:
        assert route_spec_issues([], [DOCUMENT_FORMAT, AGENT_ADAPTER]) == {
            "document-format": [],
            "agent-adapter": [],
        }


class TestPrdSchema:
    ENTRY: dict[str, str] = {
        "severity": "major",
        "kind": "ambiguity",
        "summary": "s",
        "location": "l",
        "suggestion": "g",
        "appliesTo": SPEC_ISSUE_APPLIES_SPEC,
    }

    def _errors(self, spec_issues: Any) -> list[str]:
        return PRD.validate_schema(_make_prd_payload(specIssues=spec_issues))

    def test_a_well_formed_block_validates(self) -> None:
        assert self._errors([self.ENTRY]) == []

    def test_an_empty_array_is_refused(self) -> None:
        assert any("non-empty when present" in e for e in self._errors([]))

    def test_a_non_array_is_refused(self) -> None:
        assert any("must be an array" in e for e in self._errors("nope"))

    def test_an_entry_that_is_not_an_object_is_refused(self) -> None:
        assert any("specIssues[0]: must be an object" in e for e in self._errors(["nope"]))

    def test_an_unknown_applies_to_is_refused(self) -> None:
        errors = self._errors([{**self.ENTRY, "appliesTo": "everywhere"}])
        assert any("appliesTo" in e for e in errors)

    def test_an_extra_key_is_refused(self) -> None:
        errors = self._errors([{**self.ENTRY, "owner": "me"}])
        assert any("unexpected keys: owner" in e for e in errors)

    def test_a_missing_key_is_refused(self) -> None:
        entry = {k: v for k, v in self.ENTRY.items() if k != "kind"}
        assert any("missing keys: kind" in e for e in self._errors([entry]))

    def test_a_non_string_field_is_refused(self) -> None:
        errors = self._errors([{**self.ENTRY, "severity": 3}])
        assert any("severity: must be a string" in e for e in errors)

    def test_an_empty_summary_is_refused(self) -> None:
        errors = self._errors([{**self.ENTRY, "summary": ""}])
        assert any("summary: must be non-empty" in e for e in errors)

    # The two optional arrays that were here first now share this
    # field's rule (``prd._OPTIONAL_ARRAYS``). Their behaviour is
    # covered where it already was, and re-asserting it here would only
    # duplicate the message strings:
    # tests/test_prd_allowed_paths.py::test_empty_array_rejected,
    # ::test_empty_string_item_rejected and
    # tests/test_fixtures.py::test_empty_fixtures_array_rejected.


class TestFindingsAreNotPinned:
    def test_editing_the_findings_is_not_tampering(self, tmp_path: Path) -> None:
        """Deliberate, and the same shape of reason as allowedPaths
        (#269): no gate reads this field, so an edit cannot buy a
        verdict, and pinning it would only turn an agent tidying an
        informational block into a failed run."""
        rel = "scripts/kstrl/feature/document-format/prd.json"
        path = write_component_prd(tmp_path, rel, spec_issues=[TestPrdSchema.ENTRY])
        authored = PRD.load(path)
        write_component_prd(
            tmp_path,
            rel,
            spec_issues=[{**TestPrdSchema.ENTRY, "summary": "rewritten"}],
        )
        assert PRD.load(path).tamper_changes(authored) == []

    def test_a_field_added_to_prd_is_decided_rather_than_forgotten(self) -> None:
        """``_pinned_stories`` pins a new UserStory field by default;
        ``tamper_changes`` one level up enumerates what it compares, so
        a new PRD field is silently unpinned instead. This is the
        mechanism that makes the omission a decision: two fields are
        deliberately exempt and both reasons are written down beside
        ``tamper_changes``. A third has to be argued for here first.
        """
        compared = {"branch_name", "user_stories", "fixtures"}
        exempt = {
            # Inert: scope is resolved before the run starts (#269).
            "allowed_paths",
            # Informational: no gate reads it, and the durable copy is
            # spec-issues.json (#260).
            "spec_issues",
        }
        assert {f.name for f in fields(PRD)} == compared | exempt, (
            "PRD gained or lost a field; decide whether tamper_changes "
            "must compare it and record the reason either way"
        )
