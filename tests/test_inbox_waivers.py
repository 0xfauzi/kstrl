"""Approving a policy_exception or test_adequacy item waives exactly that finding (#595).

The defect: a blocking ``policy_*`` or ``adequacy_*`` finding failed the
component and filed an inbox item, and ``ks inbox approve`` on that item
recorded the approval and changed nothing. ``ks retry`` failed on the same
finding and filed a new item. The fix stores a key for the one finding in
the item's evidence, reads the approved items once when a run starts, and
has the check re-emit a matched finding as advisory, naming the approval,
before it decides whether it passed.

Every test drives the real ``ks`` CLI in a subprocess against a real git
repository with a bare origin: ``ks factory``, ``ks inbox approve`` or
``reject``, then ``ks retry``. The engineer is a shell command
(``AGENT_CMD``) that commits the change under test, every verify command
is ``true``, review and security are skipped, and ``gh`` is a stub on PATH
that records its arguments, so the retry that passes opens a pull request
whose body can be read.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from textual.widgets import DataTable, Static

from kstrl.findings import Finding
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind
from kstrl.manifest import Component, Manifest
from kstrl.statedir import plan_prd_path
from kstrl.tui.screens.inbox import InboxScreen
from tests.helpers import gitrepo
from tests.helpers.executables import write_executable
from tests.helpers.rendered import flat
from tests.helpers.settle import mounted, settled
from tests.helpers.tui_screens import home_app

COMP = "comp"
BRANCH = f"kstrl/factory/{COMP}"
TIMEOUT = 240

#: Records every call in $GH_LOG, the PR body included. `pr merge` moves
#: origin's main to the PR head, and `pr view` reports it merged.
FAKE_GH = """#!/bin/sh
printf '%s\\n' "gh $*" >> "$GH_LOG"
if [ "$1" = "auth" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  for a in "$@"; do case "$a" in --head=*) head="${a#--head=}";; esac; done
  printf '%s' "$head" > "$GH_HEAD"
  echo "https://github.com/o/r/pull/41"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  git push -q origin "refs/heads/$(cat "$GH_HEAD"):refs/heads/main" || exit 1
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  printf '{"state": "MERGED", "mergeCommit": null}\\n'
  exit 0
fi
echo "[]"
exit 0
"""

FACTORY_FLAGS = (
    "--yes",
    "--ui",
    "plain",
    "--no-color",
    "--no-tui",
    "--max-parallel",
    "1",
    "--review-mode",
    "skip",
    "--contract-check",
    "skip",
    "--test-command",
    "true",
    "--typecheck-command",
    "true",
    "--lint-command",
    "true",
)

BASE_TESTS = "def test_one():\n    assert 1 + 1 == 2\n\n\ndef test_two():\n    assert 2 * 2 == 4\n"


def _engineer(change: str) -> str:
    """A one-iteration engineer that makes ``change`` and commits it."""
    return (
        f"{change} && git add -A && git commit -q -m work >/dev/null 2>&1; "
        "echo '<promise>COMPLETE</promise>'"
    )


POLICY_TOML = '[policy]\nenabled = true\npaths_deny = ["secrets/**"]\n'
SECRET_TOML = "[policy]\nenabled = true\npaths_deny = []\n"
ADEQUACY_TOML = '[adequacy]\nenabled = true\nlayer0 = "block"\n'

DENIED = _engineer("mkdir -p secrets && printf 'k\\n' > secrets/key.txt")
OTHER_DENIED = _engineer("mkdir -p secrets && printf 'j\\n' > secrets/other.txt")
TWO_DENIED = _engineer(
    "mkdir -p secrets && printf 'k\\n' > secrets/key.txt && printf 'j\\n' > secrets/other.txt"
)
ONE_TEST_DELETED = _engineer(
    "printf 'def test_one():\\n    assert 1 + 1 == 2\\n' > tests/test_core.py"
)
#: Deletes the OTHER base test, in the same file. Combined with
#: ONE_TEST_DELETED across two retries, the cumulative diff against the
#: original file nets to "test_one deleted" (test_two's text is back to
#: its original content, so the diff shows no line for it) - a different
#: symbol than ONE_TEST_DELETED's own "test_two deleted", hence a
#: different explanation and waiver_key at the same category and
#: location. Verified empirically: `git diff` over the two commits shows
#: only the test_one block as removed.
ANOTHER_TEST_DELETED = _engineer(
    "printf 'def test_two():\\n    assert 2 * 2 == 4\\n' > tests/test_core.py"
)
KEY_A = "AKIA" + "A" * 16
KEY_B = "AKIA" + "B" * 16
ONE_SECRET = _engineer(f"mkdir -p app && printf 'A = \"{KEY_A}\"\\n' > app/cfg.py")
TWO_SECRETS = _engineer(f'mkdir -p app && printf \'A = "{KEY_A}"\\nB = "{KEY_B}"\\n\' > app/cfg.py')
MACHINERY = _engineer("printf '# edited\\n' >> kstrl.toml")
DEPS_TOML = "[policy]\nenabled = true\npaths_deny = []\nlicense_allow = []\n"
SYMBOL_TOML = ADEQUACY_TOML


def _lockfile(names: list[str]) -> str:
    """An engineer that adds a uv.lock holding a new package per name."""
    stanzas = "".join(
        f'[[package]]\\nname = \\"{n}\\"\\nversion = \\"1.0.0\\"\\n\\n' for n in names
    )
    return _engineer(f'printf "{stanzas}" > uv.lock')


def _silent_tests(names: list[str]) -> str:
    """An engineer that appends a test asserting nothing per name to tests/test_core.py."""
    defs = "".join(f"\\n\\ndef {n}():\\n    pass\\n" for n in names)
    return _engineer(f"printf '{defs}' >> tests/test_core.py")


#: Twenty-one names: the explanation used to show twenty and "(+1 more)".
TWENTY_ONE = [f"pkg{i:02d}" for i in range(1, 22)]
#: Six names: the explanation used to show five.
SIX = [f"test_silent_{i}" for i in range(1, 7)]


@dataclass(frozen=True)
class Scenario:
    toml: str
    engineer: str
    category_prefix: str


#: One scenario per waivable kind. test_every_waivable_kind_has_a_scenario
#: fails when kstrl/waivers.py gains a kind this table does not name.
SCENARIOS: dict[ItemKind, Scenario] = {
    ItemKind.POLICY_EXCEPTION: Scenario(POLICY_TOML, DENIED, "policy_"),
    ItemKind.TEST_ADEQUACY: Scenario(ADEQUACY_TOML, ONE_TEST_DELETED, "adequacy_"),
}


# --- harness ------------------------------------------------------------------


def _repo(tmp_path: Path, toml: str) -> Path:
    """One component, a PRD whose story passes, a bare origin and a saved manifest."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_core.py").write_text(BASE_TESTS, encoding="utf-8")
    prd = root / "scripts" / "kstrl" / "feature" / COMP / "prd.json"
    prd.parent.mkdir(parents=True)
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    prd.write_text(json.dumps({"branchName": BRANCH, "userStories": [story]}), encoding="utf-8")
    (root / "kstrl.toml").write_text("[inbox]\nenabled = true\n" + toml, encoding="utf-8")
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
        "components": [
            {
                "id": COMP,
                "title": COMP,
                "description": "",
                "dependencies": [],
                "prdPath": f"scripts/kstrl/feature/{COMP}/prd.json",
                "branchName": BRANCH,
            }
        ],
    }
    _manifest_path(root).write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _manifest_path(root: Path) -> Path:
    return root / "scripts" / "kstrl" / "manifest.json"


def _env(tmp_path: Path, engineer: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL", "FACTORY_MAX_PARALLEL")
    }
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_executable(bindir / "gh", FAKE_GH)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["GH_LOG"] = str(tmp_path / "gh.log")
    env["GH_HEAD"] = str(tmp_path / "gh.head")
    env["AGENT_CMD"] = engineer
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    return env


def _ks(root: Path, env: dict[str, str], *args: str) -> tuple[int, str]:
    """Run ``python -m kstrl`` in its own process group, killed as a group on timeout."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "kstrl", *args, "--root", str(root)],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        pytest.fail(f"ks {' '.join(args)} ran past {TIMEOUT}s:\n{out}")
    return proc.returncode, out


def _factory(root: Path, env: dict[str, str], *extra: str) -> tuple[int, str]:
    return _ks(
        root,
        env,
        "factory",
        "--manifest",
        str(_manifest_path(root)),
        *FACTORY_FLAGS,
        "--max-retries",
        "0",
        *extra,
    )


def _retry(root: Path, env: dict[str, str]) -> tuple[int, str]:
    return _ks(root, env, "retry", COMP, "--yes", "--ui", "plain", "--no-color")


def _decide(root: Path, env: dict[str, str], action: str, item_id: str) -> str:
    extra = ("--comment", "not this one") if action == "reject" else ()
    code, out = _ks(root, env, "inbox", action, item_id, *extra, "--ui", "plain", "--no-color")
    assert code == 0, out
    return out


def _component(root: Path) -> Component:
    comp = Manifest.load(_manifest_path(root)).get_component(COMP)
    assert comp is not None
    return comp


def _gated(root: Path, prefix: str) -> list[Finding]:
    return [f for f in _component(root).findings if f.category.startswith(prefix)]


def _open(root: Path, kind: ItemKind) -> list[InboxItem]:
    box = Inbox(root, InboxConfig.load(root))
    return [item for item in box.open_items() if item.kind is kind]


def _failed_run(tmp_path: Path, toml: str, engineer: str) -> tuple[Path, dict[str, str]]:
    root = _repo(tmp_path, toml)
    env = _env(tmp_path, engineer)
    code, out = _factory(root, env)
    assert code == 1, out
    assert _component(root).status == "failed", out
    return root, env


def _verification_failures(root: Path) -> list[str]:
    """The failures of the last VerificationResultEvent in the latest run."""
    run_id = Manifest.load(_manifest_path(root)).run_id
    events = root / ".kstrl" / "runs" / run_id / "events.jsonl"
    failures: list[str] = []
    for line in events.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("event") == "verification_result":
            failures = list(record["data"]["failures"])
    return failures


# --- an approval waives the finding it covers -----------------------------------


def test_every_waivable_kind_has_a_scenario() -> None:
    from kstrl.waivers import WAIVABLE

    assert set(WAIVABLE) == set(SCENARIOS)
    assert {kind: s.category_prefix for kind, s in SCENARIOS.items()} == WAIVABLE


@pytest.mark.parametrize("kind", list(SCENARIOS), ids=str)
def test_an_approved_item_waives_its_finding_on_retry(tmp_path: Path, kind: ItemKind) -> None:
    scenario = SCENARIOS[kind]
    root, env = _failed_run(tmp_path, scenario.toml, scenario.engineer)
    items = _open(root, kind)
    assert items, [f.category for f in _component(root).findings]
    said = {item.id: _decide(root, env, "approve", item.id) for item in items}

    code, out = _retry(root, env)

    assert code == 0, out
    for item in items:
        # `ks inbox approve` said what the approval does, quoting the finding.
        assert f"approved {item.id[:8]}" in said[item.id], said[item.id]
        assert "waives this one finding" in said[item.id], said[item.id]
        # #595 B1: no promise of "every ks factory or ks retry run" - only
        # a reproduced (same category, location, explanation) finding.
        assert "reproduces it exactly" in said[item.id], said[item.id]
        assert "every ks factory or ks retry run" not in said[item.id], said[item.id]
        assert item.detail in said[item.id], said[item.id]
    comp = _component(root)
    assert comp.status == "completed", out
    waived = _gated(root, scenario.category_prefix)
    assert {(f.severity, *[t for t in f.tags if t.startswith("waiver:")]) for f in waived} == {
        ("advisory", f"waiver:{item.id}") for item in items
    }
    for f in waived:
        assert "[waived by inbox approval" in f.explanation, f.explanation
    assert _verification_failures(root) == []
    assert _open(root, kind) == []
    # The pull request names the approval.
    gh_log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "pr create" in gh_log
    for item in items:
        assert f"waived by inbox approval {item.id[:8]}" in gh_log, gh_log
    if kind is ItemKind.TEST_ADEQUACY:
        # #595: verify.check_test_adequacy mirrors check_policy_envelope
        # and says "satisfied after waivers" once nothing still blocks.
        # A passing check's own message reaches neither events.jsonl
        # (VerificationResultEvent.failures only holds a FAILING check's
        # message) nor the CLI output (Phase 1's line is a generic "Phase
        # 1 passed"), so this re-runs the exact check Phase 1 ran, against
        # the merged tree, with the approvals it actually read.
        from kstrl.adequacy import AdequacyConfig
        from kstrl.verify import check_test_adequacy
        from kstrl.waivers import WaiverScope, load_approvals

        base_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        subprocess.run(["git", "pull", "-q", "origin", "main"], cwd=root, check=True)
        manifest = Manifest.load(_manifest_path(root))
        scope = WaiverScope(
            project=manifest.project_name,
            spec_file=manifest.spec_file,
            plan_id=comp.plan_id,
            component=COMP,
        )
        waivers = load_approvals(Inbox(root, InboxConfig.load(root))).for_scope(scope)
        result = check_test_adequacy(root, base_sha, AdequacyConfig.load(root), waivers=waivers)
        assert "test adequacy satisfied after waivers" in result.message, result.message


# --- everything an approval does not cover still fails --------------------------


def test_a_rejected_approval_waives_nothing(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, POLICY_TOML, DENIED)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    _decide(root, env, "approve", item.id)
    out = _decide(root, env, "reject", item.id)
    assert f"rejected {item.id[:8]}" in out, out

    code, out = _retry(root, env)

    assert code == 1, out
    assert [(f.category, f.severity) for f in _gated(root, "policy_")] == [
        ("policy_paths_deny", "high")
    ]
    assert len(_open(root, ItemKind.POLICY_EXCEPTION)) == 1


def test_an_approval_does_not_cover_a_different_denied_path(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, POLICY_TOML, DENIED)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    _decide(root, env, "approve", item.id)

    code, out = _retry(root, {**env, "AGENT_CMD": TWO_DENIED})

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert finding.severity == "high"
    assert f"waiver_refused:{item.id}" in finding.tags
    assert any(
        "covers a different policy_paths_deny finding" in f for f in _verification_failures(root)
    )


def test_a_repeat_with_different_evidence_opens_a_second_item_not_a_replacement(
    tmp_path: Path,
) -> None:
    """#595 B2: the dedupe key was category-only, so a same-category repeat
    with different evidence overwrote the item the operator was about to
    read, and approving it then waived whatever was on disk last, not
    what was shown. The key now includes the waiver_key, so a repeat with
    different evidence opens its own item instead.
    """
    root, env = _failed_run(tmp_path, POLICY_TOML, DENIED)
    (item_a,) = _open(root, ItemKind.POLICY_EXCEPTION)
    assert item_a.evidence["location"] == "secrets/key.txt"

    # A retry with different evidence, same category, before item_a is approved.
    code, out = _retry(root, {**env, "AGENT_CMD": OTHER_DENIED})
    assert code == 1, out

    items = _open(root, ItemKind.POLICY_EXCEPTION)
    assert len(items) == 2, items
    assert {i.evidence["location"] for i in items} == {"secrets/key.txt", "secrets/other.txt"}
    assert item_a.id in {i.id for i in items}, "item A must survive unreplaced"

    _decide(root, env, "approve", item_a.id)

    # Approving A must not waive B: B is still the last thing on disk.
    code, out = _retry(root, {**env, "AGENT_CMD": OTHER_DENIED})

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert (finding.location, finding.severity) == ("secrets/other.txt", "high")
    assert f"waiver_refused:{item_a.id}" in finding.tags


def test_an_adequacy_repeat_with_different_evidence_opens_a_second_item_not_a_replacement(
    tmp_path: Path,
) -> None:
    """#595 B2's adequacy twin. Same defect as the policy version above:
    the dedupe key was category + location only, so a same-category,
    same-location repeat (a different test deleted from the same file)
    overwrote the item the operator was about to read. The key now
    includes the waiver_key, so a repeat with different evidence opens
    its own item instead.
    """
    root, env = _failed_run(tmp_path, ADEQUACY_TOML, ONE_TEST_DELETED)
    items_a = {i.evidence["category"]: i for i in _open(root, ItemKind.TEST_ADEQUACY)}
    item_a = items_a["adequacy_test_deleted"]
    assert item_a.evidence["location"] == "tests/test_core.py"

    # A retry that deletes the OTHER test in the same file, before item_a
    # is approved: same category, same location, different evidence.
    code, out = _retry(root, {**env, "AGENT_CMD": ANOTHER_TEST_DELETED})
    assert code == 1, out

    items = [
        i
        for i in _open(root, ItemKind.TEST_ADEQUACY)
        if i.evidence["category"] == "adequacy_test_deleted"
    ]
    assert len(items) == 2, items
    assert item_a.id in {i.id for i in items}, "item A must survive unreplaced"

    _decide(root, env, "approve", item_a.id)

    # Approving A must not waive B: B is still the last thing on disk.
    code, out = _retry(root, {**env, "AGENT_CMD": ANOTHER_TEST_DELETED})

    assert code == 1, out
    (finding,) = [f for f in _gated(root, "adequacy_") if f.category == "adequacy_test_deleted"]
    assert finding.severity == "high"
    assert f"waiver_refused:{item_a.id}" in finding.tags


def test_an_approval_does_not_cover_a_second_secret_in_the_same_file(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, SECRET_TOML, ONE_SECRET)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    assert item.evidence["category"] == "policy_secret_pattern"
    _decide(root, env, "approve", item.id)

    code, out = _retry(root, {**env, "AGENT_CMD": TWO_SECRETS})

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert (finding.location, finding.severity) == ("app/cfg.py", "high")
    assert f"waiver_refused:{item.id}" in finding.tags


def test_an_approval_does_not_cover_a_dependency_it_did_not_list(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, DEPS_TOML, _lockfile(TWENTY_ONE))
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    assert item.evidence["category"] == "policy_deps_allow_new"
    assert "pkg21" in item.detail, item.detail
    _decide(root, env, "approve", item.id)

    code, out = _retry(root, {**env, "AGENT_CMD": _lockfile([*TWENTY_ONE[:20], "pkgzz"])})

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert finding.severity == "high"
    assert f"waiver_refused:{item.id}" in finding.tags


def test_an_approval_does_not_cover_a_test_it_did_not_list(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, SYMBOL_TOML, _silent_tests(SIX))
    (item,) = _open(root, ItemKind.TEST_ADEQUACY)
    assert item.evidence["category"] == "adequacy_no_oracle"
    assert "test_silent_6" in item.detail, item.detail
    _decide(root, env, "approve", item.id)

    code, out = _retry(root, {**env, "AGENT_CMD": _silent_tests([*SIX[:5], "test_silent_7"])})

    assert code == 1, out
    (finding,) = _gated(root, "adequacy_")
    assert finding.severity == "high"
    assert f"waiver_refused:{item.id}" in finding.tags


def test_approving_one_of_two_adequacy_items_keeps_the_retry_failing(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, ADEQUACY_TOML, ONE_TEST_DELETED)
    items = {item.evidence["category"]: item for item in _open(root, ItemKind.TEST_ADEQUACY)}
    assert set(items) == {"adequacy_test_deleted", "adequacy_assertion_removed"}
    _decide(root, env, "approve", items["adequacy_test_deleted"].id)

    code, out = _retry(root, env)

    assert code == 1, out
    by_category = {f.category: f for f in _gated(root, "adequacy_")}
    assert by_category["adequacy_test_deleted"].severity == "advisory"
    assert by_category["adequacy_assertion_removed"].severity == "high"


def test_an_approval_filed_before_waivers_existed_is_refused(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, POLICY_TOML, DENIED)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    # A repeat refreshes an open item's evidence, which is how an item
    # filed before #595 (no waiver_key) is put back on disk.
    Inbox(root, InboxConfig.load(root)).add(
        ItemKind.POLICY_EXCEPTION,
        item.title,
        component=COMP,
        dedupe_key=item.dedupe_key,
        evidence={
            k: v
            for k, v in item.evidence.items()
            if k in {"category", "severity", "location", "suggestion"}
        },
    )
    out = _decide(root, env, "approve", item.id)
    assert "records approval only" in out, out

    code, out = _retry(root, env)

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert (finding.severity, f"waiver_refused:{item.id}" in finding.tags) == ("high", True)
    assert any("no evidence.waiver_key" in f for f in _verification_failures(root))


@pytest.mark.parametrize(
    "waiver_key",
    ["not-a-hex-digest", "g" * 64],
    ids=["wrong-length", "64-chars-not-hex"],
)
def test_an_approval_with_a_malformed_waiver_key_says_records_approval_only(
    tmp_path: Path, waiver_key: str
) -> None:
    """#595 B3: ``approval_effect`` must refuse exactly what the gate refuses.

    A hand-edited (or future-writer) item can carry a present but
    malformed ``waiver_key`` that the old ``approval_effect`` never
    checked, so it printed "waives this one finding" for evidence the
    gate was always going to refuse. The gate itself stays closed either
    way; only the shell's sentence is at risk. Two shapes of malformed:
    the wrong length, and exactly 64 characters but not hex - a plant
    that checks ``len(key) != 64`` instead of the hex pattern passes the
    first case and misses the second.
    """
    root, env = _failed_run(tmp_path, POLICY_TOML, DENIED)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    # A repeat refreshes an open item's evidence with a malformed key.
    Inbox(root, InboxConfig.load(root)).add(
        ItemKind.POLICY_EXCEPTION,
        item.title,
        component=COMP,
        dedupe_key=item.dedupe_key,
        evidence={**item.evidence, "waiver_key": waiver_key},
    )
    out = _decide(root, env, "approve", item.id)
    assert "records approval only" in out, out
    assert "waives this one finding" not in out, out

    code, out = _retry(root, env)

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert (finding.severity, f"waiver_refused:{item.id}" in finding.tags) == ("high", True)
    assert any("not a 64-character hex digest" in f for f in _verification_failures(root))


def test_an_approval_of_the_enforcement_machinery_halt_is_refused(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, POLICY_TOML, MACHINERY)
    (item,) = [
        i
        for i in _open(root, ItemKind.POLICY_EXCEPTION)
        if i.evidence["category"] == "policy_enforcement_machinery"
    ]
    out = _decide(root, env, "approve", item.id)
    assert "non-overridable" in out, out

    code, out = _retry(root, env)

    assert code == 1, out
    (finding,) = [
        f for f in _gated(root, "policy_") if f.category == "policy_enforcement_machinery"
    ]
    assert (finding.severity, f"waiver_refused:{item.id}" in finding.tags) == ("critical", True)


@pytest.mark.parametrize(
    ("field", "value"),
    [("projectName", "another"), ("specFile", "another.md"), ("planId", "another")],
    ids=["project", "spec_file", "plan_id"],
)
def test_an_approval_from_another_plan_is_refused(tmp_path: Path, field: str, value: str) -> None:
    root, env = _failed_run(tmp_path, POLICY_TOML, DENIED)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    _decide(root, env, "approve", item.id)
    raw = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
    if field == "planId":
        # A planned component starts from the copy its plan wrote.
        raw["components"][0]["planId"] = value
        planned = plan_prd_path(root, COMP, plan_id=value)
        planned.parent.mkdir(parents=True, exist_ok=True)
        prd = root / "scripts" / "kstrl" / "feature" / COMP / "prd.json"
        planned.write_text(prd.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        raw[field] = value
    _manifest_path(root).write_text(json.dumps(raw), encoding="utf-8")

    code, out = _retry(root, env)

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert (finding.severity, f"waiver_refused:{item.id}" in finding.tags) == ("high", True)
    assert any(
        "evidence.waiver_key does not match this run's" in f for f in _verification_failures(root)
    )


def test_a_torn_inbox_line_means_no_approval_is_applied(tmp_path: Path) -> None:
    """A line the fold cannot parse could be the rejection of the approval.

    So one unparseable line means no approval is consulted and the finding
    still blocks, and the failure says why.
    """
    root, env = _failed_run(tmp_path, POLICY_TOML, DENIED)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    _decide(root, env, "approve", item.id)
    inbox_path = Inbox(root, InboxConfig.load(root)).path
    with inbox_path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": "torn\n')

    code, out = _retry(root, env)

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert finding.severity == "high"
    assert any("approvals were not consulted" in f for f in _verification_failures(root))


async def test_the_inbox_screen_says_what_approve_and_reject_do(tmp_path: Path) -> None:
    """The TUI's choice list takes its approve and reject sentences from the
    same function ``ks inbox approve`` prints, for an item the real run filed."""
    root, _env_unused = _failed_run(tmp_path, POLICY_TOML, DENIED)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    app = home_app(root)
    async with app.run_test(size=(120, 36)) as pilot:
        await mounted(pilot, lambda: app.screen, "#home-runs")
        app.push_screen(InboxScreen())
        screen = app.screen
        assert isinstance(screen, InboxScreen)
        detail = cast(Static, await mounted(pilot, lambda: screen, "#inbox-detail"))
        await settled(pilot, lambda: bool(screen._items), what="the inbox screen to read the log")
        row = [i.id for i in screen._items].index(item.id)
        screen.query_one("#inbox-table", DataTable).move_cursor(row=row)
        await settled(
            pilot,
            lambda: f"id {item.id[:8]}" in flat(detail) and "what each choice does" in flat(detail),
            what="the policy item's choices",
        )
        text = " ".join(flat(detail).split())
    assert "waives this one finding" in text, text
    assert "No waiver: the next run fails on this finding as before." in text, text
    assert "no kstrl step reads" not in text, text


def test_an_approval_made_during_a_run_waits_for_the_next_run(tmp_path: Path) -> None:
    """The engineer approves its own item between two attempts of one run.

    The run read the approvals when it started, so attempt 2 still fails.
    The approval itself is valid: the next run applies it.
    """
    approve_open = (
        f'"{sys.executable}" -c "import sys; from pathlib import Path; '
        "from kstrl.inbox import Inbox, InboxConfig; "
        "box = Inbox(Path(sys.argv[1]), InboxConfig()); "
        "[box.approve(i.id, actor='engineer') for i in box.open_items() "
        'if str(i.kind) == \'policy_exception\']" "$WAIVER_TEST_ROOT"'
    )
    root = _repo(tmp_path, POLICY_TOML)
    env = _env(
        tmp_path,
        _engineer(f"{approve_open} && mkdir -p secrets && printf 'k\\n' > secrets/key.txt"),
    )
    env["WAIVER_TEST_ROOT"] = str(root)

    code, out = _factory(root, env, "--max-retries", "1")

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert finding.severity == "high"
    box = Inbox(root, InboxConfig.load(root))
    assert [str(i.status) for i in box.items() if i.kind is ItemKind.POLICY_EXCEPTION] == [
        "approved",
        "open",
    ]
