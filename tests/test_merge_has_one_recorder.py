"""Every merge kstrl records is a ``pr_merged`` event, whichever path confirmed it (#584).

Two paths confirm a merge. ``ComponentPipeline._phase_pr`` merges a PR
this run opened, and ``ComponentPipeline.repoll_merge_pending`` finds a
PR that a stopped run left merge-pending and that merged while nothing
was running. Before #584 the second path set ``merge_sha`` on the
manifest and emitted no ``pr_merged``, so the TUI delivery section, the
CI refresh and any other reader of the run's stream did not see the
merge. Both now call ``ComponentPipeline._record_merge``, which writes
the commit to the manifest, saves it, and emits ``pr_merged`` with the
PR number, URL and commit the manifest holds.

Two layers.

The BEHAVIOUR layer drives the real ``ks factory`` in a subprocess over
a saved manifest, with a stub ``gh`` on PATH, and reads the run back
through the manifest, the run's ``events.jsonl``, the TUI's
``load_run_state`` and ``merges_of``, and ``ci_state.recorded_merges``
given no manifest.

The CENSUS layer counts, per ``module::scope`` in ``kstrl/``, every node
that spells ``merge_sha`` or ``mergeSha`` other than a plain read of an
attribute or a name, every node that spells ``PrMerged``, and every node
that spells ``_record_merge``. It enumerates no write shapes, so an
assignment, ``setattr``, a keyword argument, a dict key or
``dataclasses.replace`` all move a count. Every row is classified. What
it cannot see is disclosed at the bottom with a strict xfail: a field
name the interpreter has to build.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.ci_state import recorded_merges
from kstrl.manifest import Manifest
from kstrl.reducer import load_run_state
from kstrl.tui.delivery import Merge, merges_of
from tests.helpers import astwalk, gitrepo
from tests.helpers.executables import write_executable
from tests.test_merge_gate_park import ENGINEER, FACTORY_FLAGS

HTTP = "http"
CMDS = "cmds"
URL_41 = "https://github.com/o/r/pull/41"
URL_42 = "https://github.com/o/r/pull/42"
SHA_41 = "a" * 40
SHA_42 = "b" * 40

#: `pr create` answers PR #42 and remembers its head; `pr merge` moves
#: origin's main to that head; `pr view N` reports N MERGED with the
#: commit in $SHA_N, or with no commit when $SHA_N is empty.
FAKE_GH = """#!/bin/sh
printf '%s\\n' "gh $*" >> "$GH_LOG"
if [ "$1" = "auth" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  for a in "$@"; do case "$a" in --head=*) head="${a#--head=}";; esac; done
  printf '%s' "$head" > "$GH_HEAD"
  echo "https://github.com/o/r/pull/42"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  git push -q origin "refs/heads/$(cat "$GH_HEAD"):refs/heads/main" || exit 1
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  if [ "$3" = "41" ]; then sha="$SHA_41"; else sha="$SHA_42"; fi
  if [ -n "$sha" ]; then
    printf '{"state": "MERGED", "mergeCommit": {"oid": "%s"}}\\n' "$sha"
  else
    printf '{"state": "MERGED", "mergeCommit": null}\\n'
  fi
  exit 0
fi
echo "[]"
exit 0
"""


def _repo(tmp_path: Path, components: list[dict[str, object]]) -> Path:
    """A repository with a bare origin, a PRD per component and a saved manifest."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    for comp in components:
        cid = str(comp["id"])
        prd = root / "scripts" / "kstrl" / "feature" / cid / "prd.json"
        prd.parent.mkdir(parents=True)
        prd.write_text(
            json.dumps({"branchName": f"kstrl/factory/{cid}", "userStories": [story]}),
            encoding="utf-8",
        )
    (root / "kstrl.toml").write_text("[inbox]\nenabled = true\n", encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    origin = tmp_path / "origin.git"
    gitrepo.git_in(tmp_path, "init", "-q", "--bare", str(origin))
    gitrepo.git_in(root, "remote", "add", "origin", str(origin))
    gitrepo.git_in(root, "push", "-q", "-u", "origin", "main")
    manifest = {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "p",
        "baseBranch": "main",
        "singlePr": False,
        "components": components,
    }
    _manifest_path(root).write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _component(cid: str, deps: list[str], **fields: object) -> dict[str, object]:
    return {
        "id": cid,
        "title": cid,
        "description": "",
        "dependencies": deps,
        "prdPath": f"scripts/kstrl/feature/{cid}/prd.json",
        "branchName": f"kstrl/factory/{cid}",
        **fields,
    }


def _parked(**fields: object) -> dict[str, object]:
    """`http` as a stopped run left it: PR #41 opened, merge not confirmed."""
    return _component(
        HTTP,
        [],
        status="merge_pending",
        prNumber=41,
        prUrl=URL_41,
        error="PR #41 not merged within 2s",
        **fields,
    )


def _manifest_path(root: Path) -> Path:
    return root / "scripts" / "kstrl" / "manifest.json"


def _factory(
    tmp_path: Path, root: Path, sha_41: str, sha_42: str = SHA_42
) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL", "FACTORY_MAX_PARALLEL")
    }
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_executable(bindir / "gh", FAKE_GH)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["GH_LOG"] = str(tmp_path / "gh.log")
    env["GH_HEAD"] = str(tmp_path / "gh.head")
    env["ENGINEER_LOG"] = str(tmp_path / "engineer.log")
    env["SHA_41"] = sha_41
    env["SHA_42"] = sha_42
    env["AGENT_CMD"] = ENGINEER
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "kstrl",
            "factory",
            "--manifest",
            str(_manifest_path(root)),
            *FACTORY_FLAGS,
            "--root",
            str(root),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=300,
    )


def _run_rows(root: Path) -> list[dict[str, object]]:
    """Every row of the one run's events.jsonl."""
    runs = sorted((root / ".kstrl" / "runs").iterdir())
    assert len(runs) == 1, runs
    text = (runs[0] / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _merged_rows(rows: list[dict[str, object]]) -> list[tuple[str, int, str, str]]:
    """(component, pr_number, pr_url, merge_sha) of every pr_merged row,
    sorted, and a list so that a merge recorded twice shows twice."""
    found: list[tuple[str, int, str, str]] = []
    for row in rows:
        if row["event"] == "pr_merged":
            data = row["data"]
            assert isinstance(data, dict)
            found.append(
                (str(row["component"]), data["pr_number"], data["pr_url"], data["merge_sha"])
            )
    return sorted(found)


def _manifest_merges(root: Path) -> list[tuple[str, int | None, str, str]]:
    """The same four fields, as the manifest holds them, for every completed component."""
    manifest = Manifest.load(_manifest_path(root))
    return sorted(
        (comp.id, comp.pr_number, comp.pr_url, comp.merge_sha)
        for comp in manifest.components
        if comp.status == "completed"
    )


class TestARepolledMergeIsInTheStream:
    def test_both_paths_record_the_same_merge_in_the_manifest_and_the_stream(
        self, tmp_path: Path
    ) -> None:
        """`http` merged while no run was going; the restarted run re-polls
        it, then runs `cmds` and merges its PR itself. Both merges are
        pr_merged events whose fields are the manifest's, and the readers
        of the stream list both."""
        root = _repo(tmp_path, [_parked(), _component(CMDS, [HTTP])])

        proc = _factory(tmp_path, root, SHA_41)
        out = proc.stdout + proc.stderr

        assert proc.returncode == 0, out
        expected = [(CMDS, 42, URL_42, SHA_42), (HTTP, 41, URL_41, SHA_41)]
        assert _manifest_merges(root) == expected
        rows = _run_rows(root)
        assert _merged_rows(rows) == expected
        # The merge is recorded before the component is reported completed,
        # the order the path that merged cmds has always used.
        http_events = [row["event"] for row in rows if row.get("component") == HTTP]
        assert http_events.index("pr_merged") < http_events.index("component_completed")
        # The TUI delivery section and the CI refresh read the stream.
        state, _source = load_run_state(root)
        assert merges_of(state) == (Merge(HTTP, 41, SHA_41), Merge(CMDS, 42, SHA_42))
        assert recorded_merges(root, None) == ((HTTP, SHA_41), (CMDS, SHA_42))

    def test_a_merge_github_published_no_commit_for_says_so_in_both_records(
        self, tmp_path: Path
    ) -> None:
        """The manifest holds a commit an earlier merge of `http` recorded,
        and GitHub publishes none for this one. Both records say "" rather
        than the manifest keeping the earlier merge's commit."""
        root = _repo(tmp_path, [_parked(mergeSha="d" * 40)])

        proc = _factory(tmp_path, root, "")
        out = proc.stdout + proc.stderr

        assert proc.returncode == 0, out
        assert _manifest_merges(root) == [(HTTP, 41, URL_41, "")]
        assert _merged_rows(_run_rows(root)) == [(HTTP, 41, URL_41, "")]

    def test_a_merge_this_run_made_with_no_commit_says_so_in_both_records(
        self, tmp_path: Path
    ) -> None:
        """The same rule on the path that merges: `cmds` holds a commit an
        earlier merge of it recorded, this run merges PR #42, and GitHub
        publishes no commit for it. Both records say "" rather than the
        manifest keeping the earlier merge's commit."""
        root = _repo(tmp_path, [_component(CMDS, [], mergeSha="d" * 40)])

        proc = _factory(tmp_path, root, SHA_41, sha_42="")
        out = proc.stdout + proc.stderr

        assert proc.returncode == 0, out
        assert _manifest_merges(root) == [(CMDS, 42, URL_42, "")]
        assert _merged_rows(_run_rows(root)) == [(CMDS, 42, URL_42, "")]

    def test_a_parked_pr_known_only_by_its_url_is_recorded_by_its_number(
        self, tmp_path: Path
    ) -> None:
        """The manifest holds `http`'s PR URL and no PR number. The re-poll
        reads the number from the URL, and the pr_merged event carries that
        number rather than 0, so the delivery section names PR #41."""
        parked = _parked()
        del parked["prNumber"]
        root = _repo(tmp_path, [parked])

        proc = _factory(tmp_path, root, SHA_41)
        out = proc.stdout + proc.stderr

        assert proc.returncode == 0, out
        assert _merged_rows(_run_rows(root)) == [(HTTP, 41, URL_41, SHA_41)]
        state, _source = load_run_state(root)
        assert merges_of(state) == (Merge(HTTP, 41, SHA_41),)


# --- census: every place that records a merge ------------------------------

_OWNERS: dict[Path, dict[int, str]] = {}


def _scoped(source_file: Path, node: ast.AST) -> str:
    """``module::scope`` for a node of a parsed package module."""
    owner = _OWNERS.get(source_file)
    if owner is None:
        owner = _OWNERS[source_file] = astwalk.scope_of(astwalk.parsed(source_file))
    return f"{astwalk.label(source_file)}::{owner[id(node)]}"


_SPELLS_SHA = astwalk.spells("merge_sha")
_SPELLS_KEY = astwalk.spells("mergeSha")


def spells_merge_sha(node: ast.AST) -> bool:
    """FLAGGING: the field or its manifest key, anywhere but a plain read.

    A ``Load`` of an attribute or a name cannot set anything, so it is
    cleared. Everything else counts, including a string constant, which
    reaches ``setattr`` and a dict key, and a keyword, which reaches
    ``dataclasses.replace`` and a constructor.
    """
    if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load):
        return False
    return _SPELLS_SHA(node) or _SPELLS_KEY(node)


#: One control per disjunct, and one per write shape the clearing half
#: must not clear.
SHA_CONTROLS = (
    "comp.merge_sha = sha\n",
    'row["mergeSha"] = sha\n',
    'setattr(comp, "merge_sha", sha)\n',
    "replace(comp, merge_sha=sha)\n",
)
#: The clearing half's own control: reads count zero.
SHA_READ = "x = comp.merge_sha\ny = merge_sha\n"

#: The scope that writes ``Component.merge_sha`` and builds ``PrMerged``.
RECORDER = "pipeline.py::ComponentPipeline._record_merge"

#: Every scope in ``kstrl/`` that spells ``merge_sha`` or ``mergeSha``
#: other than by reading it, and how many times. A new row, or a count
#: that moved, may be a new place a merge is recorded: route it through
#: ``_record_merge`` or classify it in NOT_A_MERGE_RECORD.
EXPECTED_SHA_SPELLINGS: dict[str, int] = {
    "events.py::<module>": 1,
    "manifest.py::<module>": 1,
    "manifest.py::Manifest.load": 2,
    "manifest.py::Manifest.save": 1,
    "manifest_keys.py::<module>": 1,
    RECORDER: 3,
    "pr.py::<module>": 2,
    "pr.py::_merge_and_wait": 1,
    "pr.py::push_create_and_merge_pr": 1,
    "pr.py::wait_for_merge": 1,
    "pr_state.py::<module>": 1,
    "pr_state.py::_pr_state": 1,
    "reducer.py::<module>": 1,
    "reducer.py::apply": 1,
    "tui/delivery.py::<module>": 1,
}

#: Every other row, and why it is not kstrl recording a merge.
NOT_A_MERGE_RECORD: dict[str, str] = {
    "events.py::<module>": "the PrMerged.merge_sha field declaration",
    "manifest.py::<module>": "the Component.merge_sha field declaration",
    "manifest.py::Manifest.load": "reads back the mergeSha the recorder saved",
    "manifest.py::Manifest.save": "serialises Component.merge_sha as mergeSha",
    "manifest_keys.py::<module>": "the list of keys a manifest component may hold",
    "pr.py::<module>": "the MergeConfirmation and PrOutcome field declarations",
    "pr.py::_merge_and_wait": "hands GitHub's commit to the caller on PrOutcome",
    "pr.py::push_create_and_merge_pr": "hands GitHub's commit for an existing PR to the caller",
    "pr.py::wait_for_merge": "hands GitHub's commit to the caller on MergeConfirmation",
    "pr_state.py::<module>": "the PrView field declaration",
    "pr_state.py::_pr_state": "the commit gh reported",
    "reducer.py::<module>": "the TUI's ComponentState.merge_sha field declaration",
    "reducer.py::apply": "folds a pr_merged event into the TUI's view, not the manifest",
    "tui/delivery.py::<module>": "the delivery section's Merge.merge_sha field declaration",
}

#: Every scope in ``kstrl/`` that spells ``PrMerged``, and how many times.
EXPECTED_PR_MERGED_SPELLINGS: dict[str, int] = {
    "ci_state.py::<module>": 1,
    "ci_state.py::_merges_in_run": 1,
    "events.py::<module>": 1,
    RECORDER: 1,
    "reducer.py::apply": 1,
    "tui/widgets/activity.py::humanize": 1,
}

#: Every row above except the recorder, and why it builds no event.
NOT_A_PR_MERGED_BUILD: dict[str, str] = {
    "ci_state.py::<module>": "imports the class to select on it",
    "ci_state.py::_merges_in_run": "selects pr_merged rows with isinstance",
    "events.py::<module>": "the class declaration",
    "reducer.py::apply": "folds the event with isinstance",
    "tui/widgets/activity.py::humanize": "describes the event with isinstance",
}

#: Every scope that spells ``_record_merge``: the definition, and the two
#: paths that confirm a merge.
EXPECTED_RECORDER_SPELLINGS: dict[str, int] = {
    "pipeline.py::<module>": 1,
    "pipeline.py::ComponentPipeline._phase_pr": 1,
    "pipeline.py::ComponentPipeline.repoll_merge_pending": 1,
}


class TestAMergeHasOneRecorder:
    def test_the_merge_sha_census_is_pinned(self) -> None:
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=spells_merge_sha,
            expected=EXPECTED_SHA_SPELLINGS,
            control=SHA_CONTROLS,
            message=(
                "kstrl/ spells merge_sha or mergeSha somewhere new, or a count "
                "moved. A merge recorded there skips the pr_merged event (#584): "
                "call ComponentPipeline._record_merge instead, or classify the "
                "row in NOT_A_MERGE_RECORD with the reason."
            ),
            key=_scoped,
        )

    def test_a_read_is_cleared(self) -> None:
        assert [n for n in astwalk.all_nodes(astwalk.parse(SHA_READ)) if spells_merge_sha(n)] == []

    def test_every_merge_sha_row_is_classified_once(self) -> None:
        assert RECORDER not in NOT_A_MERGE_RECORD
        assert {RECORDER, *NOT_A_MERGE_RECORD} == set(EXPECTED_SHA_SPELLINGS)

    def test_the_pr_merged_census_is_pinned(self) -> None:
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=astwalk.spells("PrMerged"),
            expected=EXPECTED_PR_MERGED_SPELLINGS,
            control="ev.PrMerged(component='c')\n",
            message=(
                "kstrl/ spells PrMerged somewhere new, or a count moved. A "
                "pr_merged event built outside ComponentPipeline._record_merge "
                "can disagree with the manifest (#584)."
            ),
            key=_scoped,
        )

    def test_every_pr_merged_row_is_classified_once(self) -> None:
        assert RECORDER not in NOT_A_PR_MERGED_BUILD
        assert {RECORDER, *NOT_A_PR_MERGED_BUILD} == set(EXPECTED_PR_MERGED_SPELLINGS)

    def test_both_confirmations_call_the_recorder(self) -> None:
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=astwalk.spells("_record_merge"),
            expected=EXPECTED_RECORDER_SPELLINGS,
            control="self._record_merge(comp, sha)\n",
            message=(
                "The paths that confirm a merge are _phase_pr and "
                "repoll_merge_pending, and each calls _record_merge once."
            ),
            key=_scoped,
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_field_name_the_interpreter_builds_is_not_seen(self) -> None:
        """Disclosed: the net folds constants, and ``str.format`` is a call."""
        astwalk.blind_spot(
            lambda source: [
                n for n in astwalk.all_nodes(astwalk.parse(source)) if spells_merge_sha(n)
            ],
            'setattr(comp, "{}_sha".format("merge"), sha)\n',
        )
